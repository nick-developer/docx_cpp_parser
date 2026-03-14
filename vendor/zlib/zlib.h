/*
 * vendor/zlib/zlib.h
 *
 * Self-contained, header-only implementation of the zlib API subset used by
 * docx_comment_parser.  Zero external dependencies.
 *
 * API provided (binary-compatible with zlib 1.3):
 *   Types  : Bytef, uInt, uLong, z_stream
 *   Macros : Z_OK, Z_STREAM_END, Z_BUF_ERROR, Z_DATA_ERROR, Z_MEM_ERROR,
 *            Z_FINISH, MAX_WBITS
 *   Funcs  : inflateInit2, inflate, inflateEnd, crc32
 *
 * Restrictions (sufficient for reading ZIP/DOCX files):
 *   - Raw DEFLATE only (windowBits == -MAX_WBITS).
 *   - Single-pass, single-call decompression (Z_FINISH flush mode).
 *   - Output buffer must be pre-allocated to the exact uncompressed size.
 *
 * Usage:
 *   In EXACTLY ONE .cpp file, before including this header:
 *     #define VENDOR_ZLIB_IMPLEMENTATION
 *   All other files just #include it normally.
 *
 * On Linux/macOS/MinGW the system <zlib.h> should be used instead; this
 * file is the fallback for MSVC Windows where no system zlib exists.
 *
 * Algorithm: RFC 1951 (DEFLATE).  CRC-32: ISO 3309 / ITU-T V.42.
 * License: MIT-0 — no attribution required.
 */

#ifndef VENDOR_ZLIB_H_INCLUDED
#define VENDOR_ZLIB_H_INCLUDED

#ifdef __cplusplus
extern "C" {
#endif

#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* ── Types ───────────────────────────────────────────────────────────────── */

typedef unsigned char  Bytef;
typedef unsigned int   uInt;
typedef unsigned long  uLong;

/* Opaque internal state — forward declared to match real zlib's ABI. */
struct internal_state;

typedef struct z_stream_s {
    const Bytef           *next_in;    /* next input byte                  */
    uInt                   avail_in;   /* bytes available at next_in       */
    uLong                  total_in;   /* total bytes consumed (unused)    */
    Bytef                 *next_out;   /* next output byte goes here       */
    uInt                   avail_out;  /* remaining space at next_out      */
    uLong                  total_out;  /* total bytes written              */
    /* padding — present in real zlib, unused by us */
    const char            *msg;
    struct internal_state *state;
    void                  *zalloc;
    void                  *zfree;
    void                  *opaque;
    int                    data_type;
    uLong                  adler;
    uLong                  reserved;
} z_stream;

/* ── Constants ───────────────────────────────────────────────────────────── */

#define Z_OK            0
#define Z_STREAM_END    1
#define Z_NEED_DICT     2
#define Z_ERRNO        (-1)
#define Z_STREAM_ERROR (-2)
#define Z_DATA_ERROR   (-3)
#define Z_MEM_ERROR    (-4)
#define Z_BUF_ERROR    (-5)

#define Z_NO_FLUSH      0
#define Z_SYNC_FLUSH    2
#define Z_FULL_FLUSH    3
#define Z_FINISH        4

#define MAX_WBITS       15

/* ── API declarations ────────────────────────────────────────────────────── */

int   inflateInit2(z_stream *strm, int windowBits);
int   inflate     (z_stream *strm, int flush);
int   inflateEnd  (z_stream *strm);
uLong crc32       (uLong crc, const Bytef *buf, uInt len);

/* ═════════════════════════════════════════════════════════════════════════
 * IMPLEMENTATION — compiled in exactly one translation unit.
 * Define VENDOR_ZLIB_IMPLEMENTATION before including this header in that TU.
 * ═════════════════════════════════════════════════════════════════════════ */
#ifdef VENDOR_ZLIB_IMPLEMENTATION

#ifdef _MSC_VER
#  pragma warning(push)
#  pragma warning(disable: 4244 4267 4100 4127)
#endif

/* ─── Bit buffer ─────────────────────────────────────────────────────────
 *
 * DEFLATE packs bits LSB-first within each byte.  We maintain a 32-bit
 * accumulator that is filled from the source bytes LSB-first.  Reads
 * always consume from the least-significant end.
 */
typedef struct {
    const uint8_t *src;
    size_t         src_len;
    size_t         byte_pos;
    uint32_t       buf;       /* bit accumulator (LSB = next bit to read) */
    int            avail;     /* valid bits in buf                         */
} Bits;

static void bits_init(Bits *b, const uint8_t *src, size_t len) {
    b->src      = src;
    b->src_len  = len;
    b->byte_pos = 0;
    b->buf      = 0;
    b->avail    = 0;
}

/* Ensure at least `need` bits are in the buffer.  Returns 0 on underflow. */
static int bits_ensure(Bits *b, int need) {
    while (b->avail < need) {
        if (b->byte_pos >= b->src_len) return 0;
        b->buf   |= (uint32_t)b->src[b->byte_pos++] << b->avail;
        b->avail += 8;
    }
    return 1;
}

/* Read n bits (consume LSB side). */
static uint32_t bits_read(Bits *b, int n) {
    uint32_t v = b->buf & ((1u << n) - 1u);
    b->buf   >>= n;
    b->avail  -= n;
    return v;
}

/* Discard bits to reach the next byte boundary. */
static void bits_align(Bits *b) {
    int drop  = b->avail & 7;
    b->buf  >>= drop;
    b->avail -= drop;
}

/* ─── Output buffer ──────────────────────────────────────────────────────
 *
 * Wraps the caller-supplied output buffer.  Back-references look back
 * into already-written bytes (no separate ring buffer needed since the
 * whole output is one contiguous array).
 */
typedef struct {
    uint8_t *data;
    size_t   cap;
    size_t   pos;
} Out;

static int out_byte(Out *o, uint8_t c) {
    if (o->pos >= o->cap) return 0;
    o->data[o->pos++] = c;
    return 1;
}

/* Copy `length` bytes from `dist` positions back. */
static int out_backref(Out *o, unsigned dist, unsigned length) {
    if (dist == 0 || dist > o->pos) return 0;
    for (unsigned i = 0; i < length; i++) {
        uint8_t c = o->data[o->pos - dist];
        if (!out_byte(o, c)) return 0;
    }
    return 1;
}

/* ─── Huffman decoder ────────────────────────────────────────────────────
 *
 * DEFLATE Huffman codes are transmitted MSB-first but our bit buffer is
 * LSB-first.  The canonical decode algorithm must therefore read bits
 * one at a time and build the code value MSB-first (shift left then OR
 * the new bit).
 *
 * We store the canonical Huffman table as:
 *   count[b]  = number of symbols with bit-length b
 *   syms[]    = symbols in canonical order (sorted by code value)
 *
 * Decoding: walk bit-lengths 1..max, reading one bit per step.  At each
 * length we check whether the accumulated code falls within the range
 * assigned to that length.  This is the same algorithm used by zlib's
 * own "inflate_table" fast-path fallback.
 */

#define HUFF_MAX_BITS  15
#define HUFF_MAX_SYMS  288

typedef struct {
    uint16_t count[HUFF_MAX_BITS + 1];
    uint16_t syms [HUFF_MAX_SYMS];
    int      max_bits;
} Huff;

static int huff_build(Huff *h, const uint8_t *lens, int nsyms) {
    int i, b;

    memset(h->count, 0, sizeof(h->count));
    h->max_bits = 0;

    for (i = 0; i < nsyms; i++) {
        if (lens[i]) {
            h->count[lens[i]]++;
            if (lens[i] > h->max_bits) h->max_bits = lens[i];
        }
    }
    if (h->max_bits == 0) return 1; /* all-zero lengths: empty tree */

    /*
     * Fill syms[] in canonical order: for each bit-length b (ascending),
     * append all symbols with that length in symbol-value order.
     *
     * This gives the same ordering that huff_decode's `index` variable
     * navigates: after skipping `count[b]` symbols at bit-length b, `index`
     * points to the start of bit-length b+1's symbols.
     *
     * IMPORTANT: we must NOT index syms[] by canonical code value.  For the
     * fixed Huffman tree, code values at bit-length 9 start at 400, which is
     * far past the 288-element array — that was the original out-of-bounds bug.
     */
    int pos = 0;
    for (b = 1; b <= h->max_bits; b++)
        for (i = 0; i < nsyms; i++)
            if (lens[i] == b) h->syms[pos++] = (uint16_t)i;

    return 1;
}

/*
 * Decode one Huffman symbol.
 *
 * We read bits one at a time, accumulating the code MSB-first.
 * At each bit-length b we compare against the range [first, first+count[b]).
 * The index within that range gives us the position in h->syms[].
 *
 * `first` tracks the first canonical code at the current bit-length,
 * `index` tracks the cumulative symbol position up to the current length.
 */
static int huff_decode(Bits *b, const Huff *h) {
    uint32_t code  = 0;
    int      first = 0;   /* first canonical code at current bit-length (as int, not shifted) */
    int      index = 0;   /* cumulative index into h->syms at current length */

    for (int bits = 1; bits <= h->max_bits; bits++) {
        if (!bits_ensure(b, 1)) return -1;
        /* Build code MSB-first: shift left, then OR the new bit. */
        code = (code << 1) | bits_read(b, 1);

        int count = h->count[bits];
        /* If code < first + count, it belongs to this bit-length. */
        if ((int)code - count < first) {
            return h->syms[index + (int)code - first];
        }
        index += count;
        first  = (first + count) << 1;
    }
    return -1; /* overrun — corrupt stream */
}

/* ─── RFC 1951 tables ────────────────────────────────────────────────────── */

static const uint16_t LEN_BASE[29] = {
    3,4,5,6,7,8,9,10,11,13,15,17,19,23,27,31,
    35,43,51,59,67,83,99,115,131,163,195,227,258
};
static const uint8_t LEN_EXTRA[29] = {
    0,0,0,0,0,0,0,0,1,1,1,1,2,2,2,2,3,3,3,3,4,4,4,4,5,5,5,5,0
};
static const uint16_t DIST_BASE[30] = {
    1,2,3,4,5,7,9,13,17,25,33,49,65,97,129,193,
    257,385,513,769,1025,1537,2049,3073,
    4097,6145,8193,12289,16385,24577
};
static const uint8_t DIST_EXTRA[30] = {
    0,0,0,0,1,1,2,2,3,3,4,4,5,5,6,6,7,7,8,8,9,9,10,10,11,11,12,12,13,13
};

/* RFC 1951 §3.2.7 — order in which code-length alphabet lengths arrive */
static const uint8_t CL_PERM[19] = {
    16,17,18,0,8,7,9,6,10,5,11,4,12,3,13,2,14,1,15
};

/* ─── Fixed Huffman trees (RFC 1951 §3.2.6) ─────────────────────────────── */

static void make_fixed(Huff *lit, Huff *dist) {
    uint8_t ll[288], dl[32];
    int i;
    for (i =   0; i < 144; i++) ll[i] = 8;
    for (i = 144; i < 256; i++) ll[i] = 9;
    for (i = 256; i < 280; i++) ll[i] = 7;
    for (i = 280; i < 288; i++) ll[i] = 8;
    huff_build(lit, ll, 288);
    for (i = 0; i < 32; i++) dl[i] = 5;
    huff_build(dist, dl, 32);
}

/* ─── Block decoders ─────────────────────────────────────────────────────── */

static int decode_stored(Bits *b, Out *o) {
    bits_align(b);
    /* LEN and NLEN are stored as two little-endian 16-bit values. */
    if (!bits_ensure(b, 32)) return Z_DATA_ERROR;
    uint32_t len  = bits_read(b, 16);
    uint32_t nlen = bits_read(b, 16);
    if ((uint16_t)(len ^ nlen) != 0xFFFFu) return Z_DATA_ERROR;
    for (uint32_t i = 0; i < len; i++) {
        if (!bits_ensure(b, 8)) return Z_DATA_ERROR;
        if (!out_byte(o, (uint8_t)bits_read(b, 8))) return Z_BUF_ERROR;
    }
    return Z_OK;
}

static int decode_huffman(Bits *b, Out *o, const Huff *lit, const Huff *dist) {
    for (;;) {
        int sym = huff_decode(b, lit);
        if (sym < 0) return Z_DATA_ERROR;

        if (sym < 256) {
            if (!out_byte(o, (uint8_t)sym)) return Z_BUF_ERROR;
        } else if (sym == 256) {
            break; /* end-of-block */
        } else {
            /* Length-distance back-reference */
            int li = sym - 257;
            if (li < 0 || li >= 29) return Z_DATA_ERROR;
            if (!bits_ensure(b, LEN_EXTRA[li])) return Z_DATA_ERROR;
            int length = LEN_BASE[li] + (int)bits_read(b, LEN_EXTRA[li]);

            int di = huff_decode(b, dist);
            if (di < 0 || di >= 30) return Z_DATA_ERROR;
            if (!bits_ensure(b, DIST_EXTRA[di])) return Z_DATA_ERROR;
            int distance = DIST_BASE[di] + (int)bits_read(b, DIST_EXTRA[di]);

            if (!out_backref(o, (unsigned)distance, (unsigned)length))
                return Z_DATA_ERROR;
        }
    }
    return Z_OK;
}

static int decode_dynamic(Bits *b, Out *o) {
    if (!bits_ensure(b, 14)) return Z_DATA_ERROR;
    int hlit  = (int)bits_read(b, 5) + 257;
    int hdist = (int)bits_read(b, 5) + 1;
    int hclen = (int)bits_read(b, 4) + 4;

    /* Read code-length alphabet lengths (in RFC-1951 permutation order). */
    uint8_t cl_lens[19] = {0};
    for (int i = 0; i < hclen; i++) {
        if (!bits_ensure(b, 3)) return Z_DATA_ERROR;
        cl_lens[CL_PERM[i]] = (uint8_t)bits_read(b, 3);
    }
    Huff cl;
    huff_build(&cl, cl_lens, 19);

    /* Decode literal/length and distance code-lengths together. */
    uint8_t lens[288 + 32];
    memset(lens, 0, sizeof(lens));
    int total = hlit + hdist;
    int i = 0;
    while (i < total) {
        int sym = huff_decode(b, &cl);
        if (sym < 0) return Z_DATA_ERROR;
        if (sym < 16) {
            lens[i++] = (uint8_t)sym;
        } else if (sym == 16) {
            if (!bits_ensure(b, 2)) return Z_DATA_ERROR;
            int rep  = 3 + (int)bits_read(b, 2);
            uint8_t v = (i > 0) ? lens[i - 1] : 0;
            while (rep-- && i < total) lens[i++] = v;
        } else if (sym == 17) {
            if (!bits_ensure(b, 3)) return Z_DATA_ERROR;
            int rep = 3 + (int)bits_read(b, 3);
            while (rep-- && i < total) lens[i++] = 0;
        } else if (sym == 18) {
            if (!bits_ensure(b, 7)) return Z_DATA_ERROR;
            int rep = 11 + (int)bits_read(b, 7);
            while (rep-- && i < total) lens[i++] = 0;
        } else {
            return Z_DATA_ERROR;
        }
    }

    Huff lit_h, dist_h;
    huff_build(&lit_h,  lens,        hlit);
    huff_build(&dist_h, lens + hlit, hdist);
    return decode_huffman(b, o, &lit_h, &dist_h);
}

/* ─── Main inflate entry point ───────────────────────────────────────────── */

static int run_inflate(const uint8_t *src, size_t src_len,
                       uint8_t *dst, size_t dst_cap,
                       size_t *written) {
    Bits b; bits_init(&b, src, src_len);
    Out  o; o.data = dst; o.cap = dst_cap; o.pos = 0;

    int bfinal;
    do {
        if (!bits_ensure(&b, 3)) return Z_DATA_ERROR;
        bfinal    = (int)bits_read(&b, 1);
        int btype = (int)bits_read(&b, 2);

        int rc;
        if (btype == 0) {
            rc = decode_stored(&b, &o);
        } else if (btype == 1) {
            Huff lit, dist;
            make_fixed(&lit, &dist);
            rc = decode_huffman(&b, &o, &lit, &dist);
        } else if (btype == 2) {
            rc = decode_dynamic(&b, &o);
        } else {
            return Z_DATA_ERROR;
        }
        if (rc != Z_OK) return rc;
    } while (!bfinal);

    *written = o.pos;
    return Z_STREAM_END;
}

/* ─── internal_state (opaque, never accessed by callers) ─────────────────── */

struct internal_state { int _unused; };

/* ─── Public API ─────────────────────────────────────────────────────────── */

int inflateInit2(z_stream *strm, int windowBits) {
    (void)windowBits;
    /* Do NOT memset the whole struct — the caller sets next_in/avail_in/
     * next_out/avail_out BEFORE calling inflateInit2, matching real zlib
     * usage.  We have no internal state to allocate, so this is a no-op
     * beyond zeroing the fields we actually own. */
    strm->total_in  = 0;
    strm->total_out = 0;
    strm->msg       = 0;
    strm->state     = 0;
    return Z_OK;
}

int inflate(z_stream *strm, int flush) {
    (void)flush;
    size_t written = 0;
    int rc = run_inflate(
        (const uint8_t *)strm->next_in,  (size_t)strm->avail_in,
        (uint8_t       *)strm->next_out, (size_t)strm->avail_out,
        &written
    );
    strm->total_out = (uLong)written;
    strm->avail_out = (uInt)((size_t)strm->avail_out - written);
    return rc;
}

int inflateEnd(z_stream *strm) {
    (void)strm;
    return Z_OK;
}

/* ─── CRC-32 (polynomial 0xEDB88320, ISO 3309 / ITU-T V.42) ─────────────── */

static uint32_t g_crc_table[256];
static int      g_crc_ready = 0;

static void build_crc_table(void) {
    uint32_t n, c;
    for (n = 0; n < 256; n++) {
        c = n;
        int k;
        for (k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
        g_crc_table[n] = c;
    }
    g_crc_ready = 1;
}

uLong crc32(uLong crc, const Bytef *buf, uInt len) {
    if (!g_crc_ready) build_crc_table();
    if (!buf) return 0;
    uint32_t c = (uint32_t)crc ^ 0xFFFFFFFFu;
    uInt i;
    for (i = 0; i < len; i++)
        c = g_crc_table[(c ^ buf[i]) & 0xFFu] ^ (c >> 8);
    return (uLong)(c ^ 0xFFFFFFFFu);
}

#ifdef _MSC_VER
#  pragma warning(pop)
#endif

#endif /* VENDOR_ZLIB_IMPLEMENTATION */

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* VENDOR_ZLIB_H_INCLUDED */
