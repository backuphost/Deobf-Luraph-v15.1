-- devm.diffcheck: recording front end for harness.lua (see diffcheck.py, which starts it).
--
--   luau diffcheck.lua -a <harness.lua> <script> <scratch file> [salt=<n>] [harness options ...]
--
-- Runs <script> through the unchanged harness and mock and writes what the script does to
-- stderr, one line per event, in a form that is the same from run to run:
--
-- ("[dc] " is "[dc<digits>] " when the reader passes nonce=<digits>, see below.)
--   [dc] START                         the harness has built the environment; the script starts
--   [dc] C<US>name<US>arg<US>arg...    a call of a function of the mock (Roblox / executor API)
--   [dc] K<US>name<US>arg<US>arg...    a call of a permissive stand-in (a member the mock does not model)
--   [dc] X<US>name<US>arg<US>arg...    a call of an executor function the harness keeps as a Lua stand-in
--   [dc] S<US>object<US>key<US>value   a property write on a mock object
--   [dc] E<US>name<US>message          that call raised (information; follows its C line)
--   [dc] P<US>name                     the script read an undefined global that is a placeholder of a
--                                      partial result (REG_n, ARG_n, UPVAL_n, __LPH_..., LuraphVM)
--   [dc] G<US>name<US>value            a global the script left behind (at the end, sorted)
--   [dc] END<US>returned | error<US>message
--
-- (<US> is the byte 0x1f.)  Values: strings and numbers by value, tables by content to a bounded
-- depth, functions / instances / other objects by kind plus a serial number in order of first
-- appearance, written as <SOH>n<STX> so that the reader can renumber them; never an address.
--
-- How it observes without touching the harness or the mock: every function and metamethod the mock
-- hands to a script is made by the global `cwrap(name, impl)`.  This file puts its own `cwrap` in
-- front, which wraps `impl`.  The harness's own `calls=1` is not used: its wrapper adds a Lua frame
-- under the implementation, and an implementation that raises with error(msg, 2) then reports the
-- position of that frame ("rbxmock:34: ...") to the script.  Here the implementation runs as the
-- main function of a coroutine of its own, exactly as it does under plain cwrap, so the error
-- messages a script sees are the ones it sees without recording.
--
-- Time and random numbers are made reproducible: os.clock / os.time / tick() read a counter that
-- starts at a value derived from salt= and advances with every reading and every wait, and
-- math.random is seeded from the salt.  Two runs with the same salt see the same values; values
-- that differ between two salts are the ones that depend on time or chance (diffcheck.py masks
-- them).
local harnessPath, samplePath, outPath = ...
local options = { select(4, ...) }
local G = getfenv()
local ewrite, realCwrap = G.ewrite, G.cwrap
local US, SOH, STX = "\31", "\1", "\2"

if not (ewrite and realCwrap and G.__LPH_RUNMAIN and G.readfile) then
	if ewrite then ewrite("[dc] FATAL" .. US .. "this luau binary is not built with devm/luau_cli.patch (cwrap, __LPH_RUNMAIN)\n")
	else print("[dc] FATAL" .. US .. "this luau binary is not built with devm/luau_cli.patch") end
	return
end

local salt = 1
local TAG = "[dc] "
do
	local pass = {}
	for _, o in ipairs(options) do
		local s = string.match(o, "^salt=(%d+)$")
		local n = string.match(o, "^nonce=(%d+)$")
		-- nonce=<digits>: the lines of this log start with "[dc<digits>] ".  The reader picks the
		-- digits per run, so text a script gets into another line (the URL of a request, with a
		-- line break in it) cannot pass for a line of the recorder.
		if s then salt = tonumber(s) elseif n then TAG = "[dc" .. n .. "] " else pass[#pass + 1] = o end
	end
	options = pass
end

-- ---------------------------------------------------------------- reproducible time and chance
local realOs = os
local now = 4096 * salt
local TICK = 1 / 64
local EPOCH = 1700000000 + 1000003 * salt
do
	math.randomseed(7919 * salt + 13)
	local fake = {}
	for k, v in pairs(realOs) do fake[k] = v end
	fake.clock = realCwrap("clock", function()
		now += TICK
		return now
	end)
	fake.time = realCwrap("time", function(...)
		if select("#", ...) > 0 and (...) ~= nil then
			local r = table.pack(pcall(realOs.time, ...))
			if not r[1] then error(r[2], 0) end
			return unpack(r, 2, r.n)
		end
		now += TICK
		return EPOCH + math.floor(now)
	end)
	fake.date = realCwrap("date", function(...)
		local fmt, t = ...
		if t == nil then t = EPOCH + math.floor(now) end
		local r = table.pack(pcall(realOs.date, fmt, t))
		if not r[1] then error(r[2], 0) end
		return unpack(r, 2, r.n)
	end)
	G.os = fake
end

-- ---------------------------------------------------------------- rendering values
local serial = 0
local ids = setmetatable({}, { __mode = "k" })       -- object -> serial number (weak: nothing is kept alive)
local known = {}                                     -- function -> name of the global it is
local mockTypeof = nil                               -- the mock's typeof implementation (not the wrapped one)
local created = setmetatable({}, { __mode = "k" })   -- instances the script made itself (Instance.new, Clone)
local rawTypeof = typeof

local function idOf(v)
	local id = ids[v]
	if not id then
		serial += 1
		id = serial
		ids[v] = id
	end
	return SOH .. id .. STX
end

local function hash(s)
	local h = 2166136261
	local n = #s
	local step = n > 262144 and n // 131072 or 1      -- very long strings: every step-th byte
	for i = 1, n, step do
		h = bit32.bxor(h, string.byte(s, i))
		h = (h * 16777619) % 4294967296
	end
	return string.format("%08x", h)
end

local function quote(s)
	local long = #s > 240
	local head = long and string.sub(s, 1, 160) or s
	head = string.gsub(head, '[%c\128-\255"\\]', function(c)
		if c == '"' or c == "\\" then return "\\" .. c end
		if c == "\n" then return "\\n" end
		return string.format("\\x%02x", string.byte(c))
	end)
	if long then return '"' .. head .. '"...(' .. #s .. " bytes, fnv " .. hash(s) .. ")" end
	return '"' .. head .. '"'
end

local function number(v)
	if v ~= v then return "nan" end
	if v == math.huge then return "inf" end
	if v == -math.huge then return "-inf" end
	if v == math.floor(v) and math.abs(v) < 2 ^ 53 then return string.format("%d", v) end
	return string.format("%.17g", v)
end

local function clean(text)
	-- text a mock object gives for itself: no addresses, one line
	text = string.gsub(text, "0x%x+", "0x?")
	text = string.gsub(text, '[%c\128-\255]', function(c) return string.format("\\x%02x", string.byte(c)) end)
	if #text > 200 then text = string.sub(text, 1, 200) .. "..." end
	return text
end

local MAXDEPTH, MAXITEMS = 3, 24
local render
local function renderTable(t, depth, path)
	if depth >= MAXDEPTH then return "{...}" end
	if path[t] then return "{cycle}" end
	path[t] = true
	local parts, count = {}, 0
	local n = 0
	while rawget(t, n + 1) ~= nil do n += 1 end
	for i = 1, math.min(n, MAXITEMS) do parts[#parts + 1] = render(rawget(t, i), depth + 1, path) end
	if n > MAXITEMS then parts[#parts + 1] = "...+" .. (n - MAXITEMS) end
	local keyed, more = {}, 0
	for k, v in next, t do
		if not (type(k) == "number" and k == math.floor(k) and k >= 1 and k <= n) then
			if #keyed < 4 * MAXITEMS then
				keyed[#keyed + 1] = (type(k) == "string" and string.match(k, "^[%a_][%w_]*$") and k or "[" .. render(k, depth + 1, path) .. "]")
					.. "=" .. render(v, depth + 1, path)
			else
				more += 1
			end
		end
	end
	table.sort(keyed)
	for i = 1, math.min(#keyed, MAXITEMS) do parts[#parts + 1] = keyed[i] end
	if #keyed > MAXITEMS or more > 0 then parts[#parts + 1] = "...+" .. (#keyed - math.min(#keyed, MAXITEMS) + more) end
	path[t] = nil
	local mt = getmetatable(t)
	return "{" .. table.concat(parts, ", ") .. "}" .. (mt ~= nil and "+mt" or "")
end

function render(v, depth, path)
	local t = type(v)
	if t == "nil" or t == "boolean" then return tostring(v) end
	if t == "number" then return number(v) end
	if t == "string" then return quote(v) end
	if t == "function" then
		local name = known[v]
		if name then return "fn<" .. name .. ">" end
		return (debug.info(v, "s") == "[C]" and "cfn" or "lfn") .. idOf(v)
	end
	if t == "thread" then return "thread" .. idOf(v) end
	if t == "buffer" then return "buffer(" .. buffer.len(v) .. " bytes, fnv " .. hash(buffer.tostring(v)) .. ")" end
	-- tables, userdata, vectors: what the mock says they are.  Only the mock's own objects are
	-- asked for their text or indexed; a script's object could run script code from a metamethod.
	local tn = mockTypeof and mockTypeof(v) or rawTypeof(v)
	if tn == "table" then return renderTable(v, depth or 0, path or {}) end
	if tn == "userdata" and t == "userdata" then return "userdata" .. idOf(v) end
	if tn == "Instance" then
		local ok, class = pcall(function() return v.ClassName end)
		local ok2, text = pcall(tostring, v)
		return "Instance<" .. clean(ok and type(class) == "string" and class or "?") .. " " ..
			quote(ok2 and type(text) == "string" and text or "?") .. (created[v] and " new>" or ">") .. idOf(v)
	end
	local ok, text = pcall(tostring, v)
	text = ok and type(text) == "string" and clean(text) or "?"
	if tn == "EnumItem" or tn == "Enum" or tn == "Enums" or t == "vector" then return "<" .. tn .. " " .. text .. ">" end
	if t == "table" and tn ~= "userdata" then return "<" .. tn .. " " .. text .. ">" end       -- datatype values (Vector2, UDim2, ...)
	return "<" .. tn .. " " .. text .. ">" .. idOf(v)
end

-- ---------------------------------------------------------------- recording
local logging, busy = false, false
local function emit(kind, ...)
	local n = select("#", ...)
	local parts = table.create(n + 1)
	parts[1] = TAG .. kind
	for i = 1, n do parts[i + 1] = (select(i, ...)) end
	ewrite(table.concat(parts, US) .. "\n")
end

local function record(kind, name, skip, ...)
	busy = true
	local n = select("#", ...)
	local parts = table.create(n + 2)
	parts[1] = TAG .. kind
	parts[2] = name
	local ok, err = pcall(function(...)
		for i = skip + 1, n do parts[#parts + 1] = render((select(i, ...))) end
	end, ...)
	if not ok then parts[#parts + 1] = "<not rendered: " .. clean(tostring(err)) .. ">" end
	ewrite(table.concat(parts, US) .. "\n")
	busy = false
end

-- names that are not recorded: the harness's own hooks, buffer writes (the VMs' own memory),
-- helpers without effect
local function recorded(name)
	if name == "" or string.sub(name, 1, 6) == "__LPH_" then return false end
	if string.sub(name, 1, 2) == "__" then return name == "__newindex" or name == "__call" end
	if string.match(name, "^write[uif]%d+$") or name == "nfcnormalize" or name == "nfdnormalize" or name == "graphemes" then return false end
	return true
end

-- A partial result names what it could not recover with globals that do not exist (REG_3, ARG_1,
-- UPVAL_2, __LPH_ENV, LuraphVM).  Reading one is reported ([dc] P<US>name): from there on the
-- result is not the script.  Reads of undefined globals go through the harness's fallback __index.
local function placeholder(k)
	return string.sub(k, 1, 6) == "__LPH_" or k == "LuraphVM" or string.match(k, "^ARG_%d+$") ~= nil
		or string.match(k, "^REG_%d+$") ~= nil or string.match(k, "^UPVAL_%d+$") ~= nil
end

local recOf = setmetatable({}, { __mode = "k" })
G.cwrap = function(name, impl, ...)
	if name == "typeof" and not mockTypeof then mockTypeof = impl end
	if name == "__index" and type(impl) == "function" and string.find(debug.info(impl, "s") or "", "harness%.lua$") then
		return realCwrap(name, function(...)
			local _, k = ...
			if logging and type(k) == "string" and placeholder(k) then emit("P", k) end
			local co = coroutine.create(impl)
			local r = table.pack(coroutine.resume(co, ...))
			if r[1] then return unpack(r, 2, r.n) end
			error(r[2], 0)
		end, ...)
	end
	if type(name) ~= "string" or type(impl) ~= "function" or not recorded(name) then return realCwrap(name, impl, ...) end
	local yielder = select("#", ...) > 0 and (...) ~= nil     -- impl(callingThread, ...)
	local skip = yielder and 1 or 0
	local rec = { name = name }
	local isWait = name == "wait"
	local makes = name == "new" or name == "Clone" or name == "clone" or name == "fromExisting"
	local function wrapped(...)
		if isWait then
			local t = select(skip + 1, ...)
			now += (type(t) == "number" and t > 0 and t < 86400) and t or 0.03
		end
		local live = logging and not busy
		if live then
			if name == "__newindex" then
				record("S", "", 0, ...)
			elseif name == "__call" then
				-- a permissive stand-in of the mock, called: its own text is the name of what was called
				local okn, text = pcall(tostring, (...))
				record("K", okn and type(text) == "string" and clean(text) or "?", 1, ...)
			else
				record("C", rec.full or name, skip, ...)
			end
		end
		-- the implementation as the main function of its own coroutine: the same stack as under
		-- plain cwrap (error levels, debug.info), which a wrapper frame under it would not give
		local co = coroutine.create(impl)
		local r = table.pack(coroutine.resume(co, ...))
		if r[1] then
			if coroutine.status(co) ~= "dead" then error("attempt to yield across metamethod/C-call boundary", 0) end
			if makes and logging and type(r[2]) == "userdata" and mockTypeof and mockTypeof(r[2]) == "Instance" then created[r[2]] = true end
			return unpack(r, 2, r.n)
		end
		if live and logging and type(r[2]) == "string" and string.sub(r[2], 1, 6) ~= "__LPH_" then
			emit("E", rec.full or name, quote(r[2]))
		end
		error(r[2], 0)
	end
	local c = realCwrap(name, wrapped, ...)
	recOf[c] = rec
	return c
end

-- ---------------------------------------------------------------- run the harness
-- The mock has to know the script's main thread: a wait in any other thread of the script
-- suspends that thread, as in Roblox, and Luraph's start-up tests exactly that.  devm's
-- instrumented copies tell the harness from their own hooks; a file that runs as it is cannot, so
-- its text gets `__LPH_MAIN();` in front of its first line (line numbers stay as they are).
--
-- The CLI's own readfile / writefile / ewrite / require are globals of the thread the harness runs
-- in, and the harness lets a script's lookup of an undefined global fall through to them (in
-- executor mode its stand-ins for readfile and writefile sit in front; without executor=1 nothing
-- does).  A script must not get at real files or write lines into this log, so here these names
-- only work for the harness, the mock and this file: the nearest Lua function on the calling
-- stack has to be one of theirs.
do
	local realRead, realEwrite, realInfo = G.readfile, ewrite, debug.info
	local mine = realInfo(1, "s")
	local function trusted()
		local level = 3              -- 1 = this function, 2 = the guarded function, 3 = its caller
		while true do
			local src = realInfo(level, "s")
			if src == nil then return false end
			if src ~= "[C]" then return src == harnessPath or src == mine or src == "rbxmock" or src == "zstd" end
			level += 1
		end
	end
	G.readfile = function(path, ...)
		if not trusted() then error("readfile is not available to scripts", 2) end
		local data = realRead(path, ...)
		if path == samplePath and type(data) == "string" then return "__LPH_MAIN(); " .. data end
		return data
	end
	G.writefile = function() error("writefile is not available to scripts", 2) end
	local function guardedWrite(text)
		if trusted() then return realEwrite(text) end
	end
	G.ewrite = guardedWrite
	G.__LPH_RUNMAIN = guardedWrite
	G.require = function() error("Attempted to call require with invalid argument(s).", trusted() and 0 or 2) end
end
local text = G.readfile(harnessPath)
local harness, cerr = loadstring(text, "=" .. harnessPath)
if not harness then
	emit("FATAL", "harness.lua does not compile: " .. clean(tostring(cerr)))
	return
end
local ok, main = pcall(harness, samplePath, outPath, unpack(options))
if not ok or type(main) ~= "function" then
	emit("FATAL", "the harness did not start the script: " .. clean(tostring(main)))
	return
end

local env = getfenv(main)
local envMt = getmetatable(env)
local globals = type(envMt) == "table" and rawget(envMt, "__index")

-- Executor functions the harness gives the script as plain Lua stand-ins (setclipboard, writefile,
-- hookfunction, queue_on_teleport, ...) are not made by cwrap.  They are replaced by a recording
-- Lua function (a Lua function stays a Lua function: iscclosure and the like answer as before).
if type(globals) == "table" then
	local PURE = { getgenv = true, getrenv = true }
	local names = {}
	for k, f in next, globals do
		if type(k) == "string" and type(f) == "function" and not recOf[f] and not PURE[k] and
			string.sub(k, 1, 6) ~= "__LPH_" and debug.info(f, "s") ~= "[C]" then
			names[#names + 1] = k
		end
	end
	-- (one recording function per NAME: the harness uses one empty function for a dozen of these
	-- names, and the log has to say which one the script called)
	for _, k in ipairs(names) do
		local f = rawget(globals, k)
		rawset(globals, k, function(...)
			if logging and not busy then record("X", k, 0, ...) end
			local r = table.pack(pcall(f, ...))
			if r[1] then return unpack(r, 2, r.n) end
			local msg = r[2]
			-- raised with error(msg, 2) (no position yet): the position of the script's call goes in front
			if type(msg) == "string" and string.sub(msg, 1, 6) ~= "__LPH_" and not string.find(msg, "^[^\n]-%.lua:%d+: ")
				and not string.find(msg, "^rbxmock:%d+: ") then
				error(msg, 2)
			end
			error(msg, 0)
		end)
	end
end

-- names: every function reachable as a global or as a field of a global table of the script
local base = {}
do
	local function walk(t, prefix, depth)
		for k, v in next, t do
			if type(k) == "string" then
				if type(v) == "function" then
					local name = prefix .. k
					if not known[v] or name < known[v] then known[v] = name end
					local rec = recOf[v]
					if rec and (not rec.full or name < rec.full) then rec.full = name end
				elseif type(v) == "table" and depth < 1 and k ~= "_G" and k ~= "shared" then
					walk(v, prefix .. k .. ".", depth + 1)
				end
			end
		end
	end
	if type(globals) == "table" then walk(globals, "", 0) end
	if type(env) == "table" then
		walk(env, "", 0)
		for k, v in next, env do base[k] = v end
	end
end

local function leftBehind(t, prefix, skipBase)
	local lines = {}
	if type(t) ~= "table" then return lines end
	for k, v in next, t do
		if not (skipBase and base[k] == v) then
			lines[#lines + 1] = prefix .. (type(k) == "string" and k or "[" .. render(k) .. "]") .. US .. render(v)
		end
	end
	table.sort(lines)
	return lines
end

G.__LPH_AFTER = function(okRun, message)
	logging = false
	busy = true
	pcall(function()
		for _, l in ipairs(leftBehind(env, "", true)) do emit("G", l) end
		for _, l in ipairs(leftBehind(rawget(env, "_G"), "_G.", false)) do emit("G", l) end
		for _, l in ipairs(leftBehind(rawget(env, "shared"), "shared.", false)) do emit("G", l) end
		local getgenv = env.getgenv
		if type(getgenv) == "function" then
			local okg, genv = pcall(getgenv)
			if okg and genv ~= env then
				for _, l in ipairs(leftBehind(genv, "getgenv().", false)) do emit("G", l) end
			end
		end
	end)
	if okRun then
		emit("END", "returned")
	else
		emit("END", "error", quote(string.match(tostring(message), "^[^\n]*") or ""))
	end
end

logging = true
emit("START")
return main
