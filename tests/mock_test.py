#!/usr/bin/env python3
"""The mock Roblox environment of the devm harness answers like Roblox (devm/rbxmock.lua, the
environment side of devm/harness.lua).

    python3 tests/mock_test.py [-v] [Name ...]

Hand-written scripts are run through the harness and what they observe is compared with what
the engine answers: enum items and values, instance methods and their errors, tags, undefined
globals, task argument errors, GUI layout numbers worked out by hand from the rules in
rbxmock.lua ("GUI layout"), EncodingService:DecompressBuffer.  No corpus file is needed.

MOCK_DEVM_DIR: a directory with harness.lua / rbxmock.lua to test instead of devm/ (the API table
is read from <dir>/../deobf/roblox_api.luau).
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEVM = os.environ.get('MOCK_DEVM_DIR') or os.path.join(ROOT, 'devm')
sys.path.insert(0, ROOT)


def find_luau():
    from devm import vmmodel
    try:
        return vmmodel.find_luau()
    except Exception:
        return None


LUAU = find_luau()

PRELUDE = '''
local function show(tag, ...)
	local t = table.pack(...)
	for i = 1, t.n do t[i] = tostring(t[i]) end
	ewrite("[t] " .. tag .. "=" .. table.concat(t, "|") .. "\\n")
end
local function err(f, ...) return (select(2, pcall(f, ...))) end
local function r2(x) return math.floor(x * 100 + 0.5) / 100 end
local function v2(v) return r2(v.X) .. "," .. r2(v.Y) end
'''


def run(script, *options):
    """Run a script in the harness; -> ({tag: text} of its show() lines, all of stderr)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'script.lua')
        with open(path, 'w') as f:
            f.write(PRELUDE + script)
        r = subprocess.run([LUAU, os.path.join(DEVM, 'harness.lua'), '-a', path, os.path.join(tmp, 'out.trace')] + list(options),
                           capture_output=True, text=True, errors='replace', timeout=120)
    got = {}
    for line in r.stderr.split('\n'):
        if line.startswith('[t] ') and '=' in line:
            k, v = line[4:].split('=', 1)
            got[k] = v
    return got, r.stderr


@unittest.skipUnless(LUAU, 'needs bin/luau')
class Enums(unittest.TestCase):
    def test_items_and_values(self):
        got, log = run('''
show("ball", Enum.PartType.Ball, Enum.PartType.Ball.Value, Enum.PartType.Ball.Name, Enum.PartType.Ball.EnumType)
show("byvalue", Enum.PartType:FromValue(3), Enum.PartType:FromValue(4), Enum.PartType:FromValue(99))
show("byname", Enum.PartType:FromName("Wedge"), Enum.PartType:FromName("Nope"))
show("items", #Enum.PartType:GetEnumItems(), Enum.PartType:GetEnumItems()[1], Enum.PartType:GetEnumItems()[5])
show("types", typeof(Enum), typeof(Enum.PartType), typeof(Enum.PartType.Ball), tostring(Enum), tostring(Enum.PartType))
show("same", Enum.PartType.Ball == Enum.PartType:FromValue(0), Enum.PartType.Ball:IsA("PartType"), Enum.PartType.Ball:IsA("NormalId"))
show("keycode", Enum.KeyCode.A.Value, Enum.KeyCode.Space.Value, Enum.NormalId.Front.Value, Enum.CompressionAlgorithm.Zstd.Value)
local chain = Enum.NormalId:FromName("Left").EnumType:FromName("Left").EnumType:FromValue(4).EnumType:FromName("Front")
show("chain", chain, chain.EnumType[chain.Name] == chain, chain.EnumType:FromValue(chain.Value) == chain)
show("enums", #Enum:GetEnums() > 400, typeof(Enum:GetEnums()[1]))
-- old names Roblox still accepts are the renamed item itself, and are not listed
show("legacy", Enum.RaycastFilterType.Blacklist, Enum.Font.GothamSemibold == Enum.Font.GothamMedium, #Enum.RaycastFilterType:GetEnumItems())
''')
        self.assertEqual(got.get('ball'), 'Enum.PartType.Ball|0|Ball|PartType', log)
        self.assertEqual(got.get('byvalue'), 'Enum.PartType.Wedge|Enum.PartType.CornerWedge|nil')
        self.assertEqual(got.get('byname'), 'Enum.PartType.Wedge|nil')
        self.assertEqual(got.get('items'), '5|Enum.PartType.Ball|Enum.PartType.CornerWedge')
        self.assertEqual(got.get('types'), 'Enums|Enum|EnumItem|Enums|PartType')
        self.assertEqual(got.get('same'), 'true|true|false')
        self.assertEqual(got.get('keycode'), '97|32|5|0')
        self.assertEqual(got.get('chain'), 'Enum.NormalId.Front|true|true')
        self.assertEqual(got.get('enums'), 'true|Enum')
        self.assertEqual(got.get('legacy'), 'Enum.RaycastFilterType.Exclude|true|2')

    def test_nothing_is_invented(self):
        got, log = run('''
show("item", err(function() return Enum.PartType.Nope end))
show("enum", err(function() return Enum.Nope end))
show("member", err(function() return Enum.PartType.Ball.Nope end))
show("self", err(Enum.PartType.FromValue, 1, 1), err(Enum.PartType.GetEnumItems))
''')
        self.assertTrue(got.get('item', '').endswith(': Nope is not a valid member of "Enum.PartType"'), log)
        self.assertTrue(got.get('enum', '').endswith(': Nope is not a valid member of "Enum"'), got.get('enum'))
        self.assertTrue(got.get('member', '').endswith(': Nope is not a valid member of "Enum.PartType.Ball"'), got.get('member'))
        self.assertEqual(got.get('self'), "Expected ':' not '.' calling member function FromValue|"
                                          "Expected ':' not '.' calling member function GetEnumItems")


@unittest.skipUnless(LUAU, 'needs bin/luau')
class Instances(unittest.TestCase):
    def test_methods_exist_and_check_self(self):
        got, log = run('''
local f = Instance.new("Folder")
show("known", err(f.AddTag, 1, "x"), err(f.FindFirstChildOfClass, UDim.new(1, 2), "x"))
show("unmodelled", err(f.GetActor, 1), err(f.QueryDescendants))
show("lower", #f:getChildren(), f:isA("Folder"), f:findFirstChild("x"))
show("isa", f:IsA("Instance"), f:IsA("GuiObject"), Instance.new("TextButton"):IsA("GuiObject"), Instance.new("ScreenGui"):IsA("LayerCollector"))
show("notmine", err(function() return f.SetControlPoints end), err(function() return f.GetService end))
show("signal", typeof(f.ChildAdded), typeof(f.AttributeChanged), f.Changed == f.Changed, rawequal(f.Changed, f.Changed))
''')
        e = "Expected ':' not '.' calling member function "
        self.assertEqual(got.get('known'), e + 'AddTag|' + e + 'FindFirstChildOfClass', log)
        self.assertEqual(got.get('unmodelled'), e + 'GetActor|' + e + 'QueryDescendants')
        self.assertEqual(got.get('lower'), '0|true|nil')
        self.assertEqual(got.get('isa'), 'true|false|true|true')
        a, b = got.get('notmine', '|').split('|')
        self.assertTrue(a.endswith('SetControlPoints is not a valid member of Folder "Folder"'), a)
        self.assertTrue(b.endswith('GetService is not a valid member of Folder "Folder"'), b)
        self.assertEqual(got.get('signal'), 'RBXScriptSignal|RBXScriptSignal|true|false')

    def test_tags(self):
        got, log = run('''
local f = Instance.new("Folder")
show("a", f:HasTag("t"), #f:GetTags())
f:AddTag("t"); f:AddTag("u"); f:AddTag("t")
show("b", f:HasTag("t"), f:HasTag("u"), f:HasTag("v"), #f:GetTags(), f:GetTags()[1], f:GetTags()[2])
f:RemoveTag("t")
show("c", f:HasTag("t"), #f:GetTags(), f:GetTags()[1])
local cs = game:GetService("CollectionService")
cs:AddTag(f, "w")
show("d", f:HasTag("w"), cs:HasTag(f, "u"), #cs:GetTags(f))
show("e", err(f.AddTag, f), f:Clone():HasTag("u"))
''')
        self.assertEqual(got.get('a'), 'false|0', log)
        self.assertEqual(got.get('b'), 'true|true|false|2|t|u')
        self.assertEqual(got.get('c'), 'false|1|u')
        self.assertEqual(got.get('d'), 'true|true|2')
        self.assertEqual(got.get('e'), 'Argument 1 missing or nil|true')

    def test_unknown_members(self):
        got, log = run('''
local g = Instance.new("Folder"); g.Name = "G"
local f = Instance.new("Folder", g); f.Name = "F"
show("created", err(function() return f.Nope end))
show("child", g.F == f)
show("place", typeof(workspace.SomeMap), typeof(game:GetService("Players").LocalPlayer))
show("game", game.Workspace == workspace, game.workspace == workspace, game.Players == game:GetService("Players"), workspace.CurrentCamera.ClassName,
	game:GetService("Players").LocalPlayer.PlayerGui.ClassName, workspace.Parent == game, game.Parent)
show("class", err(Instance.new, "NoSuchClass"), err(Instance.new))
show("service", err(function() return game:GetService("NoSuchService") end), game:FindService("NoSuchService"))
local long = string.rep("x", 150)
f.Name = long
show("name", #f.Name, tostring(f) == string.rep("x", 100))
''')
        self.assertTrue(got.get('created', '').endswith(': Nope is not a valid member of Folder "G.F"'), log)
        self.assertEqual(got.get('child'), 'true')
        self.assertEqual(got.get('place'), 'userdata|Instance')
        self.assertEqual(got.get('game'), 'true|true|true|Camera|PlayerGui|true|nil')
        self.assertEqual(got.get('class'), 'Unable to create an Instance of type "NoSuchClass"|Argument 1 missing or nil')
        a, b = got.get('service', '|').split('|')
        self.assertTrue(a.endswith(": 'NoSuchService' is not a valid Service name"), a)
        self.assertEqual(b, 'nil')
        self.assertEqual(got.get('name'), '100|true')
        # the stand-in handed out for workspace.SomeMap is named in the log
        self.assertIn('[stub] permissive stand-ins handed out', log)
        self.assertIn('Workspace.SomeMap', log)


@unittest.skipUnless(LUAU, 'needs bin/luau')
class Globals(unittest.TestCase):
    def test_unknown_global_is_nil(self):
        got, log = run('''
show("nil", SCRIPT_KEY, Foo, KRNL_LOADED, some_name)
show("libs", type(Vector2), type(TweenInfo), type(Rect), type(DockWidgetPluginGuiInfo), typeof(Enum), type(Instance))
show("fns", type(Stats), type(Version), type(Wait), type(Spawn), type(Delay), type(ElapsedTime), type(UserSettings))
''')
        self.assertEqual(got.get('nil'), 'nil|nil|nil|nil', log)
        self.assertEqual(got.get('libs'), 'table|table|table|table|Enums|table')
        self.assertEqual(got.get('fns'), 'function|function|function|function|function|function|function')
        self.assertIn('[script] undefined globals read (nil, as in Roblox): SCRIPT_KEY, Foo, KRNL_LOADED, some_name', log)

    def test_executor_libraries(self):
        got, _ = run('show("x", type(Drawing), type(WebSocket), type(request))', 'executor=1')
        self.assertEqual(got.get('x'), 'table|table|function')
        got, _ = run('show("x", type(Drawing), type(WebSocket), type(request))')
        self.assertEqual(got.get('x'), 'nil|nil|nil')

    def test_task_argument_errors(self):
        got, log = run('''
show("spawn", err(task.spawn), err(task.spawn, 1))
show("defer", err(task.defer), err(task.defer, "x"))
show("delay", err(task.delay), err(task.delay, 1), err(task.delay, "x", print))
show("cancel", err(task.cancel), err(task.cancel, 1))
show("ok", type(task.spawn(function() end)), type(task.defer(function() end)), type(task.delay(1, function() end)))
show("info", debug.info(task.spawn, "s"), debug.info(task.spawn, "n"), debug.info(task.delay, "n"))
''')
        self.assertEqual(got.get('spawn'), "invalid argument #1 to 'spawn' (function or thread expected)|"
                                           "invalid argument #1 to 'spawn' (function or thread expected)", log)
        self.assertEqual(got.get('defer'), "invalid argument #1 to 'defer' (function or thread expected)|"
                                           "invalid argument #1 to 'defer' (function or thread expected)")
        self.assertEqual(got.get('delay'), "invalid argument #2 to 'delay' (function or thread expected)|"
                                           "invalid argument #2 to 'delay' (function or thread expected)|"
                                           "invalid argument #1 to 'delay' (number expected, got string)")
        self.assertEqual(got.get('cancel'), "missing argument #1 to 'cancel' (thread expected)|"
                                            "invalid argument #1 to 'cancel' (thread expected, got number)")
        self.assertEqual(got.get('ok'), 'thread|thread|thread')
        self.assertEqual(got.get('info'), '[C]|spawn|delay')

    def test_a_spawned_thread_that_waits_is_suspended(self):
        # The run of an unmodified file knows the script's main thread too (harness.lua, where the
        # sample is loaded): only then does task.wait suspend a spawned thread until the main thread
        # waits, as Roblox's scheduler does.  Luraph v15.x start-up code checks it.
        got, log = run('''
local order = {}
local co = task.spawn(function()
	order[#order + 1] = "a"
	task.wait()
	order[#order + 1] = "c"
end)
order[#order + 1] = "b"
show("spawned", coroutine.status(co), table.concat(order))
task.wait()
order[#order + 1] = "d"
show("resumed", coroutine.status(co), table.concat(order))
local co2 = task.defer(function() order[#order + 1] = "e" end)
show("deferred", coroutine.status(co2), table.concat(order))
task.wait()
show("ran", coroutine.status(co2), table.concat(order))
local co3 = task.spawn(function() game:GetService("RunService").Heartbeat:Wait(); order[#order + 1] = "never" end)
show("signal", coroutine.status(co3))
show("below", debug.info(2, "l"))
''')
        self.assertEqual(got.get('spawned'), 'suspended|ab', log)
        self.assertEqual(got.get('resumed'), 'dead|abcd')
        self.assertEqual(got.get('deferred'), 'suspended|abcd')
        self.assertEqual(got.get('ran'), 'dead|abcde')
        self.assertEqual(got.get('signal'), 'suspended')
        self.assertEqual(got.get('below'), '')          # the chunk is the bottom of the stack, as in Roblox

    def test_clocks_and_wait_results(self):
        # tick() counts from the Unix epoch; a wait returns the time that passed: at least what was asked
        # for, at least one scheduler step, and not the same number twice (rbxmock.lua, "task")
        got, log = run('''
show("tick", tick() > 1700000000, math.abs(tick() - os.time()) < 2, math.abs(tick() - DateTime.now().UnixTimestampMillis / 1000) < 2)
local a, b = task.wait(), task.wait()
local w, total = wait()
show("task", a >= 1 / 60, a < 0.03, a ~= b, select("#", task.wait()))
show("legacy", w >= 0.029, w < 0.06, w ~= a, type(total), select("#", wait()))
show("asked", task.wait(0.5) >= 0.5, task.wait(0.5) < 0.52, wait(2) >= 2, task.wait(0) > 0)
local got
task.spawn(function() got = table.pack(wait(0.25)) end)
task.wait()
show("thread", got.n, got[1] >= 0.25, type(got[2]))
''')
        self.assertEqual(got.get('tick'), 'true|true|true', log)
        self.assertEqual(got.get('task'), 'true|true|true|1')
        self.assertEqual(got.get('legacy'), 'true|true|true|number|2')
        self.assertEqual(got.get('asked'), 'true|true|true|true')
        self.assertEqual(got.get('thread'), '2|true|number')

    def test_datetime(self):
        # documented datatype; 2020-01-02T10:30:45Z is the documentation's own example (1577961045 s)
        got, log = run('''
local d = DateTime.fromUniversalTime(2020, 1, 2, 10, 30, 45, 250)
show("type", typeof(d), typeof(DateTime.now()), type(DateTime))
show("stamp", d.UnixTimestamp, d.UnixTimestampMillis, d:ToIsoDate())
local u = d:ToUniversalTime()
show("fields", u.Year, u.Month, u.Day, u.Hour, u.Minute, u.Second, u.Millisecond)
show("epoch", DateTime.fromUnixTimestamp(0):ToIsoDate(), DateTime.fromUnixTimestampMillis(951782400000):ToIsoDate())
show("iso", DateTime.fromIsoDate("2020-01-02T10:30:45Z").UnixTimestamp, DateTime.fromIsoDate("2020-01-02T10:30:45+01:00").UnixTimestamp,
	DateTime.fromIsoDate("2020-01-02").UnixTimestamp, DateTime.fromIsoDate("yesterday"))
show("format", d:FormatUniversalTime("YYYY-MM-DD HH:mm:ss", "en-us"), d:FormatUniversalTime("MMM D, YYYY h:mm A", "en-us"), d:FormatUniversalTime("dddd", "en-us"))
show("now", math.abs(DateTime.now().UnixTimestamp - os.time()) <= 2, DateTime.now().UnixTimestamp < 4102444800, d == DateTime.fromUnixTimestampMillis(1577961045250))
''')
        self.assertEqual(got.get('type'), 'DateTime|DateTime|table', log)
        self.assertEqual(got.get('stamp'), '1577961045|1577961045250|2020-01-02T10:30:45Z')
        self.assertEqual(got.get('fields'), '2020|1|2|10|30|45|250')
        self.assertEqual(got.get('epoch'), '1970-01-01T00:00:00Z|2000-02-29T00:00:00Z')
        self.assertEqual(got.get('iso'), '1577961045|1577957445|1577923200|nil')
        self.assertEqual(got.get('format'), '2020-01-02 10:30:45|Jan 2, 2020 10:30 AM|Thursday')
        self.assertEqual(got.get('now'), 'true|true|true')
        self.assertNotIn('[stub]', log)

    def test_script_output_is_logged(self):
        _, log = run('print("hello", 1, nil)\nwarn("careful")\nprint("hello", 1, nil)')
        # (written when said, so that a run that is stopped from outside has them; repeats are counted at the end)
        self.assertEqual([l for l in log.split('\n') if l.startswith('[script]')],
                         ['[script] print: hello 1 nil', '[script] warn: careful', '[script] said 2 times: print: hello 1 nil'])


@unittest.skipUnless(LUAU, 'needs bin/luau')
class Path2D(unittest.TestCase):
    def test_without_a_parent(self):
        got, log = run('''
local p = Instance.new("Path2D")
show("a", err(p.GetLength, p), err(p.GetBoundingRect, p), err(p.GetPositionOnCurve, p, 0.5), err(p.GetTangentOnCurveArcLength, p, 0.5))
show("b", #p:GetControlPoints(), p:GetSegmentCount())
local f = Instance.new("Frame"); f.Size = UDim2.fromOffset(100, 50); p.Parent = f
p:SetControlPoints({ Path2DControlPoint.new(UDim2.fromScale(0, 0)), Path2DControlPoint.new(UDim2.fromScale(1, 0)) })
show("c", p:GetLength(), p:GetPositionOnCurve(0.5).X.Scale)
''')
        e = 'Attempting to use Path2D with invalid parent'
        self.assertEqual(got.get('a'), '|'.join([e] * 4), log)
        self.assertEqual(got.get('b'), '0|0')
        # (a frame without a parent is measured by its offsets, see rbxmock.lua, GUI layout)
        self.assertEqual(got.get('c'), '100|0.5')

    def test_measured_against_the_parent_frame(self):
        got, log = run('''
local sg = Instance.new("ScreenGui")
local f = Instance.new("Frame"); f.Size = UDim2.fromOffset(100, 50); f.Parent = sg
local p = Instance.new("Path2D"); p.Parent = f
p:SetControlPoints({ Path2DControlPoint.new(UDim2.fromScale(0, 0)), Path2DControlPoint.new(UDim2.new(1, 0, 0, 0)), Path2DControlPoint.new(UDim2.new(1, 0, 1, 0)) })
show("len", p:GetLength(), p:GetSegmentCount(), p:GetPositionOnCurve(0.25).X.Scale, p:GetPositionOnCurve(0.75).Y.Scale)
''')
        self.assertEqual(got.get('len'), '150|2|0.5|0.5', log)


@unittest.skipUnless(LUAU, 'needs bin/luau')
class Layout(unittest.TestCase):
    """Numbers worked out by hand from the rules in rbxmock.lua (section "GUI layout")."""

    TREE = '''
local function new(class, parent, props)
	local o = Instance.new(class)
	for k, v in pairs(props or {}) do o[k] = v end
	o.Parent = parent
	return o
end
local gui = Instance.new("ScreenGui")
local view = new("Frame", gui, { Position = UDim2.new(0, 10, 0, 20), Size = UDim2.new(0, 1000, 0, 500) })
'''

    def test_offsets_scale_and_anchor(self):
        got, log = run(self.TREE + '''
local a = new("TextLabel", view, { Position = UDim2.new(0.1, 5, 0.5, -3), Size = UDim2.new(0.5, 10, 0.2, 5), AnchorPoint = Vector2.new(0.5, 1) })
show("view", v2(view.AbsoluteSize), v2(view.AbsolutePosition), v2(gui.AbsoluteSize), v2(gui.AbsolutePosition))
-- size (0.5 * 1000 + 10, 0.2 * 500 + 5) = (510, 105); position (10 + 100 + 5 - 255, 20 + 250 - 3 - 105)
show("a", v2(a.AbsoluteSize), v2(a.AbsolutePosition), a.AbsoluteRotation)
show("fresh", typeof(a.AbsoluteSize), a.AbsoluteSize == a.AbsoluteSize, v2(Instance.new("Frame").AbsoluteSize))
a.Size = UDim2.new(0, 40, 0, 30)
show("after", v2(a.AbsoluteSize), v2(a.AbsolutePosition))
''')
        self.assertEqual(got.get('view'), '1000,500|10,20|0,0|0,0', log)
        self.assertEqual(got.get('a'), '510,105|-140,162|0')
        self.assertEqual(got.get('fresh'), 'Vector2|true|0,0')
        self.assertEqual(got.get('after'), '40,30|95,237')

    def test_constraint_scale_and_padding(self):
        got, log = run(self.TREE + '''
local a = new("Frame", view, { Position = UDim2.new(0.1, 5, 0.5, -3), Size = UDim2.new(0.5, 10, 0.2, 5), AnchorPoint = Vector2.new(0.5, 1) })
new("UISizeConstraint", a, { MinSize = Vector2.new(0, 120), MaxSize = Vector2.new(400, 300) })
new("UIScale", a, { Scale = 0.5 })
-- (510, 105) clamped to (400, 120), times 0.5 = (200, 60); position (10 + 105 - 100, 20 + 247 - 60)
show("a", v2(a.AbsoluteSize), v2(a.AbsolutePosition))
new("UIPadding", a, { PaddingLeft = UDim.new(0.1, 4), PaddingRight = UDim.new(0, 6), PaddingTop = UDim.new(0.25, 2) })
-- content box of a: left 0.1 * 200 + 4 * 0.5 = 22, right 6 * 0.5 = 3, top 0.25 * 60 + 2 * 0.5 = 16: 175 x 44 at (22, 16)
local b = new("TextButton", a, { Position = UDim2.new(0, 2, 1, 0), Size = UDim2.new(1, -10, 0.5, 8) })
-- size (175 - 10 * 0.5, 22 + 8 * 0.5) = (170, 26); position (15 + 22 + 2 * 0.5, 207 + 16 + 44)
show("b", v2(b.AbsoluteSize), v2(b.AbsolutePosition))
-- a constraint under a scaled ancestor is scaled with it: min 100 * 0.5 = 50 wide
new("UISizeConstraint", b, { MinSize = Vector2.new(0, 100) })
show("b2", v2(b.AbsoluteSize))
''')
        self.assertEqual(got.get('a'), '200,60|15,207', log)
        self.assertEqual(got.get('b'), '170,26|38,267')
        self.assertEqual(got.get('b2'), '170,50')

    def test_rotation(self):
        got, log = run(self.TREE + '''
local p = new("Frame", view, { Position = UDim2.new(0, 90, 0, 80), Size = UDim2.new(0, 200, 0, 100), Rotation = 90 })
local c = new("Frame", p, { Size = UDim2.new(0, 20, 0, 10), Rotation = 30 })
-- p: box (100, 100) .. (300, 200), centre (200, 150); its own rotation does not move it
show("p", v2(p.AbsoluteSize), v2(p.AbsolutePosition), p.AbsoluteRotation)
-- c: centre (10, 5) in p, (-90, -45) from p's centre, turned by 90 degrees: (45, -90) -> (245, 60); minus half its size
show("c", v2(c.AbsoluteSize), v2(c.AbsolutePosition), c.AbsoluteRotation)
''')
        self.assertEqual(got.get('p'), '200,100|100,100|90', log)
        self.assertEqual(got.get('c'), '20,10|235,55|120')

    def test_flipped_content_box(self):
        got, log = run(self.TREE + '''
local p = new("Frame", view, { Position = UDim2.new(0, -10, 0, -20), Size = UDim2.new(0, 100, 0, 100) })
new("UIPadding", p, { PaddingLeft = UDim.new(0.7, 0), PaddingRight = UDim.new(0.6, 0) })
-- content width 100 - 70 - 60 = -30: flipped, 30 wide starting at 40
local c = new("Frame", p, { Size = UDim2.new(1, 0, 0, 10) })
show("c", v2(c.AbsoluteSize), v2(c.AbsolutePosition))
''')
        self.assertEqual(got.get('c'), '30,10|40,0', log)

    def test_screen_gui_in_the_game(self):
        got, log = run('''
local gui = Instance.new("ScreenGui", game:GetService("CoreGui"))
local f = Instance.new("Frame", gui); f.Size = UDim2.new(0.5, 10, 1, 0); f.Position = UDim2.fromScale(0.5, 0)
show("a", v2(gui.AbsoluteSize), v2(gui.AbsolutePosition), v2(f.AbsoluteSize), v2(f.AbsolutePosition))
gui.IgnoreGuiInset = true
show("b", v2(gui.AbsoluteSize), v2(gui.AbsolutePosition), v2(f.AbsoluteSize))
show("cam", v2(workspace.CurrentCamera.ViewportSize))
''')
        self.assertEqual(got.get('a'), '1920,1022|0,0|970,1022|960,0', log)
        self.assertEqual(got.get('b'), '1920,1080|0,-58|970,1080')
        self.assertEqual(got.get('cam'), '1920,1080')


def zstd_frame(parts):
    """A Zstandard frame of raw and RLE blocks: parts = [bytes | (byte value, count)]."""
    size = sum(len(p) if isinstance(p, bytes) else p[1] for p in parts)
    out = bytearray(b'\x28\xb5\x2f\xfd')
    out += bytes([0x20, size])               # single segment, 1-byte content size, no checksum
    for i, p in enumerate(parts):
        last = 1 if i == len(parts) - 1 else 0
        if isinstance(p, bytes):
            h = (len(p) << 3) | last
            out += h.to_bytes(3, 'little') + p
        else:
            h = (p[1] << 3) | (1 << 1) | last
            out += h.to_bytes(3, 'little') + bytes([p[0]])
    return bytes(out)


@unittest.skipUnless(LUAU, 'needs bin/luau')
class Zstd(unittest.TestCase):
    SCRIPT = '''
local es = game:GetService("EncodingService")
local frame = buffer.fromstring("%s")
show("bad", err(es.DecompressBuffer, es, "text", Enum.CompressionAlgorithm.Zstd), err(es.DecompressBuffer, es, frame),
	err(es.DecompressBuffer, 1, frame, Enum.CompressionAlgorithm.Zstd))
local ok, out = pcall(es.DecompressBuffer, es, frame, Enum.CompressionAlgorithm.Zstd)
show("out", ok, ok and typeof(out), ok and buffer.tostring(out) or out)
'''

    def test_decompress_buffer(self):
        frame = zstd_frame([b'hello zstd', (ord('x'), 5)])
        got, log = run(self.SCRIPT % ''.join('\\x%02x' % b for b in frame))
        self.assertEqual(got.get('bad'), "invalid argument #1 to 'DecompressBuffer' (buffer expected, got string)|"
                                         "Argument 2 missing or nil|Expected ':' not '.' calling member function DecompressBuffer", log)
        if os.path.exists(os.path.join(DEVM, 'zstd.lua')):
            self.assertEqual(got.get('out'), 'true|buffer|hello zstdxxxxx', log)
        else:
            # no decoder file: the call stops with a message that says so, and the log names the file
            self.assertEqual(got.get('out'), 'false|false|EncodingService:DecompressBuffer: no Zstandard decoder (devm/zstd.lua is missing)', log)
            self.assertIn('[env] the script unpacks Zstandard data', log)


if __name__ == '__main__':
    unittest.main()
