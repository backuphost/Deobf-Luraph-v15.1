-- Minimal Roblox runtime mock for the devm harness.
-- It only has to be faithful enough for Luraph's bootstrap (environment checks) to run through
-- without throwing or hanging; it is not a general purpose emulator.
-- usage: local globals = mock(realGlobals, fnames, libnames, opts)
--   opts.here      directory of this file (the Roblox API table is read from <here>/../deobf/roblox_api.luau,
--                  the Zstandard decoder from <here>/zstd.lua when a script asks for it)
--   opts.readfile  how to read them (the CLI's readfile)
local rawTypeof, rawType = typeof, type
return function(G, fnames, libnames, opts)
	local M = {}
	opts = opts or {}
	-- What the run's log gets to know about the environment: one line per fact, each fact once.
	--   [env]   something about the mock itself (a missing data file)
	--   [stub]  a permissive stand-in handed out where the mock cannot know Roblox's answer
	--   [script] what the script itself said or asked for (print / warn texts, undefined globals)
	-- [stub] lines and the undefined globals are kept in P.notes and written at the end of the run (harness.lua;
	-- with P.live set, calls=1, as they happen); print / warn texts are written when they are said (now = true).
	-- P: the mock's own bookkeeping, in ONE field so that it stays out of the script's globals (harness.lua).
	local P = { notes = {}, live = false, counts = {}, full = false }
	M.priv = P
	local noted = {}
	local ewrite = G.ewrite
	local function note(tag, text, now)
		local line = "[" .. tag .. "] " .. text
		if noted[line] then
			P.counts[line] += 1
			return
		end
		noted[line] = true
		P.counts[line] = 1
		if #P.notes < 400 then
			P.notes[#P.notes + 1] = line
		elseif now then      -- a script that prints without end: one last line, then silence
			if P.full then return end
			line, P.full = "[script] ... more print / warn lines that are not shown", true
		end
		if ewrite and (tag == "env" or P.live or now) then ewrite(line .. "\n") end
	end
	P.note = note
	-- Every metatable the mock gives an object, by object: mock metatables are locked like the
	-- engine's ("The metatable is locked"), and an executor's getrawmetatable / setrawmetatable
	-- look behind that lock (harness.lua).  Only the mock's own setmetatable calls are recorded.
	local rawMT = setmetatable({}, { __mode = "k" })
	M.rawMT = rawMT
	local realSetmetatable = setmetatable
	local function setmetatable(t, mt)
		rawMT[t] = mt
		return realSetmetatable(t, mt)
	end
	local typeNames = setmetatable({}, { __mode = "k" })

	-- Mock functions stand in for C functions.  They must not cost more C stack than those do
	-- (scripts recurse to within a few calls of the limit), so they are not wrapped in pcall:
	-- argument errors are raised with error(msg, 2), which puts the position of a calling Lua
	-- function in front of the message exactly as luaL_error does.
	--
	-- With a CLI that has cwrap (see luau_cli.patch) every function and metamethod handed to the
	-- script IS a C closure: its Lua implementation runs on a thread of its own, so a call costs
	-- the caller one frame and no C-stack depth, like the builtin it stands for.  Scripts call
	-- whole batteries of builtins with a single frame left on the stack.
	local cwrap = G.cwrap
	local YIELD = cwrap and newproxy() or {}
	local calllog = G.__LPH_CALLLOG
	-- calls=1: the logging frame must not show.  f raises with error(msg, 2) (3 inside m()), which
	-- names no position while f is the thread's only function; called from a Lua wrapper the same
	-- level would name the wrapper ("rbxmock:62: ..." in front of every message a script compares).
	-- Through pcall the level lands on a C function again and the message stays as it is.
	local function logged(name, f)
		if not calllog then return f end
		return function(...)
			calllog(name, ...)
			local r = table.pack(pcall(f, ...))
			if r[1] then return unpack(r, 2, r.n) end
			error(r[2], 0)
		end
	end
	local function named(name, f)
		f = logged(name, f)
		if cwrap then f = cwrap(string.match(name, "[^%.]*$"), f) end
		fnames[f] = name
		return f
	end
	-- implementations that may suspend the caller: f(callerThread, ...) returns YIELD to do so
	local function namedY(name, f)
		local g = logged(name, f)
		if cwrap then
			f = cwrap(string.match(name, "[^%.]*$"), g, YIELD)
		else
			f = function(...)
				local r = table.pack(g(coroutine.running(), ...))
				if r.n == 1 and r[1] == YIELD then return coroutine.yield() end
				return unpack(r, 1, r.n)
			end
		end
		fnames[f] = name
		return f
	end
	-- (one C closure per implementation: __eq only fires when both operands share the metamethod)
	local sealed = setmetatable({}, { __mode = "k" })
	local function seal(mt)
		if cwrap then
			for k, v in pairs(mt) do
				if rawType(v) == "function" and string.sub(k, 1, 2) == "__" and rawTypeof(sealed[v]) ~= "boolean" then
					local c = sealed[v]
					if not c then
						c = cwrap(k, v)
						sealed[v] = c
						sealed[c] = true
					end
					mt[k] = c
				end
			end
			-- methods kept in an __index table
			local methods = rawget(mt, "__index")
			if rawType(methods) == "table" then
				for k, v in pairs(methods) do
					if rawType(v) == "function" and rawTypeof(sealed[v]) ~= "boolean" then
						local c = cwrap(tostring(k), v)
						sealed[c] = true
						methods[k] = c
						if not fnames[c] then fnames[c] = tostring(k) end
					end
				end
			end
		end
		return mt
	end

	-- ------------------------------------------------------------ the Roblox API table
	-- deobf/roblox_api.luau, generated from Roblox's API dump: every enum with its items and values,
	-- and per class its superclass and the names of its methods, events and properties.  With it the
	-- mock answers member lookups the way the engine does (an enum item that does not exist is an
	-- error, not a new item; a method exists on every class that has it and nowhere else).  Without
	-- the file the mock falls back to permissive answers and says so once.
	local API = { loaded = false, enums = {}, enumOrder = {}, classes = {} }
	do
		local text
		if opts.readfile and opts.here then
			local okr, t = pcall(opts.readfile, opts.here .. "/../deobf/roblox_api.luau")
			if okr and rawType(t) == "string" then text = t end
		end
		local tbl
		if text then
			local f = loadstring(text, "=roblox_api")
			local okc, t = pcall(f or error)
			if okc and rawType(t) == "table" and rawType(t.enums) == "string" and rawType(t.classes) == "string" then tbl = t end
		end
		if tbl then
			for ename, items in string.gmatch(tbl.enums, "([%w_]+):([^;]*)") do
				API.enums[ename] = items
				API.enumOrder[#API.enumOrder + 1] = ename
			end
			for cname, super, fns, evs, props in string.gmatch(tbl.classes, "([%w_]+)<([%w_]*)|([^|]*)|([^|]*)|([^;]*)") do
				API.classes[cname] = { super = super ~= "" and super or nil, raw = { fns, evs, props } }
			end
			API.loaded = true
		else
			note("env", "the Roblox API table (deobf/roblox_api.luau) could not be read: enum items and instance members " ..
				"are answered permissively, which Luraph's start-up probes can tell from Roblox")
		end
	end
	-- (members are parsed when a class is first asked about)
	local function classInfo(cls)
		local c = API.classes[cls]
		if c and c.raw then
			c.m, c.e, c.p = {}, {}, {}
			for x in string.gmatch(c.raw[1], "[^,]+") do c.m[x] = true end
			for x in string.gmatch(c.raw[2], "[^,]+") do c.e[x] = true end
			for x in string.gmatch(c.raw[3], "[^,]+") do
				local pname, vt = string.match(x, "^([^:]+):?(.*)$")
				c.p[pname] = vt ~= "" and vt or true
			end
			c.raw = nil
		end
		return c
	end
	-- "method" | "event" | "prop" (plus the property's value type) for a member of the class or of
	-- one it inherits from; nil when the class has no such member or is not in the table
	local function memberKind(cls, k)
		for _ = 1, 32 do
			local c = cls and classInfo(cls)
			if not c then return nil end
			if c.m[k] then return "method" end
			if c.e[k] then return "event" end
			if c.p[k] then return "prop", c.p[k] ~= true and c.p[k] or nil end
			cls = c.super
		end
		return nil
	end
	local function classIsA(cls, name)
		for _ = 1, 32 do
			if cls == name then return true end
			local c = cls and API.classes[cls]
			if not c then return false end
			cls = c.super
		end
		return false
	end
	P.api = API

	-- ------------------------------------------------------------ permissive value stub
	local stubMT = {}
	M.recentStubs, M.recentN = {}, 0     -- names of the last permissive stubs handed out (diagnosis)
	local function stub(name, tname)
		local t = setmetatable({}, stubMT)
		typeNames[t] = tname or "userdata"
		rawset(t, "__name", name)
		M.recentStubs[M.recentN % 8 + 1] = name
		M.recentN += 1
		return t
	end
	stubMT.__index = function(t, k)
		if type(k) == "number" then return nil end
		local name = rawget(t, "__name")
		local v = stub(name .. "." .. tostring(k))
		rawset(t, k, v)
		return v
	end
	stubMT.__call = function(t) return stub(rawget(t, "__name") .. "()") end
	stubMT.__tostring = function(t) return rawget(t, "__name") end
	stubMT.__len = function() return 0 end
	stubMT.__concat = function(a, b) return tostring(a) .. tostring(b) end
	stubMT.__iter = function() return function() return nil end end
	for _, mm in ipairs({ "__add", "__sub", "__mul", "__div", "__mod", "__pow", "__idiv" }) do
		stubMT[mm] = function(a, b) return type(a) == "table" and a or b end
	end
	stubMT.__unm = function(a) return a end
	stubMT.__lt = function() return false end
	stubMT.__le = function() return true end
	seal(stubMT)
	M.stub = stub

	-- ------------------------------------------------------------ signals
	-- Like Roblox, every read of a signal property yields a new wrapper object: two reads are
	-- equal under == (shared __eq) but are different table keys, and so are their method closures.
	local sigCore = setmetatable({}, { __mode = "k" })
	-- events that fire every frame in Roblox (the harness's watch counts a wait on them as one frame)
	local frameSignals = { Heartbeat = true, RenderStepped = true, Stepped = true, PreRender = true, PreSimulation = true,
		PostSimulation = true, PreAnimation = true }
	local function sigEq(a, b) return sigCore[a] ~= nil and sigCore[a] == sigCore[b] end
	local function newSignal(name)
		local conns = {}
		local core = {}
		local methods = {}
		methods.Connect = function(_, fn)
			local c = newproxy(true)
			local cmt = getmetatable(c)
			typeNames[c] = "RBXScriptConnection"
			local state = { Connected = true }
			conns[c] = fn
			state.Disconnect = named("Disconnect", function(self)
				if self ~= c then error("Expected ':' not '.' calling member function Disconnect", 2) end
				state.Connected = false
				conns[c] = nil
			end)
			cmt.__index = function(_, k) return state[k] end
			cmt.__type = "RBXScriptConnection"
			cmt.__tostring = function() return "Connection" end
			cmt.__metatable = "The metatable is locked"
			seal(cmt)
			return c
		end
		methods.connect = methods.Connect
		methods.Once = methods.Connect
		methods.ConnectParallel = methods.Connect
		methods.Wait = true    -- see make(): the only method that can suspend its caller
		local function make()
			local sig = newproxy(true)
			local smt = getmetatable(sig)
			typeNames[sig] = "RBXScriptSignal"
			sigCore[sig] = core
			smt.__index = function(_, k)
				local m = methods[k]
				if m and k == "Wait" then
					-- inside a coroutine of the script the wait really suspends it (nothing ever fires here)
					return namedY(k, function(co, self)
						if sigCore[self] ~= core then error("Expected ':' not '.' calling member function " .. k, 2) end
						if M.mainThread and co ~= M.mainThread then return YIELD end
						-- (the main thread goes on at once: tell the harness's watch on a run that does not
						-- end what was waited for: one frame for the per-frame events, and for any other
						-- event a long time, since nothing here ever fires it)
						if M.onWait then M.onWait(name .. ":Wait()", frameSignals[name] and 1 / 60 or 60) end
						return nil
					end)
				end
				if m then
					-- a fresh closure per read, named and strict about its arguments like the real method
					return named(k, function(self, ...)
						if sigCore[self] ~= core then error("Expected ':' not '.' calling member function " .. k, 2) end
						if k ~= "Wait" and rawType((...)) ~= "function" then
							error("Attempt to connect failed: Passed value is not a function", 2)
						end
						return m(self, ...)
					end)
				end
				return nil
			end
			smt.__eq = sigEq
			smt.__type = "RBXScriptSignal"
			smt.__tostring = function() return "Signal " .. name end
			smt.__metatable = "The metatable is locked"
			seal(smt)
			return sig
		end
		local function fire(...)
			local list = {}
			for _, fn in pairs(conns) do list[#list + 1] = fn end
			for _, fn in ipairs(list) do pcall(fn, ...) end
		end
		return make, fire
	end

	-- ------------------------------------------------------------ instances
	local data = setmetatable({}, { __mode = "k" })
	local methods = {}
	local services = {}
	local newInstance

	local signalProps = {
		Changed = true, ChildAdded = true, ChildRemoved = true, DescendantAdded = true, DescendantRemoving = true,
		AncestryChanged = true, AttributeChanged = true, Destroying = true,
		AncestryChanged = true, Destroying = true, Heartbeat = true, RenderStepped = true, Stepped = true,
		PreRender = true, PreSimulation = true, PostSimulation = true, PlayerAdded = true, PlayerRemoving = true,
		CharacterAdded = true, Touched = true, InputBegan = true, InputEnded = true, InputChanged = true,
		OnClientEvent = true, OnServerEvent = true, Event = true, MouseButton1Click = true, Activated = true,
		Died = true, Close = true, Idled = true, Loaded = true, ChildRemoving = true,
	}

	local function getSignal(d, k)
		local s = d.signals[k]
		if not s then
			local make, fire = newSignal(k)
			s = { make = make, fire = fire }
			d.signals[k] = s
		end
		return s
	end

	local function setParent(inst, parent)
		local d = data[inst]
		if d.destroyed and parent ~= nil then error("The Parent property of " .. d.props.Name .. " is locked", 0) end
		local old = d.props.Parent
		if old == parent then return end
		if old and data[old] then
			local ch = data[old].children
			for i, c in ipairs(ch) do
				if c == inst then table.remove(ch, i) break end
			end
			getSignal(data[old], "ChildRemoved").fire(inst)
		end
		d.props.Parent = parent
		if parent and data[parent] then
			table.insert(data[parent].children, inst)
			getSignal(data[parent], "ChildAdded").fire(inst)
		end
	end

	local propDefaults = {
		FieldOfView = 70, WalkSpeed = 16, JumpPower = 50, JumpHeight = 7.2, Health = 100, MaxHealth = 100,
		HipHeight = 2, Transparency = 0, BackgroundTransparency = 0, TextTransparency = 0, ImageTransparency = 0,
		Rotation = 0, ZIndex = 1, LayoutOrder = 0, TextSize = 14, Volume = 0.5, TimePosition = 0, PlaybackSpeed = 1,
		HoldDuration = 0, MaxActivationDistance = 10, Brightness = 1, Range = 16, Thickness = 1, Reflectance = 0,
		UserId = 1, AccountAge = 365, PlaceId = 1, GameId = 1, JobId = "00000000-0000-0000-0000-000000000000",
		Enabled = true, Visible = true, Active = true, Anchored = false, CanCollide = true, Massless = false,
		Text = "", ActionText = "", ObjectText = "", DisplayName = "Player", Value = 0,
	}
	local vec2Props = { ViewportSize = { 1920, 1080 }, AbsoluteSize = { 100, 100 }, AbsolutePosition = { 0, 0 },
		AbsoluteContentSize = { 100, 100 }, AbsoluteWindowSize = { 100, 100 }, CanvasPosition = { 0, 0 },
		TextBounds = { 50, 14 } }
	-- properties whose value the mock derives on every read (GUI layout results): name -> f(instance)
	local computed = {}
	-- properties with a fixed engine default that depends on the class: class -> { name -> f() }
	local classDefaults = {}
	local function fullName(inst)
		local parts = {}
		local x = inst
		while x and data[x] and data[x].props.ClassName ~= "DataModel" do
			table.insert(parts, 1, tostring(data[x].props.Name))
			x = data[x].props.Parent
		end
		return table.concat(parts, ".")
	end
	-- An engine method the mock has no model for: it exists, insists on an Instance as `self` like
	-- every method, and answers with a permissive stand-in (the run's log names each one used).
	local unmodelledFns = {}
	local function unmodelled(k)
		local f = unmodelledFns[k]
		if not f then
			f = named(k, function(self, ...)
				if not data[self] then error("Expected ':' not '.' calling member function " .. k, 2) end
				local cls = data[self].props.ClassName
				note("stub", cls .. ":" .. k .. "() has no model in the mock: answered with a permissive stand-in")
				return stub(cls .. ":" .. k .. "()")
			end)
			unmodelledFns[k] = f
		end
		return f
	end
	-- (methods executors add to the DataModel; not in Roblox's API table)
	local dataModelExtras = { HttpGet = true, HttpPost = true }
	-- created: made by the script (Instance.new, Clone of such), so the mock knows every child it
	-- has; an instance of the place (services, the player, ...) may have children the mock lacks.
	function newInstance(class, name, created)
		local u = newproxy(true)
		local mt = getmetatable(u)
		local d = { props = { ClassName = class, Name = name or class, Archivable = true }, children = {}, signals = {}, attrs = {},
			created = created or false }
		data[u] = d
		typeNames[u] = "Instance"
		-- (a class the API table does not have is answered as before the table existed)
		local known = API.loaded and API.classes[class] ~= nil
		mt.__index = function(_, k)
			local v = d.props[k]
			if v ~= nil then return v end
			if k == "Parent" then return nil end
			local kind, vtype
			if known then kind, vtype = memberKind(class, k) end
			if kind == "event" or (not known and signalProps[k]) then return getSignal(d, k).make() end
			if kind == "method" or not known or (dataModelExtras[k] and class == "DataModel") then
				v = methods[k]
				if v ~= nil then return v end
				if kind == "method" then return unmodelled(k) end
			end
			if kind == "prop" and type(k) == "string" then
				local f = computed[k]
				if f then
					v = f(u, d)
					if v ~= nil then return v end
				end
				f = classDefaults[k]
				if f then
					v = f(class)
					if v ~= nil then
						d.props[k] = v
						return v
					end
				end
			end
			if kind == nil or kind == "prop" then
				-- a child of that name (a method or an event hides it; so does a property the mock
				-- has a value for, while one it has none for gives way: `game.Workspace`)
				for _, c in ipairs(d.children) do
					if data[c].props.Name == k then return c end
				end
				if class == "DataModel" and type(k) == "string" and services[k] then return services[k] end
			end
			if type(k) ~= "string" then return nil end
			if known and kind == nil and d.created then
				-- the script made this instance itself: what it has is known, and this is not part of it
				error(k .. " is not a valid member of " .. class .. " \"" .. fullName(u) .. "\"", 2)
			end
			-- well-known properties get a value of the right type (scripts do arithmetic on them)
			v = propDefaults[k]
			if v == nil and vec2Props[k] and M.Vector2 then v = M.Vector2.new(vec2Props[k][1], vec2Props[k][2]) end
			if v ~= nil then
				d.props[k] = v
				return v
			end
			-- The mock cannot know: a property whose engine value it has no model for, or a name
			-- that is no member of the class and would be a child in the real place.  A permissive
			-- stand-in keeps the script going; the run's log lists every one handed out.
			if kind == "prop" then
				note("stub", class .. "." .. k .. ": property" .. (vtype and " (" .. vtype .. ")" or "") .. " without a value in the mock")
			elseif known then
				note("stub", fullName(u) .. "." .. k .. ": not a member of " .. class .. "; taken for a child the real place would have")
			else
				note("stub", class .. "." .. k .. ": the class is not in the API table")
			end
			v = stub(d.props.Name .. "." .. k)
			d.props[k] = v
			return v
		end
		mt.__newindex = function(_, k, v)
			if k == "Parent" then
				setParent(u, v)
			elseif k == "Name" then
				-- a name is a string of at most 100 characters: the engine cuts a longer one (Luraph's
				-- start-up code assigns a long name and counts what comes back)
				if rawType(v) == "number" then v = tostring(v) end
				if rawType(v) == "string" and #v > 100 then v = string.sub(v, 1, 100) end
				d.props[k] = v
			else
				d.props[k] = v
			end
			getSignal(d, "Changed").fire(k)
		end
		mt.__tostring = function() return d.props.Name end
		mt.__type = "Instance"
		mt.__metatable = "The metatable is locked"
			seal(mt)
		return u
	end
	M.newInstance = newInstance

	-- Methods insist on an Instance as `self`, like the real ones (scripts call them with other
	-- values on purpose and expect the error).
	local function m(name, f)
		methods[name] = named(name, function(self, ...)
			if not data[self] then
				error("Expected ':' not '.' calling member function " .. name, 2)
			end
			return f(self, ...)
		end)
	end
	m("GetChildren", function(self)
		local out = {}
		for i, c in ipairs(data[self].children) do out[i] = c end
		return out
	end)
	m("GetDescendants", function(self)
		local out = {}
		local function rec(x)
			for _, c in ipairs(data[x].children) do
				out[#out + 1] = c
				rec(c)
			end
		end
		rec(self)
		return out
	end)
	m("FindFirstChild", function(self, name)
		for _, c in ipairs(data[self].children) do
			if data[c].props.Name == name then return c end
		end
		return nil
	end)
	m("FindFirstChildOfClass", function(self, cls)
		for _, c in ipairs(data[self].children) do
			if data[c].props.ClassName == cls then return c end
		end
		return nil
	end)
	methods.FindFirstChildWhichIsA = methods.FindFirstChildOfClass
	m("WaitForChild", function(self, name)
		for _, c in ipairs(data[self].children) do
			if data[c].props.Name == name then return c end
		end
		local c = newInstance("Folder", name)
		setParent(c, self)
		return c
	end)
	m("IsA", function(self, cls)
		local c = data[self].props.ClassName
		if API.loaded and API.classes[c] then return classIsA(c, cls) end
		return c == cls or cls == "Instance"
	end)
	methods.isA = methods.IsA
	local function ancestor(self, pred)
		local p = data[self].props.Parent
		while p and data[p] do
			if pred(p) then return p end
			p = data[p].props.Parent
		end
		return nil
	end
	m("FindFirstAncestor", function(self, name)
		return ancestor(self, function(p) return data[p].props.Name == name end)
	end)
	m("FindFirstAncestorOfClass", function(self, class)
		return ancestor(self, function(p) return data[p].props.ClassName == class end)
	end)
	m("FindFirstAncestorWhichIsA", function(self, class)
		return ancestor(self, function(p) return methods.IsA(p, class) end)
	end)
	m("FindFirstChildWhichIsA", function(self, class, recursive)
		for _, c in ipairs(recursive and methods.GetDescendants(self) or data[self].children) do
			if methods.IsA(c, class) then return c end
		end
		return nil
	end)
	m("FindFirstDescendant", function(self, name)
		for _, c in ipairs(methods.GetDescendants(self)) do
			if data[c].props.Name == name then return c end
		end
		return nil
	end)
	m("IsDescendantOf", function(self, other)
		local p = data[self].props.Parent
		while p do
			if p == other then return true end
			p = data[p] and data[p].props.Parent
		end
		return false
	end)
	m("IsAncestorOf", function(self, other) return methods.IsDescendantOf(other, self) end)
	m("Destroy", function(self)
		local d = data[self]
		setParent(self, nil)
		d.destroyed = true
		for _, c in ipairs({ unpack(d.children) }) do methods.Destroy(c) end
		for _, s in pairs(d.signals) do s.fire = function() end end
	end)
	methods.Remove = methods.Destroy
	methods.destroy, methods.remove = methods.Destroy, methods.Destroy
	m("ClearAllChildren", function(self)
		for _, c in ipairs({ unpack(data[self].children) }) do methods.Destroy(c) end
	end)
	m("Clone", function(self)
		local d = data[self]
		local c = newInstance(d.props.ClassName, d.props.Name, d.created)
		for k, v in pairs(d.props) do
			if k ~= "Parent" then data[c].props[k] = v end
		end
		for k, v in pairs(d.attrs) do data[c].attrs[k] = v end
		if d.tags then data[c].tags = table.clone(d.tags) end
		for _, ch in ipairs(d.children) do setParent(methods.Clone(ch), c) end
		return c
	end)
	m("GetFullName", function(self) return fullName(self) end)
	m("GetPropertyChangedSignal", function(self, prop) return getSignal(data[self], "Changed:" .. tostring(prop)).make() end)
	m("GetAttribute", function(self, k) return data[self].attrs[k] end)
	m("SetAttribute", function(self, k, v) data[self].attrs[k] = v end)
	m("GetAttributes", function(self)
		local o = {}
		for k, v in pairs(data[self].attrs) do o[k] = v end
		return o
	end)
	m("GetAttributeChangedSignal", function(self, k) return getSignal(data[self], "Attr:" .. tostring(k)).make() end)
	-- Tags (Instance:AddTag / HasTag / RemoveTag / GetTags, and the CollectionService methods of the
	-- same names, which take the instance first).  Luraph's v15 start-up code tags a Folder and
	-- asks for the tag back.
	do
		-- (level: how many mock functions are between the engine method and the caller of tagName)
		local function tagName(v, argn, level)
			if v == nil then error("Argument " .. argn .. " missing or nil", level + 1) end
			if rawType(v) == "number" then return tostring(v) end
			if rawType(v) ~= "string" then error("Unable to cast value to std::string", level + 1) end
			return v
		end
		-- (self, tag) for an Instance, (service, instance, tag) for CollectionService
		local function target(self, a, b)
			if data[self].props.ClassName == "CollectionService" then
				if a == nil then error("Argument 1 missing or nil", 4) end
				if not data[a] then error("Unable to cast value to Object", 4) end
				return a, tagName(b, 2, 4)
			end
			return self, tagName(a, 1, 4)
		end
		m("AddTag", function(self, a, b)
			local inst, tag = target(self, a, b)
			local d = data[inst]
			d.tags = d.tags or {}
			if not table.find(d.tags, tag) then table.insert(d.tags, tag) end
		end)
		m("RemoveTag", function(self, a, b)
			local inst, tag = target(self, a, b)
			local i = data[inst].tags and table.find(data[inst].tags, tag)
			if i then table.remove(data[inst].tags, i) end
		end)
		m("HasTag", function(self, a, b)
			local inst, tag = target(self, a, b)
			return data[inst].tags ~= nil and table.find(data[inst].tags, tag) ~= nil
		end)
		m("GetTags", function(self, a)
			local inst = self
			if data[self].props.ClassName == "CollectionService" then
				if a == nil then error("Argument 1 missing or nil", 3) end
				if not data[a] then error("Unable to cast value to Object", 3) end
				inst = a
			end
			return table.clone(data[inst].tags or {})
		end)
	end
	local game
	m("GetService", function(self, name)
		local s = services[name]
		if not s then
			if name == nil then error("Argument 1 missing or nil", 3) end
			if API.loaded and not (rawType(name) == "string" and API.classes[name]) then
				error("'" .. tostring(name) .. "' is not a valid Service name", 3)
			end
			s = newInstance(name, name)
			services[name] = s
			setParent(s, game)
		end
		return s
	end)
	m("FindService", function(self, name)
		if services[name] then return services[name] end
		if API.loaded and not (rawType(name) == "string" and API.classes[name]) then return nil end
		return methods.GetService(self, name)
	end)
	methods.service, methods.getService = methods.GetService, methods.GetService
	m("IsLoaded", function() return true end)
	m("IsStudio", function() return false end)
	-- server=1 in the harness: the script is analysed as server code
	M.serverSide = false
	m("IsClient", function() return not M.serverSide end)
	m("IsServer", function() return M.serverSide end)
	m("IsPropertyModified", function(self, name)
		if rawType(name) ~= "string" then error("Unable to cast " .. rawTypeof(name) .. " to string", 3) end
		return false
	end)
	m("IsRunning", function() return true end)
	m("IsRunMode", function() return false end)
	m("IsEdit", function() return false end)
	m("GetAsync", function() error("HttpService is not allowed to send requests", 0) end)
	m("PostAsync", function() error("HttpService is not allowed to send requests", 0) end)
	m("RequestAsync", function() error("HttpService is not allowed to send requests", 0) end)
	-- game:HttpGet() is a DataModel shorthand used by some executor-bundled scripts.
	-- Return an empty string so the script can continue past the network gate without crashing.
	-- The harness logs every request and can supply a canned reply (option httpreply=<number>):
	-- loaders that mix a server-provided number into their decryption key can then be traced further.
	m("HttpGet", function(_, url)
		if M.onHttp then return M.onHttp("GET", url) end
		return ""
	end)
	m("HttpPost", function(_, url)
		if M.onHttp then return M.onHttp("POST", url) end
		return ""
	end)
	methods.HttpGetAsync, methods.HttpPostAsync = methods.HttpGet, methods.HttpPost
	m("GenerateGUID", function() return "00000000-0000-0000-0000-000000000000" end)
	m("JSONEncode", function() return "{}" end)
	m("JSONDecode", function() return {} end)
	m("GetPlayers", function() return {} end)
	-- EncodingService:DecompressBuffer(buffer, Enum.CompressionAlgorithm.Zstd): Luraph v14.7 keeps its
	-- VM chunk and data Zstandard-compressed and has Roblox unpack them.  The decoder is devm/zstd.lua
	-- (a chunk that returns { decompress = function(b: buffer): buffer }), loaded when first asked for.
	do
		local zstd
		local function decoder()
			if zstd == nil then
				zstd = false
				local okr, text = false, nil
				if opts.readfile and opts.here then okr, text = pcall(opts.readfile, opts.here .. "/zstd.lua") end
				if okr and rawType(text) == "string" then
					local f, e = loadstring(text, "=zstd")
					local okc, lib = pcall(f or error, e)
					if okc and rawType(lib) == "table" and rawType(lib.decompress) == "function" then
						zstd = lib
					else
						note("env", "devm/zstd.lua did not load: " .. tostring(lib))
					end
				end
			end
			return zstd
		end
		m("DecompressBuffer", function(self, buf, algo)
			if rawType(buf) ~= "buffer" then
				error("invalid argument #1 to 'DecompressBuffer' (buffer expected, got " .. rawTypeof(buf) .. ")", 3)
			end
			if algo == nil then error("Argument 2 missing or nil", 3) end
			if typeNames[algo] ~= "EnumItem" or tostring(algo) ~= "Enum.CompressionAlgorithm.Zstd" then
				error("Unable to cast " .. (typeNames[algo] or rawTypeof(algo)) .. " to token", 3)
			end
			local lib = decoder()
			if not lib then
				note("env", "the script unpacks Zstandard data with EncodingService:DecompressBuffer, and the decoder " ..
					"(devm/zstd.lua) is not there: the run stops at this call")
				error("EncodingService:DecompressBuffer: no Zstandard decoder (devm/zstd.lua is missing)", 0)
			end
			local okd, out = pcall(lib.decompress, buf)
			if not okd or rawType(out) ~= "buffer" then
				note("env", "EncodingService:DecompressBuffer: the Zstandard data did not decode (" ..
					string.sub(tostring(out), 1, 120) .. ")")
				error("Failed to decompress buffer", 3)
			end
			return out
		end)
	end
	methods.getPlayers, methods.players = methods.GetPlayers, methods.GetPlayers
	-- GuiService:GetGuiInset() returns TWO Vector2 (API reference: inset from the top left corner
	-- and from the bottom right corner); a one-value stand-in makes `local a, b = ...; b.X` fail.
	-- 58 is the height of the current top bar (36 before the 2023 redesign): from Roblox's
	-- announcement, not measured on a client.  Layout arithmetic of GUI scripts only.
	m("GetGuiInset", function()
		if not M.Vector2 then return stub("GuiService.GetGuiInset()"), stub("GuiService.GetGuiInset()") end
		return M.Vector2.new(0, 58), M.Vector2.new(0, 0)
	end)
	m("BindToClose", function() end)
	m("FireServer", function() end)
	m("FireClient", function() end)
	m("FireAllClients", function() end)
	m("Fire", function() end)
	m("InvokeServer", function() return nil end)
	m("Kick", function() end)

	methods.children, methods.getChildren = methods.GetChildren, methods.GetChildren
	methods.clone, methods.findFirstChild, methods.isDescendantOf = methods.Clone, methods.FindFirstChild, methods.IsDescendantOf

	game = newInstance("DataModel", "Game")
	local workspace = newInstance("Workspace", "Workspace")
	services.Workspace = workspace
	setParent(workspace, game)
	data[game].props.workspace = workspace      -- (the property; `game.Workspace` finds the child)
	data[game].props.PlaceId = 1
	data[game].props.GameId = 1
	data[game].props.JobId = "5c3a1a7e-0b1f-4c2d-9e57-3f6a8d2b4c10"    -- (empty only in Studio; scripts derive keys from it)
	for _, s in ipairs({ "Players", "RunService", "ReplicatedStorage", "ReplicatedFirst", "StarterPlayer", "StarterGui",
		"Lighting", "HttpService", "TweenService", "UserInputService", "CoreGui", "StarterPack", "TeleportService" }) do
		methods.GetService(game, s)
	end
	do
		local sp = services.StarterPlayer
		setParent(newInstance("StarterPlayerScripts", "StarterPlayerScripts"), sp)
		setParent(newInstance("StarterCharacterScripts", "StarterCharacterScripts"), sp)
		local players = services.Players
		local lp = newInstance("Player", "Player")
		data[lp].props.UserId = 1
		setParent(lp, players)
		data[players].props.LocalPlayer = lp
		setParent(newInstance("PlayerGui", "PlayerGui"), lp)
		setParent(newInstance("PlayerScripts", "PlayerScripts"), lp)
		setParent(newInstance("Backpack", "Backpack"), lp)
		setParent(newInstance("Camera", "Camera"), workspace)
		data[workspace].props.CurrentCamera = methods.FindFirstChild(workspace, "Camera")
	end
	local script = newInstance("LocalScript", "Script")
	setParent(script, methods.FindFirstChild(data[services.Players].props.LocalPlayer, "PlayerScripts"))

	-- scriptpath=<Service.Folder...Name>: where the script under analysis sits.  Scripts check
	-- their own GetFullName() and refuse to run elsewhere.
	function M.placeScript(path)
		local parts = string.split(path, ".")
		local parent = methods.GetService(game, parts[1])
		for i = 2, #parts - 1 do
			local child = methods.FindFirstChild(parent, parts[i])
			if not child then
				child = newInstance("Folder", parts[i])
				setParent(child, parent)
			end
			parent = child
		end
		data[script].props.Name = parts[#parts]
		setParent(script, parent)
	end

	M.game = game
	M.Game = game
	M.workspace = workspace
	M.Workspace = workspace
	M.script = script

	-- ------------------------------------------------------------ libraries
	-- Library tables are plain tables, as in Roblox: scripts test getmetatable(Vector2) == nil.
	-- Constructors of datatypes the mock does not model return permissive stubs of that type.
	local known = {
		Color3 = { "new", "fromRGB", "fromHSV", "fromHex" },
		CFrame = { "new" }, Rect = { "new" }, Random = { "new" }, UDim = { "new" }, UDim2 = { "new", "fromScale", "fromOffset" },
		TweenInfo = { "new" }, NumberRange = { "new" }, NumberSequence = { "new" }, ColorSequence = { "new" },
		BrickColor = { "new", "random", "palette", "White", "Gray", "DarkGray", "Black", "Red", "Yellow", "Green", "Blue" },
		Ray = { "new" }, Region3 = { "new" }, Region3int16 = { "new" }, Font = { "new", "fromEnum", "fromName", "fromId" },
		DateTime = { "now", "fromUnixTimestamp", "fromUnixTimestampMillis", "fromUniversalTime", "fromLocalTime", "fromIsoDate" },
		PhysicalProperties = { "new" }, Axes = { "new" }, Faces = { "new" }, Vector3int16 = { "new" }, Vector2int16 = { "new" },
		NumberSequenceKeypoint = { "new" }, ColorSequenceKeypoint = { "new" }, OverlapParams = { "new" }, RaycastParams = { "new" },
		PathWaypoint = { "new" }, CatalogSearchParams = { "new" }, FloatCurveKey = { "new" }, RotationCurveKey = { "new" },
		SharedTable = { "new", "clear", "clone", "cloneAndFreeze", "increment", "isFrozen", "size", "update" },
		Content = { "fromUri", "fromObject", "fromAssetId" }, Secret = {}, ValueCurveKey = { "new" },
		DockWidgetPluginGuiInfo = { "new" },
	}
	local ctorCalls = setmetatable({}, { __mode = "k" })
	M.ctorCalls = ctorCalls
	local function lib(name, tname)
		local l = {}
		libnames[l] = name
		for _, k in ipairs(known[name] or { "new" }) do
			local full = name .. "." .. k
			l[k] = named(full, function(...)
				note("stub", full .. "(): the mock has no model of this datatype; the value is a permissive stand-in")
				local s = stub(full .. "()", tname or name)
				ctorCalls[s] = { full, table.pack(...) }       -- (a constant of this type is dumped as this call)
				return s
			end)
		end
		return l
	end
	M.lib = lib
	-- Roblox's own datatype libraries: the only capitalised globals besides Enum, Instance, Game and
	-- Workspace that exist without the script defining them (harness.lua answers nil for the rest).
	P.knownLib = function(name) return known[name] ~= nil end
	-- executor=1: the drawing and socket libraries executors add (not modelled: permissive stand-ins)
	P.executorLibs = function()
		known.Drawing = { "new", "clear" }
		known.WebSocket = { "connect" }
	end

	-- a plain table like the real one: scripts test getmetatable(Instance) == nil
	local Instance = {}
	libnames[Instance] = "Instance"
	rawset(Instance, "new", named("Instance.new", function(class, parent)
		if class == nil then error("Argument 1 missing or nil", 2) end
		if rawType(class) ~= "string" then error("invalid argument #1 to 'new' (string expected, got " .. rawTypeof(class) .. ")", 2) end
		if API.loaded and not API.classes[class] then error("Unable to create an Instance of type \"" .. class .. "\"", 2) end
		local i = newInstance(class, nil, true)
		if parent then setParent(i, parent) end
		return i
	end))
	rawset(Instance, "fromExisting", named("Instance.fromExisting", function(x) return methods.Clone(x) end))
	M.Instance = Instance

	-- UDim / UDim2 / Vector2 with real arithmetic (Luraph's bootstrap derives key material from
	-- Path2D geometry, so these have to compute, not just exist)
	local UDimMT, UDim2MT, Vec2MT = {}, {}, {}
	local function udim(s, o)
		local t = setmetatable({ Scale = s or 0, Offset = o or 0 }, UDimMT)
		typeNames[t] = "UDim"
		return t
	end
	UDimMT.__add = function(a, b) return udim(a.Scale + b.Scale, a.Offset + b.Offset) end
	UDimMT.__sub = function(a, b) return udim(a.Scale - b.Scale, a.Offset - b.Offset) end
	UDimMT.__unm = function(a) return udim(-a.Scale, -a.Offset) end
	UDimMT.__eq = function(a, b) return a.Scale == b.Scale and a.Offset == b.Offset end
	UDimMT.__tostring = function(a) return a.Scale .. ", " .. a.Offset end
	local function udim2(xs, xo, ys, yo)
		local t = setmetatable({ X = udim(xs, xo), Y = udim(ys, yo) }, UDim2MT)
		t.Width, t.Height = t.X, t.Y
		typeNames[t] = "UDim2"
		return t
	end
	UDim2MT.__add = function(a, b)
		return udim2(a.X.Scale + b.X.Scale, a.X.Offset + b.X.Offset, a.Y.Scale + b.Y.Scale, a.Y.Offset + b.Y.Offset)
	end
	UDim2MT.__sub = function(a, b)
		return udim2(a.X.Scale - b.X.Scale, a.X.Offset - b.X.Offset, a.Y.Scale - b.Y.Scale, a.Y.Offset - b.Y.Offset)
	end
	UDim2MT.__eq = function(a, b) return a.X == b.X and a.Y == b.Y end
	UDim2MT.__tostring = function(a) return "{" .. tostring(a.X) .. "}, {" .. tostring(a.Y) .. "}" end
	UDim2MT.__index = {
		Lerp = function(a, b, t)
			return udim2(a.X.Scale + (b.X.Scale - a.X.Scale) * t, a.X.Offset + (b.X.Offset - a.X.Offset) * t,
				a.Y.Scale + (b.Y.Scale - a.Y.Scale) * t, a.Y.Offset + (b.Y.Offset - a.Y.Offset) * t)
		end,
	}
	local function vec2(x, y)
		local t = setmetatable({ X = x or 0, Y = y or 0 }, Vec2MT)
		t.x, t.y = t.X, t.Y
		t.Magnitude = math.sqrt(t.X * t.X + t.Y * t.Y)
		typeNames[t] = "Vector2"
		return t
	end
	Vec2MT.__add = function(a, b) return vec2(a.X + b.X, a.Y + b.Y) end
	Vec2MT.__sub = function(a, b) return vec2(a.X - b.X, a.Y - b.Y) end
	Vec2MT.__mul = function(a, b)
		if type(a) == "number" then return vec2(a * b.X, a * b.Y) end
		if type(b) == "number" then return vec2(a.X * b, a.Y * b) end
		return vec2(a.X * b.X, a.Y * b.Y)
	end
	Vec2MT.__div = function(a, b)
		if type(b) == "number" then return vec2(a.X / b, a.Y / b) end
		return vec2(a.X / b.X, a.Y / b.Y)
	end
	Vec2MT.__idiv = function(a, b)
		if type(b) == "number" then return vec2(a.X // b, a.Y // b) end
		return vec2(a.X // b.X, a.Y // b.Y)
	end
	local function sign(x) return x > 0 and 1 or (x < 0 and -1 or 0) end
	local vec2Methods = {
		Abs = function(a) return vec2(math.abs(a.X), math.abs(a.Y)) end,
		Ceil = function(a) return vec2(math.ceil(a.X), math.ceil(a.Y)) end,
		Floor = function(a) return vec2(math.floor(a.X), math.floor(a.Y)) end,
		Sign = function(a) return vec2(sign(a.X), sign(a.Y)) end,
		Dot = function(a, b) return a.X * b.X + a.Y * b.Y end,
		Cross = function(a, b) return a.X * b.Y - a.Y * b.X end,
		Angle = function(a, b, signed)
			local ang = math.atan2(a.X * b.Y - a.Y * b.X, a.X * b.X + a.Y * b.Y)
			return signed and ang or math.abs(ang)
		end,
		FuzzyEq = function(a, b, eps)
			eps = eps or 1e-5
			return math.abs(a.X - b.X) <= eps and math.abs(a.Y - b.Y) <= eps
		end,
		Lerp = function(a, b, t) return vec2(a.X + (b.X - a.X) * t, a.Y + (b.Y - a.Y) * t) end,
		Max = function(a, ...)
			local x, y = a.X, a.Y
			for _, b in ipairs({ ... }) do x, y = math.max(x, b.X), math.max(y, b.Y) end
			return vec2(x, y)
		end,
		Min = function(a, ...)
			local x, y = a.X, a.Y
			for _, b in ipairs({ ... }) do x, y = math.min(x, b.X), math.min(y, b.Y) end
			return vec2(x, y)
		end,
	}
	for k, f in pairs(vec2Methods) do vec2Methods[k] = named(k, f) end
	Vec2MT.__index = function(a, k)
		if k == "Unit" then
			local m = rawget(a, "Magnitude")
			return m > 0 and vec2(a.X / m, a.Y / m) or vec2(0 / 0, 0 / 0)
		end
		return vec2Methods[k]
	end
	Vec2MT.__unm = function(a) return vec2(-a.X, -a.Y) end
	Vec2MT.__eq = function(a, b) return a.X == b.X and a.Y == b.Y end
	Vec2MT.__tostring = function(a) return a.X .. ", " .. a.Y end
	seal(UDimMT)
	seal(UDim2MT)
	seal(Vec2MT)
	-- Color3: scripts read the channels, lerp and convert, so it has to be a real value
	do
		local C3MT = {}
		local function color3(r, g, b)
			local t = setmetatable({ R = r or 0, G = g or 0, B = b or 0 }, C3MT)
			t.r, t.g, t.b = t.R, t.G, t.B
			typeNames[t] = "Color3"
			return t
		end
		local function toHSV(c)
			local mx, mn = math.max(c.R, c.G, c.B), math.min(c.R, c.G, c.B)
			local d = mx - mn
			local h = 0
			if d > 0 then
				if mx == c.R then h = ((c.G - c.B) / d) % 6
				elseif mx == c.G then h = (c.B - c.R) / d + 2
				else h = (c.R - c.G) / d + 4 end
				h /= 6
			end
			return h, mx == 0 and 0 or d / mx, mx
		end
		local function fromHSV(h, s, v)
			h, s, v = h or 0, s or 0, v or 0
			local i = math.floor(h * 6)
			local f = h * 6 - i
			local p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
			i %= 6
			if i == 0 then return color3(v, t, p) elseif i == 1 then return color3(q, v, p)
			elseif i == 2 then return color3(p, v, t) elseif i == 3 then return color3(p, q, v)
			elseif i == 4 then return color3(t, p, v) end
			return color3(v, p, q)
		end
		C3MT.__index = {
			Lerp = function(a, b, t) return color3(a.R + (b.R - a.R) * t, a.G + (b.G - a.G) * t, a.B + (b.B - a.B) * t) end,
			ToHSV = toHSV,
			ToHex = function(c)
				return string.format("%02x%02x%02x", math.floor(c.R * 255 + 0.5), math.floor(c.G * 255 + 0.5), math.floor(c.B * 255 + 0.5))
			end,
		}
		C3MT.__eq = function(a, b) return a.R == b.R and a.G == b.G and a.B == b.B end
		C3MT.__tostring = function(c) return c.R .. ", " .. c.G .. ", " .. c.B end
		seal(C3MT)
		local L = lib("Color3")
		rawset(L, "new", named("Color3.new", color3))
		rawset(L, "fromRGB", named("Color3.fromRGB", function(r, g, b) return color3((r or 0) / 255, (g or 0) / 255, (b or 0) / 255) end))
		rawset(L, "fromHSV", named("Color3.fromHSV", fromHSV))
		rawset(L, "toHSV", named("Color3.toHSV", toHSV))
		rawset(L, "fromHex", named("Color3.fromHex", function(hex)
			hex = string.gsub(tostring(hex), "#", "")
			return color3((tonumber(string.sub(hex, 1, 2), 16) or 0) / 255, (tonumber(string.sub(hex, 3, 4), 16) or 0) / 255,
				(tonumber(string.sub(hex, 5, 6), 16) or 0) / 255)
		end))
		M.Color3 = L
	end
	-- DateTime: a point in time with millisecond resolution (documented datatype; a key-system loader
	-- compares DateTime.now().UnixTimestamp with numbers).  Constructors, the two properties, ToUniversalTime,
	-- ToIsoDate and the numeric format tokens follow the documentation.  Not verified against the engine:
	-- tostring(), the error texts, range checks; local time is taken as UTC; month and day names are English.
	do
		local DTMT = {}
		local function datetime(ms)
			ms = math.floor(ms)
			local t = setmetatable({ UnixTimestamp = ms // 1000, UnixTimestampMillis = ms }, DTMT)
			typeNames[t] = "DateTime"
			return t
		end
		local function civil(ms)        -- -> year, month, day, hour, minute, second, millisecond, weekday (0 = Sunday)
			local days, rest = ms // 86400000, ms % 86400000
			local z = days + 719468
			local era = z // 146097
			local doe = z - era * 146097
			local yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
			local doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
			local mp = (5 * doy + 2) // 153
			local d, m = doy - (153 * mp + 2) // 5 + 1, mp < 10 and mp + 3 or mp - 9
			return yoe + era * 400 + (m <= 2 and 1 or 0), m, d, rest // 3600000, rest // 60000 % 60, rest // 1000 % 60, rest % 1000,
				(days + 4) % 7
		end
		local function fromCivil(y, m, d, h, mi, sec, ms)
			y, m, d = y or 1970, m or 1, d or 1
			if m <= 2 then y -= 1 end
			local era = y // 400
			local yoe = y - era * 400
			local doy = (153 * (m > 2 and m - 3 or m + 9) + 2) // 5 + d - 1
			local days = era * 146097 + yoe * 365 + yoe // 4 - yoe // 100 + doy - 719468
			return datetime(days * 86400000 + (h or 0) * 3600000 + (mi or 0) * 60000 + (sec or 0) * 1000 + (ms or 0))
		end
		local months = { "January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
			"November", "December" }
		local weekdays = { "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday" }
		local function fields(self)
			if typeNames[self] ~= "DateTime" then error("Expected ':' not '.' calling member function ToUniversalTime", 3) end
			local y, m, d, h, mi, sec, ms = civil(self.UnixTimestampMillis)
			return { Year = y, Month = m, Day = d, Hour = h, Minute = mi, Second = sec, Millisecond = ms }
		end
		local function format(self, fmt)
			if typeNames[self] ~= "DateTime" then error("Expected ':' not '.' calling member function FormatUniversalTime", 3) end
			if rawType(fmt) ~= "string" then error("Argument 1 missing or nil", 3) end
			local y, m, d, h, mi, sec, ms, wd = civil(self.UnixTimestampMillis)
			local h12 = (h + 11) % 12 + 1
			local tokens = { YYYY = string.format("%04d", y), YY = string.format("%02d", y % 100), MMMM = months[m],
				MMM = string.sub(months[m], 1, 3), MM = string.format("%02d", m), M = tostring(m), DD = string.format("%02d", d),
				D = tostring(d), dddd = weekdays[wd + 1], ddd = string.sub(weekdays[wd + 1], 1, 3), HH = string.format("%02d", h),
				H = tostring(h), hh = string.format("%02d", h12), h = tostring(h12), mm = string.format("%02d", mi), m = tostring(mi),
				ss = string.format("%02d", sec), s = tostring(sec), SSS = string.format("%03d", ms), A = h < 12 and "AM" or "PM",
				a = h < 12 and "am" or "pm" }
			return (string.gsub(fmt, "%a+", function(word)
				if tokens[word] then return tokens[word] end
				if string.match(word, "^[YMDdHhmsSAaLlTZzQWwEeGgkXx]+$") then
					note("env", "DateTime format token '" .. word .. "' is not modelled: left as it is")
				end
				return word
			end))
		end
		DTMT.__index = { ToUniversalTime = fields, ToLocalTime = fields, FormatUniversalTime = format, FormatLocalTime = format,
			ToIsoDate = function(self)
				if typeNames[self] ~= "DateTime" then error("Expected ':' not '.' calling member function ToIsoDate", 2) end
				local y, m, d, h, mi, sec = civil(self.UnixTimestampMillis)
				return string.format("%04d-%02d-%02dT%02d:%02d:%02dZ", y, m, d, h, mi, sec)
			end }
		DTMT.__eq = function(a, b) return a.UnixTimestampMillis == b.UnixTimestampMillis end
		DTMT.__tostring = function(a) return tostring(a.UnixTimestampMillis) end
		local L = lib("DateTime")
		local t0, c0 = os.time(), os.clock()
		rawset(L, "now", named("DateTime.now", function() return datetime((t0 + (os.clock() - c0)) * 1000) end))
		local function num(v, i, fname)
			if rawType(v) ~= "number" then
				error("invalid argument #" .. i .. " to '" .. fname .. "' (number expected, got " .. rawTypeof(v) .. ")", 3)
			end
			return v
		end
		rawset(L, "fromUnixTimestamp", named("DateTime.fromUnixTimestamp", function(sec)
			return datetime(num(sec, 1, "fromUnixTimestamp") * 1000)
		end))
		rawset(L, "fromUnixTimestampMillis", named("DateTime.fromUnixTimestampMillis", function(ms)
			return datetime(num(ms, 1, "fromUnixTimestampMillis"))
		end))
		rawset(L, "fromUniversalTime", named("DateTime.fromUniversalTime", fromCivil))
		rawset(L, "fromLocalTime", named("DateTime.fromLocalTime", fromCivil))
		rawset(L, "fromIsoDate", named("DateTime.fromIsoDate", function(text)
			if rawType(text) ~= "string" then return nil end
			local y, m, d, rest = string.match(text, "^(%d%d%d%d)%-(%d%d)%-(%d%d)(.*)$")
			if not y then return nil end
			local h, mi, sec, frac, zone = 0, 0, 0, "", ""
			if rest ~= "" then
				h, mi, sec, frac, zone = string.match(rest, "^T(%d%d):(%d%d):(%d%d)(%.?%d*)(.*)$")
				if not h then return nil end
			end
			local shift = 0
			local sign, zh, zm = string.match(zone, "^([+-])(%d%d):?(%d%d)$")
			if sign then shift = (tonumber(zh) * 60 + tonumber(zm)) * (sign == "+" and -60000 or 60000)
			elseif zone ~= "" and zone ~= "Z" then return nil end
			local dt = fromCivil(tonumber(y), tonumber(m), tonumber(d), tonumber(h), tonumber(mi), tonumber(sec),
				frac ~= "" and frac ~= "." and math.floor(tonumber("0" .. frac) * 1000 + 0.5) or 0)
			return datetime(dt.UnixTimestampMillis + shift)
		end))
		M.DateTime = L
	end
	do
		local L = lib("UDim")
		rawset(L, "new", named("UDim.new", udim))
		M.UDim = L
		L = lib("UDim2")
		rawset(L, "new", named("UDim2.new", function(a, b, c, d)
			if type(a) == "table" then return udim2(a.Scale, a.Offset, b.Scale, b.Offset) end
			return udim2(a or 0, b or 0, c or 0, d or 0)
		end))
		rawset(L, "fromScale", named("UDim2.fromScale", function(x, y) return udim2(x, 0, y, 0) end))
		rawset(L, "fromOffset", named("UDim2.fromOffset", function(x, y) return udim2(0, x, 0, y) end))
		M.UDim2 = L
		L = lib("Vector2")
		rawset(L, "new", named("Vector2.new", vec2))
		rawset(L, "zero", vec2(0, 0))
		rawset(L, "one", vec2(1, 1))
		M.Vector2 = L
		L = lib("Path2DControlPoint")
		rawset(L, "new", named("Path2DControlPoint.new", function(pos, left, right)
			for i = 1, 3 do
				local a = (i == 1 and pos) or (i == 2 and left) or right
				if typeNames[a] ~= "UDim2" and (i == 1 or a ~= nil) then
					error("invalid argument #" .. i .. " to 'new' (UDim2 expected, got " .. (typeNames[a] or rawTypeof(a)) .. ")", 2)
				end
			end
			local t = { Position = pos or udim2(0, 0, 0, 0), LeftTangent = left or udim2(0, 0, 0, 0),
				RightTangent = right or udim2(0, 0, 0, 0) }
			typeNames[t] = "Path2DControlPoint"
			return t
		end))
		M.Path2DControlPoint = L
	end

	-- ------------------------------------------------------------ GUI layout
	-- AbsoluteSize, AbsolutePosition and AbsoluteRotation of GuiObjects, derived on every read from
	-- the tree as it is then (the engine updates them before a read sees them).  Luraph's start-up
	-- code (v14.6 on) builds a ScreenGui "ChallengeGui" with a fixed-size Frame and a random tree of
	-- Frame / TextLabel / TextButton under it, with UISizeConstraint, UIScale and UIPadding children,
	-- AnchorPoint and Rotation, reads AbsolutePosition and AbsoluteSize of about ten of them and
	-- compares math.floor(value * 100) with numbers it carries (a difference of 1 is allowed; any
	-- other answer starts its tamper response).  The rules below reproduce those numbers:
	--   content box of the parent: its box minus its UIPadding (scale part relative to the
	--     parent's own size, offsets times the parent's scale); a negative extent is flipped
	--     (the box starts at the far edge and has the absolute size)
	--   size = scale * content + offset * k, k = product of the UIScale of all ancestors;
	--     clamped to the UISizeConstraint (MinSize / MaxSize times k), then times the own UIScale
	--   position = content origin + scale * content + offset * k - AnchorPoint * size
	--   Rotation turns an object and its descendants about its centre; AbsolutePosition is the
	--     turned centre minus half the (unturned) size
	-- Not modelled, and said so in the run's log when met: layouts (UIListLayout, UIGridLayout, ...),
	-- UIAspectRatioConstraint, AutomaticSize, SizeConstraint, ScrollingFrame canvases, BillboardGui /
	-- SurfaceGui.  A ScreenGui that is not in the game has no size here (0, 0), and an object without
	-- a gui parent is laid out against the same empty box; the probe does not depend on either (its
	-- gui is in no parent and its first Frame has offsets only), and Roblox's answer was not measured.
	do
		local LAYER = { ScreenGui = true, BillboardGui = true, SurfaceGui = true, LayerCollector = true }
		local OBJECT = { Frame = true, TextLabel = true, TextButton = true, TextBox = true, ImageLabel = true, ImageButton = true,
			ScrollingFrame = true, CanvasGroup = true, ViewportFrame = true, VideoFrame = true }
		local kinds = {}
		local function guiKind(cls)
			local k = kinds[cls]
			if k == nil then
				if API.loaded and API.classes[cls] then
					k = (classIsA(cls, "GuiObject") and "object") or (classIsA(cls, "LayerCollector") and "layer") or false
				else
					k = (OBJECT[cls] and "object") or (LAYER[cls] and "layer") or false
				end
				kinds[cls] = k
			end
			return k
		end
		local UNMODELLED = { UIListLayout = true, UIGridLayout = true, UITableLayout = true, UIPageLayout = true,
			UIAspectRatioConstraint = true, UIFlexItem = true, UITextSizeConstraint = false }
		local function modifier(d, cls)
			local found
			for _, c in ipairs(d.children) do
				local cc = data[c].props.ClassName
				if cc == cls and not found then found = data[c] end
				if UNMODELLED[cc] then note("env", "GUI layout: " .. cc .. " is not modelled; AbsoluteSize / AbsolutePosition ignore it") end
			end
			return found
		end
		local function num(v, default) return rawType(v) == "number" and v or default end
		local function ownScale(d)
			local u = modifier(d, "UIScale")
			return u and num(u.props.Scale, 1) or 1
		end
		local function udimPx(v, extent, k)
			if typeNames[v] ~= "UDim" then return 0 end
			return num(v.Scale, 0) * extent + num(v.Offset, 0) * k
		end
		local function inGame(inst)
			local p = inst
			for _ = 1, 64 do
				if p == nil then return false end
				if p == game then return true end
				p = data[p] and data[p].props.Parent
			end
			return false
		end
		-- -> width, height, centre x, centre y, rotation (degrees), scale handed to children
		local function layout(inst, depth)
			local d = data[inst]
			local kind = guiKind(d.props.ClassName)
			if not kind then return 0, 0, 0, 0, 0, 1 end
			if kind == "layer" or depth > 64 then
				local s = ownScale(d)
				if d.props.ClassName == "ScreenGui" and inGame(inst) then
					-- the viewport the mock uses elsewhere (Camera.ViewportSize) below the top bar
					-- (GuiService:GetGuiInset); AbsolutePosition does not count the bar
					local w, h, top = vec2Props.ViewportSize[1], vec2Props.ViewportSize[2], 58
					if d.props.IgnoreGuiInset == true then return w, h, w / 2, h / 2 - top, 0, s end
					return w, h - top, w / 2, (h - top) / 2, 0, s
				end
				if d.props.ClassName ~= "ScreenGui" then
					note("env", "GUI layout: " .. d.props.ClassName .. " is not modelled; its AbsoluteSize is (0, 0) here")
				end
				return 0, 0, 0, 0, 0, s
			end
			-- (an object without a gui parent is laid out like one in a ScreenGui outside the game:
			-- against an empty box at the origin)
			local parent = d.props.Parent
			local pd = parent and data[parent]
			local pw, ph, pcx, pcy, prot, k = 0, 0, 0, 0, 0, 1
			if pd and guiKind(pd.props.ClassName) then
				pw, ph, pcx, pcy, prot, k = layout(parent, depth + 1)
			else
				pd = nil
			end
			-- the parent's content box
			local l, t, cw, ch = 0, 0, pw, ph
			local pad = pd and modifier(pd, "UIPadding")
			if pad then
				l, t = udimPx(pad.props.PaddingLeft, pw, k), udimPx(pad.props.PaddingTop, ph, k)
				cw = pw - l - udimPx(pad.props.PaddingRight, pw, k)
				ch = ph - t - udimPx(pad.props.PaddingBottom, ph, k)
				if cw < 0 then l, cw = l + cw, -cw end
				if ch < 0 then t, ch = t + ch, -ch end
			end
			local w, h = 0, 0
			local size = d.props.Size
			if typeNames[size] == "UDim2" then
				w, h = size.X.Scale * cw + size.X.Offset * k, size.Y.Scale * ch + size.Y.Offset * k
			end
			local con = modifier(d, "UISizeConstraint")
			if con then
				local mn, mx = con.props.MinSize, con.props.MaxSize
				if typeNames[mx] == "Vector2" then w, h = math.min(w, mx.X * k), math.min(h, mx.Y * k) end
				if typeNames[mn] == "Vector2" then w, h = math.max(w, mn.X * k), math.max(h, mn.Y * k) end
			end
			local s = ownScale(d)
			w, h = w * s, h * s
			local x, y = l, t
			local pos = d.props.Position
			if typeNames[pos] == "UDim2" then
				x, y = l + pos.X.Scale * cw + pos.X.Offset * k, t + pos.Y.Scale * ch + pos.Y.Offset * k
			end
			local an = d.props.AnchorPoint
			if typeNames[an] == "Vector2" then x, y = x - an.X * w, y - an.Y * h end
			local auto, sc = d.props.AutomaticSize, d.props.SizeConstraint
			if (auto ~= nil and tostring(auto) ~= "Enum.AutomaticSize.None") or (sc ~= nil and tostring(sc) ~= "Enum.SizeConstraint.RelativeXY") then
				note("env", "GUI layout: AutomaticSize and SizeConstraint are not modelled; AbsoluteSize / AbsolutePosition ignore them")
			end
			if pd and pd.props.ClassName == "ScrollingFrame" then
				note("env", "GUI layout: the canvas of a ScrollingFrame is not modelled; its children are laid out in its window")
			end
			-- centre relative to the parent's centre, turned with the parent
			local rx, ry = x + w / 2 - pw / 2, y + h / 2 - ph / 2
			local a = math.rad(prot)
			local ca, sa = math.cos(a), math.sin(a)
			return w, h, pcx + rx * ca - ry * sa, pcy + rx * sa + ry * ca, prot + num(d.props.Rotation, 0), k * s
		end
		P.layout = function(inst) return layout(inst, 0) end
		local make = vec2
		computed.AbsoluteSize = function(inst, d)
			if not guiKind(d.props.ClassName) then return nil end
			local w, h = layout(inst, 0)
			return make(w, h)
		end
		computed.AbsolutePosition = function(inst, d)
			if not guiKind(d.props.ClassName) then return nil end
			local w, h, cx, cy = layout(inst, 0)
			return make(cx - w / 2, cy - h / 2)
		end
		computed.AbsoluteRotation = function(inst, d)
			if not guiKind(d.props.ClassName) then return nil end
			local _, _, _, _, rot = layout(inst, 0)
			return rot
		end
		-- engine defaults of the properties the rules read (documented defaults of these classes)
		local function zero2() return udim2(0, 0, 0, 0) end
		classDefaults.Size = function(cls) return guiKind(cls) == "object" and zero2() or nil end
		classDefaults.Position = classDefaults.Size
		classDefaults.AnchorPoint = function(cls) return guiKind(cls) == "object" and vec2(0, 0) or nil end
		classDefaults.Scale = function(cls) return cls == "UIScale" and 1 or nil end
		classDefaults.MinSize = function(cls) return cls == "UISizeConstraint" and vec2(0, 0) or nil end
		classDefaults.MaxSize = function(cls) return cls == "UISizeConstraint" and vec2(math.huge, math.huge) or nil end
		for _, side in ipairs({ "PaddingLeft", "PaddingRight", "PaddingTop", "PaddingBottom" }) do
			classDefaults[side] = function(cls) return cls == "UIPadding" and udim(0, 0) or nil end
		end
	end

	-- Path2D: a chain of cubic Beziers. Segment i runs from point i to point i+1 with handles
	-- pos[i] + RightTangent[i] and pos[i+1] + LeftTangent[i+1]; t in [0,1] spans all segments evenly.
	do
		-- (the frame a path is measured against: the AbsoluteSize of its parent, see GUI layout)
		local function absSize(inst)
			local p = data[inst].props.Parent
			if p and data[p] then
				local w, h = P.layout(p)
				return w, h
			end
			return 0, 0
		end
		-- Curve model: float32 throughout, in the engine's order of operations.  Ported from the
		-- Studio-verified model of the "deobf" Luraph deobfuscator (envlog.luau, Path2D section):
		--   pixel = f32(f32(scale * W) + offset); a handle of 0 px is left out (cubic / quadratic /
		--   line); power basis evaluated by Horner's rule
		--   GetLength: adaptive bisection per segment (chord a, half chords b; b - a < 1e-5 -> b)
		--   arc-length queries: s = f32(t * L), then 32 RK4 steps of du/ds = 1/|B'(u)|
		-- Luraph feeds the float32 bit patterns of these results into its decryption key.
		local FB = buffer.create(4)
		local function f32(x)
			buffer.writef32(FB, 0, x)
			return buffer.readf32(FB, 0)
		end
		local function resolve(u, W, H)
			return f32(f32(f32(u.X.Scale) * W) + u.X.Offset), f32(f32(f32(u.Y.Scale) * H) + u.Y.Offset)
		end
		local function coeffs(sg)
			local n = #sg // 2
			local x0, y0 = sg[1], sg[2]
			local c = { n = n, x0 = x0, y0 = y0 }
			if n == 2 then
				c.c1x, c.c1y = f32(sg[3] - x0), f32(sg[4] - y0)
			elseif n == 3 then
				local x1, y1, x2, y2 = sg[3], sg[4], sg[5], sg[6]
				c.c1x, c.c1y = f32(f32(x1 - x0) * 2), f32(f32(y1 - y0) * 2)
				c.c2x, c.c2y = f32(f32(x0 - f32(x1 * 2)) + x2), f32(f32(y0 - f32(y1 * 2)) + y2)
			else
				local x1, y1, x2, y2, x3, y3 = sg[3], sg[4], sg[5], sg[6], sg[7], sg[8]
				c.c1x, c.c1y = f32(f32(x1 - x0) * 3), f32(f32(y1 - y0) * 3)
				c.c2x = f32(f32(f32(x0 - f32(x1 * 2)) + x2) * 3)
				c.c2y = f32(f32(f32(y0 - f32(y1 * 2)) + y2) * 3)
				c.c3x = f32(f32(f32(x3 - x0) + f32(x1 * 3)) - f32(x2 * 3))
				c.c3y = f32(f32(f32(y3 - y0) + f32(y1 * 3)) - f32(y2 * 3))
			end
			return c
		end
		-- the control points in pixels: { x, y, left dx, left dy, right dx, right dy }
		local function points(self)
			local W, H = absSize(self)
			local pts = {}
			for i, cp in ipairs(data[self].points or {}) do
				local px, py = resolve(cp.Position, W, H)
				local lx, ly = resolve(cp.LeftTangent, W, H)
				local rx, ry = resolve(cp.RightTangent, W, H)
				pts[i] = { px, py, lx, ly, rx, ry }
			end
			return pts
		end
		local function segments(self)
			local pts = points(self)
			local segs = {}
			for i = 1, #pts - 1 do
				local a, b = pts[i], pts[i + 1]
				local sg = { a[1], a[2] }
				if a[5] ~= 0 or a[6] ~= 0 then
					sg[#sg + 1] = f32(a[1] + a[5]); sg[#sg + 1] = f32(a[2] + a[6])
				end
				if b[3] ~= 0 or b[4] ~= 0 then
					sg[#sg + 1] = f32(b[1] + b[3]); sg[#sg + 1] = f32(b[2] + b[4])
				end
				sg[#sg + 1] = b[1]; sg[#sg + 1] = b[2]
				segs[i] = coeffs(sg)
				segs[i].x3, segs[i].y3 = b[1], b[2]
			end
			return segs
		end
		local function eval(c, u)
			if c.n == 2 then
				return f32(c.x0 + f32(c.c1x * u)), f32(c.y0 + f32(c.c1y * u))
			elseif c.n == 3 then
				return f32(c.x0 + f32(f32(c.c1x + f32(c.c2x * u)) * u)), f32(c.y0 + f32(f32(c.c1y + f32(c.c2y * u)) * u))
			end
			local ix, iy = f32(c.c2x + f32(c.c3x * u)), f32(c.c2y + f32(c.c3y * u))
			return f32(c.x0 + f32(f32(c.c1x + f32(ix * u)) * u)), f32(c.y0 + f32(f32(c.c1y + f32(iy * u)) * u))
		end
		local function tangent(c, u)
			if c.n == 2 then return c.c1x, c.c1y end
			if c.n == 3 then
				local k = f32(u * 2)
				return f32(c.c1x + f32(c.c2x * k)), f32(c.c1y + f32(c.c2y * k))
			end
			local k = f32(3 * u)
			return f32(c.c1x + f32(u * f32(f32(c.c2x * 2) + f32(c.c3x * k)))),
				f32(c.c1y + f32(u * f32(f32(c.c2y * 2) + f32(c.c3y * k))))
		end
		local function dist(ax, ay, bx, by)
			local dx, dy = f32(bx - ax), f32(by - ay)
			return f32(math.sqrt(f32(f32(dx * dx) + f32(dy * dy))))
		end
		local TOL = f32(1e-5)
		-- B(u) - P0: the length routine leaves the P0 term out (different rounding)
		local function eval0(c, u)
			if c.n == 2 then
				return f32(c.c1x * u), f32(c.c1y * u)
			elseif c.n == 3 then
				return f32(f32(c.c1x + f32(c.c2x * u)) * u), f32(f32(c.c1y + f32(c.c2y * u)) * u)
			end
			local ix, iy = f32(c.c2x + f32(c.c3x * u)), f32(c.c2y + f32(c.c3y * u))
			return f32(f32(c.c1x + f32(ix * u)) * u), f32(f32(c.c1y + f32(iy * u)) * u)
		end
		local function seglen(c)
			local function rec(t0, t1, x0, y0, x1, y1, depth)
				local tm = f32(f32(t0 + t1) * 0.5)
				local xm, ym = eval0(c, tm)
				local a = dist(x0, y0, x1, y1)
				local b = f32(dist(x0, y0, xm, ym) + dist(xm, ym, x1, y1))
				if depth >= 30 or f32(b - a) < TOL then return b end
				return f32(rec(t0, tm, x0, y0, xm, ym, depth + 1) + rec(tm, t1, xm, ym, x1, y1, depth + 1))
			end
			local x0, y0 = eval0(c, 0)
			local x1, y1 = eval0(c, 1)
			return rec(0, 1, x0, y0, x1, y1, 0)
		end
		local SIXTH = f32(1 / 6)
		local function arcU(c, sl)
			local d2x, d2y = f32(c.c2x * 2), f32(c.c2y * 2)
			local d3x, d3y
			if c.n == 4 then d3x, d3y = f32(c.c3x * 3), f32(c.c3y * 3) end
			local function inv(u)
				local dx, dy
				if c.n == 3 then
					dx, dy = f32(c.c1x + f32(d2x * u)), f32(c.c1y + f32(d2y * u))
				else
					dx = f32(c.c1x + f32(u * f32(d2x + f32(u * d3x))))
					dy = f32(c.c1y + f32(u * f32(d2y + f32(u * d3y))))
				end
				return math.sqrt(f32(f32(dx * dx) + f32(dy * dy)))
			end
			local h = f32(sl / 32)
			local u = 0
			for _ = 1, 32 do
				local k1 = f32(h / f32(inv(u)))
				local k2 = f32(h / f32(inv(f32(u + f32(k1 * 0.5)))))
				local k3 = f32(h / f32(inv(f32(u + f32(k2 * 0.5)))))
				local k4 = f32(h / f32(inv(f32(u + k3))))
				u = f32(u + f32(f32(f32(k1 + f32(2 * f32(k2 + k3))) + k4) * SIXTH))
			end
			return u
		end
		local function length(segs)
			local t = 0
			for _, c in ipairs(segs) do
				c.len = c.len or seglen(c)
				t = f32(t + c.len)
			end
			return t
		end
		local function locate(segs, t)
			local n = #segs
			if n == 0 then return nil end
			t = f32(t)
			if t < 0 then t = 0 elseif t > 1 then t = 1 end
			local x = f32(t * n)
			local k = math.min(math.floor(x), n - 1)
			return segs[k + 1], f32(x - k)
		end
		local function locateArc(self, segs, t)
			if #segs == 0 then return nil end
			t = f32(t)
			if t < 0 then t = 0 elseif t > 1 then t = 1 end
			local L = length(segs)
			local sl = f32(t * L)
			for _, c in ipairs(segs) do
				if sl <= c.len then
					if c.n == 2 then return c, f32(sl / c.len) end
					return c, arcU(c, sl)
				end
				sl = f32(sl - c.len)
			end
			return segs[1], 0
		end
		-- positions come back as pure-scale UDim2s: the pixel position divided by the parent size
		local function posAt(self, c, u)
			local W, H = absSize(self)
			local x, y = eval(c, u)
			return udim2(W ~= 0 and f32(x / W) or 0, 0, H ~= 0 and f32(y / H) or 0, 0)
		end
		local function tanAt(self, c, u)
			return vec2(tangent(c, u))
		end
		local function segLength(self, c) return seglen(c) end
		m("SetControlPoints", function(self, pts)
			local copy = {}
			for i, p in ipairs(pts) do copy[i] = p end
			data[self].points = copy
		end)
		m("GetControlPoints", function(self)
			local out = {}
			for i, p in ipairs(data[self].points or {}) do out[i] = p end
			return out
		end)
		m("GetControlPoint", function(self, i) return (data[self].points or {})[i] end)
		m("InsertControlPoint", function(self, i, p)
			data[self].points = data[self].points or {}
			table.insert(data[self].points, i, p)
		end)
		m("RemoveControlPoint", function(self, i) table.remove(data[self].points or {}, i) end)
		m("UpdateControlPoint", function(self, i, p) (data[self].points or {})[i] = p end)
		m("GetSegmentCount", function(self) return math.max(#(data[self].points or {}) - 1, 0) end)
		m("GetMaxControlPoints", function() return 250 end)
		-- A Path2D without a parent has no frame to measure against: the engine refuses curve
		-- queries on it (observed in Studio by the second engine, deobf/envlog.luau).
		local function needParent(self)
			if data[self].props.Parent == nil then error("Attempting to use Path2D with invalid parent", 4) end
		end
		m("GetLength", function(self)
			needParent(self)
			return length(segments(self))
		end)
		-- Rect: Min / Max corners, Width / Height (float32, as the engine stores them)
		local RectMT = {}
		local function rect(x0, y0, x1, y1)
			local t = setmetatable({ Min = vec2(x0, y0), Max = vec2(x1, y1), Width = f32(x1 - x0), Height = f32(y1 - y0) }, RectMT)
			typeNames[t] = "Rect"
			return t
		end
		RectMT.__eq = function(a, b) return a.Min == b.Min and a.Max == b.Max end
		RectMT.__tostring = function(a)
			return string.format("%.9g, %.9g, %.9g, %.9g", a.Min.X, a.Min.Y, a.Max.X, a.Max.Y)
		end
		seal(RectMT)
		-- The box of the curve's control polygon (each point and the tangent handles that shape a
		-- segment) widened by half the stroke (Thickness, pixels) on every side, in the parent's
		-- pixels.  The engine's rule is not documented beyond "encloses the path, expanded by its
		-- rendered thickness": this is the plausible one, the same as the second engine's
		-- (deobf/envlog.luau, CHAIN.p2d.rect), not one measured in Roblox.
		m("GetBoundingRect", function(self)
			needParent(self)
			local pts = points(self)
			if #pts < 2 then
				note("stub", "Path2D:GetBoundingRect() of a path without a segment has no model in the mock: answered with a permissive stand-in")
				return stub("Path2D:GetBoundingRect()", "Rect")
			end
			local T = data[self].props.Thickness
			if type(T) ~= "number" then T = 1 end
			local lx, ly, hx, hy = math.huge, math.huge, -math.huge, -math.huge
			local function add(x, y)
				lx, hx = math.min(lx, x), math.max(hx, x)
				ly, hy = math.min(ly, y), math.max(hy, y)
			end
			for i, q in ipairs(pts) do
				add(q[1], q[2])
				if i > 1 then add(f32(q[1] + q[3]), f32(q[2] + q[4])) end
				if i < #pts then add(f32(q[1] + q[5]), f32(q[2] + q[6])) end
			end
			local e = f32(f32(T) / 2)
			return rect(f32(lx - e), f32(ly - e), f32(hx + e), f32(hy + e))
		end)
		m("GetPositionOnCurve", function(self, t)
			needParent(self)
			local seg, u = locate(segments(self), t)
			if not seg then return udim2(0, 0, 0, 0) end
			return posAt(self, seg, u)
		end)
		m("GetTangentOnCurve", function(self, t)
			needParent(self)
			local seg, u = locate(segments(self), t)
			if not seg then return vec2(0, 0) end
			return tanAt(self, seg, u)
		end)
		m("GetPositionOnCurveArcLength", function(self, t)
			needParent(self)
			local seg, u = locateArc(self, segments(self), t)
			if not seg then return udim2(0, 0, 0, 0) end
			return posAt(self, seg, u)
		end)
		m("GetTangentOnCurveArcLength", function(self, t)
			needParent(self)
			local seg, u = locateArc(self, segments(self), t)
			if not seg then return vec2(0, 0) end
			return tanAt(self, seg, u)
		end)
	end

	-- Enum: every enum, item and value of the API table.  An item that does not exist is an error
	-- (`Enum.PartType.Foo`) or nil (`FromName`, `FromValue`), never a new item: Luraph's start-up
	-- code asks for items by value and compares what comes back.
	do
		local enumTypes = {}
		-- Old names of items that Roblox still accepts (`Enum.RaycastFilterType.Blacklist`,
		-- `Enum.Font.GothamSemibold`): the "LegacyNames" of Roblox's API dump.  The API table lists
		-- current names only (gen_roblox.py drops them), so they are kept here: old name -> item.
		local LEGACY = {
			AccessoryType = { TeeShirt = "TShirt" },
			AssetType = { TeeShirt = "TShirt", TeeShirtAccessory = "TShirtAccessory" },
			AvatarAssetType = { TeeShirtAccessory = "TShirtAccessory" },
			Axis = { Back = "Z", Bottom = "Y", Front = "Z", Left = "X", Right = "X", Top = "Y" },
			BinType = { Laser = "Script", Rocket = "Script", Slingshot = "Script" },
			CatalogSortType = { RecentlyUpdated = "RecentlyCreated" },
			ConnectionError = { DisconnectNoResponse = "DisconnectRaknetErrors" },
			ContextActionPriority = { Default = "Medium" },
			ControlMode = { ["Mouse Lock Switch"] = "MouseLockSwitch" },
			DeveloperMemoryTag = { PhysicsParts = "BaseParts" },
			ExplosionType = { CratersAndDebris = "Craters" },
			ExternalEditorMode = { CustomEditor = "UserSelectedEditor" },
			FluidForces = { Enabled = "Experimental" },
			Font = { GothamSemibold = "GothamMedium", Montserrat = "Gotham", MontserratBlack = "GothamBlack", MontserratBold = "GothamBold", MontserratMedium = "GothamMedium" },
			FormFactor = { Block = "Brick" },
			InputType = { Action1 = "NoInput", Action2 = "NoInput", Action3 = "NoInput", Action4 = "NoInput", Action5 = "NoInput", LeftTread = "NoInput", RightTread = "NoInput", Steer = "NoInput", Throtle = "NoInput", Throttle = "NoInput", UpDown = "NoInput" },
			KeyCode = { Touch = "TouchPosition", Unknown = "None" },
			Material = { Aluminum = "Foil", ["Corroded Metal"] = "CorrodedMetal" },
			ModelLevelOfDetail = { Deactivated = "Disabled" },
			QualityLevel = { ["Level  1"] = "Level01", ["Level  2"] = "Level02", ["Level  3"] = "Level03", ["Level  4"] = "Level04", ["Level  5"] = "Level05", ["Level  6"] = "Level06", ["Level  7"] = "Level07", ["Level  8"] = "Level08", ["Level  9"] = "Level09", ["Level 10"] = "Level10", ["Level 11"] = "Level11", ["Level 12"] = "Level12", ["Level 13"] = "Level13", ["Level 14"] = "Level14", ["Level 15"] = "Level15", ["Level 16"] = "Level16", ["Level 17"] = "Level17", ["Level 18"] = "Level18", ["Level 19"] = "Level19", ["Level 20"] = "Level20", ["Level 21"] = "Level21" },
			RaycastFilterType = { Blacklist = "Exclude", Whitelist = "Include" },
			Style = { ["Alternating Supports"] = "AlternatingSupports", ["Bridge Style Supports"] = "BridgeStyleSupports", ["No Supports"] = "NoSupports" },
			SurfaceType = { Bumps = "Glue", Spawn = "Smooth", Unjoinable = "Smooth" },
			ThreadPoolConfig = { PartialThread = "Auto" },
		}
		local function enumItem(et, etName, name, value)
			local u = newproxy(true)
			local mt = getmetatable(u)
			typeNames[u] = "EnumItem"
			local props = { Name = name, Value = value, EnumType = et }
			props.IsA = named("IsA", function(self, n)
				if self ~= u then error("Expected ':' not '.' calling member function IsA", 2) end
				return n == etName
			end)
			mt.__index = function(_, k)
				local v = props[k]
				if v == nil and API.loaded then
					error(tostring(k) .. " is not a valid member of \"Enum." .. etName .. "." .. name .. "\"", 2)
				end
				return v
			end
			mt.__tostring = function() return "Enum." .. etName .. "." .. name end
			mt.__type = "EnumItem"
			mt.__metatable = "The metatable is locked"
			seal(mt)
			return u
		end
		local function enumType(etName)
			local et = enumTypes[etName]
			if et then return et end
			if API.loaded and not API.enums[etName] then return nil end
			et = newproxy(true)
			enumTypes[etName] = et
			local mt = getmetatable(et)
			typeNames[et] = "Enum"
			local items, byValue, order, n = {}, {}, {}, 0
			local function item(name, value)
				local it = items[name]
				if not it then
					if value == nil then
						value = n
					end
					n += 1
					it = enumItem(et, etName, name, value)
					items[name] = it
					order[#order + 1] = it
					if byValue[value] == nil then byValue[value] = it end
				end
				return it
			end
			if API.loaded then
				for iname, v in string.gmatch(API.enums[etName], "([%w_]+)=(%-?%d+)") do item(iname, tonumber(v)) end
				for old, new in pairs(LEGACY[etName] or {}) do
					if items[old] == nil then items[old] = items[new] end
				end
			elseif etName == "PartType" then
				item("Ball", 0) item("Block", 1) item("Cylinder", 2)
			end
			local ms = {}
			ms.GetEnumItems = named("GetEnumItems", function(self)
				if self ~= et then error("Expected ':' not '.' calling member function GetEnumItems", 2) end
				return table.clone(order)
			end)
			ms.FromName = named("FromName", function(self, name)
				if self ~= et then error("Expected ':' not '.' calling member function FromName", 2) end
				if rawType(name) ~= "string" then error("Argument 1 missing or nil", 2) end
				if API.loaded then return items[name] end
				return item(name)
			end)
			ms.FromValue = named("FromValue", function(self, v)
				if self ~= et then error("Expected ':' not '.' calling member function FromValue", 2) end
				if rawType(v) ~= "number" then error("Argument 1 missing or nil", 2) end
				if API.loaded then return byValue[v] end
				return byValue[v] or item("Value" .. tostring(v), v)
			end)
			mt.__index = function(_, k)
				if ms[k] then return ms[k] end
				if API.loaded then
					local it = rawType(k) == "string" and items[k] or nil
					if it == nil then
						-- (said in the log: the table has no legacy names, so this can be the table's gap)
						note("env", "Enum." .. etName .. "." .. tostring(k) .. " is not in the API table: answered with Roblox's error " ..
							"for an unknown item (if it is an old name Roblox still accepts, the table lacks it)")
						error(tostring(k) .. " is not a valid member of \"Enum." .. etName .. "\"", 2)
					end
					return it
				end
				if type(k) ~= "string" then return nil end
				return item(k)
			end
			mt.__tostring = function() return etName end
			mt.__type = "Enum"
			mt.__metatable = "The metatable is locked"
			seal(mt)
			return et
		end
		local Enum = newproxy(true)
		local mt = getmetatable(Enum)
		typeNames[Enum] = "Enums"
		local ms = { GetEnums = named("GetEnums", function(self)
			if self ~= Enum then error("Expected ':' not '.' calling member function GetEnums", 2) end
			local out = {}
			for i, ename in ipairs(API.enumOrder) do out[i] = enumType(ename) end
			return out
		end) }
		mt.__index = function(_, k)
			if ms[k] then return ms[k] end
			local et = rawType(k) == "string" and enumType(k) or nil
			if et == nil and API.loaded then error(tostring(k) .. " is not a valid member of \"Enum\"", 2) end
			return et
		end
		mt.__tostring = function() return "Enums" end
		mt.__type = "Enums"
		mt.__metatable = "The metatable is locked"
			seal(mt)
		M.Enum = Enum
		M.enumName = function(v)
			if typeNames[v] == "Enums" then return "Enum" end
			if typeNames[v] == "Enum" or typeNames[v] == "EnumItem" then return tostring(v) end
			return nil
		end
		P.enumItem = function(etName, name)
			local et = enumType(etName)
			local ok, it = pcall(function() return et and et[name] end)
			return ok and it or nil
		end
	end

	-- Random: Roblox uses PCG32 (XSH-RR, increment 105); NextNumber joins two draws into 64 bits.
	do
		local Random = lib("Random")
		local function mul32(a, b)
			local a1, a0 = a // 65536, a % 65536
			local b1, b0 = b // 65536, b % 65536
			local cross = a1 * b0 + a0 * b1
			local low = a0 * b0 + (cross % 65536) * 65536
			return a1 * b1 + cross // 65536 + low // 4294967296, low % 4294967296
		end
		local CH, CL = 0x5851F42D, 0x4C957F2D
		local function construct(hi, lo)
			local function draw()
				local ohi, olo = hi, lo
				local phi, plo = mul32(olo, CL)
				local _, c1 = mul32(olo, CH)
				local _, c2 = mul32(ohi, CL)
				plo += 105
				if plo >= 4294967296 then
					plo -= 4294967296
					phi += 1
				end
				hi, lo = (phi + c1 + c2) % 4294967296, plo
				-- xorshifted = ((old >> 18) ^ old) >> 27 ; rot = old >> 59
				local s18lo = bit32.bor(bit32.rshift(olo, 18), bit32.lshift(ohi, 14))
				local s18hi = bit32.rshift(ohi, 18)
				local xlo, xhi = bit32.bxor(s18lo, olo), bit32.bxor(s18hi, ohi)
				local xs = bit32.bor(bit32.rshift(xlo, 27), bit32.lshift(xhi, 5))
				return bit32.rrotate(xs, bit32.rshift(ohi, 27))
			end
			local r = newproxy(true)
			local mt = getmetatable(r)
			typeNames[r] = "Random"
			local ms = {}
			local function number()
				local l = draw()
				local h = draw()
				return h * 2 ^ -32 + l * 2 ^ -64
			end
			ms.NextNumber = named("NextNumber", function(_, a, b)
				if a and b then return a + (b - a) * number() end
				return number()
			end)
			ms.NextInteger = named("NextInteger", function(_, a, b)
				local h, l = mul32(draw(), (b - a + 1) % 4294967296)
				return a + h
			end)
			ms.NextUnitVector = named("NextUnitVector", function()
				draw()
				draw()
				if M.Vector3 then
					-- (the direction itself is not derived from the generator state)
					return M.Vector3.new(0, 1, 0)
				end
				return stub("Vector3", "Vector3")
			end)
			ms.Shuffle = named("Shuffle", function(_, t)
				for i = #t, 2, -1 do
					local j = 1 + mul32(draw(), i)
					t[i], t[j] = t[j], t[i]
				end
			end)
			ms.Clone = named("Clone", function() return construct(hi, lo) end)
			mt.__index = function(_, k) return ms[k] end
			mt.__type = "Random"
			mt.__tostring = function() return "Random" end
			mt.__metatable = "The metatable is locked"
			seal(mt)
			return r, draw, function(h, l) hi, lo = h, l end, function() return hi, lo end
		end
		rawset(Random, "new", named("Random.new", function(seed)
			if seed ~= nil and rawType(seed) ~= "number" then
				error("invalid argument #1 to 'new' (number expected, got " .. rawTypeof(seed) .. ")", 2)
			end
			seed = math.floor(tonumber(seed) or 0)
			local r, draw, set, get = construct(0, 0)
			draw()
			local hi, lo = get()
			local shi, slo = (seed // 4294967296) % 4294967296, seed % 4294967296
			lo += slo
			if lo >= 4294967296 then
				lo -= 4294967296
				hi += 1
			end
			set((hi + shi) % 4294967296, lo)
			draw()
			return r
		end))
		M.Random = Random
	end

	-- Vector3: the native vector type, as in Roblox (type() == "vector", typeof() == "Vector3").
	-- Needs setvectormetatable from the patched CLI; without it Vector3 stays a permissive stub.
	if G.setvectormetatable and G.vector then
		local vec = G.vector
		local create = vec.create
		-- (a component that is not a number is a stand-in value from the analysis run: the vector
		-- is built all the same, so that the code after it gets to run)
		local function comp(x) return type(x) == "number" and x or 0 end
		local function v3(x, y, z) return create(comp(x), comp(y), comp(z)) end
		local function sign(x) return x > 0 and 1 or (x < 0 and -1 or 0) end
		local methods = {
			Abs = function(a) return vec.abs(a) end,
			Ceil = function(a) return vec.ceil(a) end,
			Floor = function(a) return vec.floor(a) end,
			Sign = function(a) return create(sign(a.x), sign(a.y), sign(a.z)) end,
			Dot = function(a, b) return vec.dot(a, b) end,
			Cross = function(a, b) return vec.cross(a, b) end,
			Angle = function(a, b, axis)
				local ang = math.atan2(vec.magnitude(vec.cross(a, b)), vec.dot(a, b))
				if axis and vec.dot(vec.cross(a, b), axis) < 0 then ang = -ang end
				return ang
			end,
			FuzzyEq = function(a, b, eps)
				eps = eps or 1e-5
				local d = a - b
				return math.abs(d.x) <= eps and math.abs(d.y) <= eps and math.abs(d.z) <= eps
			end,
			Lerp = function(a, b, t) return a + (b - a) * t end,
			Max = function(a, ...)
				for _, b in ipairs({ ... }) do a = vec.max(a, b) end
				return a
			end,
			Min = function(a, ...)
				for _, b in ipairs({ ... }) do a = vec.min(a, b) end
				return a
			end,
		}
		for k, f in pairs(methods) do methods[k] = named(k, f) end
		G.setvectormetatable(seal({
			__index = function(v, k)
				-- (a computed key does not take the VM's built-in x/y/z path)
				if k == "X" or k == "x" then return v.x end
				if k == "Y" or k == "y" then return v.y end
				if k == "Z" or k == "z" then return v.z end
				if k == "Magnitude" then return vec.magnitude(v) end
				if k == "Unit" then return vec.normalize(v) end
				return methods[k]
			end,
			__type = "Vector3",
		}))
		local L = {}
		libnames[L] = "Vector3"
		L.new = named("Vector3.new", v3)
		L.zero, L.one = create(0, 0, 0), create(1, 1, 1)
		L.xAxis, L.yAxis, L.zAxis = create(1, 0, 0), create(0, 1, 0), create(0, 0, 1)
		local axes = { X = L.xAxis, Y = L.yAxis, Z = L.zAxis, Right = L.xAxis, Top = L.yAxis, Back = L.zAxis,
			Left = -L.xAxis, Bottom = -L.yAxis, Front = -L.zAxis }
		local function fromEnum(item)
			local ok, name = pcall(function() return item.Name end)
			return (ok and axes[name]) or L.zero
		end
		-- like the real constructors these reject a missing argument (scripts call them bare
		-- inside xpcall and expect the error to come from the builtin itself)
		L.FromAxis = named("Vector3.FromAxis", function(e)
			if e == nil then error("Argument 1 missing or nil", 2) end
			return fromEnum(e)
		end)
		L.FromNormalId = named("Vector3.FromNormalId", function(e)
			if e == nil then error("Argument 1 missing or nil", 2) end
			return fromEnum(e)
		end)
		L.fromAxis, L.fromNormalId = L.FromAxis, L.FromNormalId
		M.Vector3 = L
		M.vectorTypeName = "Vector3"

		-- CFrame: position + rotation matrix.  Scripts only exercise the API (construct, convert,
		-- combine); the numbers are right for the common cases but nothing here is bit-exact.
		local CF = {}
		libnames[CF] = "CFrame"
		local cfMT = {}
		local function cf(p, r)
			local t = setmetatable({ p = p or create(0, 0, 0), r = r or { 1, 0, 0, 0, 1, 0, 0, 0, 1 } }, cfMT)
			typeNames[t] = "CFrame"
			return t
		end
		local function isvec(v) return rawType(v) == "vector" end
		local function iscf(v) return rawType(v) == "table" and typeNames[v] == "CFrame" end
		local function mulmat(a, b)
			local o = {}
			for i = 0, 2 do
				for j = 0, 2 do
					o[i * 3 + j + 1] = a[i * 3 + 1] * b[j + 1] + a[i * 3 + 2] * b[j + 4] + a[i * 3 + 3] * b[j + 7]
				end
			end
			return o
		end
		local function rotvec(r, v)
			return create(r[1] * v.x + r[2] * v.y + r[3] * v.z, r[4] * v.x + r[5] * v.y + r[6] * v.z, r[7] * v.x + r[8] * v.y + r[9] * v.z)
		end
		local function transpose(r) return { r[1], r[4], r[7], r[2], r[5], r[8], r[3], r[6], r[9] } end
		local function rx(a) local c, s_ = math.cos(a), math.sin(a) return { 1, 0, 0, 0, c, -s_, 0, s_, c } end
		local function ry(a) local c, s_ = math.cos(a), math.sin(a) return { c, 0, s_, 0, 1, 0, -s_, 0, c } end
		local function rz(a) local c, s_ = math.cos(a), math.sin(a) return { c, -s_, 0, s_, c, 0, 0, 0, 1 } end
		local function num(x) return rawType(x) == "number" and x or 0 end
		local function axisAngle(axis, angle)
			local m = vec.magnitude(axis)
			if m == 0 then return { 1, 0, 0, 0, 1, 0, 0, 0, 1 } end
			local x, y, z = axis.x / m, axis.y / m, axis.z / m
			local c, s_ = math.cos(angle), math.sin(angle)
			local t = 1 - c
			return { t * x * x + c, t * x * y - s_ * z, t * x * z + s_ * y, t * x * y + s_ * z, t * y * y + c, t * y * z - s_ * x,
				t * x * z - s_ * y, t * y * z + s_ * x, t * z * z + c }
		end
		local function fromColumns(pos, right, up, back)
			return cf(pos, { right.x, up.x, back.x, right.y, up.y, back.y, right.z, up.z, back.z })
		end
		local function look(pos, dir, upv)
			upv = isvec(upv) and upv or create(0, 1, 0)
			if not isvec(dir) or vec.magnitude(dir) == 0 then return cf(pos) end
			local back = -vec.normalize(dir)
			local right = vec.cross(upv, back)
			if vec.magnitude(right) == 0 then right = create(1, 0, 0) else right = vec.normalize(right) end
			return fromColumns(pos, right, vec.cross(back, right), back)
		end
		local function eulerXYZ(x, y, z) return mulmat(mulmat(rx(num(x)), ry(num(y))), rz(num(z))) end
		local function eulerYXZ(x, y, z) return mulmat(mulmat(ry(num(y)), rx(num(x))), rz(num(z))) end
		CF.new = named("CFrame.new", function(...)
			local n = select("#", ...)
			local a, b = ...
			if n == 0 then return cf() end
			if isvec(a) and isvec(b) then return look(a, b - a) end
			if isvec(a) then return cf(a) end
			if n == 3 then return cf(create(num(a), num(b), num((select(3, ...))))) end
			if n == 7 then
				local x, y, z, qx, qy, qz, qw = ...
				local m = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
				if m == 0 then return cf(create(x, y, z)) end
				qx, qy, qz, qw = qx / m, qy / m, qz / m, qw / m
				return cf(create(x, y, z), { 1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw),
					2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw),
					2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy) })
			end
			if n == 12 then
				local t = { ... }
				return cf(create(t[1], t[2], t[3]), { unpack(t, 4, 12) })
			end
			error("Invalid number of arguments: " .. n, 2)
		end)
		CF.identity = cf()
		CF.Angles = named("CFrame.Angles", function(x, y, z) return cf(nil, eulerXYZ(x, y, z)) end)
		CF.fromEulerAnglesXYZ = named("CFrame.fromEulerAnglesXYZ", function(x, y, z) return cf(nil, eulerXYZ(x, y, z)) end)
		CF.fromEulerAnglesYXZ = named("CFrame.fromEulerAnglesYXZ", function(x, y, z) return cf(nil, eulerYXZ(x, y, z)) end)
		CF.fromOrientation = named("CFrame.fromOrientation", function(x, y, z) return cf(nil, eulerYXZ(x, y, z)) end)
		CF.fromEulerAngles = named("CFrame.fromEulerAngles", function(x, y, z) return cf(nil, eulerXYZ(x, y, z)) end)
		CF.fromAxisAngle = named("CFrame.fromAxisAngle", function(axis, angle) return cf(nil, axisAngle(axis, num(angle))) end)
		CF.fromMatrix = named("CFrame.fromMatrix", function(pos, vx, vy, vz)
			if not isvec(vz) then vz = vec.cross(vx, vy) end
			return fromColumns(pos, vx, vy, vz)
		end)
		CF.lookAt = named("CFrame.lookAt", function(at, target, upv) return look(at, target - at, upv) end)
		CF.lookAlong = named("CFrame.lookAlong", function(at, dir, upv) return look(at, dir, upv) end)
		CF.fromRotationBetweenVectors = named("CFrame.fromRotationBetweenVectors", function(a, b)
			local axis = vec.cross(a, b)
			local ma, mb = vec.magnitude(a), vec.magnitude(b)
			if ma == 0 or mb == 0 or vec.magnitude(axis) == 0 then return cf() end
			return cf(nil, axisAngle(axis, math.acos(math.clamp(vec.dot(a, b) / (ma * mb), -1, 1))))
		end)
		local cfm = {}
		local function toXYZ(r)
			local y = math.asin(math.clamp(r[3], -1, 1))
			return math.atan2(-r[6], r[9]), y, math.atan2(-r[2], r[1])
		end
		local function toYXZ(r)
			local x = math.asin(math.clamp(-r[6], -1, 1))
			return x, math.atan2(r[3], r[9]), math.atan2(r[4], r[5])
		end
		local function toAxis(r)
			local angle = math.acos(math.clamp((r[1] + r[5] + r[9] - 1) / 2, -1, 1))
			local axis = create(r[8] - r[6], r[3] - r[7], r[4] - r[2])
			if vec.magnitude(axis) == 0 then return create(1, 0, 0), angle end
			return vec.normalize(axis), angle
		end
		cfm.Inverse = function(a) local t = transpose(a.r) return cf(-rotvec(t, a.p), t) end
		cfm.Orthonormalize = function(a) return cf(a.p, a.r) end
		cfm.Lerp = function(a, b, t)
			local rel = mulmat(transpose(a.r), b.r)
			local axis, angle = toAxis(rel)
			return cf(a.p + (b.p - a.p) * t, mulmat(a.r, axisAngle(axis, angle * t)))
		end
		cfm.ToWorldSpace = function(a, b) return cf(a.p + rotvec(a.r, b.p), mulmat(a.r, b.r)) end
		cfm.ToObjectSpace = function(a, b) return cfm.ToWorldSpace(cfm.Inverse(a), b) end
		cfm.PointToWorldSpace = function(a, v) return a.p + rotvec(a.r, v) end
		cfm.PointToObjectSpace = function(a, v) return rotvec(transpose(a.r), v - a.p) end
		cfm.VectorToWorldSpace = function(a, v) return rotvec(a.r, v) end
		cfm.VectorToObjectSpace = function(a, v) return rotvec(transpose(a.r), v) end
		cfm.ToEulerAnglesXYZ = function(a) return toXYZ(a.r) end
		cfm.ToEulerAnglesYXZ = function(a) return toYXZ(a.r) end
		cfm.ToOrientation = function(a) return toYXZ(a.r) end
		cfm.ToEulerAngles = function(a) return toXYZ(a.r) end
		cfm.GetComponents = function(a) return a.p.x, a.p.y, a.p.z, unpack(a.r, 1, 9) end
		cfm.components = cfm.GetComponents
		cfm.ToAxisAngle = function(a) return toAxis(a.r) end
		cfm.FuzzyEq = function(a, b, eps)
			eps = eps or 1e-5
			if vec.magnitude(a.p - b.p) > eps then return false end
			for i = 1, 9 do
				if math.abs(a.r[i] - b.r[i]) > eps then return false end
			end
			return true
		end
		cfm.AngleBetween = function(a, b) return (select(2, toAxis(mulmat(transpose(a.r), b.r)))) end
		for k, f in pairs(cfm) do
			cfm[k] = named(k, function(self, ...)
				if not iscf(self) then error("Expected ':' not '.' calling member function " .. k, 2) end
				return f(self, ...)
			end)
		end
		cfMT.__index = function(a, k)
			if k == "Position" or k == "p" then return a.p end
			if k == "Rotation" then return cf(nil, a.r) end
			if k == "X" then return a.p.x elseif k == "Y" then return a.p.y elseif k == "Z" then return a.p.z end
			if k == "RightVector" or k == "XVector" then return create(a.r[1], a.r[4], a.r[7]) end
			if k == "UpVector" or k == "YVector" then return create(a.r[2], a.r[5], a.r[8]) end
			if k == "ZVector" then return create(a.r[3], a.r[6], a.r[9]) end
			if k == "LookVector" then return create(-a.r[3], -a.r[6], -a.r[9]) end
			local m = cfm[k]
			if m ~= nil then return m end
			error(tostring(k) .. " is not a valid member of CFrame", 2)
		end
		cfMT.__mul = function(a, b)
			if iscf(a) and iscf(b) then return cfm.ToWorldSpace(a, b) end
			if iscf(a) and isvec(b) then return a.p + rotvec(a.r, b) end
			error("attempt to perform arithmetic (mul) on CFrame", 2)
		end
		cfMT.__add = function(a, b) return cf(a.p + b, a.r) end
		cfMT.__sub = function(a, b) return cf(a.p - b, a.r) end
		cfMT.__eq = function(a, b) return cfm.FuzzyEq(a, b, 0) end
		cfMT.__tostring = function(a) return table.concat({ cfm.GetComponents(a) }, ", ") end
		cfMT.__metatable = "The metatable is locked"
			seal(cfMT)
		M.CFrame = CF
	end

	-- task
	do
		local task = {}
		local queue = {}
		-- Roblox's scheduler resumes threads without a parent, so a spawned thread has the whole
		-- C stack budget; coroutine.resume would pass on the depth of the caller (scripts measure it).
		local resume = G.resumetop or coroutine.resume
		-- A value that is neither a function nor a thread: the engine's argument error
		-- (luaL_argerror form, no "got <type>").  Luraph's start-up code calls spawn, defer and delay
		-- without arguments inside xpcall; before this the message was that of the mock's own
		-- coroutine.create with a position inside rbxmock.lua.
		local function run(f, ...)
			if type(f) == "thread" then
				resume(f, ...)
				return f
			end
			if type(f) ~= "function" then error("invalid argument #1 to 'spawn' (function or thread expected)", 2) end
			local co = coroutine.create(f)
			resume(co, ...)
			return co
		end
		task.spawn = named("task.spawn", run)
		task.defer = named("task.defer", function(f, ...)
			if type(f) ~= "thread" and type(f) ~= "function" then
				error("invalid argument #1 to 'defer' (function or thread expected)", 2)
			end
			local co = type(f) == "thread" and f or coroutine.create(f)
			queue[#queue + 1] = { co, ... }
			return co
		end)
		task.delay = named("task.delay", function(t, f, ...)
			if t ~= nil and rawType(t) ~= "number" then
				error("invalid argument #1 to 'delay' (number expected, got " .. rawTypeof(t) .. ")", 2)
			end
			if type(f) ~= "thread" and type(f) ~= "function" then
				error("invalid argument #2 to 'delay' (function or thread expected)", 2)
			end
			return type(f) == "thread" and f or coroutine.create(f)
		end)
		-- What a wait returns is the time that really passed (documentation of task.wait and wait): at
		-- least what was asked for, at least one step of the scheduler (task.wait resumes on the next
		-- Heartbeat, the legacy wait on its 30 Hz step), and in practice never the same number twice.
		-- A key-system loader compares the results of wait() and task.wait() and takes equal ones for an
		-- emulator.  Nothing really waits here, so the number is made: the asked time or one step, plus
		-- a part of a step that changes from call to call (a fixed sequence, so that runs repeat).
		local waits = 0
		local function waited(t, step)
			waits += 1
			return math.max(t or 0, step) + (waits * 7919 % 997) / 997 * step * 0.25
		end
		local function waiter(step, legacy)
			return function(co, t)
				if t ~= nil and rawType(t) ~= "number" then
					error("invalid argument #1 to 'wait' (number expected, got " .. rawTypeof(t) .. ")", 2)
				end
				if M.mainThread and co ~= M.mainThread then
					-- a coroutine of the script: really yield (it shows as "suspended" until the scheduler,
					-- i.e. the next wait of the main thread, resumes it)
					queue[#queue + 1] = { co, waited(t, step), legacy and os.clock() or nil }
					return YIELD
				end
				local q = queue
				queue = {}
				for _, item in ipairs(q) do
					if coroutine.status(item[1]) == "suspended" then resume(unpack(item)) end
				end
				local w = waited(t, step)
				-- (what the main thread waited, for the harness's watch on a run that does not end)
				if M.onWait then M.onWait(legacy and "wait()" or "task.wait()", w) end
				if legacy then return w, os.clock() end       -- (elapsed, elapsedTime())
				return w
			end
		end
		local waitImpl = waiter(1 / 30, true)
		task.wait = namedY("task.wait", waiter(1 / 60, false))
		task.cancel = named("task.cancel", function(...)
			if select("#", ...) == 0 then error("missing argument #1 to 'cancel' (thread expected)", 2) end
			local co = ...
			if rawType(co) ~= "thread" then
				error("invalid argument #1 to 'cancel' (thread expected, got " .. rawTypeof(co) .. ")", 2)
			end
			if type(co) == "thread" and coroutine.status(co) == "suspended" then coroutine.close(co) end
		end)
		task.synchronize = named("task.synchronize", function() end)
		task.desynchronize = named("task.desynchronize", function() end)
		libnames[task] = "task"
		M.task = task
		M.wait = namedY("wait", waitImpl)
		M.spawn = named("spawn", function(f, ...)
			if type(f) ~= "thread" and type(f) ~= "function" then
				error("invalid argument #1 to 'spawn' (function or thread expected)", 2)
			end
			return run(f, ...)
		end)
		M.delay = named("delay", function() end)
	end

	-- debug: Roblox adds profiling / memory-category helpers to Luau's info and traceback
	do
		local d = {}
		for k, v in pairs(G.debug) do d[k] = v end
		local category = "Default"
		d.profilebegin = named("debug.profilebegin", function() end)
		d.profileend = named("debug.profileend", function() end)
		d.getmemorycategory = named("debug.getmemorycategory", function() return category end)
		d.setmemorycategory = named("debug.setmemorycategory", function(c)
			local old = category
			category = tostring(c)
			return old
		end)
		d.resetmemorycategory = named("debug.resetmemorycategory", function() category = "Default" end)
		libnames[d] = "debug"
		M.debug = d
	end

	-- tick(): seconds since the Unix epoch with a fraction (documentation; local time, taken as UTC here).
	-- A key-system loader requires tick() and os.time() to be later than a date it carries.
	do
		local t0, c0 = os.time(), os.clock()
		M.tick = named("tick", function() return t0 + (os.clock() - c0) end)
	end
	M.time = named("time", os.clock)
	M.elapsedTime = named("elapsedTime", os.clock)
	-- print / warn: nothing is shown, but what the script said goes into the run's log
	local function said(kind)
		return function(...)
			local n = select("#", ...)
			if n == 0 then return end
			local parts = table.pack(...)
			for i = 1, n do
				local okt, text = pcall(tostring, parts[i])
				parts[i] = okt and rawType(text) == "string" and text or "?"
			end
			local text = string.gsub(string.sub(table.concat(parts, " ", 1, n), 1, 300), "[%c]", " ")
			note("script", kind .. ": " .. text, true)
			if M.onSay then M.onSay() end
		end
	end
	M.warn = named("warn", said("warn"))
	M.print = named("print", said("print"))
	-- What was noted during the run and not written yet, for the end of the run (harness.lua):
	-- how often the script said a line it said more than once, then one line each for the undefined
	-- globals it read and for the permissive stand-ins it was given.
	P.flush = function()
		if not ewrite then return end
		local globals, stubs, nsaid = {}, {}, 0
		for _, line in ipairs(P.notes) do
			local g = string.match(line, "^%[script%] undefined global read: (.*)$")
			local st = string.match(line, "^%[stub%] (%S+)")
			if st then st = string.gsub(st, ":$", "") end
			if g then
				globals[#globals + 1] = g
			elseif st then
				stubs[#stubs + 1] = st
			elseif string.sub(line, 1, 8) == "[script]" and P.counts[line] > 1 then
				nsaid += 1
				if nsaid <= 60 then
					ewrite("[script] said " .. P.counts[line] .. " times: " .. string.sub(line, 10) .. "\n")
				end
			end
		end
		if nsaid > 60 then ewrite("[script] ... " .. (nsaid - 60) .. " more repeated print / warn lines\n") end
		local function list(t)
			local more = #t > 40 and (", ... " .. (#t - 40) .. " more") or ""
			return table.concat(t, ", ", 1, math.min(#t, 40)) .. more
		end
		if #globals > 0 then ewrite("[script] undefined globals read (nil, as in Roblox): " .. list(globals) .. "\n") end
		if #stubs > 0 then
			ewrite("[stub] permissive stand-ins handed out where the mock has no Roblox answer: " .. list(stubs) .. "\n")
		end
		P.notes = {}
	end
	-- the capitalised spellings Roblox still has of its old global functions
	M.Wait, M.Spawn, M.Delay, M.ElapsedTime = M.wait, M.spawn, M.delay, M.elapsedTime
	M.stats = named("stats", function() return methods.GetService(game, "Stats") end)
	M.Stats = M.stats
	-- (the client's version string; this one is made up in the engine's format)
	M.version = named("version", function() return "0.650.0.6500692" end)
	M.Version = M.version
	M.settings = named("settings", function() return stub("settings") end)
	M.UserSettings = named("UserSettings", function() return stub("UserSettings") end)
	M.shared = {}
	libnames[M.shared] = "shared"
	M.typeof = named("typeof", function(...)
		if select("#", ...) == 0 then error("missing argument #1", 2) end
		local v = ...
		local tn = typeNames[v]
		if tn then return tn end
		tn = rawTypeof(v)
		if tn == "vector" and M.vectorTypeName then return M.vectorTypeName end
		return tn
	end)
	M.type = named("type", function(...)
		if select("#", ...) == 0 then error("missing argument #1", 2) end
		local v = ...
		if getmetatable(v) == stubMT then return "userdata" end
		return rawType(v)
	end)
	M.typeNames = typeNames

	-- math_floor_precision: not a Roblox or executor global.  The ZekeHub/Luarmor build
	-- (loadstring6) reads it from the environment, so an earlier loader stage must define it, and
	-- pushes (math_floor_precision(seed) - expected(seed)) % 2^24 into its key six times, with a
	-- seed that differs from run to run (iteration order of fresh tables used as keys).  The key
	-- is only stable if the real function equals the script's own keyed permutation of the seed
	-- plus a constant; that function is not in the file.  This stand-in returns its argument, so
	-- the key comes out wrong and the payload does not decrypt; every call is reported.
	M.mfpCalls = 0
	M.math_floor_precision = named("math_floor_precision", function(v)
		M.mfpCalls += 1
		if M.onMfp then M.onMfp(M.mfpCalls, v) end
		if M.mfpDelta == 99999999 then error("attempt to call a nil value", 0) end
		if M.mfpDelta then
			return setmetatable({}, { __sub = function(_, expected)
				if M.onMfpExpected then M.onMfpExpected(M.mfpCalls, v, expected) end
				return M.mfpDelta
			end })
		end
		return v
	end)

	return M
end
