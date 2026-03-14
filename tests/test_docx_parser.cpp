/**
 * Self-contained test suite for docx_comment_parser.
 *
 * Creates a synthetic .docx in-memory (using zlib to deflate XML parts),
 * writes it to a temp file, then exercises the parser API.
 *
 * No external test framework required.
 */

#include "docx_comment_parser.h"

#include <cassert>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
// On MSVC the test's in-memory ZIP builder uses crc32(), Bytef, and uInt.
// The vendored header supplies all three without requiring a system zlib.
// On Linux/macOS/MinGW the system <zlib.h> is used (no IMPLEMENTATION define,
// so the function bodies live only in zip_reader.cpp's TU).
#ifdef _MSC_VER
#  include "../vendor/zlib/zlib.h"
#else
#  include <zlib.h>
#endif

// ─── Minimal ZIP writer (stored entries only – good enough for testing) ───────

static void write_le16(std::vector<uint8_t>& v, uint16_t x) {
    v.push_back(x & 0xFF); v.push_back(x >> 8);
}
static void write_le32(std::vector<uint8_t>& v, uint32_t x) {
    for (int i = 0; i < 4; ++i, x >>= 8) v.push_back(x & 0xFF);
}

struct ZipBuilder {
    struct Entry {
        std::string name;
        std::vector<uint8_t> data;
        uint32_t offset{0};
        uint32_t crc{0};
    };
    std::vector<Entry> entries;

    void add(const std::string& name, const std::string& content) {
        Entry e;
        e.name.assign(name);
        e.data.assign(content.begin(), content.end());
        e.crc  = crc32(0, reinterpret_cast<const Bytef*>(content.data()),
                       static_cast<uInt>(content.size()));
        entries.push_back(std::move(e));
    }

    std::vector<uint8_t> build() {
        std::vector<uint8_t> out;
        out.reserve(8192);

        for (auto& e : entries) {
            e.offset = static_cast<uint32_t>(out.size());
            // Local file header
            write_le32(out, 0x04034b50u);   // sig
            write_le16(out, 20);             // version needed
            write_le16(out, 0);              // flags
            write_le16(out, 0);              // method: stored
            write_le16(out, 0);              // mod time
            write_le16(out, 0);              // mod date
            write_le32(out, e.crc);
            write_le32(out, static_cast<uint32_t>(e.data.size())); // comp size
            write_le32(out, static_cast<uint32_t>(e.data.size())); // uncomp size
            write_le16(out, static_cast<uint16_t>(e.name.size()));
            write_le16(out, 0);              // extra len
            for (char c : e.name) out.push_back(static_cast<uint8_t>(c));
            out.insert(out.end(), e.data.begin(), e.data.end());
        }

        uint32_t cd_start = static_cast<uint32_t>(out.size());
        uint16_t cd_count = static_cast<uint16_t>(entries.size());

        for (const auto& e : entries) {
            write_le32(out, 0x02014b50u);    // CD sig
            write_le16(out, 20);             // version made by
            write_le16(out, 20);             // version needed
            write_le16(out, 0);              // flags
            write_le16(out, 0);              // method: stored
            write_le16(out, 0);
            write_le16(out, 0);
            write_le32(out, e.crc);
            write_le32(out, static_cast<uint32_t>(e.data.size()));
            write_le32(out, static_cast<uint32_t>(e.data.size()));
            write_le16(out, static_cast<uint16_t>(e.name.size()));
            write_le16(out, 0); write_le16(out, 0); // extra, comment
            write_le16(out, 0); write_le16(out, 0); // disk, int attrib
            write_le32(out, 0);              // ext attrib
            write_le32(out, e.offset);
            for (char c : e.name) out.push_back(static_cast<uint8_t>(c));
        }

        uint32_t cd_size = static_cast<uint32_t>(out.size()) - cd_start;

        // EOCD
        write_le32(out, 0x06054b50u);
        write_le16(out, 0); write_le16(out, 0);   // disk numbers
        write_le16(out, cd_count);
        write_le16(out, cd_count);
        write_le32(out, cd_size);
        write_le32(out, cd_start);
        write_le16(out, 0);   // comment len

        return out;
    }
};

// ─── XML fixtures ────────────────────────────────────────────────────────────

static const char* CONTENT_TYPES = R"(<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml"  ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/comments.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
</Types>)";

static const char* RELS = R"(<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>)";

static const char* WORD_RELS = R"(<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
    Target="comments.xml"/>
</Relationships>)";

// comments.xml: two comments, second is a reply (para_id linkage via commentsExtended)
static const char* COMMENTS_XML = R"(<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:comment w:id="0" w:author="Alice Tester" w:date="2024-03-01T10:00:00Z" w:initials="AT">
    <w:p w14:paraId="AAAA0001">
      <w:pPr><w:pStyle w:val="CommentText"/></w:pPr>
      <w:r><w:t>This sentence needs revision.</w:t></w:r>
    </w:p>
  </w:comment>
  <w:comment w:id="1" w:author="Bob Reviewer" w:date="2024-03-02T14:30:00Z" w:initials="BR">
    <w:p w14:paraId="AAAA0002">
      <w:r><w:t>Agreed, let me fix this.</w:t></w:r>
    </w:p>
  </w:comment>
  <w:comment w:id="2" w:author="Alice Tester" w:date="2024-03-03T09:00:00Z" w:initials="AT">
    <w:p w14:paraId="AAAA0003">
      <w:r><w:t>Thanks for the quick fix!</w:t></w:r>
    </w:p>
  </w:comment>
</w:comments>)";

// commentsExtended.xml: comments 1 and 2 are replies to comment 0
static const char* COMMENTS_EXT_XML = R"(<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w16cex:commentsEx xmlns:w16cex="http://schemas.microsoft.com/office/word/2018/wordml/cex">
  <w16cex:commentEx w16cex:paraId="AAAA0001" w16cex:done="0"/>
  <w16cex:commentEx w16cex:paraId="AAAA0002" w16cex:paraIdParent="AAAA0001" w16cex:done="0"/>
  <w16cex:commentEx w16cex:paraId="AAAA0003" w16cex:paraIdParent="AAAA0001" w16cex:done="1"/>
</w16cex:commentsEx>)";

// document.xml: a paragraph anchored to comment 0
static const char* DOCUMENT_XML = R"(<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:commentRangeStart w:id="0"/>
      <w:r><w:t xml:space="preserve">The quick brown fox </w:t></w:r>
      <w:r><w:t>jumps over the lazy dog.</w:t></w:r>
      <w:commentRangeEnd w:id="0"/>
      <w:r><w:commentReference w:id="0"/></w:r>
    </w:p>
  </w:body>
</w:document>)";

// ─── Helpers ─────────────────────────────────────────────────────────────────

static std::string make_temp_docx() {
    ZipBuilder zb;
    zb.add("[Content_Types].xml",            CONTENT_TYPES);
    zb.add("_rels/.rels",                    RELS);
    zb.add("word/_rels/document.xml.rels",   WORD_RELS);
    zb.add("word/comments.xml",              COMMENTS_XML);
    zb.add("word/commentsExtended.xml",      COMMENTS_EXT_XML);
    zb.add("word/document.xml",              DOCUMENT_XML);

    auto bytes = zb.build();

    // Write to temp file
    std::string path = "/tmp/test_docx_parser_fixture.docx";
    std::ofstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("Cannot create temp file: " + path);
    f.write(reinterpret_cast<const char*>(bytes.data()), bytes.size());
    return path;
}

// ─── Assertion helpers ────────────────────────────────────────────────────────

static int g_passed = 0;
static int g_failed = 0;

#define CHECK(expr) \
    do { \
        if (!(expr)) { \
            std::cerr << "FAIL [" << __LINE__ << "] " #expr "\n"; \
            ++g_failed; \
        } else { \
            ++g_passed; \
        } \
    } while(0)

#define CHECK_EQ(a, b) \
    do { \
        if ((a) != (b)) { \
            std::cerr << "FAIL [" << __LINE__ << "] " #a " == " #b \
                      << "  (got: " << (a) << " vs " << (b) << ")\n"; \
            ++g_failed; \
        } else { \
            ++g_passed; \
        } \
    } while(0)

// ─── Tests ────────────────────────────────────────────────────────────────────

void test_basic_parsing(const std::string& path) {
    std::cout << "\n=== test_basic_parsing ===\n";
    docx::DocxParser p;
    p.parse(path);

    const auto& comments = p.comments();
    CHECK_EQ(comments.size(), 3u);

    const auto* c0 = p.find_by_id(0);
    CHECK(c0 != nullptr);
    CHECK_EQ(c0->author,   std::string("Alice Tester"));
    CHECK_EQ(c0->initials, std::string("AT"));
    CHECK_EQ(c0->date,     std::string("2024-03-01T10:00:00Z"));
    CHECK(c0->text.find("revision") != std::string::npos);
    CHECK_EQ(c0->paragraph_style, std::string("CommentText"));
}

void test_threading(const std::string& path) {
    std::cout << "\n=== test_threading ===\n";
    docx::DocxParser p;
    p.parse(path);

    const auto* c0 = p.find_by_id(0);
    const auto* c1 = p.find_by_id(1);
    const auto* c2 = p.find_by_id(2);

    CHECK(c0 && !c0->is_reply);
    CHECK(c1 &&  c1->is_reply);
    CHECK_EQ(c1->parent_id, 0);
    CHECK(c2 &&  c2->is_reply);
    CHECK_EQ(c2->parent_id, 0);

    // Replies on root
    CHECK_EQ(c0->replies.size(), 2u);

    // Thread chain
    CHECK_EQ(c0->thread_ids.size(), 3u);
    CHECK_EQ(c0->thread_ids[0], 0);

    auto thread = p.thread(0);
    CHECK_EQ(thread.size(), 3u);
}

void test_done_flag(const std::string& path) {
    std::cout << "\n=== test_done_flag ===\n";
    docx::DocxParser p;
    p.parse(path);

    const auto* c2 = p.find_by_id(2);
    CHECK(c2 != nullptr);
    CHECK(c2->done);

    const auto* c0 = p.find_by_id(0);
    CHECK(c0 && !c0->done);
}

void test_anchor_text(const std::string& path) {
    std::cout << "\n=== test_anchor_text ===\n";
    docx::DocxParser p;
    p.parse(path);

    const auto* c0 = p.find_by_id(0);
    CHECK(c0 != nullptr);
    CHECK(!c0->referenced_text.empty());
    CHECK(c0->referenced_text.find("quick brown fox") != std::string::npos);
}

void test_by_author(const std::string& path) {
    std::cout << "\n=== test_by_author ===\n";
    docx::DocxParser p;
    p.parse(path);

    auto alice = p.by_author("Alice Tester");
    CHECK_EQ(alice.size(), 2u);

    auto bob = p.by_author("Bob Reviewer");
    CHECK_EQ(bob.size(), 1u);

    auto nobody = p.by_author("Nobody");
    CHECK(nobody.empty());
}

void test_stats(const std::string& path) {
    std::cout << "\n=== test_stats ===\n";
    docx::DocxParser p;
    p.parse(path);

    const auto& s = p.stats();
    CHECK_EQ(s.total_comments,     3u);
    CHECK_EQ(s.total_replies,      2u);
    CHECK_EQ(s.total_root_comments,1u);
    CHECK_EQ(s.total_resolved,     1u);
    CHECK_EQ(s.unique_authors.size(), 2u);
    CHECK_EQ(s.earliest_date, std::string("2024-03-01T10:00:00Z"));
    CHECK_EQ(s.latest_date,   std::string("2024-03-03T09:00:00Z"));
}

void test_root_comments(const std::string& path) {
    std::cout << "\n=== test_root_comments ===\n";
    docx::DocxParser p;
    p.parse(path);

    auto roots = p.root_comments();
    CHECK_EQ(roots.size(), 1u);
    CHECK_EQ(roots[0]->id, 0);
}

void test_batch_parser(const std::string& path) {
    std::cout << "\n=== test_batch_parser ===\n";
    docx::BatchParser bp(2);
    bp.parse_all({path, path});   // same file twice — valid

    CHECK_EQ(bp.errors().size(), 0u);
    CHECK_EQ(bp.comments(path).size(), 3u);
    CHECK_EQ(bp.stats(path).total_comments, 3u);

    bp.release_all();
}

void test_missing_file() {
    std::cout << "\n=== test_missing_file ===\n";
    docx::DocxParser p;
    bool threw = false;
    try {
        p.parse("/nonexistent/path/file.docx");
    } catch (const docx::DocxFileError&) {
        threw = true;
    } catch (...) {}
    CHECK(threw);
}

// ─── main ─────────────────────────────────────────────────────────────────────

int main() {
    std::string path;
    try {
        path = make_temp_docx();
        std::cout << "Test fixture: " << path << "\n";
    } catch (const std::exception& ex) {
        std::cerr << "Failed to create fixture: " << ex.what() << "\n";
        return 1;
    }

    test_basic_parsing(path);
    test_threading(path);
    test_done_flag(path);
    test_anchor_text(path);
    test_by_author(path);
    test_stats(path);
    test_root_comments(path);
    test_batch_parser(path);
    test_missing_file();

    std::remove(path.c_str());

    std::cout << "\n──────────────────────────────\n";
    std::cout << "Results: " << g_passed << " passed, " << g_failed << " failed\n";
    return (g_failed == 0) ? 0 : 1;
}
