/**
 * Python bindings for docx_comment_parser using pybind11.
 *
 * Build command (example):
 *   c++ -O2 -std=c++17 -fPIC -shared \
 *       -I../include $(python3-config --includes) \
 *       -I$(python3 -c "import pybind11; print(pybind11.get_include())") \
 *       python_bindings.cpp ../src/docx_parser.cpp ../src/batch_parser.cpp \
 *       ../src/zip_reader.cpp \
 *       -lz \
 *       -o docx_comment_parser$(python3-config --extension-suffix)
 *
 * Module name
 * -----------
 * This extension is the compiled *core* of the ``docx_comment_parser`` package
 * and is therefore named ``_core``.  End users never import it directly — the
 * pure-Python package ``src/docx_comment_parser/__init__.py`` re-exports every
 * public name from here, so the historical import path is unchanged:
 *
 *   import docx_comment_parser as dcp
 *   parser = dcp.DocxParser()
 *   parser.parse("my_file.docx")
 *   for c in parser.comments():
 *       print(c.author, c.text)
 */

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/operators.h>

#include "docx_comment_parser.h"

namespace py = pybind11;
using namespace docx;

namespace {

// Python exception types, created in the module initialiser below.  The module
// dict holds the owning references for the life of the interpreter, so these
// are borrowed handles.
//
// They live at file scope because pybind11's ExceptionTranslator is a plain
// function pointer: the translator must be capture-less.
PyObject* g_exc_parser = nullptr;
PyObject* g_exc_file   = nullptr;
PyObject* g_exc_format = nullptr;

void translate_docx_exception(std::exception_ptr p) {
    try {
        if (p) std::rethrow_exception(p);
    }
    // Most-derived first: the first matching clause wins.  Registering three
    // separate translators would not work, because pybind11 tries them in
    // reverse registration order and the base-class handler would swallow every
    // derived type before the specific ones were reached.
    catch (const DocxFileError&   e) { PyErr_SetString(g_exc_file,   e.what()); }
    catch (const DocxFormatError& e) { PyErr_SetString(g_exc_format, e.what()); }
    catch (const DocxParserError& e) { PyErr_SetString(g_exc_parser, e.what()); }
}

} // anonymous namespace

PYBIND11_MODULE(_core, m) {
    m.doc() = R"doc(
docx_comment_parser._core
=========================
Compiled C++ core for extracting all comment metadata from .docx files.

Import the parent package instead of this module:

    import docx_comment_parser as dcp

Classes
-------
CommentRef          – lightweight reference to a related comment
CommentMetadata     – all data extracted for one comment
DocumentCommentStats– document-level aggregate statistics
DocxParser          – single-file parser
BatchParser         – multi-file parallel parser
)doc";

    // ── CommentRef ────────────────────────────────────────────────────────────
    py::class_<CommentRef>(m, "CommentRef",
        "Lightweight reference to a related (replied-to) comment.")
        .def_readonly("id",           &CommentRef::id,
            "Comment id (w:id attribute).")
        .def_readonly("author",       &CommentRef::author,
            "Author name of the referenced comment.")
        .def_readonly("date",         &CommentRef::date,
            "ISO-8601 date string of the referenced comment.")
        .def_readonly("text_snippet", &CommentRef::text_snippet,
            "First 120 characters of the referenced comment's text.")
        .def("__repr__", [](const CommentRef& r){
            return "<CommentRef id=" + std::to_string(r.id)
                 + " author='" + r.author + "'>";
        });

    // ── CommentMetadata ───────────────────────────────────────────────────────
    py::class_<CommentMetadata>(m, "CommentMetadata", R"doc(
All metadata extracted for a single comment (w:comment element).

Attributes
----------
id                  : int   – w:id
author              : str   – w:author
date                : str   – ISO-8601 date (as stored in XML)
initials            : str   – w:initials
text                : str   – full plain-text of comment body
paragraph_style     : str   – style of first paragraph inside comment
range_start_para_id : str   – paraId of commentRangeStart (OOXML 2016+)
range_end_para_id   : str   – paraId of commentRangeEnd   (OOXML 2016+)
referenced_text     : str   – document text anchored by this comment
is_reply            : bool  – True if this comment is a reply
parent_id           : int   – id of parent comment (-1 if root)
replies             : list[CommentRef]  – direct replies (on parent)
para_id             : str   – unique per-comment paragraph id
para_id_parent      : str   – parent paragraph id string
done                : bool  – resolved/done flag (OOXML 2016+)
paragraph_index     : int   – 0-based paragraph in document body
run_index           : int   – 0-based run within paragraph
thread_ids          : list[int] – ordered ids of entire thread (root only)
)doc")
        .def_readonly("id",                   &CommentMetadata::id)
        .def_readonly("author",               &CommentMetadata::author)
        .def_readonly("date",                 &CommentMetadata::date)
        .def_readonly("initials",             &CommentMetadata::initials)
        .def_readonly("text",                 &CommentMetadata::text)
        .def_readonly("paragraph_style",      &CommentMetadata::paragraph_style)
        .def_readonly("range_start_para_id",  &CommentMetadata::range_start_para_id)
        .def_readonly("range_end_para_id",    &CommentMetadata::range_end_para_id)
        .def_readonly("referenced_text",      &CommentMetadata::referenced_text)
        .def_readonly("is_reply",             &CommentMetadata::is_reply)
        .def_readonly("parent_id",            &CommentMetadata::parent_id)
        .def_readonly("replies",              &CommentMetadata::replies)
        .def_readonly("para_id",              &CommentMetadata::para_id)
        .def_readonly("para_id_parent",       &CommentMetadata::para_id_parent)
        .def_readonly("done",                 &CommentMetadata::done)
        .def_readonly("paragraph_index",      &CommentMetadata::paragraph_index)
        .def_readonly("run_index",            &CommentMetadata::run_index)
        .def_readonly("thread_ids",           &CommentMetadata::thread_ids)
        .def("to_dict", [](const CommentMetadata& m) {
            py::dict d;
            d["id"]                   = m.id;
            d["author"]               = m.author;
            d["date"]                 = m.date;
            d["initials"]             = m.initials;
            d["text"]                 = m.text;
            d["paragraph_style"]      = m.paragraph_style;
            d["range_start_para_id"]  = m.range_start_para_id;
            d["range_end_para_id"]    = m.range_end_para_id;
            d["referenced_text"]      = m.referenced_text;
            d["is_reply"]             = m.is_reply;
            d["parent_id"]            = m.parent_id;
            d["para_id"]              = m.para_id;
            d["para_id_parent"]       = m.para_id_parent;
            d["done"]                 = m.done;
            d["paragraph_index"]      = m.paragraph_index;
            d["run_index"]            = m.run_index;
            d["thread_ids"]           = m.thread_ids;
            py::list replies;
            for (const auto& r : m.replies) {
                py::dict rd;
                rd["id"]           = r.id;
                rd["author"]       = r.author;
                rd["date"]         = r.date;
                rd["text_snippet"] = r.text_snippet;
                replies.append(rd);
            }
            d["replies"] = replies;
            return d;
        }, "Return all metadata as a Python dict.")
        .def("__repr__", [](const CommentMetadata& m){
            return "<CommentMetadata id=" + std::to_string(m.id)
                 + " author='" + m.author + "'"
                 + (m.is_reply ? " [reply]" : "") + ">";
        });

    // ── DocumentCommentStats ─────────────────────────────────────────────────
    py::class_<DocumentCommentStats>(m, "DocumentCommentStats", R"doc(
Document-level comment statistics.

Attributes
----------
file_path         : str
total_comments    : int
total_resolved    : int   – comments with done=True
total_replies     : int
total_root_comments : int
unique_authors    : list[str]
earliest_date     : str   – ISO-8601
latest_date       : str   – ISO-8601
)doc")
        .def_readonly("file_path",          &DocumentCommentStats::file_path)
        .def_readonly("total_comments",     &DocumentCommentStats::total_comments)
        .def_readonly("total_resolved",     &DocumentCommentStats::total_resolved)
        .def_readonly("total_replies",      &DocumentCommentStats::total_replies)
        .def_readonly("total_root_comments",&DocumentCommentStats::total_root_comments)
        .def_readonly("unique_authors",     &DocumentCommentStats::unique_authors)
        .def_readonly("earliest_date",      &DocumentCommentStats::earliest_date)
        .def_readonly("latest_date",        &DocumentCommentStats::latest_date)
        .def("to_dict", [](const DocumentCommentStats& s){
            py::dict d;
            d["file_path"]            = s.file_path;
            d["total_comments"]       = s.total_comments;
            d["total_resolved"]       = s.total_resolved;
            d["total_replies"]        = s.total_replies;
            d["total_root_comments"]  = s.total_root_comments;
            d["unique_authors"]       = s.unique_authors;
            d["earliest_date"]        = s.earliest_date;
            d["latest_date"]          = s.latest_date;
            return d;
        }, "Return stats as a Python dict.")
        .def("__repr__", [](const DocumentCommentStats& s){
            return "<DocumentCommentStats total=" + std::to_string(s.total_comments)
                 + " file='" + s.file_path + "'>";
        });

    // ── DocxParser ────────────────────────────────────────────────────────────
    py::class_<DocxParser>(m, "DocxParser", R"doc(
Single-file .docx comment parser.

Example
-------
>>> import docx_comment_parser as dcp
>>> p = dcp.DocxParser()
>>> p.parse("report.docx")
>>> for c in p.comments():
...     print(c.id, c.author, c.text[:60])
)doc")
        .def(py::init<>())
        .def("parse",
             &DocxParser::parse,
             py::arg("file_path"),
             R"doc(
Parse a .docx file and extract all comment metadata.

Parameters
----------
file_path : str
    Absolute or relative path to the .docx file.

Raises
------
DocxFileError   if the file cannot be opened.
DocxFormatError if required OOXML parts are missing or malformed.
)doc")
        .def("comments",
             &DocxParser::comments,
             py::return_value_policy::reference_internal,
             "Return list of all CommentMetadata objects (sorted by id).")
        .def("stats",
             &DocxParser::stats,
             py::return_value_policy::reference_internal,
             "Return DocumentCommentStats for the parsed file.")
        .def("find_by_id",
             [](const DocxParser& self, int id) -> py::object {
                 const CommentMetadata* m = self.find_by_id(id);
                 if (!m) return py::none();
                 return py::cast(*m);
             },
             py::arg("id"),
             "Return CommentMetadata for the given id, or None if not found.")
        .def("by_author",
             [](const DocxParser& self, const std::string& author){
                 auto ptrs = self.by_author(author);
                 py::list result;
                 for (auto* p : ptrs) result.append(*p);
                 return result;
             },
             py::arg("author"),
             "Return list of CommentMetadata authored by the given person.")
        .def("root_comments",
             [](const DocxParser& self){
                 auto ptrs = self.root_comments();
                 py::list result;
                 for (auto* p : ptrs) result.append(*p);
                 return result;
             },
             "Return non-reply root comments in document order.")
        .def("thread",
             [](const DocxParser& self, int root_id){
                 auto ptrs = self.thread(root_id);
                 py::list result;
                 for (auto* p : ptrs) result.append(*p);
                 return result;
             },
             py::arg("root_id"),
             "Return ordered list of CommentMetadata forming the thread for root_id.");

    // ── BatchParser ───────────────────────────────────────────────────────────
    py::class_<BatchParser>(m, "BatchParser", R"doc(
Multi-file parallel .docx comment parser.

Example
-------
>>> import docx_comment_parser as dcp, glob
>>> bp = dcp.BatchParser(max_threads=4)
>>> bp.parse_all(glob.glob("/docs/*.docx"))
>>> for path in glob.glob("/docs/*.docx"):
...     print(path, bp.stats(path).total_comments)
>>> bp.release_all()
)doc")
        .def(py::init<unsigned int>(),
             py::arg("max_threads") = 0u,
             "Create a BatchParser. max_threads=0 uses all CPU cores.")
        .def("parse_all",
             &BatchParser::parse_all,
             py::arg("file_paths"),
             py::call_guard<py::gil_scoped_release>(),
             R"doc(
Parse a list of .docx files in parallel.

Files that fail are recorded in errors() rather than raising.

Parameters
----------
file_paths : list[str]
)doc")
        .def("comments",
             [](const BatchParser& self, const std::string& fp) {
                 return self.comments(fp);
             },
             py::arg("file_path"),
             "Return list of CommentMetadata for a previously parsed file.")
        .def("stats",
             [](const BatchParser& self, const std::string& fp){
                 return self.stats(fp);
             },
             py::arg("file_path"),
             "Return DocumentCommentStats for a previously parsed file.")
        .def("errors",
             [](const BatchParser& self){
                 py::dict d;
                 for (const auto& kv : self.errors())
                     d[py::str(kv.first)] = kv.second;
                 return d;
             },
             "Return dict of {file_path: error_message} for files that failed.")
        .def("parsed_files",
             &BatchParser::parsed_files,
             "Return sorted list of file paths that parsed successfully and "
             "still hold results (released files are excluded).")
        .def("release",
             &BatchParser::release,
             py::arg("file_path"),
             "Free memory for a specific parsed file.")
        .def("release_all",
             &BatchParser::release_all,
             "Free memory for all parsed files.");

    // ── Exception types ───────────────────────────────────────────────────────
    //
    // These are built by hand rather than with py::register_exception() for two
    // reasons:
    //
    //  1. Hierarchy.  register_exception() accepts a single base, so the C++
    //     relationship (DocxFileError : DocxParserError) could not be expressed
    //     alongside the historical builtin bases.  PyErr_NewException accepts a
    //     *tuple* of bases, so DocxFileError can be both a DocxParserError and
    //     an OSError.  Existing `except OSError` / `except ValueError` /
    //     `except DocxParserError` code keeps working, and `except
    //     DocxFileError` starts working.
    //
    //  2. Dispatch order.  pybind11 tries exception translators in reverse
    //     registration order, so the previously-last-registered DocxParserError
    //     handler caught every derived type first — DocxFileError and
    //     DocxFormatError could never actually be raised.  The single
    //     translate_docx_exception() above, with most-derived-first catch
    //     clauses, fixes that.

    py::object exc_parser = py::reinterpret_steal<py::object>(
        PyErr_NewException("docx_comment_parser._core.DocxParserError",
                           PyExc_RuntimeError, nullptr));

    py::object file_bases = py::make_tuple(
        exc_parser, py::reinterpret_borrow<py::object>(py::handle(PyExc_OSError)));
    py::object exc_file = py::reinterpret_steal<py::object>(
        PyErr_NewException("docx_comment_parser._core.DocxFileError",
                           file_bases.ptr(), nullptr));

    py::object format_bases = py::make_tuple(
        exc_parser, py::reinterpret_borrow<py::object>(py::handle(PyExc_ValueError)));
    py::object exc_format = py::reinterpret_steal<py::object>(
        PyErr_NewException("docx_comment_parser._core.DocxFormatError",
                           format_bases.ptr(), nullptr));

    m.add_object("DocxParserError", exc_parser);
    m.add_object("DocxFileError",   exc_file);
    m.add_object("DocxFormatError", exc_format);

    // The module dict now owns a reference to each type for the life of the
    // interpreter, so the translator holds plain borrowed handles.
    g_exc_parser = exc_parser.ptr();
    g_exc_file   = exc_file.ptr();
    g_exc_format = exc_format.ptr();

    py::register_exception_translator(&translate_docx_exception);
}
