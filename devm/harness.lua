-- Luraph devirtualizer: runtime dump harness (runs under Luau CLI).
-- usage: luau harness.lua <patched_sample.lua> <out.json>
local samplePath, outPath = ...
local _G = getfenv() -- the CLI runs scripts in a sandboxed thread: script globals live here, not in the real _G
local layers = { select(3, ...) }
local layerIdx = 0
local traceMode = string.sub(outPath, -6) == ".trace"
local trace = {}

local ABORT = "__LPH_ABORT__"
local aborted = false
local function abort()
	aborted = true
	error(ABORT, 0)
end

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
local describeValue     -- Roblox datatype value -> constructor expression (set once the mock is loaded)
local fidOf = setmetatable({}, { __mode = "k" })
function __LPH_F(id, fn)
	fidOf[fn] = id
	return fn
end
local constFns = {}
local closEnv
function ser(v)
	local t = type(v)
	if t == "nil" then return "null"
	elseif t == "number" then return serNum(v)
	elseif t == "boolean" then return v and "true" or "false"
	elseif t == "string" then return '{"s":"' .. hex(v) .. '"}'
	elseif t == "table" then
		if v == envTable then return '{"env":true}' end
		if libnames[v] then return '{"lib":"' .. libnames[v] .. '"}' end
		local dv = describeValue and describeValue(v)
		if dv then return '{"g":"' .. dv .. '"}' end
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
		local id = fidOf[v]
		if id then
			-- single-line scripts: closures are identified by the id injected around their definition
			if closEnv and closEnv[v] then
				return '{"lfn":0,"fid":' .. id .. ',"name":"' .. hex(name or "") .. '","cenv":' .. ser(closEnv[v]) .. "}"
			end
			return '{"lfn":0,"fid":' .. id .. ',"name":"' .. hex(name or "") .. '"}'
		end
		if closEnv and closEnv[v] then
			return '{"lfn":' .. tostring(line) .. ',"name":"' .. hex(name or "") .. '","cenv":' .. ser(closEnv[v]) .. "}"
		end
		return '{"lfn":' .. tostring(line) .. ',"name":"' .. hex(name or "") .. '"}'
	elseif t == "buffer" then
		return '{"buf":"' .. hex(buffer.tostring(v)) .. '"}'
	else
		local en = enumName and enumName(v)
		if en then return '{"g":"' .. en .. '"}' end
		if t == "vector" and v.X == v.X and v.Y == v.Y and v.Z == v.Z then
			return '{"g":"Vector3.new(' .. string.format("%.14g, %.14g, %.14g", v.X, v.Y, v.Z) .. ')"}'
		end
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

-- Constant pools.  In some builds a proto's constant table is {[-1] = keys, [-2] = index array} with
-- an __index metamethod (itself VM code) that decodes pool entry index[pc] from the bytecode the
-- first time an instruction asks for it.  Instructions that never ran never asked, so the whole
-- pool is read out here through the live metamethod: pools[id of keys table][index] = value.
local poolsJson = nil
local function dumpPools(root, extra)
	local seen, queue = {}, { root }
	for _, t in ipairs(extra or {}) do
		if type(t) == "table" then queue[#queue + 1] = t end
	end
	local qi = 1
	local groups, order = {}, {}
	while qi <= #queue do
		local t = queue[qi]
		qi += 1
		local mt = getmetatable(t)
		if type(mt) == "table" and type(rawget(mt, "__index")) == "function" and type(rawget(t, -1)) == "table"
			and type(rawget(t, -2)) == "table" and not groups[rawget(t, -1)] then
			groups[rawget(t, -1)] = t
			order[#order + 1] = rawget(t, -1)
		end
		for k, v in next, t do
			if type(v) == "table" and not seen[v] and not libnames[v] and v ~= envTable then
				seen[v] = true
				queue[#queue + 1] = v
			end
		end
	end
	local parts = {}
	for _, keys in ipairs(order) do
		local t = groups[keys]
		local idx = rawget(t, -2)
		local K = 1073741823
		local saved = rawget(idx, K)
		local entries, misses, n = {}, 0, 0
		local i = 0
		while misses < 4096 and i < 1048576 do
			rawset(idx, K, i)
			local ok, v = pcall(function() return t[K] end)
			rawset(t, K, nil)
			if ok and v ~= nil then
				entries[#entries + 1] = '"' .. i .. '":' .. ser(v)
				misses = 0
				n += 1
			else
				misses += 1
			end
			i += 1
		end
		rawset(idx, K, saved)
		ewrite("[pool] " .. n .. " constants read from the pool of table " .. tostring(tblIds[keys]) .. "\n")
		parts[#parts + 1] = ser(keys):match("%d+") and ('"' .. ser(keys):match("%d+") .. '":{' .. table.concat(entries, ",") .. "}") or nil
	end
	poolsJson = "{" .. table.concat(parts, ",") .. "}"
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
	local okp, errp = pcall(dumpPools, root, protos)
	if not okp then ewrite("[pool] failed: " .. tostring(errp) .. "\n") end
	emit("{")
	if poolsJson then emit('"pools":' .. poolsJson .. ",") end
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
-- optional per-instruction hook (model.patched_source(step=True)): ring buffer of the last steps
local stepCount, stepLimit, ring, ringN = 0, math.huge, {}, 4000
local traceFrom, traceTo = math.huge, math.huge
local dumpAt = 1          -- dump the VM state at the n-th interpreter entry
local stubs = {}          -- protos replaced by a no-op (tamper responses)
local condCount, condRing, condRingN = 0, {}, 512
local condStep = {}
local runCount = 0
local stopAtFail = false
local showErrors = false
local runLog = false
local dumpEnd = false      -- dump the VM state when the script ends (normally or with an error)
local stubHits = 0
local mockRef = nil
-- oracle mode (oracle=1, needs the step-instrumented sample): for every executed instruction, the
-- numbers held by registers and interpreter locals on entry that were the same on every visit.
-- The lifter uses them to resolve self-decrypting instructions whose key is computed at run time.
local oracleOn = false
local watchLive = false
local watchPid, watchRegs, watchN, watchLog = {}, nil, 0, {}   -- watch=<pid>: remember the arguments of its last calls
local extraDepth = 0
local cGlobals = nil        -- environment of C functions as seen by getfenv(level)
local serverSide = false   -- server=1: RunService:IsServer() is true
local executorEnv = false  -- executor=1: provide identifyexecutor / iscclosure / islclosure
local pokePath = nil       -- poke=<file>: instructions to execute once after the run (see pokePass)
local mfpLog = false       -- mfplog=1: report every math_floor_precision call
local dumpNew = false
local recordBinds = false  -- binds=1: remember what payload closures were bound to, without exercising them
local exercise = false     -- exercise=1: after the script ends, call every payload function once
local budgetAt = math.huge
local probing = false      -- true while the poke pass runs
local pidAlias = {}        -- pid of a probe's copy of a proto -> pid of the proto itself
local tbPid = nil          -- tb=<pid>: print the Lua call stack when that proto first runs
local mfpDelta = nil       -- option mfpdelta=<n>: math_floor_precision(seed) - expected(seed) comes out as n
local showGlobals = false   -- globals=1: when the script ends, list the globals it defined
local baseGlobals = {}
local httpMap, httpMapPath = nil, nil    -- httpmap=<file> of `<url substring><TAB><reply body>` lines
local httpReply = nil      -- canned reply for game:HttpGet / HttpPost (option httpreply=<number>)
local orc, orcNames = {}, {}
local orcLast = setmetatable({}, { __mode = "k" })
-- the instruction at pc as text: every numeric field of the proto's code arrays
local insNames = {}
local function insText(pid, pc)
	local rec = protos[pid]
	if not rec then return "" end
	local names = insNames[pid]
	if not names then
		names = {}
		for name, arr in rec.vars do
			if type(arr) == "table" and arr ~= rec.self and type(name) == "string" then names[#names + 1] = name end
		end
		table.sort(names)
		insNames[pid] = names
	end
	local parts = {}
	for _, name in names do
		local x = rawget(rec.vars[name], pc)
		if type(x) == "number" then parts[#parts + 1] = name .. "=" .. string.format("%.17g", x) end
	end
	return table.concat(parts, " ")
end
local function writeOracle()
	if not oracleOn then return end
	oracleOn = false
	local parts = {}
	for key, rec in orc do
		local r, v = {}, {}
		for i, x in rec.r do
			if x == x and math.abs(x) ~= math.huge then r[#r + 1] = string.format('"%d":%.17g', i, x) end
		end
		for n, x in rec.v do
			if x == x and math.abs(x) ~= math.huge then v[#v + 1] = string.format('"%s":%.17g', n, x) end
		end
		local vs = {}
		for i, t in rec.vers do vs[i] = string.format('[%d,"%s"]', rec.steps[i], t) end
		local sc = {}
		for k2 in rec.succ do sc[#sc + 1] = string.format("[%d,%d]", k2 // 2 ^ 24, k2 % 2 ^ 24) end
		parts[#parts + 1] = string.format('"%d:%d:%d":{"n":%d,"p":%d,"r":{%s},"v":{%s},"vers":[%s],"last":%d,"final":"%s","succ":[%s]}', key // 2 ^ 32,
			(key // 2 ^ 24) % 256, key % 2 ^ 24, rec.n, rec.probe and 1 or 0, table.concat(r, ","), table.concat(v, ","), table.concat(vs, ","),
			rec.last or rec.steps[1], insText(key // 2 ^ 32, key % 2 ^ 24), table.concat(sc, ","))
	end
	writefile(outPath .. ".oracle", "{" .. table.concat(parts, ",\n") .. "}")
end
-- State read and written by code inlined into the sample (no calls: scripts measure the depth of
-- both the Lua call stack and the C stack, so instrumentation must not add frames):
--   __LPH_N.c  interpreter-closure entries so far     __LPH_N.t  entry number from which __LPH_RUN is called
--   __LPH_N.s  VM instructions executed so far         __LPH_N.f  true while the current instruction's
--   __LPH_S    protos replaced by a no-op                         branch decision is to be inverted
--   __LPH_X    instruction numbers whose decision is inverted
--   __LPH_R    ring of the last instructions (pid, loop, pc)
__LPH_N = { c = 0, s = 0, f = false, t = 1 }
__LPH_S = stubs
__LPH_T = setmetatable({}, { __mode = "k" })   -- protos already seen (dumpnew=1)
__LPH_X = {}
__LPH_R = {}
local RINGN = 4096
local scriptPath = nil
for i = #layers, 1, -1 do
	local sp = string.match(layers[i], "^scriptpath=(.+)$")
	local pk = string.match(layers[i], "^poke=(.+)$")
	local hm = string.match(layers[i], "^httpmap=(.+)$")
	if hm then
		httpMapPath = hm
		table.remove(layers, i)
	elseif sp then
		scriptPath = sp
		table.remove(layers, i)
	elseif pk then
		pokePath = pk
		table.remove(layers, i)
	end
end
while layers[1] and string.match(layers[1], "^%w+=%d+$") do
	local k, v = string.match(table.remove(layers, 1), "^(%w+)=(%d+)$")
	if k == "steps" then stepLimit = tonumber(v) elseif k == "dumpat" then dumpAt = tonumber(v)
	elseif k == "stub" then stubs[tonumber(v)] = true
	elseif k == "flipstep" then __LPH_X[tonumber(v)] = true
	elseif k == "stopatfail" then stopAtFail = tonumber(v) ~= 0
	elseif k == "errors" then showErrors = tonumber(v) ~= 0
	elseif k == "runlog" then runLog = tonumber(v) ~= 0
	elseif k == "dumpend" then dumpEnd = tonumber(v) ~= 0
	elseif k == "oracle" then oracleOn = tonumber(v) ~= 0
	elseif k == "server" then serverSide = tonumber(v) ~= 0
	elseif k == "executor" then executorEnv = tonumber(v) ~= 0
	elseif k == "exercise" then exercise = tonumber(v) ~= 0
	elseif k == "mfplog" then mfpLog = tonumber(v) ~= 0
	elseif k == "dumpnew" then dumpNew = tonumber(v) ~= 0
	elseif k == "binds" then recordBinds = tonumber(v) ~= 0
	elseif k == "tb" then tbPid = tonumber(v)
	elseif k == "depth" then extraDepth = tonumber(v)
	elseif k == "watch" then watchPid[tonumber(v)] = true
	elseif k == "watchlive" then watchLive = tonumber(v) ~= 0
	elseif k == "httpreply" then httpReply = v
	elseif k == "calls" then
		-- calls=1: log every call of a mocked Roblox / executor function (diagnosis)
		local n = 0
		_G.__LPH_CALLLOG = function(name, ...)
			if name == "GetComponents" then return end
			n += 1
			if n > 20000 then return end
			local a = table.pack(...)
			for i = 1, math.min(a.n, 4) do a[i] = type(a[i]) == "string" and string.format("%q", string.sub(a[i], 1, 80)) or tostring(a[i]) end
			ewrite("[call] " .. name .. "(" .. table.concat(a, ", ", 1, math.min(a.n, 4)) .. ")\n")
		end
	elseif k == "mfpdelta" then mfpDelta = tonumber(v)
	elseif k == "globals" then showGlobals = tonumber(v) > 0 and tonumber(v)
	elseif k == "tracefrom" then traceFrom = tonumber(v) elseif k == "traceto" then traceTo = tonumber(v) end
end
if oracleOn then traceMode = true end
__LPH_N.t = (traceMode or dumpEnd) and (runLog and 0 or math.huge) or dumpAt
local shadowOf, shadow = nil, nil
local prevRec, prevPc = nil, nil
local function brief(v)
	local t = type(v)
	if t == "number" then return string.format("%.17g", v)
	elseif t == "string" then return string.format("%q", #v > 48 and string.sub(v, 1, 48) .. "..." or v)
	elseif t == "table" then
		local n = rawlen(v)
		if n > 0 and n <= 18 then
			local parts = {}
			for i = 1, n do
				local x = rawget(v, i)
				if type(x) ~= "number" and type(x) ~= "boolean" then parts = nil break end
				parts[i] = tostring(x)
			end
			if parts then return "{" .. table.concat(parts, ",") .. "}" end
		end
		if n == 0 then
			-- dictionary: show (some of) its string keys
			local keys, cnt = {}, 0
			local k = next(v)
			while k ~= nil do
				cnt += 1
				if #keys < 70 then
					keys[#keys + 1] = type(k) == "string" and k or ("[" .. tostring(k) .. "]")
					if k == "n" and type(rawget(v, "n")) == "number" then keys[#keys] = "n=" .. rawget(v, "n") end
				end
				k = next(v, k)
			end
			if cnt > 0 then
				table.sort(keys)
				return "table{" .. table.concat(keys, ",") .. (cnt > #keys and ",..." or "") .. "}"
			end
		end
		return "table#" .. tostring(n)
	elseif t == "function" then return "fn:" .. tostring(fnames[v] or (protoOfFn[v] and "P" .. protoOfFn[v]) or (fidOf[v] and "F" .. fidOf[v]) or debug.info(v, "l"))
	else
		local ok, text = pcall(tostring, v)
		if not ok then text = "<" .. typeof(v) .. ">" end
		return (string.gsub(string.sub(text, 1, 40), "%s+", " "))
	end
end
local function ringText(n)
	local parts = {}
	for i = math.max(1, stepCount - n + 1), stepCount do
		local v = ring[i % ringN]
		parts[#parts + 1] = string.format("%d:%d:%d", v // 2 ^ 32, (v // 2 ^ 24) % 256, v % 2 ^ 24)
	end
	return table.concat(parts, " ")
end
-- branch-condition hook of the discovery variant (model.patched_source(conds=True)): records which
-- instructions take data-dependent decisions; inverting them is done by the inlined flag
function __LPH_C(kind, cond)
	condCount += 1
	local v = not not cond
	condRing[condCount % condRingN] = kind * 2 + (v and 1 or 0)
	condStep[condCount % condRingN] = __LPH_N.s
	return v
end
local function condText(n)
	local parts = {}
	for i = math.max(1, condCount - n + 1), condCount do
		local x = condRing[i % condRingN]
		parts[#parts + 1] = string.format("%d:%d:%d", condStep[i % condRingN], x // 2, x % 2)
	end
	return table.concat(parts, " ")
end
local function flowText(n)
	local parts = {}
	local last = __LPH_N.s
	for i = math.max(1, last - n + 1), last do
		local v = __LPH_R[i % RINGN]
		if v then parts[#parts + 1] = string.format("%d:%d:%d", v // 2 ^ 28, (v // 2 ^ 24) % 16, v % 2 ^ 24) end
	end
	return table.concat(parts, " ")
end

local lastRegs, seenErr = nil, {}
function __LPH_STEP(pid, loop, pc, regs, names, ...)
	if aborted then error(ABORT, 0) end
	stepCount += 1
	if stepCount > budgetAt then error("__LPH_BUDGET", 0) end
	pid = pidAlias[pid] or pid
	if tbPid == pid then
		tbPid = nil
		ewrite("[traceback] first instruction of proto " .. pid .. " (step " .. stepCount .. ")\n" .. debug.traceback() .. "\n")
	end
	if watchPid[pid] and regs then
		if regs ~= watchRegs then watchRegs, watchN = regs, 0 end
		watchN += 1
		if watchN == 2 then
			local parts = {}
			for i = 0, 60 do
				local v = rawget(regs, i)
				if v ~= nil then parts[#parts + 1] = "R" .. i .. "=" .. brief(v) end
			end
			watchLog[#watchLog + 1] = "pid " .. pid .. " step " .. stepCount .. ": " .. table.concat(parts, " ")
			if watchLive then
				-- watchlive=1: print every watched call as it happens, strings in full
				for i = 0, 60 do
					local v = rawget(regs, i)
					if type(v) == "string" then ewrite("[watch] step " .. stepCount .. " R" .. i .. " #" .. #v .. " " .. string.format("%q", v) .. "\n") end
				end
			end
			if #watchLog > 6 then table.remove(watchLog, 1) end
		end
	end
	if oracleOn and regs then
		local key = pid * 2 ^ 32 + loop * 2 ^ 24 + pc
		local rec = orc[key]
		local ver = insText(pid, pc)
		-- control-flow edges that were actually taken (per activation: one register array each)
		local prev = orcLast[regs]
		if prev then prev.succ[loop * 2 ^ 24 + pc] = true end
		if not rec then
			local r, v = {}, {}
			for i = 0, 1023 do
				local x = rawget(regs, i)
				if type(x) == "number" then r[i] = x end
			end
			if names then
				local list = orcNames[names]
				if not list then
					list = string.split(names, ",")
					orcNames[names] = list
				end
				local vals = { ... }
				for i, n in list do
					if type(vals[i]) == "number" then v[n] = vals[i] end
				end
				rec = { r = r, v = v, n = 1, names = list }
			else
				rec = { r = r, v = v, n = 1 }
			end
			rec.vers = { ver }
			rec.steps = { stepCount }
			rec.succ = {}
			rec.probe = probing      -- only ever executed by a probe: its register values mean nothing
			orc[key] = rec
		else
			rec.n += 1
			if not probing then rec.probe = false end
			if rec.vers[#rec.vers] ~= ver and #rec.vers < 16 then
				rec.vers[#rec.vers + 1] = ver
				rec.steps[#rec.steps + 1] = stepCount
			end
			rec.last = stepCount
			for i, x in rec.r do
				if rawget(regs, i) ~= x then rec.r[i] = nil end
			end
			if rec.names then
				local vals = { ... }
				for i, n in rec.names do
					if rec.v[n] ~= nil and vals[i] ~= rec.v[n] then rec.v[n] = nil end
				end
			end
		end
		orcLast[regs] = rec
	end
	ring[stepCount % ringN] = pid * 2 ^ 32 + loop * 2 ^ 24 + pc
	if showErrors and regs ~= lastRegs and regs then
		-- back in another activation: report error messages a protected call left in its registers
		lastRegs = regs
		for i = 0, 300 do
			local v = rawget(regs, i)
			if type(v) == "string" and #v < 400 and not seenErr[v] and string.find(v, "^[%w_%.%[%]\" /=]*:%d+: ") then
				seenErr[v] = true
				ewrite("[error] step " .. stepCount .. " pid " .. pid .. ": " .. v .. "\n")
			end
		end
	end
	if stepCount >= traceFrom and regs then
		-- value trace: registers that changed since the previous step of the same activation
		if shadowOf ~= regs then
			shadowOf, shadow = regs, {}
			ewrite("[enter] pid=" .. pid .. "\n")
		end
		local parts = {}
		for i = 0, 400 do
			local v = rawget(regs, i)
			if v ~= shadow[i] then
				shadow[i] = v
				parts[#parts + 1] = "R" .. i .. "=" .. brief(v)
			end
		end
		-- constants the previous instruction decoded on demand (visible only after it ran)
		if prevRec then
			local ks = {}
			local name, arr = next(prevRec.vars)      -- no generic-for here: the iterator call would use C stack
			while name ~= nil do
				if type(arr) == "table" and arr ~= prevRec.self and getmetatable(arr) then
					local x = rawget(arr, prevPc)
					if x ~= nil then ks[#ks + 1] = name .. "=" .. brief(x) end
				end
				name, arr = next(prevRec.vars, name)
			end
			if #ks > 0 then
				table.sort(ks)
				ewrite("      k{" .. table.concat(ks, " ") .. "}\n")
			end
		end
		prevRec, prevPc = protos[pid], pc
		-- the instruction's own fields (opcode / operand / constant arrays of the proto)
		local ins = {}
		local rec = protos[pid]
		if rec then
			local name, arr = next(rec.vars)
			while name ~= nil do
				if type(arr) == "table" then
					local x = rawget(arr, pc)
					if x ~= nil and type(x) ~= "function" and arr ~= rec.self then ins[#ins + 1] = name .. "=" .. brief(x) end
				end
				name, arr = next(rec.vars, name)
			end
			table.sort(ins)
		end
		ewrite(string.format("[t] %d %d:%d:%d  {%s}  %s\n", stepCount, pid, loop, pc, table.concat(ins, " "), table.concat(parts, " ")))
		if stepCount >= traceTo then abort() end
	end
	if stepCount >= stepLimit then
		ewrite("[steps] " .. stepCount .. " last: " .. ringText(ringN) .. "\n")
		abort()
	end
end

-- Recording costs calls; with a single frame left on the script's stack (it probes for that) a
-- Lua __LPH_STEP that calls anything would overflow where the bare script does not.
if cwrap and (oracleOn or traceMode or showErrors or tbPid) then
	__LPH_STEP = cwrap("__LPH_STEP", __LPH_STEP)
end

function __LPH_RUN(names, n, ...)
	if aborted then error(ABORT, 0) end
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
	if __LPH_N.c > runCount then runCount = __LPH_N.c else runCount += 1 end
	if stubs[env.__pid] then
		stubHits += 1
		if stubHits <= 5000 then
			ewrite("[stub] tamper response " .. tostring(env.__pid) .. " suppressed (entry " .. runCount .. ", step " ..
				math.max(stepCount, __LPH_N.s) .. ")\n")
		end
		for _, w in watchLog do ewrite("    [watch] " .. w .. "\n") end
		if stopAtFail then
			ewrite("[fail] step=" .. __LPH_N.s .. " run=" .. runCount .. "\n")
			ewrite("[flow] " .. flowText(1200) .. "\n")
			ewrite("[conds] " .. condText(condRingN) .. "\n")
			abort()
		end
		return function() end
	end
	if traceMode then
		if runLog and runCount <= 3000 then
			ewrite("[run] pid=" .. tostring(env.__pid) .. " n=" .. runCount .. " steps=" .. math.max(stepCount, __LPH_N.s) .. "\n")
		end
		return nil
	end
	if dumpNew then
		-- dumpnew=1 (sample patched with newproto): this is called for closures of protos not in
		-- __LPH_T only.  The first call registers every table of the first program; a later call
		-- whose proto and code arrays are all new means the bootstrap has started the script.
		local first = next(__LPH_T) == nil
		local fresh = 0
		for _, v in env do
			if type(v) == "table" and not __LPH_T[v] then fresh += 1 end
		end
		local queue = {}
		for _, v in env do if type(v) == "table" then queue[#queue + 1] = v end end
		local qi = 1
		while qi <= #queue do
			local t = queue[qi]
			qi += 1
			if not __LPH_T[t] and not libnames[t] and t ~= envTable then
				__LPH_T[t] = true
				for _, v in next, t do
					if type(v) == "table" and not __LPH_T[v] then queue[#queue + 1] = v end
				end
			end
		end
		if first or fresh < 4 then return nil end
		ewrite("[dumpnew] the script starts at interpreter entry " .. runCount .. "\n")
	elseif runCount < dumpAt then return nil end
	local root = { kind = "run", vars = env }
	finish(root)
	abort()
end

-- factory-style VMs: record every proto instantiation
function __LPH_PROTO(interp, names, n, ...)
	if aborted then error(ABORT, 0) end
	local vals = { ... }
	local env = {}
	for i = 1, n do
		env[names[i]] = vals[i]
	end
	local rec = { interp = interp, vars = env, self = vals[1] }
	if pokePath then
		-- to start this proto again at another instruction (see pokePass)
		rec.names, rec.vals, rec.n, rec.factory = names, vals, n, debug.info(2, "f")
	end
	protos[#protos + 1] = rec
	if #protos == 1 and mockRef then mockRef.mainThread = coroutine.running() end
	if traceMode and runLog then ewrite("[proto] pid=" .. #protos .. " interp=" .. interp .. "\n") end
	return #protos
end

-- for samples that carry their own instrumentation: marks the calling thread as the script's main thread
function __LPH_MAIN()
	if mockRef then mockRef.mainThread = coroutine.running() end
end

-- metamethod closures of the loader: remember the variables they captured
closEnv = setmetatable({}, { __mode = "k" })
function __LPH_CLOS(names, n, ...)
	local vals = { ... }
	local fn = vals[n + 1]
	local env = {}
	for i = 1, n do
		env[names[i]] = vals[i]
	end
	closEnv[fn] = env
	return fn
end

local lastBind = {}        -- pid -> arguments of the last upvalue binding of that proto
local regList = {}         -- every VM closure made, in order (exercise=1 calls the payload's)
function __LPH_REG(pid, fn)
	protoOfFn[fn] = pid
	if (exercise or pokePath or recordBinds) and protos[pid] and protos[1] and protos[pid].interp ~= protos[1].interp then
		-- fn binds upvalues and returns the callable closure: remember every closure it makes
		-- (payload only: the bootstrap counts the frames of its own calls)
		local bind = fn
		fn = function(...)
			local closure = bind(...)
			lastBind[pid] = table.pack(...)
			-- end-state dump: the values the closure was bound to (builds whose payload reads its
			-- jump states and library functions straight out of this list)
			if protos[pid].ups == nil then protos[pid].ups = { ... } end
			if type(closure) == "function" then
				protoOfFn[closure] = pid
				regList[#regList + 1] = closure
			end
			return closure
		end
		protoOfFn[fn] = pid
	end
	return fn
end

-- ---------------------------------------------------------------- environment (Roblox mock)
-- Wrap a Lua implementation so that its errors read like those of a C function: no position of
-- the implementation, and the position of the calling Lua function (if any) in front, as luaL_error does.
local function clike(impl)
	return function(...)
		local r = table.pack(pcall(impl, ...))
		if r[1] then return unpack(r, 2, r.n) end
		local msg = r[2]
		if type(msg) == "string" then
			msg = string.gsub(msg, "^[^\n:]*harness%.lua:%d+: ", "")
			msg = string.gsub(msg, "^rbxmock:%d+: ", "")
			error(msg, 2)
		end
		error(msg, 0)
	end
end
_G.__LPH_CLIKE = clike
local here = string.match(debug.info(1, "s"), "^(.*)[/\\][^/\\]*$") or "."
local mock = assert(loadstring(readfile(here .. "/rbxmock.lua"), "=rbxmock"))()(_G, fnames, libnames)
mockRef = mock
if scriptPath then mock.placeScript(scriptPath) end
mock.serverSide = serverSide
mock.mfpDelta = mfpDelta
mock.onMfp = function(i, v)
	if i == 1 and not mfpDelta then ewrite("[env] the script calls the global math_floor_precision, which only an earlier loader stage defines; its key cannot be reproduced without it [step " .. tostring(stepCount) .. "]\n") end
	if mfpLog then ewrite("[mfp] call " .. i .. " arg " .. tostring(v) .. " [step " .. tostring(stepCount) .. "]\n") end
end
mock.onMfpExpected = function(i, v, e)
	ewrite("[env] math_floor_precision call " .. i .. ": seed " .. tostring(v) .. ", expected " .. tostring(e) .. ", answering expected+" .. tostring(mfpDelta) .. "\n")
end
do
	-- A script's constants can be datatype values (Luraph caches `Color3.fromRGB(1, 2, 3)` in the
	-- instruction that built it).  In the dump they are written as the expression that makes them.
	local function n(x)
		if x ~= x or x == math.huge or x == -math.huge then return nil end
		if x == math.floor(x) and math.abs(x) < 2 ^ 53 then return string.format("%d", x) end
		return string.format("%.14g", x)
	end
	local function all(...)
		local out = {}
		for i = 1, select("#", ...) do
			local v = select(i, ...)
			if type(v) ~= "number" then return nil end
			local t = n(v)
			if not t then return nil end
			out[i] = t
		end
		return table.concat(out, ", ")
	end
	describeValue = function(v)
		local tn = mock.typeNames and mock.typeNames[v]
		if tn == "Color3" then
			local r, g, b = rawget(v, "R"), rawget(v, "G"), rawget(v, "B")
			if type(r) ~= "number" or type(g) ~= "number" or type(b) ~= "number" then return nil end
			local R, G, B = r * 255, g * 255, b * 255
			if math.abs(R - math.floor(R + 0.5)) < 1e-6 and math.abs(G - math.floor(G + 0.5)) < 1e-6 and math.abs(B - math.floor(B + 0.5)) < 1e-6 then
				return "Color3.fromRGB(" .. all(math.floor(R + 0.5), math.floor(G + 0.5), math.floor(B + 0.5)) .. ")"
			end
			local a = all(r, g, b)
			return a and "Color3.new(" .. a .. ")"
		elseif tn == "Vector3" then
			local a = all(rawget(v, "X"), rawget(v, "Y"), rawget(v, "Z"))
			return a and "Vector3.new(" .. a .. ")"
		elseif tn == "Vector2" then
			local a = all(rawget(v, "X"), rawget(v, "Y"))
			return a and "Vector2.new(" .. a .. ")"
		elseif tn == "UDim" then
			local a = all(rawget(v, "Scale"), rawget(v, "Offset"))
			return a and "UDim.new(" .. a .. ")"
		elseif tn == "UDim2" then
			local x, y = rawget(v, "X"), rawget(v, "Y")
			if type(x) ~= "table" or type(y) ~= "table" then return nil end
			local a = all(rawget(x, "Scale"), rawget(x, "Offset"), rawget(y, "Scale"), rawget(y, "Offset"))
			return a and "UDim2.new(" .. a .. ")"
		end
		return nil
	end
end
mock.onHttp = function(method, url)
	if httpMapPath and not httpMap then
		httpMap = {}
		local okm, text = pcall(readfile, httpMapPath)
		if not okm then ewrite("[http] cannot read reply map: " .. tostring(text) .. "\n") text = "" end
		for line in string.gmatch(text, "[^\n]+") do
			local key, body = string.match(line, "^([^\t]+)\t(.*)$")
			if key then table.insert(httpMap, { key, body }) end
		end
	end
	if httpMap and type(url) == "string" then
		for _, e in ipairs(httpMap) do
			if string.find(url, e[1], 1, true) then
				ewrite("[http] " .. method .. " " .. url .. " -> canned reply for '" .. e[1] .. "' [step " .. tostring(stepCount) .. "]\n")
				return e[2]
			end
		end
	end
	ewrite("[http] " .. method .. " " .. tostring(url) .. (httpReply and (" -> canned reply " .. httpReply) or " -> empty reply (no network)") .. " [step " .. tostring(stepCount) .. "]\n")
	return httpReply or ""
end
enumName = mock.enumName
local rbxLibs = {}
local function rbxLib(name)
	local lib = rbxLibs[name]
	if lib then return lib end
	lib = mock[name] or mock.lib(name)
	rbxLibs[name] = lib
	return lib
end
-- The script's environment is a plain table, as in Roblox: reading an existing global must not run
-- a metamethod (scripts measure the remaining C stack, and a Lua __index handler costs a level).
-- Only names that are not predefined fall through to the lazy Roblox-library fallback.
local fallback = setmetatable({}, {
	__index = function(t, k)
		if traceMode and string.sub(tostring(k), 1, 5) ~= "__LPH" then ewrite("[global] " .. tostring(k) .. "\n") end
		local v = mock[k]
		if v == nil then v = _G[k] end
		if v == nil and type(k) == "string" and string.match(k, "^%u") and k ~= "LPH_NO_VIRTUALIZE" and string.sub(k, 1, 4) ~= "LPH_" then
			v = rbxLib(k)
		end
		return v
	end,
})
-- (looking up a name that is not defined costs the script no Lua frame: it does so on purpose
-- with a single frame left on the stack)
if cwrap then
	local mt = getmetatable(fallback)
	mt.__index = cwrap("__index", mt.__index)
end
sandbox = setmetatable({}, { __index = fallback })
function populateSandbox()
	-- math: use a copy built by iterating the native table, matching the reference
	-- tool's wrapLib which also creates a new table from pairs(math).  The copy
	-- has the same keys inserted in the same order, so pairs(mathCopy) gives the
	-- same iteration sequence as the reference environment's pairs(math).
	do
		local mathCopy = {}
		for k, v in pairs(math) do mathCopy[k] = v end
		libnames[mathCopy] = "math"
		rawset(sandbox, "math", mathCopy)
	end
	for _, n in ipairs({ "assert", "error", "gcinfo", "getmetatable", "next", "newproxy", "print", "rawequal", "rawget",
		"rawset", "rawlen", "select", "setmetatable", "tonumber", "tostring", "type", "typeof", "ipairs", "pairs",
		"pcall", "xpcall", "unpack", "require", "string", "table", "bit32", "buffer", "coroutine", "os",
		"utf8", "debug", "vector", "_VERSION", "loadstring",
		"__LPH_RUN", "__LPH_PROTO", "__LPH_CLOS", "__LPH_REG", "__LPH_F", "__LPH_STEP", "__LPH_C", "__LPH_FIXKEY",
		"__LPH_N", "__LPH_S", "__LPH_X", "__LPH_R", "__LPH_T",
		"__LPH_CRASH", "__LPH_ENV", "__LPH_MAIN" }) do
		local v = _G[n]
		if v ~= nil then rawset(sandbox, n, v) end
	end
	for k, v in pairs(mock) do
		if type(k) == "string" and k ~= "onHttp" then rawset(sandbox, k, v) end
	end
	for _, n in ipairs({ "game", "workspace", "script", "Enum", "Instance", "Vector3", "Vector2", "CFrame", "Color3", "UDim",
		"UDim2", "Rect", "Random", "task", "wait", "spawn", "delay", "tick", "time", "warn", "shared", "_G",
		"TweenInfo", "NumberRange", "NumberSequence", "ColorSequence", "BrickColor", "Ray", "Region3", "Font",
		"Path2DControlPoint", "DateTime", "PhysicalProperties", "Axes", "Faces", "Vector3int16", "Vector2int16",
		"NumberSequenceKeypoint", "ColorSequenceKeypoint", "OverlapParams", "RaycastParams", "SharedTable",
		-- executor-specific globals probed by environment-fingerprinting scripts
		"math_floor_precision" }) do
		if rawget(sandbox, n) == nil then
			local v = fallback[n]
			if v ~= nil then rawset(sandbox, n, v) end
		end
	end
	-- Like a Roblox script environment, what getfenv() hands out is a nearly empty table whose
	-- metatable falls through to the globals (a table, so a lookup still costs no call): scripts
	-- enumerate it and hash the names they find.  script, _G and shared are real fields of it:
	-- anti-cheat code treats an environment that only inherits _G / shared as an executor's.
	local globals = sandbox
	rawset(globals, "_G", {})       -- the scripts' shared table, not the CLI's own globals
	if executorEnv and rawget(globals, "request") == nil then
		-- executor HTTP: nothing is sent.  Every call is logged and answered with a canned reply
		-- (httpreply=<text>, default empty body), so the script's handling of it can be traced.
		local function request(opts)
			local url = type(opts) == "table" and (opts.Url or opts.url) or opts
			local method = type(opts) == "table" and (opts.Method or opts.method) or "GET"
			local body = mock.onHttp(tostring(method), url)
			return { StatusCode = 200, StatusMessage = "OK", Success = true, Body = body, Headers = {} }
		end
		for _, n in ipairs({ "request", "http_request" }) do rawset(globals, n, request) end
		-- other executor functions scripts expect to exist; inert stand-ins
		if rawget(globals, "gethwid") == nil then rawset(globals, "gethwid", function() return "devm-offline-hwid" end) end
		for _, n in ipairs({ "setclipboard", "toclipboard" }) do
			if rawget(globals, n) == nil then rawset(globals, n, function() end) end
		end
		-- require(ModuleScript): the game's modules do not exist here; hand out a permissive stub
		do
			local realRequire = rawget(globals, "require")
			local function impl(m)
				if type(m) == "string" and realRequire then return realRequire(m) end
				return mock.stub("require(" .. tostring(m) .. ")")
			end
			local f = cwrap and cwrap("require", impl) or impl
			fnames[f] = "require"
			rawset(globals, "require", f)
		end
		-- the rest of the usual executor API, inert: hooks install nothing, fire* do nothing
		local genv = setmetatable({}, { __index = globals })
		local noop = function() end
		local ex = {
			getgenv = function() return genv end, getrenv = function() return globals end,
			newcclosure = function(f) return f end, clonefunction = function(f) return f end,
			hookfunction = function(f) return f end, replaceclosure = function(f) return f end,
			hookmetamethod = function() return function() end end,
			checkcaller = function() return true end, getnamecallmethod = function() return "" end,
			cloneref = function(x) return x end, compareinstances = function(a, b) return a == b end,
			gethui = function() return fallback.game:GetService("CoreGui") end,
			getconnections = function() return {} end, getgc = function() return {} end,
			getinstances = function() return {} end, getnilinstances = function() return {} end,
			getloadedmodules = function() return {} end, getrunningscripts = function() return {} end,
			isexecutorclosure = function() return false end, getcustomasset = function() return "" end,
			getthreadidentity = function() return 8 end, setthreadidentity = noop,
			fireproximityprompt = noop, firetouchinterest = noop, fireclickdetector = noop, firesignal = noop,
			setfpscap = noop, queue_on_teleport = noop, setclipboard = noop, toclipboard = noop,
			sethiddenproperty = noop, gethiddenproperty = noop, setsimulationradius = noop,
		}
		for n, f in pairs(ex) do
			if rawget(globals, n) == nil then rawset(globals, n, f) end
		end
		-- executor file system: empty and read-only
		local fs = { isfile = function() return false end, isfolder = function() return false end,
			listfiles = function() return {} end, writefile = function() end, appendfile = function() end,
			makefolder = function() end, delfile = function() end, delfolder = function() end,
			readfile = function(path) error("file does not exist: " .. tostring(path), 2) end }
		for n, f in pairs(fs) do
			if rawget(globals, n) == nil then rawset(globals, n, f) end
		end
	end
	if not executorEnv then
		-- a plain Roblox client has none of these; scripts with their own anti-cheat look for them
		for _, n in ipairs({ "identifyexecutor", "iscclosure", "islclosure" }) do
			mock[n] = nil
			rawset(globals, n, nil)
		end
	end
	sandbox = setmetatable({ script = rawget(globals, "script"), _G = rawget(globals, "_G"), shared = rawget(globals, "shared") },
		{ __index = globals })
	cGlobals = setmetatable({ _G = rawget(globals, "_G"), shared = rawget(globals, "shared") }, { __index = globals })
end

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
	-- insertion order fixes the iteration order of the table, which scripts hash: the same order
	-- as the reference environment (graphemes, nfcnormalize, nfdnormalize after the built-ins)
	u.graphemes = true
	u.nfcnormalize = function(s) return enc(nfc(s)) end
	u.nfdnormalize = function(s) return enc(nfd(s)) end
	-- utf8.graphemes(s, i, j): stateful iterator over (first byte, last byte) of each grapheme cluster;
	-- a cluster is approximated as a starter followed by combining marks / joiners
	u.graphemes = function(s, i, j)
		i = i or 1
		j = j or #s
		local pos = i
		local function width(p)
			local b = string.byte(s, p)
			return b < 0x80 and 1 or (b < 0xE0 and 2 or (b < 0xF0 and 3 or 4))
		end
		local function extends(p)
			local ok, cp = pcall(utf8.codepoint, s, p)
			if not ok then return false end
			return (U.ccc[cp] or 0) ~= 0 or (cp >= 0x300 and cp <= 0x36F) or cp == 0x200D or (cp >= 0xFE00 and cp <= 0xFE0F)
		end
		local function iter()
			if pos > j then return nil end
			local first = pos
			pos += width(pos)
			while pos <= j and extends(pos) do pos += width(pos) end
			return first, pos - 1
		end
		return cwrap and cwrap("", iter) or iter
	end
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
		local size = (string.find(k, "64") and 8) or (string.find(k, "32") and 4) or (string.find(k, "16") and 2) or 1
		b[k] = function(...)
			local buf, off, v = ...
			if type(v) == "table" or type(v) == "userdata" then v = 0 end
			if type(buf) == "buffer" and type(off) == "number" and type(v) == "number" and off >= 0 and off + size <= buffer.len(buf) then
				return real(buf, off, v)       -- no extra C call on the path that cannot fail
			end
			-- failing call: let the C function raise without this wrapper's position, then re-raise
			-- with the position of the calling Lua function (if any), as luaL_error would have
			local r
			if select("#", ...) >= 3 then r = table.pack(pcall(real, buf, off, v, select(4, ...))) else r = table.pack(pcall(real, ...)) end
			if r[1] then return unpack(r, 2, r.n) end
			error(r[2], 2)
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
-- debug.info must describe harness/mock functions the way it describes the C functions they
-- stand in for: scripts compare debug.info(type, "slnaf") with "[C]", -1, "type", 0, true, type,
-- and walk the stack from error handlers expecting [handler, builtin, xpcall, caller].
-- The stack shown to the script is the real one with every run of mock frames (and the C frames
-- they call through) folded into a single frame that looks like a C function.
do
	local realInfo = debug.info
	local d = {}
	for k, v in pairs(mock.debug or debug) do d[k] = v end
	local function mockLua(src)
		return src ~= "[C]" and src ~= "sample" and string.sub(src, 1, 5) ~= "layer"
	end
	local function describe(a, b)
		local out, n = {}, 0
		for i = 1, #b do
			local ch = string.sub(b, i, i)
			n += 1
			if ch == "s" then out[n] = "[C]"
			elseif ch == "l" then out[n] = -1
			elseif ch == "n" then out[n] = string.match(fnames[a] or "", "[^%.]*$")
			elseif ch == "a" then out[n] = 0; n += 1; out[n] = true
			elseif ch == "f" then out[n] = a
			else error("invalid option", 3) end
		end
		return unpack(out, 1, n)
	end
	d.info = function(a, b, c)
		if type(a) == "function" and type(b) == "string" then
			if mockLua(realInfo(a, "s")) then return describe(a, b) end
			return realInfo(a, b)
		end
		if type(a) == "number" and type(b) == "string" then
			if a < 0 then return realInfo(a, b) end
			if a == 0 then return describe(d.info, b) end
			-- virtual frames, innermost first: positive = real level, function = folded mock frame
			local frames, n = {}, 0
			local lvl = 2
			while true do
				local f, src = realInfo(lvl, "fs")
				if f == nil then break end
				if mockLua(src) then
					while n > 0 do
						local top = frames[n]
						if type(top) == "function" or realInfo(top, "s") == "[C]" then n -= 1 else break end
					end
					n += 1
					frames[n] = f
				else
					n += 1
					frames[n] = lvl
				end
				if n > a and type(frames[a]) == "number" and not mockLua(src) and src ~= "[C]" then break end
				lvl += 1
			end
			local fr = frames[a]
			if a > n or fr == nil then return end
			if type(fr) == "function" then return describe(fr, b) end
			return realInfo(fr, b)
		end
		if c ~= nil then return realInfo(a, b, c) end
		return realInfo(a, b)
	end
	for k, v in pairs(d) do
		if type(v) == "function" and not fnames[v] then fnames[v] = "debug." .. k end
	end
	libnames[d] = "debug"
	mock.debug = d
end
fnames[mock.getfenv] = "getfenv"

-- Closure-kind predicates that executors add to the environment.  Luraph's bootstrap looks them
-- up and, when present, uses them in its builtin sweep; the reference environment the key
-- material was checked against (an executor-style one) has them, so they are provided here too.
mock.iscclosure = function(f)
	if type(f) ~= "function" then error("invalid argument #1 to 'iscclosure' (function expected, got " .. typeof(f) .. ")", 2) end
	return foreign(f)
end
mock.islclosure = function(f)
	if type(f) ~= "function" then error("invalid argument #1 to 'islclosure' (function expected, got " .. typeof(f) .. ")", 2) end
	return not foreign(f)
end
mock.identifyexecutor = function() return "Wave", "1.0.0" end
fnames[mock.identifyexecutor] = "identifyexecutor"
fnames[mock.iscclosure] = "iscclosure"
fnames[mock.islclosure] = "islclosure"

-- With cwrap (luau_cli.patch) the shims above become real C closures: one frame per call, "[C]"
-- to debug.info and to stack walks without any help.  debug.info itself is then the real one.
if cwrap then
	local marker = newproxy()
	local realInfo = debug.info
	local function native(name, impl)
		local f = cwrap(string.match(name, "[^%.]*$"), impl)
		fnames[f] = name
		return f
	end
	-- needs the caller's stack (levels): the implementation is handed the calling thread
	local function nativeT(name, impl)
		local f = cwrap(string.match(name, "[^%.]*$"), impl, marker)
		fnames[f] = name
		return f
	end
	local function isC(f) return realInfo(f, "s") == "[C]" end
	local function atLevel(co, level, what)
		if level < 0 then error("invalid argument #1 to '" .. what .. "' (level must be non-negative)", 0) end
		local f = realInfo(co, level, "f")
		if f == nil then error("invalid argument #1 to '" .. what .. "' (invalid level)", 0) end
		return f
	end
	mock.getfenv = nativeT("getfenv", function(co, f)
		if f == nil then f = 1 end
		local byLevel = false
		if type(f) == "number" then
			if f == 0 then return sandbox end
			f = atLevel(co, f, "getfenv")
			byLevel = true
		elseif type(f) ~= "function" then
			error("invalid argument #1 to 'getfenv' (number expected, got " .. typeof(f) .. ")", 0)
		end
		if isC(f) then
			-- a C function on the stack (getfenv called through pcall) has the engine's global
			-- table as its environment, where _G and shared are real fields
			if byLevel and cGlobals then return cGlobals end
			return sandbox
		end
		local env = realGetfenv(f)
		if env == _G then return sandbox end
		return env
	end)
	mock.setfenv = nativeT("setfenv", function(co, f, env)
		if type(env) ~= "table" then
			error("invalid argument #2 to 'setfenv' (table expected, got " .. (env == nil and "no value" or typeof(env)) .. ")", 0)
		end
		local target = f
		if type(f) == "number" then
			if f == 0 then return end
			target = atLevel(co, f, "setfenv")
		elseif type(f) ~= "function" then
			error("invalid argument #1 to 'setfenv' (number expected, got " .. typeof(f) .. ")", 0)
		end
		if isC(target) then error("'setfenv' cannot change environment of given object", 0) end
		realSetfenv(target, env)
		return f
	end)
	mock.iscclosure = native("iscclosure", function(f)
		if type(f) ~= "function" then error("invalid argument #1 to 'iscclosure' (function expected, got " .. typeof(f) .. ")", 0) end
		return isC(f)
	end)
	mock.islclosure = native("islclosure", function(f)
		if type(f) ~= "function" then error("invalid argument #1 to 'islclosure' (function expected, got " .. typeof(f) .. ")", 0) end
		return not isC(f)
	end)
	mock.identifyexecutor = native("identifyexecutor", function() return "Wave", "1.0.0" end)
	-- buffer writes fed with mock objects (where Roblox would have produced a number) must not throw
	for _, k in ipairs({ "writeu8", "writei8", "writeu16", "writei16", "writeu32", "writei32", "writef32", "writef64" }) do
		local real = buffer[k]
		mock.buffer[k] = native("buffer." .. k, function(...)
			local r
			if select("#", ...) >= 3 then
				local buf, off, v = ...
				if type(v) == "table" or type(v) == "userdata" then v = 0 end
				r = table.pack(pcall(real, buf, off, v, select(4, ...)))
			else
				r = table.pack(pcall(real, ...))
			end
			if not r[1] then error(r[2], 0) end
			return unpack(r, 2, r.n)
		end)
	end
	for _, k in ipairs({ "nfdnormalize", "nfcnormalize", "graphemes" }) do
		mock.utf8[k] = native("utf8." .. k, mock.utf8[k])
	end
	local d = {}
	for k, v in pairs(mock.debug) do d[k] = v end
	d.info = realInfo
	libnames[d] = "debug"
	mock.debug = d
end

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
populateSandbox()
for k, v in pairs(sandbox) do baseGlobals[k] = v end
setfenv(fn, sandbox)
envTable = sandbox
if showGlobals == 2 then
	-- globals=2: the script sees a logging proxy (detectable; for diagnosis only)
	local real, seen = sandbox, {}
	local proxy = setmetatable({}, {
		__index = function(_, k)
			local v = real[k]
			if not seen[k] then seen[k] = true; ewrite("[gread] " .. tostring(k) .. " -> " .. type(v) .. "\n") end
			return v
		end,
		__newindex = function(_, k, v)
			ewrite("[gwrite] " .. tostring(k) .. " = " .. type(v) .. " " .. brief(v) .. "\n")
			real[k] = v
		end,
	})
	setfenv(fn, proxy)
	envTable = proxy
end
-- Run at the call depth of an ordinary script thread (Lua frames and C calls alike): scripts probe
-- how much of either stack is left.  A CLI built with luau_cli.patch runs the function this script
-- returns as the main function of a fresh top-level thread; a stock CLI gets a plain call, one
-- frame deeper than the real thing.
-- Code decrypts itself as it runs, so functions nobody called (handlers, the API of a module)
-- stay ciphertext.  Call each closure of the payload once, on a thread of its own, with permissive
-- stand-in arguments and an instruction budget; closures made along the way are called too.
local function exercisePayload()
	if not protos[1] then return end
	local boot = protos[1].interp
	local calls, i = 0, 1
	local called = {}
	local before = stepCount
	while i <= #regList and calls < 4000 do
		local fn = regList[i]
		i += 1
		local rec = protos[protoOfFn[fn]]
		if rec and rec.interp ~= boot and not called[fn] then
			called[fn] = true
			calls += 1
			local args = {}
			for j = 1, 8 do args[j] = mockRef.stub("arg" .. j) end
			budgetAt = stepCount + 200000
			local co = coroutine.create(fn)
			local _, okr, errr = pcall(coroutine.resume, co, unpack(args))
			if not okr and calls <= 400 then ewrite("[exercise] proto " .. protoOfFn[fn] .. ": " .. string.sub(tostring(errr), 1, 160) .. "\n") end
		end
	end
	budgetAt = math.huge
	ewrite("[exercise] called " .. calls .. " payload closures, " .. (stepCount - before) .. " more instructions\n")
end

-- Instructions on paths that never ran are still encrypted, and so are their constants: each
-- decrypts and rewrites itself when it executes, with keys the interpreter carries in locals.
-- The lifter knows those keys for every instruction it reached statically; the poke file lists
-- them, one instruction per line:
--     pid;<param>=<number>;<param>.<index>=<number>;...
-- For each line the proto's factory is called again with those parameters replaced (entry pc,
-- dispatch mode, keys), its upvalues are bound as they were last, and the interpreter runs until
-- it fails for lack of the values the real path would have had in its registers (or for a few
-- hundred instructions).  The code arrays and constant tables are shared with the real proto, so the
-- plain instruction and its decoded constants are in the dump afterwards.
local function pokePass()
	local text = readfile(pokePath)
	local done, skipped = 0, 0
	-- A probe starts with empty registers and may wander into code that already ran; whatever it
	-- rewrites there (instructions patch themselves with register values) is put back.
	local guard = {}
	for key in orc do
		local pid, pc = key // 2 ^ 32, key % 2 ^ 24
		local rec = protos[pid]
		if rec and protos[1] and rec.interp ~= protos[1].interp then
			for _, arr in rec.vars do
				if type(arr) == "table" and arr ~= rec.self and getmetatable(arr) == nil then
					local g = guard[arr]
					if not g then
						g = {}
						guard[arr] = g
					end
					local v = rawget(arr, pc)
					g[pc] = v == nil and guard or v      -- (guard itself stands for nil)
				end
			end
		end
	end
	probing = true
	local restored = 0
	local function restore()
		for arr, g in guard do
			for pc, v in g do
				local want = v
				if v == guard then want = nil end
				if rawget(arr, pc) ~= want then
					rawset(arr, pc, want)
					restored += 1
				end
			end
		end
	end
	for line in string.gmatch(text, "[^\n]+") do
		local fields = string.split(line, ";")
		local pid = tonumber(fields[1])
		local rec = protos[pid]
		local ups = lastBind[pid]
		if rec and rec.factory and ups then
			local vals = table.clone(rec.vals)
			local pos = {}
			for i = 1, rec.n do pos[rec.names[i]] = i end
			local copied = {}
			for j = 2, #fields do
				local name, value = string.match(fields[j], "^([^=]+)=(.+)$")
				local base, idx = string.match(name, "^([^.]+)%.(%d+)$")
				if base then
					local i = pos[base]
					if i then
						if not copied[i] then
							vals[i] = table.clone(vals[i])
							copied[i] = true
						end
						vals[i][tonumber(idx)] = tonumber(value)
					end
				elseif pos[name] then
					vals[pos[name]] = tonumber(value)
				end
			end
			local before = #protos
			local okf, binder = pcall(rec.factory, unpack(vals, 1, rec.n))
			for clone = before + 1, #protos do pidAlias[clone] = pid end
			local okb, closure
			if okf and type(binder) == "function" then okb, closure = pcall(binder, unpack(ups, 1, ups.n)) end
			if okb and type(closure) == "function" then
				budgetAt = stepCount + 400
				pcall(coroutine.resume, coroutine.create(closure))
				restore()
				done += 1
			else
				skipped += 1
			end
		else
			skipped += 1
		end
	end
	budgetAt = math.huge
	probing = false
	ewrite("[poke] started at " .. done .. " instructions that had not run (" .. skipped .. " skipped, " .. restored ..
		" fields of executed instructions put back)\n")
end

function __LPH_AFTER(ok, message)
	if showGlobals then
		ewrite("[globals] script " .. (ok and "returned" or "stopped: " .. tostring(message)) .. "\n")
		for k, v in pairs(sandbox) do
			if baseGlobals[k] ~= v then ewrite("[globals] " .. tostring(k) .. " = " .. type(v) .. " " .. brief(v) .. "\n") end
		end
	end
	if exercise and not aborted then
		local okx, errx = pcall(exercisePayload)
		if not okx then ewrite("[exercise] stopped: " .. tostring(errx) .. "\n") end
	end
	if pokePath and not aborted then
		local okx, errx = pcall(pokePass)
		if not okx then ewrite("[poke] stopped: " .. tostring(errx) .. "\n") end
	end
	if oracleOn then
		if not ok and showErrors and mock.recentStubs then
			local names = {}
			for j = 0, 7 do names[#names + 1] = mock.recentStubs[(mock.recentN + j) % 8 + 1] end
			ewrite("[stubs] last permissive stubs: " .. table.concat(names, ", ") .. "\n")
		end
		ewrite("[oracle] " .. (ok and "script returned" or "script stopped: " .. tostring(string.match(tostring(message), "^[^\n]*"))) ..
			" after " .. stepCount .. " instructions\n")
		writeOracle()
		if not dumpEnd then return end
	end
	if dumpEnd and not done then
		ewrite("[end] " .. (ok and "script returned" or "script stopped: " .. tostring(string.match(tostring(message), "^[^\n]*"))) ..
			" after " .. __LPH_N.c .. " closure entries\n")
		finish({ kind = "end", vars = { __pid = 1 } })
		return
	end
	if not ok and not aborted then
		ewrite(tostring(message) .. "\n")
		ewrite("[end] error after " .. math.max(stepCount, __LPH_N.s) .. " instructions, " .. __LPH_N.c .. " closure entries\n")
	end
	if traceMode then
		if ok then ewrite("[done] script returned normally\n") end
		return
	end
	if not done then
		ewrite("NOT REACHED: the script ended without entering the VM hook\n")
		error("hook not reached")
	end
end
if __LPH_RUNMAIN then
	-- depth=<n>: run the script n C calls deeper (a ModuleScript inside n nested requires)
	for _ = 1, extraDepth do
		local inner = fn
		fn = function(...) return coroutine.wrap(inner)(...) end
	end
	return fn
end
local ok, message = true, ""
fn()
__LPH_AFTER(true, "")
