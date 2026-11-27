"""Checks for the static container decoder and the Zstandard decoders.

    python3 tests/codecs_test.py                 (from the repository root; needs bin/luau for the Luau decoder)
    LURAPH_CORPUS=/path/to/obfuscator-samples/Luraph python3 tests/codecs_test.py Corpus

Without a corpus (everything is synthetic, built by the test itself):

* devm/zstd.lua on Zstandard frames
  - embedded as hex (made once with libzstd from synthetic text), so the decoder is tested where no
    libzstd exists;
  - hand-made by this file for the parts of the format an encoder rarely emits (RLE literals, Huffman
    tree as direct weights, tree reuse, sequence codes in RLE mode, every repeat-offset case, a match
    further back than one 32-bit read can express);
  - generated with the system's libzstd at test time when it loads (levels 1-19, with and without
    checksum and content size, stored / RLE / compressed blocks, sizes around the block boundary,
    several frames in a row, skippable frames);
  and that every part of the format was really exercised (the decoder's own counters), that damaged
  input ends in `zstd: ...` errors, and that the size limit holds.
* devm/zstd.py: libzstd and the Luau fallback give the same bytes; errors are ZstdError.
* devm/container_static.py on miniature containers built here from synthetic Lua text with Python's
  lzma module: one and two payloads, both byte orders, the variant without string.pack, the Zstd
  variant, the v15 object form, chunk names, what stands around a container, texts that are not
  containers, damaged ones; and on examples/v15.2/sample.luau against the sandbox (devm/unwrap.lua).

With LURAPH_CORPUS (a directory with one sub-directory per Luraph version; files are picked by
directory and by what they turn out to be, never by name; LURAPH_CORPUS_SKIP may name a file listing
relative paths to leave alone): the static decoder against the sandbox (devm/unwrap.lua) on containers
of every version, and devm/zstd.lua against libzstd on the frames of Zstd containers.
"""
import lzma
import os
import random
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from devm import container_static, zstd  # noqa: E402

ZSTD_LUA = os.path.join(ROOT, 'devm', 'zstd.lua')
UNWRAP_LUA = os.path.join(ROOT, 'devm', 'unwrap.lua')
CORPUS = os.environ.get('LURAPH_CORPUS')


def find_luau():
    try:
        return zstd._luau()
    except zstd.ZstdError:
        return None


LUAU = find_luau()
HAVE_LIB = bool(zstd._libzstd())

# ------------------------------------------------------------------------------------------------ synthetic data


def synthetic(n):
    """Lua-looking text, the same on every run."""
    out = []
    size = i = 0
    while size < n:
        line = 'local v%d = t[%d] + %d -- %s\n' % (i, i * 7 % 13, i * i % 97, 'xyz'[i % 3] * (i % 5))
        out.append(line)
        size += len(line)
        i += 1
    return ''.join(out).encode()[:n]


# frames libzstd 1.5.5 made of synthetic(n): (n, hex)
HEX_FRAMES = {
    'tiny_level3': (40, '28b52ffd2028250100e06c6f63616c207630203d20745b305d202b2030202d2d200a31375d2002004008369b63'),
    'small_level19_checksum': (700, '28b52ffd64bc01b5060022881611a0ed9c5f72388087dbad49292ee0770d0ca16cea6d5e9376b9f4774f969c7466f492ced7'
        '987379ddfe1ec23f6df6b5f7e7b23ed1536dd3a87f0cb3299b0aa5d47c7bce9910d7da5ffa7310c6082488c0c88a410522c3'
        '6b0ebe4ba8418c7cfdcfd026aa60ec011038299091224d3aa9645c3aaadaf6ca29f45668aa8542c86f5cc8a8aa6db528332d'
        '0fd42886e717b207454dc4a297d927f41c13d70815c2ae361f4722c2c30120d253731b837431a169d523d56057a70ce90496'
        'ea1d9eb61995f7db780c0f83b53a4ed6e638586b61c5a902be21ccba'),
    'medium_level1_checksum_nosize': (1500, '28b52ffd04087d0d00720e2417902769030d1557876a7a77d794dddd9bf6d51c5cefc7131d863e3b5aee553895e722f5cb1f'
        'd65d9e5161d193e8841e9a1b899059de107d5566f544fa9c6a8ee81bf244aca3b41a274ddd72e6e5199329c8e5132bcf7c17'
        '3cf733d3d8454ca0d33a5a08c9f78e965b4d474b5651d8104110d9d1822f14a8a3c553b8a3e50cd06128e9682102060d2839'
        '286180a0801080caa8b14dad9afd0ef04291b21b1028af246e03642b1f16fb5ddc9563711a32d798b8beeab9e218905f9aed'
        'db7d54c48563f14b7bc9b9d344e9582ef5cce208d0cfc7d88a5c00f388632ccbd7aa7db539a1e3722fa8d9e5ce98aa95db86'
        'b1ee0077e0384c0008bf06e990a58a91aa84e37aa0ef0db79743550b6e815bf1c96d001a722c056ca5b542c57162bcca59f6'
        '4ec1320df7b0af7cacda2ccf6f0bbadb912a5b719fcb159e2238d66da9f337c474891a8e31c4cbb838c2c4b2579b90e04f55'
        'b7dd94ce541e55753ba99532f62e4c181b598cb7cd9418cc959e54f9eacb5937a598b7c48d181d01c350693f113ac5dfbe60'
        '7d4df8b7541ca40b5e3a093c1d37172873d89faa2315d1f2e5b8245eb416cf8bc63278d19ad5a20ae03c895a'),
}

# ------------------------------------------------------------------------------------------------ hand-made frames

MAGIC = b'\x28\xb5\x2f\xfd'
LL_BASE = list(range(16)) + [16, 18, 20, 22, 24, 28, 32, 40, 48, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536]
LL_BITS = [0] * 16 + [1, 1, 1, 1, 2, 2, 3, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
ML_BASE = list(range(3, 35)) + [35, 37, 39, 41, 43, 47, 51, 59, 67, 83, 99, 131, 259, 515, 1027, 2051, 4099, 8195, 16387, 32771, 65539]
ML_BITS = [0] * 32 + [1, 1, 1, 1, 2, 2, 3, 3, 4, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]


def backward_bits(fields):
    """[(value, number of bits)] in the order a decoder reads them -> the bytes of a backward bit stream."""
    acc = 1
    for v, nb in fields:
        assert 0 <= v < (1 << nb) or (nb == 0 and v == 0)
        acc = (acc << nb) | v
    return acc.to_bytes((acc.bit_length() + 7) // 8, 'little')


def z_block(kind, body, last=False, size=None):
    """kind 0 raw, 1 RLE (body = the byte, size = the run), 2 compressed"""
    n = len(body) if size is None else size
    return struct.pack('<I', (1 if last else 0) | (kind << 1) | (n << 3))[:3] + body


def z_frame(blocks, window_log=20, checksum=None):
    return MAGIC + bytes([4 if checksum is not None else 0, (window_log - 10) << 3]) + b''.join(blocks) + (
        struct.pack('<I', checksum) if checksum is not None else b'')


def z_raw_frame(data):
    """`data` as a frame of raw (stored) blocks: a valid Zstandard frame without any compressor."""
    parts = [data[i:i + 100000] for i in range(0, len(data), 100000)] or [b'']
    return z_frame([z_block(0, p, last=(i == len(parts) - 1)) for i, p in enumerate(parts)])


def lit_raw(data):
    return struct.pack('<I', 0 | (3 << 2) | (len(data) << 4))[:3] + data


def lit_rle(byte, n):
    return struct.pack('<H', 1 | (1 << 2) | (n << 4)) + bytes([byte])


def huffman_codes(weights):
    """weights[symbol] (0 = unused), all symbols given -> {symbol: (code, bits)}"""
    total = sum(1 << (w - 1) for w in weights if w)
    maxbits = total.bit_length() - 1
    assert total == 1 << maxbits, 'weights must fill a power of two'
    nxt = [0] * (maxbits + 2)
    for w in weights:
        if w:
            nxt[w + 1] += 1 << (w - 1)
    for w in range(2, maxbits + 2):
        nxt[w] += nxt[w - 1]
    codes = {}
    for sym, w in enumerate(weights):
        if w:
            codes[sym] = (nxt[w] >> (w - 1), maxbits + 1 - w)
            nxt[w] += 1 << (w - 1)
    return codes


def tree_direct(weights):
    """Huffman tree description with direct weights; the last symbol's weight is implied."""
    explicit = weights[:-1]
    out = bytearray([127 + len(explicit)])
    for i in range(0, len(explicit), 2):
        out.append((explicit[i] << 4) | (explicit[i + 1] if i + 1 < len(explicit) else 0))
    return bytes(out)


def lit_huffman(data, weights, streams=1, tree=True):
    """compressed literals section; tree=False reuses the previous tree"""
    codes = huffman_codes(weights)

    def stream(part):
        return backward_bits([codes[b] for b in part])
    desc = tree_direct(weights) if tree else b''
    if streams == 1:
        body, sf = desc + stream(data), 0
    else:
        per = (len(data) + 3) // 4
        parts = [stream(data[i * per:(i + 1) * per]) for i in range(4)]
        body, sf = desc + struct.pack('<HHH', len(parts[0]), len(parts[1]), len(parts[2])) + b''.join(parts), 1
    assert len(data) < 1024 and len(body) < 1024
    return struct.pack('<I', (2 if tree else 3) | (sf << 2) | (len(data) << 4) | (len(body) << 14))[:3] + body


def code_of(value, base, bits):
    for c in range(len(base) - 1, -1, -1):
        if base[c] <= value:
            assert value - base[c] < (1 << bits[c])
            return c, value - base[c], bits[c]
    raise ValueError(value)


def sequences_rle(seqs, repeat=False):
    """Sequences section with all three codes in RLE mode.  seqs: [(literal length, offset value as coded:
    offset + 3, or 1..3 for a repeat offset, match length)], all with the same three codes.
    repeat=True: the section says "same tables as the block before" instead of naming the codes."""
    fields = []
    codes = None
    for ll, ofv, ml in seqs:
        lc, lx, lb = code_of(ll, LL_BASE, LL_BITS)
        mc, mx, mb = code_of(ml, ML_BASE, ML_BITS)
        oc = ofv.bit_length() - 1
        assert codes in (None, (lc, oc, mc)), 'RLE mode: one code per kind'
        codes = (lc, oc, mc)
        fields += [(ofv - (1 << oc), oc), (mx, mb), (lx, lb)]
    if repeat:
        return bytes([len(seqs), (3 << 6) | (3 << 4) | (3 << 2)]) + backward_bits(fields)
    return bytes([len(seqs), (1 << 6) | (1 << 4) | (1 << 2), codes[0], codes[1], codes[2]]) + backward_bits(fields)


def run_sequences(history, literals, seqs, rep=None):
    """what a block with these literals and sequences regenerates after `history` (reference executor).
    rep: the three repeat offsets, updated in place (a frame starts with 1, 4, 8)"""
    out = bytearray(history)
    rep = [1, 4, 8] if rep is None else rep
    lp = 0
    for ll, ofv, ml in seqs:
        out += literals[lp:lp + ll]
        lp += ll
        if ofv > 3:
            off = ofv - 3
            rep[:] = [off, rep[0], rep[1]]
        else:
            idx = ofv + (1 if ll == 0 else 0)
            if idx == 1:
                off = rep[0]
            elif idx == 2:
                off = rep[1]
                rep[:] = [off, rep[0], rep[2]]
            elif idx == 3:
                off = rep[2]
                rep[:] = [off, rep[0], rep[1]]
            else:
                off = rep[0] - 1
                rep[:] = [off, rep[0], rep[1]]
        assert 0 < off <= len(out)
        for _ in range(ml):
            out.append(out[-off])
    out += literals[lp:]
    return bytes(out[len(history):])


def handmade_frames():
    """[(name, frame, content)]"""
    out = []
    first = b'0123456789ABCDEFGHIJ'
    rnd = random.Random(7)
    out.append(('rle_literals', z_frame([z_block(2, lit_rle(0x61, 300) + b'\x00', last=True)]), b'a' * 300))
    # all three sequence codes in RLE mode; offsets into an earlier raw block, one overlapping copy (offset 2 < length 7)
    seqs = [(5, 16 + x, 7) for x in (0, 3, 9, 15, 4)]
    out.append(('rle_sequence_codes', z_frame([z_block(0, first), z_block(2, lit_rle(0x71, 40) + sequences_rle(seqs), last=True)]),
                first + run_sequences(first, b'q' * 40, seqs)))
    # the next block reuses the three tables ("repeat" mode)
    more = [(5, 16 + x, 7) for x in (1, 14)]
    lits2 = b'0123456789'
    rep = [1, 4, 8]
    one = run_sequences(first, b'q' * 40, seqs, rep)
    two = run_sequences(first + one, lits2, more, rep)
    out.append(('repeat_sequence_codes', z_frame([z_block(0, first), z_block(2, lit_rle(0x71, 40) + sequences_rle(seqs)),
                                                  z_block(2, lit_raw(lits2) + sequences_rle(more, repeat=True), last=True)]),
                first + one + two))
    seqs = [(4, 3 + 2, 7), (4, 3 + 3, 7)]
    out.append(('overlapping_matches', z_frame([z_block(0, first), z_block(2, lit_raw(b'wxyzWXYZ') + sequences_rle(seqs), last=True)]),
                first + run_sequences(first, b'wxyzWXYZ', seqs)))
    # the repeat offsets: values 1..3 with and without literals in front; one sequence per block, so the
    # history must also survive from block to block
    seqs = [(2, 3 + 10, 4), (0, 3, 4), (2, 1, 4), (0, 1, 4), (2, 3, 4), (0, 2, 4), (2, 2, 4)]
    lits = b'abcdefgh'
    blocks = [z_block(0, first)]
    lp = 0
    for s in seqs:
        blocks.append(z_block(2, lit_raw(lits[lp:lp + s[0]]) + sequences_rle([s])))
        lp += s[0]
    blocks.append(z_block(0, b'', last=True))
    out.append(('repeat_offsets', z_frame(blocks), first + run_sequences(first, lits, seqs)))
    # Huffman literals with the tree written as direct weights: one stream, four streams, tree reuse
    weights = [0] * 97 + [4, 3, 2, 1, 1]            # a b c d e
    data = bytes(rnd.choice(b'aaaaaaaabbbbccde') for _ in range(700))
    out.append(('huffman_direct_1_stream', z_frame([z_block(2, lit_huffman(data[:200], weights, 1) + b'\x00', last=True)]), data[:200]))
    out.append(('huffman_direct_4_streams', z_frame([z_block(2, lit_huffman(data, weights, 4) + b'\x00', last=True)]), data))
    out.append(('huffman_tree_reuse', z_frame([z_block(2, lit_huffman(data, weights, 4) + b'\x00'),
                                               z_block(2, lit_huffman(data[100:400], weights, 4, tree=False) + b'\x00'),
                                               z_block(2, lit_huffman(data[5:90], weights, 1, tree=False) + b'\x00', last=True)]),
                data + data[100:400] + data[5:90]))
    weights = [11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1]  # code lengths 1 .. 11
    data = bytes(rnd.choice([0] * 40 + [1] * 20 + [2] * 10 + [3] * 5 + [4, 4, 5, 6, 7, 8, 9, 10, 11]) for _ in range(900))
    out.append(('huffman_11_bits', z_frame([z_block(2, lit_huffman(data, weights, 4) + b'\x00', last=True)]), data))
    out.append(('raw_and_rle_blocks', z_frame([z_block(0, b'abc'), z_block(1, b'z', size=1000), z_block(0, b'', last=True)]),
                b'abc' + b'z' * 1000))
    out.append(('empty_frame', z_frame([z_block(0, b'', last=True)]), b''))
    out.append(('stored', z_raw_frame(synthetic(250000)), synthetic(250000)))
    return out


def long_offset_frame():
    """a match 2^26 + something bytes back: offset code 26, more extra bits than one 32-bit read holds"""
    head = b'HELLO-LONG-OFFSET'
    nrle = 513
    seq = (0, nrle * 131072 + len(head) + 3, len(head))
    blocks = [z_block(0, head)] + [z_block(1, b'x', size=131072)] * nrle + [z_block(2, lit_raw(b'') + sequences_rle([seq]), last=True)]
    return z_frame(blocks, window_log=27), head + b'x' * (nrle * 131072) + head


# ------------------------------------------------------------------------------------------------ libzstd as encoder

_enc = None


def lib_compress(data, level=3, checksum=False, content_size=True, window_log=None):
    """One frame made by the system's libzstd (only called when it loads)."""
    global _enc
    import ctypes
    if _enc is None:
        lib = zstd._libzstd()
        lib.ZSTD_createCCtx.restype = ctypes.c_void_p
        lib.ZSTD_freeCCtx.argtypes = [ctypes.c_void_p]
        lib.ZSTD_CCtx_setParameter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.ZSTD_CCtx_setParameter.restype = ctypes.c_size_t
        lib.ZSTD_compress2.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
        lib.ZSTD_compress2.restype = ctypes.c_size_t
        lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
        lib.ZSTD_compressBound.restype = ctypes.c_size_t
        _enc = lib
    lib = _enc
    ctx = lib.ZSTD_createCCtx()
    try:
        params = [(100, level), (201, int(checksum)), (200, int(content_size))] + ([(101, window_log)] if window_log else [])
        for key, value in params:                 # ZSTD_c_compressionLevel, _checksumFlag, _contentSizeFlag, _windowLog
            r = lib.ZSTD_CCtx_setParameter(ctx, key, value)
            assert not lib.ZSTD_isError(r), lib.ZSTD_getErrorName(r)
        cap = lib.ZSTD_compressBound(len(data))
        buf = ctypes.create_string_buffer(cap)
        r = lib.ZSTD_compress2(ctx, buf, cap, data, len(data))
        assert not lib.ZSTD_isError(r), lib.ZSTD_getErrorName(r)
        return buf.raw[:r]
    finally:
        lib.ZSTD_freeCCtx(ctx)


def generated_frames():
    """[(name, frame, content)] from libzstd: every level, header variants, block kinds, boundary sizes."""
    rnd = random.Random(20261126)
    words = [''.join(rnd.choice('abcdefghijklmnopqrstuvwxyz') for _ in range(rnd.randint(2, 9))) for _ in range(300)]

    def text(n):
        parts = []
        size = 0
        while size < n:
            w = rnd.choice(words) + rnd.choice([' ', ' ', ', ', '.\n', '(', ') ', ' = '])
            parts.append(w)
            size += len(w)
        return ''.join(parts).encode()[:n]

    def records(n):
        buf = bytearray()
        x = 0
        while len(buf) < n:
            x += rnd.randint(0, 3)
            buf += struct.pack('<IHBB', x, rnd.randint(0, 40), rnd.choice((0, 0, 0, 1, 7)), rnd.randint(0, 255))
        return bytes(buf[:n])
    data = {
        'empty': b'', 'one_byte': b'x', 'run_1000': b'a' * 1000, 'run_300000': b'\0' * 300000, 'period_3': b'abc' * 30000,
        'random_100': rnd.randbytes(100), 'random_150000': rnd.randbytes(150000),
        'random_twice': (lambda r: r + r)(rnd.randbytes(70000)),
        'text_60': text(60), 'text_1000': text(1000), 'text_40000': text(40000), 'text_131071': text(131071),
        'text_131072': text(131072), 'text_131073': text(131073), 'text_300000': text(300000),
        'synthetic_50000': synthetic(50000), 'records_60000': records(60000),
        'skewed': bytes(rnd.choice(b'aaaaaaaaaaaaaaaabbbbcd') for _ in range(60000)),
        'mixed': text(50000) + rnd.randbytes(140000) + b'q' * 150000 + records(40000),
    }
    out = []
    for name, d in data.items():
        for level in range(1, 20):
            if len(d) > 100000 and level not in (1, 3, 9, 19):
                continue
            cs, fcs = level % 2 == 1, level % 3 != 0
            out.append(('%s level %d%s%s' % (name, level, ' checksum' if cs else '', '' if fcs else ' no size'),
                        lib_compress(d, level, cs, fcs), d))
        out.append((name + ' window 2^10', lib_compress(d, 5, True, False, 10), d))
        out.append((name + ' fast', lib_compress(d, -5, True, True), d))
    a, b, c = data['text_1000'], data['random_100'], data['synthetic_50000']
    skip = struct.pack('<II', 0x184D2A53, 11) + b'hello world'
    out.append(('three frames', lib_compress(a) + lib_compress(b, 9, True) + lib_compress(c, 19, True, False), a + b + c))
    out.append(('skippable frames', skip + lib_compress(a) + skip + lib_compress(b) + struct.pack('<II', 0x184D2A50, 0), a + b))
    return out


# ------------------------------------------------------------------------------------------------ running zstd.lua

DRIVER = r'''
-- luau driver.lua -a ZSTD_LUA MANIFEST REPORT      (manifest: name <TAB> input file <TAB> expected file | "-" | "limit:N")
local zpath, manifest, report = ...
local zstd = assert(loadstring(readfile(zpath), "=zstd"))()
local out, stats = {}, {}
for line in string.gmatch(readfile(manifest), "[^\n]+") do
	local name, inp, exp = string.match(line, "^([^\t]*)\t([^\t]*)\t([^\t]*)$")
	local limit = tonumber(string.match(exp, "^limit:(%d+)$"))
	local t = os.clock()
	local ok, res = pcall(zstd.decompress, buffer.fromstring(readfile(inp)), limit, stats)
	local dt = os.clock() - t
	if not ok then
		table.insert(out, "error\t" .. name .. "\t" .. string.gsub(tostring(res), "%s", " "))
	elseif exp == "-" or limit then
		table.insert(out, "data\t" .. name .. "\t" .. buffer.len(res))
	else
		local want = readfile(exp)
		local same = buffer.len(res) == #want and buffer.tostring(res) == want
		table.insert(out, string.format("%s\t%s\t%d\t%.4f", same and "same" or "differ", name, buffer.len(res), dt))
	end
end
for k, v in stats do table.insert(out, "stat\t" .. k .. "\t" .. v) end
writefile(report, table.concat(out, "\n") .. "\n")
'''


def run_lua(cases, timeout=600):
    """cases: [(name, frame, expected bytes | None (an error is expected) | int (size limit))]
    -> ({name: (verdict, detail)}, {counter: value})"""
    with tempfile.TemporaryDirectory(prefix='codecs-test-') as tmp:
        man = []
        for i, (name, frame, want) in enumerate(cases):
            src = os.path.join(tmp, '%d.zst' % i)
            with open(src, 'wb') as f:
                f.write(frame)
            if want is None:
                exp = '-'
            elif isinstance(want, int):
                exp = 'limit:%d' % want
            else:
                exp = os.path.join(tmp, '%d.bin' % i)
                with open(exp, 'wb') as f:
                    f.write(want)
            man.append('%d\t%s\t%s' % (i, src, exp))
        for name, text in (('driver.lua', DRIVER), ('manifest.tsv', '\n'.join(man) + '\n')):
            with open(os.path.join(tmp, name), 'w') as f:
                f.write(text)
        r = subprocess.run([LUAU, os.path.join(tmp, 'driver.lua'), '-a', ZSTD_LUA, os.path.join(tmp, 'manifest.tsv'),
                            os.path.join(tmp, 'report.tsv')], capture_output=True, text=True, errors='replace', timeout=timeout)
        if r.returncode != 0 or not os.path.exists(os.path.join(tmp, 'report.tsv')):
            raise AssertionError('the Luau driver failed: ' + (r.stderr or r.stdout)[-2000:])
        res, stats = {}, {}
        with open(os.path.join(tmp, 'report.tsv'), encoding='latin-1') as f:
            for line in f.read().split('\n'):
                p = line.split('\t')
                if p[0] == 'stat':
                    stats[p[1]] = int(p[2])
                elif len(p) >= 3:
                    res[cases[int(p[1])][0]] = (p[0], p[2])
        return res, stats


@unittest.skipUnless(LUAU, 'bin/luau is not there')
class ZstdLua(unittest.TestCase):
    def check_same(self, cases):
        res, stats = run_lua(cases)
        bad = ['%s: %s %s' % (name, res.get(name, ('missing', ''))[0], res.get(name, ('', ''))[1])
               for name, _f, _w in cases if res.get(name, ('missing',))[0] != 'same']
        self.assertEqual(bad, [])
        return stats

    def test_embedded_frames(self):
        cases = [(name, bytes.fromhex(hx), synthetic(n)) for name, (n, hx) in HEX_FRAMES.items()]
        two = cases[0][1] + cases[1][1]
        cases.append(('two frames', two, cases[0][2] + cases[1][2]))
        stats = self.check_same(cases)
        self.assertEqual(stats.get('frames'), 5)
        self.assertGreaterEqual(stats.get('checksums_verified', 0), 3)
        self.assertGreaterEqual(stats.get('literals_huffman_1_stream', 0) + stats.get('literals_huffman_4_streams', 0), 1)
        self.assertGreaterEqual(stats.get('frames_without_content_size', 0), 1)

    def test_handmade_frames(self):
        stats = self.check_same(handmade_frames())
        for key in ('literals_rle', 'literals_raw', 'huffman_tree_direct', 'literals_treeless', 'literals_huffman_1_stream',
                    'literals_huffman_4_streams', 'll_rle', 'of_rle', 'ml_rle', 'll_repeat', 'of_repeat', 'ml_repeat',
                    'blocks_raw', 'blocks_rle', 'blocks_without_sequences'):
            self.assertGreater(stats.get(key, 0), 0, key)

    def test_long_offset(self):
        frame, want = long_offset_frame()
        self.check_same([('offset beyond 2^26', frame, want)])

    def test_damaged_input(self):
        n, hx = HEX_FRAMES['small_level19_checksum']
        good = bytes.fromhex(hx)
        rnd = random.Random(3)
        cases = [('cut %d' % k, good[:k], None) for k in list(range(0, 24)) + [len(good) // 2, len(good) - 5, len(good) - 1]]
        cases += [('bad magic', b'\x29' + good[1:], None), ('stray bytes', good + b'\x00\x01', None),
                  ('checksum', good[:-1] + bytes([good[-1] ^ 1]), None), ('reserved bit', good[:4] + bytes([good[4] | 8]) + good[5:], None),
                  ('dictionary', MAGIC + b'\x01\x00\x07' + good[6:], None), ('noise', rnd.randbytes(3000), None),
                  ('reserved block type', z_frame([z_block(3, b'abc', last=True)]), None),
                  ('treeless without a tree', z_frame([z_block(2, lit_huffman(b'aabb', [0] * 97 + [1, 1], 1, tree=False) + b'\x00', last=True)]), None),
                  ('offset before the start', z_frame([z_block(2, lit_raw(b'abcd') + sequences_rle([(4, 3 + 9, 5)]), last=True)]), None),
                  ('endless block list', MAGIC + b'\x00\x50' + z_block(1, b'x', size=100000) * 5, None)]
        for i in range(400):
            g = bytearray(good)
            for _ in range(rnd.randint(1, 3)):
                g[rnd.randrange(4, len(g))] ^= 1 << rnd.randrange(8)
            cases.append(('flip %d' % i, bytes(g), synthetic(n)))
        res, _stats = run_lua(cases)
        self.assertEqual(len(res), len(cases))
        errors = 0
        for name, (verdict, detail) in res.items():
            if name == 'cut 0':
                self.assertEqual((verdict, detail), ('data', '0'))        # no frame at all: no data
            elif name.startswith('flip') and verdict == 'same':
                pass                            # the flipped bit carries no information (the header has an unused bit)
            else:
                # the frame has a checksum: damage never goes through as different data
                self.assertEqual(verdict, 'error', '%s: decoded although the frame is damaged' % name)
                self.assertTrue(detail.startswith('zstd: '), '%s: %s' % (name, detail))
                errors += 1
        self.assertGreater(errors, 400)

    def test_size_limit(self):
        bomb = z_frame([z_block(1, b'x', size=131072)] * 200 + [z_block(0, b'', last=True)])        # 26 MB from 800 bytes
        res, _ = run_lua([('within', bomb, 30000000), ('beyond', bomb, 1000000),
                          ('announced', MAGIC + b'\xe0' + struct.pack('<Q', 1 << 40) + z_block(0, b'', last=True), 1 << 28)])
        self.assertEqual(res['within'], ('data', str(200 * 131072)))
        self.assertEqual(res['beyond'][0], 'error')
        self.assertIn('limit', res['beyond'][1])
        self.assertEqual(res['announced'][0], 'error')
        self.assertIn('limit', res['announced'][1])

    @unittest.skipUnless(HAVE_LIB, 'libzstd is not installed')
    def test_against_libzstd(self):
        cases = generated_frames()
        for name, frame, want in cases:
            self.assertEqual(zstd.decompress(frame, backend='libzstd'), want, name)
        stats = self.check_same(cases + handmade_frames())
        # every part of the format was really used
        for key in ('blocks_raw', 'blocks_rle', 'blocks_compressed', 'blocks_without_sequences', 'checksums_verified',
                    'frames_skippable', 'frames_single_segment', 'frames_with_window_descriptor', 'frames_with_content_size',
                    'frames_without_content_size', 'huffman_tree_fse', 'huffman_tree_direct', 'literals_raw', 'literals_rle',
                    'literals_treeless', 'literals_huffman_1_stream', 'literals_huffman_4_streams',
                    'll_predefined', 'll_rle', 'll_fse', 'll_repeat', 'of_predefined', 'of_rle', 'of_fse', 'of_repeat',
                    'ml_predefined', 'ml_rle', 'ml_fse', 'ml_repeat'):
            self.assertGreater(stats.get(key, 0), 0, key)

    @unittest.skipUnless(HAVE_LIB, 'libzstd is not installed')
    def test_handmade_frames_are_valid(self):
        """libzstd reads the hand-made frames the same way (so they test the format, not this file's idea of it)"""
        for name, frame, want in handmade_frames() + [('long offset',) + long_offset_frame()]:
            self.assertEqual(zstd.decompress(frame, backend='libzstd'), want, name)


class ZstdPy(unittest.TestCase):
    def test_errors(self):
        if zstd.backend() is None:
            self.skipTest('neither libzstd nor bin/luau')
        for bad in (b'not a frame at all', MAGIC, bytes.fromhex(HEX_FRAMES['tiny_level3'][1])[:-3]):
            with self.assertRaises(zstd.ZstdError):
                zstd.decompress(bad)
        self.assertEqual(zstd.decompress(b''), b'')

    def test_default_backend(self):
        if zstd.backend() is None:
            self.skipTest('neither libzstd nor bin/luau')
        for name, (n, hx) in HEX_FRAMES.items():
            self.assertEqual(zstd.decompress(bytes.fromhex(hx)), synthetic(n), name)
        self.assertEqual(zstd.decompress(z_raw_frame(synthetic(250000))), synthetic(250000))

    @unittest.skipUnless(LUAU, 'bin/luau is not there')
    def test_lua_backend(self):
        n, hx = HEX_FRAMES['medium_level1_checksum_nosize']
        self.assertEqual(zstd.decompress(bytes.fromhex(hx), backend='lua'), synthetic(n))
        self.assertEqual(zstd.decompress(b'', backend='lua'), b'')
        with self.assertRaises(zstd.ZstdError) as cm:
            zstd.decompress(bytes.fromhex(hx)[:-1], backend='lua')
        self.assertIn('zstd: ', str(cm.exception))
        with self.assertRaises(zstd.ZstdError):
            zstd.decompress(z_frame([z_block(1, b'x', size=131072)] * 20 + [z_block(0, b'', last=True)]), limit=100000, backend='lua')


# ------------------------------------------------------------------------------------------------ miniature containers

HEADER = '-- This file was protected using Luraph Obfuscator v14.7 [https://lura.ph/]\n\n'


def base85(data, order, tag='%'):
    """The payload literal's text: "LPH" + one character + Luraph's base85 of `data`."""
    data += b'\0' * (-len(data) % 4)
    out = []
    for (v,) in struct.iter_unpack(order + 'I', data):
        if v == 0:
            out.append('z')
            continue
        digits = []
        for _ in range(5):
            v, d = divmod(v, 85)
            digits.append(chr(33 + d))
        out.append(''.join(reversed(digits)))
    return 'LPH' + tag + ''.join(out)


def lzma_raw(data):
    """What Luraph's container inflates: raw LZMA1, lc=3 lp=0 pb=0, with end marker."""
    enc = lzma.LZMACompressor(format=lzma.FORMAT_RAW, filters=[
        {'id': lzma.FILTER_LZMA1, 'lc': 3, 'lp': 0, 'pb': 0, 'dict_size': 1 << 20}])
    return enc.compress(data) + enc.flush()


def quoted(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def container(chunk, data=None, order='>', codec='lzma', spaces=0, data_kind='string', pack=True, quote=False,
              header=HEADER, tail=';'):
    """A text with the token structure of Luraph's compressed container (the decompressor itself is left
    out: the static decoder never looks at it, and nothing here is ever run)."""
    comp = (lambda b: z_raw_frame(b)) if codec == 'zstd' else lzma_raw
    raws = [comp(chunk)] + ([comp(data)] if data is not None else [])
    lits = [base85(r, order, tag) for r, tag in zip(raws, '%/')]
    lit = (lambda s: quoted(s)) if quote else (lambda s: '[==[' + s + ']==]')
    fmt = '"%sI4"' % order if pack else 'nil'
    t = [header, 'return(function()local S,P,L,R,Q=string.sub,string.pack,loadstring,string.rep,pcall;local O=5;',
         'local D=function(s)s=S(s,O);return(string.gsub(s,".....",function(g)return P(%s,0);end));end;' % fmt,
         'local a=D(%s);' % lit(lits[0])]
    if data is not None:
        t.append('local b=D(%s);' % lit(lits[1]))
    if codec == 'zstd':
        t.append('local E=game:GetService("EncodingService");a=string.sub(a,1,%d);' % len(raws[0]))
        if data is not None:
            t.append('b=string.sub(b,1,%d);b=E:DecompressBuffer(buffer.fromstring(b),Enum.CompressionAlgorithm.Zstd);' % len(raws[1]))
        t.append('a=buffer.tostring(E:DecompressBuffer(buffer.fromstring(a),Enum.CompressionAlgorithm.Zstd));')
    else:
        t.append('local I=function(s)return s;end;a=I(a);')
        if data is not None:
            t.append('b=I(b);' + ('b=buffer.fromstring(b);' if data_kind == 'buffer' else ''))
    name = '"Luraph"' + ('..R(" ",%d)' % spaces if spaces else '')
    t.append('local k,f=Q(L,a,%s,nil);assert(k and f,"Luraph decompression error");' % name)
    t.append('return f(b);' if data is not None else 'return f;')
    t.append('end)()(...)' + tail)
    return ''.join(t)


def container_v15(chunk, data, offset=36, order=(4, 3, 1, 0, 2), data_first=False, tail=';'):
    """A text with the token structure of the v15 container (object instead of function): the 85 digits start
    at chr(offset), character k of a group is the digit of weight 85^order[k], and the ten printable characters
    outside the alphabet stand for the ten most frequent groups."""
    import collections

    def groups(blob):
        raw = lzma_raw(blob)
        raw += b'\0' * (-len(raw) % 4)
        out = []
        for (v,) in struct.iter_unpack('<I', raw):
            digit = [(v // 85 ** e) % 85 for e in range(5)]
            out.append(''.join(chr(offset + digit[e]) for e in order))
        return out
    g1, g2 = groups(chunk), groups(data)
    keys = [chr(c) for c in range(32, 127) if not offset <= c < offset + 85]
    table = dict(zip(keys, [g for g, _n in collections.Counter(g1 + g2).most_common(10)]))
    back = {v: k for k, v in table.items()}
    lits = ['[==[LPH' + tag + ''.join(back.get(g, g) for g in gs) + ']==]' for gs, tag in ((g1, '+'), (g2, '!'))]
    fields = ['p=' + lits[0], 'u=' + lits[1]]
    if data_first:
        fields.reverse()
    return ''.join([
        '-- This file was protected using Luraph Obfuscator v15.1 [https://lura.ph/]\n\n',
        'return setmetatable({N="Luraph",k="function",[42]=loadstring,[56]=buffer.tostring,',
        'W={' + ','.join('[%s]=%s' % (quoted(k), quoted(v)) for k, v in table.items()) + '},', fields[0], ',',
        'q=function(g)local h,x,y=g.r,g.p,g.u;local a,b=h(g,x,%d),h(g,y,%d);' % (len(chunk), len(data)),
        'local f=g[42](g[56](a),g.N,nil);assert(f,"Luraph Decompression Error: ");return f(b);end,',
        fields[1], ',r=function(g,q,Q)return q;end,l="[%a_]"},{}):q()(...)', tail])


CHUNK = b'return(function(...)local t={...};' + synthetic(3000) + b'return t;end)(...);'
DATA = bytes(range(256)) * 20 + bytes(4000) + synthetic(900)


class ContainerStatic(unittest.TestCase):
    def test_one_payload(self):
        for order in '<>':
            for quote in (False, True):
                res = container_static.decode(container(CHUNK, order=order, quote=quote))
                self.assertEqual((res.chunk, res.chunk_name, res.data, res.data_kind, res.codec, res.byte_order, res.notes),
                                 (CHUNK, 'Luraph', None, None, 'lzma', order, []))

    def test_two_payloads(self):
        for order, kind, spaces in (('>', 'string', 2), ('<', 'buffer', 4), ('<', 'string', 1), ('>', 'buffer', 3)):
            res = container_static.decode(container(CHUNK, DATA, order=order, data_kind=kind, spaces=spaces))
            self.assertEqual((res.chunk, res.chunk_name, res.data, res.data_kind, res.codec, res.payloads),
                             (CHUNK, 'Luraph' + ' ' * spaces, DATA, kind, 'lzma', 2))
            self.assertEqual(res.notes, [])

    def test_zstd_variant(self):
        if zstd.backend() is None:
            self.skipTest('neither libzstd nor bin/luau')
        res = container_static.decode(container(CHUNK, DATA, order='<', codec='zstd', spaces=3))
        self.assertEqual((res.chunk, res.chunk_name, res.data, res.data_kind, res.codec, res.notes),
                         (CHUNK, 'Luraph   ', DATA, 'buffer', 'zstd', []))

    def test_no_pack_format(self):
        for order in '<>':
            res = container_static.decode(container(CHUNK, DATA, order=order, pack=False))
            self.assertEqual((res.chunk, res.data, res.byte_order), (CHUNK, DATA, order))
            self.assertTrue(any('by trial' in n for n in res.notes), res.notes)

    def test_around_the_container(self):
        text = container(CHUNK, DATA, spaces=1)
        body = text[len(HEADER):]
        variants = {
            'generated': '-- This file was generated using Luraph Obfuscator v14.2 [https://lura.ph/]\n\n' + body,
            'no header': body,
            'comment and guard first': '-- licence text\nif not is_from_loader then warn("use the loader") return end;\n' + text,
            'owner code after the header': HEADER + 'getgenv().key = "abc";\nlocal function f() return 1 end\n' + body,
            'blank lines and CRLF': '\r\n\r\n' + text.replace('\n', '\r\n'),
            'no semicolon': container(CHUNK, DATA, spaces=1, tail=''),
        }
        for name, t in variants.items():
            res = container_static.decode(t)
            self.assertIsNotNone(res, name)
            self.assertEqual((res.chunk, res.data, res.chunk_name, res.notes), (CHUNK, DATA, 'Luraph ', []), name)
        # a file read as bytes gives the same
        self.assertEqual(container_static.decode(text.encode('latin-1')).chunk, CHUNK)
        # text behind the container is ignored, and said so; payload-like strings in it are not taken for payloads
        res = container_static.decode(text + '\nreturn({x=[==[' + base85(b'junk' * 40, '<') + ']==]})')
        self.assertEqual((res.chunk, res.data, res.payloads), (CHUNK, DATA, 2))
        self.assertTrue(any('after the end of the container' in n for n in res.notes), res.notes)
        # line break after the header lost: the program sits in the comment
        res = container_static.decode(HEADER.rstrip('\n') + body)
        self.assertEqual((res.chunk, res.data), (CHUNK, DATA))
        self.assertTrue(any('same line' in n for n in res.notes), res.notes)

    def test_not_a_container(self):
        lit = '[==[' + base85(lzma_raw(CHUNK), '>') + ']==]'
        for name, t in {
            'empty': '', 'comment only': HEADER, 'plain script': 'print("hello")\nreturn 1\n',
            'function VM with arguments': HEADER + 'return(function(a,b,c)return a(b,c);end)(' + lit + ',string.sub,5)(...);',
            'table VM': HEADER + 'return({a=' + lit + ',b=function(s)return s.a;end}):b()(...);',
            'setmetatable object': HEADER + 'return setmetatable({a=' + lit + '},{}):b()(...);',
            'function without payload': HEADER + 'return(function()local x="LPH";return print;end)()(...);',
            'nested, not top level': HEADER + 'local f=function()return(function()return ' + lit + ';end)();end;return f;',
            'returns something else first': HEADER + 'return 5;' + container(CHUNK)[len(HEADER):],
        }.items():
            self.assertIsNone(container_static.decode(t), name)

    def test_damaged(self):
        text = container(CHUNK, DATA, order='<', data_kind='buffer', spaces=2)
        first = text.index('LPH')
        second = text.index('LPH', first + 3)
        # cut inside the first payload: nothing can be read
        with self.assertRaises(container_static.ContainerError) as cm:
            container_static.decode(text[:first + 500])
        self.assertIsNone(cm.exception.partial)
        self.assertIn('first payload', str(cm.exception))
        # cut inside the second payload, or behind it: the chunk is there, the result is still an error
        for cut, complete_data in ((second + 300, False), (text.index(']==]', second) + 6, True), (len(text) - 9, True)):
            with self.assertRaises(container_static.ContainerError) as cm:
                container_static.decode(text[:cut])
            part = cm.exception.partial
            self.assertIsNotNone(part, cut)
            self.assertEqual(part.chunk, CHUNK)
            self.assertEqual(part.data, DATA if complete_data else None)
            self.assertIn('cut off', str(cm.exception))
        # payload text damaged: a group that is no 32-bit value, a changed character, a missing piece
        for name, bad in (('overflow', text[:first + 50] + 'uuuuu' + text[first + 55:]),
                          ('changed', text[:first + 200] + ('!' if text[first + 200] != '!' else '#') + text[first + 201:]),
                          ('dropped', text[:first + 300] + text[first + 301:])):
            with self.assertRaises(container_static.ContainerError, msg=name) as cm:
                container_static.decode(bad)
            self.assertIn('first payload', str(cm.exception), name)
        bad = text[:second + 100] + ('!' if text[second + 100] != '!' else '#') + text[second + 101:]
        with self.assertRaises(container_static.ContainerError) as cm:
            container_static.decode(bad)
        self.assertIn('second payload', str(cm.exception))
        self.assertEqual(cm.exception.partial.chunk, CHUNK)
        self.assertIsNone(cm.exception.partial.data)
        # a payload that is neither LZMA nor announced as Zstd
        other = text.replace(text[first:text.index(']==]', first)], base85(b'\x1f\x8b' + bytes(range(200)), '<'), 1)
        with self.assertRaises(container_static.ContainerError):
            container_static.decode(other)

    def test_cut_off_zstd_container(self):
        """the text ends before the code that names the codec: the Zstandard magic number in the payload decides"""
        if zstd.backend() is None:
            self.skipTest('neither libzstd nor bin/luau')
        text = container(CHUNK, DATA, order='<', codec='zstd', spaces=3)
        second = text.index('LPH', text.index('LPH') + 3)
        with self.assertRaises(container_static.ContainerError) as cm:
            container_static.decode(text[:second + 80])
        part = cm.exception.partial
        self.assertEqual((part.chunk, part.codec, part.data), (CHUNK, 'zstd', None))

    def test_v15_container(self):
        for offset, order, data_first in ((36, (4, 3, 1, 0, 2), False), (32, (1, 4, 3, 2, 0), True), (41, (0, 1, 2, 3, 4), False),
                                          (39, (2, 0, 4, 1, 3), True)):
            text = container_v15(CHUNK, DATA, offset, order, data_first)
            res = container_static.decode(text)
            self.assertIsNotNone(res, (offset, order))
            self.assertEqual((res.chunk, res.chunk_name, res.data, res.data_kind, res.codec, res.payloads),
                             (CHUNK, 'Luraph', DATA, 'buffer', 'lzma', 2))
            self.assertTrue(any(''.join(map(str, order)) in n for n in res.notes), res.notes)
        text = container_v15(CHUNK, DATA)
        # cut off: inside a payload, and behind the object
        first = text.index('LPH')
        with self.assertRaises(container_static.ContainerError):
            container_static.decode(text[:first + 400])
        with self.assertRaises(container_static.ContainerError) as cm:
            container_static.decode(text[:-7])
        self.assertEqual((cm.exception.partial.chunk, cm.exception.partial.data), (CHUNK, DATA))
        # damaged payload text: the data payload (the chunk is still read), then the chunk payload
        second = text.index('LPH', first + 3)
        for cut in (second + 30, second + 300):
            with self.assertRaises(container_static.ContainerError) as cm:
                container_static.decode(text[:cut] + text[cut + 1:])
            self.assertEqual((cm.exception.partial.chunk, cm.exception.partial.data), (CHUNK, None))
        with self.assertRaises(container_static.ContainerError) as cm:
            container_static.decode(text[:first + 200] + text[first + 201:])
        self.assertIsNone(cm.exception.partial)
        # a plain v15 VM has one payload and no substitution table: not a container
        one = text[:second - 6] + text[text.index(']==]', second) + 5:]
        self.assertIsNone(container_static.decode(one))

    @unittest.skipUnless(LUAU, 'bin/luau is not there')
    def test_repository_sample(self):
        """examples/v15.2/sample.luau is a v15 container: the static result is what the sandbox captures"""
        path = os.path.join(ROOT, 'examples', 'v15.2', 'sample.luau')
        if not os.path.exists(path):
            self.skipTest('examples/v15.2/sample.luau is not there')
        with open(path, 'rb') as f:
            res = container_static.decode(f.read())
        self.assertIsNotNone(res)
        with tempfile.TemporaryDirectory(prefix='codecs-sample-') as tmp:
            got = sandbox_unwrap(path, tmp)
        self.assertIsNotNone(got)
        self.assertEqual((got[0], got[1], got[2], got[3]), (res.chunk, res.chunk_name, res.data, res.data_kind))

    def test_lexer(self):
        lex = container_static._lex
        self.assertEqual([t[:2] for t in lex('a = "x\\"y\\65\\x41\\z  \n b" --[[ c ]] .. [=[\nq]]=] -- end')],
                         [('id', 'a'), ('op', '='), ('str', 'x"yAAb'), ('op', '..'), ('str', 'q]')])
        self.assertEqual(lex('x = "abc')[-1][:2], ('cut', 'abc'))
        self.assertEqual(lex('x = [==[abc]]')[-1][:2], ('cut', 'abc]]'))
        self.assertEqual(lex("'\\x4'")[0][:2], ('str', 'x4'))           # Lua 5.1 reading of a malformed escape
        self.assertEqual([container_static._number(s) for s in ('4', '0x10', '0B101', '1_0', '2.0', '1e2', 'zz')],
                         [4, 16, 5, 10, 2, 100, None])


# ------------------------------------------------------------------------------------------------ corpus

def corpus_files(per_dir):
    """{version directory: [paths]}: the first `per_dir` files of each (sorted), minus LURAPH_CORPUS_SKIP."""
    skip = set()
    if os.environ.get('LURAPH_CORPUS_SKIP'):
        with open(os.environ['LURAPH_CORPUS_SKIP']) as f:
            skip = set(f.read().split())
    out = {}
    for d in sorted(os.listdir(CORPUS)):
        full = os.path.join(CORPUS, d)
        if not os.path.isdir(full) or d.startswith('.'):
            continue
        names = [n for n in sorted(os.listdir(full)) if d + '/' + n not in skip and os.path.isfile(os.path.join(full, n))]
        out[d] = [os.path.join(full, n) for n in names[:per_dir]]
    return out


def sandbox_unwrap(path, tmp, timeout=300):
    """What the container loads according to the sandbox: (chunk bytes, chunk name, data bytes or None, kind) or None."""
    prefix = os.path.join(tmp, 'u')
    for n in os.listdir(tmp):
        os.remove(os.path.join(tmp, n))
    try:
        subprocess.run([LUAU, UNWRAP_LUA, '-a', path, prefix], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if not os.path.exists(prefix + '.info'):
        return None
    with open(prefix + '.info', encoding='latin-1') as f:
        row = f.read().split('\n')[0].split('\t')
    if len(row) < 3 or not os.path.exists(prefix + '.chunk1.lua'):
        return None
    with open(prefix + '.chunk1.lua', 'rb') as f:
        chunk = f.read()
    data = kind = None
    if os.path.exists(prefix + '.chunk1.arg1.bin'):
        with open(prefix + '.chunk1.arg1.bin', 'rb') as f:
            data = f.read()
        kind = row[3].split(',')[0].split(':')[0] if len(row) > 3 else None
    return chunk, row[1], data, kind


@unittest.skipUnless(CORPUS and os.path.isdir(CORPUS or ''), 'LURAPH_CORPUS is not set')
class Corpus(unittest.TestCase):
    PER_DIR = int(os.environ.get('CODECS_CORPUS_PER_DIR', '60'))
    SANDBOX_PER_DIR = 3

    def test_static_decoder_against_sandbox(self):
        """containers of every version: the static result is what devm/unwrap.lua captures when it runs them"""
        if not LUAU:
            self.skipTest('bin/luau is not there')
        seen = {'files': 0, 'containers': 0, 'damaged': 0, 'compared': 0, 'sandbox gave nothing': 0}
        frames = []
        real = zstd.decompress

        def spy(data, *a, **k):
            out = real(data, *a, **k)
            if len(frames) < 6:
                frames.append((bytes(data), out))
            return out
        zstd.decompress = spy
        try:
            with tempfile.TemporaryDirectory(prefix='codecs-corpus-') as tmp:
                for d, paths in corpus_files(self.PER_DIR).items():
                    compared = 0
                    for path in paths:
                        with open(path, 'rb') as f:
                            text = f.read().decode('latin-1')
                        seen['files'] += 1
                        try:
                            res = container_static.decode(text)
                        except container_static.ContainerError as e:
                            seen['damaged'] += 1
                            self.assertTrue(str(e))
                            continue
                        if res is None:
                            continue
                        seen['containers'] += 1
                        self.assertTrue(res.chunk_name.startswith('Luraph'))
                        self.assertIn(b'return', res.chunk[:200])
                        self.assertEqual(res.data is None, res.data_kind is None)
                        if res.codec != 'lzma' or compared >= self.SANDBOX_PER_DIR:
                            continue
                        got = sandbox_unwrap(path, tmp)
                        if got is None:         # e.g. a container for Lua 5.4, which Luau cannot run
                            seen['sandbox gave nothing'] += 1
                            continue
                        compared += 1
                        seen['compared'] += 1
                        where = os.path.basename(d)
                        self.assertEqual(got[0], res.chunk, where + ': chunk')
                        self.assertEqual(got[1], res.chunk_name, where + ': chunk name')
                        self.assertEqual(got[2], res.data, where + ': data')
                        if res.data_kind and got[3]:
                            self.assertEqual(got[3], res.data_kind, where + ': data kind')
        finally:
            zstd.decompress = real
        print('corpus: %(files)d files read, %(containers)d containers decoded, %(damaged)d damaged, %(compared)d compared '
              'with the sandbox (%(sandbox gave nothing)d more where the sandbox gave nothing)' % seen, file=sys.stderr)
        self.assertGreater(seen['compared'], 0)
        if frames and HAVE_LIB:
            res, _ = run_lua([('frame %d' % i, f, out) for i, (f, out) in enumerate(frames)])
            self.assertEqual([v for v, _d in res.values()], ['same'] * len(frames))
            print('corpus: %d Zstandard frames of containers: zstd.lua == libzstd' % len(frames), file=sys.stderr)


if __name__ == '__main__':
    unittest.main()
