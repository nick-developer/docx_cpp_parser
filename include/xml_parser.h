#pragma once
/**
 * Minimal, allocation-efficient XML parser for OOXML use.
 *
 * Supports the subset of XML found in .docx files:
 *   - UTF-8 content
 *   - Namespaced elements/attributes (prefix:local)
 *   - Entity references: &amp; &lt; &gt; &quot; &apos;
 *   - CDATA sections
 *   - Comments (skipped)
 *   - Processing instructions (skipped)
 *
 * Uses a SAX-style callback interface to avoid building a full DOM tree,
 * except for a small DOM builder used for comments.xml.
 *
 * No external dependencies beyond <string>, <vector>, <functional>.
 */

#include <string>
#include <string_view>
#include <vector>
#include <functional>
#include <unordered_map>
#include <memory>
#include <stdexcept>
#include <cstring>
#include <cassert>

namespace docx {

// ─── Attribute ────────────────────────────────────────────────────────────────

struct XmlAttr {
    std::string prefix;   // may be empty
    std::string local;    // local name
    std::string value;
    std::string qname() const { return prefix.empty() ? local : prefix + ":" + local; }
};

using XmlAttrs = std::vector<XmlAttr>;

// ─── SAX callbacks ───────────────────────────────────────────────────────────

struct SaxHandler {
    // prefix:local  (prefix may be empty)
    std::function<void(std::string_view prefix, std::string_view local,
                       const XmlAttrs& attrs)>                   start_element;
    std::function<void(std::string_view prefix, std::string_view local)> end_element;
    std::function<void(std::string_view text)>                   characters;
};

// ─── SAX parser ──────────────────────────────────────────────────────────────

/**
 * Stream a null-terminated (or length-bounded) XML buffer through SAX callbacks.
 * Returns false on unrecoverable parse error.
 */
bool sax_parse(const char* data, std::size_t len, SaxHandler& handler);

// ─── Minimal DOM ─────────────────────────────────────────────────────────────

struct XmlNode {
    std::string              prefix;
    std::string              local;
    XmlAttrs                 attrs;
    std::string              text;         // concatenated character data
    std::vector<std::unique_ptr<XmlNode>> children;

    bool is(const char* lname) const { return local == lname; }

    /** Get attribute value by local name (ignoring prefix). */
    std::string attr(const char* lname) const {
        for (const auto& a : attrs)
            if (a.local == lname) return a.value;
        return {};
    }

    /** First child with given local name. */
    XmlNode* child(const char* lname) const {
        for (const auto& c : children)
            if (c->local == lname) return c.get();
        return nullptr;
    }
};

/**
 * Parse XML into a DOM tree.  Returns nullptr on error.
 */
std::unique_ptr<XmlNode> dom_parse(const char* data, std::size_t len);

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Collect all text below a node (recursive). */
std::string node_text(const XmlNode& node);

/** Truncate a string to max_bytes (at a UTF-8 boundary). */
std::string truncate_utf8(const std::string& s, std::size_t max_bytes);

} // namespace docx
