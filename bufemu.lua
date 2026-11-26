-- 纯 Lua 模拟 Luau buffer 库：所有 buffer 操作走这里，最后可整体 dump。
-- 用稀疏表存字节（写 O(1)），避免 string 拼接的 O(n^2)。
--
-- Covers the whole Luau buffer library (create, fromstring, tostring, len, read*/write* for
-- i8..u32, f32, f64, readstring, writestring, readbits, writebits, copy, fill) with the
-- library's optional arguments, so a container that uses any of them gets real answers
-- (readf32/readf64 used to return 0, copy/fill needed all their arguments).
local byte, sub, char, concat = string.byte, string.sub, string.char, table.concat
local floor = math.floor

local BE = {}
local all = {}
local seq = 0

local function mk(n, init)
  n = n or 0
  seq = seq + 1
  local b = {t = {}, n = n, init = init, id = seq}
  if #all < 4096 then all[#all + 1] = b end
  return b
end

local function isbuf(b) return type(b) == "table" and type(b.t) == "table" and type(b.n) == "number" end
BE.__isbuffer = isbuf

function BE.create(n) return mk(n) end
function BE.fromstring(s) return mk(#s, s) end
function BE.len(b) return b.n end

local function getb(b, o)
  if not isbuf(b) then
    print("[BEERR] buffer=" .. tostring(b) .. " off=" .. tostring(o))
    print(debug.traceback("", 3))
    error("bad buffer", 0)
  end
  local v = b.t[o]
  if v ~= nil then return v end
  return b.init and byte(b.init, o + 1) or 0
end
local function putb(b, o, v) b.t[o] = v % 256 end

-- bytes o .. o+n-1 as a string, built in pieces (table.unpack of a huge table overflows)
local function slice(b, o, n)
  local parts, chunk, cnt = {}, {}, 0
  for i = o, o + n - 1 do
    cnt = cnt + 1
    chunk[cnt] = getb(b, i)
    if cnt == 1024 then parts[#parts + 1] = char(table.unpack(chunk)); chunk = {}; cnt = 0 end
  end
  if cnt > 0 then parts[#parts + 1] = char(table.unpack(chunk, 1, cnt)) end
  return concat(parts)
end

function BE.tostring(b) return slice(b, 0, b.n) end
function BE.readstring(b, o, n) return slice(b, o, n) end

function BE.copy(dst, doff, src, soff, sz)
  soff = soff or 0
  sz = sz or (src.n - soff)
  if dst == src and doff > soff then          -- overlapping: copy backwards
    for i = sz - 1, 0, -1 do dst.t[doff + i] = getb(src, soff + i) end
  else
    for i = 0, sz - 1 do dst.t[doff + i] = getb(src, soff + i) end
  end
end
function BE.fill(b, off, val, sz)
  sz = sz or (b.n - off)
  val = val % 256
  for i = 0, sz - 1 do b.t[off + i] = val end
end

function BE.readi8(b, o) local v = getb(b, o); return v >= 128 and v - 256 or v end
function BE.readu8(b, o) return getb(b, o) end
function BE.readi16(b, o) local a, c = getb(b, o), getb(b, o + 1)
  local v = a + c * 256; return v >= 32768 and v - 65536 or v end
function BE.readu16(b, o) return getb(b, o) + getb(b, o + 1) * 256 end
function BE.readi32(b, o)
  local v = getb(b, o) + getb(b, o + 1) * 256 + getb(b, o + 2) * 65536 + getb(b, o + 3) * 16777216
  if v >= 2147483648 then v = v - 4294967296 end
  return v end
function BE.readu32(b, o) return getb(b, o) + getb(b, o + 1) * 256 + getb(b, o + 2) * 65536 + getb(b, o + 3) * 16777216 end
function BE.readf32(b, o) return (string.unpack("<f", slice(b, o, 4))) end
function BE.readf64(b, o) return (string.unpack("<d", slice(b, o, 8))) end

function BE.writei8(b, o, v) putb(b, o, v) end
function BE.writeu8(b, o, v) putb(b, o, v) end
function BE.writei16(b, o, v) v = v % 65536; putb(b, o, v); putb(b, o + 1, floor(v / 256)) end
function BE.writeu16(b, o, v) BE.writei16(b, o, v) end
function BE.writei32(b, o, v)
  v = v % 4294967296
  putb(b, o, v % 256); putb(b, o + 1, floor(v / 256) % 256)
  putb(b, o + 2, floor(v / 65536) % 256); putb(b, o + 3, floor(v / 16777216) % 256)
end
function BE.writeu32(b, o, v) BE.writei32(b, o, v) end
local function puts(b, o, s) for i = 1, #s do b.t[o + i - 1] = byte(s, i) end end
function BE.writef32(b, o, v) puts(b, o, string.pack("<f", v)) end
function BE.writef64(b, o, v) puts(b, o, string.pack("<d", v)) end
function BE.writestring(b, o, s, cnt)
  cnt = cnt or #s
  for i = 1, cnt do putb(b, o + i - 1, byte(s, i)) end
end

-- bit access: bit offsets count from the least significant bit of byte 0
function BE.readbits(b, bitoff, count)
  local v, mul = 0, 1
  for i = 0, count - 1 do
    local p = bitoff + i
    local bt = getb(b, floor(p / 8))
    if floor(bt / 2 ^ (p % 8)) % 2 == 1 then v = v + mul end
    mul = mul * 2
  end
  return v
end
function BE.writebits(b, bitoff, count, value)
  for i = 0, count - 1 do
    local p = bitoff + i
    local o = floor(p / 8)
    local bt = getb(b, o)
    local mask = 2 ^ (p % 8)
    local has = floor(bt / mask) % 2 == 1
    local want = floor(value / 2 ^ i) % 2 == 1
    if has ~= want then b.t[o] = want and bt + mask or bt - mask end
  end
end

function BE.__dump(minlen)
  minlen = minlen or 512
  local out = {}
  for i = 1, #all do
    local b = all[i]
    if b.n >= minlen then
      out[#out + 1] = b
    end
  end
  return out
end

return BE
