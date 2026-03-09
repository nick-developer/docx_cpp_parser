#pragma once
/**
 * Minimal, zero-dependency ZIP reader built on zlib's inflate.
 *
 * Supports:
 *   - DEFLATE (method 8) and stored (method 0) entries
 *   - ZIP64 end-of-central-directory
 *   - Only what is needed for reading .docx (no encryption, no ZIP64 data descriptors)
 *
 * Memory model: file is memory-mapped (or read into a single buffer on non-POSIX),
 * then individual entries are inflated on demand into caller-provided or heap buffers.
 * No entry data is kept alive between calls.
 */

#include <string>
#include <vector>
#include <memory>
#include <cstdint>
#include <cstddef>
#include <functional>
#include <stdexcept>

namespace docx {

struct ZipError : std::runtime_error {
    explicit ZipError(const std::string& m) : std::runtime_error(m) {}
};

struct ZipEntry {
    std::string  name;
    std::uint32_t crc32{0};
    std::uint64_t compressed_size{0};
    std::uint64_t uncompressed_size{0};
    std::uint64_t local_header_offset{0};
    std::uint16_t compression_method{0};  // 0=stored, 8=deflate
};

class ZipReader {
public:
    ZipReader();
    ~ZipReader();

    ZipReader(const ZipReader&)            = delete;
    ZipReader& operator=(const ZipReader&) = delete;
    ZipReader(ZipReader&&)                 noexcept;
    ZipReader& operator=(ZipReader&&)      noexcept;

    /** Open a zip file and read the central directory. */
    void open(const std::string& path);

    /** Close and release memory. */
    void close();

    /** All entries in the archive. */
    const std::vector<ZipEntry>& entries() const noexcept;

    /** Returns true if the archive contains an entry with this name. */
    bool has_entry(const std::string& name) const noexcept;

    /**
     * Inflate (or copy) entry data into a fresh std::vector<char>.
     * Throws ZipError if entry is not found or decompression fails.
     */
    std::vector<char> read_entry(const std::string& name) const;

    /**
     * Inflate entry directly into caller-provided buffer.
     * buf must be at least entry.uncompressed_size bytes.
     * Returns number of bytes written.
     */
    std::size_t read_entry_into(const std::string& name,
                                char* buf, std::size_t buf_size) const;

    /**
     * Iterate over entries whose name matches a prefix.
     * Callback receives (entry, inflated_data).
     */
    void for_each_with_prefix(const std::string& prefix,
                               const std::function<void(const ZipEntry&,
                                                        std::vector<char>&&)>& cb) const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace docx
