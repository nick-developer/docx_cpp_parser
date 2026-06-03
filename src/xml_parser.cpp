#include "xml_parser.h"
#include <cctype>
#include <cstring>
#include <stack>
#include <algorithm>
#include <cstdint>

#ifdef _WIN32
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  ifndef NOMINMAX
#    define NOMINMAX
#  endif
#  include <windows.h>
#else
#  if __has_include(<iconv.h>)
#    include <iconv.h>
#    include <cerrno>
#    define DOCX_HAS_ICONV 1
#  endif
#endif

namespace docx {

// ─── Encoding detection and transcoding ──────────────────────────────────────

// Scan the XML prolog (ASCII-compatible encodings only) for encoding="..."
static std::string extract_xml_encoding_decl(const char* data, std::size_t len) {
    const std::size_t scan = std::min(len, std::size_t(512));
    const char* p   = data;
    const char* end = data + scan;

    // Skip UTF-8 BOM if present
    if (scan >= 3 &&
        (unsigned char)p[0] == 0xEF &&
        (unsigned char)p[1] == 0xBB &&
        (unsigned char)p[2] == 0xBF)
        p += 3;

    if (p + 5 > end) return {};
    if (p[0]!='<'||p[1]!='?'||p[2]!='x'||p[3]!='m'||p[4]!='l') return {};

    // Find the closing "?>"
    const char* pi_end = p + 5;
    while (pi_end + 1 < end && !(pi_end[0]=='?' && pi_end[1]=='>')) ++pi_end;

    p += 5; // skip "<?xml"
    while (p < pi_end) {
        while (p < pi_end && (*p==' '||*p=='\t'||*p=='\n'||*p=='\r')) ++p;
        if (p + 8 <= pi_end && std::strncmp(p, "encoding", 8) == 0) {
            p += 8;
            while (p < pi_end && (*p==' '||*p=='\t')) ++p;
            if (p >= pi_end || *p != '=') { ++p; continue; }
            ++p;
            while (p < pi_end && (*p==' '||*p=='\t')) ++p;
            if (p >= pi_end) break;
            char q = *p++;
            if (q != '"' && q != '\'') break;
            const char* vs = p;
            while (p < pi_end && *p != q) ++p;
            std::string name(vs, p);
            for (char& c : name)
                c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
            return name;
        }
        // skip to next whitespace or end
        while (p < pi_end && *p!=' '&&*p!='\t'&&*p!='\n'&&*p!='\r') ++p;
    }
    return {};
}

// Detect the encoding of an XML byte stream (BOM takes precedence over declaration).
static std::string detect_encoding(const char* data, std::size_t len) {
    if (len == 0) return "UTF-8";
    const auto* b = reinterpret_cast<const uint8_t*>(data);

    if (len >= 4) {
        if (b[0]==0x00 && b[1]==0x00 && b[2]==0xFE && b[3]==0xFF) return "UTF-32BE";
        if (b[0]==0xFF && b[1]==0xFE && b[2]==0x00 && b[3]==0x00) return "UTF-32LE";
    }
    if (len >= 3 && b[0]==0xEF && b[1]==0xBB && b[2]==0xBF) return "UTF-8-BOM";
    if (len >= 2) {
        if (b[0]==0xFF && b[1]==0xFE) return "UTF-16LE";
        if (b[0]==0xFE && b[1]==0xFF) return "UTF-16BE";
    }

    std::string decl = extract_xml_encoding_decl(data, len);
    return decl.empty() ? "UTF-8" : decl;
}

// ─── Built-in UTF-32 and UTF-16 converters (no platform dependency) ──────────

static std::string utf32_to_utf8(const char* data, std::size_t len, bool le) {
    std::string out;
    out.reserve(len / 2);
    const auto* p   = reinterpret_cast<const uint8_t*>(data);
    const auto* end = p + (len & ~std::size_t(3));
    while (p < end) {
        uint32_t cp = le
            ? ((uint32_t)p[0] | ((uint32_t)p[1]<<8) | ((uint32_t)p[2]<<16) | ((uint32_t)p[3]<<24))
            : (((uint32_t)p[0]<<24) | ((uint32_t)p[1]<<16) | ((uint32_t)p[2]<<8) | (uint32_t)p[3]);
        p += 4;
        if (cp == 0) continue; // skip null code points (BOM artefacts)
        if      (cp < 0x80)     { out += (char)cp; }
        else if (cp < 0x800)    { out += (char)(0xC0|(cp>>6));  out += (char)(0x80|(cp&0x3F)); }
        else if (cp < 0x10000)  { out += (char)(0xE0|(cp>>12)); out += (char)(0x80|((cp>>6)&0x3F)); out += (char)(0x80|(cp&0x3F)); }
        else if (cp < 0x110000) { out += (char)(0xF0|(cp>>18)); out += (char)(0x80|((cp>>12)&0x3F)); out += (char)(0x80|((cp>>6)&0x3F)); out += (char)(0x80|(cp&0x3F)); }
    }
    return out;
}

static std::string utf16_to_utf8(const char* data, std::size_t len, bool le) {
    std::string out;
    out.reserve(len);
    const auto* p   = reinterpret_cast<const uint8_t*>(data);
    const auto* end = p + (len & ~std::size_t(1));
    while (p < end) {
        uint16_t u = le ? ((uint16_t)p[0] | ((uint16_t)p[1]<<8))
                        : (((uint16_t)p[0]<<8) | (uint16_t)p[1]);
        p += 2;
        uint32_t cp = u;
        if (u >= 0xD800 && u <= 0xDBFF && p < end) {
            uint16_t lo = le ? ((uint16_t)p[0] | ((uint16_t)p[1]<<8))
                             : (((uint16_t)p[0]<<8) | (uint16_t)p[1]);
            if (lo >= 0xDC00 && lo <= 0xDFFF) {
                cp = 0x10000u + ((uint32_t)(u - 0xD800) << 10) + (lo - 0xDC00);
                p += 2;
            }
        }
        if (cp == 0) continue;
        if      (cp < 0x80)     { out += (char)cp; }
        else if (cp < 0x800)    { out += (char)(0xC0|(cp>>6));  out += (char)(0x80|(cp&0x3F)); }
        else if (cp < 0x10000)  { out += (char)(0xE0|(cp>>12)); out += (char)(0x80|((cp>>6)&0x3F)); out += (char)(0x80|(cp&0x3F)); }
        else                    { out += (char)(0xF0|(cp>>18)); out += (char)(0x80|((cp>>12)&0x3F)); out += (char)(0x80|((cp>>6)&0x3F)); out += (char)(0x80|(cp&0x3F)); }
    }
    return out;
}

// ─── Platform-specific MBCS → UTF-8 ─────────────────────────────────────────

#ifdef _WIN32

static UINT encoding_name_to_codepage(const std::string& enc) {
    struct E { const char* name; UINT cp; };
    static const E table[] = {
        {"UTF-8",        65001}, {"UTF8",          65001},
        {"US-ASCII",     20127}, {"ASCII",          20127},
        {"WINDOWS-1250", 1250},  {"CP1250",         1250},
        {"WINDOWS-1251", 1251},  {"CP1251",         1251},
        {"WINDOWS-1252", 1252},  {"CP1252",         1252},
        {"WINDOWS-1253", 1253},  {"CP1253",         1253},
        {"WINDOWS-1254", 1254},  {"CP1254",         1254},
        {"WINDOWS-1255", 1255},  {"CP1255",         1255},
        {"WINDOWS-1256", 1256},  {"CP1256",         1256},
        {"WINDOWS-1257", 1257},  {"CP1257",         1257},
        {"WINDOWS-1258", 1258},  {"CP1258",         1258},
        {"ISO-8859-1",  28591},  {"LATIN-1",       28591}, {"LATIN1",  28591},
        {"ISO-8859-2",  28592},  {"LATIN-2",       28592}, {"LATIN2",  28592},
        {"ISO-8859-3",  28593},
        {"ISO-8859-4",  28594},
        {"ISO-8859-5",  28595},
        {"ISO-8859-6",  28596},
        {"ISO-8859-7",  28597},
        {"ISO-8859-8",  28598},
        {"ISO-8859-9",  28599},  {"LATIN-5",       28599},
        {"ISO-8859-10", 28600},
        {"ISO-8859-13", 28603},
        {"ISO-8859-14", 28604},
        {"ISO-8859-15", 28605},  {"LATIN-9",       28605},
        {"ISO-8859-16", 28606},
        {"ISO-8859-11", 874},    {"TIS-620",          874}, {"TIS620",    874},
        {"SHIFT-JIS",     932},  {"SHIFT_JIS",        932}, {"SJIS",      932}, {"MS932",   932},
        {"GBK",           936},  {"GB2312",           936}, {"GB_2312",   936},
        {"GB18030",     54936},
        {"BIG5",          950},  {"BIG-5",            950},
        {"EUC-JP",      20932},  {"EUCJP",          20932}, {"EUC_JP",  20932},
        {"EUC-KR",      51949},  {"EUCKR",          51949}, {"EUC_KR",  51949},
        {"EUC-CN",      51936},  {"EUCCN",          51936},
        {"KOI8-R",      20866},  {"KOI8R",          20866},
        {"KOI8-U",      21866},  {"KOI8U",          21866},
        {"IBM850",        850},  {"CP850",            850},
        {"IBM852",        852},  {"CP852",            852},
        {"IBM866",        866},  {"CP866",            866},
        {"IBM437",        437},  {"CP437",            437},
        {"ISO-2022-JP", 50220},
        {"ISO-2022-KR", 50225},
    };
    for (const auto& e : table)
        if (enc == e.name) return e.cp;
    return 65001; // fallback: UTF-8
}

static std::string win_mbcs_to_utf8(const char* data, int len, UINT cp) {
    if (len <= 0) return {};
    int wlen = MultiByteToWideChar(cp, 0, data, len, nullptr, 0);
    if (wlen <= 0) return std::string(data, len);
    std::wstring wide(wlen, L'\0');
    MultiByteToWideChar(cp, 0, data, len, &wide[0], wlen);
    int ulen = WideCharToMultiByte(CP_UTF8, 0, wide.data(), wlen, nullptr, 0, nullptr, nullptr);
    if (ulen <= 0) return std::string(data, len);
    std::string utf8(ulen, '\0');
    WideCharToMultiByte(CP_UTF8, 0, wide.data(), wlen, &utf8[0], ulen, nullptr, nullptr);
    return utf8;
}

#elif defined(DOCX_HAS_ICONV)

static std::string iconv_enc_name(const std::string& enc) {
    struct A { const char* xml; const char* ic; };
    static const A table[] = {
        {"UTF-8",        "UTF-8"},   {"UTF8",        "UTF-8"},
        {"US-ASCII",     "ASCII"},   {"ASCII",       "ASCII"},
        {"WINDOWS-1250", "WINDOWS-1250"}, {"CP1250",  "WINDOWS-1250"},
        {"WINDOWS-1251", "WINDOWS-1251"}, {"CP1251",  "WINDOWS-1251"},
        {"WINDOWS-1252", "WINDOWS-1252"}, {"CP1252",  "WINDOWS-1252"},
        {"WINDOWS-1253", "WINDOWS-1253"}, {"CP1253",  "WINDOWS-1253"},
        {"WINDOWS-1254", "WINDOWS-1254"}, {"CP1254",  "WINDOWS-1254"},
        {"WINDOWS-1255", "WINDOWS-1255"}, {"CP1255",  "WINDOWS-1255"},
        {"WINDOWS-1256", "WINDOWS-1256"}, {"CP1256",  "WINDOWS-1256"},
        {"WINDOWS-1257", "WINDOWS-1257"}, {"CP1257",  "WINDOWS-1257"},
        {"WINDOWS-1258", "WINDOWS-1258"}, {"CP1258",  "WINDOWS-1258"},
        {"ISO-8859-1",  "ISO-8859-1"},  {"LATIN-1",  "ISO-8859-1"}, {"LATIN1",  "ISO-8859-1"},
        {"ISO-8859-2",  "ISO-8859-2"},  {"LATIN-2",  "ISO-8859-2"}, {"LATIN2",  "ISO-8859-2"},
        {"ISO-8859-3",  "ISO-8859-3"},
        {"ISO-8859-4",  "ISO-8859-4"},
        {"ISO-8859-5",  "ISO-8859-5"},
        {"ISO-8859-6",  "ISO-8859-6"},
        {"ISO-8859-7",  "ISO-8859-7"},
        {"ISO-8859-8",  "ISO-8859-8"},
        {"ISO-8859-9",  "ISO-8859-9"},  {"LATIN-5",  "ISO-8859-9"},
        {"ISO-8859-10", "ISO-8859-10"},
        {"ISO-8859-11", "TIS-620"},     {"TIS-620",  "TIS-620"}, {"TIS620", "TIS-620"},
        {"ISO-8859-13", "ISO-8859-13"},
        {"ISO-8859-14", "ISO-8859-14"},
        {"ISO-8859-15", "ISO-8859-15"}, {"LATIN-9",  "ISO-8859-15"},
        {"ISO-8859-16", "ISO-8859-16"},
        {"SHIFT-JIS",   "SHIFT_JIS"},   {"SHIFT_JIS","SHIFT_JIS"}, {"SJIS","SHIFT_JIS"}, {"MS932","SHIFT_JIS"},
        {"GBK",         "GBK"},         {"GB2312",   "GBK"}, {"GB_2312","GBK"},
        {"GB18030",     "GB18030"},
        {"BIG5",        "BIG5"},        {"BIG-5",    "BIG5"},
        {"EUC-JP",      "EUC-JP"},      {"EUCJP",    "EUC-JP"}, {"EUC_JP","EUC-JP"},
        {"EUC-KR",      "EUC-KR"},      {"EUCKR",    "EUC-KR"}, {"EUC_KR","EUC-KR"},
        {"EUC-CN",      "EUC-CN"},      {"EUCCN",    "EUC-CN"},
        {"KOI8-R",      "KOI8-R"},      {"KOI8R",    "KOI8-R"},
        {"KOI8-U",      "KOI8-U"},      {"KOI8U",    "KOI8-U"},
        {"IBM850",      "IBM850"},       {"CP850",    "IBM850"},
        {"IBM852",      "IBM852"},       {"CP852",    "IBM852"},
        {"IBM866",      "IBM866"},       {"CP866",    "IBM866"},
        {"IBM437",      "IBM437"},       {"CP437",    "IBM437"},
        {"ISO-2022-JP", "ISO-2022-JP"},
        {"ISO-2022-KR", "ISO-2022-KR"},
        {"ISO-2022-CN", "ISO-2022-CN"},
    };
    for (const auto& a : table)
        if (enc == a.xml) return a.ic;
    return enc;
}

static std::string iconv_convert(const char* data, std::size_t len, const std::string& from) {
    iconv_t cd = iconv_open("UTF-8", from.c_str());
    if (cd == (iconv_t)-1) {
        // Try with //TRANSLIT to substitute untranslatable characters
        std::string try2 = from + "//TRANSLIT";
        cd = iconv_open("UTF-8", try2.c_str());
        if (cd == (iconv_t)-1) return std::string(data, len);
    }

    std::string out(len * 4, '\0');
    char*       inp     = const_cast<char*>(data);
    std::size_t inleft  = len;
    char*       outp    = &out[0];
    std::size_t outleft = out.size();

    while (inleft > 0) {
        std::size_t rc = iconv(cd, &inp, &inleft, &outp, &outleft);
        if (rc == (std::size_t)-1) {
            if (errno == E2BIG) {
                std::size_t used = out.size() - outleft;
                out.resize(out.size() * 2);
                outp    = &out[0] + used;
                outleft = out.size() - used;
            } else {
                // EILSEQ or EINVAL: skip one byte, emit U+FFFD
                if (inleft > 0) { ++inp; --inleft; }
                if (outleft >= 3) {
                    *outp++ = '\xEF'; *outp++ = '\xBF'; *outp++ = '\xBD';
                    outleft -= 3;
                }
            }
        }
    }
    iconv(cd, nullptr, nullptr, &outp, &outleft); // flush shift state
    iconv_close(cd);

    out.resize(out.size() - outleft);
    return out;
}

#endif // DOCX_HAS_ICONV

// ─── Public entry point ───────────────────────────────────────────────────────

std::string transcode_to_utf8(const char* data, std::size_t len) {
    // Strip artificial trailing null added by callers for parser safety
    if (len > 0 && data[len - 1] == '\0') --len;
    if (len == 0) return {};

    const auto* b   = reinterpret_cast<const uint8_t*>(data);
    std::string enc = detect_encoding(data, len);

    // ── UTF-32 (built-in converter, strip 4-byte BOM) ──
    if (enc == "UTF-32LE") {
        std::size_t skip = (len >= 4 && b[0]==0xFF && b[1]==0xFE && b[2]==0x00 && b[3]==0x00) ? 4 : 0;
        return utf32_to_utf8(data + skip, len - skip, true);
    }
    if (enc == "UTF-32BE") {
        std::size_t skip = (len >= 4 && b[0]==0x00 && b[1]==0x00 && b[2]==0xFE && b[3]==0xFF) ? 4 : 0;
        return utf32_to_utf8(data + skip, len - skip, false);
    }

    // ── UTF-16 (built-in converter, strip 2-byte BOM) ──
    if (enc == "UTF-16LE") {
        std::size_t skip = (len >= 2 && b[0]==0xFF && b[1]==0xFE) ? 2 : 0;
        return utf16_to_utf8(data + skip, len - skip, true);
    }
    if (enc == "UTF-16BE") {
        std::size_t skip = (len >= 2 && b[0]==0xFE && b[1]==0xFF) ? 2 : 0;
        return utf16_to_utf8(data + skip, len - skip, false);
    }

    // ── UTF-8 BOM: strip BOM, return rest ──
    if (enc == "UTF-8-BOM")
        return std::string(data + 3, len - 3);

    // ── Plain UTF-8 / ASCII: no conversion needed ──
    if (enc == "UTF-8" || enc == "US-ASCII" || enc == "ASCII")
        return std::string(data, len);

    // ── Everything else: platform conversion ──
#ifdef _WIN32
    UINT cp = encoding_name_to_codepage(enc);
    if (cp == 65001) return std::string(data, len); // already UTF-8
    return win_mbcs_to_utf8(data, static_cast<int>(len), cp);
#elif defined(DOCX_HAS_ICONV)
    std::string ic = iconv_enc_name(enc);
    if (ic == "UTF-8") return std::string(data, len);
    return iconv_convert(data, len, ic);
#else
    return std::string(data, len); // no conversion available; hope it's UTF-8
#endif
}

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
    // Transcode to UTF-8 if the stream uses a different encoding.
    // `input` owns the buffer when conversion was performed; otherwise it holds
    // a copy of the (null-stripped) UTF-8 bytes.
    std::string input = transcode_to_utf8(data, len);
    data = input.data();
    len  = input.size();

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
            ++p;
            while (p + 1 < end && !(p[0] == '?' && p[1] == '>')) ++p;
            if (p + 1 < end) p += 2;
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
