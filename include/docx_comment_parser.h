#pragma once

// ─── Symbol export/import macro ───────────────────────────────────────────────
//
// On Windows (MSVC and MinGW) DLL symbols must be explicitly exported by the
// *building* translation unit and imported by *consuming* translation units.
//
//   DOCX_BUILDING_DLL  – defined by CMake / setup.py when compiling the library
//                        itself; expands DOCX_API to __declspec(dllexport).
//                        NOT defined when a downstream project includes this header,
//                        so DOCX_API expands to __declspec(dllimport) instead.
//
// On Linux/macOS with -fvisibility=hidden the macro marks symbols as default
// visibility so the linker exports them even though the TU default is hidden.

#ifdef _WIN32
  #ifdef DOCX_BUILDING_DLL
    #define DOCX_API __declspec(dllexport)
  #else
    #define DOCX_API __declspec(dllimport)
  #endif
#else
  #define DOCX_API __attribute__((visibility("default")))
#endif

#include <string>
#include <vector>
#include <memory>
#include <unordered_map>
#include <cstdint>
#include <stdexcept>

namespace docx {

// ─────────────────────────────────────────────
//  Data Structures
// ─────────────────────────────────────────────

/**
 * Represents a resolved replied-to comment reference.
 * Stored by value inside CommentMetadata for cache locality.
 */
struct DOCX_API CommentRef {
    int         id{-1};
    std::string author;
    std::string date;
    std::string text_snippet;   // first 120 chars of referenced comment
};

/**
 * All metadata extracted for a single comment (w:comment element).
 * Members are ordered to minimise padding.
 */
struct DOCX_API CommentMetadata {
    // --- identity ---
    int         id{-1};                  // w:id
    std::string author;                  // w:author
    std::string date;                    // w:date  (ISO-8601 string as stored in XML)
    std::string initials;                // w:initials

    // --- content ---
    std::string text;                    // full plain-text of comment body
    std::string paragraph_style;         // style of first paragraph inside comment

    // --- anchoring ---
    std::string range_start_para_id;     // w:commentRangeStart / paraId (OOXML 2016+)
    std::string range_end_para_id;
    std::string referenced_text;         // document text the comment is anchored to

    // --- threading (OOXML 2016+) ---
    bool        is_reply{false};
    int         parent_id{-1};           // w:paraIdParent → resolved parent comment id
    std::vector<CommentRef> replies;     // populated on parent comment

    // --- extended (from commentsExtended.xml) ---
    std::string para_id;                 // unique per-comment para ID
    std::string para_id_parent;          // parent para ID string (before id resolution)
    bool        done{false};             // @w16cex:done

    // --- document position ---
    int         paragraph_index{-1};     // 0-based paragraph in document body
    int         run_index{-1};           // 0-based run within that paragraph

    // Convenience: resolved full thread chain (only set on root comments)
    std::vector<int> thread_ids;         // ordered list of ids in this thread
};

/**
 * Document-level comment statistics and metadata.
 */
struct DOCX_API DocumentCommentStats {
    std::string              file_path;
    std::size_t              total_comments{0};
    std::size_t              total_resolved{0};
    std::size_t              total_replies{0};
    std::size_t              total_root_comments{0};
    std::vector<std::string> unique_authors;
    std::string              earliest_date;
    std::string              latest_date;
};

// ─────────────────────────────────────────────
//  Exception hierarchy
// ─────────────────────────────────────────────

struct DOCX_API DocxParserError : std::runtime_error {
    explicit DocxParserError(const std::string& msg) : std::runtime_error(msg) {}
};

struct DOCX_API DocxFileError : DocxParserError {
    explicit DocxFileError(const std::string& msg) : DocxParserError(msg) {}
};

struct DOCX_API DocxFormatError : DocxParserError {
    explicit DocxFormatError(const std::string& msg) : DocxParserError(msg) {}
};

// ─────────────────────────────────────────────
//  DocxParser
// ─────────────────────────────────────────────

/**
 * Parses one .docx file and extracts all comment metadata.
 *
 * Thread-safety: one DocxParser per thread; results are immutable after parse().
 *
 * Memory strategy:
 *   - ZIP entries are inflated one at a time and immediately parsed;
 *     raw deflated bytes are never kept alive simultaneously.
 *   - XML parsing uses a SAX-style streaming pass to avoid building a full DOM
 *     for the main document body (comments.xml is small enough for DOM).
 */
class DOCX_API DocxParser {
public:
    DocxParser();
    ~DocxParser();

    // Non-copyable, movable
    DocxParser(const DocxParser&)            = delete;
    DocxParser& operator=(const DocxParser&) = delete;
    DocxParser(DocxParser&&)                 noexcept;
    DocxParser& operator=(DocxParser&&)      noexcept;

    /**
     * Parse a .docx file.
     * @param file_path  Absolute or relative path to the .docx file.
     * @throws DocxFileError   if the file cannot be opened / is not a valid ZIP.
     * @throws DocxFormatError if required OOXML parts are missing or malformed.
     */
    void parse(const std::string& file_path);

    /** Returns all extracted comments (sorted by id). */
    const std::vector<CommentMetadata>& comments() const noexcept;

    /** Returns document-level statistics computed after parse(). */
    const DocumentCommentStats&         stats()    const noexcept;

    /** Lookup a comment by its w:id.  Returns nullptr if not found. */
    const CommentMetadata* find_by_id(int id) const noexcept;

    /** Returns comments authored by a specific person. */
    std::vector<const CommentMetadata*> by_author(const std::string& author) const;

    /** Returns the root (non-reply) comments, in document order. */
    std::vector<const CommentMetadata*> root_comments() const;

    /** Returns the full reply chain for a given root comment id. */
    std::vector<const CommentMetadata*> thread(int root_id) const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

// ─────────────────────────────────────────────
//  BatchParser  – process many files efficiently
// ─────────────────────────────────────────────

/**
 * Processes multiple .docx files, optionally in parallel.
 *
 * Each file is parsed independently; memory for each result set is
 * released as soon as the caller calls release(file_path).
 */
class DOCX_API BatchParser {
public:
    /**
     * @param max_threads  0 = use std::thread::hardware_concurrency().
     */
    explicit BatchParser(unsigned int max_threads = 0);
    ~BatchParser();

    BatchParser(const BatchParser&)            = delete;
    BatchParser& operator=(const BatchParser&) = delete;

    /**
     * Parse a list of .docx files.
     * Files that fail are recorded in errors() rather than throwing.
     */
    void parse_all(const std::vector<std::string>& file_paths);

    /** Results for a specific file (throws if file was never parsed). */
    const std::vector<CommentMetadata>& comments(const std::string& file_path) const;

    /** Stats for a specific file. */
    const DocumentCommentStats&         stats   (const std::string& file_path) const;

    /** Map of file_path → error message for files that failed. */
    const std::unordered_map<std::string, std::string>& errors() const noexcept;

    /** Free the results for a specific file to reclaim memory. */
    void release(const std::string& file_path);

    /** Free all results. */
    void release_all();

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace docx
