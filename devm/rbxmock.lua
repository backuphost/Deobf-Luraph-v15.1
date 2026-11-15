-- Minimal Roblox runtime mock for the devm harness.
-- It only has to be faithful enough for Luraph's bootstrap (environment checks) to run through
-- without throwing or hanging; it is not a general purpose emulator.
-- usage: local globals = mock(realGlobals, fnames, libnames)
local rawTypeof, rawType = typeof, type
return function(G, fnames, libnames)
	local M = {}
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
	local function logged(name, f)
		if not calllog then return f end
		return function(...) calllog(name, ...) return f(...) end
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

	-- ------------------------------------------------------------ permissive value stub
	local stubMT = {}
	local function stub(name, tname)
		local t = setmetatable({}, stubMT)
		typeNames[t] = tname or "userdata"
		rawset(t, "__name", name)
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

	function newInstance(class, name)
		local u = newproxy(true)
		local mt = getmetatable(u)
		local d = { props = { ClassName = class, Name = name or class, Archivable = true }, children = {}, signals = {}, attrs = {} }
		data[u] = d
		typeNames[u] = "Instance"
		mt.__index = function(_, k)
			local v = d.props[k]
			if v ~= nil then return v end
			if k == "Parent" then return nil end
			if signalProps[k] then return getSignal(d, k).make() end
			v = methods[k]
			if v ~= nil then return v end
			for _, c in ipairs(d.children) do
				if data[c].props.Name == k then return c end
			end
			if class == "DataModel" and type(k) == "string" and services[k] then return services[k] end
			if type(k) ~= "string" then return nil end
			-- unknown member: hand out a permissive stub so the script keeps going
			v = stub(d.props.Name .. "." .. k)
			d.props[k] = v
			return v
		end
		mt.__newindex = function(_, k, v)
			if k == "Parent" then
				setParent(u, v)
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
		return c == cls or cls == "Instance"
	end)
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
	m("ClearAllChildren", function(self)
		for _, c in ipairs({ unpack(data[self].children) }) do methods.Destroy(c) end
	end)
	m("Clone", function(self)
		local d = data[self]
		local c = newInstance(d.props.ClassName, d.props.Name)
		for k, v in pairs(d.props) do
			if k ~= "Parent" then data[c].props[k] = v end
		end
		for _, ch in ipairs(d.children) do setParent(methods.Clone(ch), c) end
		return c
	end)
	m("GetFullName", function(self)
		local parts = {}
		local x = self
		while x and data[x] and data[x].props.ClassName ~= "DataModel" do
			table.insert(parts, 1, data[x].props.Name)
			x = data[x].props.Parent
		end
		return table.concat(parts, ".")
	end)
	m("GetPropertyChangedSignal", function(self, prop) return getSignal(data[self], "Changed:" .. tostring(prop)).make() end)
	m("GetAttribute", function(self, k) return data[self].attrs[k] end)
	m("SetAttribute", function(self, k, v) data[self].attrs[k] = v end)
	m("GetAttributes", function(self)
		local o = {}
		for k, v in pairs(data[self].attrs) do o[k] = v end
		return o
	end)
	m("GetAttributeChangedSignal", function(self, k) return getSignal(data[self], "Attr:" .. tostring(k)).make() end)
	local game
	m("GetService", function(self, name)
		local s = services[name]
		if not s then
			s = newInstance(name, name)
			services[name] = s
			setParent(s, game)
		end
		return s
	end)
	methods.FindService = methods.GetService
	methods.service = methods.GetService
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
	m("GenerateGUID", function() return "00000000-0000-0000-0000-000000000000" end)
	m("JSONEncode", function() return "{}" end)
	m("JSONDecode", function() return {} end)
	m("GetPlayers", function() return {} end)
	m("BindToClose", function() end)
	m("FireServer", function() end)
	m("FireClient", function() end)
	m("FireAllClients", function() end)
	m("Fire", function() end)
	m("InvokeServer", function() return nil end)
	m("Kick", function() end)

	game = newInstance("DataModel", "Game")
	local workspace = newInstance("Workspace", "Workspace")
	services.Workspace = workspace
	setParent(workspace, game)
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
	}
	local function lib(name, tname)
		local l = {}
		libnames[l] = name
		for _, k in ipairs(known[name] or { "new" }) do
			local full = name .. "." .. k
			l[k] = named(full, function() return stub(full .. "()", tname or name) end)
		end
		return l
	end
	M.lib = lib

	-- a plain table like the real one: scripts test getmetatable(Instance) == nil
	local Instance = {}
	libnames[Instance] = "Instance"
	rawset(Instance, "new", named("Instance.new", function(class, parent)
		if class == nil then error("Argument 1 missing or nil", 2) end
		if rawType(class) ~= "string" then error("invalid argument #1 to 'new' (string expected, got " .. rawTypeof(class) .. ")", 2) end
		local i = newInstance(class)
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

	-- Path2D: a chain of cubic Beziers. Segment i runs from point i to point i+1 with handles
	-- pos[i] + RightTangent[i] and pos[i+1] + LeftTangent[i+1]; t in [0,1] spans all segments evenly.
	do
		local function absSize(inst)
			local p = data[inst].props.Parent
			while p and data[p] do
				local sz = data[p].props.Size
				if typeNames[sz] == "UDim2" then
					local pw, ph = absSize(p)
					return sz.X.Scale * pw + sz.X.Offset, sz.Y.Scale * ph + sz.Y.Offset
				end
				p = data[p].props.Parent
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
		local function segments(self)
			local W, H = absSize(self)
			local pts = {}
			for i, cp in ipairs(data[self].points or {}) do
				local px, py = resolve(cp.Position, W, H)
				local lx, ly = resolve(cp.LeftTangent, W, H)
				local rx, ry = resolve(cp.RightTangent, W, H)
				pts[i] = { px, py, lx, ly, rx, ry }
			end
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
		m("GetLength", function(self) return length(segments(self)) end)
		m("GetPositionOnCurve", function(self, t)
			local seg, u = locate(segments(self), t)
			if not seg then return udim2(0, 0, 0, 0) end
			return posAt(self, seg, u)
		end)
		m("GetTangentOnCurve", function(self, t)
			local seg, u = locate(segments(self), t)
			if not seg then return vec2(0, 0) end
			return tanAt(self, seg, u)
		end)
		m("GetPositionOnCurveArcLength", function(self, t)
			local seg, u = locateArc(self, segments(self), t)
			if not seg then return udim2(0, 0, 0, 0) end
			return posAt(self, seg, u)
		end)
		m("GetTangentOnCurveArcLength", function(self, t)
			local seg, u = locateArc(self, segments(self), t)
			if not seg then return vec2(0, 0) end
			return tanAt(self, seg, u)
		end)
	end

	-- Enum
	do
		local enumTypes = {}
		local function enumItem(et, etName, name, value)
			local u = newproxy(true)
			local mt = getmetatable(u)
			typeNames[u] = "EnumItem"
			local props = { Name = name, Value = value, EnumType = et }
			props.IsA = named("IsA", function(self, n)
				if self ~= u then error("Expected ':' not '.' calling member function IsA", 2) end
				return n == etName
			end)
			mt.__index = function(_, k) return props[k] end
			mt.__tostring = function() return "Enum." .. etName .. "." .. name end
			mt.__type = "EnumItem"
			mt.__metatable = "The metatable is locked"
			seal(mt)
			return u
		end
		local function enumType(etName)
			local et = enumTypes[etName]
			if et then return et end
			et = newproxy(true)
			enumTypes[etName] = et
			local mt = getmetatable(et)
			typeNames[et] = "Enum"
			local items, byValue, n = {}, {}, 0
			local function item(name, value)
				local it = items[name]
				if not it then
					if value == nil then
						value = n
					end
					n += 1
					it = enumItem(et, etName, name, value)
					items[name] = it
					if byValue[value] == nil then byValue[value] = it end
				end
				return it
			end
			if etName == "PartType" then
				item("Ball", 0) item("Block", 1) item("Cylinder", 2)
			end
			local ms = {}
			ms.GetEnumItems = named("GetEnumItems", function(self)
				if self ~= et then error("Expected ':' not '.' calling member function GetEnumItems", 2) end
				local o = {}
				for _, it in pairs(items) do o[#o + 1] = it end
				return o
			end)
			ms.FromName = named("FromName", function(self, name)
				if self ~= et then error("Expected ':' not '.' calling member function FromName", 2) end
				if rawType(name) ~= "string" then error("Argument 1 missing or nil", 2) end
				return item(name)
			end)
			ms.FromValue = named("FromValue", function(self, v)
				if self ~= et then error("Expected ':' not '.' calling member function FromValue", 2) end
				if rawType(v) ~= "number" then error("Argument 1 missing or nil", 2) end
				return byValue[v] or item("Value" .. tostring(v), v)
			end)
			mt.__index = function(_, k)
				if ms[k] then return ms[k] end
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
		local ms = { GetEnums = named("GetEnums", function() return {} end) }
		mt.__index = function(_, k)
			if ms[k] then return ms[k] end
			if type(k) ~= "string" then return nil end
			return enumType(k)
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
		local function v3(x, y, z) return create(x or 0, y or 0, z or 0) end
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
		local function run(f, ...)
			if type(f) == "thread" then
				resume(f, ...)
				return f
			end
			local co = coroutine.create(f)
			resume(co, ...)
			return co
		end
		task.spawn = named("task.spawn", run)
		task.defer = named("task.defer", function(f, ...)
			local co = type(f) == "thread" and f or coroutine.create(f)
			queue[#queue + 1] = { co, ... }
			return co
		end)
		task.delay = named("task.delay", function(_, f, ...)
			return type(f) == "thread" and f or coroutine.create(f)
		end)
		local function waitImpl(co, t)
			if t ~= nil and rawType(t) ~= "number" then
				error("invalid argument #1 to 'wait' (number expected, got " .. rawTypeof(t) .. ")", 2)
			end
			if M.mainThread and co ~= M.mainThread then
				-- a coroutine of the script: really yield (it shows as "suspended" until the scheduler,
				-- i.e. the next wait of the main thread, resumes it)
				queue[#queue + 1] = { co, t or 0.03 }
				return YIELD
			end
			local q = queue
			queue = {}
			for _, item in ipairs(q) do
				if coroutine.status(item[1]) == "suspended" then resume(unpack(item)) end
			end
			return t or 0.03
		end
		task.wait = namedY("task.wait", waitImpl)
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
		M.spawn = named("spawn", function(f, ...) return run(f, ...) end)
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

	M.tick = named("tick", os.clock)
	M.time = named("time", os.clock)
	M.elapsedTime = named("elapsedTime", os.clock)
	M.warn = named("warn", function() end)
	M.print = named("print", function() end)
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
