#include "zip_reader.h"

// On MSVC there is no system zlib.  The vendored single-header implementation
// in vendor/zlib/zlib.h covers exactly the API used here (inflate + CRC-32).
// On Linux, macOS, and MinGW the system <zlib.h> is always available and
// preferred (smaller binary, battle-tested, benefits from system updates).
// VENDOR_ZLIB_IMPLEMENTATION activates the function bodies in this one TU only;
// all other TUs include the header for declarations without re-defining anything.
#ifdef _MSC_VER
#  define VENDOR_ZLIB_IMPLEMENTATION
#  include "../vendor/zlib/zlib.h"
#else
#  include <zlib.h>
#endif
#include <cstring>
#include <cassert>
#include <algorithm>
#include <unordered_map>
#include <fstream>
#include <stdexcept>

// ─── Platform memory-map ────────────────────────────────────────────────────
#ifdef _WIN32
  #include <windows.h>
#else
  #include <sys/mman.h>
  #include <sys/stat.h>
  #include <fcntl.h>
  #include <unistd.h>
#endif

namespace docx {

// ─── ZIP binary layout constants ────────────────────────────────────────────

static constexpr uint32_t EOCD_SIGNATURE        = 0x06054b50;
static constexpr uint32_t EOCD64_SIGNATURE      = 0x06064b50;
static constexpr uint32_t EOCD64_LOCATOR_SIG    = 0x07064b50;
static constexpr uint32_t CD_SIGNATURE           = 0x02014b50;
static constexpr uint32_t LOCAL_SIGNATURE        = 0x04034b50;
static constexpr uint16_t ZIP64_EXTRA_TAG        = 0x0001;

// ─── Helpers ────────────────────────────────────────────────────────────────

static inline uint16_t read_u16(const uint8_t* p) {
    return static_cast<uint16_t>(p[0]) | (static_cast<uint16_t>(p[1]) << 8);
}
static inline uint32_t read_u32(const uint8_t* p) {
    return static_cast<uint32_t>(p[0])
         | (static_cast<uint32_t>(p[1]) << 8)
         | (static_cast<uint32_t>(p[2]) << 16)
         | (static_cast<uint32_t>(p[3]) << 24);
}
static inline uint64_t read_u64(const uint8_t* p) {
    uint64_t lo = read_u32(p), hi = read_u32(p + 4);
    return lo | (hi << 32);
}

// ─── Impl ───────────────────────────────────────────────────────────────────

struct ZipReader::Impl {
    // Memory-mapped file view
    const uint8_t* data{nullptr};
    std::size_t    size{0};

#ifdef _WIN32
    HANDLE hFile{INVALID_HANDLE_VALUE};
    HANDLE hMap{nullptr};
#else
    int    fd{-1};
#endif

    std::vector<ZipEntry>                       entries_vec;
    std::unordered_map<std::string, std::size_t> name_to_idx;

    // ── open / close ──────────────────────────────────────────────────────

    void open(const std::string& path) {
#ifdef _WIN32
        hFile = CreateFileA(path.c_str(), GENERIC_READ, FILE_SHARE_READ,
                            nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (hFile == INVALID_HANDLE_VALUE)
            throw ZipError("Cannot open file: " + path);

        LARGE_INTEGER sz{};
        GetFileSizeEx(hFile, &sz);
        size = static_cast<std::size_t>(sz.QuadPart);

        hMap = CreateFileMappingA(hFile, nullptr, PAGE_READONLY, 0, 0, nullptr);
        if (!hMap) { CloseHandle(hFile); throw ZipError("Cannot map file: " + path); }
        data = static_cast<const uint8_t*>(MapViewOfFile(hMap, FILE_MAP_READ, 0, 0, 0));
        if (!data) { CloseHandle(hMap); CloseHandle(hFile); throw ZipError("MapViewOfFile failed"); }
#else
        fd = ::open(path.c_str(), O_RDONLY);
        if (fd < 0) throw ZipError("Cannot open file: " + path);

        struct stat st{};
        if (::fstat(fd, &st) < 0) { ::close(fd); throw ZipError("fstat failed: " + path); }
        size = static_cast<std::size_t>(st.st_size);

        data = static_cast<const uint8_t*>(
            ::mmap(nullptr, size, PROT_READ, MAP_PRIVATE, fd, 0));
        if (data == MAP_FAILED) {
            ::close(fd);
            throw ZipError("mmap failed: " + path);
        }
#endif
        parse_central_directory();
    }

    void close() noexcept {
        entries_vec.clear();
        name_to_idx.clear();
        if (!data) return;
#ifdef _WIN32
        UnmapViewOfFile(data);
        CloseHandle(hMap);
        CloseHandle(hFile);
        hFile = INVALID_HANDLE_VALUE;
        hMap  = nullptr;
#else
        ::munmap(const_cast<uint8_t*>(data), size);
        ::close(fd);
        fd = -1;
#endif
        data = nullptr;
        size = 0;
    }

    // ── Central directory parsing ─────────────────────────────────────────

    void parse_central_directory() {
        if (size < 22) throw ZipError("File too small to be a ZIP archive");

        // Locate EOCD (search backwards for signature + comment ≤ 65535)
        std::size_t eocd_pos = 0;
        bool found = false;
        std::size_t search_start = (size >= 22 + 65535) ? size - 22 - 65535 : 0;
        for (std::size_t i = size - 22; i >= search_start; --i) {
            if (read_u32(data + i) == EOCD_SIGNATURE) { eocd_pos = i; found = true; break; }
            if (i == 0) break;
        }
        if (!found) throw ZipError("EOCD signature not found — not a valid ZIP");

        uint64_t cd_offset = read_u32(data + eocd_pos + 16);
        uint64_t cd_count  = read_u16(data + eocd_pos + 10);

        // Check for ZIP64 locator just before EOCD
        if (eocd_pos >= 20 && read_u32(data + eocd_pos - 20) == EOCD64_LOCATOR_SIG) {
            uint64_t eocd64_off = read_u64(data + eocd_pos - 20 + 8);
            if (eocd64_off + 56 <= size && read_u32(data + eocd64_off) == EOCD64_SIGNATURE) {
                cd_count  = read_u64(data + eocd64_off + 32);
                cd_offset = read_u64(data + eocd64_off + 48);
            }
        }

        entries_vec.reserve(cd_count);
        const uint8_t* p = data + cd_offset;

        for (uint64_t i = 0; i < cd_count; ++i) {
            if (p + 46 > data + size || read_u32(p) != CD_SIGNATURE)
                throw ZipError("Central directory entry signature mismatch");

            uint16_t method      = read_u16(p + 10);
            uint32_t crc         = read_u32(p + 16);
            uint64_t comp_sz     = read_u32(p + 20);
            uint64_t uncomp_sz   = read_u32(p + 24);
            uint16_t name_len    = read_u16(p + 28);
            uint16_t extra_len   = read_u16(p + 30);
            uint16_t comment_len = read_u16(p + 32);
            uint64_t lh_offset   = read_u32(p + 42);

            std::string name(reinterpret_cast<const char*>(p + 46), name_len);

            // Parse ZIP64 extra field if sentinel values present
            const uint8_t* ex = p + 46 + name_len;
            const uint8_t* ex_end = ex + extra_len;
            while (ex + 4 <= ex_end) {
                uint16_t tag = read_u16(ex);
                uint16_t sz  = read_u16(ex + 2);
                if (tag == ZIP64_EXTRA_TAG) {
                    const uint8_t* d = ex + 4;
                    if (uncomp_sz == 0xFFFFFFFF && d + 8 <= ex_end) { uncomp_sz = read_u64(d); d += 8; }
                    if (comp_sz   == 0xFFFFFFFF && d + 8 <= ex_end) { comp_sz   = read_u64(d); d += 8; }
                    if (lh_offset == 0xFFFFFFFF && d + 8 <= ex_end) { lh_offset = read_u64(d); }
                    break;
                }
                ex += 4 + sz;
            }

            ZipEntry e;
            e.name                 = std::move(name);
            e.crc32                = crc;
            e.compressed_size      = comp_sz;
            e.uncompressed_size    = uncomp_sz;
            e.local_header_offset  = lh_offset;
            e.compression_method   = method;

            name_to_idx[e.name] = entries_vec.size();
            entries_vec.push_back(std::move(e));

            p += 46 + name_len + extra_len + comment_len;
        }
    }

    // ── Inflate ───────────────────────────────────────────────────────────

    std::size_t inflate_entry(const ZipEntry& e, char* out, std::size_t out_sz) const {
        // Skip local file header
        const uint8_t* lh = data + e.local_header_offset;
        if (lh + 30 > data + size || read_u32(lh) != LOCAL_SIGNATURE)
            throw ZipError("Local file header signature mismatch for: " + e.name);

        uint16_t lh_name_len  = read_u16(lh + 26);
        uint16_t lh_extra_len = read_u16(lh + 28);
        const uint8_t* compressed = lh + 30 + lh_name_len + lh_extra_len;

        if (e.compression_method == 0) {
            // Stored
            if (e.compressed_size > out_sz)
                throw ZipError("Buffer too small for stored entry: " + e.name);
            std::memcpy(out, compressed, static_cast<std::size_t>(e.compressed_size));
            return static_cast<std::size_t>(e.compressed_size);
        }

        if (e.compression_method != 8)
            throw ZipError("Unsupported compression method " +
                           std::to_string(e.compression_method) + " in: " + e.name);

        // DEFLATE
        z_stream zs{};
        zs.next_in   = const_cast<Bytef*>(compressed);
        zs.avail_in  = static_cast<uInt>(e.compressed_size);
        zs.next_out  = reinterpret_cast<Bytef*>(out);
        zs.avail_out = static_cast<uInt>(out_sz);

        if (inflateInit2(&zs, -MAX_WBITS) != Z_OK)
            throw ZipError("inflateInit2 failed for: " + e.name);

        int ret = inflate(&zs, Z_FINISH);
        inflateEnd(&zs);

        if (ret != Z_STREAM_END)
            throw ZipError("inflate failed (" + std::to_string(ret) + ") for: " + e.name);

        return static_cast<std::size_t>(zs.total_out);
    }
};

// ─── ZipReader public API ────────────────────────────────────────────────────

ZipReader::ZipReader()  : impl_(std::make_unique<Impl>()) {}
ZipReader::~ZipReader() { impl_->close(); }

ZipReader::ZipReader(ZipReader&&) noexcept = default;
ZipReader& ZipReader::operator=(ZipReader&&) noexcept = default;

void ZipReader::open(const std::string& path) { impl_->open(path); }
void ZipReader::close()                        { impl_->close(); }

const std::vector<ZipEntry>& ZipReader::entries() const noexcept {
    return impl_->entries_vec;
}

bool ZipReader::has_entry(const std::string& name) const noexcept {
    return impl_->name_to_idx.count(name) > 0;
}

std::vector<char> ZipReader::read_entry(const std::string& name) const {
    auto it = impl_->name_to_idx.find(name);
    if (it == impl_->name_to_idx.end())
        throw ZipError("Entry not found: " + name);
    const ZipEntry& e = impl_->entries_vec[it->second];
    std::vector<char> buf(static_cast<std::size_t>(e.uncompressed_size) + 1, '\0');
    impl_->inflate_entry(e, buf.data(), buf.size() - 1);
    buf.resize(static_cast<std::size_t>(e.uncompressed_size));
    return buf;
}

std::size_t ZipReader::read_entry_into(const std::string& name,
                                        char* buf, std::size_t buf_size) const {
    auto it = impl_->name_to_idx.find(name);
    if (it == impl_->name_to_idx.end())
        throw ZipError("Entry not found: " + name);
    return impl_->inflate_entry(impl_->entries_vec[it->second], buf, buf_size);
}

void ZipReader::for_each_with_prefix(
    const std::string& prefix,
    const std::function<void(const ZipEntry&, std::vector<char>&&)>& cb) const
{
    for (const auto& e : impl_->entries_vec) {
        if (e.name.size() >= prefix.size() &&
            e.name.compare(0, prefix.size(), prefix) == 0)
        {
            cb(e, read_entry(e.name));
        }
    }
}

} // namespace docx
