#include "docx_comment_parser.h"
#include "zip_reader.h"
#include "xml_parser.h"

#include <algorithm>
#include <set>
#include <unordered_map>
#include <functional>

namespace docx {

namespace {

std::string collect_wt_text(const XmlNode& node) {
    std::string result;
    std::function<void(const XmlNode&)> walk = [&](const XmlNode& n) {
        for (const auto& c : n.children) {
            if (c->local == "t") { result += node_text(*c); }
            else { walk(*c); }
        }
    };
    walk(node);
    return result;
}

std::string first_para_style(const XmlNode& comment_node) {
    for (const auto& c : comment_node.children) {
        if (c->local != "p") continue;
        for (const auto& pc : c->children) {
            if (pc->local != "pPr") continue;
            for (const auto& ppc : pc->children)
                if (ppc->local == "pStyle") return ppc->attr("val");
        }
    }
    return {};
}

void parse_comments_xml(const std::vector<char>& buf,
                         std::vector<CommentMetadata>& out,
                         std::unordered_map<int, std::size_t>& id_to_idx)
{
    auto doc = dom_parse(buf.data(), buf.size());
    if (!doc) throw DocxFormatError("Failed to parse word/comments.xml");

    for (const auto& c : doc->children) {
        if (c->local != "comment") continue;
        std::string id_str = c->attr("id");
        if (id_str.empty()) continue;
        int id = -1;
        try { id = std::stoi(id_str); } catch (...) { continue; }
        CommentMetadata m;
        m.id    = id;
        m.author   = c->attr("author");
        m.date     = c->attr("date");
        m.initials = c->attr("initials");
        m.text     = collect_wt_text(*c);
        m.paragraph_style = first_para_style(*c);
        for (const auto& pc : c->children)
            if (pc->local == "p") { m.para_id = pc->attr("paraId"); break; }
        id_to_idx[m.id] = out.size();
        out.push_back(std::move(m));
    }
}

void parse_comments_extended(const std::vector<char>& buf,
                               std::vector<CommentMetadata>& comments)
{
    std::unordered_map<std::string, std::size_t> para_id_map;
    para_id_map.reserve(comments.size());
    for (std::size_t i = 0; i < comments.size(); ++i)
        if (!comments[i].para_id.empty()) para_id_map[comments[i].para_id] = i;

    SaxHandler h;
    h.start_element = [&](std::string_view, std::string_view local,
                           const XmlAttrs& attrs) {
        if (local != "commentEx") return;
        std::string para_id, para_par, done_str;
        for (const auto& a : attrs) {
            if      (a.local == "paraId")       para_id  = a.value;
            else if (a.local == "paraIdParent") para_par = a.value;
            else if (a.local == "done")         done_str = a.value;
        }
        if (para_id.empty()) return;
        auto it = para_id_map.find(para_id);
        if (it == para_id_map.end()) return;
        comments[it->second].para_id_parent = para_par;
        comments[it->second].done           = (done_str == "1");
    };
    sax_parse(buf.data(), buf.size(), h);
}

void parse_comments_ids(const std::vector<char>& buf,
                         std::vector<CommentMetadata>& comments)
{
    SaxHandler h;
    h.start_element = [&](std::string_view, std::string_view local,
                           const XmlAttrs& attrs) {
        if (local != "commentId") return;
        std::string id_str, para_id;
        for (const auto& a : attrs) {
            if      (a.local == "id")     id_str  = a.value;
            else if (a.local == "paraId") para_id = a.value;
        }
        if (id_str.empty() || para_id.empty()) return;
        int id{-1};
        try { id = std::stoi(id_str); } catch (...) { return; }
        for (auto& m : comments)
            if (m.id == id && m.para_id.empty()) { m.para_id = para_id; break; }
    };
    sax_parse(buf.data(), buf.size(), h);
}

void resolve_threads(std::vector<CommentMetadata>& comments,
                     const std::unordered_map<int, std::size_t>& id_to_idx)
{
    std::unordered_map<std::string, std::size_t> para_id_map;
    para_id_map.reserve(comments.size());
    for (std::size_t i = 0; i < comments.size(); ++i)
        if (!comments[i].para_id.empty()) para_id_map[comments[i].para_id] = i;

    for (auto& m : comments) {
        if (m.para_id_parent.empty()) continue;
        auto it = para_id_map.find(m.para_id_parent);
        if (it == para_id_map.end()) continue;
        m.parent_id = comments[it->second].id;
        m.is_reply  = true;
    }

    for (auto& m : comments) {
        if (!m.is_reply || m.parent_id < 0) continue;
        auto it = id_to_idx.find(m.parent_id);
        if (it == id_to_idx.end()) continue;
        CommentRef ref;
        ref.id           = m.id;
        ref.author       = m.author;
        ref.date         = m.date;
        ref.text_snippet = truncate_utf8(m.text, 120);
        comments[it->second].replies.push_back(std::move(ref));
    }

    for (auto& m : comments) {
        if (m.is_reply) continue;
        std::vector<int> chain{m.id};
        for (std::size_t head = 0; head < chain.size(); ++head) {
            auto it = id_to_idx.find(chain[head]);
            if (it == id_to_idx.end()) continue;
            for (const auto& ref : comments[it->second].replies)
                chain.push_back(ref.id);
        }
        m.thread_ids = std::move(chain);
    }
}

void parse_document_body(const std::vector<char>& buf,
                          std::vector<CommentMetadata>& comments,
                          const std::unordered_map<int, std::size_t>& id_to_idx)
{
    std::unordered_map<int, std::string> anchor_text;
    anchor_text.reserve(comments.size());

    bool in_run = false;
    std::string run_text;
    std::unordered_map<int, std::string> range_text;

    SaxHandler h;
    h.start_element = [&](std::string_view, std::string_view local,
                           const XmlAttrs& attrs) {
        if (local == "r") { in_run = true; run_text.clear(); }
        else if (local == "commentRangeStart") {
            for (const auto& a : attrs)
                if (a.local == "id") {
                    try { range_text[std::stoi(a.value)] = {}; } catch(...) {}
                    break;
                }
        } else if (local == "commentRangeEnd") {
            for (const auto& a : attrs)
                if (a.local == "id") {
                    try {
                        int id = std::stoi(a.value);
                        auto it = range_text.find(id);
                        if (it != range_text.end()) {
                            anchor_text[id] = truncate_utf8(it->second, 240);
                            range_text.erase(it);
                        }
                    } catch(...) {}
                    break;
                }
        }
    };
    h.end_element = [&](std::string_view, std::string_view local) {
        if (local == "r") {
            in_run = false;
            for (auto& kv : range_text) kv.second += run_text;
            run_text.clear();
        }
    };
    h.characters = [&](std::string_view text) {
        if (in_run) run_text.append(text);
    };

    sax_parse(buf.data(), buf.size(), h);

    for (auto& kv : anchor_text) {
        auto it = id_to_idx.find(kv.first);
        if (it != id_to_idx.end())
            comments[it->second].referenced_text = std::move(kv.second);
    }
}

DocumentCommentStats compute_stats(const std::string& path,
                                    const std::vector<CommentMetadata>& comments)
{
    DocumentCommentStats s;
    s.file_path      = path;
    s.total_comments = comments.size();
    std::set<std::string> authors;
    for (const auto& m : comments) {
        authors.insert(m.author);
        if (m.done)     ++s.total_resolved;
        if (m.is_reply) ++s.total_replies;
        else            ++s.total_root_comments;
        if (s.earliest_date.empty() || (!m.date.empty() && m.date < s.earliest_date))
            s.earliest_date = m.date;
        if (s.latest_date.empty()   || (!m.date.empty() && m.date > s.latest_date))
            s.latest_date   = m.date;
    }
    s.unique_authors.assign(authors.begin(), authors.end());
    return s;
}

} // anonymous namespace

struct DocxParser::Impl {
    std::vector<CommentMetadata>         comments_;
    std::unordered_map<int, std::size_t> id_to_idx_;
    DocumentCommentStats                 stats_;

    void parse(const std::string& path) {
        comments_.clear(); id_to_idx_.clear();
        ZipReader zip;
        try { zip.open(path); } catch (const ZipError& e) { throw DocxFileError(e.what()); }
        if (!zip.has_entry("word/comments.xml")) return;
        { auto buf = zip.read_entry("word/comments.xml"); buf.push_back('\0'); parse_comments_xml(buf, comments_, id_to_idx_); }
        if (comments_.empty()) return;
        if (zip.has_entry("word/commentsExtended.xml")) { auto buf = zip.read_entry("word/commentsExtended.xml"); buf.push_back('\0'); parse_comments_extended(buf, comments_); }
        if (zip.has_entry("word/commentsIds.xml")) { auto buf = zip.read_entry("word/commentsIds.xml"); buf.push_back('\0'); parse_comments_ids(buf, comments_); }
        resolve_threads(comments_, id_to_idx_);
        if (zip.has_entry("word/document.xml")) { auto buf = zip.read_entry("word/document.xml"); buf.push_back('\0'); parse_document_body(buf, comments_, id_to_idx_); }
        std::sort(comments_.begin(), comments_.end(), [](const CommentMetadata& a, const CommentMetadata& b){ return a.id < b.id; });
        id_to_idx_.clear();
        for (std::size_t i = 0; i < comments_.size(); ++i) id_to_idx_[comments_[i].id] = i;
        stats_ = compute_stats(path, comments_);
    }
};

DocxParser::DocxParser()  : impl_(std::make_unique<Impl>()) {}
DocxParser::~DocxParser() = default;
DocxParser::DocxParser(DocxParser&&) noexcept = default;
DocxParser& DocxParser::operator=(DocxParser&&) noexcept = default;

void DocxParser::parse(const std::string& file_path) { impl_->parse(file_path); }
const std::vector<CommentMetadata>& DocxParser::comments() const noexcept { return impl_->comments_; }
const DocumentCommentStats& DocxParser::stats() const noexcept { return impl_->stats_; }

const CommentMetadata* DocxParser::find_by_id(int id) const noexcept {
    auto it = impl_->id_to_idx_.find(id);
    return it == impl_->id_to_idx_.end() ? nullptr : &impl_->comments_[it->second];
}

std::vector<const CommentMetadata*> DocxParser::by_author(const std::string& author) const {
    std::vector<const CommentMetadata*> r;
    for (const auto& m : impl_->comments_) if (m.author == author) r.push_back(&m);
    return r;
}

std::vector<const CommentMetadata*> DocxParser::root_comments() const {
    std::vector<const CommentMetadata*> r;
    for (const auto& m : impl_->comments_) if (!m.is_reply) r.push_back(&m);
    return r;
}

std::vector<const CommentMetadata*> DocxParser::thread(int root_id) const {
    const CommentMetadata* root = find_by_id(root_id);
    if (!root) return {};
    std::vector<const CommentMetadata*> r;
    for (int id : root->thread_ids)
        if (auto* m = find_by_id(id)) r.push_back(m);
    return r;
}

} // namespace docx
