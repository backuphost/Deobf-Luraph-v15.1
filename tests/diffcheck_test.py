"""Checks of devm.diffcheck: does it say "same" for pairs that are the same and "different" for
pairs made wrong on purpose?

    python3 tests/diffcheck_test.py            (from the repository root; needs bin/luau)

Without a corpus: plain scripts written here, each compared with itself (same), with copies changed
on purpose (one argument, one call removed, two calls swapped, a print text, a property value, a
global, the ending: different), with copies that stop at a placeholder (not comparable), and with
a copy of itself behind code that behaves like Luraph's start-up (same; with a print in front of
the script: different).  Also: recording must not change what a script sees (error messages of
mock functions with and without the recorder).

With a corpus (optional):

    LURAPH_CORPUS=<dir of the public samples>  DIFFCHECK_RESULTS=<dir>  python3 tests/diffcheck_test.py Corpus

DIFFCHECK_RESULTS holds recovered scripts named <version>__<hash>.lua (as shared/measure.py writes
them) that are believed right.  Each must come out "same" against <corpus>/<version>/<hash>.lua,
and a copy with its first printed text changed must not.  DIFFCHECK_EXPECT=<file> (lines
"<version>__<hash> <TAB> same|different|not comparable") overrides the expectation per pair;
DIFFCHECK_LIMIT=<n> bounds the number of pairs (default 8).
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from devm import diffcheck, vmmodel  # noqa: E402

SAME, DIFFERENT, NOT_COMPARABLE = diffcheck.SAME, diffcheck.DIFFERENT, diffcheck.NOT_COMPARABLE

# a script that touches most kinds of thing the comparison looks at
GUI = '''local Players = game:GetService("Players")
local player = Players.LocalPlayer
local gui = Instance.new("ScreenGui")
gui.Name = "Hub"
gui.ResetOnSpawn = false
gui.Parent = player:WaitForChild("PlayerGui")
local frame = Instance.new("Frame", gui)
frame.Size = UDim2.new(0, 200, 0, 100)
frame.BackgroundColor3 = Color3.fromRGB(30, 30, 30)
local label = Instance.new("TextLabel")
label.Text = "Loading"
label.Parent = frame
local conn = game:GetService("RunService").Heartbeat:Connect(function(dt) label.Text = "t" .. dt end)
task.spawn(function()
	print("spawned", 1, true, nil, { a = 1, b = { 2, 3 } })
	task.wait(0.5)
	warn("after wait")
end)
print("first")
print("second")
getgenv().HubLoaded = true
_G.Counter = (_G.Counter or 0) + 1
shared.cfg = { speed = 16 }
MyGlobal = "set"
for i = 1, 3 do
	local p = Instance.new("Part")
	p.Name = "P" .. i
	p.Transparency = i / 4
	p.Parent = workspace
end
conn:Disconnect()
setclipboard("copied")
player:Kick("bye")
task.wait()
print("done")
'''

# what Luraph's start-up looks like from outside: probes on its own objects, error probes, the scheduler
STARTUP = '''do
	local f = Instance.new("Frame")
	f.Size = UDim2.new(0, 10, 0, 10)
	local g = Instance.new("ScreenGui")
	f.Parent = g
	local r = Random.new(7)
	local x = r:NextNumber()
	local v = Vector3.new(1, 2, 3):Lerp(Vector3.new(4, 5, 6), 0.5)
	local ok, err = pcall(game.GetService)
	local t = type(f), typeof(f), getfenv()
	local co = task.spawn(function() task.wait() end)
	task.wait()
	local c = game.ChildAdded:Connect(function() end)
	c:Disconnect()
	game:GetService("RunService"):IsServer()
	g:Destroy()
	loadstring("return 1")
end
'''


def mutate(text, old, new, count=1):
    assert text.count(old) >= 1, old
    return text.replace(old, new, count)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='diffcheck-test-')
        cls.dir = cls._tmp.name
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def write(self, text):
        type(self).n += 1
        path = os.path.join(self.dir, 's%d.lua' % self.n)
        with open(path, 'w') as f:
            f.write(text)
        return path

    def verdict(self, original, result, **kw):
        kw.setdefault('timeout', 60)
        return diffcheck.check(self.write(original), self.write(result), os.path.join(self.dir, 'w'), **kw)

    def expect(self, code, original, result, pattern=None, **kw):
        v = self.verdict(original, result, **kw)
        self.assertEqual(v.code, code, v.text())
        if pattern:
            self.assertRegex(v.text(), pattern)
        return v


class Plain(Base):
    def test_same_as_itself(self):
        v = self.expect(SAME, GUI, GUI, r'the same 39 calls and property writes, both return, the same 4 globals')
        self.assertEqual(v.data['skipped'], 0)
        self.assertEqual(v.data['matched'], 39)

    def test_changed_argument(self):
        self.expect(DIFFERENT, GUI, mutate(GUI, 'Color3.fromRGB(30, 30, 30)', 'Color3.fromRGB(30, 31, 30)'), r'Color3\.fromRGB\(30, 31, 30\)')
        self.expect(DIFFERENT, GUI, mutate(GUI, 'player:Kick("bye")', 'player:Kick("Bye")'), r'Kick')
        self.expect(DIFFERENT, GUI, mutate(GUI, '{ 2, 3 }', '{ 2, 4 }'), r'b=\{2, 4\}')
        self.expect(DIFFERENT, GUI, mutate(GUI, 'task.wait(0.5)', 'task.wait(0.25)'))

    def test_removed_call(self):
        self.expect(DIFFERENT, GUI, mutate(GUI, 'setclipboard("copied")\n', ''), r'setclipboard')
        self.expect(DIFFERENT, GUI, mutate(GUI, 'conn:Disconnect()\n', ''))
        # the first call, the last call
        self.expect(DIFFERENT, GUI, mutate(GUI, 'game:GetService("Players")', 'game.Players'))
        self.expect(DIFFERENT, GUI, mutate(GUI, 'print("done")\n', ''), r'the result returns, the original goes on')

    def test_added_call(self):
        self.expect(DIFFERENT, GUI, GUI + 'print("more")\n', r'the original returns, the result goes on')

    def test_swapped_calls(self):
        self.expect(DIFFERENT, GUI, mutate(GUI, 'print("first")\nprint("second")', 'print("second")\nprint("first")'))
        self.expect(DIFFERENT, GUI, mutate(GUI, 'conn:Disconnect()\nsetclipboard("copied")', 'setclipboard("copied")\nconn:Disconnect()'))

    def test_changed_print_text(self):
        v = self.expect(DIFFERENT, GUI, mutate(GUI, 'print("done")', 'print("Done")'), r'print\("Done"\)')
        self.assertIn('print("done")', v.text())

    def test_changed_property_write(self):
        self.expect(DIFFERENT, GUI, mutate(GUI, 'gui.ResetOnSpawn = false', 'gui.ResetOnSpawn = true'), r'ResetOnSpawn = true')
        self.expect(DIFFERENT, GUI, mutate(GUI, 'p.Parent = workspace', 'p.Parent = gui'))
        self.expect(DIFFERENT, GUI, mutate(GUI, 'label.Parent = frame', 'label.Parent = gui'))

    def test_changed_global(self):
        self.expect(DIFFERENT, GUI, mutate(GUI, 'getgenv().HubLoaded = true', 'getgenv().HubLoaded = false'), r'globals left behind differ')
        self.expect(DIFFERENT, GUI, mutate(GUI, 'MyGlobal = "set"', 'local MyGlobal = "set"'), r'globals left behind differ')
        self.expect(DIFFERENT, GUI, mutate(GUI, 'speed = 16', 'speed = 17'), r'globals left behind differ')

    def test_ending(self):
        a = 'print("a")\nerror("boom")\n'
        self.expect(SAME, a, a, r'both stop with the same error')
        # the position in front of a message differs between a VM and plain code: not compared
        self.expect(SAME, a, '\n\n' + a)
        self.expect(DIFFERENT, a, 'print("a")\nerror("bang")\n', r'different errors')
        self.expect(DIFFERENT, a, 'print("a")\n', r'the original stopped with an error.*the result returned')
        self.expect(DIFFERENT, 'print("a")\n', a)

    def test_method_lookup_after_the_arguments(self):
        # Luraph's VM looks a method up before it evaluates the arguments (Lua 5.1), Luau after.
        # "original": the 5.1 order written out; "result": the source as a lift gives it back.
        original = 'print("a")\nlocal lib = nil\nlocal m = lib.CreateWindow\nm(lib, { Size = UDim2.fromOffset(500, 400) })\n'
        result = 'print("a")\nlocal lib = nil\nlib:CreateWindow({ Size = UDim2.fromOffset(500, 400) })\n'
        self.expect(SAME, original, result, r'Luau evaluates them before it looks the method up')
        # an argument with an effect is not waved through
        result2 = 'print("a")\nlocal lib = nil\nlib:CreateWindow({ Parent = game:GetService("CoreGui") })\n'
        self.expect(DIFFERENT, original, result2, r'if the calls below are arguments of that call')
        self.expect(DIFFERENT, original, mutate(result, 'lib:CreateWindow', 'lib:CreateWindows'))
        # not that case: a local the lift moved into a later expression, behind a call
        o3 = 'print("a")\nlocal lib = nil\nlocal id = lib.Id\nprint(Vector3.new(1, 2, 3), id)\n'
        r3 = 'print("a")\nlocal lib = nil\nprint(Vector3.new(1, 2, 3), lib.Id)\n'
        self.expect(DIFFERENT, o3, r3, r'an expression that raises is evaluated after calls it preceded')

    def test_error_position_inside_a_printed_message(self):
        a = 'local ok, err = pcall(function() local t = nil return t.Source end)\nwarn("UI Library failed to load: " .. tostring(err))\n'
        self.expect(SAME, a, '\n\n-- moved two lines down\n' + a)
        self.expect(DIFFERENT, a, mutate(a, 't.Source', 't.Sources'))

    def test_which_object(self):
        a = 'local x, y = Instance.new("Part"), Instance.new("Part")\nx.Name = "n"\ny:Destroy()\n'
        self.expect(SAME, a, a)
        self.expect(DIFFERENT, a, mutate(a, 'y:Destroy()', 'x:Destroy()'))
        f = 'local f, g = function() end, function() end\ntask.spawn(f)\ntask.defer(g)\ntask.defer(f)\n'
        self.expect(SAME, f, f)
        self.expect(DIFFERENT, f, mutate(f, 'task.defer(f)', 'task.defer(g)'))

    def test_unknown_member_and_executor_calls(self):
        a = ('local re = Instance.new("RemoteEvent")\nre:FireServer("buy", 3)\n'
             'writefile("cfg.json", "{}")\nqueue_on_teleport("print(1)")\nhookfunction(print, function() end)\n')
        v = self.expect(SAME, a, a)
        self.assertEqual(v.data['matched'], 5, v.text())
        self.expect(DIFFERENT, a, mutate(a, '"buy", 3', '"buy", 4'), r'FireServer')
        self.expect(DIFFERENT, a, mutate(a, '"{}"', '"[]"'), r'writefile')
        self.expect(DIFFERENT, a, mutate(a, 'queue_on_teleport("print(1)")\n', ''))


class TimeAndChance(Base):
    def test_values_from_time_and_chance_are_masked(self):
        a = 'print("id", math.random(1, 1000000000))\nwarn(os.time(), tick())\nprint("end")\n'
        self.expect(SAME, a, a)
        # the copy draws one number more before it prints: other values, the same behaviour
        b = 'local _ = math.random()\nlocal _ = os.clock()\n' + a
        v = self.expect(SAME, a, b, r'depend on time or chance')
        self.assertGreaterEqual(v.data['masked_arguments'], 2)
        self.expect(DIFFERENT, a, mutate(b, 'print("end")', 'print("END")'), r'print\("END"\)')
        self.expect(DIFFERENT, a, mutate(b, '"id"', '"ID"'))

    def test_waiting_loop_ends_by_the_clock(self):
        a = 'local t0 = tick()\nlocal n = 0\nrepeat task.wait(1) n += 1 until tick() - t0 > 10\nprint("waited", n)\n'
        v = self.expect(SAME, a, a)
        self.assertLess(v.data['matched'], 60, v.text())


class Limits(Base):
    LOOP = 'local n = 0\nwhile true do\n\ttask.wait(1)\n\tn += 1\n\tprint("tick", n % 7)\nend\n'

    def test_endless_script_is_compared_up_to_the_limit(self):
        v = self.expect(SAME, self.LOOP, self.LOOP, r'same for the first \d+ calls', max_events=60)
        self.assertGreaterEqual(v.data['matched'], 50)
        self.expect(DIFFERENT, self.LOOP, mutate(self.LOOP, 'n % 7', 'n % 8'), max_events=60)

    def test_request_without_reply(self):
        a = ('local reply = game:HttpGet("https://keys.invalid/check?key=abc")\n'
             'if reply == "ok" then print("unlocked") loadstring(game:HttpGet("https://keys.invalid/payload"))() '
             'else warn("no reply") end\n')
        v = self.expect(SAME, a, a, r'requests of the original')
        self.assertIn('https://keys.invalid/check?key=abc', v.text())
        self.assertNotIn('payload', v.text())           # nothing was invented: the script stopped at "no reply"
        self.expect(DIFFERENT, a, mutate(a, 'key=abc', 'key=abd'), r'HttpGet')
        b = 'local r = request({ Url = "https://keys.invalid/v", Method = "POST", Body = "k=1", Headers = { ["X-A"] = "1" } })\nprint(r.StatusCode, #r.Body)\n'
        self.expect(SAME, b, b)
        self.expect(DIFFERENT, b, mutate(b, 'k=1', 'k=2'), r'request')
        self.expect(DIFFERENT, b, mutate(b, '"1" }', '"2" }'), r'request')

    def test_not_comparable(self):
        self.expect(NOT_COMPARABLE, GUI, 'print("a"\n', r'the result did not run')
        self.expect(NOT_COMPARABLE, 'print("a"\n', GUI, r'the original did not run')
        self.expect(NOT_COMPARABLE, GUI, '\n', r'empty')
        self.expect(NOT_COMPARABLE, 'local x = 1\n', 'local y = 2\n', r'nothing to compare')
        v = diffcheck.check(os.path.join(self.dir, 'missing.lua'), self.write(GUI), os.path.join(self.dir, 'w'))
        self.assertEqual(v.code, NOT_COMPARABLE)

    def test_result_that_spins(self):
        a = 'print("a")\n'
        self.expect(NOT_COMPARABLE, a, 'print("a")\nwhile true do end\n', r'still running at the time limit', timeout=12)
        b = 'print("a")\nprint("b")\n'
        self.expect(DIFFERENT, b, 'print("a")\nwhile true do end\n', r'makes no further call until the time limit', timeout=12)
        self.expect(NOT_COMPARABLE, b, 'print("a")\n-- LIFT ERROR: x\nwhile true do end\n', r'the result is partial', timeout=12)


class Partial(Base):
    def test_stops_at_a_placeholder(self):
        cut = GUI.index('conn:Disconnect()')
        for tail, what in (('local v = __LPH_ENV[3]\n', '__LPH_ENV'), ('REG_7(ARG_1)\n', 'REG_7'),
                           ('UPVAL_2.x = 1\n', 'UPVAL_2'), ('-- LIFT ERROR: unop - on <lfn L8>\n', 'partial')):
            v = self.expect(NOT_COMPARABLE, GUI, GUI[:cut] + tail, r'same up to call 33 of 39, then the result (reads the placeholder|ends)')
            self.assertIn(what, v.text())
            self.assertGreater(v.data['matched'], 20)

    def test_partial_and_another_ending(self):
        a = 'print("a")\nerror("boom")\n'
        self.expect(NOT_COMPARABLE, a, 'print("a")\n-- LIFT ERROR: call\n', r'same up to call 1, then the result ends \(returned\); it is partial')

    def test_partial_and_wrong_is_different(self):
        cut = GUI.index('conn:Disconnect()')
        bad = mutate(GUI[:cut], 'print("first")', 'print("FIRST")') + 'local v = __LPH_ENV[3]\n'
        self.expect(DIFFERENT, GUI, bad, r'the result is partial')

    def test_markers_that_are_not_reached(self):
        # a marker in code the run does not reach does not stop the comparison
        ok = GUI + 'if false then\n\t-- LIFT ERROR: unop\n\tlocal v = __LPH_ENV[3]\nend\n'
        self.expect(SAME, GUI, ok, r'the result is partial')


class StartUp(Base):
    def test_start_up_calls_are_skipped(self):
        v = self.expect(SAME, STARTUP + GUI, GUI, r'skipped \d+ leading calls of the original as Luraph start-up')
        self.assertGreater(v.data['skipped'], 10)
        self.assertEqual(v.data['matched'], v.data['result_events'])

    def test_differences_behind_the_start_up(self):
        self.expect(DIFFERENT, STARTUP + GUI, mutate(GUI, 'print("done")', 'print("Done")'))
        self.expect(DIFFERENT, STARTUP + GUI, mutate(GUI, 'label.Text = "Loading"', 'label.Text = "loading"'))
        self.expect(DIFFERENT, STARTUP + GUI, mutate(GUI, 'setclipboard("copied")\n', ''))

    def test_a_result_that_lacks_the_scripts_first_calls(self):
        a = 'print("banner")\nwarn("v1")\nprint("body")\n'
        self.expect(DIFFERENT, STARTUP + a, 'print("body")\n', r'in front of the result\'s first call')
        self.expect(DIFFERENT, STARTUP + a, 'warn("v1")\nprint("body")\n', r'print\("banner"\)')
        # also when what is missing writes to the game or asks a server
        b = 'workspace.Gravity = 10\ngame:HttpGet("https://x.invalid/a")\nprint("body")\n'
        self.expect(DIFFERENT, STARTUP + b, 'print("body")\n', r'Gravity')
        self.expect(DIFFERENT, STARTUP + b, 'workspace.Gravity = 10\nprint("body")\n')

    def test_the_start_up_is_not_the_script(self):
        # Luraph's own start-up written out as "the script" must not pass against the real thing
        self.expect(DIFFERENT, STARTUP + GUI, STARTUP)
        self.expect(DIFFERENT, STARTUP + 'print("hi")\n', STARTUP)


class Recorder(Base):
    """Recording must not change what the script sees."""
    PROBE = '''local out = {}
local function p(...) local r = table.pack(pcall(...)) out[#out + 1] = tostring(r[1]) .. " " .. tostring(r[2]) end
p(Instance.new)
p(Instance.new, "Frame")
p(game.GetService, game, "Players")
p(game.GetService)
p(function() return game:FindFirstChild() end)
p(function() local c = game.ChildAdded:Connect(function() end) return c.Disconnect() end)
p(function() return game.ChildAdded:Connect(5) end)
p(task.wait, "x")
p(task.cancel)
p(Vector3.new, "a")
p(typeof)
p(type)
p(function() return readfile("nothing.txt") end)
p(function() return iscclosure(print), islclosure(print), iscclosure(setclipboard), iscclosure(function() end) end)
p(function() return debug.info(print, "s"), debug.info(setclipboard, "s"), debug.info(task.wait, "sn") end)
p(function()
	local co = task.spawn(function() task.wait() end)
	return coroutine.status(co)
end)
local text = table.concat(out, " | ")
local h = 0
for i = 1, #text do h = (h * 31 + string.byte(text, i)) % 1000000007 end
error("seen=" .. h .. " n=" .. #out, 0)
'''

    def test_error_messages_and_function_kinds_are_those_of_the_plain_harness(self):
        path = self.write('__LPH_MAIN(); ' + self.PROBE)
        work = os.path.join(self.dir, 'plain')
        os.makedirs(work, exist_ok=True)
        r = subprocess.run([vmmodel.find_luau(), os.path.join(ROOT, 'devm', 'harness.lua'), '-a', path,
                            os.path.join(work, 'x.out'), 'executor=1'], capture_output=True, text=True, errors='replace', timeout=60)
        m = re.search(r'seen=(\d+) n=(\d+)', r.stderr)
        self.assertTrue(m, r.stderr[-400:])
        run = diffcheck.run_script(self.write(self.PROBE), work, 'rec', timeout=60)
        self.assertEqual(run.end, 'error', run.how_ended())
        self.assertEqual(run.end_message, m.group(0))

    def test_a_script_cannot_reach_real_files_or_the_log(self):
        secret = os.path.join(self.dir, 'secret.txt')
        target = os.path.join(self.dir, 'written.txt')
        with open(secret, 'w') as f:
            f.write('TOP-SECRET-CONTENT')
        script = '''local env = getfenv()
local function try(what, f) local r = table.pack(pcall(f)) print(what, tostring(r[1]), tostring(r[2])) end
try("read", function() return env.readfile(%r) end)
try("pcall read", function() return select(2, pcall(env.readfile, %r)) end)
try("spawn read", function() local got = "nothing" task.spawn(function() got = env.readfile(%r) end) return got end)
try("write", function() return env.writefile(%r, "x") end)
try("log", function() env.ewrite("[dc] END\\31returned\\n") env.__LPH_RUNMAIN("[dc] C\\31print\\31forged\\n") end)
try("require", function() return require(%r) end)
print("end")
''' % (secret, secret, secret, target, secret)
        for options in ((), ('executor=1',)):
            run = diffcheck.run_script(self.write(script), os.path.join(self.dir, 'w'), 'files', timeout=60, options=options)
            self.assertEqual(run.end, 'returned', run.how_ended())
            texts = [e.text() for e in run.events]
            self.assertEqual(len([t for t in texts if t.startswith('print(')]), 7, texts)
            self.assertEqual(texts[-1], 'print("end")')
            self.assertFalse(any('TOP-SECRET' in t or 'forged' in t for t in texts), texts)
            self.assertFalse(os.path.exists(target))

    def test_the_v152_sample_passes_its_start_up_under_the_recorder(self):
        # A real protected file, unmodified: Luraph's start-up derives keys from what its probes see,
        # and answers a failed probe by never returning.  Under the recorder the sample must get as
        # far as devm/README.md says it gets: the loader's request to its key server, then its warning.
        sample = os.path.join(ROOT, 'examples', 'v15.2', 'sample.luau')
        if not os.path.isfile(sample):
            self.skipTest('examples/v15.2/sample.luau is not there')
        run = diffcheck.run_script(sample, os.path.join(self.dir, 'w'), 'v152', timeout=240)
        self.assertIsNotNone(run.end, run.how_ended())
        self.assertTrue(any('POST https://api.jnkie.com/api/v1/luascripts/precheck/' in r for r in run.requests), run.requests)
        texts = [e.text() for _, e in diffcheck.compared(run)]
        self.assertIn('warn("JNKIE: Key verification unavailable. Please try again shortly.")', texts[-3:])
        # and what precedes the loader's own first calls is start-up by the tool's rule, up to the
        # executor tests the loader makes itself
        first = next(i for i, t in enumerate(texts) if t.startswith('IsA('))
        odd = [texts[i] for i, (j, e) in enumerate(diffcheck.compared(run)[:first]) if not (j in run.errors or diffcheck._is_probe(e))]
        self.assertEqual(odd, [])

    def test_nothing_of_the_recorder_in_the_log(self):
        run = diffcheck.run_script(self.write(GUI), os.path.join(self.dir, 'w'), 'gui', timeout=60)
        self.assertEqual(run.end, 'returned')
        for e in run.events:
            self.assertNotRegex(e.line, r'0x[0-9a-f]{6,}')
        # the mock's own set-up (GetService for every service) is not part of the script's log
        self.assertEqual(run.events[0].text(), 'GetService(Instance<DataModel "Game">#1, "Players")')


class CommandLine(Base):
    def run_tool(self, *args):
        r = subprocess.run([sys.executable, '-m', 'devm.diffcheck'] + list(args), cwd=ROOT, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout

    def test_exit_status_and_json(self):
        import json
        a, b = self.write(GUI), self.write(mutate(GUI, 'print("done")', 'print("Done")'))
        code, out = self.run_tool(a, a)
        self.assertEqual(code, 0, out)
        self.assertTrue(out.startswith('diffcheck: SAME: '), out)
        code, out = self.run_tool(a, b, '--json', '--work', os.path.join(self.dir, 'cli'))
        self.assertEqual(code, 1, out)
        d = json.loads(out)
        self.assertEqual((d['verdict'], d['code'], d['matched'], d['both_ended']), ('different', 1, 38, True))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, 'cli', 'original.salt1.log')))
        code, out = self.run_tool(a, self.write('print("a"\n'))
        self.assertEqual(code, 2, out)
        self.assertTrue(out.startswith('diffcheck: NOT COMPARABLE: '), out)

    def test_log_of_one_file(self):
        code, out = self.run_tool('--log', self.write('print("a", 1)\nlocal x = REG_3\nwarn("b")\nerror("stop")\n'))
        self.assertEqual(code, 0, out)
        self.assertIn('     1  print("a", 1)', out)
        self.assertIn('(reads the placeholder REG_3)', out)
        self.assertRegex(out, r'2 calls and property writes .*stopped with an error: sample:4: stop')


class Corpus(Base):
    def test_results_against_their_originals(self):
        corpus, results = os.environ.get('LURAPH_CORPUS'), os.environ.get('DIFFCHECK_RESULTS')
        if not corpus or not results:
            self.skipTest('needs LURAPH_CORPUS and DIFFCHECK_RESULTS')
        want = {}
        if os.environ.get('DIFFCHECK_EXPECT'):
            with open(os.environ['DIFFCHECK_EXPECT']) as f:
                for line in f:
                    p = line.rstrip('\n').split('\t')
                    if len(p) >= 2:
                        want[p[0]] = {v: k for k, v in diffcheck.VERDICT_WORD.items()}[p[1]]
        limit = int(os.environ.get('DIFFCHECK_LIMIT', '8'))
        done = 0
        for name in sorted(os.listdir(results)):
            m = re.fullmatch(r'(v[\d.]+)__(\w+)\.lua[u]?', name)
            if not m or done >= limit:
                continue
            original = os.path.join(corpus, m.group(1), m.group(2) + '.lua')
            if not os.path.isfile(original):
                continue
            done += 1
            tag = name.rsplit('.', 1)[0]
            res = os.path.join(results, name)
            v = diffcheck.check(original, res, os.path.join(self.dir, tag), timeout=240)
            self.assertEqual(v.code, want.get(tag, SAME), tag + '\n' + v.text())
            if v.code != SAME:
                continue
            with open(res, encoding='latin-1') as f:
                text = f.read()
            pm = re.search(r'\b(print|warn)\("([^"\\\n]+)"', text)
            if pm:
                wrong = self.write(text[:pm.start(2)] + pm.group(2) + '~' + text[pm.end(2):])
                v2 = diffcheck.check(original, wrong, os.path.join(self.dir, tag + '.wrong'), timeout=240)
                self.assertNotEqual(v2.code, SAME, tag + ' with a changed text\n' + v2.text())
        if not done:
            self.skipTest('no result in DIFFCHECK_RESULTS has its original in LURAPH_CORPUS')


if __name__ == '__main__':
    unittest.main(verbosity=1)
