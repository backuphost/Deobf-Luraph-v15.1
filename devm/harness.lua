-- Luraph devirtualizer: runtime dump harness (runs under Luau CLI).
-- usage: luau harness.lua <patched_sample.lua> <out.json>
local samplePath, outPath = ...
local _G = getfenv() -- the CLI runs scripts in a sandboxed thread: script globals live here, not in the real _G

-- ---------------------------------------------------------------- the engine's libraries, kept apart
-- The script shares the engine's library tables (string, table, math, ...; string is also what the
-- string metatable's __index is).  The engine freezes them, but in executor mode the script has the
-- executor's setreadonly / make_writeable (as on a real executor) and can unfreeze them and replace
-- their functions.  So this file never calls through them once the script can run: the names below
-- are private copies, taken here, before the mock or the script are loaded, and never handed out.
-- rbxmock.lua runs with the same copies (mockEnv).  Whatever a script does to the shared tables
-- changes neither what the harness and the mock write nor what they compute.  sharedLib keeps the
-- shared tables themselves: the copies the script is given are made from them.
local LIB_NAMES = { "string", "table", "math", "bit32", "buffer", "coroutine", "os", "utf8", "debug" }
local sharedLib, privateLibs = {}, {}
local string, table, math, bit32, buffer, coroutine, os, utf8, debug
do
	local clone, freeze = _G.table.clone, _G.table.freeze
	for _, n in ipairs(LIB_NAMES) do
		sharedLib[n] = _G[n]
		privateLibs[n] = freeze(clone(_G[n]))
	end
	local P = privateLibs
	string, table, math, bit32, buffer = P.string, P.table, P.math, P.bit32, P.buffer
	coroutine, os, utf8, debug = P.coroutine, P.os, P.utf8, P.debug
end
local tostring, type, select, pcall, unpack, error = tostring, type, select, pcall, unpack, error

-- ---------------------------------------------------------------- the CLI's own functions
-- The CLI (luau_cli.patch) gives this file real file access (readfile, writefile), its stderr
-- (ewrite, and __LPH_RUNMAIN, which is the same C function), C closures (cwrap), a scheduler
-- resume (resumetop), the vector metatable (setvectormetatable), executor natives (__LPH_NATIVE)
-- and the CLI's module loader (require, which reads and runs files from disk).  They are kept in
-- locals here and taken out of every global table once the mock is built (hideHostGlobals): the
-- script's world never sees them.  In executor mode the script gets the mock's stand-ins for
-- readfile / writefile; in plain-client mode they are nil, as in Roblox.
local readfile, writefile, cwrap = readfile, writefile, cwrap
local hostRunmain, hostNative = __LPH_RUNMAIN, __LPH_NATIVE
local HOST_NAMES = { "readfile", "writefile", "ewrite", "cwrap", "resumetop", "setvectormetatable", "__LPH_RUNMAIN",
	"__LPH_NATIVE", "require", "loadfile", "dofile", "callgrind" }
local realGlobals = getmetatable(_G) and rawget(getmetatable(_G), "__index")
if type(realGlobals) ~= "table" then realGlobals = nil end
-- Everything this file writes to stderr goes through ewrite below.  The Python side reads that log
-- line by line and trusts lines by their leading tag ("[oracle] script returned ...", "[http] ...").
-- Text that comes from the script (an URL, a member name, an error message, a chunk name in a
-- stack trace) is part of the line the harness or the mock started, never a line of its own:
-- characters other readers take for a line end become "?", and a line after the first one of a
-- write that would read as a tagged log line (or as NOT REACHED / COMPILE ERROR) is indented by
-- one space.  (Stack frames, "[C] ..." and "[string "..."]:n ...", are left as they are.)
local ewrite = ewrite      -- (the CLI's: replaced by the checked writer below)
do
	-- (only the private copies above: nothing the script can reach or replace)
	local hostEwrite = ewrite
	local find, gsub = string.find, string.gsub
	local function tagged(line)
		if find(line, "^%[") then
			return not (find(line, "^%[C%]") or find(line, '^%[string "'))
		end
		return find(line, "^NOT REACHED") ~= nil or find(line, "^COMPILE ERROR") ~= nil
			or find(line, "^LAYER COMPILE ERROR") ~= nil
	end
	local function clean(s, all)
		s = gsub(s, "[%z\1-\8\11-\31\127]", "?")
		s = gsub(s, "\226\128[\168\169]", "?")      -- U+2028, U+2029
		s = gsub(s, "\194\133", "?")                -- U+0085
		if all and tagged(s) then s = " " .. s end
		if find(s, "\n", 1, true) then
			s = gsub(s, "\n([^\n]+)", function(line)
				if tagged(line) then return "\n " .. line end
				return nil
			end)
		end
		return s
	end
	-- all=true: text that is the script's from its first character (an error message), so its
	-- first line is treated like the others
	ewrite = function(s, all) return hostEwrite(clean(tostring(s), all)) end
end
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
-- Chunk names (as debug.info "s" gives them) of the script's own code: the file that is run and
-- whatever it loads with loadstring.  modelSrc is the one the Python side has a model of (the
-- functions of that chunk carry the ids and lines the dump refers to).
local scriptSrc = { sample = true }
local modelSrc = "sample"
local function isScriptSrc(src)
	return scriptSrc[src] == true or string.sub(src, 1, 5) == "layer"
end
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
		if debug.info(v, "s") ~= modelSrc then return '{"o":"function"}' end
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

local function tableJson(tb)
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
	return '{"arr":[' .. table.concat(arr, ",") .. '],"hash":[' .. table.concat(hash, ",") .. '],"mt":' .. mts .. "}"
end

local function dumpTables()
	local i = 1
	emit('"tables":{')
	local first = true
	while i <= #tblList do
		emit((first and "" or ",") .. '"' .. i .. '":' .. tableJson(tblList[i]))
		first = false
		i += 1
	end
	emit("}")
end

-- ---------------------------------------------------------------- hooks
local protos = {}
local done = false
local lazyInsn = false       -- lazyinsn=1, see forceLazy
local interpFid = {}         -- interp=<function id> (one per interpreter of the VM), see findConstFns
local quietSteps = 0
local quietHooks = false     -- VM code run by the harness itself (reading constant pools) is not traced

-- Constant tables can carry an __index hook that decrypts entries on first use; touch every
-- pending entry so the dump only ever sees plain values.
local lazyForced, lazyStuck = 0, 0
local forcing = false        -- true while forceLazy reads: __LPH_W / __LPH_OVER then stop a read that does not end
local function readField(t, k)
	return t[k]
end
local function forceLazy(root)
	local seen, queue = {}, { root }
	local qi = 1
	local N = rawget(_G, "__LPH_N")
	local wasQ, wasB = N and N.q, N and N.b
	forcing = true
	while qi <= #queue do
		local t = queue[qi]
		qi += 1
		local mt = getmetatable(t)
		if type(mt) == "table" and type(rawget(mt, "__index")) == "function" and type(rawget(t, 0)) == "table" then
			local keys = {}
			for k in next, rawget(t, 0) do keys[#keys + 1] = k end
			for _, k in ipairs(keys) do
				-- The hook is code of the script's loader (a decoder loop) or VM code.  Called for a
				-- key it does not expect it may never return: loops of the loader are counted while
				-- N.q is set (__LPH_W), VM instructions against a budget of their own (__LPH_OVER).
				quietSteps = 0
				if N then N.q = true; N.b = N.s + 2000000 end
				local ok, err = pcall(readField, t, k)
				lazyForced += 1
				if not ok and err == "__LPH_POOL_BUDGET" then
					lazyStuck += 1
					break
				end
			end
		elseif lazyInsn and type(mt) == "table" and type(rawget(mt, "__index")) == "function" then
			-- lazyinsn=1 (one table per instruction, Luraph v14.0 - v14.2): the instruction table
			-- itself carries the hook; a constant not decoded yet is a nil field among its 7 or 8.
			-- Only tables that look like an instruction (nothing but small array fields) are read.
			local small = true
			for k in next, t do
				if type(k) ~= "number" or k < 0 or k > 9 or k ~= math.floor(k) then small = false break end
			end
			if small then
				for k = 1, 8 do
					if rawget(t, k) == nil then pcall(function() return t[k] end) end
				end
			end
		end
		for k, v in next, t do
			if type(v) == "table" and not seen[v] and not libnames[v] and v ~= envTable then
				seen[v] = true
				queue[#queue + 1] = v
			end
		end
	end
	forcing = false
	if N then N.q, N.b = wasQ, wasB end
end

-- Constant pools.  In some builds a proto's constant table is {[-1] = pool, [-2] = index array} with
-- an __index metamethod (itself VM code) that decodes pool[index[pc]] from the bytecode the first
-- time an instruction asks for it.  The index is a position in the bytecode, so only the exact
-- values the instructions carry can be decoded, and instructions that never ran still carry
-- encrypted ones.  With serve=<dir> the harness therefore stays alive after the dump and answers
-- the lifter, which knows the decrypted index:
--     <dir>/req :  "<seq> <table id in the dump> <index> <protos known> <tables known>"      <dir>/resp :  "<seq>\n<value>\nEND"
local serveDir = nil
local function resolvePath(protos, path)
	-- __LPH_PROTOS[36]["vars"]["r"][5]  (the paths the lifter gives dumped objects)
	local n, rest = string.match(path, "^__LPH_PROTOS%[(%d+)%](.*)$")
	local obj = n and protos[tonumber(n)]
	while obj ~= nil and rest ~= "" do
		local key, r2 = string.match(rest, '^%["([^"]*)"%](.*)$')
		if not key then
			key, r2 = string.match(rest, "^%[(%-?%d+)%](.*)$")
			key = tonumber(key)
		end
		if key == nil or type(obj) ~= "table" then return nil end
		obj, rest = rawget(obj, key), r2
	end
	return obj
end
local function servePools(protos)
	-- A constant can be a function of the script (the closure maker of a nested function): asking
	-- for it makes the loader build that function, which is then a new proto.  New protos and
	-- the tables they bring are sent along with the answer.
	-- Decoding can change the loader's own state (it decrypts bytecode and offset tables in
	-- place).  Every request must start from the state the run ended in, so the buffers and
	-- tables of numbers in the proto records are copied once and put back after each request.
	local keepBufs, keepTabs, seenT = {}, {}, {}
	local function keep(v, depth)
		if type(v) == "buffer" then
			if not seenT[v] then
				seenT[v] = true
				local c = buffer.create(buffer.len(v))
				buffer.copy(c, 0, v)
				keepBufs[#keepBufs + 1] = { v, c }
			end
		elseif type(v) == "table" and not seenT[v] and not libnames[v] and v ~= envTable then
			seenT[v] = true
			local copy, n = {}, 0
			for k, x in next, v do
				if type(x) == "number" and type(k) == "number" then
					copy[k] = x
					n += 1
				end
			end
			if n > 0 and n <= 200000 then keepTabs[#keepTabs + 1] = { v, copy } end
			if depth < 2 then
				for _, x in next, v do keep(x, depth + 1) end
			end
		end
	end
	for _, p in ipairs(protos) do
		if type(p.vars) == "table" then
			for _, v in next, p.vars do keep(v, 0) end
		end
	end
	local function restore()
		for _, e in ipairs(keepBufs) do buffer.copy(e[1], 0, e[2]) end
		for _, e in ipairs(keepTabs) do
			local t, copy = e[1], e[2]
			for k, x in next, t do
				if type(x) == "number" and type(k) == "number" and copy[k] ~= x then
					rawset(t, k, copy[k])
				end
			end
			for k, x in copy do
				if rawget(t, k) ~= x then rawset(t, k, x) end
			end
		end
	end
	writefile(serveDir .. "/ready", "1")
	local K = 1073741823
	local last = nil
	while true do
		local okr, req = pcall(readfile, serveDir .. "/req")
		if okr and type(req) == "string" and req ~= last and string.sub(req, -1) == "\n" then
			last = req
			local seq, id, index, haveP, haveT, atPc = string.match(req, "^(%d+) (%d+) (%-?%d+) (%d+) (%d+) ?(%-?%d*)\n")
			if seq == nil then
				if string.match(req, "^quit") then return end
			else
				local t = tblList[tonumber(id)]
				local out = "null"
				local why = "not a constant table"
				local npatch = 0
				local tdiff = {}
				if type(t) == "table" and type(rawget(t, -2)) == "table" then
					local idx = rawget(t, -2)
					-- (the decoder can mix the instruction's own position into what it reads: ask
					-- at the position the lifter is at, when it says so)
					local K = tonumber(atPc) or K
					local saved = rawget(idx, K)
					local savedV = rawget(t, K)
					rawset(t, K, nil)
					rawset(idx, K, tonumber(index))
					-- "P <path> <offset> <hex>": the lifter's view of a bytecode buffer on this path
					local undo = {}
					for path, off, hx in string.gmatch(req, "\nP (%S+) (%d+) (%x+)") do
						local b = resolvePath(protos, path)
						off = tonumber(off)
						if type(b) == "buffer" and off + #hx / 2 <= buffer.len(b) then
							local raw = string.gsub(hx, "%x%x", function(h) return string.char(tonumber(h, 16)) end)
							undo[#undo + 1] = { b, off, buffer.readstring(b, off, #raw) }
							buffer.writestring(b, off, raw)
							npatch += 1
						end
					end
					-- "T <path> <key> <number>": an entry of a shared table as this path has left it
					local tundo = {}
					for path, key, val in string.gmatch(req, "\nT (%S+) (%-?%d+) (%S+)") do
						local tb = resolvePath(protos, path)
						if type(tb) == "table" then
							key = tonumber(key)
							tundo[#tundo + 1] = { tb, key, rawget(tb, key) }
							rawset(tb, key, tonumber(val))
							npatch += 1
						end
					end
					quietSteps = 0
					quietHooks, __LPH_N.q = true, true
					local nprotos0 = #protos
					local ok, v = pcall(function() return t[K] end)
					quietHooks, __LPH_N.q = false, false
					if #protos > nprotos0 then
						-- A function was built: its entries in the loader's shared tables (encrypted
						-- offsets of its constants) are new.  They stay (as created), and the client
						-- is told about them; everything else goes back to the state of the run's end.
						for _, e in ipairs(keepTabs) do
							local tb, copy = e[1], e[2]
							local tid = tblIds[tb]
							local ents = nil
							for k, x in next, tb do
								if type(k) == "number" and type(x) == "number" and copy[k] == nil then
									copy[k] = x
									if tid and k == math.floor(k) and x == x and math.abs(x) ~= math.huge then
										ents = ents or {}
										ents[#ents + 1] = string.format("[%d,%.17g]", k, x)
									end
								end
							end
							if ents then tdiff[#tdiff + 1] = '"' .. tid .. '":[' .. table.concat(ents, ",") .. "]" end
						end
					end
					restore()
					rawset(t, K, savedV)
					rawset(idx, K, saved)
					if ok and v ~= nil and (type(v) ~= "function" or protoOfFn[v] or fnames[v]) then
						out = ser(v)
					else
						why = ok and ("a " .. typeof(v)) or v
						if ok and type(v) == "table" then
							local mt = getmetatable(v)
							local keys = {}
							for k, x in next, v do
								keys[#keys + 1] = tostring(k) .. "=" .. typeof(x)
								if #keys > 14 then break end
							end
							why = why .. " mt=" .. typeof(mt) .. (type(mt) == "table" and (" __call=" .. typeof(rawget(mt, "__call")) .. " __index=" .. typeof(rawget(mt, "__index"))) or "") .. " {" .. table.concat(keys, ",") .. "}"
						end
					end
				end
				if out == "null" then
					ewrite("[serve] no answer for table " .. id .. " index " .. index .. ": " .. tostring(why) .. " (" .. npatch .. " buffer patches applied)\n")
				end
				-- everything the client does not have yet (it says how many protos and tables it knows)
				local np, nt = {}, {}
				for i = tonumber(haveP) + 1, #protos do np[#np + 1] = ser(protos[i]) end
				local j = tonumber(haveT)
				while j < #tblList do
					j += 1
					nt[#nt + 1] = '"' .. j .. '":' .. tableJson(tblList[j])
				end
				writefile(serveDir .. "/resp", seq .. '\n{"v":' .. out .. ',"protos":[' .. table.concat(np, ",") ..
					'],"tables":{' .. table.concat(nt, ",") .. '},"tdiff":{' .. table.concat(tdiff, ",") .. "}}\nEND")
			end
		end
	end
end

-- The bootstrap installs tables of small functions that ignore their arguments and return a fixed
-- number (runtime "key" material for self-modifying code). Record their values so the lifter can
-- fold calls to them.
-- In a native replay of the lifted init these are functions of the layer.  When the VM ran its own
-- init (dumpnew=1) they are closures of the VM: functions made by one of its interpreters
-- (interp=<function id>).  Those are called like a lazy constant is read: VM instructions against
-- a budget, loops of the loader under the watchdog.
local function isKeyFn(v)
	if string.sub(debug.info(v, "s"), 1, 5) == "layer" then return true end
	local id = fidOf[v]
	return id ~= nil and interpFid[id] == true
end
local function callKeyFn(f, ...)
	local N = rawget(_G, "__LPH_N")
	if not N or not fidOf[f] then return pcall(f, ...) end
	local wasQ, wasB, wasForcing = N.q, N.b, forcing
	forcing = true
	quietSteps = 0
	N.q, N.b = true, N.s + 200000
	local r = table.pack(pcall(f, ...))
	forcing = wasForcing
	N.q, N.b = wasQ, wasB
	return unpack(r, 1, r.n)
end
local function findConstFns(root)
	local seen, queue = {}, { root }
	local qi = 1
	while qi <= #queue do
		local t = queue[qi]
		qi += 1
		local n, all = 0, true
		for k, v in next, t do
			if type(v) == "function" and type(k) == "number" and isKeyFn(v) then
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
				local ok1, a, extra = callKeyFn(f, 1, 2, 3, 4)
				local ok2, b = callKeyFn(f, 977, 13, 40961, 7)
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
	local t0 = os.clock()
	forceLazy(root)
	if lazyForced >= 1000 or lazyStuck > 0 then
		-- (large scripts have hundreds of thousands of constants, each decoded by VM code)
		ewrite(string.format("[dump] %d lazy constants read in %.1f s%s\n", lazyForced, os.clock() - t0,
			lazyStuck > 0 and string.format("; %d table(s) with a hook that did not return for a pending key: left as they were", lazyStuck) or ""))
	end
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
-- a run that does not end (see watch.tick): stall=<s>, stallwait=<s>, stallrun=<s>; tamper=0 (no stop at
-- Luraph's tamper response, see watch.entered); beat=<s> (diagnosis)
local watch = { secs = math.huge, wait = math.huge, run = math.huge, beat = nil, tamper = true }
watch.code = setmetatable({}, { __mode = "k" })   -- a proto's own numeric arrays -> its number (codeVersion)
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
-- Code-state versions.  Scripts that decrypt a block of their own instructions before use change
-- their code arrays while they run, so what an instruction's unexecuted successor would have seen
-- is the code as it was when that instruction ran, not as it is at the end.  For every proto the
-- numeric arrays of its record are copied at its first instruction (version 0); at the first
-- execution of each further instruction they are compared with the last copy and the changes
-- recorded as the next version.  The oracle notes each instruction's version ("sv").
local codeState = {}
-- Tables of numbers one level below a record entry (the loader's own state, shared by every proto:
-- e.g. a table of encrypted bytecode offsets that instructions decrypt in place on first use).
-- Their contents when the first proto starts are kept once, as the state to replay from.
local sharedBase, sharedList = {}, {}
local function codeVersion(pid)
	local cs = codeState[pid]
	if not cs then
		local p = protos[pid]
		if not p or type(p.vars) ~= "table" then return 0 end
		cs = { names = {}, tabs = {}, shadow = {}, base = {}, vers = {}, cur = 0 }
		-- The proto's own tables.  A lazy view of one of them (`P = setmetatable({[0] = K}, decode)`:
		-- key 0 holds the proto's own operand array) is not a table of the loader: that array is
		-- followed below under its own name, per proto.
		local direct = {}
		for _, v in next, p.vars do
			if type(v) == "table" then direct[v] = true end
		end
		for name, v in next, p.vars do
			if type(name) == "string" and type(v) == "table" and not libnames[v] and v ~= envTable then
				-- (not a constant table's {[-1] = pool, [-2] = index array}: that array is the
				-- proto's own and is followed as one of its code arrays)
				local own = rawget(v, -1) ~= nil or rawget(v, -2) ~= nil
				for k2, w in next, v do
					if type(w) == "table" and not own and not direct[w] and not sharedBase[w] and (type(k2) == "number" or type(k2) == "string") then
						local copy, n = {}, 0
						for k, x in next, w do
							if type(k) == "number" and type(x) == "number" then
								copy[k] = x
								n += 1
								if n > 2000000 then break end
							end
						end
						sharedBase[w] = copy
						if n > 0 and n <= 2000000 then
							sharedList[#sharedList + 1] = { name = name, key = k2, tbl = w }
						end
					end
				end
				local copy, any = {}, false
				for k, x in next, v do
					if type(k) == "number" and type(x) == "number" then
						copy[k] = x
						any = true
					end
				end
				if any then
					local n = #cs.names + 1
					cs.names[n], cs.tabs[n], cs.shadow[n], cs.base[n] = name, v, copy, table.clone(copy)
					watch.code[v] = pid
				end
			end
		end
		codeState[pid] = cs
		return 0
	end
	local diff
	for i, t in ipairs(cs.tabs) do
		local sh = cs.shadow[i]
		for k, x in next, t do
			if type(x) == "number" and sh[k] ~= x and type(k) == "number" then
				diff = diff or {}
				diff[#diff + 1] = string.format("[%d,%d,%.17g]", i, k, x)
				sh[k] = x
			end
		end
	end
	if diff then
		cs.cur += 1
		cs.vers[cs.cur] = "[" .. table.concat(diff, ",") .. "]"
	end
	return cs.cur
end
local function writeSnaps()
	local parts = {}
	for pid, cs in codeState do
		if cs.cur > 0 then
			local names, bases = {}, {}
			for i, n in ipairs(cs.names) do
				names[i] = '"' .. n .. '"'
				local ent = {}
				for k, x in cs.base[i] do
					if x == x and math.abs(x) ~= math.huge and k == math.floor(k) then ent[#ent + 1] = string.format('"%d":%.17g', k, x) end
				end
				bases[i] = "{" .. table.concat(ent, ",") .. "}"
			end
			parts[#parts + 1] = string.format('"%d":{"names":[%s],"base":[%s],"vers":[%s]}', pid, table.concat(names, ","),
				table.concat(bases, ","), table.concat(cs.vers, ","))
		end
	end
	-- shared tables: only the entries that changed since
	local sh = {}
	for _, e in ipairs(sharedList) do
		local ent = {}
		for k, x in sharedBase[e.tbl] do
			local now = rawget(e.tbl, k)
			if now ~= x and x == x and math.abs(x) ~= math.huge and k == math.floor(k) then
				ent[#ent + 1] = string.format('"%d":%.17g', k, x)
			end
		end
		if #ent > 0 then
			-- "tid": the table's id in the dump that follows (the lifter matches the entry by it:
			-- the same name and key lead to a different table in every proto)
			local tid = string.match(ser(e.tbl), '^{"t":(%d+)}$')
			sh[#sh + 1] = string.format('{"name":"%s","key":%s,%s"base":{%s}}', e.name,
				type(e.key) == "number" and string.format("%d", e.key) or '"' .. e.key .. '"',
				tid and '"tid":' .. tid .. "," or "", table.concat(ent, ","))
		end
	end
	parts[#parts + 1] = '"shared":[' .. table.concat(sh, ",") .. "]"
	writefile(outPath .. ".snaps", "{" .. table.concat(parts, ",\n") .. "}")
end
local function writeOracle()
	if not oracleOn then return end
	oracleOn = false
	writeSnaps()
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
		parts[#parts + 1] = string.format('"%d:%d:%d":{"n":%d,"p":%d,"sv":%d,"r":{%s},"v":{%s},"vers":[%s],"last":%d,"final":"%s","succ":[%s]}', key // 2 ^ 32,
			(key // 2 ^ 24) % 256, key % 2 ^ 24, rec.n, rec.probe and 1 or 0, rec.sv or 0, table.concat(r, ","), table.concat(v, ","), table.concat(vs, ","),
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
--   __LPH_N.w  dumpnew=1: the proto whose entry is the dump point (set by __LPH_MARK)
--   __LPH_N.b  instruction budget of the counting hook (budget=<n>; __LPH_OVER is called beyond it)
--   __LPH_N.l  rounds of the loader's own while / repeat loops (code outside the interpreters: the
--              loader, and the script's functions Luraph keeps as plain Lua)
--   __LPH_N.k  __LPH_L is called once __LPH_N.l is beyond this (the watch on a run that does not end)
__LPH_N = { c = 0, s = 0, f = false, t = 1, q = false, w = nil, b = math.huge, l = 0, k = math.huge }
-- dumpnew=1 (proto-table layout, see Model.patched_source(newproto=True)): the loader's call of the
-- VM's first program, Luraph's own init, goes through __LPH_MARK.  What that program returns is the
-- proto of the script; the interpreter-entry hooks compare their proto with it.
local markSteps = nil
function __LPH_MARK(p, ...)
	if type(p) == "table" then
		__LPH_N.w = p
		markSteps = __LPH_N.s
		ewrite("[handover] the VM's first program returned a proto after " .. __LPH_N.s .. " instructions\n")
	else
		ewrite("[handover] the VM's first program returned " .. typeof(p) .. ", not a proto (" .. __LPH_N.s .. " instructions)\n")
	end
	return p, ...
end
-- dumpnew=1 for a loader that runs a single root program (Luraph v14.9: no first-program call to
-- mark; the root program is Luraph's start-up code, which builds the script's program itself and
-- calls it).  The entry hooks call __LPH_ENTER for every proto that is not in __LPH_T.  At the
-- first entry, the root program's, everything reachable from its proto goes into __LPH_T: that is
-- the root program.  The first proto entered later that is not in it did not exist when the root
-- program started: the script.
local rootSeen = false
function __LPH_ENTER(proto, names, n, ...)
	if quietHooks or type(proto) ~= "table" then return nil end
	if not rootSeen then
		rootSeen = true
		local queue, qi, count = { proto }, 1, 1
		__LPH_T[proto] = true
		while qi <= #queue do
			local t = queue[qi]
			qi += 1
			for k, v in next, t do
				if type(v) == "table" and __LPH_T[v] == nil and not libnames[v] and v ~= envTable then
					__LPH_T[v] = true
					count += 1
					queue[#queue + 1] = v
				end
			end
		end
		ewrite("[root] the VM's root program starts (" .. count .. " tables of program data)\n")
		return nil
	end
	__LPH_N.w = proto
	markSteps = __LPH_N.s
	ewrite("[handover] the root program entered a program that did not exist when it started, after " .. __LPH_N.s .. " instructions\n")
	return __LPH_RUN(names, n, ...)
end
-- the counting hook ran out of budget: the program spins (Luraph's tamper response is an endless
-- loop) or waits
function __LPH_OVER()
	if forcing then error("__LPH_POOL_BUDGET", 0) end
	if aborted then error(ABORT, 0) end
	ewrite("[budget] stopped after " .. __LPH_N.s .. " VM instructions" ..
		(rootSeen and ": the root program entered no program other than its own" or
		__LPH_N.w == nil and ": the VM's first program has not returned" or ": the script's proto was never entered") .. "\n")
	abort()
end
-- proto-table layout, step hook: a number per proto table, given out by the closure maker
local pidOf, pidN = setmetatable({}, { __mode = "k" }), 0
function __LPH_PID(proto)
	if type(proto) ~= "table" then return 0 end
	local id = pidOf[proto]
	if not id then
		pidN += 1
		id = pidN
		pidOf[proto] = id
	end
	return id
end
-- __LPH_N.q: the harness itself is calling into the loader (servePools); __LPH_W is called from every
-- loop of the loader then and stops one that does not end (garbage in, endless decoding)
function __LPH_W()
	quietSteps += 1
	if quietSteps > 20000 then error("__LPH_POOL_BUDGET", 0) end
end
__LPH_S = stubs
__LPH_T = setmetatable({}, { __mode = "k" })   -- protos already seen (dumpnew=1)
__LPH_X = {}
__LPH_R = {}
local RINGN = 4096
local scriptPath = nil
-- A VM chunk that the shipped file only loads at run time (Luraph's compressed container
-- decompresses the chunk and its data buffer, loadstrings the chunk under the name "Luraph" and
-- calls it with the buffer).  The script that runs is then the shipped file itself
-- (carrier=<file>), unchanged; <patched_sample> is the instrumented copy of the chunk and is
-- compiled in place of the original text (chunksrc=<file>) when the script hands that text to
-- loadstring.  So the chunk runs at the call depth, under the chunk name, with the arguments and
-- in front of the surrounding code it has in the real thing.  chunkarg=<file>: for a chunk that
-- was unwrapped earlier, the bytes of its buffer argument (see __LPH_CARRY).
local carrierPath, chunkSrcPath, chunkArgPath = nil, nil, nil
for i = #layers, 1, -1 do
	local sp = string.match(layers[i], "^scriptpath=(.+)$")
	local pk = string.match(layers[i], "^poke=(.+)$")
	local hm = string.match(layers[i], "^httpmap=(.+)$")
	local sv = string.match(layers[i], "^serve=(.+)$")
	local ca = string.match(layers[i], "^carrier=(.+)$")
	local cs = string.match(layers[i], "^chunksrc=(.+)$")
	local cg = string.match(layers[i], "^chunkarg=(.+)$")
	if ca or cs or cg then
		carrierPath, chunkSrcPath, chunkArgPath = ca or carrierPath, cs or chunkSrcPath, cg or chunkArgPath
		table.remove(layers, i)
	elseif sv then
		serveDir = sv
		table.remove(layers, i)
	elseif hm then
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
	elseif k == "lazyinsn" then lazyInsn = tonumber(v) ~= 0
	elseif k == "interp" then interpFid[tonumber(v)] = true
	elseif k == "budget" then __LPH_N.b = tonumber(v)
	elseif k == "stall" then watch.secs = tonumber(v) > 0 and tonumber(v) or math.huge
	elseif k == "stallwait" then watch.wait = tonumber(v) > 0 and tonumber(v) or math.huge
	elseif k == "stallrun" then watch.run = tonumber(v) > 0 and tonumber(v) or math.huge
	elseif k == "beat" then watch.beat = tonumber(v) > 0 and tonumber(v) or nil
	elseif k == "tamper" then watch.tamper = tonumber(v) ~= 0
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

-- ---------------------------------------------------------------- a run that does not end
-- The end-state dump (dumpend=1) is taken when the script ends, and many scripts never do: a key
-- system waits for its key, a GUI script sits in its event loop, `while true do task.wait() end`.
-- Nothing really waits in the mock (a wait of the main thread returns at once, so does an event's
-- :Wait()), so such a script spins.  The traced run (oracle=1 dumpend=1) is therefore watched.
--   progress   a VM instruction runs for the first time or in a new version of its code, or a
--              program is made (__LPH_PROTO)
--   start      the script's own code runs: a program on an interpreter other than the first one
--              (the payload of two-interpreter builds), or the script's first line of output or
--              first request (builds whose script's top level is in Luraph's main function)
--   stall=<s>      after the start: <s> seconds without progress, the script loops over code it
--                  has run before
--   stallwait=<s>  <s> seconds of waiting the mock simulated without progress (task.wait, wait, an
--                  event's :Wait(), which counts one frame for Heartbeat and its kin and 60 s for an
--                  event the mock never fires): a wait loop.  Counted from the beginning of the run:
--                  Luraph's start-up code does not wait in loops
--   stallrun=<s>   <s> seconds after the start, progress or not
-- Before the start, 4 x <stall> seconds without progress end the run without a dump: it spins in
-- Luraph's start-up code (an environment check that failed loops for ever) or in code that shows
-- nothing, and a lift would be the start-up code.  Otherwise the run is stopped like a budget stop
-- (every instruction, loop round and hook raises while the script unwinds) and then ends as one
-- that returned: [stalled] line, exercise pass, oracle, dump, serve mode.  Seconds are processor
-- time of this process (watch.clock), which never sleeps.  beat=<s> prints a [beat] line every <s>
-- seconds (diagnosis: what the run is doing).
-- VM code is watched through the instruction hook; the rest (the loader, and the script's
-- functions Luraph keeps as plain Lua) through its while / repeat loops, which count their rounds
-- in __LPH_N.l and call __LPH_L every watch.TICK rounds (Model.patched_source).
-- (one table: the main chunk is close to Luau's limit of 200 locals)
watch.TICK = 65536
-- Processor time of this process: utime + stime of /proc/self/stat (in 1/100 s, the unit Linux
-- reports there).  Luau's os.clock is a monotonic wall clock, which runs on while the process
-- waits for a processor: on a loaded machine a run would meet its limits after less work.  Where
-- /proc cannot be read, os.clock it is (the [watch] line says which).
function watch.cpu()
	local ok, s = pcall(readfile, "/proc/self/stat")
	local rest = ok and type(s) == "string" and string.match(s, "%) (.*)$")
	if not rest then return nil end
	local f = {}
	for x in string.gmatch(rest, "%S+") do
		f[#f + 1] = x
		if #f >= 13 then break end
	end
	local u, st = tonumber(f[12]), tonumber(f[13])
	return u and st and (u + st) / 100 or nil
end
do
	local last = watch.cpu()
	if last then
		-- (a read that fails later keeps the last value: the clock never goes back)
		watch.clock = function()
			local c = watch.cpu()
			if c and c > last then last = c end
			return last
		end
		watch.clockName = "processor time"
	else
		watch.clock, watch.clockName = os.clock, "wall-clock time (os.clock)"
	end
end
watch.on = oracleOn and dumpEnd
watch.ticking = watch.on or watch.beat ~= nil
watch.progress, watch.seen, watch.distinct = 0, -1, 0
watch.from, watch.how = nil, nil          -- run time at the start, and what showed it
watch.new, watch.virt, watch.steps, watch.loops = watch.clock(), 0, 0, 0   -- since the last progress
watch.waits = {}                          -- kind of wait -> calls since the last progress
watch.stalled, watch.before = nil, false  -- why the run was stopped; before the start?
watch.waitsBefore = 0                     -- waits of the main thread before the start
watch.gapBefore, watch.gapAfter = 0, 0    -- longest stretch without progress before / after the start
watch.ended, watch.exercising = false, false
watch.loopStop = math.huge                -- exercise / poke pass: loop rounds the current call may run
watch.t0, watch.beatNext = watch.clock(), 0
-- the programs the script's output and requests came from (the one that ran the last VM
-- instruction before each): pipeline.py takes the script from that program's interpreter
watch.effects = {}
if watch.ticking then __LPH_N.k = watch.TICK end
do
	local w = watch
	-- effect: output or a request (counted for the program that ran the last VM instruction)
	function w.start(how, effect)
		if w.ended or w.exercising or probing then return end
		-- (once the run is stopped, output and requests raise the stop again, like waits: see w.waited)
		if aborted then error(ABORT, 0) end
		if effect then
			local v = ring[stepCount % ringN]
			local pid = v and v // 2 ^ 32 or 0
			if pid > 0 then w.effects[pid] = (w.effects[pid] or 0) + 1 end
		end
		if w.from or not w.on then return end
		w.from, w.how = w.clock(), how
		w.progress += 1
		-- (waits before this point: Luraph's start-up code is not seen to wait, see w.stop)
		ewrite("[started] the script's own code runs (" .. how .. ") after " .. stepCount .. " VM instructions" ..
			(w.waitsBefore > 0 and " and " .. w.waitsBefore .. " waits of the main thread" or "") .. "\n")
	end
	local function mostWaits()
		local kind, n, total = nil, 0, 0
		for k, c in w.waits do
			total += c
			if c > n then kind, n = k, c end
		end
		return kind, total
	end
	local function what(now)
		local kind, total = mostWaits()
		local steps, loops = stepCount - w.steps, __LPH_N.l - w.loops
		local quiet = string.format("no new code for %d s", now - w.new)
		if kind then
			-- (task.wait(math.huge) waits for ever)
			return string.format("waiting in %s (%s: %d waits, %s of waiting simulated, %d VM instructions)", kind, quiet,
				total, w.virt < 1e12 and string.format("%d s", w.virt) or "an endless time", steps)
		elseif steps >= loops then
			return string.format("in a loop of its VM code (%s: %d VM instructions, none of them new)", quiet, steps)
		end
		return string.format("in a loop outside the VM's code (%s: %d rounds of plain-Lua loops, %d VM instructions)", quiet,
			loops, steps)
	end
	local function beatLine(now)
		local kind, total = mostWaits()
		local last = {}
		for i = math.max(1, stepCount - 11), stepCount do
			local v = ring[i % ringN]
			if v then last[#last + 1] = string.format("%d:%d:%d", v // 2 ^ 32, (v // 2 ^ 24) % 256, v % 2 ^ 24) end
		end
		return string.format("[beat] %.0f s: %d VM instructions (%d distinct), %d programs, %d loop rounds%s; new code %.0f s "
			.. "ago, since then %d instructions, %d loop rounds, %d waits%s, %.1f s of waiting simulated; last instructions: %s\n",
			now - w.t0, stepCount, w.distinct, #protos, __LPH_N.l,
			w.from and string.format(", the script started %.0f s ago (%s)", now - w.from, w.how) or "",
			now - w.new, stepCount - w.steps, __LPH_N.l - w.loops, total, kind and (" (mostly " .. kind .. ")") or "",
			w.virt, table.concat(last, " "))
	end
	-- pid: the program of the instruction the hook is called for (from __LPH_STEP, before the ring
	-- has it); otherwise the last instruction's
	function w.tick(pid)
		local now = w.clock()
		-- (the longest stretch without progress, before and after the start: the [watch] line, which
		-- says how close a run that ended came to the limits)
		local gap = now - w.new
		if w.from then
			if gap > w.gapAfter then w.gapAfter = gap end
		elseif gap > w.gapBefore then
			w.gapBefore = gap
		end
		if w.progress ~= w.seen then
			w.seen = w.progress
			w.new, w.virt, w.steps, w.loops = now, 0, stepCount, __LPH_N.l
			table.clear(w.waits)
		end
		if w.beat and now >= w.beatNext and not w.ended then
			w.beatNext = now + w.beat
			ewrite(beatLine(now))
		end
		if w.stalled or not w.on or w.ended or quietHooks or forcing or probing or w.exercising then return end
		local why
		if w.virt >= w.wait then
			why = what(now)
		elseif w.from then
			if now - w.new >= w.secs then
				why = what(now)
			elseif now - w.from >= w.run then
				why = string.format("still running (the limit for a run after the script starts is %d s; new code ran %d s ago)",
					w.run, now - w.new)
			end
		elseif now - w.new >= 4 * w.secs then
			why = what(now)
			w.before = true
		end
		if why then w.stop(now, why, pid) end
	end
	function w.stop(now, why, pid)
		-- (where the script was: the program it was stopped in, evidence for pipeline.py like the
		-- programs its output came from)
		if not pid then
			local v = ring[stepCount % ringN]
			pid = v and v // 2 ^ 32 or 0
		end
		w.where = pid
		if w.from then
			w.stalled = string.format("the script did not end: it was stopped %d s after %s, %s; code it would "
				.. "load or fetch later is missing", now - w.from, w.how, why)
		elseif w.before then
			w.stalled = string.format("the run did not end and showed nothing of the script (no output, no request, no program "
				.. "on another interpreter): it was stopped after %d s, %s.  It spins in Luraph's start-up code (an environment "
				.. "check that failed loops for ever) or in code that shows nothing", now - w.t0, why)
		else
			-- (a wait loop before anything else of the script showed: Luraph's start-up code does not
			-- wait, so the waiting is the script's own code, run by a program of the first interpreter
			-- -- its top level in Luraph's main function, or a program that function entered.  That
			-- state is lifted, unlike a spin before the start: a script that builds its window without
			-- a word and then sits in its update loop is all in there)
			w.stalled = string.format("the script did not end: it was stopped after %d s, before it showed itself otherwise "
				.. "(no output, no request, no program on another interpreter), %s.  Luraph's start-up code does not wait: the "
				.. "waits are the script's own code, on the first interpreter; code it would load or fetch later is missing",
				now - w.t0, why)
		end
		aborted = true
		__LPH_N.k = -1
		error(ABORT, 0)
	end
	-- Luraph's tamper response: a VM function that is handed the code arrays of another program
	-- (the caller loads its own arrays into registers and passes them on), scrambles them and then
	-- loops for ever, rewriting bytes of the VM's bytecode buffer (README "The tamper response").
	-- Nothing of the script runs after it, and every round damages what the lift reads (code of
	-- functions that never ran, lazily decoded constants): the run is stopped as soon as the
	-- arrays are in its registers (its first instructions copy its arguments there), before it
	-- changes anything.  Checked at every instruction that runs for the first time.
	function w.entered(pid, regs)
		if w.stalled or quietHooks or forcing then return end
		local owner, n = nil, 0
		for i = 0, 20 do
			local t = rawget(regs, i)
			local o = type(t) == "table" and w.code[t]
			if o and o ~= pid and (owner == nil or owner == o) then owner, n = o, n + 1 end
		end
		if n < 2 then return end
		-- (a call of the exercise or probe pass ends here: the code it would scramble is the dump's)
		if w.ended or probing or w.exercising then error("__LPH_BUDGET", 0) end
		if not w.from then w.before = true end
		w.stop(w.clock(), string.format("at the entry of Luraph's tamper response (program %d, handed %d code arrays of program "
			.. "%d: it scrambles them and then loops for ever; stopped before it changed them)", pid, n, owner), pid)
	end
	-- the mock's waits of the main thread (rbxmock.lua M.onWait): `secs` of waiting simulated.  Once
	-- the run is stopped every wait raises the stop again, like the instruction and loop hooks: a
	-- script that catches it (pcall) and waits on in code without a hook (a plain-Lua `for` loop)
	-- would otherwise spin until the last-resort limit
	function w.waited(kind, secs)
		if aborted then error(ABORT, 0) end
		if not w.on or w.ended then return end
		if not w.from then w.waitsBefore += 1 end
		w.tick()
		w.waits[kind] = (w.waits[kind] or 0) + 1
		w.virt += secs
		w.tick()
	end
end
-- every watch.TICK rounds of the loader's while / repeat loops (and every round once the run is
-- stopped, or beyond the loop budget of an exercised call)
function __LPH_L()
	local N = __LPH_N
	if aborted then
		N.k = -1
		error(ABORT, 0)
	end
	if N.l > watch.loopStop then
		N.k = -1
		error("__LPH_BUDGET", 0)
	end
	N.k = math.min(N.l + watch.TICK, watch.loopStop)
	watch.tick()
end
if cwrap and watch.ticking then __LPH_L = cwrap("__LPH_L", __LPH_L) end

local lastRegs, seenErr = nil, {}
function __LPH_STEP(pid, loop, pc, regs, names, ...)
	if quietHooks then
		-- a pool read with an index that is not in the pool can decode garbage for a long time
		quietSteps += 1
		if quietSteps > 20000 then error("__LPH_POOL_BUDGET", 0) end
		return
	end
	if aborted then error(ABORT, 0) end
	stepCount += 1
	if stepCount > budgetAt then error("__LPH_BUDGET", 0) end
	if watch.ticking and stepCount % 1024 == 0 then watch.tick(pidAlias[pid] or pid) end
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
		-- (an instruction that runs for the first time: Luraph's tamper response is new code, and
		-- its arguments reach the registers with its first instructions, see watch.entered)
		if not rec and watch.on and watch.tamper then watch.entered(pid, regs) end
		if not rec then
			if codeState[pid] == nil and protos[pid] and protos[pid].caller == nil then
				-- the program's first instruction: the one before it was its caller's call (pipeline.py
				-- follows these links from the programs the script's output came from to the program
				-- Luraph's start-up code entered)
				local last = ring[(stepCount - 1) % ringN]
				protos[pid].caller = last and last // 2 ^ 32 or 0
			end
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
			rec.sv = codeVersion(pid)
			rec.probe = probing      -- only ever executed by a probe: its register values mean nothing
			orc[key] = rec
			watch.progress += 1
			watch.distinct += 1
		else
			rec.n += 1
			if not probing then rec.probe = false end
			if rec.vers[#rec.vers] ~= ver and #rec.vers < 16 then
				rec.vers[#rec.vers + 1] = ver
				rec.steps[#rec.steps + 1] = stepCount
				watch.progress += 1
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
	if quietHooks then return nil end
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
		-- dumpnew=1 (sample patched with newproto): the hook calls this for the proto __LPH_MARK
		-- recorded only, i.e. when the VM's init has returned and the script's main function starts
		if __LPH_N.w == nil then return nil end
		ewrite("[dumpnew] the script starts at interpreter entry " .. runCount .. " (" .. __LPH_N.s .. " VM instructions, " ..
			tostring(markSteps) .. " of them the init)\n")
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
	watch.progress += 1
	if interp ~= protos[1].interp and not watch.from then
		-- (what showed the start, in the words of pipeline.py's notes, which may call that program a
		-- helper of the build: "the program(s) the run made on other interpreters")
		watch.start("the run made program " .. #protos .. " on another interpreter")
	end
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
-- (the mock writes its log lines through the same checked writer; it reads ewrite, cwrap,
-- resumetop and setvectormetatable from the table given to it while it is being built, and never later)
rawset(_G, "ewrite", ewrite)
-- The mock's code runs with the private library copies (see sharedLib above): its environment
-- reads them first and everything else from this thread's globals, where its global writes go too.
-- Chunks it loads (the API table, the zstd decoder) get the same environment.
local mockEnv = setmetatable({}, { __index = _G, __newindex = _G })
for n, lib in pairs(privateLibs) do rawset(mockEnv, n, lib) end
rawset(mockEnv, "loadstring", function(text, name)
	local f, e = loadstring(text, name)
	if f then setfenv(f, mockEnv) end
	return f, e
end)
local mockChunk = assert(loadstring(readfile(here .. "/rbxmock.lua"), "=rbxmock"))
setfenv(mockChunk, mockEnv)
local mock = mockChunk()(_G, fnames, libnames, { here = here, readfile = readfile })
mockRef = mock
-- Take the CLI's functions out of every global table: this thread's (where the harness and the
-- mock look up their globals) and the engine's own, behind it (read-only: opened for this with
-- the executor native, when the CLI has it).  From here on only the locals above have them.
local function hideHostGlobals()
	for _, n in ipairs(HOST_NAMES) do rawset(_G, n, nil) end
	if realGlobals then
		local setro = hostNative and hostNative.setreadonly
		local frozen = table.isfrozen(realGlobals)
		if frozen and setro then pcall(setro, realGlobals, false) end
		if not table.isfrozen(realGlobals) then
			for _, n in ipairs(HOST_NAMES) do rawset(realGlobals, n, nil) end
			if frozen and setro then pcall(setro, realGlobals, true) end
		end
	end
end
hideHostGlobals()
if executorEnv then mock.priv.executorLibs() end
if _G.__LPH_CALLLOG then mock.priv.live = true end
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
	-- a UDim's Scale is a float32 (rbxmock.lua): written as the shortest decimal that gives it back
	local F32 = buffer.create(4)
	local function scale(x)
		if type(x) ~= "number" or x ~= x or x == math.huge or x == -math.huge then return x end
		for digits = 1, 9 do
			local d = tonumber(string.format("%." .. digits .. "g", x))
			if d then
				buffer.writef32(F32, 0, d)
				if buffer.readf32(F32, 0) == x then return d end
			end
		end
		return x
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
			local a = all(scale(rawget(v, "Scale")), rawget(v, "Offset"))
			return a and "UDim.new(" .. a .. ")"
		elseif tn == "UDim2" then
			local x, y = rawget(v, "X"), rawget(v, "Y")
			if type(x) ~= "table" or type(y) ~= "table" then return nil end
			local a = all(scale(rawget(x, "Scale")), rawget(x, "Offset"), scale(rawget(y, "Scale")), rawget(y, "Offset"))
			return a and "UDim2.new(" .. a .. ")"
		end
		-- a datatype the mock does not model: the constructor call that made it
		local c = mock.ctorCalls and mock.ctorCalls[v]
		-- (only values that cannot change afterwards: a RaycastParams gets its fields set later)
		local immutable = { Rect = true, NumberRange = true, NumberSequence = true, ColorSequence = true, TweenInfo = true,
			Font = true, Region3 = true, Ray = true, NumberSequenceKeypoint = true, ColorSequenceKeypoint = true,
			BrickColor = true, PhysicalProperties = true, Vector3int16 = true, Vector2int16 = true, CFrame = true }
		if c and immutable[string.match(c[1], "^[^.]+")] then
			local out = {}
			for i = 1, c[2].n do
				local a = c[2][i]
				local t
				if type(a) == "number" then t = n(a)
				elseif type(a) == "string" then t = string.format("%q", a)
				elseif type(a) == "boolean" or a == nil then t = tostring(a)
				else t = describeValue(a) end
				if not t then return nil end
				out[i] = t
			end
			return c[1] .. "(" .. table.concat(out, ", ") .. ")"
		end
		return nil
	end
end
-- what the script does that Luraph's start-up code does not (see watch.start), and its waits
mock.onSay = function() watch.start("its first line of output", true) end
mock.onWait = watch.waited
mock.onHttp = function(method, url)
	watch.start("its first request", true)
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
-- Names the script read that nothing defines.  Roblox answers nil for them, and so does the mock:
-- only the datatype libraries Roblox really has are made on demand (a table for every capitalised
-- name let a key-system loader post `SCRIPT_KEY`, a table, as its request body).
local undefinedRead = {}
-- The script's environment is a plain table, as in Roblox: reading an existing global must not run
-- a metamethod (scripts measure the remaining C stack, and a Lua __index handler costs a level).
-- Only names that are not predefined fall through to the lazy Roblox-library fallback.
-- (the mock's own bookkeeping is not part of the script's world: scripts enumerate the global
-- table behind their environment and look at every name they find, and what is in here writes
-- the run's log)
local mockInternal = { onHttp = true, onSay = true, onWait = true, onMfp = true, onMfpExpected = true, mfpDelta = true, mfpCalls = true, ctorCalls = true,
	enumName = true, lib = true, newInstance = true, serverSide = true, stub = true, typeNames = true,
	vectorTypeName = true, mainThread = true, recentStubs = true, recentN = true, placeScript = true, rawMT = true,
	priv = true }
-- What a lookup may take from this thread's globals: the engine's standard library (whatever the
-- engine's global table holds once the CLI's functions are out of it) and the hooks the
-- instrumented copies call (__LPH_*).  Never the harness's own globals (its tables, the run-end
-- hook, the call logger), whatever their names.
local stdNames = {}
for _, n in ipairs({ "assert", "collectgarbage", "error", "gcinfo", "getfenv", "getmetatable", "ipairs", "loadstring",
	"newproxy", "next", "pairs", "pcall", "print", "rawequal", "rawget", "rawlen", "rawset", "select", "setfenv",
	"setmetatable", "tonumber", "tostring", "type", "typeof", "unpack", "xpcall", "_VERSION", "bit32", "buffer",
	"coroutine", "debug", "math", "os", "string", "table", "utf8", "vector" }) do stdNames[n] = true end
if realGlobals then
	for k in pairs(realGlobals) do
		if type(k) == "string" and string.sub(k, 1, 2) ~= "__" then stdNames[k] = true end
	end
end
stdNames._G = nil
for _, n in ipairs(HOST_NAMES) do stdNames[n] = nil end
local HOOK_DENY = { __LPH_RUNMAIN = true, __LPH_NATIVE = true, __LPH_AFTER = true, __LPH_CALLLOG = true, __LPH_CLIKE = true }
local function shareable(k)
	if type(k) ~= "string" then return false end
	if stdNames[k] then return true end
	return string.sub(k, 1, 6) == "__LPH_" and not HOOK_DENY[k]
end
-- `ewrite` as the script sees it: what it is given goes into the run's log as a [script] line,
-- like print (the probe scripts of the tests report through it); never a line of its own.
local scriptEwrite = function(...)
	local okt, text = pcall(tostring, (...))
	text = okt and type(text) == "string" and text or "?"
	text = string.gsub(string.gsub(string.sub(text, 1, 2000), "\n$", ""), "%c", " ")
	mock.priv.note("script", "ewrite: " .. text, true)
	watch.start("its first line of output", true)
end
if cwrap then scriptEwrite = cwrap("ewrite", scriptEwrite) end
local fallback = setmetatable({}, {
	__index = function(t, k)
		if traceMode and string.sub(tostring(k), 1, 5) ~= "__LPH" then ewrite("[global] " .. tostring(k) .. "\n") end
		local v = nil
		if not mockInternal[k] then v = mock[k] end
		if v == nil and k == "ewrite" then v = scriptEwrite end
		if v == nil and shareable(k) then v = _G[k] end
		if v == nil and type(k) == "string" and mock.priv.knownLib(k) then
			v = rbxLib(k)
		end
		if v == nil and type(k) == "string" and string.sub(k, 1, 5) ~= "__LPH" and not undefinedRead[k] then
			undefinedRead[k] = true
			mock.priv.note("script", "undefined global read: " .. string.sub(k, 1, 60))
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
		for k, v in pairs(sharedLib.math) do mathCopy[k] = v end
		libnames[mathCopy] = "math"
		rawset(sandbox, "math", mathCopy)
	end
	-- require: the CLI's own loader reads and runs files from disk.  The script gets Roblox's
	-- require instead: the game's ModuleScripts do not exist here, so a module is a permissive
	-- stand-in (as any member the mock has no answer for), and a string is refused as Roblox does.
	local require
	do
		local function impl(m)
			if type(m) == "string" then error("Attempted to call require with invalid argument(s).", cwrap and 0 or 2) end
			return mock.stub("require(" .. tostring(m) .. ")")
		end
		require = cwrap and cwrap("require", impl) or impl
		fnames[require] = "require"
	end
	-- (the names go in in this order: scripts walk the table behind their environment, and the
	-- order next() gives depends on the order the keys came in)
	for _, n in ipairs({ "assert", "error", "gcinfo", "getmetatable", "next", "newproxy", "print", "rawequal", "rawget",
		"rawset", "rawlen", "select", "setmetatable", "tonumber", "tostring", "type", "typeof", "ipairs", "pairs",
		"pcall", "xpcall", "unpack", "require", "string", "table", "bit32", "buffer", "coroutine", "os",
		"utf8", "debug", "vector", "_VERSION", "loadstring",
		"__LPH_RUN", "__LPH_PROTO", "__LPH_CLOS", "__LPH_REG", "__LPH_F", "__LPH_STEP", "__LPH_C", "__LPH_FIXKEY",
		"__LPH_N", "__LPH_S", "__LPH_X", "__LPH_R", "__LPH_T", "__LPH_MARK", "__LPH_OVER", "__LPH_PID", "__LPH_ENTER",
		"__LPH_CRASH", "__LPH_ENV", "__LPH_MAIN", "__LPH_CARRY" }) do
		local v = _G[n]
		if n == "require" then v = require end
		if v ~= nil then rawset(sandbox, n, v) end
	end
	for k, v in pairs(mock) do
		if type(k) == "string" and not mockInternal[k] then rawset(sandbox, k, v) end
	end
	for _, n in ipairs({ "game", "workspace", "script", "Enum", "Instance", "Vector3", "Vector2", "CFrame", "Color3", "UDim",
		"UDim2", "Rect", "Random", "task", "wait", "spawn", "delay", "tick", "time", "warn", "shared", "_G",
		"TweenInfo", "NumberRange", "NumberSequence", "ColorSequence", "BrickColor", "Ray", "Region3", "Font",
		"Path2DControlPoint", "DateTime", "PhysicalProperties", "Axes", "Faces", "Vector3int16", "Vector2int16",
		"NumberSequenceKeypoint", "ColorSequenceKeypoint", "OverlapParams", "RaycastParams", "SharedTable",
		-- executor-specific globals probed by environment-fingerprinting scripts
		"math_floor_precision" }) do
		if rawget(sandbox, n) == nil then
			local v = n == "_G" and {} or fallback[n]
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
		-- An executor's request is native code: it reads the fields of the options table itself,
		-- without running metamethods, and refuses anything that is not a plain string where it
		-- wants one.  Scripts check exactly that (an options table whose Url / Headers carry
		-- __index, __tostring and __concat traps tells an HTTP spy written in Lua from the real
		-- thing), so the stand-in must not index, convert or concatenate what it is given.
		local function request(...)
			local opts = ...
			if type(opts) ~= "table" then
				error("invalid argument #1 to 'request' (table expected, got " .. (select("#", ...) == 0 and "no value" or typeof(opts)) .. ")", 0)
			end
			local url = rawget(opts, "Url")
			if type(url) ~= "string" then error("invalid 'Url' field in request options (string expected, got " .. typeof(url) .. ")", 0) end
			local method = rawget(opts, "Method")
			if method == nil then method = "GET" end
			if type(method) ~= "string" then error("invalid 'Method' field in request options (string expected, got " .. typeof(method) .. ")", 0) end
			local headers = rawget(opts, "Headers")
			if headers ~= nil then
				if type(headers) ~= "table" then error("invalid 'Headers' field in request options (table expected, got " .. typeof(headers) .. ")", 0) end
				for k, v in next, headers do
					if type(k) ~= "string" or type(v) ~= "string" then error("invalid 'Headers' field in request options (strings expected)", 0) end
				end
			end
			local reqBody = rawget(opts, "Body")
			if reqBody ~= nil and type(reqBody) ~= "string" then error("invalid 'Body' field in request options (string expected, got " .. typeof(reqBody) .. ")", 0) end
			local body = mock.onHttp(method, url)
			return { StatusCode = 200, StatusMessage = "OK", Success = true, Body = body, Headers = {} }
		end
		if cwrap then request = cwrap("request", request) end
		fnames[request] = "request"
		for _, n in ipairs({ "request", "http_request" }) do rawset(globals, n, request) end
		-- other executor functions scripts expect to exist; inert stand-ins
		if rawget(globals, "gethwid") == nil then rawset(globals, "gethwid", function() return "devm-offline-hwid" end) end
		for _, n in ipairs({ "setclipboard", "toclipboard" }) do
			if rawget(globals, n) == nil then rawset(globals, n, function() end) end
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
		-- (the other usual name of identifyexecutor)
		if rawget(globals, "getexecutorname") == nil then rawset(globals, "getexecutorname", rawget(globals, "identifyexecutor")) end
		-- Metatable and read-only access, part of every executor's API (the "UNC" set).  They are
		-- C closures like an executor's own (cwrap), and work on the mock's objects: mock
		-- metatables are locked, the mock keeps the real ones in mock.rawMT.
		do
			local realGetmt, realSetmt = getmetatable, setmetatable
			local roState = setmetatable({}, { __mode = "k" })
			local function argErr(i, name, want, v, n)
				error("invalid argument #" .. i .. " to '" .. name .. "' (" .. want .. " expected, got " ..
					(i > n and "no value" or typeof(v)) .. ")", 0)
			end
			local function rawmt(o)
				local mt = mock.rawMT and mock.rawMT[o]
				if mt ~= nil then return mt end
				local ok, m = pcall(realGetmt, o)
				if ok and type(m) == "table" then return m end
				return nil
			end
			local more = {
				getrawmetatable = function(...)
					if select("#", ...) == 0 then argErr(1, "getrawmetatable", "any", nil, 0) end
					return rawmt((...))
				end,
				setrawmetatable = function(...)
					local n = select("#", ...)
					local o, mt = ...
					if n == 0 then argErr(1, "setrawmetatable", "any", nil, 0) end
					if mt ~= nil and type(mt) ~= "table" then argErr(2, "setrawmetatable", "table", mt, n) end
					if type(o) == "table" then
						local old = rawmt(o)
						local lock = type(old) == "table" and rawget(old, "__metatable") or nil
						local frozen = type(old) == "table" and table.isfrozen(old)
						if lock ~= nil and not frozen then rawset(old, "__metatable", nil) end
						local ok = pcall(realSetmt, o, mt)
						if lock ~= nil and not frozen then rawset(old, "__metatable", lock) end
						if ok and mock.rawMT then mock.rawMT[o] = mt end
					end
					return o
				end,
				setreadonly = function(...)
					local n = select("#", ...)
					local t, ro = ...
					if type(t) ~= "table" then argErr(1, "setreadonly", "table", t, n) end
					roState[t] = ro and true or false
				end,
				isreadonly = function(...)
					local n = select("#", ...)
					local t = ...
					if type(t) ~= "table" then argErr(1, "isreadonly", "table", t, n) end
					if roState[t] ~= nil then return roState[t] end
					-- (Roblox freezes its library tables; here they are plain tables or copies)
					return table.isfrozen(t) or libnames[t] ~= nil
				end,
			}
			-- clonefunction: a new function object that behaves like the given one (scripts test
			-- `clonefunction(f) ~= f`).  Lua closures are cloned by the VM (__LPH_NATIVE); a C
			-- function gets a C trampoline.  Without the native the function itself is returned.
			do
				local nat = hostNative and hostNative.clonefunction
				more.clonefunction = function(...)
					local f = ...
					if type(f) ~= "function" then argErr(1, "clonefunction", "function", f, select("#", ...)) end
					local c = nat and nat(f)
					-- (the VM gives the clone the thread's globals; it has to see its original's)
					if c ~= nil then setfenv(c, getfenv(f)) end
					if c == nil and nat and cwrap then
						c = cwrap(debug.info(f, "n") or "", function(...) return f(...) end)
						if fnames[f] then fnames[c] = fnames[f] end
					end
					if c ~= nil and protoOfFn[f] then protoOfFn[c] = protoOfFn[f] end
					if c ~= nil and fidOf[f] then fidOf[c] = fidOf[f] end
					return c == nil and f or c
				end
				if nat then
					rawset(globals, "clonefunction", nil)       -- (replaces the identity stand-in)
				end
			end
			-- A CLI built with the current luau_cli.patch has them as real C functions
			-- (__LPH_NATIVE): only those see behind a lock the script itself put on a table
			-- (`setmetatable({}, {__metatable = "locked"})`, which scripts use to test the
			-- executor) and can lift table.freeze.  The Lua versions above are the fallback.
			local native = hostNative or {}
			if not hostNative then
				ewrite("[env] this luau binary predates the executor natives of devm/luau_cli.patch (__LPH_NATIVE): " ..
					"getrawmetatable, setrawmetatable, setreadonly and clonefunction are approximations, and a script " ..
					"that tests them (key-system loaders do) stops in its executor check; rebuild the CLI with the patch\n")
			end
			local setro = native.setreadonly or more.setreadonly
			more.make_writeable = function(t) return setro(t, false) end
			more.make_readonly = function(t) return setro(t, true) end
			for n, f in pairs(more) do
				if rawget(globals, n) == nil then
					if native[n] and n ~= "clonefunction" then f = native[n] elseif cwrap then f = cwrap(n, f) end
					fnames[f] = n
					rawset(globals, n, f)
				end
			end
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
	for k, v in pairs(sharedLib.utf8) do u[k] = v end
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
	for k, v in pairs(sharedLib.buffer) do b[k] = v end
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
	return not isScriptSrc(src)
end
local realSetfenv, realGetfenv = setfenv, getfenv
mock.setfenv = function(f, env)
	if foreign(f) then error("'setfenv' cannot change environment of given object", 2) end
	if type(f) == "number" and f > 0 then f += 1 end
	return realSetfenv(f, env)
end
-- (the harness's globals, the mock's environment and the engine's own global table are never handed
-- to the script: they are what a function of the harness, of the mock or a thread the script did
-- not make has)
local function ownEnv(env)
	if env == _G or env == mockEnv or (realGlobals and env == realGlobals) then return sandbox end
	return env
end
mock.getfenv = function(f)
	if foreign(f) then return sandbox end
	if f == nil then f = 1 end
	if f == 0 then return sandbox end
	if type(f) == "number" and f > 0 then
		local ok, env = pcall(realGetfenv, f + 2)
		if not ok then return sandbox end
		return ownEnv(env)
	end
	return ownEnv(realGetfenv(f))
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
	for k, v in pairs(mock.debug or sharedLib.debug) do d[k] = v end
	local function mockLua(src)
		return src ~= "[C]" and not isScriptSrc(src)
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
		return ownEnv(realGetfenv(f))
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
-- The mock has to know the script's main thread: task.wait / signal:Wait() suspend every other
-- thread of the script (rbxmock.lua), and Luraph's start-up code (v15.x) checks that a spawned
-- thread that waits is suspended.  A copy instrumented for a factory VM says so at its first proto
-- (__LPH_PROTO).  Every other text (an unmodified file, a recovered script, a copy with the
-- proto-table hooks, which do not mark the thread) is given a call of __LPH_MAIN in front of its
-- first token: same line, no frame left on the stack, and the answers are the same in every kind of run.
local fn, err
local markMain = not string.find(src, "__LPH_PROTO", 1, true) and not string.find(src, "__LPH_MAIN", 1, true)
if markMain then fn = loadstring("__LPH_MAIN();" .. src, "=sample") end
if not fn then fn, err = loadstring(src, "=sample") end
if not fn then
	ewrite("COMPILE ERROR: " .. tostring(err) .. "\n")
	error("compile")
end
-- loadstring as the script sees it: the chunk gets the script's environment (in Roblox a loaded
-- chunk shares the globals of the thread that loaded it) and the chunk name the script asked for
-- (Luau turns a plain name such as "Luraph" into the source id `[string "Luraph"]`, which is what
-- error positions and debug.info show; without a name the source text itself is the name).
local chunkOrig, chunkPatched, chunkLoads = nil, nil, 0
do
	local realLoad, realInfo = loadstring, debug.info
	local function argText(v, n, i)
		return i > n and "no value" or typeof(v)
	end
	local function impl(...)
		local n = select("#", ...)
		local text, name = ...
		if type(text) == "number" then text = tostring(text) end
		if type(text) ~= "string" then
			error("invalid argument #1 to 'loadstring' (string expected, got " .. argText(text, n, 1) .. ")", 0)
		end
		if type(name) == "number" then name = tostring(name) end
		if name ~= nil and type(name) ~= "string" then
			error("invalid argument #2 to 'loadstring' (string expected, got " .. argText(name, n, 2) .. ")", 0)
		end
		local subst = chunkOrig ~= nil and text == chunkOrig
		local f, e = realLoad(subst and chunkPatched or text, name or text)
		if not f then
			if subst then ewrite("COMPILE ERROR (instrumented chunk): " .. tostring(e) .. "\n") end
			return nil, e
		end
		setfenv(f, envTable or sandbox)
		local short = realInfo(f, "s")
		scriptSrc[short] = true
		if subst then
			modelSrc = short
			chunkLoads += 1
		end
		return f
	end
	local f = cwrap and cwrap("loadstring", impl) or impl
	fnames[f] = "loadstring"
	mock.loadstring = f
end
function __LPH_CARRY()
	-- for the stand-in carrier of a chunk that was unwrapped earlier (run.py --chunk-arg):
	-- the chunk's text and its buffer argument
	return chunkOrig, chunkArgPath and buffer.fromstring(readfile(chunkArgPath)) or nil
end
if carrierPath then
	if not chunkSrcPath then error("carrier= needs chunksrc=") end
	chunkPatched, chunkOrig = src, readfile(chunkSrcPath)
	-- (the carrier starts the chunk in its own thread: the same mark, unless the chunk's hooks set it)
	fn, err = loadstring((markMain and "__LPH_MAIN();" or "") .. readfile(carrierPath), "=sample")
	if not fn then
		ewrite("COMPILE ERROR (carrier): " .. tostring(err) .. "\n")
		error("compile")
	end
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
	-- A function that stops on its stand-in arguments (arithmetic or a comparison on a stub) is
	-- called again with numbers, then strings, then nothing: each real run decrypts more of it
	-- and creates more of its nested functions.
	local kinds = {
		function(j) return mockRef.stub("arg" .. j) end,
		function(j) return j end,
		function(j) return "arg" .. j end,
		function(j) return nil end,
	}
	local t0 = os.clock()
	watch.exercising = true
	while i <= #regList and calls < 200000 and os.clock() - t0 < 900 do
		local fn = regList[i]
		i += 1
		local rec = protos[protoOfFn[fn]]
		if rec and rec.interp ~= boot and not called[fn] then
			called[fn] = true
			calls += 1
			for k, make in ipairs(kinds) do
				local args = {}
				for j = 1, 8 do args[j] = make(j) end
				budgetAt = stepCount + 200000
				-- (and its plain-Lua loops: a function of a script that waits for ever can do so too)
				watch.loopStop = __LPH_N.l + 2000000
				__LPH_N.k = __LPH_N.l + watch.TICK
				local co = coroutine.create(fn)
				local _, okr, errr = pcall(coroutine.resume, co, unpack(args, 1, 8))
				if okr then break end
				if k == 1 and calls <= 400 then ewrite("[exercise] proto " .. protoOfFn[fn] .. ": " .. string.sub(tostring(errr), 1, 160) .. "\n") end
			end
		end
	end
	budgetAt = math.huge
	watch.loopStop, __LPH_N.k = math.huge, math.huge
	watch.exercising = false
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
				watch.loopStop = __LPH_N.l + 100000
				__LPH_N.k = __LPH_N.l + watch.TICK
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
	watch.loopStop, __LPH_N.k = math.huge, math.huge
	probing = false
	ewrite("[poke] started at " .. done .. " instructions that had not run (" .. skipped .. " skipped, " .. restored ..
		" fields of executed instructions put back)\n")
end

local function afterRun(ok, message)
	watch.ended = true
	-- a run the watch stopped (watch.tick) ends from here on like one that returned
	if watch.stalled then aborted = false end
	__LPH_N.k = math.huge
	if mock.priv and mock.priv.flush then mock.priv.flush() end       -- what the script printed, read and was handed (rbxmock.lua)
	if carrierPath and chunkLoads == 0 then
		ewrite("[carrier] the script never handed the expected chunk text to loadstring: nothing was instrumented\n")
	end
	if next(watch.effects) ~= nil then
		local list = {}
		for pid, n in watch.effects do list[#list + 1] = pid end
		table.sort(list)
		for i, pid in ipairs(list) do list[i] = string.format("%d (%d)", pid, watch.effects[pid]) end
		ewrite("[effects] the script's output and requests came from programs " .. table.concat(list, ", ") .. "\n")
	end
	if watch.on then
		-- (how close the run came to the limits of vmmodel.STALL: diagnosis, and the measure the
		-- defaults were checked with)
		local now = watch.clock()
		ewrite(string.format("[watch] %.0f s of " .. watch.clockName .. "; the script %s; longest stretch without new code: %.0f s "
			.. "before the start, %.0f s after it; %.0f s of waiting simulated since the last new code\n", now - watch.t0,
			watch.from and string.format("started at %.0f s (%s)", watch.from - watch.t0, watch.how) or "never showed itself",
			watch.gapBefore, watch.gapAfter, watch.virt))
	end
	-- how the run ended, for the [oracle] and [end] lines
	local how = ok and "script returned" or "script stopped: " .. tostring(string.match(tostring(message), "^[^\n]*"))
	if watch.stalled then
		if (watch.where or 0) > 0 then ewrite("[where] the run was stopped in program " .. watch.where .. "\n") end
		ewrite("[stalled] " .. watch.stalled .. "\n")
		how = "script did not end (stopped by the watch on a run that does not end)"
		if watch.before then
			-- nothing of the script ran that a lift could show: no oracle, no dump (vmmodel says why)
			ewrite("[oracle] " .. how .. " after " .. stepCount .. " instructions\n")
			return
		end
	end
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
		ewrite("[oracle] " .. how .. " after " .. stepCount .. " instructions\n")
		writeOracle()
		if not dumpEnd then return end
	end
	if dumpEnd and not done then
		ewrite("[end] " .. how .. " after " .. __LPH_N.c .. " closure entries\n")
		finish({ kind = "end", vars = { __pid = 1 } })
		if serveDir then servePools(protos) end
		return
	end
	if not ok and not aborted then
		ewrite(tostring(message) .. "\n", true)       -- (the script's text from its first character)
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
-- Called by the CLI once the script's main thread has ended.  The CLI would print an error
-- raised in here to stderr as it is; it goes through the checked writer instead.
function __LPH_AFTER(ok, message)
	local okA, err = pcall(afterRun, ok, message)
	if not okA then
		ewrite(tostring(err) .. "\n", true)
		error("the harness's end-of-run step failed (see the line above)", 0)
	end
end
if hostRunmain then
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
