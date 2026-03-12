#include "xml_parser.h"
#include <cctype>
#include <cstring>
#include <stack>

namespace docx {

// ─── Character helpers ────────────────────────────────────────────────────────

static inline bool is_name_start(char c) {
    return std::isalpha(static_cast<unsigned char>(c)) || c == '_' || c == ':' ||
           (static_cast<unsigned char>(c) >= 0x80); // allow UTF-8 high bytes
}
static inline bool is_name_char(char c) {
    return is_name_start(c) || std::isdigit(static_cast<unsigned char>(c)) ||
           c == '-' || c == '.';
}
static inline void skip_ws(const char*& p, const char* end) {
    while (p < end && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) ++p;
}

// ─── Entity decode ────────────────────────────────────────────────────────────

static void decode_entities(const char* s, std::size_t len, std::string& out) {
    out.reserve(out.size() + len);
    const char* end = s + len;
    while (s < end) {
        if (*s != '&') { out += *s++; continue; }
        ++s; // skip '&'
        const char* semi = static_cast<const char*>(std::memchr(s, ';', static_cast<std::size_t>(end - s)));
        if (!semi) { out += '&'; continue; }
        std::string_view ref(s, static_cast<std::size_t>(semi - s));
        if      (ref == "amp")  out += '&';
        else if (ref == "lt")   out += '<';
        else if (ref == "gt")   out += '>';
        else if (ref == "quot") out += '"';
        else if (ref == "apos") out += '\'';
        else if (!ref.empty() && ref[0] == '#') {
            // Numeric character reference
            unsigned long cp = 0;
            if (ref.size() > 1 && ref[1] == 'x')
                cp = std::strtoul(std::string(ref.substr(2)).c_str(), nullptr, 16);
            else
                cp = std::strtoul(std::string(ref.substr(1)).c_str(), nullptr, 10);
            // Encode as UTF-8
            if (cp < 0x80) {
                out += static_cast<char>(cp);
            } else if (cp < 0x800) {
                out += static_cast<char>(0xC0 | (cp >> 6));
                out += static_cast<char>(0x80 | (cp & 0x3F));
            } else if (cp < 0x10000) {
                out += static_cast<char>(0xE0 | (cp >> 12));
                out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
                out += static_cast<char>(0x80 | (cp & 0x3F));
            } else {
                out += static_cast<char>(0xF0 | (cp >> 18));
                out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
                out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
                out += static_cast<char>(0x80 | (cp & 0x3F));
            }
        } else {
            // Unknown entity – emit raw
            out += '&';
            out.append(ref);
            out += ';';
        }
        s = semi + 1;
    }
}

// ─── Parse qualified name ─────────────────────────────────────────────────────

static bool parse_qname(const char*& p, const char* end,
                         std::string& prefix, std::string& local) {
    if (p >= end || !is_name_start(*p)) return false;
    const char* start = p;
    while (p < end && is_name_char(*p)) ++p;
    std::string_view full(start, static_cast<std::size_t>(p - start));
    auto colon = full.find(':');
    if (colon == std::string_view::npos) {
        prefix.clear();
        local.assign(full);
    } else {
        prefix.assign(full.substr(0, colon));
        local.assign(full.substr(colon + 1));
    }
    return true;
}

// ─── Parse attribute value ────────────────────────────────────────────────────

static bool parse_attr_value(const char*& p, const char* end, std::string& out) {
    if (p >= end) return false;
    char quote = *p;
    if (quote != '"' && quote != '\'') return false;
    ++p;
    const char* start = p;
    while (p < end && *p != quote) ++p;
    if (p >= end) return false;
    decode_entities(start, static_cast<std::size_t>(p - start), out);
    ++p;  // skip closing quote
    return true;
}

// ─── SAX parser ───────────────────────────────────────────────────────────────

bool sax_parse(const char* data, std::size_t len, SaxHandler& h) {
    const char* p   = data;
    const char* end = data + len;

    // Reusable buffers to reduce allocations
    std::string prefix, local, text_buf;

    while (p < end) {
        if (*p != '<') {
            // Collect character data
            const char* start = p;
            while (p < end && *p != '<') ++p;
            if (h.characters && p > start) {
                text_buf.clear();
                decode_entities(start, static_cast<std::size_t>(p - start), text_buf);
                if (!text_buf.empty()) h.characters(text_buf);
            }
            continue;
        }

        ++p; // skip '<'
        if (p >= end) break;

        // Comment
        if (p + 2 < end && p[0] == '!' && p[1] == '-' && p[2] == '-') {
            p += 3;
            while (p + 2 < end && !(p[0]=='-' && p[1]=='-' && p[2]=='>')) ++p;
            if (p + 2 < end) p += 3;
            continue;
        }

        // CDATA
        if (p + 8 < end && std::strncmp(p, "![CDATA[", 8) == 0) {
            p += 8;
            const char* cdata_start = p;
            while (p + 2 < end && !(p[0]==']' && p[1]==']' && p[2]=='>')) ++p;
            if (h.characters && p > cdata_start)
                h.characters(std::string_view(cdata_start, static_cast<std::size_t>(p - cdata_start)));
            if (p + 2 < end) p += 3;
            continue;
        }

        // Processing instruction or XML declaration
        if (*p == '?') {
            while (p < end && *p != '>') ++p;
            if (p < end) ++p;
            continue;
        }

        // DOCTYPE
        if (p + 7 < end && std::strncmp(p, "!DOCTYPE", 8) == 0) {
            int depth = 1;
            ++p;
            while (p < end && depth > 0) {
                if (*p == '<') ++depth;
                else if (*p == '>') --depth;
                ++p;
            }
            continue;
        }

        // End element
        if (*p == '/') {
            ++p;
            prefix.clear(); local.clear();
            parse_qname(p, end, prefix, local);
            skip_ws(p, end);
            if (p < end && *p == '>') ++p;
            if (h.end_element) h.end_element(prefix, local);
            continue;
        }

        // Start element
        prefix.clear(); local.clear();
        if (!parse_qname(p, end, prefix, local)) {
            // Skip malformed tag
            while (p < end && *p != '>') ++p;
            if (p < end) ++p;
            continue;
        }

        XmlAttrs attrs;
        bool self_closing = false;

        while (p < end) {
            skip_ws(p, end);
            if (p >= end) break;
            if (*p == '>') { ++p; break; }
            if (*p == '/' && p + 1 < end && p[1] == '>') {
                self_closing = true; p += 2; break;
            }
            // Attribute
            std::string attr_prefix, attr_local;
            if (!parse_qname(p, end, attr_prefix, attr_local)) {
                // Skip garbage char
                ++p; continue;
            }
            skip_ws(p, end);
            if (p < end && *p == '=') {
                ++p;
                skip_ws(p, end);
                XmlAttr a;
                a.prefix = std::move(attr_prefix);
                a.local  = std::move(attr_local);
                parse_attr_value(p, end, a.value);
                attrs.push_back(std::move(a));
            }
            // Else: standalone attribute (rare in OOXML) – ignore
        }

        if (h.start_element) h.start_element(prefix, local, attrs);
        if (self_closing && h.end_element) h.end_element(prefix, local);
    }

    return true;
}

// ─── DOM builder ─────────────────────────────────────────────────────────────

std::unique_ptr<XmlNode> dom_parse(const char* data, std::size_t len) {
    auto root = std::make_unique<XmlNode>();
    root->local = "#document";

    std::stack<XmlNode*> stack;
    stack.push(root.get());

    SaxHandler h;

    h.start_element = [&](std::string_view pre, std::string_view loc,
                           const XmlAttrs& attrs) {
        if (stack.empty()) return;
        auto node = std::make_unique<XmlNode>();
        node->prefix.assign(pre);
        node->local.assign(loc);
        node->attrs = attrs;
        XmlNode* raw = node.get();
        stack.top()->children.push_back(std::move(node));
        stack.push(raw);
    };

    h.end_element = [&](std::string_view, std::string_view) {
        if (stack.size() > 1) stack.pop();
    };

    h.characters = [&](std::string_view text) {
        if (!stack.empty())
            stack.top()->text += text;
    };

    sax_parse(data, len, h);

    // Return first real child of #document (the root element)
    if (!root->children.empty())
        return std::move(root->children[0]);
    return nullptr;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

std::string node_text(const XmlNode& node) {
    std::string result = node.text;
    for (const auto& c : node.children)
        result += node_text(*c);
    return result;
}

std::string truncate_utf8(const std::string& s, std::size_t max_bytes) {
    if (s.size() <= max_bytes) return s;
    std::size_t i = max_bytes;
    while (i > 0 && (static_cast<unsigned char>(s[i]) & 0xC0) == 0x80) --i;
    return s.substr(0, i) + "\xe2\x80\xa6"; // UTF-8 ellipsis
}

} // namespace docx
