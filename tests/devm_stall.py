"""Checks for devm's traced run of a script that does not end (a key system waiting for its key, a GUI
script in its event loop, `while true do task.wait() end`).  Synthetic probes only: hand-written
Lua that calls the harness's hooks the way an instrumented VM chunk does, run through
devm/harness.lua and its mock with small limits.

    python3 tests/devm_stall.py            (from the repository root; seconds, needs bin/luau)

* a script whose own code has started (a program on a second interpreter, a line of output, a
  request) and then loops over code that already ran -- VM code, a task.wait() / wait() loop, an
  event's :Wait(), a plain-Lua loop -- is stopped by the harness's watch (harness.lua watch.tick):
  the `[stalled]` line says why, the oracle and the end-state dump are written, and the run ends
  within seconds;
* the script cannot hold the run up by catching the stop (pcall in a loop);
* a run that loops before showing anything of the script ends without a dump (Luraph's start-up
  code spinning: nothing of the script to lift);
* a run that keeps making new code is stopped at the run limit; a run that ends is left alone;
* waits of a coroutine other than the main thread do not count;
* the exercise pass cannot hang in a plain-Lua loop of an exercised function;
* Luraph's tamper response (a function handed another program's code arrays) is stopped before it
  changes anything; before the script showed itself that is a failure without a dump;
* the harness says which programs the script's output came from (`[effects]`), where a stopped run
  was (`[where]`) and which program called each program first (`caller`); pipeline.script_programs
  and pipeline.entered_program take the script from that evidence (helpers on other interpreters
  are not the script; a program the start-up code entered at its end and never came back from is);
* serve mode starts after a stalled run (vmmodel.start_server / run_oracle, the route the lifter
  uses for lazily decoded constants), and run_oracle passes the `[stalled]` line on as a note;
* the instrumented copy of a VM chunk counts the rounds of its plain-Lua loops (Model.patched_source);
* the report: a stalled run is PARTIAL with its `[stalled]` line, also read back off a file's head;
  the front end's final line is `result: PARTIAL: the script did not end: ... (devm)`, and devm's
  result stands (the second engine is not tried).
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from devm import run, vmmodel  # noqa: E402
import allinone  # noqa: E402

HARNESS = os.path.join(ROOT, 'devm', 'harness.lua')
SAMPLE = os.path.join(ROOT, 'examples', 'v15.2', 'sample.luau')
# what Model.patched_source puts at the head of a plain-Lua while / repeat loop body
LOOP = 'if __LPH_N.q then __LPH_W() end;__LPH_N.l=__LPH_N.l+1;if __LPH_N.l>__LPH_N.k then __LPH_L() end;'


def have_luau():
    try:
        return bool(vmmodel.find_luau())
    except RuntimeError:
        return False


# Probes.  __LPH_PROTO(interp, ...) is what the closure maker of an instrumented chunk calls for every
# program it makes, __LPH_STEP(pid, loop, pc, registers) what a dispatch loop calls per instruction.
PAYLOAD_VM_LOOP = '''
local regs = {}
local boot = __LPH_PROTO(0, {"K"}, 1, {1, 2, 3})
__LPH_STEP(boot, 0, 1, regs)
local main = __LPH_PROTO(1, {"K"}, 1, {4, 5, 6})
local function f() return 1 end
local function g() return 2 end
while true do __LPH_STEP(main, 0, 1, regs); __LPH_STEP(main, 0, 2, regs) end
'''
TASK_WAIT_LOOP = '''
local regs = {}
local p = __LPH_PROTO(0, {"K"}, 1, {1})
__LPH_STEP(p, 0, 1, regs)
print("[Probe] starting")
warn("[Probe] no key given")
while true do task.wait(); __LPH_STEP(p, 0, 2, regs) end
'''
LEGACY_WAIT_LOOP = '''
local regs = {}
local p = __LPH_PROTO(0, {"K"}, 1, {1})
print("x")
while true do wait(1); __LPH_STEP(p, 0, 2, regs) end
'''
HEARTBEAT_LOOP = '''
local regs = {}
local p = __LPH_PROTO(0, {"K"}, 1, {1})
local rs = game:GetService("RunService")
print("x")
while true do rs.Heartbeat:Wait(); __LPH_STEP(p, 0, 2, regs) end
'''
EVENT_NEVER_FIRED = '''
local p = __LPH_PROTO(0, {"K"}, 1, {1})
local ev = Instance.new("BindableEvent")
print("waiting for a click")
while true do ev.Event:Wait() end
'''
PLAIN_LOOP = '''
local p = __LPH_PROTO(0, {"K"}, 1, {1})
print("x")
local n = 0
while true do %s n = n + 1 end
''' % LOOP
CATCHES_THE_STOP = '''
local regs = {}
local p = __LPH_PROTO(0, {"K"}, 1, {1})
print("x")
while true do
    %s
    pcall(function() while true do __LPH_STEP(p, 0, 1, regs) end end)
end
''' % LOOP
SILENT_SPIN = '''
local regs = {}
local p = __LPH_PROTO(0, {"K"}, 1, {1})
while true do __LPH_STEP(p, 0, 1, regs) end
'''
NEW_CODE_FOR_EVER = '''
local regs = {}
print("x")
local i = 0
while true do i += 1; local p = __LPH_PROTO(0, {"K"}, 1, {i}); __LPH_STEP(p, 0, 1, regs) end
'''
ENDS = '''
local regs = {}
local p = __LPH_PROTO(0, {"K"}, 1, {1})
for i = 1, 10 do task.wait(); __LPH_STEP(p, 0, i, regs) end
print("done")
'''
COROUTINE_WAITS = '''
local regs = {}
local p = __LPH_PROTO(0, {"K"}, 1, {1})
for i = 1, 50 do task.spawn(function() while true do task.wait(100) end end) end
for i = 1, 20 do task.wait(); __LPH_STEP(p, 0, i, regs) end
print("done")
'''
# Luraph's tamper response: a function handed the code arrays of another program (its caller's)
# scrambles them and loops for ever.  The probe's "scrambling" counts in A[1].
TAMPER = '''
local regs = {}
local A, B, C = {1, 2, 3}, {4, 5, 6}, {7, 8, 9}
local victim = __LPH_PROTO(0, {"A", "B", "C"}, 3, A, B, C)
__LPH_STEP(victim, 0, 1, regs)
%s
local tamper = __LPH_PROTO(0, {"X"}, 1, {0})
local r2 = {}
__LPH_STEP(tamper, 0, 1, r2)                -- (vararg set-up: the arguments are not in the registers yet)
r2[0], r2[1], r2[2], r2[3] = {}, A, B, C
local pc = 2
while true do __LPH_STEP(tamper, 0, pc, r2); A[1] = A[1] + 1; pc = pc %% 50 + 2 end
'''
# a function handed one array of another program, or its own arrays, is no tamper response
NOT_TAMPER = '''
local regs = {}
local A, B = {1, 2, 3}, {4, 5, 6}
local p = __LPH_PROTO(0, {"A", "B"}, 2, A, B)
__LPH_STEP(p, 0, 1, regs)
local q = __LPH_PROTO(0, {"X"}, 1, {0})
__LPH_STEP(q, 0, 1, {[0] = A, [1] = 5})
__LPH_STEP(p, 0, 2, {[0] = A, [1] = B})
print("done")
'''
# a payload function that loops for ever in plain Lua when the exercise pass calls it
EXERCISED_LOOP = '''
local regs = {}
local boot = __LPH_PROTO(0, {"K"}, 1, {1})
local pid = __LPH_PROTO(1, {"K"}, 1, {2})
local bind = __LPH_REG(pid, function() return function() local n = 0; while true do %s n = n + 1 end end end)
local closure = bind()
__LPH_STEP(pid, 0, 1, regs)
print("done")
''' % LOOP


def run_probe(text, *options, limit=120):
    """(seconds, stderr, dump or None, oracle written?) of the probe run through the harness"""
    import json
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, 'sample.lua')
        with open(src, 'w') as f:
            f.write(text)
        out = os.path.join(d, 'end.json')
        t = time.time()
        r = subprocess.run([vmmodel.find_luau(), HARNESS, '-a', src, out, 'oracle=1', 'dumpend=1'] + list(options),
                           capture_output=True, text=True, errors='replace', timeout=limit)
        took = time.time() - t
        dump = None
        if os.path.exists(out):
            with open(out) as f:
                dump = json.load(f)
        return took, r.stderr, dump, os.path.exists(out + '.oracle')


def stalled_line(stderr):
    return next((ln for ln in stderr.split('\n') if ln.startswith('[stalled] ')), None)


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class Watch(unittest.TestCase):
    OPTS = ('stall=1', 'stallwait=30', 'stallrun=20')

    def stalled(self, text, *options):
        took, err, dump, oracle = run_probe(text, *(options or self.OPTS))
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertIsNotNone(dump, err[-1500:])
        self.assertTrue(oracle, err[-1500:])
        self.assertLess(took, 30)
        self.assertIn('[oracle] script did not end', err)
        self.assertTrue(line.startswith('[stalled] the script did not end: it was stopped '), line)
        self.assertTrue(line.endswith('code it would load or fetch later is missing'), line)
        return line, err, dump

    def test_vm_loop_after_the_payload_started(self):
        line, err, dump = self.stalled(PAYLOAD_VM_LOOP)
        self.assertIn('(a program on interpreter 1)', line)
        self.assertIn('in a loop of its VM code (no new code for 1 s', line)
        self.assertEqual(len(dump['protos']), 2)

    def test_task_wait_loop(self):
        line, err, _ = self.stalled(TASK_WAIT_LOOP)
        self.assertIn('(its first line of output)', line)
        self.assertIn('waiting in task.wait()', line)
        # 30 s of waiting simulated at 1/60 s or a little more a call
        n = int(line.split(' waits, ')[0].rsplit(' ', 1)[1])
        self.assertTrue(1400 <= n <= 1801, line)
        self.assertIn('[script] warn: [Probe] no key given', err)

    def test_legacy_wait_loop(self):
        line, _, _ = self.stalled(LEGACY_WAIT_LOOP)
        self.assertIn('waiting in wait()', line)

    def test_endless_wait(self):
        line, _, _ = self.stalled(TASK_WAIT_LOOP.replace('task.wait()', 'task.wait(math.huge)'))
        self.assertIn('waiting in task.wait() (no new code for 0 s: 1 waits, an endless time of waiting simulated', line)

    def test_heartbeat_wait_loop(self):
        line, _, _ = self.stalled(HEARTBEAT_LOOP)
        self.assertIn('waiting in Heartbeat:Wait()', line)

    def test_event_nothing_fires(self):
        """An event the mock never fires counts 60 s a wait: with stallwait=120 the second one stops it."""
        line, _, _ = self.stalled(EVENT_NEVER_FIRED, 'stall=60', 'stallwait=120', 'stallrun=60')
        self.assertIn('waiting in Event:Wait()', line)
        self.assertIn(': 2 waits, 120 s of waiting simulated', line)

    def test_plain_lua_loop(self):
        line, _, _ = self.stalled(PLAIN_LOOP)
        self.assertIn('in a loop outside the VM\'s code', line)

    def test_catching_the_stop_does_not_hold_the_run(self):
        self.stalled(CATCHES_THE_STOP)

    def test_new_code_for_ever_meets_the_run_limit(self):
        line, _, _ = self.stalled(NEW_CODE_FOR_EVER, 'stall=30', 'stallwait=30', 'stallrun=2')
        self.assertIn('still running (the limit for a run after the script starts is 2 s', line)

    def test_tamper_response_is_stopped_at_its_entry(self):
        took, err, dump, oracle = run_probe(TAMPER % 'warn("no key given")', *self.OPTS)
        self.assertIn('[where] the run was stopped in program 2', err)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertIn('at the entry of Luraph\'s tamper response (program 2, handed 3 code arrays of program 1', line)
        self.assertIsNotNone(dump)
        self.assertLess(took, 10)
        # nothing was scrambled: the victim's first array is as it was (its id in the dump: proto 1's A)
        rec = dump['tables'][str(dump['protos'][0]['t'])]
        env = next(v for k, v in rec['hash'] if k == {'s': '76617273'})        # "vars"
        a = next(v for k, v in dump['tables'][str(env['t'])]['hash'] if k == {'s': '41'})     # "A"
        self.assertEqual(dump['tables'][str(a['t'])]['arr'], [1, 2, 3])

    def test_tamper_response_before_the_script_is_a_failure(self):
        took, err, dump, oracle = run_probe(TAMPER % '', *self.OPTS)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertTrue(line.startswith('[stalled] the run did not end and showed nothing of the script'), line)
        self.assertIn('tamper response', line)
        self.assertIsNone(dump)

    def test_not_a_tamper_response(self):
        took, err, dump, oracle = run_probe(NOT_TAMPER, *self.OPTS)
        self.assertIsNone(stalled_line(err), err[-1500:])
        self.assertIn('[oracle] script returned', err)

    def test_spin_before_the_script_shows_itself(self):
        """No program on a second interpreter, no output, no request: 4 x stall seconds, then the run ends
        without an oracle or a dump (a lift would be Luraph's start-up code)."""
        took, err, dump, oracle = run_probe(SILENT_SPIN, *self.OPTS)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertTrue(line.startswith('[stalled] the run did not end and showed nothing of the script'), line)
        self.assertIn('Luraph\'s start-up code', line)
        self.assertIsNone(dump)
        self.assertFalse(oracle)
        self.assertLess(took, 30)
        # the failure message devm prints keeps the line (vmmodel.harness_tail)
        self.assertIn('[stalled] the run did not end', vmmodel.harness_tail(err))

    def test_a_run_that_ends_is_left_alone(self):
        for text in (ENDS, COROUTINE_WAITS):
            took, err, dump, oracle = run_probe(text, 'stall=1', 'stallwait=1', 'stallrun=1')
            self.assertIsNone(stalled_line(err), err[-1500:])
            self.assertIn('[oracle] script returned', err)
            self.assertIsNotNone(dump)

    def test_off_without_options(self):
        """Without the options the watch is off: the old behaviour for every other harness run."""
        took, err, dump, oracle = run_probe(TASK_WAIT_LOOP.replace('while true do', 'for _ = 1, 40000 do'))
        self.assertIsNone(stalled_line(err), err[-1500:])
        self.assertIn('[oracle] script returned', err)

    def test_exercise_cannot_hang_in_a_plain_loop(self):
        took, err, dump, oracle = run_probe(EXERCISED_LOOP, 'exercise=1', *self.OPTS)
        self.assertIn('[exercise] called 1 payload closures', err)
        self.assertIsNotNone(dump, err[-1500:])
        self.assertLess(took, 60)

    def test_beat_lines(self):
        took, err, dump, oracle = run_probe(PAYLOAD_VM_LOOP, 'beat=1', *self.OPTS)
        beats = [ln for ln in err.split('\n') if ln.startswith('[beat] ')]
        self.assertTrue(beats, err[-1500:])
        self.assertIn('VM instructions', beats[-1])


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class ServeAfterStall(unittest.TestCase):
    """run_oracle on a one-line model: the harness stops the script, dumps, and stays alive to answer
    the lifter (vmmodel.start_server, PoolServer); the [stalled] line is one of the run's notes."""

    def test_serve(self):
        class Probe:
            oneline = True
            run_options = ()

            def patched_source(self, **kw):
                return TASK_WAIT_LOOP

            def step_vars(self):
                return {}

        keep = dict(vmmodel.STALL)
        vmmodel.STALL.update(stall=1, stallwait=30, stallrun=20)
        try:
            with tempfile.TemporaryDirectory() as d:
                t = time.time()
                dump, oracle, notes = vmmodel.run_oracle(Probe(), d, 'probe')
                self.assertLess(time.time() - t, 30)
                server = vmmodel.SERVER
                self.assertIsNotNone(server)
                self.assertIsNone(server.proc.poll())        # serving
                self.assertTrue(os.path.exists(os.path.join(d, 'serve', 'ready')))
                server.close()
                server.proc.wait(timeout=10)
                vmmodel.SERVER = None
        finally:
            vmmodel.STALL.clear()
            vmmodel.STALL.update(keep)
        self.assertTrue(any(n.startswith('[stalled] the script did not end') for n in notes), notes)
        self.assertTrue(any(n.startswith('[oracle] script did not end') for n in notes), notes)
        self.assertEqual(len(dump['protos']), 1)
        self.assertTrue(oracle)

    def test_options(self):
        self.assertEqual(vmmodel.stall_options(), ['stall=%d' % vmmodel.STALL['stall'], 'stallwait=%d' % vmmodel.STALL['stallwait'],
                                                   'stallrun=%d' % vmmodel.STALL['stallrun']])
        self.assertEqual(vmmodel.stall_options(['stall=5'], stallwait=7, stallrun=None),
                         ['stallwait=7', 'stallrun=%d' % vmmodel.STALL['stallrun']])
        self.assertEqual(vmmodel.stall_options(stall=0, stallwait=0, stallrun=0), ['stall=0', 'stallwait=0', 'stallrun=0'])


@unittest.skipUnless(os.path.exists(SAMPLE), 'needs the v15.2 sample')
class LoopRounds(unittest.TestCase):
    def test_instrumented_chunk_counts_loop_rounds(self):
        """The plain-Lua loops of a VM chunk (outside its interpreters) count their rounds; the
        dispatch loops do not (they have the instruction hook)."""
        from devm import container, normalize
        with tempfile.TemporaryDirectory() as d:
            c = container.unwrap(SAMPLE, d, 'sample', log=lambda *a: None)
            with open(c.chunk_src, encoding='latin-1') as f:
                text, _ = normalize.inline_protected_raw(f.read())
        m = vmmodel.Model(normalize.reflow(text), oneline=True)
        src = m.patched_source(step=True, stepvars=m.step_vars())
        self.assertGreater(src.count(LOOP), 5)
        # (a dispatch loop's body starts with the instruction hook, never with the loop count)
        self.assertNotIn(LOOP + ' __LPH_STEP(', src)
        self.assertNotIn(LOOP + '__LPH_STEP(', src)


class ScriptPrograms(unittest.TestCase):
    """pipeline.script_programs: the script is taken from the interpreter its output came from."""

    def test_helpers_on_other_interpreters(self):
        from devm import pipeline
        interps = [5] * 36 + [0] + [5] * 32 + [4] + [5] * 4      # programs 37 (interpreter 0) and 70 (4)
        notes = ['[effects] the script\'s output and requests came from programs 35 (1), 36 (1)']
        self.assertEqual(pipeline.script_programs(interps, [37, 70], notes), ([], [37, 70]))
        note = pipeline.startup_note(notes + ['[script] print: hi'], None, 1, 6, helpers=[37, 70])
        self.assertTrue(note.startswith('[startup] the script\'s output and requests came from programs of the first '
                                        'interpreter, not from the 2 program(s) the run made on other interpreters (37, 70'), note)
        self.assertIn('The script\'s top level ran inside that program', note)

    def test_payload_interpreter(self):
        from devm import pipeline
        interps = [1] * 35 + [2] * 3 + [3]
        notes = ['[effects] the script\'s output and requests came from programs 37 (2)']
        self.assertEqual(pipeline.script_programs(interps, [36, 37, 38, 39], notes), ([36, 37, 38], None))
        # without the evidence nothing changes
        self.assertEqual(pipeline.script_programs(interps, [36, 37, 38, 39], []), ([36, 37, 38, 39], None))
        self.assertEqual(pipeline.effect_programs(notes), {37: 2})


class EnteredProgram(unittest.TestCase):
    """pipeline.entered_program: the program Luraph's start-up code entered at its end and never came
    back from is the script, when the script's output came from programs that run under it."""

    @staticmethod
    def oracle(firsts):
        """{(pid, 0, pc): {'vers': [[step, '']]}} with one instruction per first step given"""
        out = {}
        for pid, steps in firsts.items():
            for pc, st in enumerate(steps):
                out[(pid, 0, pc)] = {'vers': [[st, '']]}
        return out

    def test_hand_over(self):
        from devm import pipeline
        callers = {2: 1, 3: 1, 4: 3, 5: 4, 6: 3}
        oracle = self.oracle({1: [1, 2, 3, 99], 2: [10], 3: [100, 101], 4: [102], 5: [103], 6: [104]})
        got = pipeline.entered_program({5: 1, 6: 1}, callers, oracle)
        self.assertEqual(got[0], 3)
        self.assertIn('program 3 is the script: the first program (Luraph\'s start-up code) called it at step 100 and '
                      'ran no new instruction after that', got[1])

    def test_top_level_in_the_first_program(self):
        from devm import pipeline
        callers = {2: 1, 3: 2}
        # the first program said something itself
        self.assertIsNone(pipeline.entered_program({1: 1, 3: 1}, callers, self.oracle({1: [1], 2: [5], 3: [6]})))
        # ... or went on with code of its own after it called the program the output came from
        oracle = self.oracle({1: [1] + list(range(10, 60)), 2: [5], 3: [6]})
        self.assertIsNone(pipeline.entered_program({3: 1}, callers, oracle))
        # a few new instructions of the start-up code's own epilogue are allowed
        oracle = self.oracle({1: [1, 2] + list(range(10, 20)), 2: [5], 3: [6]})
        self.assertEqual(pipeline.entered_program({3: 1}, callers, oracle)[0], 2)

    def test_stopped_in(self):
        from devm import pipeline
        self.assertEqual(pipeline.stopped_in(['[where] the run was stopped in program 7']), {7: 1})
        self.assertEqual(pipeline.stopped_in(['[stalled] x']), {})
        got = pipeline.entered_program({5: 1}, {2: 1, 5: 2}, self.oracle({1: [1], 2: [10], 5: [11]}),
                                       evidence='the run was stopped in')
        self.assertIn('the run was stopped in program(s) 5', got[1])

    def test_no_evidence(self):
        from devm import pipeline
        self.assertIsNone(pipeline.entered_program({}, {2: 1}, self.oracle({1: [1], 2: [5]})))
        # (a chain that does not reach the first program)
        self.assertIsNone(pipeline.entered_program({3: 1}, {3: 0}, self.oracle({1: [1], 3: [5]})))


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class Effects(unittest.TestCase):
    def test_effects_line_names_the_program(self):
        text = '''
local regs = {}
local a = __LPH_PROTO(0, {"K"}, 1, {1})
local b = __LPH_PROTO(0, {"K"}, 1, {2})
__LPH_STEP(a, 0, 1, regs)
__LPH_STEP(b, 0, 1, regs)
print("from b")
warn("from b again")
__LPH_STEP(a, 0, 2, regs)
print("from a")
'''
        took, err, dump, oracle = run_probe(text, 'stall=5')
        line = next((ln for ln in err.split('\n') if ln.startswith('[effects] ')), None)
        self.assertEqual(line, '[effects] the script\'s output and requests came from programs 1 (1), 2 (2)')
        # each program's caller: the program of the instruction before its first one
        from devm import pe
        d = pe.Dump(dump, None)
        callers = [d.conv(p, 'x').h.get('caller') for p in dump['protos']]
        self.assertEqual(callers, [0, 1])


class Report(unittest.TestCase):
    STALL = ('[stalled] the script did not end: it was stopped 91 s after it started (its first line of output), '
             'waiting in task.wait() (no new code for 90 s: 5400 waits, 90 s of waiting simulated, 21600 VM '
             'instructions); code it would load or fetch later is missing')
    NOTES = ['[oracle] script did not end (stopped by the watch on a run that does not end) after 1200 instructions',
             '[script] print: [Probe] starting', STALL]

    def test_run_report(self):
        lines = run.run_report(self.NOTES)
        self.assertIn('traced run: the script did not end and was stopped, see the [stalled] line (1200 VM instructions)', lines)
        self.assertIn(self.STALL, lines)

    def test_partial(self):
        lines = run.report('local x = 1\nprint(x)\n', notes=self.NOTES, parse=(True, None, ''))
        self.assertEqual(lines[0], 'result: PARTIAL')
        self.assertTrue(any(ln.startswith('  the script did not end') for ln in lines), lines)

    def test_read_off_the_head(self):
        text = '-- x.lua, %s.  How the run went and what this file lacks:\n--   %s\n--   result: PARTIAL\nlocal x = 1\n' % (
            run.HEAD_MARK, self.STALL)
        lines = run.report(text)
        self.assertEqual(lines[0], 'result: PARTIAL')
        self.assertTrue(any(ln.startswith('  the script did not end') for ln in lines), lines)


class FrontEnd(unittest.TestCase):
    """allinone.run_devm reads devm's [stalled] line into the result; hand_over keeps devm's result and
    its final line says that the script did not end."""

    def setUp(self):
        self.saved = allinone._stream, allinone.say, allinone.run_deobf
        self.said = []
        allinone.say = lambda *a: self.said.append(' '.join(str(x) for x in a))
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        allinone._stream, allinone.say, allinone.run_deobf = self.saved
        self.tmp.cleanup()

    def fake_devm(self, report):
        def stream(cmd, timeout, **kw):
            out = cmd[cmd.index('-o') + 1]
            with open(out, 'w') as f:
                f.write('-- x\nlocal key = getgenv().Key\nif not key then warn("no key given") end\n')
            return 0, 'written %s (3 lines)\n%s\n' % (out, '\n'.join(report))
        allinone._stream = stream

    def test_stalled_result_stands(self):
        self.fake_devm(['traced run: the script did not end and was stopped, see the [stalled] line (1200 VM instructions)',
                        Report.STALL, 'result: PARTIAL', '  the script did not end (see the [stalled] line above): ...'])
        tried = []
        allinone.run_deobf = lambda *a, **k: tried.append(1) or {'ok': False, 'reason': 'x', 'why': [], 'engine': 'deobf'}
        out = os.path.join(self.tmp.name, 'out.lua')
        rc = allinone.hand_over('in.lua', out, 'devm', work=os.path.join(self.tmp.name, 'w'))
        self.assertEqual(rc, allinone.PARTIAL)
        self.assertFalse(tried)
        self.assertTrue(os.path.exists(out))
        last = self.said[-1]
        self.assertTrue(last.startswith('result: PARTIAL: the script did not end: it was stopped 91 s after it started'), last)
        self.assertIn('code it would load or fetch later is missing', last)
        self.assertTrue(last.endswith('(devm)'), last)

    def test_startup_result_says_so(self):
        self.fake_devm([Report.STALL, 'start-up code: the run created no program on an interpreter other than the first '
                        'one (4 modelled): ...', 'result: PARTIAL'])
        res = allinone.run_devm('in.lua', os.path.join(self.tmp.name, 'o.lua'), work=os.path.join(self.tmp.name, 'w'))
        self.assertTrue(res['ok'])
        self.assertFalse(res['complete'])
        self.assertTrue(res['why'][0].startswith('the script did not end'), res['why'])
        self.assertIn('begins with Luraph\'s start-up code', res['why'][1])


if __name__ == '__main__':
    unittest.main()
