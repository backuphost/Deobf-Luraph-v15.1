-- Minimal Roblox runtime mock for the devm harness.
-- It only has to be faithful enough for Luraph's bootstrap (environment checks) to run through
-- without throwing or hanging; it is not a general purpose emulator.
-- usage: local globals = mock(realGlobals, fnames, libnames)
local rawTypeof, rawType = typeof, type
return function(G, fnames, libnames)
	local M = {}
	local typeNames = setmetatable({}, { __mode = "k" })

	local function named(name, f)
		fnames[f] = name
		return f
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
			state.Disconnect = function()
				state.Connected = false
				conns[c] = nil
			end
			cmt.__index = function(_, k) return state[k] end
			cmt.__type = "RBXScriptConnection"
			cmt.__tostring = function() return "Connection" end
			cmt.__metatable = "The metatable is locked"
			return c
		end
		methods.connect = methods.Connect
		methods.Once = methods.Connect
		methods.ConnectParallel = methods.Connect
		methods.Wait = function()
			-- inside a coroutine of the script the wait really suspends it (nothing ever fires here)
			if M.mainThread and coroutine.running() ~= M.mainThread and coroutine.isyieldable() then
				return coroutine.yield()
			end
			return nil
		end
		local function make()
			local sig = newproxy(true)
			local smt = getmetatable(sig)
			typeNames[sig] = "RBXScriptSignal"
			sigCore[sig] = core
			smt.__index = function(_, k)
				local m = methods[k]
				if m then return function(...) return m(...) end end
				return nil
			end
			smt.__eq = sigEq
			smt.__type = "RBXScriptSignal"
			smt.__tostring = function() return "Signal " .. name end
			smt.__metatable = "The metatable is locked"
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
	m("IsClient", function() return true end)
	m("IsServer", function() return false end)
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
	data[game].props.JobId = ""
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

	M.game = game
	M.Game = game
	M.workspace = workspace
	M.Workspace = workspace
	M.script = script

	-- ------------------------------------------------------------ libraries
	local function lib(name, tname)
		local l
		l = setmetatable({}, {
			__index = function(t, k)
				if type(k) ~= "string" then return nil end
				local full = name .. "." .. k
				local f = function() return stub(full .. "()", tname or name) end
				fnames[f] = full
				rawset(t, k, f)
				return f
			end,
		})
		libnames[l] = name
		return l
	end
	M.lib = lib

	local Instance = lib("Instance")
	rawset(Instance, "new", named("Instance.new", function(class, parent)
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
	for k, f in pairs(vec2Methods) do named(k, f) end
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
		-- A segment is the list of its control positions: 4 for a cubic, 3 when one of the two
		-- handles is zero (Roblox then drops it: the segment is a quadratic), 2 when both are.
		local function isZero(u)
			return u.X.Scale == 0 and u.X.Offset == 0 and u.Y.Scale == 0 and u.Y.Offset == 0
		end
		local function segments(self)
			local pts = data[self].points or {}
			local segs = {}
			for i = 1, #pts - 1 do
				local a, b = pts[i], pts[i + 1]
				local seg = { a.Position }
				if not isZero(a.RightTangent) then seg[#seg + 1] = a.Position + a.RightTangent end
				if not isZero(b.LeftTangent) then seg[#seg + 1] = b.Position + b.LeftTangent end
				seg[#seg + 1] = b.Position
				segs[i] = seg
			end
			return segs
		end
		local function locate(segs, t)
			local n = #segs
			if n == 0 then return nil end
			local x = math.clamp(t, 0, 1) * n
			local i = math.min(math.floor(x), n - 1)
			return segs[i + 1], x - i
		end
		-- de Casteljau on plain numbers
		local function bez(p, u)
			local q = table.clone(p)
			for level = #q - 1, 1, -1 do
				for i = 1, level do q[i] = q[i] + (q[i + 1] - q[i]) * u end
			end
			return q[1]
		end
		local function dbez(p, u)
			local n = #p - 1
			local d = {}
			for i = 1, n do d[i] = n * (p[i + 1] - p[i]) end
			return bez(d, u)
		end
		local function px(self, seg)
			local w, h = absSize(self)
			local xs, ys = {}, {}
			for i = 1, #seg do
				xs[i] = seg[i].X.Scale * w + seg[i].X.Offset
				ys[i] = seg[i].Y.Scale * h + seg[i].Y.Offset
			end
			return xs, ys
		end
		-- positions come back as pure-scale UDim2s: the pixel position divided by the parent size
		-- (checked against the key material of real samples)
		local function posAt(self, seg, u)
			local w, h = absSize(self)
			local xs, ys = px(self, seg)
			local x, y = bez(xs, u), bez(ys, u)
			return udim2(w ~= 0 and x / w or 0, 0, h ~= 0 and y / h or 0, 0)
		end
		local function tanAt(self, seg, u)
			local xs, ys = px(self, seg)
			return vec2(dbez(xs, u), dbez(ys, u))
		end
		local STEPS = 2000
		local function segLength(self, seg, upto)
			local xs, ys = px(self, seg)
			local total = 0
			local lx, ly = xs[1], ys[1]
			for i = 1, STEPS do
				local u = (upto or 1) * i / STEPS
				local x, y = bez(xs, u), bez(ys, u)
				total += math.sqrt((x - lx) ^ 2 + (y - ly) ^ 2)
				lx, ly = x, y
			end
			return total
		end
		local function locateArc(self, segs, t)
			local lens, total = {}, 0
			for i, seg in ipairs(segs) do
				lens[i] = segLength(self, seg)
				total += lens[i]
			end
			local target = math.clamp(t, 0, 1) * total
			for i, seg in ipairs(segs) do
				if target <= lens[i] or i == #segs then
					local lo, hi = 0, 1
					for _ = 1, 40 do
						local mid = (lo + hi) / 2
						if segLength(self, seg, mid) < target then lo = mid else hi = mid end
					end
					return seg, (lo + hi) / 2
				end
				target -= lens[i]
			end
			return nil
		end
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
		m("GetLength", function(self)
			local total = 0
			for _, seg in ipairs(segments(self)) do total += segLength(self, seg) end
			return total
		end)
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
			props.IsA = named("IsA", function(_, n) return n == etName end)
			mt.__index = function(_, k) return props[k] end
			mt.__tostring = function() return "Enum." .. etName .. "." .. name end
			mt.__type = "EnumItem"
			mt.__metatable = "The metatable is locked"
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
			ms.GetEnumItems = named("GetEnumItems", function()
				local o = {}
				for _, it in pairs(items) do o[#o + 1] = it end
				return o
			end)
			ms.FromName = named("FromName", function(_, name) return item(name) end)
			ms.FromValue = named("FromValue", function(_, v) return byValue[v] or item("Value" .. tostring(v), v) end)
			mt.__index = function(_, k)
				if ms[k] then return ms[k] end
				if type(k) ~= "string" then return nil end
				return item(k)
			end
			mt.__tostring = function() return etName end
			mt.__type = "Enum"
			mt.__metatable = "The metatable is locked"
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
			return r, draw, function(h, l) hi, lo = h, l end, function() return hi, lo end
		end
		rawset(Random, "new", named("Random.new", function(seed)
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
		for k, f in pairs(methods) do named(k, f) end
		G.setvectormetatable({
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
		})
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
		L.FromAxis = named("Vector3.FromAxis", fromEnum)
		L.FromNormalId = named("Vector3.FromNormalId", fromEnum)
		L.fromAxis, L.fromNormalId = L.FromAxis, L.FromNormalId
		M.Vector3 = L
		M.vectorTypeName = "Vector3"
	end

	-- task
	do
		local task = {}
		local queue = {}
		local function run(f, ...)
			if type(f) == "thread" then
				coroutine.resume(f, ...)
				return f
			end
			local co = coroutine.create(f)
			coroutine.resume(co, ...)
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
		task.wait = named("task.wait", function(t)
			local co = coroutine.running()
			if M.mainThread and co ~= M.mainThread and coroutine.isyieldable() then
				-- a coroutine of the script: really yield (it shows as "suspended" until the scheduler,
				-- i.e. the next wait of the main thread, resumes it)
				queue[#queue + 1] = { co, t or 0.03 }
				return coroutine.yield()
			end
			local q = queue
			queue = {}
			for _, item in ipairs(q) do
				if coroutine.status(item[1]) == "suspended" then coroutine.resume(unpack(item)) end
			end
			return t or 0.03
		end)
		task.cancel = named("task.cancel", function(co)
			if type(co) == "thread" and coroutine.status(co) == "suspended" then coroutine.close(co) end
		end)
		task.synchronize = named("task.synchronize", function() end)
		task.desynchronize = named("task.desynchronize", function() end)
		libnames[task] = "task"
		M.task = task
		M.wait = named("wait", task.wait)
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
	M.typeof = named("typeof", function(v)
		local tn = typeNames[v]
		if tn then return tn end
		tn = rawTypeof(v)
		if tn == "vector" and M.vectorTypeName then return M.vectorTypeName end
		return tn
	end)
	M.type = named("type", function(v)
		if getmetatable(v) == stubMT then return "userdata" end
		return rawType(v)
	end)
	M.typeNames = typeNames

	-- Executor-specific globals probed by some scripts to fingerprint the host environment.
	-- Providing passthrough stubs prevents nil-call crashes while keeping the analysis running.
	-- math_floor_precision: ZekeHub executor precision helper used in key-derivation routines.
	M.math_floor_precision = named("math_floor_precision", function(v, p)
		local factor = 10 ^ (p or 0)
		return math.floor(v * factor + 0.5) / factor
	end)

	return M
end
