-- Luraph devirtualizer: runtime dump harness (runs under Luau CLI).
-- usage: luau harness.lua <patched_sample.lua> <out.json>
local samplePath, outPath = ...
local _G = getfenv() -- the CLI runs scripts in a sandboxed thread: script globals live here, not in the real _G
local layers = { select(3, ...) }
local layerIdx = 0
local traceMode = string.sub(outPath, -6) == ".trace"

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
local capOf, capVars
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
		local cap = capOf[v]
		if cap then
			return '{"lfn":' .. tostring(line) .. ',"fid":' .. cap.fid .. ',"cap":' .. ser(capVars(cap)) .. "}"
		end
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
	local root = { kind = "run", vars = env, pid = (select(n + 1, ...)) }
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

-- loader closures the lifter must be able to run: remember their upvalues at creation
capOf = setmetatable({}, { __mode = "k" })
function __LPH_CAP(fid, names, fn, getter)
	capOf[fn] = { fid = fid, names = names, getter = getter }
	return fn
end
function capVars(cap)
	if not cap.vars then
		local env = {}
		if cap.getter then
			local vals = table.pack(cap.getter())
			for i = 1, #cap.names do
				env[cap.names[i]] = vals[i]
			end
		end
		cap.vars = env
	end
	return cap.vars
end

function __LPH_REG(pid, fn)
	protoOfFn[fn] = pid
	return fn
end

-- ---------------------------------------------------------------- environment stubs
local stubName = setmetatable({}, { __mode = "k" })
local function brief(v)
	local t = type(v)
	if t == "string" then
		if #v > 40 then return string.format("%q", string.sub(v, 1, 40)) .. "~" .. #v end
		return string.format("%q", v)
	elseif t == "number" or t == "boolean" or t == "nil" then
		return tostring(v)
	elseif t == "table" and stubName[v] then
		return stubName[v]
	end
	return t
end
local function briefArgs(...)
	local n = select("#", ...)
	local parts = {}
	for i = 1, math.min(n, 8) do parts[i] = brief((select(i, ...))) end
	return table.concat(parts, ",")
end
-- trace mode streams every observable event to disk, so a script that ends in a deliberate
-- busy loop (Luraph's anti-tamper "crash" does exactly that) still leaves a complete trace
local TRACE_CAP = 60000
local traceCount = 0
local traceFull = false
local function tr(s)
	if not traceMode or traceFull then return end
	traceCount += 1
	appendfile(outPath, s .. "\n")
	if traceCount >= TRACE_CAP then
		traceFull = true
		appendfile(outPath, "RESULT trace cap reached\n")
		exitafter(0)
	end
end
local stubSeq = 0
local function stub(name)
	local t = {}
	stubName[t] = name
	local mt = {
		__index = function(_, k)
			tr("get " .. name .. "." .. brief(k))
			local v = stub(name .. "." .. tostring(k))
			rawset(t, k, v)
			return v
		end,
		__newindex = function(_, k, v)
			tr("set " .. name .. "." .. brief(k) .. " = " .. brief(v))
			rawset(t, k, v)
		end,
		__call = function(_, ...)
			tr("call " .. name .. "(" .. briefArgs(...) .. ")")
			stubSeq += 1
			return stub(name .. "()")
		end,
		__tostring = function() return name end,
		__len = function() return 0 end,
		__iter = function() return function() return nil end end,
		__concat = function(a, b) return tostring(a) .. tostring(b) end,
		__unm = function(a) return a end,
		__eq = function(a, b) return rawequal(a, b) end,
		__lt = function() return false end,
		__le = function() return true end,
	}
	for _, mm in ipairs({ "__add", "__sub", "__mul", "__div", "__mod", "__pow", "__idiv" }) do
		mt[mm] = function(a, b) return if type(a) == "number" then a elseif type(b) == "number" then b else 0 end
	end
	return setmetatable(t, mt)
end
-- deferred work (task.defer / task.delay / task.spawn after a yield) runs after the main chunk
local deferred = {}
local taskImpl = {
	spawn = function(f, ...)
		if type(f) == "function" then
			local co = coroutine.create(f)
			local ok, e = coroutine.resume(co, ...)
			if not ok then tr("task error " .. tostring(e)) end
			return co
		end
		return f
	end,
	defer = function(f, ...)
		if type(f) == "function" then deferred[#deferred + 1] = table.pack(f, ...) end
		return f
	end,
	delay = function(_, f, ...)
		if type(f) == "function" then deferred[#deferred + 1] = table.pack(f, ...) end
		return f
	end,
	wait = function(n) return n or 0 end,
	cancel = function() end,
	synchronize = function() end,
	desynchronize = function() end,
}
-- Roblox datatype libraries: Vector3.new etc. become named stub functions
local rbxLibs = {}
local function rbxLib(name, impl)
	local lib = rbxLibs[name]
	if lib then return lib end
	lib = setmetatable({}, {
		__index = function(t, k)
			local full = name .. "." .. tostring(k)
			local real = traceMode and impl and impl[k]
			local f = cwrap(function(...)
				tr("call " .. full .. "(" .. briefArgs(...) .. ")")
				if real then return real(...) end
				return stub(full .. "()")
			end, tostring(k))
			fnames[f] = full
			rawset(t, k, f)
			return f
		end,
	})
	libnames[lib] = name
	rbxLibs[name] = lib
	return lib
end
local stubGlobals = { script = stub("script"), game = stub("game"), workspace = stub("workspace"),
	task = rbxLib("task", taskImpl) }
if traceMode then
	stubGlobals.shared = stub("shared")
	for _, n in ipairs({ "wait", "spawn", "delay", "warn", "tick", "time", "elapsedTime", "settings", "UserSettings" }) do
		local f = cwrap(function(...)
			tr("call " .. n .. "(" .. briefArgs(...) .. ")")
			if n == "tick" or n == "time" or n == "elapsedTime" or n == "wait" then return 0 end
			if n == "spawn" then return taskImpl.spawn(...) end
			if n == "delay" then return taskImpl.delay(...) end
			if n == "warn" then return end
			return stub(n .. "()")
		end, n)
		stubGlobals[n] = f
	end
end
sandbox = setmetatable({}, {
	__index = function(t, k)
		if string.sub(tostring(k), 1, 5) ~= "__LPH" then tr(tostring(k)) end
		local v = _G[k]
		if v ~= nil then return v end
		v = stubGlobals[k]
		if v ~= nil then return v end
		if type(k) == "string" and string.match(k, "^%u") then
			return rbxLib(k)
		end
		return nil
	end,
	__newindex = function(t, k, v)
		tr("setglobal " .. tostring(k) .. " = " .. brief(v))
		rawset(t, k, v)
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
if traceMode then
	writefile(outPath, "")
	exitafter(tonumber(layers[#layers + 1] or "") or 20)
end
local ok, e = pcall(fn)
if traceMode then
	local di = 1
	while di <= #deferred and di < 1000 do
		local d = deferred[di]
		di += 1
		local dok, de = pcall(d[1], table.unpack(d, 2, d.n))
		if not dok then tr("deferred error " .. tostring(de)) end
	end
	tr("RESULT " .. tostring(ok) .. " " .. tostring(e))
	return
end
if not done then
	ewrite("NOT REACHED: " .. tostring(e) .. "\n")
	error("hook not reached")
end
