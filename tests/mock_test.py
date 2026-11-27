#!/usr/bin/env python3
"""The mock Roblox environment of the devm harness answers like Roblox (devm/rbxmock.lua, the
environment side of devm/harness.lua).

    python3 tests/mock_test.py [-v] [Name ...]

Hand-written scripts are run through the harness and what they observe is compared with what
the engine answers: enum items and values, instance methods and their errors, tags, undefined
globals, task argument errors, GUI layout numbers worked out by hand from the rules in
rbxmock.lua ("GUI layout"), Path2D bounding boxes worked out by hand, EncodingService:DecompressBuffer.
No corpus file is needed.

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
sys.path.append(HERE)     # (path2d_cases.py)

from path2d_cases import (PATH2D_ARGS, PATH2D_ARGS_WANT, PATH2D_LATE, PATH2D_LATE_WANT, PATH2D_STORED,  # noqa: E402
                          PATH2D_STORED_WANT, matches)


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
        # (a script has no writer of its own: what it hands to `ewrite` is logged as a [script] line)
        if line.startswith(SAID) and '=' in line:
            k, v = line[len(SAID):].split('=', 1)
            got[k] = v
    return got, r.stderr


SAID = '[script] ewrite: [t] '


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
        # a recovered script can carry placeholders such as __LPH_ENV["M"]: it is run like any other text
        got, log = run('local x = __LPH_ENV\nlocal co = task.spawn(function() task.wait() end)\nshow("status", coroutine.status(co), x)')
        self.assertEqual(got.get('status'), 'suspended|nil', log)

    def test_main_thread_of_a_chunk_started_by_the_carrier(self):
        # a chunk unwrapped earlier runs through a stand-in carrier (harness.lua, carrier= / chunksrc=):
        # the thread the carrier starts it in is the script's main thread
        with tempfile.TemporaryDirectory() as tmp:
            chunk, carrier = os.path.join(tmp, 'chunk.lua'), os.path.join(tmp, 'carrier.lua')
            with open(chunk, 'w') as f:
                f.write('local co = task.spawn(function() task.wait() end)\newrite("[t] status=" .. coroutine.status(co) .. "\\n")\n')
            with open(carrier, 'w') as f:
                f.write('local text, buf = __LPH_CARRY()\nlocal f = loadstring(text, "Luraph")\nreturn f(buf)\n')
            r = subprocess.run([LUAU, os.path.join(DEVM, 'harness.lua'), '-a', chunk, os.path.join(tmp, 'out.trace'),
                                'carrier=' + carrier, 'chunksrc=' + chunk], capture_output=True, text=True, errors='replace', timeout=120)
        self.assertIn('[t] status=suspended', r.stderr, r.stderr)

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


class Report(unittest.TestCase):
    def test_the_report_shows_what_the_script_said(self):
        # devm.run's final report: the script's own output and the mock's stand-ins are part of it
        from devm import run as devm_run
        out = devm_run.run_report([
            '[oracle] script returned after 10 instructions',
            '[script] print: hello',
            '[script] undefined globals read (nil, as in Roblox): SCRIPT_KEY',
            '[stub] permissive stand-ins handed out where the mock has no Roblox answer: Path2D:GetBoundingRect()',
            '[stub] tamper response 3 suppressed (entry 1, step 2)'])
        self.assertIn('  script: print: hello', out)
        self.assertIn('  script: undefined globals read (nil, as in Roblox): SCRIPT_KEY', out)
        self.assertIn('  mock: permissive stand-ins handed out where the mock has no Roblox answer: Path2D:GetBoundingRect()', out)
        self.assertFalse(any('tamper response' in line for line in out))
        many = devm_run.run_report(['[script] print: %d' % i for i in range(40)])
        self.assertEqual(len([line for line in many if line.startswith('  script: ')]), 13)       # 12 and a count


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

    def test_bounding_rect(self):
        # Hand-computed: the box of each point and of the tangent handles that shape a segment, in the
        # parent's pixels, widened by Thickness / 2 on every side (rbxmock.lua, GetBoundingRect)
        got, log = run('''
local sg = Instance.new("ScreenGui")
local f = Instance.new("Frame"); f.Size = UDim2.fromOffset(300, 200); f.Parent = sg
local p = Instance.new("Path2D"); p.Parent = f
local function box(r) return r.Min.X .. "," .. r.Min.Y .. "," .. r.Max.X .. "," .. r.Max.Y end
-- a straight segment (10, 20) - (110, 70)
p:SetControlPoints({ Path2DControlPoint.new(UDim2.fromOffset(10, 20)), Path2DControlPoint.new(UDim2.fromOffset(110, 70)) })
local r = p:GetBoundingRect()
show("line", typeof(r), box(r), r.Width, r.Height)
p.Thickness = 3
show("thick", box(p:GetBoundingRect()))
p.Thickness = 1
-- handles (50, 10) and (150, 10): the box holds them (the curve itself only reaches y = 20)
p:SetControlPoints({ Path2DControlPoint.new(UDim2.fromOffset(20, 50), UDim2.new(), UDim2.fromOffset(30, -40)),
	Path2DControlPoint.new(UDim2.fromOffset(180, 50), UDim2.fromOffset(-30, -40), UDim2.new()) })
r = p:GetBoundingRect()
show("curve", box(r), tostring(r), r == p:GetBoundingRect())
-- Scale and Offset in the 300 x 200 frame: points (85, 80), (220, 50); handles (120, 30), (220, 190);
-- the first point's left handle (-65, 80) shapes no segment
p:SetControlPoints({ Path2DControlPoint.new(UDim2.new(0.25, 10, 0.5, -20), UDim2.new(-0.5, 0, 0, 0), UDim2.new(0.1, 5, 0, -50)),
	Path2DControlPoint.new(UDim2.new(0.75, -5, 0.25, 0), UDim2.new(0, 0, 0.5, 40), UDim2.new()) })
r = p:GetBoundingRect()
show("mixed", box(r), r.Width, r.Height)
''')
        self.assertEqual(got.get('line'), 'Rect|9.5,19.5,110.5,70.5|101|51', log)
        self.assertEqual(got.get('thick'), '8.5,18.5,111.5,71.5')
        self.assertEqual(got.get('curve'), '19.5,9.5,180.5,50.5|19.5, 9.5, 180.5, 50.5|true')
        self.assertEqual(got.get('mixed'), '84.5,29.5,220.5,190.5|136|161')
        self.assertNotIn('GetBoundingRect() has no model', log)

    def test_stored_udim_values(self):
        # The same script and hand-computed numbers as the deobf engine's check (tests/path2d_cases.py,
        # PATH2D_STORED; tests/deobf_corpus.py): both environments store a UDim as the engine does (float32 Scale, int32 Offset truncated toward
        # zero) and UDim2:Lerp rounds the offsets, so a Path2D gives the same Rect and length in both.
        got, log = run(PATH2D_STORED.replace('print(', 'show('))
        for tag, want in PATH2D_STORED_WANT.items():
            parts = got.get(tag, '').split('|')
            self.assertEqual(parts[:-1], want[:-1], (tag, parts, log))
            self.assertAlmostEqual(float(parts[-1]), want[-1], delta=0.005, msg=tag)
        got, log = run('''
show("udim", UDim.new(0.5, -2.7).Offset, UDim.new(0.1, 0).Scale, (UDim.new(1, 2.9) + UDim.new(0, 0.5)).Offset, (-UDim.new(0, 3.9)).Offset)
show("udim2", tostring(UDim2.new(0.1, 1.9, 0, -1.9)), UDim2.fromOffset(4.5, -4.5).X.Offset, UDim2.fromScale(0.3, 0).X.Scale)
show("unm", tostring(-UDim2.new(0.25, 3, 0.5, -4)), UDim2.new(0, 2.5, 0, 0) == UDim2.fromOffset(2, 0))
''')
        self.assertEqual(got.get('udim'), '-2|0.10000000149011612|2|-3', log)
        self.assertEqual(got.get('udim2'), '{0.100000001, 1}, {0, -1}|4|0.30000001192092896')
        self.assertEqual(got.get('unm'), '{-0.25, -3}, {-0.5, 4}|true')

    def test_constructor_arguments(self):
        # The same script and hand-computed numbers as the deobf engine's check (tests/path2d_cases.py,
        # PATH2D_ARGS): numeric strings, a missing value, UDim2.new(UDim, UDim), offsets in (-1, 0)
        got, log = run(PATH2D_ARGS.replace('print(', 'show('))
        for tag, want in PATH2D_ARGS_WANT.items():
            parts = got.get(tag, '').split('|')
            self.assertTrue(matches(parts, want), (tag, parts, want, log))

    def test_measured_at_query_time(self):
        # The same script and numbers as the deobf engine's check (tests/path2d_cases.py, PATH2D_LATE):
        # the curve is measured
        # against the parent Frame as it is when asked (points set before the parent and before a
        # resize); a path of one point gets a stand-in of type Rect, said in the log.
        got, log = run(PATH2D_LATE.replace('print(', 'show('))
        for tag, want in PATH2D_LATE_WANT.items():
            parts = got.get(tag, '').split('|')
            self.assertEqual(parts, [str(w) for w in want], (tag, parts, log))
        self.assertIn('[stub] permissive stand-ins handed out where the mock has no Roblox answer: Path2D:GetBoundingRect()', log)

    def test_rect_value(self):
        # a Rect is a userdata like the engine's: locked metatable, no unknown members, read-only
        got, log = run('''
local f = Instance.new("Frame"); f.Size = UDim2.fromOffset(100, 50)
local p = Instance.new("Path2D"); p.Parent = f
p:SetControlPoints({ Path2DControlPoint.new(UDim2.fromOffset(10, 20)), Path2DControlPoint.new(UDim2.fromOffset(30, 40)) })
local r = p:GetBoundingRect()
show("type", type(r), typeof(r), getmetatable(r), r == p:GetBoundingRect(), r ~= nil)
show("foo", err(function() return r.Foo end))
show("set", err(function() r.Width = 5 end), r.Width)
show("new", err(function() r.Foo = 5 end))
-- Min / Max are a new Vector2 on every read: a write through one does not change the Rect
pcall(function() r.Min.X = 999 end)
pcall(function() local m = r.Max; m.Y = -1 end)
show("corners", rawequal(r.Min, r.Min), r.Min == r.Min, r.Min.X, r.Max.Y, tostring(r), r == p:GetBoundingRect())
''')
        self.assertEqual(got.get('type'), 'userdata|Rect|The metatable is locked|true|true', log)
        self.assertEqual(got.get('corners'), 'false|true|9.5|40.5|9.5, 19.5, 30.5, 40.5|true')
        self.assertTrue(got.get('foo', '').endswith('Foo is not a valid member of Rect'), got.get('foo'))
        a, b = got.get('set', '|').split('|')
        self.assertTrue(a.endswith('Width cannot be assigned to'), a)
        self.assertEqual(b, '21')
        self.assertTrue(got.get('new', '').endswith('Foo is not a valid member of Rect'), got.get('new'))


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

    EXACT = '''
local function x17(v) return string.format("%.17g,%.17g", v.X, v.Y) end
'''

    def test_single_precision(self):
        """AbsoluteSize / AbsolutePosition are float32 values computed in float32 (each operation
        rounded): exact numbers, worked out by hand with the float32 rounding of every step
        (f32(0.1) = 0.100000001490116, f32(0.3) = 0.300000011920929, f32(0.7) = 0.699999988079071,
        f32(0.9) = 0.899999976158142, f32(0.35) = 0.349999994039536)."""
        got, log = run(self.EXACT + '''
local gui = Instance.new("ScreenGui", game:GetService("CoreGui"))
local f = Instance.new("Frame", gui); f.Size = UDim2.fromScale(0.1, 0.3); f.Position = UDim2.fromScale(0.7, 0.1)
local g = Instance.new("Frame", f); g.Size = UDim2.new(0.33, 0, 0.6, 0)
local c = Instance.new("Frame", f); c.Size = UDim2.fromScale(0.35, 0.9); c.Position = UDim2.fromScale(0.5, 0.5)
c.AnchorPoint = Vector2.new(0.5, 0.5)
show("f", x17(f.AbsoluteSize), x17(f.AbsolutePosition), f.AbsoluteSize.X == 192, math.floor(f.AbsolutePosition.X * 100))
show("g", x17(g.AbsoluteSize), x17(g.AbsolutePosition))
show("c", x17(c.AbsoluteSize), x17(c.AbsolutePosition))
local sg = Instance.new("ScreenGui")
local outer = Instance.new("Frame", sg); outer.Size = UDim2.fromOffset(400, 300); outer.Position = UDim2.fromOffset(17, 23)
for _, s in ipairs({ 0.3, 0.7, 0.9, 0.35 }) do
	local o = Instance.new("Frame", outer); o.Size = UDim2.fromScale(s, s); o.Position = UDim2.fromScale(s, 1 - s)
	show("s" .. s, x17(o.AbsoluteSize), x17(o.AbsolutePosition))
end
''')
        # viewport 1920 x 1022 (below the top bar): f32(f32(0.1) * 1920) = 192 (192.0000029 in double
        # rounds to 192 in float32), f32(f32(0.3) * 1022) = 306.600006103516; position
        # f32(f32(0.7) * 1920) = 1344 (1343.99997711 in double), f32(f32(0.1) * 1022) = 102.200004577637
        self.assertEqual(got.get('f'), '192,306.60000610351562|1344,102.20000457763672|true|134400', log)
        # f32(f32(0.33) * 192) = 63.3600006103516, f32(f32(0.6) * 306.600006103516) = 183.960006713867
        self.assertEqual(got.get('g'), '63.360000610351562,183.96000671386719|1344,102.20000457763672')
        # size f32(f32(0.35) * 192) = 67.1999969482422, f32(f32(0.9) * 306.600006103516) = 275.940002441406;
        # x = 1344 + f32(f32(0.5 * 192) - f32(0.5 * 67.1999969482422)) = 1406.40002441406,
        # y = 102.200004577637 + f32(153.300003051758 - 137.970001220703) = 117.530006408691
        self.assertEqual(got.get('c'), '67.199996948242188,275.94000244140625|1406.4000244140625,117.53000640869141')
        # in a 400 x 300 frame at (17, 23): Scale s and 1 - s (stored as float32).  0.3: width
        # f32(f32(0.3) * 400) = 120.000007629395, x 17 + that = 137 (a tie, rounded to even);
        # 0.9: y = 23 + f32(f32(0.1) * 300) = 53 (29.999999999999993 + 23 in double precision)
        self.assertEqual(got.get('s0.3'), '120.00000762939453,90|137,233')
        self.assertEqual(got.get('s0.7'), '280,210|297,113')
        self.assertEqual(got.get('s0.9'), '360,270|377,53')
        self.assertEqual(got.get('s0.35'), '140,105|157,218')

    def test_path2d_in_a_frame_sized_with_scale(self):
        """A Path2D is measured against its parent's AbsoluteSize, float32: a Frame of Scale 0.7 x 0.3 in
        400 x 300 is exactly 280 x 90 (279.99999523 in double precision), so the curve is the one of a
        Frame of Offset 280 x 90, bit for bit."""
        got, log = run(self.EXACT + '''
local sg = Instance.new("ScreenGui")
local outer = Instance.new("Frame", sg); outer.Size = UDim2.fromOffset(400, 300)
local fs = Instance.new("Frame", outer); fs.Size = UDim2.fromScale(0.7, 0.3)
local fo = Instance.new("Frame", outer); fo.Size = UDim2.fromOffset(280, 90)
local function pts()
	return { Path2DControlPoint.new(UDim2.fromScale(0.1, 0.2), UDim2.new(), UDim2.fromOffset(13, 7)),
		Path2DControlPoint.new(UDim2.fromScale(0.9, 0.7)), Path2DControlPoint.new(UDim2.fromScale(1, 1)) }
end
local p = Instance.new("Path2D", fs); p:SetControlPoints(pts())
local q = Instance.new("Path2D", fo); q:SetControlPoints(pts())
local function f17(v) return string.format("%.17g", v) end
local function answers(path)
	local r = path:GetBoundingRect()
	return table.concat({ f17(path:GetLength()), f17(path:GetPositionOnCurveArcLength(0.37).X.Scale),
		f17(path:GetPositionOnCurve(0.61).Y.Scale), x17(path:GetTangentOnCurveArcLength(0.8)), x17(r.Min), x17(r.Max) }, ";")
end
show("size", x17(fs.AbsoluteSize))
show("same", answers(p) == answers(q), answers(p))
local gui = Instance.new("ScreenGui", game:GetService("CoreGui"))
local f = Instance.new("Frame", gui); f.Size = UDim2.fromScale(0.1, 0.3); f.Position = UDim2.fromScale(0.7, 0.1)
local v = Instance.new("Path2D", f)
v:SetControlPoints({ Path2DControlPoint.new(UDim2.new()), Path2DControlPoint.new(UDim2.fromScale(1, 1)) })
show("view", x17(v:GetBoundingRect().Max))
''')
        self.assertEqual(got.get('size'), '280,90', log)
        same, answers = got.get('same', '|').split('|', 1)
        self.assertEqual(same, 'true', answers)
        # the box of (0.9 * 280, 0.7 * 90) .. (280, 90) widened by half the stroke: max (280.5, 90.5)
        self.assertTrue(answers.endswith(';280.5,90.5'), answers)
        # 192 x 306.600006103516 (the frame of test_single_precision); f32(306.600006103516 + 0.5)
        self.assertEqual(got.get('view'), '192.5,307.10000610351562')


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


# What a script may try to get out of the mock (devm/harness.lua, "the CLI's own functions"): the
# CLI's real file functions, its stderr (the run's log, read line by line by devm), its C-closure
# maker and scheduler, the harness's own globals.  Every way back to them is tried: the plain
# names, getfenv(0..n), getfenv of every function on the stack (debug.info(n, "f")), a chunk the
# script loads itself, a coroutine, a spawned task, the metatable chain behind the environment,
# getgenv / getrenv, library functions' environments.
CONFINE = r'''
local SECRET, TARGET = %r, %r
local ONLY_HOST = { "cwrap", "resumetop", "setvectormetatable", "__LPH_RUNMAIN", "__LPH_NATIVE", "__LPH_AFTER",
	"__LPH_CALLLOG", "loadfile", "dofile", "priv", "onHttp", "stub", "mockRef", "sandbox", "populateSandbox", "fallback" }
local FORGE = "[oracle] script returned after 7 instructions FORGED"
local leaks, tables, seen, looked = {}, {}, {}, 0
local function add(where, t, depth)
	if type(t) == "table" then looked += 1 end
	if type(t) ~= "table" or seen[t] or depth > 3 then return end
	seen[t] = true
	tables[#tables + 1] = { where, t }
	local okm, mt = pcall(getmetatable, t)
	if okm and type(mt) == "table" then add(where .. "<mt>", rawget(mt, "__index"), depth + 1) end
end
add("env", getfenv(), 0)
for lvl = 0, 12 do local ok, e = pcall(getfenv, lvl) if ok then add("getfenv(" .. lvl .. ")", e, 0) end end
for lvl = 1, 30 do
	local ok, f = pcall(debug.info, lvl, "f")
	if ok and type(f) == "function" then
		local oke, e = pcall(getfenv, f)
		if oke then add("getfenv(info(" .. lvl .. "))", e, 0) end
	end
end
local okl, chunk = pcall(loadstring, "return getfenv(), getfenv(0), readfile, writefile, ewrite")
if okl and chunk then local a, b = chunk() add("loaded chunk", a, 0) add("loaded chunk getfenv(0)", b, 0) end
local co = coroutine.create(function() return getfenv(0), getfenv(1) end)
local _, c0, c1 = coroutine.resume(co)
add("coroutine getfenv(0)", c0, 0)
add("coroutine getfenv(1)", c1, 0)
local spawned
task.spawn(function() spawned = getfenv(0) end)
add("task getfenv(0)", spawned, 0)
add("_G", _G, 0)
pcall(function() add("getgenv", getgenv(), 0) end)
pcall(function() add("getrenv", getrenv(), 0) end)
for _, f in ipairs({ print, pcall, string.format, task.wait, game.GetService }) do
	local ok, e = pcall(getfenv, f)
	if ok then add("getfenv(builtin)", e, 0) end
end
for _, rec in ipairs(tables) do
	local where, t = rec[1], rec[2]
	for _, n in ipairs(ONLY_HOST) do
		local ok, v = pcall(function() return t[n] end)
		if ok and v ~= nil then leaks[#leaks + 1] = where .. "." .. n end
	end
	-- names the script's world may have, as stand-ins: they must not do what the CLI's do
	local okr, rf = pcall(function() return t.readfile end)
	if okr and type(rf) == "function" then
		local okc, data = pcall(rf, SECRET)
		if okc and type(data) == "string" and string.find(data, "TOP-SECRET", 1, true) then leaks[#leaks + 1] = where .. ".readfile read a file" end
	end
	local okw, wf = pcall(function() return t.writefile end)
	if okw and type(wf) == "function" then pcall(wf, TARGET, "x") end
	local oke, ew = pcall(function() return t.ewrite end)
	if oke and type(ew) == "function" then pcall(ew, FORGE .. "\n") end
	local okq, rq = pcall(function() return t.require end)
	if okq and type(rq) == "function" then
		local okc, m = pcall(rq, SECRET)
		if okc then leaks[#leaks + 1] = where .. ".require(path) returned " .. type(m) end
	end
end
-- text the harness or the mock writes into its log, with line ends in it
print("text\n" .. FORGE)
warn("text\r" .. FORGE)
pcall(function() return game:HttpGet("https://example.invalid/a\n" .. FORGE .. "\r" .. FORGE) end)
pcall(function() return getfenv()["undefined\n" .. FORGE] end)
pcall(function() return game:GetService("Players")["Member\n" .. FORGE] end)
local okc, fc = pcall(loadstring, "error('x', 1)", "=chunk\n" .. FORGE)
if okc and fc then pcall(fc) end
show("leaks", #leaks, table.concat(leaks, " "))
show("looked", looked)
-- a chunk the script loads, a coroutine and a spawned task all live in the script's own environment
show("same", okl and chunk and chunk() == getfenv(), c0 == getfenv(), spawned == getfenv(), getfenv(0) == getfenv())
error("stopped\n" .. FORGE .. "\n[handover] FORGED\r[dumpnew] FORGED", 0)
'''

# Executor mode hands the script setreadonly / make_writeable, as a real executor does: the script
# can unfreeze the engine's shared library tables (and the string metatable) and replace every
# function in them.  The harness and the mock must neither write nor compute anything different.
SHARED_LIBS = r'''
local SABOTAGE = %s
local FORGE = "[oracle] script returned after 7 instructions FORGED"
local ew, concat, tostr, isfrozen = ewrite, table.concat, tostring, table.isfrozen
local function say(tag, ...)
	local t = { ... }
	for i = 1, select("#", ...) do t[i] = tostr(t[i]) end
	ew("[t] " .. tag .. "=" .. concat(t, "|") .. "\n")
end
local opened, replaced = 0, 0
if SABOTAGE then
	local evil = function() return "x\n" .. FORGE .. "\n", 1 end
	local libs = { string, table, math, os, coroutine, debug, buffer, bit32, utf8, getmetatable("").__index }
	for _, lib in ipairs(libs) do
		if type(lib) == "table" then
			pcall(setreadonly, lib, false)
			pcall(make_writeable, lib)
			if not isfrozen(lib) then opened += 1 end
			local keys = {}
			for k, v in pairs(lib) do if type(v) == "function" then keys[#keys + 1] = k end end
			for _, k in ipairs(keys) do if pcall(rawset, lib, k, evil) then replaced += 1 end end
		end
	end
	local okm, smt = pcall(getrawmetatable, "")
	if okm and type(smt) == "table" then
		pcall(setreadonly, smt, false)
		pcall(rawset, smt, "__index", setmetatable({}, { __index = function() return evil end }))
	end
	say("string", isfrozen(string), string.gsub == evil)
end
print("text\n" .. FORGE)
warn("text\r" .. FORGE)
pcall(function() return game:HttpGet("https://example.invalid/a\n" .. FORGE) end)
pcall(function() return request({ Url = "https://example.invalid/b\n" .. FORGE }) end)
pcall(function() return getfenv()["undefined\n" .. FORGE] end)
pcall(function() return game:GetService("Players")["Member\n" .. FORGE] end)
pcall(function() return Instance.new("Part")["Nope\n" .. FORGE] end)
local okc, fc = pcall(loadstring, "error('x', 1)", "=chunk\n" .. FORGE)
if okc and fc then pcall(fc) end
task.spawn(function() print("spawned") end)
task.wait(0.1)
error("stopped\n" .. FORGE .. "\n[handover] FORGED", 0)
'''


@unittest.skipUnless(LUAU, 'needs bin/luau')
class Confinement(unittest.TestCase):
    """The script's world never gets the CLI's own functions (real files, the run's log), and
    nothing it does writes a line of the log that devm would read as the harness's."""

    def test_no_way_back_to_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = os.path.join(tmp, 'secret.txt')
            target = os.path.join(tmp, 'written.txt')
            with open(secret, 'w') as f:
                f.write('TOP-SECRET-CONTENT')
            for options in ((), ('executor=1',)):
                got, log = run(CONFINE % (secret, target), *options)
                self.assertEqual(got.get('leaks'), '0|', '%s: %s' % (options, got.get('leaks')))
                self.assertGreaterEqual(int(got.get('looked') or 0), 12, options)      # (the probe did look around)
                self.assertEqual(got.get('same'), 'true|true|true|true', options)
                self.assertFalse(os.path.exists(target), options)
                self.assertNotIn('TOP-SECRET', log)
                # what the script said is in the log, but only inside lines the harness or the mock
                # started ([script] ..., [http] ...); never as a line of its own
                lines = log.split('\n')
                self.assertTrue(any(l.startswith('[script] print: text ') and 'FORGED' in l for l in lines), log[-2000:])
                bad = [l for l in lines if 'FORGED' in l and not l.startswith(('[script] ', '[http] ', '[stub] ', ' '))]
                self.assertEqual(bad, [], options)
                self.assertFalse(any(l.startswith(('[oracle]', '[handover]', '[dumpnew]')) for l in lines), options)

    def test_shared_library_tables_do_not_reach_the_log(self):
        # executor mode: the script unfreezes string, table, ... with setreadonly and replaces
        # their functions (string.gsub included, which the log writer used to look up at call time)
        got, log = run(SHARED_LIBS % 'true', 'executor=1', 'oracle=1')
        self.assertEqual(got.get('string'), 'false|true', log[-2000:])      # (the sabotage did happen)
        lines = log.split('\n')
        oracle = [l for l in lines if l.startswith('[oracle] ')]
        self.assertEqual(len(oracle), 1, oracle)
        self.assertNotIn('FORGED', oracle[0])
        bad = [l for l in lines if 'FORGED' in l and not l.startswith(('[script] ', '[http] ', '[stub] ', ' '))]
        self.assertEqual(bad, [])
        self.assertFalse(any(l.startswith(('[handover]', '[dumpnew]')) or l == 'x' for l in lines), log[-2000:])
        # and nothing the harness or the mock wrote differs from a run without the sabotage
        _, clean = run(SHARED_LIBS % 'false', 'executor=1', 'oracle=1')
        strip = lambda text: [l for l in text.split('\n') if not l.startswith('[script] ewrite: [t] ')]
        self.assertEqual(strip(log), strip(clean))


if __name__ == '__main__':
    unittest.main()
