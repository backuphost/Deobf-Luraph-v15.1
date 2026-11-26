-- Zstandard decompression in pure Luau (RFC 8878), for the Roblox mock and as devm/zstd.py's fallback.
--
--     local zstd = loadstring(readfile(here .. "/zstd.lua"), "=zstd")()
--     local out = zstd.decompress(buf)             -- buffer -> buffer
--     local out = zstd.decompress(buf, limit, stats)
--
-- `buf` holds one or more frames (skippable frames are skipped); the result is their content, joined.
-- `limit` (default 256 MiB) bounds the result: a few bytes of Zstandard can announce gigabytes.
-- `stats`, when a table, receives counters of what the frames used (block kinds, literal kinds, table
-- modes ...): the tests use it to show that every part of the format was exercised.
--
-- Supported: frame header with and without content size and window descriptor, raw / RLE / compressed
-- blocks, literals raw / RLE / Huffman (1 or 4 streams, tree given directly or FSE-compressed, tree
-- repeat), sequences in predefined / RLE / FSE / repeat mode, repeat offsets, the content checksum
-- (XXH64, computed in 32-bit halves because Luau numbers are doubles; verified), several frames in a row.
-- Not supported: dictionaries (a frame that names one is refused).
--
-- Every failure is error("zstd: <what>", 0).  The decoder never reads outside its input, never writes
-- more than `limit` bytes, and every loop is bounded by the input or by a size the format fixes
-- (a block holds at most min(window, 128 KiB) bytes, a match never reaches before the start of its frame
-- or beyond the window).
--
-- Only buffer, bit32, string, table and math are used; nothing global is written.  Run as a script
-- (`luau zstd.lua -a IN OUT [LIMIT]`, the repository's patched interpreter) it decompresses a file.
local band, bor, bxor, lshift, rshift, extract, countlz =
	bit32.band, bit32.bor, bit32.bxor, bit32.lshift, bit32.rshift, bit32.extract, bit32.countlz
local readu8, readu16, readu32, writeu8 = buffer.readu8, buffer.readu16, buffer.readu32, buffer.writeu8
local bcopy, bfill, bcreate, blen = buffer.copy, buffer.fill, buffer.create, buffer.len
local floor = math.floor

local MAX_BLOCK = 131072
local DEFAULT_LIMIT = 268435456
local PRESIZE = 67108864 -- a frame's announced size is allocated at once up to this; beyond, the result grows as it is produced

local function fail(what)
	error("zstd: " .. what, 0)
end

local function count(stats, key)
	if stats then
		stats[key] = (stats[key] or 0) + 1
	end
end

---------------------------------------------------------------------------------------------- XXH64

local P1H, P1L = 0x9E3779B1, 0x85EBCA87
local P2H, P2L = 0xC2B2AE3D, 0x27D4EB4F
local P3H, P3L = 0x165667B1, 0x9E3779F9
local P4H, P4L = 0x85EBCA77, 0xC2B2AE63
local P5H, P5L = 0x27D4EB2F, 0x165667C5

-- low 64 bits of (ah:al) * (bh:bl), as two 32-bit halves; every intermediate stays below 2^53
local function mul64(ah, al, bh, bl)
	local a0, a1 = band(al, 0xFFFF), rshift(al, 16)
	local b0, b1 = band(bl, 0xFFFF), rshift(bl, 16)
	local mid = a0 * b1 + a1 * b0
	local lo = a0 * b0 + (mid % 65536) * 65536
	local hi = a1 * b1 + floor(mid / 65536) + a0 * bh + ((a1 * bh) % 65536) * 65536 + b0 * ah + ((b1 * ah) % 65536) * 65536
	if lo >= 4294967296 then
		lo -= 4294967296
		hi += 1
	end
	return hi % 4294967296, lo
end

-- rotl(acc + input * P2, 31) * P1
local function xround(ah, al, ih, il)
	local mh, ml = mul64(ih, il, P2H, P2L)
	local l = al + ml
	local h = ah + mh
	if l >= 4294967296 then
		l -= 4294967296
		h += 1
	end
	if h >= 4294967296 then
		h -= 4294967296
	end
	return mul64(bor(lshift(h, 31), rshift(l, 1)), bor(lshift(l, 31), rshift(h, 1)), P1H, P1L)
end

local function add64(ah, al, bh, bl)
	local l = al + bl
	local h = ah + bh
	if l >= 4294967296 then
		l -= 4294967296
		h += 1
	end
	return h % 4294967296, l
end

local function rotl64(h, l, r) -- 0 < r < 32
	return bor(lshift(h, r), rshift(l, 32 - r)), bor(lshift(l, r), rshift(h, 32 - r))
end

-- h = (h xor round(0, v)) * P1 + P4
local function merge(hh, hl, vh, vl)
	local kh, kl = xround(0, 0, vh, vl)
	hh, hl = mul64(bxor(hh, kh), bxor(hl, kl), P1H, P1L)
	return add64(hh, hl, P4H, P4L)
end

-- low 32 bits of XXH64(buf[start, start + len), seed 0): Zstandard's content checksum
local function xxh64low(buf, start, len)
	local p = start
	local e = start + len
	local hh, hl
	if len >= 32 then
		local v1h, v1l = add64(P1H, P1L, P2H, P2L)
		local v2h, v2l = P2H, P2L
		local v3h, v3l = 0, 0
		local v4h, v4l = 4294967295 - P1H, 4294967296 - P1L
		repeat
			v1h, v1l = xround(v1h, v1l, readu32(buf, p + 4), readu32(buf, p))
			v2h, v2l = xround(v2h, v2l, readu32(buf, p + 12), readu32(buf, p + 8))
			v3h, v3l = xround(v3h, v3l, readu32(buf, p + 20), readu32(buf, p + 16))
			v4h, v4l = xround(v4h, v4l, readu32(buf, p + 28), readu32(buf, p + 24))
			p += 32
		until p + 32 > e
		local ah, al = rotl64(v1h, v1l, 1)
		local bh, bl = rotl64(v2h, v2l, 7)
		local ch, cl = rotl64(v3h, v3l, 12)
		local dh, dl = rotl64(v4h, v4l, 18)
		hh, hl = add64(ah, al, bh, bl)
		hh, hl = add64(hh, hl, ch, cl)
		hh, hl = add64(hh, hl, dh, dl)
		hh, hl = merge(hh, hl, v1h, v1l)
		hh, hl = merge(hh, hl, v2h, v2l)
		hh, hl = merge(hh, hl, v3h, v3l)
		hh, hl = merge(hh, hl, v4h, v4l)
	else
		hh, hl = P5H, P5L
	end
	hh, hl = add64(hh, hl, floor(len / 4294967296), len % 4294967296)
	while p + 8 <= e do
		local kh, kl = xround(0, 0, readu32(buf, p + 4), readu32(buf, p))
		hh, hl = rotl64(bxor(hh, kh), bxor(hl, kl), 27)
		hh, hl = mul64(hh, hl, P1H, P1L)
		hh, hl = add64(hh, hl, P4H, P4L)
		p += 8
	end
	if p + 4 <= e then
		local kh, kl = mul64(0, readu32(buf, p), P1H, P1L)
		hh, hl = rotl64(bxor(hh, kh), bxor(hl, kl), 23)
		hh, hl = mul64(hh, hl, P2H, P2L)
		hh, hl = add64(hh, hl, P3H, P3L)
		p += 4
	end
	while p < e do
		local kh, kl = mul64(0, readu8(buf, p), P5H, P5L)
		hh, hl = rotl64(bxor(hh, kh), bxor(hl, kl), 11)
		hh, hl = mul64(hh, hl, P1H, P1L)
		p += 1
	end
	hl = bxor(hl, rshift(hh, 1)) -- h ^= h >> 33
	hh, hl = mul64(hh, hl, P2H, P2L)
	hl = bxor(hl, bor(rshift(hl, 29), lshift(hh, 3))) -- h ^= h >> 29
	hh = bxor(hh, rshift(hh, 29))
	hh, hl = mul64(hh, hl, P3H, P3L)
	return bxor(hl, hh) -- h ^= h >> 32, low half
end

---------------------------------------------------------------------------------------------- bit streams

-- The `nb` bits below bit position `p` of the backward bit stream whose first byte is src[base]
-- (0 <= nb <= 32).  Bits below the start of the stream read as zero, as in the reference decoder; the
-- caller notices from its own position (p - nb < 0) that the stream was over-read.
local function rbits(src, base, p, nb)
	if nb == 0 then
		return 0
	end
	local q = p - nb
	if q >= 0 then
		if nb <= 25 then
			return extract(readu32(src, base + rshift(q, 3)), band(q, 7), nb)
		end
		local q2 = q + 16
		return extract(readu32(src, base + rshift(q2, 3)), band(q2, 7), nb - 16) * 65536
			+ extract(readu32(src, base + rshift(q, 3)), band(q, 7), 16)
	end
	if p <= 0 then
		return 0
	end
	return rbits(src, base, p, p) * 2 ^ (nb - p)
end

---------------------------------------------------------------------------------------------- FSE tables

-- decoding table for normalised counts freq[1 .. nsym] (-1 = "less than one") with 2^log states
local function buildFse(freq, nsym, log)
	local size = lshift(1, log)
	local sym = table.create(size, 0)
	local nextv = table.create(nsym, 0)
	local high = size
	for s = 1, nsym do
		if freq[s] == -1 then
			sym[high] = s - 1
			high -= 1
			nextv[s] = 1
		end
	end
	local step = rshift(size, 1) + rshift(size, 3) + 3
	local mask = size - 1
	local pos = 0
	for s = 1, nsym do
		local f = freq[s]
		if f > 0 then
			nextv[s] = f
			for _ = 1, f do
				sym[pos + 1] = s - 1
				repeat
					pos = band(pos + step, mask)
				until pos < high
			end
		end
	end
	if pos ~= 0 then
		fail("corrupt FSE table (the symbols do not fill it)")
	end
	local nb = table.create(size, 0)
	local base = table.create(size, 0)
	for i = 1, size do
		local s = sym[i] + 1
		local x = nextv[s]
		nextv[s] = x + 1
		local bits = log - (31 - countlz(x))
		nb[i] = bits
		base[i] = lshift(x, bits) - size
	end
	return { sym = sym, nb = nb, base = base, log = log }
end

-- FSE table description at src[pos, stop): -> table, position after it
local function readFse(src, pos, stop, maxLog, maxSym)
	if pos >= stop then
		fail("truncated block (FSE table description)")
	end
	local avail = (stop - pos) * 8
	local log = 5 + band(readu8(src, pos), 15)
	if log > maxLog then
		fail("corrupt FSE table (accuracy log " .. log .. ", at most " .. maxLog .. " allowed)")
	end
	local bitpos = 4
	local remaining = lshift(1, log)
	local freq = {}
	local n = 0
	while remaining > 0 and n <= maxSym do
		local bits = 32 - countlz(remaining + 1)
		local v = extract(readu32(src, pos + rshift(bitpos, 3)), band(bitpos, 7), bits)
		local lower = lshift(1, bits - 1) - 1
		local threshold = lshift(1, bits) - 1 - (remaining + 1)
		if band(v, lower) < threshold then
			v = band(v, lower)
			bitpos += bits - 1
		else
			if v > lower then
				v -= threshold
			end
			bitpos += bits
		end
		local proba = v - 1
		remaining -= if proba < 0 then 1 else proba
		n += 1
		freq[n] = proba
		if proba == 0 then
			repeat
				local rep = extract(readu32(src, pos + rshift(bitpos, 3)), band(bitpos, 7), 2)
				bitpos += 2
				for _ = 1, rep do
					n += 1
					freq[n] = 0
				end
				if n > maxSym + 1 or bitpos > avail then
					fail("corrupt FSE table (too many symbols)")
				end
			until rep ~= 3
		end
		if bitpos > avail then
			fail("truncated block (FSE table description)")
		end
	end
	if remaining ~= 0 or n > maxSym + 1 then
		fail("corrupt FSE table (probabilities do not add up)")
	end
	return buildFse(freq, n, log), pos + rshift(bitpos + 7, 3)
end

local function predefined(list, log)
	return buildFse(list, #list, log)
end

local LL_DEFAULT = predefined({ 4, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 2, 1, 1, 1, 1, 1,
	-1, -1, -1, -1 }, 6)
local ML_DEFAULT = predefined({ 1, 4, 3, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
	1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, -1, -1, -1, -1, -1, -1, -1 }, 6)
local OF_DEFAULT = predefined({ 1, 1, 1, 1, 1, 1, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, -1, -1, -1, -1, -1 }, 5)

local LL_BASE = { 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 20, 22, 24, 28, 32, 40, 48, 64, 128, 256, 512,
	1024, 2048, 4096, 8192, 16384, 32768, 65536 }
local LL_BITS = { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 3, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16 }
local ML_BASE = { 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
	32, 33, 34, 35, 37, 39, 41, 43, 47, 51, 59, 67, 83, 99, 131, 259, 515, 1027, 2051, 4099, 8195, 16387, 32771, 65539 }
local ML_BITS = { 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2,
	3, 3, 4, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16 }

-- the decoding table of one of the three sequence codes, by its mode
local function seqTable(mode, src, pos, stop, previous, default, maxLog, maxSym, stats, what)
	if mode == 0 then
		count(stats, what .. "_predefined")
		return default, pos
	elseif mode == 1 then
		if pos >= stop then
			fail("truncated block (RLE sequence table)")
		end
		local s = readu8(src, pos)
		if s > maxSym then
			fail("corrupt block (sequence symbol " .. s .. " out of range)")
		end
		count(stats, what .. "_rle")
		return { sym = { s }, nb = { 0 }, base = { 0 }, log = 0 }, pos + 1
	elseif mode == 2 then
		count(stats, what .. "_fse")
		return readFse(src, pos, stop, maxLog, maxSym)
	end
	if not previous then
		fail("corrupt block (repeat mode without an earlier sequence table)")
	end
	count(stats, what .. "_repeat")
	return previous, pos
end

---------------------------------------------------------------------------------------------- literals

-- Huffman tree description at src[pos, stop): fills st.htab (entry = symbol * 16 + number of bits,
-- indexed by the next st.hbits bits of the stream + 1); -> position after the description
local function readHufTree(src, pos, stop, st, stats)
	if pos >= stop then
		fail("truncated block (Huffman tree)")
	end
	local hb = readu8(src, pos)
	pos += 1
	local w = st.weights
	local n = 0
	if hb >= 128 then
		n = hb - 127
		local bytes = rshift(n + 1, 1)
		if pos + bytes > stop then
			fail("truncated block (Huffman weights)")
		end
		for i = 0, n - 1 do
			local b = readu8(src, pos + rshift(i, 1))
			w[i + 1] = if band(i, 1) == 0 then rshift(b, 4) else band(b, 15)
		end
		pos += bytes
		count(stats, "huffman_tree_direct")
	else
		local wend = pos + hb
		if hb == 0 or wend > stop then
			fail("truncated block (Huffman weights)")
		end
		local t, p0 = readFse(src, pos, wend, 6, 255)
		local len = wend - p0
		if len <= 0 then
			fail("corrupt block (no Huffman weight stream)")
		end
		local last = readu8(src, wend - 1)
		if last == 0 then
			fail("corrupt block (Huffman weight stream has no end mark)")
		end
		local p = (len - 1) * 8 + (31 - countlz(last))
		local tsym, tnb, tbase, log = t.sym, t.nb, t.base, t.log
		local s1 = rbits(src, p0, p, log)
		p -= log
		local s2 = rbits(src, p0, p, log)
		p -= log
		while true do
			if n > 253 then
				fail("corrupt block (more than 255 Huffman weights)")
			end
			n += 1
			w[n] = tsym[s1 + 1]
			local nb = tnb[s1 + 1]
			s1 = tbase[s1 + 1] + rbits(src, p0, p, nb)
			p -= nb
			if p < 0 then
				n += 1
				w[n] = tsym[s2 + 1]
				break
			end
			n += 1
			w[n] = tsym[s2 + 1]
			nb = tnb[s2 + 1]
			s2 = tbase[s2 + 1] + rbits(src, p0, p, nb)
			p -= nb
			if p < 0 then
				n += 1
				w[n] = tsym[s1 + 1]
				break
			end
		end
		pos = wend
		count(stats, "huffman_tree_fse")
	end
	-- the last weight is implied: the weights must fill a power of two
	local sum = 0
	local ones = 0
	for i = 1, n do
		local x = w[i]
		if x > 11 then
			fail("corrupt block (Huffman weight " .. x .. ")")
		elseif x > 0 then
			sum += lshift(1, x - 1)
			if x == 1 then
				ones += 1
			end
		end
	end
	if sum == 0 then
		fail("corrupt block (all Huffman weights are zero)")
	end
	local maxbits = 32 - countlz(sum)
	if maxbits > 11 then
		fail("corrupt block (Huffman code longer than 11 bits)")
	end
	local left = lshift(1, maxbits) - sum
	if band(left, left - 1) ~= 0 then
		fail("corrupt block (Huffman weights do not fill the code space)")
	end
	local lastw = 32 - countlz(left)
	n += 1
	w[n] = lastw
	if lastw == 1 then
		ones += 1
	end
	if ones < 2 or band(ones, 1) == 1 then
		fail("corrupt block (Huffman weights are not a prefix code)")
	end
	-- codes are handed out from weight 1 (the longest codes, lowest table indices) upwards, symbols in order
	local nextIdx = st.rank
	for x = 1, maxbits + 1 do
		nextIdx[x] = 0
	end
	for i = 1, n do
		local x = w[i]
		if x > 0 then
			nextIdx[x + 1] += lshift(1, x - 1)
		end
	end
	for x = 2, maxbits + 1 do
		nextIdx[x] += nextIdx[x - 1]
	end
	local tab = st.htab
	for i = 1, n do
		local x = w[i]
		if x > 0 then
			local e = (i - 1) * 16 + (maxbits + 1 - x)
			local from = nextIdx[x]
			local len = lshift(1, x - 1)
			for k = from + 1, from + len do
				tab[k] = e
			end
			nextIdx[x] = from + len
		end
	end
	st.hbits = maxbits
	return pos
end

-- one Huffman-coded stream src[base, base + len) -> `n` symbols at lit[lpos ...]
local function hufStream(src, base, len, lit, lpos, n, tab, maxbits)
	if len <= 0 then
		fail("corrupt block (empty Huffman stream)")
	end
	local last = readu8(src, base + len - 1)
	if last == 0 then
		fail("corrupt block (Huffman stream has no end mark)")
	end
	local p = (len - 1) * 8 + (31 - countlz(last))
	for i = lpos, lpos + n - 1 do
		local q = p - maxbits
		local e
		if q >= 0 then
			e = tab[extract(readu32(src, base + rshift(q, 3)), band(q, 7), maxbits) + 1]
		else
			if p <= 0 then
				fail("corrupt block (Huffman stream ends early)")
			end
			e = tab[lshift(extract(readu32(src, base), 0, p), maxbits - p) + 1]
		end
		writeu8(lit, i, rshift(e, 4))
		p -= band(e, 15)
	end
	if p ~= 0 then
		fail("corrupt block (Huffman stream not used up)")
	end
end

-- literals section at src[pos, stop): -> buffer holding the literals, their offset, their length, position after the section
local function literals(src, pos, stop, st, stats)
	if pos >= stop then
		fail("truncated block (literals header)")
	end
	local b0 = readu8(src, pos)
	local kind = band(b0, 3)
	local sf = band(rshift(b0, 2), 3)
	if kind < 2 then
		local size, hl
		if sf == 1 then
			size, hl = rshift(readu16(src, pos), 4), 2
		elseif sf == 3 then
			size, hl = rshift(band(readu32(src, pos), 0xFFFFFF), 4), 3
		else
			size, hl = rshift(b0, 3), 1
		end
		pos += hl
		if size > MAX_BLOCK then
			fail("corrupt block (" .. size .. " literals)")
		end
		if kind == 0 then
			if pos + size > stop then
				fail("truncated block (raw literals)")
			end
			count(stats, "literals_raw")
			return src, pos, size, pos + size
		end
		if pos >= stop then
			fail("truncated block (RLE literals)")
		end
		bfill(st.lit, 0, readu8(src, pos), size)
		count(stats, "literals_rle")
		return st.lit, 0, size, pos + 1
	end
	local regen, comp, hl, four
	if sf < 2 then
		local v = band(readu32(src, pos), 0xFFFFFF)
		regen, comp, hl, four = band(rshift(v, 4), 0x3FF), rshift(v, 14), 3, sf == 1
	elseif sf == 2 then
		local v = readu32(src, pos)
		regen, comp, hl, four = band(rshift(v, 4), 0x3FFF), rshift(v, 18), 4, true
	else
		local v = readu32(src, pos)
		regen, comp, hl, four = band(rshift(v, 4), 0x3FFFF), rshift(v, 22) + readu8(src, pos + 4) * 1024, 5, true
	end
	pos += hl
	local lend = pos + comp
	if lend > stop then
		fail("truncated block (compressed literals)")
	end
	if regen > MAX_BLOCK then
		fail("corrupt block (" .. regen .. " literals)")
	end
	if kind == 2 then
		pos = readHufTree(src, pos, lend, st, stats)
	else
		if not st.hbits then
			fail("corrupt block (literals reuse a Huffman tree, but there is none)")
		end
		count(stats, "literals_treeless")
	end
	local tab, maxbits, lit = st.htab, st.hbits, st.lit
	if four then
		if lend - pos < 6 then
			fail("truncated block (Huffman jump table)")
		end
		local s1, s2, s3 = readu16(src, pos), readu16(src, pos + 2), readu16(src, pos + 4)
		pos += 6
		local s4 = lend - pos - s1 - s2 - s3
		local per = rshift(regen + 3, 2)
		local lastn = regen - 3 * per
		if s4 <= 0 or lastn < 0 then
			fail("corrupt block (Huffman jump table)")
		end
		hufStream(src, pos, s1, lit, 0, per, tab, maxbits)
		hufStream(src, pos + s1, s2, lit, per, per, tab, maxbits)
		hufStream(src, pos + s1 + s2, s3, lit, 2 * per, per, tab, maxbits)
		hufStream(src, pos + s1 + s2 + s3, s4, lit, 3 * per, lastn, tab, maxbits)
		count(stats, "literals_huffman_4_streams")
	else
		hufStream(src, pos, lend - pos, lit, 0, regen, tab, maxbits)
		count(stats, "literals_huffman_1_stream")
	end
	return lit, 0, regen, lend
end

---------------------------------------------------------------------------------------------- frames

local function grow(out, cap, need, used, limit)
	local ncap = cap * 2
	if ncap > limit then
		ncap = limit
	end
	if ncap < need then
		ncap = need
	end
	local nb = bcreate(ncap)
	bcopy(nb, 0, out, 0, used)
	return nb, ncap
end

-- one frame whose magic number ends at `pos`; -> position after the frame, out, cap, op
local function frame(src, pos, n, out, cap, op, limit, st, stats)
	if pos >= n then
		fail("truncated input (frame header)")
	end
	local fhd = readu8(src, pos)
	pos += 1
	local fcsFlag = rshift(fhd, 6)
	local single = band(fhd, 32) ~= 0
	local hasSum = band(fhd, 4) ~= 0
	local didSize = band(fhd, 3)
	if band(fhd, 8) ~= 0 then
		fail("corrupt frame header (reserved bit set)")
	end
	if didSize == 3 then
		didSize = 4
	end
	local fcsSize = if fcsFlag == 0 then (if single then 1 else 0) else lshift(1, fcsFlag)
	if pos + (if single then 0 else 1) + didSize + fcsSize > n then
		fail("truncated input (frame header)")
	end
	local window = 0
	if not single then
		local wd = readu8(src, pos)
		pos += 1
		if rshift(wd, 3) > 21 then -- windows above 2 GiB: no implementation accepts them
			fail("frame needs a window of 2^" .. (10 + rshift(wd, 3)) .. " bytes")
		end
		window = 2 ^ (10 + rshift(wd, 3))
		window += window / 8 * band(wd, 7)
	end
	local did = 0
	for i = didSize - 1, 0, -1 do
		did = did * 256 + readu8(src, pos + i)
	end
	pos += didSize
	if did ~= 0 then
		fail("the frame needs dictionary " .. did .. " (dictionaries are not supported)")
	end
	local fcs = nil
	if fcsSize == 1 then
		fcs = readu8(src, pos)
	elseif fcsSize == 2 then
		fcs = readu16(src, pos) + 256
	elseif fcsSize == 4 then
		fcs = readu32(src, pos)
	elseif fcsSize == 8 then
		fcs = readu32(src, pos) + readu32(src, pos + 4) * 4294967296
	end
	pos += fcsSize
	if single then
		window = fcs
	end
	-- Block_Maximum_Size: no block is larger than the window, or than 128 KiB
	local blockLimit = if window < MAX_BLOCK then window else MAX_BLOCK
	count(stats, "frames")
	count(stats, if fcs then "frames_with_content_size" else "frames_without_content_size")
	count(stats, if single then "frames_single_segment" else "frames_with_window_descriptor")
	local fstart = op
	local maxop = limit
	if fcs then
		if fcs > limit - op then
			fail("the frame announces " .. fcs .. " bytes: more than the limit of " .. limit)
		end
		maxop = op + fcs
		if maxop > cap and maxop <= PRESIZE then
			local nb = bcreate(maxop)
			bcopy(nb, 0, out, 0, op)
			out, cap = nb, maxop
		end
	end
	local rep1, rep2, rep3 = 1, 4, 8
	local llT, ofT, mlT = nil, nil, nil
	st.hbits = nil
	local lastBlock
	repeat
		if pos + 3 > n then
			fail("truncated input (block header)")
		end
		local bh = band(readu32(src, pos), 0xFFFFFF)
		pos += 3
		lastBlock = band(bh, 1) == 1
		local btype = band(rshift(bh, 1), 3)
		local bsize = rshift(bh, 3)
		if bsize > blockLimit and btype < 3 then
			fail("corrupt frame (block of " .. bsize .. " bytes, the frame allows " .. blockLimit .. ")")
		end
		if btype == 0 or btype == 1 then
			local need = op + bsize
			if need > maxop then
				fail(if fcs then "corrupt frame (more data than its header announces)" else "the data expands beyond the limit of " .. limit .. " bytes")
			end
			if need > cap then
				out, cap = grow(out, cap, need, op, limit)
			end
			if btype == 0 then
				if pos + bsize > n then
					fail("truncated input (raw block)")
				end
				bcopy(out, op, src, pos, bsize)
				pos += bsize
				count(stats, "blocks_raw")
			else
				if pos >= n then
					fail("truncated input (RLE block)")
				end
				bfill(out, op, readu8(src, pos), bsize)
				pos += 1
				count(stats, "blocks_rle")
			end
			op = need
		elseif btype == 2 then
			local stop = pos + bsize
			if stop > n then
				fail("truncated input (compressed block)")
			end
			count(stats, "blocks_compressed")
			local bstart = op
			local lit, lp, ln
			lit, lp, ln, pos = literals(src, pos, stop, st, stats)
			local lend = lp + ln
			if pos >= stop then
				fail("truncated block (sequences header)")
			end
			local nseq = readu8(src, pos)
			pos += 1
			if nseq >= 128 then
				if nseq == 255 then
					if pos + 2 > stop then
						fail("truncated block (sequences header)")
					end
					nseq = readu16(src, pos) + 0x7F00
					pos += 2
				else
					if pos >= stop then
						fail("truncated block (sequences header)")
					end
					nseq = (nseq - 128) * 256 + readu8(src, pos)
					pos += 1
				end
			end
			if nseq == 0 then
				if pos ~= stop then
					fail("corrupt block (data after an empty sequences section)")
				end
				count(stats, "blocks_without_sequences")
			else
				if pos >= stop then
					fail("truncated block (sequence modes)")
				end
				local modes = readu8(src, pos)
				pos += 1
				if band(modes, 3) ~= 0 then
					fail("corrupt block (reserved sequence mode bits)")
				end
				llT, pos = seqTable(rshift(modes, 6), src, pos, stop, llT, LL_DEFAULT, 9, 35, stats, "ll")
				ofT, pos = seqTable(band(rshift(modes, 4), 3), src, pos, stop, ofT, OF_DEFAULT, 8, 31, stats, "of")
				mlT, pos = seqTable(band(rshift(modes, 2), 3), src, pos, stop, mlT, ML_DEFAULT, 9, 52, stats, "ml")
				local len = stop - pos
				if len <= 0 then
					fail("truncated block (no sequence bit stream)")
				end
				local lastb = readu8(src, stop - 1)
				if lastb == 0 then
					fail("corrupt block (sequence bit stream has no end mark)")
				end
				local base = pos
				local p = (len - 1) * 8 + (31 - countlz(lastb))
				local llSym, llNb, llBase = llT.sym, llT.nb, llT.base
				local ofSym, ofNb, ofBase = ofT.sym, ofT.nb, ofT.base
				local mlSym, mlNb, mlBase = mlT.sym, mlT.nb, mlT.base
				local sll = rbits(src, base, p, llT.log)
				p -= llT.log
				local sof = rbits(src, base, p, ofT.log)
				p -= ofT.log
				local sml = rbits(src, base, p, mlT.log)
				p -= mlT.log
				if p < 0 then
					fail("corrupt block (sequence bit stream too short)")
				end
				local blockMax = bstart + blockLimit
				for i = 1, nseq do
					local ofc = ofSym[sof + 1]
					local mlc = mlSym[sml + 1]
					local llc = llSym[sll + 1]
					local q
					-- offset code: the value is 2^code + `code` more bits
					local ofv = 1
					if ofc > 0 then
						q = p - ofc
						if q >= 0 and ofc <= 25 then
							ofv = lshift(1, ofc) + extract(readu32(src, base + rshift(q, 3)), band(q, 7), ofc)
						else
							ofv = 2 ^ ofc + rbits(src, base, p, ofc)
						end
						p = q
					end
					local ml = ML_BASE[mlc + 1]
					local nb = ML_BITS[mlc + 1]
					if nb > 0 then
						q = p - nb
						ml += if q >= 0 then extract(readu32(src, base + rshift(q, 3)), band(q, 7), nb) else rbits(src, base, p, nb)
						p = q
					end
					local ll = LL_BASE[llc + 1]
					nb = LL_BITS[llc + 1]
					if nb > 0 then
						q = p - nb
						ll += if q >= 0 then extract(readu32(src, base + rshift(q, 3)), band(q, 7), nb) else rbits(src, base, p, nb)
						p = q
					end
					if i < nseq then
						nb = llNb[sll + 1]
						if nb > 0 then
							q = p - nb
							sll = llBase[sll + 1]
								+ (if q >= 0 then extract(readu32(src, base + rshift(q, 3)), band(q, 7), nb) else rbits(src, base, p, nb))
							p = q
						else
							sll = llBase[sll + 1]
						end
						nb = mlNb[sml + 1]
						if nb > 0 then
							q = p - nb
							sml = mlBase[sml + 1]
								+ (if q >= 0 then extract(readu32(src, base + rshift(q, 3)), band(q, 7), nb) else rbits(src, base, p, nb))
							p = q
						else
							sml = mlBase[sml + 1]
						end
						nb = ofNb[sof + 1]
						if nb > 0 then
							q = p - nb
							sof = ofBase[sof + 1]
								+ (if q >= 0 then extract(readu32(src, base + rshift(q, 3)), band(q, 7), nb) else rbits(src, base, p, nb))
							p = q
						else
							sof = ofBase[sof + 1]
						end
					end
					if p < 0 then
						fail("corrupt block (sequence bit stream ends early)")
					end
					-- the offset: a new one, or one of the three last offsets
					local offset
					if ofv > 3 then
						offset = ofv - 3
						rep3 = rep2
						rep2 = rep1
						rep1 = offset
					else
						if ll == 0 then
							ofv += 1
						end
						if ofv == 1 then
							offset = rep1
						elseif ofv == 2 then
							offset = rep2
							rep2 = rep1
							rep1 = offset
						else
							if ofv == 3 then
								offset = rep3
							else
								offset = rep1 - 1
								if offset == 0 then
									fail("corrupt block (match offset 0)")
								end
							end
							rep3 = rep2
							rep2 = rep1
							rep1 = offset
						end
					end
					local need = op + ll + ml
					if need > blockMax then
						fail("corrupt block (it regenerates more than the " .. blockLimit .. " bytes a block may hold)")
					end
					if need > maxop then
						fail(if fcs then "corrupt frame (more data than its header announces)" else "the data expands beyond the limit of " .. limit .. " bytes")
					end
					if need > cap then
						out, cap = grow(out, cap, need, op, limit)
					end
					if ll > 0 then
						if lp + ll > lend then
							fail("corrupt block (a sequence wants more literals than the block has)")
						end
						bcopy(out, op, lit, lp, ll)
						op += ll
						lp += ll
					end
					if offset > op - fstart then
						fail("corrupt block (match offset " .. offset .. " reaches before the start of the frame)")
					elseif offset > window then
						fail("corrupt block (match offset " .. offset .. " reaches beyond the window of " .. window .. " bytes)")
					end
					local from = op - offset
					if offset >= ml then
						bcopy(out, op, out, from, ml)
					elseif offset == 1 then
						bfill(out, op, readu8(out, from), ml)
					else
						-- the match overlaps what it writes: the bytes from `from` on repeat with period `offset`
						local done = 0
						repeat
							local k = offset + done
							if k > ml - done then
								k = ml - done
							end
							bcopy(out, op + done, out, from, k)
							done += k
						until done >= ml
					end
					op = need
				end
				if p ~= 0 then
					fail("corrupt block (sequence bit stream not used up)")
				end
			end
			-- literals after the last sequence
			local rest = lend - lp
			if rest > 0 then
				local need = op + rest
				if need > maxop then
					fail(if fcs then "corrupt frame (more data than its header announces)" else "the data expands beyond the limit of " .. limit .. " bytes")
				end
				if need > cap then
					out, cap = grow(out, cap, need, op, limit)
				end
				bcopy(out, op, lit, lp, rest)
				op = need
			end
			if op - bstart > blockLimit then
				fail("corrupt block (it regenerates more than the " .. blockLimit .. " bytes a block may hold)")
			end
			pos = stop
		else
			fail("corrupt frame (reserved block type)")
		end
	until lastBlock
	if fcs and op - fstart ~= fcs then
		fail("corrupt frame (" .. (op - fstart) .. " bytes of content, its header announces " .. fcs .. ")")
	end
	if hasSum then
		if pos + 4 > n then
			fail("truncated input (content checksum)")
		end
		if xxh64low(out, fstart, op - fstart) ~= readu32(src, pos) then
			fail("content checksum mismatch")
		end
		pos += 4
		count(stats, "checksums_verified")
	end
	return pos, out, cap, op
end

local function decompress(input, limit, stats)
	if type(input) == "string" then
		input = buffer.fromstring(input)
	elseif type(input) ~= "buffer" then
		fail("a buffer is expected, got " .. typeof(input))
	end
	if limit == nil then
		limit = DEFAULT_LIMIT
	elseif type(limit) ~= "number" or limit < 0 or limit > 1073741824 then
		fail("the size limit must be a number of bytes up to 2^30")
	end
	if type(stats) ~= "table" then
		stats = nil
	end
	local n = blen(input)
	-- a copy with 8 spare bytes, so that 32-bit reads near the end stay inside
	local src = bcreate(n + 8)
	bcopy(src, 0, input, 0, n)
	local cap = math.min(limit, math.max(1024, n * 3))
	local out = bcreate(cap)
	local op = 0
	local pos = 0
	local st = {
		lit = bcreate(MAX_BLOCK + 8),
		htab = table.create(2048, 0),
		hbits = nil,
		weights = table.create(256, 0),
		rank = table.create(13, 0),
	}
	while pos < n do
		if n - pos < 4 then
			fail((n - pos) .. " stray byte(s) after the last frame")
		end
		local magic = readu32(src, pos)
		if magic == 0xFD2FB528 then
			pos, out, cap, op = frame(src, pos + 4, n, out, cap, op, limit, st, stats)
		elseif band(magic, 0xFFFFFFF0) == 0x184D2A50 then
			if n - pos < 8 then
				fail("truncated input (skippable frame)")
			end
			pos += 8 + readu32(src, pos + 4)
			if pos > n then
				fail("truncated input (skippable frame)")
			end
			count(stats, "frames_skippable")
		else
			fail(string.format("not a Zstandard frame (it starts with %08X at offset %d)", magic, pos))
		end
	end
	if cap ~= op then
		local exact = bcreate(op)
		bcopy(exact, 0, out, 0, op)
		out = exact
	end
	return out
end

local M = { decompress = decompress, xxh64low = xxh64low }

-- as a script: luau zstd.lua -a IN OUT [LIMIT]
local a1, a2, a3 = ...
if type(a1) == "string" and type(a2) == "string" and type(readfile) == "function" and type(writefile) == "function" then
	local data = readfile(a1)
	writefile(a2, buffer.tostring(decompress(buffer.fromstring(data), tonumber(a3))))
end

return M
