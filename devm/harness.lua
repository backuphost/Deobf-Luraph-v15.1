-- Luraph devirtualizer: runtime dump harness (runs under Luau CLI).
-- usage: luau harness.lua <patched_sample.lua> <out.json>
local samplePath, outPath = ...
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
		return '{"lfn":' .. tostring(line) .. ',"name":"' .. hex(name or "") .. '"}'
	elseif t == "buffer" then
		return '{"buf":"' .. hex(buffer.tostring(v)) .. '"}'
	else
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

local function finish(root)
	if done then return end
	done = true
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
		__LPH_ENV = env
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
function __LPH_PROTO(names, n, ...)
	local vals = { ... }
	local env = {}
	for i = 1, n do
		env[names[i]] = vals[i]
	end
	protos[#protos + 1] = env
	return #protos
end

function __LPH_REG(pid, fn)
	protoOfFn[fn] = pid
	return fn
end

-- ---------------------------------------------------------------- environment stubs
local function stub(name)
	local t = {}
	return setmetatable(t, {
		__index = function(_, k) return stub(name .. "." .. tostring(k)) end,
		__call = function() return stub(name .. "()") end,
		__tostring = function() return name end,
	})
end
-- Roblox datatype libraries: Vector3.new etc. become named stub functions
local rbxLibs = {}
local function rbxLib(name)
	local lib = rbxLibs[name]
	if lib then return lib end
	lib = setmetatable({}, {
		__index = function(t, k)
			local f = function(...) return stub(name .. "." .. tostring(k) .. "()") end
			fnames[f] = name .. "." .. tostring(k)
			rawset(t, k, f)
			return f
		end,
	})
	libnames[lib] = name
	rbxLibs[name] = lib
	return lib
end
local stubGlobals = { script = stub("script"), game = stub("game"), workspace = stub("workspace") }
sandbox = setmetatable({}, {
	__index = function(t, k)
		if traceMode and #trace < 200000 and string.sub(tostring(k), 1, 5) ~= "__LPH" then trace[#trace + 1] = tostring(k) end
		local v = _G[k]
		if v ~= nil then return v end
		v = stubGlobals[k]
		if v ~= nil then return v end
		if type(k) == "string" and string.match(k, "^%u") then
			return rbxLib(k)
		end
		return nil
	end,
})

local src = readfile(samplePath)
local fn, err = loadstring(src, "=sample")
if not fn then
	ewrite("COMPILE ERROR: " .. tostring(err) .. "\n")
	error("compile")
end
setfenv(fn, sandbox)
envTable = sandbox
local ok, e = pcall(fn)
if traceMode then
	trace[#trace + 1] = "RESULT " .. tostring(ok) .. " " .. tostring(e)
	writefile(outPath, table.concat(trace, "\n") .. "\n")
	return
end
if not done then
	ewrite("NOT REACHED: " .. tostring(e) .. "\n")
	error("hook not reached")
end
