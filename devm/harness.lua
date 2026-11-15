-- Luraph devirtualizer: runtime dump harness (runs under Luau CLI).
-- usage: luau harness.lua <patched_sample.lua> <out.json>
local samplePath, outPath = ...
local _G = getfenv() -- the CLI runs scripts in a sandboxed thread: script globals live here, not in the real _G
local layers = { select(3, ...) }
local layerIdx = 0
local traceMode = string.sub(outPath, -6) == ".trace"
local trace = {}

local ABORT = "__LPH_ABORT__"

-- ---------------------------------------------------------------- names of builtins
local fnames = {}
local function reg(prefix, t)
	for k, v in pairs(t) do
		if type(v) == "function" and type(k) == "string" then
			if not fnames[v] then fnames[v] = prefix .. k end
		end
	end
end
for _, lib in ipairs({ "string", "table", "math", "bit32", "buffer", "coroutine", "os", "utf8", "debug", "vector" }) do
	if _G[lib] then reg(lib .. ".", _G[lib]) end
end
for _, n in ipairs({ "assert", "error", "gcinfo", "getfenv", "getmetatable", "next", "newproxy", "print",
	"rawequal", "rawget", "rawset", "rawlen", "select", "setfenv", "setmetatable", "tonumber", "tostring",
	"type", "typeof", "ipairs", "pairs", "pcall", "xpcall", "unpack", "require" }) do
	if _G[n] then fnames[_G[n]] = n end
end
local libnames = {}
for _, lib in ipairs({ "string", "table", "math", "bit32", "buffer", "coroutine", "os", "utf8", "debug", "vector" }) do
	if _G[lib] then libnames[_G[lib]] = lib end
end

-- ---------------------------------------------------------------- serializer
local tblIds, tblList = {}, {}
local protoOfFn = {}
local out = {}
local function emit(s) out[#out + 1] = s end

local function hex(s)
	return (string.gsub(s, ".", function(c) return string.format("%02x", string.byte(c)) end))
end

local ser
local function serNum(v)
	if v ~= v then return '{"f":"nan"}' end
	if v == math.huge then return '{"f":"inf"}' end
	if v == -math.huge then return '{"f":"-inf"}' end
	if v == math.floor(v) and math.abs(v) < 2 ^ 53 then
		if v == 0 and 1 / v < 0 then return '{"f":"-0"}' end
		return string.format("%d", v)
	end
	return string.format("%.17g", v)
end

local envTable
local enumName
local constFns = {}
function ser(v)
	local t = type(v)
	if t == "nil" then return "null"
	elseif t == "number" then return serNum(v)
	elseif t == "boolean" then return v and "true" or "false"
	elseif t == "string" then return '{"s":"' .. hex(v) .. '"}'
	elseif t == "table" then
		if v == envTable then return '{"env":true}' end
		if libnames[v] then return '{"lib":"' .. libnames[v] .. '"}' end
		local id = tblIds[v]
		if not id then
			id = #tblList + 1
			tblIds[v] = id
			tblList[id] = v
		end
		return '{"t":' .. id .. "}"
	elseif t == "function" then
		if protoOfFn[v] then return '{"proto":' .. protoOfFn[v] .. "}" end
		if fnames[v] then return '{"fn":"' .. fnames[v] .. '"}' end
		local line = debug.info(v, "l")
		local name = debug.info(v, "n")
		if constFns[v] ~= nil then
			local c = constFns[v]
			return '{"cfn":' .. (c.value ~= nil and serNum(c.value) or "null") .. ',"k":' .. c.k .. "}"
		end
		if debug.info(v, "s") ~= "sample" then return '{"o":"function"}' end
		return '{"lfn":' .. tostring(line) .. ',"name":"' .. hex(name or "") .. '"}'
	elseif t == "buffer" then
		return '{"buf":"' .. hex(buffer.tostring(v)) .. '"}'
	else
		local en = enumName and enumName(v)
		if en then return '{"g":"' .. en .. '"}' end
		return '{"o":"' .. t .. '"}'
	end
end

local function dumpTables()
	local i = 1
	emit('"tables":{')
	local first = true
	while i <= #tblList do
		local tb = tblList[i]
		local parts = {}
		local n = 0
		for k in next, tb do
			if type(k) == "number" and k == math.floor(k) and k >= 1 and k > n then n = k end
		end
		if n > 4 * 1000 * 1000 then n = 0 end
		local arr = {}
		for j = 1, n do arr[j] = ser(rawget(tb, j)) end
		local hash = {}
		for k, v in next, tb do
			if not (type(k) == "number" and k == math.floor(k) and k >= 1 and k <= n) then
				hash[#hash + 1] = "[" .. ser(k) .. "," .. ser(v) .. "]"
			end
		end
		local mt = getmetatable(tb)
		local mts = "null"
		if type(mt) == "table" then mts = ser(mt) end
		emit((first and "" or ",") .. '"' .. i .. '":{"arr":[' .. table.concat(arr, ",") .. '],"hash":[' ..
			table.concat(hash, ",") .. '],"mt":' .. mts .. "}")
		first = false
		i += 1
	end
	emit("}")
end

-- ---------------------------------------------------------------- hooks
local protos = {}
local done = false

-- Constant tables can carry an __index hook that decrypts entries on first use; touch every
-- pending entry so the dump only ever sees plain values.
local function forceLazy(root)
	local seen, queue = {}, { root }
	local qi = 1
	while qi <= #queue do
		local t = queue[qi]
		qi += 1
		local mt = getmetatable(t)
		if type(mt) == "table" and type(rawget(mt, "__index")) == "function" and type(rawget(t, 0)) == "table" then
			local keys = {}
			for k in next, rawget(t, 0) do keys[#keys + 1] = k end
			for _, k in ipairs(keys) do pcall(function() return t[k] end) end
		end
		for k, v in next, t do
			if type(v) == "table" and not seen[v] and not libnames[v] and v ~= envTable then
				seen[v] = true
				queue[#queue + 1] = v
			end
		end
	end
end

-- The bootstrap installs tables of small functions that ignore their arguments and return a fixed
-- number (runtime "key" material for self-modifying code). Record their values so the lifter can
-- fold calls to them.
local function findConstFns(root)
	local seen, queue = {}, { root }
	local qi = 1
	while qi <= #queue do
		local t = queue[qi]
		qi += 1
		local n, all = 0, true
		for k, v in next, t do
			if type(v) == "function" and type(k) == "number" and string.sub(debug.info(v, "s"), 1, 5) == "layer" then
				n += 1
			else
				all = false
			end
			if type(v) == "table" and not seen[v] and not libnames[v] and v ~= envTable then
				seen[v] = true
				queue[#queue + 1] = v
			end
		end
		if all and n >= 2 then
			local vals = {}
			for k, f in next, t do
				local ok1, a, extra = pcall(f, 1, 2, 3, 4)
				local ok2, b = pcall(f, 977, 13, 40961, 7)
				if ok1 and ok2 and type(a) == "number" and a == b and extra == nil then
					constFns[f] = { k = k, value = a }
				else
					-- could not be evaluated outside Roblox: the lifter has to solve for it
					constFns[f] = { k = k }
				end
				vals[#vals + 1] = k .. "=" .. tostring(constFns[f].value)
			end
			ewrite("[keyfn] " .. table.concat(vals, " ") .. "\n")
		end
	end
end

local function finish(root)
	if done then return end
	done = true
	forceLazy(root)
	findConstFns(root)
	emit("{")
	emit('"root":' .. ser(root) .. ",")
	local ps = {}
	for i, p in ipairs(protos) do ps[i] = ser(p) end
	emit('"protos":[' .. table.concat(ps, ",") .. "],")
	dumpTables()
	emit("}")
	writefile(outPath, table.concat(out))
end

-- called at the entry of the interpreter closure: names = {"a","b"}, vals = {a, b} (n values)
local sandbox
function __LPH_RUN(names, n, ...)
	local vals = { ... }
	local env = {}
	for i = 1, n do
		env[names[i]] = vals[i]
	end
	if layerIdx < #layers then
		-- swap in the next (already lifted) layer instead of interpreting it
		layerIdx += 1
		_G.__LPH_ENV = env
		local src = readfile(layers[layerIdx])
		local fn, err = loadstring(src, "=layer" .. layerIdx)
		if not fn then
			ewrite("LAYER COMPILE ERROR: " .. tostring(err) .. "\n")
			error("layer compile")
		end
		setfenv(fn, sandbox)
		return fn
	end
	if traceMode then return nil end
	local root = { kind = "run", vars = env }
	finish(root)
	error(ABORT, 0)
end

-- factory-style VMs: record every proto instantiation
function __LPH_PROTO(interp, names, n, ...)
	local vals = { ... }
	local env = {}
	for i = 1, n do
		env[names[i]] = vals[i]
	end
	protos[#protos + 1] = { interp = interp, vars = env }
	return #protos
end

function __LPH_REG(pid, fn)
	protoOfFn[fn] = pid
	return fn
end

-- ---------------------------------------------------------------- environment (Roblox mock)
local here = string.match(debug.info(1, "s"), "^(.*)[/\\][^/\\]*$") or "."
local mock = assert(loadstring(readfile(here .. "/rbxmock.lua"), "=rbxmock"))()(_G, fnames, libnames)
enumName = mock.enumName
local rbxLibs = {}
local function rbxLib(name)
	local lib = rbxLibs[name]
	if lib then return lib end
	lib = mock[name] or mock.lib(name)
	rbxLibs[name] = lib
	return lib
end
sandbox = setmetatable({}, {
	__index = function(t, k)
		if traceMode and #trace < 200000 and string.sub(tostring(k), 1, 5) ~= "__LPH" then trace[#trace + 1] = tostring(k) end
		local v = mock[k]
		if v ~= nil then return v end
		v = _G[k]
		if v ~= nil then return v end
		if type(k) == "string" and string.match(k, "^%u") and k ~= "LPH_NO_VIRTUALIZE" and string.sub(k, 1, 4) ~= "LPH_" then
			return rbxLib(k)
		end
		return nil
	end,
})

-- utf8.nfcnormalize / utf8.nfdnormalize (tables generated by unidata.py next to the output file)
do
	local dir = string.match(outPath, "^(.*)[/\\][^/\\]*$") or "."
	local okd, text = pcall(readfile, dir .. "/nfdata.lua")
	local U = okd and loadstring(text, "=nfdata")() or { dec = {}, ccc = {}, comp = {} }
	local SB, LB, VB, TB, LC, VC, TC = 0xAC00, 0x1100, 0x1161, 0x11A7, 19, 21, 28
	local function nfd(s)
		local out = {}
		for _, cp in utf8.codes(s) do
			if cp >= SB and cp < SB + LC * VC * TC then
				local si = cp - SB
				out[#out + 1] = LB + si // (VC * TC)
				out[#out + 1] = VB + (si % (VC * TC)) // TC
				if si % TC ~= 0 then out[#out + 1] = TB + si % TC end
			elseif U.dec[cp] then
				for _, x in ipairs(U.dec[cp]) do out[#out + 1] = x end
			else
				out[#out + 1] = cp
			end
		end
		-- canonical ordering (stable bubble over combining marks)
		for i = 2, #out do
			local j = i
			while j > 1 do
				local a, b = U.ccc[out[j - 1]] or 0, U.ccc[out[j]] or 0
				if b ~= 0 and a > b then
					out[j - 1], out[j] = out[j], out[j - 1]
					j -= 1
				else
					break
				end
			end
		end
		return out
	end
	local function compose(a, b)
		if a >= LB and a < LB + LC and b >= VB and b < VB + VC then
			return SB + ((a - LB) * VC + (b - VB)) * TC
		end
		if a >= SB and a < SB + LC * VC * TC and (a - SB) % TC == 0 and b > TB and b < TB + TC then
			return a + (b - TB)
		end
		return U.comp[a .. "_" .. b]
	end
	local function nfc(s)
		local cps = nfd(s)
		local out = {}
		local starter = nil
		for _, cp in ipairs(cps) do
			local c = U.ccc[cp] or 0
			local merged = false
			if starter then
				local blocked = #out > starter and (U.ccc[out[#out]] or 0) >= c
				if not blocked then
					local r = compose(out[starter], cp)
					if r then
						out[starter] = r
						merged = true
					end
				end
			end
			if not merged then
				out[#out + 1] = cp
				if c == 0 then starter = #out end
			end
		end
		return out
	end
	local function enc(cps)
		local parts = {}
		for i = 1, #cps, 4000 do
			parts[#parts + 1] = utf8.char(unpack(cps, i, math.min(i + 3999, #cps)))
		end
		return table.concat(parts)
	end
	local u = {}
	for k, v in pairs(utf8) do u[k] = v end
	u.nfdnormalize = function(s) return enc(nfd(s)) end
	u.nfcnormalize = function(s) return enc(nfc(s)) end
	u.graphemes = function(s, i, j) return utf8.codes(s) end
	for k, v in pairs(u) do
		if type(v) == "function" then fnames[v] = "utf8." .. k end
	end
	libnames[u] = "utf8"
	mock.utf8 = u
	if samplePath == "--nftest" then
		for _, str in ipairs(layers) do
			print(table.concat({ string.byte(u.nfcnormalize(str), 1, -1) }, ","), "|", table.concat({ string.byte(u.nfdnormalize(str), 1, -1) }, ","))
		end
		return
	end
end

-- buffer writes fed with mock objects (where Roblox would have produced a number) must not throw
do
	local b = {}
	for k, v in pairs(buffer) do b[k] = v end
	for _, k in ipairs({ "writeu8", "writei8", "writeu16", "writei16", "writeu32", "writei32", "writef32", "writef64" }) do
		local real = buffer[k]
		b[k] = function(buf, off, v)
			if type(v) ~= "number" then v = 0 end
			return real(buf, off, v)
		end
		fnames[b[k]] = "buffer." .. k
	end
	libnames[b] = "buffer"
	mock.buffer = b
end

-- Harness/mock functions stand in for C functions: the sample must not be able to re-environment them.
local function foreign(f)
	if type(f) ~= "function" then return false end
	local src = debug.info(f, "s")
	return src ~= "sample" and string.sub(src, 1, 5) ~= "layer"
end
local realSetfenv, realGetfenv = setfenv, getfenv
mock.setfenv = function(f, env)
	if foreign(f) then error("'setfenv' cannot change environment of given object", 2) end
	if type(f) == "number" and f > 0 then f += 1 end
	return realSetfenv(f, env)
end
mock.getfenv = function(f)
	if foreign(f) then return sandbox end
	if f == nil then f = 1 end
	if type(f) == "number" and f > 0 then
		local ok, env = pcall(realGetfenv, f + 2)
		if not ok then return sandbox end
		if env == _G then return sandbox end
		return env
	end
	return realGetfenv(f)
end
fnames[mock.setfenv] = "setfenv"
fnames[mock.getfenv] = "getfenv"

-- ---------------------------------------------------------------- bootstrap helpers
-- The bootstrap layer derives an 8-byte XOR key from the outcome of its environment checks and
-- uses it to decrypt the rest of the bytecode buffer. The checks cannot all pass outside Roblox,
-- so recover the key statistically instead: the already-plain prefix of the same buffer gives the
-- byte distribution of serialized bytecode, and each key byte is chosen to best reproduce it.
function _G.__LPH_FIXKEY(key, buf, off, len)
	local total = buffer.len(buf)
	if off + len > total then len = total - off end
	-- per-residue histograms of the ciphertext
	local hist = {}
	for r = 0, 7 do
		local h = table.create(256, 0)
		for i = off + r, off + len - 1, 8 do
			local b = buffer.readu8(buf, i) + 1
			h[b] += 1
		end
		hist[r] = h
	end
	-- start: the most frequent plaintext byte of serialized bytecode is 0
	local k = {}
	for r = 0, 7 do
		local best, bestN = 0, -1
		for c = 0, 255 do
			if hist[r][c + 1] > bestN then best, bestN = c, hist[r][c + 1] end
		end
		k[r] = best
	end
	-- refine: all eight residues must decrypt to the same byte distribution
	for _ = 1, 6 do
		local pooled = table.create(256, 0.5)
		local n = 128
		for r = 0, 7 do
			for c = 0, 255 do
				local p = bit32.bxor(c, k[r]) + 1
				pooled[p] += hist[r][c + 1]
				n += hist[r][c + 1]
			end
		end
		local lg = table.create(256, 0)
		for i = 1, 256 do lg[i] = math.log(pooled[i] / n) end
		for r = 0, 7 do
			local best, bestScore = k[r], -math.huge
			for cand = 0, 255 do
				local sc = 0
				for c = 0, 255 do
					local h = hist[r][c + 1]
					if h > 0 then sc += h * lg[bit32.bxor(c, cand) + 1] end
				end
				if sc > bestScore then best, bestScore = cand, sc end
			end
			k[r] = best
		end
	end
	local old = {}
	for r = 0, 7 do
		old[r + 1] = key[r + 1]
		key[r + 1] = k[r]
	end
	ewrite(string.format("[fixkey] buffer=%d off=%d len=%d computed={%s} recovered={%s}\n", total, off, len,
		table.concat(old, ","), table.concat(key, ",")))
end

local crashes = 0
function _G.__LPH_CRASH()
	crashes += 1
	if crashes <= 4000 then
		ewrite("[bootstrap] failed environment check at " .. tostring(debug.info(2, "l")) .. " <- " .. tostring(debug.info(3, "l")) .. " <- " .. tostring(debug.info(4, "l")) .. "\n")
	end
end

local src = readfile(samplePath)
local fn, err = loadstring(src, "=sample")
if not fn then
	ewrite("COMPILE ERROR: " .. tostring(err) .. "\n")
	error("compile")
end
setfenv(fn, sandbox)
envTable = sandbox
local ok, e = xpcall(fn, function(m) return tostring(m) .. "\n" .. debug.traceback() end)
if traceMode then
	trace[#trace + 1] = "RESULT " .. tostring(ok) .. " " .. tostring(e)
	writefile(outPath, table.concat(trace, "\n") .. "\n")
	return
end
if not done then
	ewrite("NOT REACHED: " .. tostring(e) .. "\n")
	error("hook not reached")
end
