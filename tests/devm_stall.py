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
* the script cannot hold the run up by catching the stop (pcall in a loop), also not by waiting on
  in a loop without a hook (each wait raises the stop again);
* a run that spins before showing anything of the script ends without a dump (Luraph's start-up
  code spinning: nothing of the script to lift); one that waits in a loop before showing anything
  else is dumped and lifted (the start-up code does not wait: the waits are the script's), and the
  first program's note says so; the [started] line counts the waits before it;
* a run that keeps making new code is stopped at the run limit; a run that ends is left alone;
* waits of a coroutine other than the main thread do not count;
* the exercise pass cannot hang in a plain-Lua loop of an exercised function;
* Luraph's tamper response (a function handed another program's code arrays) is stopped before it
  changes anything; before the script showed itself that is a failure without a dump; tamper=0
  (devm --no-tamper-stop) lets it run, and the time limits stop it;
* the harness says which programs the script's output came from (`[effects]`), under which program
  of another interpreter on its VM call chain each line ran (`[under]`, with binds=1), where a
  stopped run was and under which program (`[where]`) and which program called each program first
  (`caller`); pipeline.script_programs and pipeline.entered_program take the script from that
  evidence, weighed by counts (helpers on other interpreters are not the script, also when one
  program the output came from was first called from a helper, or when a run without output was
  stopped outside them; in a run that did not end, a program the start-up code entered at the end
  of its run is lifted on its own, PARTIAL, with a note that the first program's code before the
  call is missing and `--root 1`; a run that ended keeps the first program, whose note names the
  entered program);
* a program made on another interpreter is the script's start only once the run is in or under it:
  a spin of the start-up code after it made a helper ends without a dump, and the [stalled] line
  names the program the run was stopped in or under, or the output, never a helper;
* serve mode starts after a stalled run (vmmodel.start_server / run_oracle, the route the lifter
  uses for lazily decoded constants), and run_oracle passes the `[stalled]` line on as a note;
* the instrumented copy of a VM chunk counts the rounds of its plain-Lua loops (Model.patched_source);
* the report: a stalled run is PARTIAL with its `[stalled]` line, a result lifted from the entered
  program is PARTIAL with its `script program:` line, both also read back off a file's head; the
  front end's final line is `result: PARTIAL: the script did not end: ... (devm)`, and devm's
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
# the script catches the stop and then waits on in code without a hook (a plain-Lua `for` loop): the
# wait itself raises the stop again
CATCH_THEN_WAIT = '''
local regs = {}
local p = __LPH_PROTO(0, {"K"}, 1, {1})
local ev = Instance.new("BindableEvent")
print("x")
pcall(function() while true do task.wait(); __LPH_STEP(p, 0, 1, regs) end end)
for _ = 1, 2 ^ 40 do pcall(function() end); %s end
print("not reached")
'''
# a script that shows nothing (its top level in the first program) and sits in a wait loop
SILENT_WAIT = '''
local regs = {}
local p = __LPH_PROTO(0, {"K"}, 1, {1})
__LPH_STEP(p, 0, 1, regs)
local gui = Instance.new("ScreenGui")
while true do task.wait(); __LPH_STEP(p, 0, 2, regs) end
'''
WAITS_THEN_OUTPUT = '''
local regs = {}
local p = __LPH_PROTO(0, {"K"}, 1, {1})
for i = 1, 3 do task.wait(); __LPH_STEP(p, 0, i, regs) end
print("ready")
'''
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
        # (what showed the start, in the words of pipeline's notes on programs of other interpreters)
        self.assertIn('stopped 1 s after the run made program 2 on another interpreter, in a loop of its VM code (no new '
                      'code for 1 s', line)
        self.assertEqual(len(dump['protos']), 2)

    def test_task_wait_loop(self):
        line, err, _ = self.stalled(TASK_WAIT_LOOP)
        self.assertIn(' after its first line of output, waiting in task.wait()', line)
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

    def test_a_wait_after_the_stop_raises_it_again(self):
        """The script catches the stop, then waits (or prints) in a loop the harness has no hook in: each
        wait raises the stop again (else the run would spin until start_server's last-resort limit)."""
        for wait in ('task.wait()', 'wait()', 'ev.Event:Wait()', 'game:GetService("RunService").Heartbeat:Wait()',
                     'print("again")', 'warn("again")'):
            took, err, dump, oracle = run_probe(CATCH_THEN_WAIT % wait, *self.OPTS, limit=60)
            self.assertIsNotNone(stalled_line(err), (wait, err[-1500:]))
            self.assertIsNotNone(dump, (wait, err[-1500:]))
            self.assertNotIn('not reached', err)
            self.assertLess(took, 30, wait)

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

    def test_tamper_stop_can_be_switched_off(self):
        """tamper=0 (devm --no-tamper-stop): the function runs, changes the victim's array, and the time limits
        stop the run."""
        took, err, dump, oracle = run_probe(TAMPER % 'warn("no key given")', 'tamper=0', *self.OPTS)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertNotIn('tamper response', line)
        self.assertIn('in a loop of its VM code', line)
        rec = dump['tables'][str(dump['protos'][0]['t'])]
        env = next(v for k, v in rec['hash'] if k == {'s': '76617273'})        # "vars"
        a = next(v for k, v in dump['tables'][str(env['t'])]['hash'] if k == {'s': '41'})     # "A"
        self.assertNotEqual(dump['tables'][str(a['t'])]['arr'][0], 1)

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

    def test_silent_wait_loop_before_the_start_is_lifted(self):
        """A wait loop before the script showed anything else: Luraph's start-up code does not wait, so the
        waits are the script's (its top level in the first program): unlike a silent spin, the state is
        dumped and lifted, and the line says so."""
        took, err, dump, oracle = run_probe(SILENT_WAIT, *self.OPTS)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertNotIn('[started]', err)
        self.assertTrue(line.startswith('[stalled] the script did not end: it was stopped after '), line)
        from devm import pipeline
        self.assertIn(pipeline.WAITED_FIRST, line)
        self.assertIn('waiting in task.wait()', line)
        self.assertIn('the waits are the script\'s own code', line)
        self.assertIsNotNone(dump)
        self.assertTrue(oracle)
        self.assertLess(took, 30)
        # the first program's note does not claim the script showed nothing
        note = pipeline.startup_note(['[oracle] script did not end', line], None, 1, 2)
        self.assertIn('The run was stopped while the script waited, before it showed anything else', note)

    def test_waits_before_the_start_are_counted(self):
        took, err, dump, oracle = run_probe(WAITS_THEN_OUTPUT, *self.OPTS)
        self.assertIn('[started] the script\'s own code runs (its first line of output) after 3 VM instructions and 3 waits '
                      'of the main thread', err)

    def test_a_run_that_ends_is_left_alone(self):
        for text in (ENDS, COROUTINE_WAITS):
            took, err, dump, oracle = run_probe(text, 'stall=1', 'stallwait=1', 'stallrun=1')
            self.assertIsNone(stalled_line(err), err[-1500:])
            self.assertIn('[oracle] script returned', err)
            self.assertIsNotNone(dump)
            # how close it came to the limits (the measure the defaults were checked with on the corpus)
            line = next((ln for ln in err.split('\n') if ln.startswith('[watch] ')), '')
            self.assertRegex(line, r'^\[watch\] \d+ s of processor time; the script started at \d+ s \(its first line of '
                                   r'output\); longest stretch without new code: \d+ s before the start, \d+ s after it; ')

    def test_no_time_limits_without_options(self):
        """Without stall= / stallwait= / stallrun= the watch has no time limits: a long wait loop runs to its
        end.  (oracle=1 dumpend=1 still turn the watch on: [effects], callers, the tamper-response check.)"""
        took, err, dump, oracle = run_probe(TASK_WAIT_LOOP.replace('while true do', 'for _ = 1, 40000 do'))
        self.assertIsNone(stalled_line(err), err[-1500:])
        self.assertIn('[oracle] script returned', err)
        self.assertIn('[started] the script\'s own code runs (its first line of output)', err)

    def test_exercise_cannot_hang_in_a_plain_loop(self):
        took, err, dump, oracle = run_probe(EXERCISED_LOOP, 'exercise=1', *self.OPTS)
        self.assertIn('[exercise] called 1 payload closures', err)
        self.assertIsNotNone(dump, err[-1500:])
        self.assertLess(took, 60)

    def test_limits_are_processor_time(self):
        """The limits count the harness's processor time (/proc/self/stat), not wall-clock time: a run that
        waits for a processor (here: stopped for 3 s) does not reach them sooner."""
        import signal
        if not os.path.exists('/proc/self/stat'):
            self.skipTest('no /proc')
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'sample.lua')
            with open(src, 'w') as f:
                f.write(PAYLOAD_VM_LOOP)
            p = subprocess.Popen([vmmodel.find_luau(), HARNESS, '-a', src, os.path.join(d, 'end.json'), 'oracle=1', 'dumpend=1',
                                  'stall=2', 'stallwait=30', 'stallrun=20'], stderr=subprocess.PIPE, text=True, errors='replace')
            time.sleep(0.3)
            p.send_signal(signal.SIGSTOP)
            time.sleep(3)
            p.send_signal(signal.SIGCONT)
            err = p.communicate(timeout=120)[1]
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertIn('in a loop of its VM code (no new code for 2 s', line)
        self.assertIn('s of processor time;', next(ln for ln in err.split('\n') if ln.startswith('[watch] ')))

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
                                                   'stallrun=%d' % vmmodel.STALL['stallrun'], 'tamper=1'])
        self.assertEqual(vmmodel.stall_options(['stall=5', 'tamper=0'], stallwait=7, stallrun=None),
                         ['stallwait=7', 'stallrun=%d' % vmmodel.STALL['stallrun']])
        self.assertEqual(vmmodel.stall_options(stall=0, stallwait=0, stallrun=0, tamper=0),
                         ['stall=0', 'stallwait=0', 'stallrun=0', 'tamper=0'])

    def test_devm_option(self):
        """devm.run --no-tamper-stop reaches the harness as tamper=0 (through deob.py / allinone.py: --engine-args)."""
        seen = {}

        def fake(path, workdir, **kw):
            seen.update(kw)
            raise run.DevmError('no model', 'stop here')
        import signal
        keep, handlers = run.deobfuscate, {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGHUP)}
        run.deobfuscate = fake
        try:
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(SystemExit):
                    run.main([os.path.join(d, 'x.lua'), '-o', os.path.join(d, 'o.lua'), '--work', d, '--no-tamper-stop'])
                self.assertIs(seen.get('tamper_stop'), False)
                with self.assertRaises(SystemExit):
                    run.main([os.path.join(d, 'x.lua'), '-o', os.path.join(d, 'o.lua'), '--work', d])
                self.assertIsNone(seen.get('tamper_stop'))
        finally:
            run.deobfuscate = keep
            for n, h in handlers.items():
                signal.signal(n, h)


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
        interps = [3, 3, 3, 1, 3, 2, 3]      # programs 4 (interpreter 1) and 6 (interpreter 2)
        notes = ['[effects] the script\'s output and requests came from programs 2 (1), 3 (1)']
        self.assertEqual(pipeline.script_programs(interps, [4, 6], notes), ([], [4, 6]))
        note = pipeline.startup_note(notes + ['[script] print: hi'], None, 1, 3, helpers=[4, 6])
        self.assertTrue(note.startswith('[startup] the script\'s output and requests came from programs of the first '
                                        'interpreter, not from the 2 program(s) the run made on other interpreters (4, 6'), note)
        self.assertIn('The script\'s top level ran inside that program', note)

    def test_payload_interpreter(self):
        from devm import pipeline
        interps = [1] * 4 + [2] * 3 + [3]
        notes = ['[effects] the script\'s output and requests came from programs 6 (2)']
        self.assertEqual(pipeline.script_programs(interps, [5, 6, 7, 8], notes), ([5, 6, 7], None))
        # without the evidence nothing changes
        self.assertEqual(pipeline.script_programs(interps, [5, 6, 7, 8], []), ([5, 6, 7, 8], None))
        self.assertEqual(pipeline.effect_programs(notes), {6: 2})

    def test_output_from_a_function_the_script_called(self):
        """Without the call chains (no [under] line: a run without binds=1 or exercise=1), the output came from
        a routine of the first interpreter (4) whose first instruction ran right after one of program 5 on
        interpreter 2.  That first caller is the only link to program 5, and it says nothing about where the
        routine's output ran (a helper of the build can have called it first): the line counts for neither
        interpreter, and with nothing else the first interpreter is kept (the call chains decide this case, see
        CallChain.test_output_of_a_routine_counts_for_its_caller)."""
        from devm import pipeline
        interps = [1, 1, 1, 1, 2]
        notes = ['[effects] the script\'s output and requests came from programs 4 (1)']
        callers = {2: 1, 3: 2, 4: 5, 5: 1}
        self.assertEqual(pipeline.script_votes(interps, [5], notes, callers, 1), ({}, 'undecided'))
        self.assertEqual(pipeline.script_programs(interps, [5], notes, callers, 1), ([], [5]))
        self.assertEqual(pipeline.helpers_reason(interps, [5], notes, callers, 1),
                         'the script\'s 1 line(s) of output and requests came from programs of the first interpreter that a '
                         'program of another interpreter called first, a link that does not show where their output ran (the '
                         'run did not record the VM call chains): without other evidence the first interpreter is kept, not')
        # (the same through a routine that routine called: 3 under 4 under 5)
        notes3 = ['[effects] the script\'s output and requests came from programs 3 (2)']
        self.assertEqual(pipeline.script_programs(interps, [5], notes3, {2: 1, 3: 4, 4: 5, 5: 1}, 1), ([], [5]))
        # output from the program on interpreter 2 itself is evidence for it
        notes5 = ['[effects] the script\'s output and requests came from programs 4 (1), 5 (1)']
        self.assertEqual(pipeline.script_programs(interps, [5], notes5, callers, 1), ([5], None))
        # without the links, or when the routine runs under the first program, program 5 is a helper
        self.assertEqual(pipeline.script_programs(interps, [5], notes), ([], [5]))
        self.assertEqual(pipeline.script_programs(interps, [5], notes, {2: 1, 3: 2, 4: 1, 5: 4}, 1), ([], [5]))
        # (links that go round in a circle end the walk)
        self.assertEqual(pipeline.script_programs(interps, [5], notes, {3: 4, 4: 3}, 1), ([], [5]))

    # programs 1-4 and 6, 7 on the first interpreter (1), 5 on interpreter 2 and 8 on interpreter 3: helpers
    INTERPS = [1, 1, 1, 1, 2, 1, 1, 3]

    def test_one_program_does_not_decide(self):
        """The script's output came from programs 6 and 7 of the first interpreter, five lines each; 7 was first
        entered right after an instruction of helper 5.  Its first caller alone does not make 5 the script:
        the evidence is weighed, and an interpreter other than the first is taken only when more of it points
        there."""
        from devm import pipeline
        callers = {2: 1, 3: 1, 4: 1, 5: 1, 6: 4, 7: 5, 8: 1}
        notes = ['[effects] the script\'s output and requests came from programs 6 (5), 7 (5)']
        self.assertEqual(pipeline.script_votes(self.INTERPS, [5, 8], notes, callers), ({1: 5}, 'effects'))
        self.assertEqual(pipeline.script_programs(self.INTERPS, [5, 8], notes, callers), ([], [5, 8]))
        reason = pipeline.helpers_reason(self.INTERPS, [5, 8], notes, callers)
        tail = ('5 more from programs of the first interpreter that a program of another interpreter called first, a link '
                'that does not show where their output ran (the run did not record the VM call chains), counted for neither')
        self.assertEqual(reason, 'the script\'s output and requests came from programs of the first interpreter (5 line(s); %s), '
                                 'not from' % tail)
        note = pipeline.startup_note(notes + ['[script] print: hi'], None, 1, 3, helpers=[5, 8], reason=reason)
        self.assertTrue(note.startswith('[startup] the script\'s output and requests came from programs of the first '
                                        'interpreter (5 line(s); %s), not from the 2 program(s) the run made on other '
                                        'interpreters (5, 8: helpers)' % tail), note)
        # more lines from program 7 than from 6: still no evidence for program 5 (one first-caller link does not
        # decide, however many lines that program printed)
        notes = ['[effects] the script\'s output and requests came from programs 6 (2), 7 (5)']
        self.assertEqual(pipeline.script_programs(self.INTERPS, [5, 8], notes, callers), ([], [5, 8]))
        # output from program 5 itself is
        notes = ['[effects] the script\'s output and requests came from programs 5 (3), 6 (2), 7 (5)']
        self.assertEqual(pipeline.script_votes(self.INTERPS, [5, 8], notes, callers), ({2: 3, 1: 2}, 'effects'))
        self.assertEqual(pipeline.script_programs(self.INTERPS, [5, 8], notes, callers), ([5], None))
        self.assertEqual(pipeline.helpers_reason(self.INTERPS, [5, 8], ['[effects] the script\'s output and requests came '
                                                                         'from programs 5 (1), 6 (2)'], callers),
                         '2 of the script\'s 3 lines of output and requests came from programs of the first interpreter (1 '
                         'from programs of other interpreters), not from')

    def test_call_chain_decides(self):
        """The harness's [under] line places each line of output and request by its VM call chain: where it is
        there, the first callers are not used."""
        from devm import pipeline
        callers = {2: 1, 3: 1, 4: 1, 5: 1, 6: 4, 7: 5, 8: 1}
        notes = ['[effects] the script\'s output and requests came from programs 6 (2), 7 (5)',
                 '[under] the VM call chains of the script\'s output and requests: under no program of another '
                 'interpreter (7)']
        self.assertEqual(pipeline.under_programs(notes), {0: 7})
        self.assertEqual(pipeline.script_votes(self.INTERPS, [5, 8], notes, callers), ({1: 7}, 'under'))
        self.assertEqual(pipeline.script_programs(self.INTERPS, [5, 8], notes, callers), ([], [5, 8]))
        self.assertEqual(pipeline.helpers_reason(self.INTERPS, [5, 8], notes, callers),
                         'the script\'s output and requests came from programs of the first interpreter, not from')
        # the script on interpreter 3 calls a routine of the first interpreter (7) that prints: program 8
        notes = ['[effects] the script\'s output and requests came from programs 7 (6), 8 (1)',
                 '[under] the VM call chains of the script\'s output and requests: under program 8 (6), under no program '
                 'of another interpreter (1)']
        self.assertEqual(pipeline.under_programs(notes), {8: 6, 0: 1})
        self.assertEqual(pipeline.script_programs(self.INTERPS, [5, 8], notes, callers), ([8], None))
        # (a tie is no evidence for another interpreter)
        notes[1] = ('[under] the VM call chains of the script\'s output and requests: under program 8 (3), under no program '
                    'of another interpreter (3)')
        self.assertEqual(pipeline.script_programs(self.INTERPS, [5, 8], notes, callers), ([], [5, 8]))

    def test_stopped_run_without_output(self):
        """A run that did not end and showed no output and no request: the program it was stopped in decides,
        placed by its call chain where the [where] line gives it, else through its first callers."""
        from devm import pipeline
        callers = {2: 1, 3: 1, 4: 1, 5: 1, 6: 1, 7: 6, 8: 5}
        where = '[where] the run was stopped in program 7'
        self.assertEqual(pipeline.stopped_in([where]), {7: 1})
        self.assertIsNone(pipeline.stopped_under([where]))
        self.assertEqual(pipeline.script_votes(self.INTERPS, [5, 8], [where], callers), ({1: 1}, 'where'))
        self.assertEqual(pipeline.script_programs(self.INTERPS, [5, 8], [where], callers), ([], [5, 8]))
        self.assertEqual(pipeline.helpers_reason(self.INTERPS, [5, 8], [where], callers),
                         'the run was stopped in program 7, a program of the first interpreter (the run did not record the VM '
                         'call chains), not in or under')
        # first called from program 5: no evidence for its interpreter (that link is only the caller of program
        # 7's first instruction); the first interpreter is kept
        self.assertEqual(pipeline.script_votes(self.INTERPS, [5, 8], [where], {**callers, 7: 5}), ({}, 'undecided'))
        self.assertEqual(pipeline.script_programs(self.INTERPS, [5, 8], [where], {**callers, 7: 5}), ([], [5, 8]))
        self.assertEqual(pipeline.helpers_reason(self.INTERPS, [5, 8], [where], {**callers, 7: 5}),
                         'the run was stopped in program 7, a program of the first interpreter that program 5 of another '
                         'interpreter called first, a link that does not show where it ran (the run did not record the VM '
                         'call chains): without other evidence the first interpreter is kept, not')
        # a stop on a thread whose start is not known: like the harness's start rule, the first interpreter
        notes = [where + ', not placed: on a thread whose call chain starts there']
        self.assertEqual(pipeline.stopped_under(notes), pipeline.UNPLACED)
        self.assertEqual(pipeline.script_votes(self.INTERPS, [5, 8], notes, {**callers, 7: 5}), ({1: 1}, 'unplaced'))
        self.assertEqual(pipeline.script_programs(self.INTERPS, [5, 8], notes, {**callers, 7: 5}), ([], [5, 8]))
        # the call chain where it was stopped, when the line gives it, decides over the first callers
        for tail, got in ((', under no program of another interpreter', ([], [5, 8])),
                          (', under program 8 of another interpreter', ([8], None)),
                          (', under program 5 of another interpreter', ([5], None))):
            notes = [where + tail]
            self.assertEqual(pipeline.stopped_in(notes), {7: 1})
            self.assertEqual(pipeline.script_programs(self.INTERPS, [5, 8], notes, {7: 5}), got, tail)
        notes = ['[where] the run was stopped in program 8, on another interpreter']
        self.assertEqual(pipeline.stopped_under(notes), 8)
        self.assertEqual(pipeline.script_programs(self.INTERPS, [5, 8], notes, {}), ([8], None))
        # output and requests, where there are any, are the evidence
        notes = ['[effects] the script\'s output and requests came from programs 8 (1)', where]
        self.assertEqual(pipeline.script_programs(self.INTERPS, [5, 8], notes, callers), ([8], None))


class EnteredProgram(unittest.TestCase):
    """pipeline.entered_candidate / entered_program: the program Luraph's start-up code entered at the end of
    its run, when the script's output came from programs that run under it.  Only a run that did not end
    is lifted from that program, and the note says what the result then lacks."""

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
        self.assertEqual(pipeline.entered_candidate({5: 1, 6: 1}, callers, oracle), (3, 100, 0))
        got = pipeline.entered_program({5: 1, 6: 1}, callers, oracle, stalled=True)
        self.assertEqual(got[0], 3)
        self.assertIn('program 3 is taken for the script: the run did not end, and the first program called it at step 100 '
                      'and ran no new instruction after that', got[1])
        # what the result lacks, and how to get it
        self.assertIn('The first program\'s code before that call is not in the result: Luraph\'s start-up code, and any '
                      'part of the script\'s top level that Luraph put in that program (--root 1 lifts the first program)',
                      got[1])

    def test_a_run_that_ended_keeps_the_first_program(self):
        """A v15.2-style build: the first program is Luraph's start-up code followed by the script's top level
        (300 instructions), whose last statement calls the script's main function (program 2), which
        prints; the run returns.  Lifting program 2 alone would drop the top level: the first program is
        the result, and its note names program 2."""
        from devm import pipeline
        oracle = self.oracle({1: list(range(1, 301)) + [400, 401, 402], 2: list(range(301, 351))})
        self.assertEqual(pipeline.entered_candidate({2: 3}, {2: 1}, oracle, 1), (2, 301, 3))
        self.assertIsNone(pipeline.entered_program({2: 3}, {2: 1}, oracle, 1))
        self.assertIsNone(pipeline.entered_program({2: 3}, {2: 1}, oracle, 1, stalled=False))
        note = pipeline.startup_note(['[oracle] script returned after 402 instructions', '[script] print: hi'], None, 1, 1,
                                     entered=(2, 301, 3))
        self.assertIn('The script\'s top level ran inside that program', note)
        self.assertIn('The output came from program 2 (and what it calls), which the first program called at the end of '
                      'its run: --root 2 lifts it on its own, without the first program\'s code before the call', note)

    def test_top_level_in_the_first_program(self):
        from devm import pipeline
        callers = {2: 1, 3: 2}
        # the first program said something itself
        self.assertIsNone(pipeline.entered_program({1: 1, 3: 1}, callers, self.oracle({1: [1], 2: [5], 3: [6]}),
                                                   stalled=True))
        # ... or went on with code of its own after it called the program the output came from
        oracle = self.oracle({1: [1] + list(range(10, 60)), 2: [5], 3: [6]})
        self.assertIsNone(pipeline.entered_program({3: 1}, callers, oracle, stalled=True))
        self.assertIsNone(pipeline.entered_candidate({3: 1}, callers, oracle))
        # a few new instructions of the start-up code's own epilogue are allowed
        oracle = self.oracle({1: [1, 2] + list(range(10, 20)), 2: [5], 3: [6]})
        self.assertEqual(pipeline.entered_program({3: 1}, callers, oracle, stalled=True)[0], 2)

    def test_stopped_in(self):
        from devm import pipeline
        self.assertEqual(pipeline.stopped_in(['[where] the run was stopped in program 7']), {7: 1})
        self.assertEqual(pipeline.stopped_in(['[stalled] x']), {})
        got = pipeline.entered_program({5: 1}, {2: 1, 5: 2}, self.oracle({1: [1], 2: [10], 5: [11]}),
                                       evidence='the run was stopped in', stalled=True)
        self.assertIn('the run was stopped in program(s) 5', got[1])

    def test_no_evidence(self):
        from devm import pipeline
        self.assertIsNone(pipeline.entered_program({}, {2: 1}, self.oracle({1: [1], 2: [5]}), stalled=True))
        # (a chain that does not reach the first program)
        self.assertIsNone(pipeline.entered_program({3: 1}, {3: 0}, self.oracle({1: [1], 3: [5]}), stalled=True))


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


# Programs made on a second interpreter: one that the start-up code calls (a helper of the build), or the script.
# __LPH_REG(pid, binder) is what the closure maker calls with each program's binder; with binds=1 (devm's traced
# run) the harness learns the closures it makes, and with them the program of each closure on the call stack.
HELPER_FIRST_CALLS_ROUTINE = '''
local r0, r1, r2, r3 = {}, {}, {}, {}
local boot = __LPH_PROTO(0, {"K"}, 1, {1})
__LPH_STEP(boot, 0, 1, r0)
local routine = __LPH_PROTO(0, {"K"}, 1, {2})
local function routineFn(say) __LPH_STEP(routine, 0, 1, r2); if say then print("from the routine") end end
local helper = __LPH_PROTO(1, {"K"}, 1, {3})
local h = __LPH_REG(helper, function() return function() __LPH_STEP(helper, 0, 1, r1); routineFn(false) end end)()
h()
__LPH_STEP(boot, 0, 2, r0)
routineFn(true)
routineFn(true)
__LPH_STEP(boot, 0, 3, r0)
local other = __LPH_PROTO(0, {"K"}, 1, {4})
__LPH_STEP(other, 0, 1, r3)
for i = 1, 3 do print("from the script") end
'''
SCRIPT_CALLS_ROUTINE = '''
local r0, r1, r2 = {}, {}, {}
local boot = __LPH_PROTO(0, {"K"}, 1, {1})
__LPH_STEP(boot, 0, 1, r0)
local routine = __LPH_PROTO(0, {"K"}, 1, {2})
local function routineFn() __LPH_STEP(routine, 0, 1, r2); print("hello") end
local spid = __LPH_PROTO(1, {"K"}, 1, {3})
local m = __LPH_REG(spid, function() return function()
    __LPH_STEP(spid, 0, 1, r1)
    for i = 1, 3 do routineFn() end
    __LPH_STEP(spid, 0, 2, r1)
end end)()
m()
__LPH_STEP(boot, 0, 2, r0)
routineFn()
'''
# a helper is made, then the start-up code spins (an environment check that failed)
HELPER_THEN_SPIN = '''
local r0, r1 = {}, {}
local boot = __LPH_PROTO(0, {"K"}, 1, {1})
__LPH_STEP(boot, 0, 1, r0)
local helper = __LPH_PROTO(1, {"K"}, 1, {2})
local h = __LPH_REG(helper, function() return function() __LPH_STEP(helper, 0, 1, r1) return 7 end end)()
h()
while true do %s __LPH_STEP(boot, 0, 2, r0) end
'''
# the program on the second interpreter is the script, and loops in a routine of the first interpreter it called
SCRIPT_LOOPS_IN_ROUTINE = '''
local r0, r1, r2 = {}, {}, {}
local boot = __LPH_PROTO(0, {"K"}, 1, {1})
__LPH_STEP(boot, 0, 1, r0)
local main = __LPH_PROTO(1, {"K"}, 1, {2})
local routine = __LPH_PROTO(0, {"K"}, 1, {3})
local function spin() while true do __LPH_STEP(routine, 0, 1, r2) end end
local m = __LPH_REG(main, function() return function() __LPH_STEP(main, 0, 1, r1) spin() end end)()
m()
'''
# a helper is made first; the script, on the first interpreter, prints and loops
HELPER_THEN_SCRIPT = '''
local r0, r1 = {}, {}
local boot = __LPH_PROTO(0, {"K"}, 1, {1})
__LPH_STEP(boot, 0, 1, r0)
local helper = __LPH_PROTO(1, {"K"}, 1, {2})
local h = __LPH_REG(helper, function() return function() __LPH_STEP(helper, 0, 1, r1) return 7 end end)()
h()
local s = __LPH_PROTO(0, {"K"}, 1, {3})
__LPH_STEP(s, 0, 1, r0)
print("ready")
while true do __LPH_STEP(s, 0, 2, r0) end
'''

# the script's program (second interpreter) prints, then runs a plain function of its own in a thread
# of its own (task.spawn), which prints too: that thread's stack starts at the plain function
SCRIPT_SPAWNS_PLAIN = '''
local r0, r1, r2 = {}, {}, {}
local boot = __LPH_PROTO(0, {"K"}, 1, {1})
__LPH_STEP(boot, 0, 1, r0)
local routine = __LPH_PROTO(0, {"K"}, 1, {2})
local function routineFn() __LPH_STEP(routine, 0, 1, r2) end
local spid = __LPH_PROTO(1, {"K"}, 1, {3})
local function beat() routineFn(); print("beat") end
local m = __LPH_REG(spid, function() return function()
    __LPH_STEP(spid, 0, 1, r1)
    routineFn(); print("hello")
    task.spawn(beat)
    __LPH_STEP(spid, 0, 2, r1)
end end)()
m()
__LPH_STEP(boot, 0, 2, r0)
'''
# the same with a function of the script's program run in the thread: its closure is on that stack
SCRIPT_SPAWNS_ITS_OWN = '''
local r0, r1, r2 = {}, {}, {}
local boot = __LPH_PROTO(0, {"K"}, 1, {1})
__LPH_STEP(boot, 0, 1, r0)
local routine = __LPH_PROTO(0, {"K"}, 1, {2})
local function routineFn() __LPH_STEP(routine, 0, 1, r2) end
local spid = __LPH_PROTO(1, {"K"}, 1, {3})
local beat = __LPH_REG(spid, function() return function() __LPH_STEP(spid, 0, 3, r1); routineFn(); print("beat") end end)()
local m = __LPH_REG(spid, function() return function()
    __LPH_STEP(spid, 0, 1, r1)
    routineFn(); print("hello")
    task.spawn(beat)
    __LPH_STEP(spid, 0, 2, r1)
end end)()
m()
__LPH_STEP(boot, 0, 2, r0)
'''
# the start-up code prints, and prints again in a thread it started
STARTUP_SPAWNS = '''
local r0, r1 = {}, {}
local boot = __LPH_PROTO(0, {"K"}, 1, {1})
__LPH_STEP(boot, 0, 1, r0)
local helper = __LPH_PROTO(1, {"K"}, 1, {2})
local h = __LPH_REG(helper, function() return function() __LPH_STEP(helper, 0, 1, r1) return 7 end end)()
h()
__LPH_STEP(boot, 0, 2, r0)
print("one")
task.spawn(function() __LPH_STEP(boot, 0, 3, r0); print("two") end)
__LPH_STEP(boot, 0, 4, r0)
'''

# the script's program (second interpreter) spawns a plain function that loops in a routine of the first
# interpreter, without output (%s: what the loop does besides)
SPAWNED_LOOP = '''
local r0, r1, r2 = {}, {}, {}
local boot = __LPH_PROTO(0, {"K"}, 1, {1})
__LPH_STEP(boot, 0, 1, r0)
local routine = __LPH_PROTO(0, {"K"}, 1, {2})
local spid = __LPH_PROTO(1, {"K"}, 1, {3})
local function loop() while true do __LPH_STEP(routine, 0, 1, r2) %s end end
local m = __LPH_REG(spid, function() return function()
    __LPH_STEP(spid, 0, 1, r1)
    task.spawn(loop)
    __LPH_STEP(spid, 0, 2, r1)
end end)()
m()
__LPH_STEP(boot, 0, 2, r0)
'''
# ... a plain function of it enters Luraph's tamper response (handed the code arrays of program 1) in a thread
SPAWNED_TAMPER = '''
local r0, r1 = {}, {}
local A, B, C = {1, 2, 3}, {4, 5, 6}, {7, 8, 9}
local boot = __LPH_PROTO(0, {"A", "B", "C"}, 3, A, B, C)
__LPH_STEP(boot, 0, 1, r0)
local routine = __LPH_PROTO(0, {"K"}, 1, {2})
local spid = __LPH_PROTO(1, {"K"}, 1, {3})
local function check()
    local tamper = __LPH_PROTO(0, {"X"}, 1, {0})
    local r2 = {}
    __LPH_STEP(tamper, 0, 1, r2)
    r2[0], r2[1], r2[2], r2[3] = {}, A, B, C
    local pc = 2
    while true do __LPH_STEP(tamper, 0, pc, r2); A[1] = A[1] + 1; pc = pc % 50 + 2 end
end
local m = __LPH_REG(spid, function() return function()
    __LPH_STEP(spid, 0, 1, r1)
    task.spawn(check)
    __LPH_STEP(spid, 0, 2, r1)
end end)()
m()
__LPH_STEP(boot, 0, 2, r0)
'''
# the start-up code makes helper 2 (second interpreter), which runs routine 3 (first interpreter) first; then
# it starts a wait loop in routine 3 (%s: how) and the main thread waits; no output
STARTUP_WAIT_LOOP = '''
local r0, r1, r2 = {}, {}, {}
local boot = __LPH_PROTO(0, {"K"}, 1, {1})
__LPH_STEP(boot, 0, 1, r0)
local helper = __LPH_PROTO(1, {"K"}, 1, {2})
local routine = __LPH_PROTO(0, {"K"}, 1, {3})
local function routineFn() __LPH_STEP(routine, 0, 1, r2) end
local h = __LPH_REG(helper, function() return function() __LPH_STEP(helper, 0, 1, r1); routineFn() end end)()
h()
__LPH_STEP(boot, 0, 2, r0)
local function waiter() while true do routineFn(); task.wait() end end
%s
__LPH_STEP(boot, 0, 3, r0)
while true do task.wait(1) end
'''


# The start-up code (program 1), a routine of its interpreter (program 2) that a plain function runs in a busy
# loop, and the script's program 3 on another interpreter.  THREADS_SCRIPT makes and calls program 3, whose code
# after its first instruction is the %s; what the start-up code does before that goes between the two parts.
THREADS_HEAD = '''
local r0, r1, r2 = {}, {}, {}
local boot = __LPH_PROTO(0, {"K"}, 1, {1})
__LPH_STEP(boot, 0, 1, r0)
local routine = __LPH_PROTO(0, {"K"}, 1, {2})
local spid = __LPH_PROTO(1, {"K"}, 1, {3})
local function busy() while true do __LPH_STEP(routine, 0, 1, r2) end end
'''
THREADS_SCRIPT = '''
local m = __LPH_REG(spid, function() return function()
    __LPH_STEP(spid, 0, 1, r1)
    %s
end end)()
m()
'''


def probe_evidence(dump, err):
    """(interpreter of each program, callers, notes) of a probe run, as pipeline.script_programs takes them"""
    from devm import pe
    d = pe.Dump(dump, None)
    recs = [d.conv(p, 'x').h for p in dump['protos']]
    callers = {i + 1: int(h['caller']) for i, h in enumerate(recs) if isinstance(h.get('caller'), (int, float))}
    notes = [ln for ln in err.split('\n') if ln.startswith(('[effects]', '[under]', '[where]', '[stalled]', '[script]'))]
    return [h.get('interp') for h in recs], callers, notes


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class CallChain(unittest.TestCase):
    """The harness places each line of output and request, and the place a run was stopped, by its VM call
    chain (watch.under: the innermost program of another interpreter on the stack of the thread that ran
    the last VM instruction); a program made on another interpreter is the script's start only once the
    run is found in or under it."""
    OPTS = ('binds=1', 'stall=1', 'stallwait=30', 'stallrun=20')

    def test_output_of_a_routine_counts_for_its_caller(self):
        took, err, dump, oracle = run_probe(SCRIPT_CALLS_ROUTINE, *self.OPTS)
        self.assertIn('[effects] the script\'s output and requests came from programs 2 (4)', err)
        self.assertIn('[under] the VM call chains of the script\'s output and requests: under program 3 (3), under no '
                      'program of another interpreter (1)', err)
        from devm import pipeline
        interps, callers, notes = probe_evidence(dump, err)
        self.assertEqual(interps, [0, 0, 1])
        self.assertEqual(pipeline.script_programs(interps, [3], notes, callers), ([3], None))

    def test_a_helper_that_called_a_routine_first(self):
        """Helper 3 (second interpreter) calls routine 2 first, so the routine's first caller is the helper; then
        the start-up code calls the routine, which prints twice, and program 4 prints three times.  The call
        chains place all five lines on the first interpreter; the first callers would place two under the
        helper, which the weighing outvotes."""
        took, err, dump, oracle = run_probe(HELPER_FIRST_CALLS_ROUTINE, *self.OPTS)
        self.assertIn('[effects] the script\'s output and requests came from programs 2 (2), 4 (3)', err)
        self.assertIn('[under] the VM call chains of the script\'s output and requests: under no program of another '
                      'interpreter (5)', err)
        from devm import pipeline
        interps, callers, notes = probe_evidence(dump, err)
        self.assertEqual(callers[2], 3)
        self.assertEqual(pipeline.script_programs(interps, [3], notes, callers), ([], [3]))
        without = [n for n in notes if not n.startswith('[under]')]
        self.assertEqual(pipeline.script_votes(interps, [3], without, callers), ({0: 3}, 'effects'))
        self.assertEqual(pipeline.script_programs(interps, [3], without, callers), ([], [3]))

    def test_no_under_line_without_the_closures(self):
        """Without binds=1 (or exercise=1) the harness does not know the closures of the programs: no [under]
        line, and pipeline.script_programs follows the first callers."""
        took, err, dump, oracle = run_probe(SCRIPT_CALLS_ROUTINE, 'stall=1')
        self.assertIn('[effects] ', err)
        self.assertNotIn('[under] ', err)

    def test_start_up_code_spinning_after_a_helper_is_refused(self):
        """The run made a program on another interpreter (a helper the start-up code calls), and the start-up
        code then spins: that program is not the script's start, the rules before the start apply (4 x stall
        seconds), and the run ends without a dump, like any spin of the start-up code."""
        took, err, dump, oracle = run_probe(HELPER_THEN_SPIN % '', *self.OPTS)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertTrue(line.startswith('[stalled] the run did not end and showed nothing of the script (no output, no '
                                        'request, and it was not in or under a program it made on another interpreter '
                                        '(2)): it was stopped after 4 s'), line)
        self.assertIn('[where] the run was stopped in program 1, under no program of another interpreter', err)
        self.assertNotIn('[started]', err)
        self.assertIsNone(dump)
        self.assertFalse(oracle)
        self.assertRegex(err, r'\[watch\] \d+ s of [a-z ()]+; the script showed no output and no request \(the start: the run '
                              r'made program 2 on another interpreter at \d+ s\)')

    def test_a_silent_wait_after_a_helper(self):
        """... while a wait loop after it is the script's own code on the first interpreter, lifted (the state
        before the start that waits), and the helper is no evidence for its interpreter."""
        took, err, dump, oracle = run_probe(HELPER_THEN_SPIN % 'task.wait();', *self.OPTS)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        from devm import pipeline
        self.assertIn(pipeline.WAITED_FIRST + ' (no output, no request, and it was not in or under a program it made on '
                      'another interpreter (2)), waiting in task.wait()', line)
        self.assertIsNotNone(dump)
        interps, callers, notes = probe_evidence(dump, err)
        self.assertEqual(pipeline.script_programs(interps, [2], notes, callers), ([], [2]))

    def test_the_script_loops_in_a_routine_it_called(self):
        """The program on the second interpreter loops in a routine of the first interpreter it called: the run is
        under that program, which is the script's start; the [stalled] line names it, and the payload is its
        interpreter's."""
        took, err, dump, oracle = run_probe(SCRIPT_LOOPS_IN_ROUTINE, *self.OPTS)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertIn('stopped 1 s after the run made program 2 on another interpreter, in a loop of its VM code', line)
        self.assertIn('[where] the run was stopped in program 3, under program 2 of another interpreter', err)
        self.assertIsNotNone(dump)
        from devm import pipeline
        interps, callers, notes = probe_evidence(dump, err)
        self.assertEqual(pipeline.stopped_under(notes), 2)
        self.assertEqual(pipeline.script_programs(interps, [2], notes, callers), ([2], None))

    def test_start_reason_agrees_with_the_payload(self):
        """A helper is made first, and the script on the first interpreter prints and loops: the [stalled] line's
        start is the output, not the helper, which the payload choice sets aside."""
        took, err, dump, oracle = run_probe(HELPER_THEN_SCRIPT, *self.OPTS)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertIn('stopped 1 s after its first line of output, in a loop of its VM code', line)
        self.assertNotIn('program 2', line)
        self.assertIn('[where] the run was stopped in program 3, under no program of another interpreter', err)
        from devm import pipeline
        interps, callers, notes = probe_evidence(dump, err)
        self.assertEqual(pipeline.script_programs(interps, [2], notes, callers), ([], [2]))
        note = pipeline.startup_note(notes, None, 1, 2, helpers=[2], reason=pipeline.helpers_reason(interps, [2], notes, callers))
        self.assertTrue(note.startswith('[startup] the script\'s output and requests came from programs of the first '
                                        'interpreter, not from the 1 program(s) the run made on other interpreters (2: '
                                        'helpers)'), note)

    def test_a_thread_the_script_spawned(self):
        """The script's program prints, and a plain function of its own prints in a thread task.spawn started:
        that thread's stack starts at the plain function, but the mock's task library told the harness where
        the code that started it ran (under program 3): both lines are placed there."""
        took, err, dump, oracle = run_probe(SCRIPT_SPAWNS_PLAIN, *self.OPTS)
        self.assertIn('[effects] the script\'s output and requests came from programs 2 (2)', err)
        self.assertIn('[under] the VM call chains of the script\'s output and requests: under program 3 (2)\n', err)
        from devm import pipeline
        interps, callers, notes = probe_evidence(dump, err)
        self.assertEqual(interps, [0, 0, 1])
        self.assertEqual(pipeline.script_votes(interps, [3], notes, callers), ({1: 2}, 'under'))
        self.assertEqual(pipeline.script_programs(interps, [3], notes, callers), ([3], None))

    def test_a_coroutine_of_the_script(self):
        """... while a thread the script's coroutine library runs has no known start: its line is not placed
        (not 'under none', which would tie with the program's own line and give the first program); the
        payload is kept."""
        took, err, dump, oracle = run_probe(SCRIPT_SPAWNS_PLAIN.replace('task.spawn(beat)', 'coroutine.wrap(beat)()'),
                                            *self.OPTS)
        self.assertIn('[under] the VM call chains of the script\'s output and requests: under program 3 (1), not placed: on '
                      'a thread whose call chain starts there (1)', err)
        from devm import pipeline
        interps, callers, notes = probe_evidence(dump, err)
        self.assertEqual(pipeline.under_programs(notes), {3: 1})
        self.assertEqual(pipeline.script_votes(interps, [3], notes, callers), ({1: 1}, 'under'))
        self.assertEqual(pipeline.script_programs(interps, [3], notes, callers), ([3], None))

    def test_a_thread_running_a_function_of_the_script_program(self):
        """A thread whose function is a closure of the script's program: its stack shows that program."""
        took, err, dump, oracle = run_probe(SCRIPT_SPAWNS_ITS_OWN, *self.OPTS)
        self.assertIn('[under] the VM call chains of the script\'s output and requests: under program 3 (2)\n', err)

    def test_a_thread_the_start_up_code_spawned(self):
        """The start-up code prints on the VM's thread and in a thread it started with task.spawn: both under
        no program of another interpreter, and the helper is set aside."""
        took, err, dump, oracle = run_probe(STARTUP_SPAWNS, *self.OPTS)
        self.assertIn('[under] the VM call chains of the script\'s output and requests: under no program of another '
                      'interpreter (2)\n', err)
        from devm import pipeline
        interps, callers, notes = probe_evidence(dump, err)
        self.assertEqual(pipeline.script_votes(interps, [2], notes, callers), ({0: 2}, 'under'))
        self.assertEqual(pipeline.script_programs(interps, [2], notes, callers), ([], [2]))
        self.assertEqual(pipeline.not_placed(notes), 0)

    def test_a_coroutine_of_the_start_up_code(self):
        """... and in a coroutine (no known start): one line under none, one not placed."""
        took, err, dump, oracle = run_probe(STARTUP_SPAWNS.replace('task.spawn(function()', 'coroutine.wrap(function()')
                                            .replace('print("two") end)', 'print("two") end)()'), *self.OPTS)
        self.assertIn('[under] the VM call chains of the script\'s output and requests: under no program of another '
                      'interpreter (1), not placed: on a thread whose call chain starts there (1)', err)
        from devm import pipeline
        interps, callers, notes = probe_evidence(dump, err)
        self.assertEqual(pipeline.script_votes(interps, [2], notes, callers), ({0: 1}, 'under'))
        self.assertEqual(pipeline.script_programs(interps, [2], notes, callers), ([], [2]))
        self.assertEqual(pipeline.not_placed(notes), 1)
        self.assertEqual(pipeline.helpers_reason(interps, [2], notes, callers),
                         'the script\'s output and requests came from programs of the first interpreter (1 line(s); 1 more '
                         'on threads whose start is not known, not placed), not from')

    def test_no_line_placed(self):
        """An [under] line that placed none of the lines: the call chains were known and do not say, so the
        first callers are not used (program 2 was first entered from program 3, which would take program 3);
        like the harness's start rule, the first interpreter is kept."""
        from devm import pipeline
        interps = [1, 1, 2]
        notes = ['[effects] the script\'s output and requests came from programs 2 (2)',
                 '[under] the VM call chains of the script\'s output and requests: not placed: on a thread whose call '
                 'chain starts there (2)']
        self.assertEqual(pipeline.under_programs(notes), {})
        self.assertEqual(pipeline.script_votes(interps, [3], notes, {2: 3, 3: 1}), ({1: 2}, 'unplaced'))
        self.assertEqual(pipeline.script_programs(interps, [3], notes, {2: 3, 3: 1}), ([], [3]))
        self.assertEqual(pipeline.helpers_reason(interps, [3], notes, {2: 3, 3: 1}),
                         'the script\'s 2 line(s) of output and requests ran on threads whose start is not known (their call '
                         'chains start there and show no program of another interpreter): without evidence for another '
                         'interpreter the first one is kept, not')

    def test_spawned_busy_loop_of_the_script(self):
        """The script's program (second interpreter) starts a silent busy loop with task.spawn in a plain function
        that runs a routine of the first interpreter: the loop is under the script's program (where the code that
        spawned it ran), the run is stopped like one that started, and the payload is lifted."""
        took, err, dump, oracle = run_probe(SPAWNED_LOOP % '', *self.OPTS)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertIn('stopped 1 s after the run made program 3 on another interpreter, in a loop of its VM code', line)
        self.assertIn('[where] the run was stopped in program 2, under program 3 of another interpreter', err)
        self.assertIsNotNone(dump)
        from devm import pipeline
        interps, callers, notes = probe_evidence(dump, err)
        self.assertEqual(pipeline.script_programs(interps, [3], notes, callers), ([3], None))

    def test_tamper_response_on_a_spawned_thread_of_the_script(self):
        """... and so is Luraph's tamper response entered on such a thread before any output: a stop of the
        script's run, with a dump, not a failure of the start-up code."""
        took, err, dump, oracle = run_probe(SPAWNED_TAMPER, *self.OPTS)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertIn('after the run made program 3 on another interpreter, at the entry of Luraph\'s tamper response', line)
        self.assertIn('[where] the run was stopped in program 4, under program 3 of another interpreter', err)
        self.assertIsNotNone(dump)

    def test_spin_on_a_thread_whose_start_is_not_known(self):
        """The same loop started with the script's coroutine library: where the thread was started is not known.
        The run ends without a dump (a lift would be the first program, start-up code and all), and the
        [stalled] line says that whether the spin is the start-up code's or the script's is not known."""
        took, err, dump, oracle = run_probe((SPAWNED_LOOP % '').replace('task.spawn(loop)', 'coroutine.wrap(loop)()'),
                                            *self.OPTS)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertTrue(line.startswith('[stalled] the run did not end and showed nothing of the script (no output, no '
                                        'request; it was stopped on a thread whose start is not known'), line)
        self.assertIn('so whether it ran under a program it made on another interpreter (3) is not known', line)
        self.assertIn('which of the two is not known, and nothing is lifted', line)
        self.assertNotIn('It spins in Luraph\'s start-up code', line)
        self.assertIn('[where] the run was stopped in program 2, not placed: on a thread whose call chain starts there', err)
        self.assertIsNone(dump)

    def test_busy_loop_the_script_deferred(self):
        """(The verifier's probe T1.)  The script's program starts the busy loop with task.defer (it runs at the
        main thread's next wait): placed under the script's program, a stop of its run with a dump, the payload
        kept."""
        took, err, dump, oracle = run_probe(THREADS_HEAD + THREADS_SCRIPT % 'task.defer(busy)\n    task.wait()',
                                            *self.OPTS)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertIn('after the run made program 3 on another interpreter, in a loop of its VM code', line)
        self.assertIn('[where] the run was stopped in program 2, under program 3 of another interpreter', err)
        self.assertIsNotNone(dump)
        from devm import pipeline
        interps, callers, notes = probe_evidence(dump, err)
        self.assertEqual(pipeline.script_programs(interps, [3], notes, callers), ([3], None))

    def test_busy_loop_the_script_runs_in_a_coroutine(self):
        """(T2.)  ... started with the script's coroutine library instead: the thread's start is not known; not
        placed, the [stalled] line says which of the two is not known, nothing is lifted."""
        took, err, dump, oracle = run_probe(THREADS_HEAD + THREADS_SCRIPT % 'coroutine.wrap(busy)()', *self.OPTS)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertIn('it was stopped on a thread whose start is not known', line)
        self.assertIn('which of the two is not known, and nothing is lifted', line)
        self.assertIn('[where] the run was stopped in program 2, not placed: on a thread whose call chain starts there', err)
        self.assertIsNone(dump)

    def test_busy_loop_the_start_up_code_deferred(self):
        """(T3.)  The start-up code defers the busy loop before it makes the script's program, which then waits:
        the loop is under no program of another interpreter (where the code that deferred it ran): a spin of the
        start-up code, nothing lifted."""
        took, err, dump, oracle = run_probe(THREADS_HEAD + 'task.defer(busy)\n' + THREADS_SCRIPT % 'task.wait()',
                                            *self.OPTS)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertIn('[where] the run was stopped in program 2, under no program of another interpreter', err)
        self.assertIn('It spins in Luraph\'s start-up code', line)
        self.assertIsNone(dump)

    def test_wait_loop_the_start_up_code_spawned(self):
        """The start-up code makes helper 2 (second interpreter), which enters routine 3 (first interpreter) first;
        then the start-up code spawns a wait loop in routine 3 and the main thread waits, with no output: the loop
        is under no program of another interpreter (where the code that spawned it ran), and the helper is set
        aside, as the [stalled] line says."""
        took, err, dump, oracle = run_probe(STARTUP_WAIT_LOOP % 'task.spawn(waiter)', *self.OPTS)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertIn('the waits are the script\'s own code, on the first interpreter', line)
        self.assertIn('[where] the run was stopped in program 3, under no program of another interpreter', err)
        from devm import pipeline
        interps, callers, notes = probe_evidence(dump, err)
        self.assertEqual(callers[3], 2)
        self.assertEqual(pipeline.script_programs(interps, [2], notes, callers), ([], [2]))

    def test_wait_loop_on_a_thread_whose_start_is_not_known(self):
        """The same wait loop run by the script's coroutine library: the stop is not placed.  The [where] line says
        so, and the program choice does what the harness's start rule does (the first interpreter), not follow
        routine 3's first caller to the helper; the [stalled] line and the choice agree."""
        took, err, dump, oracle = run_probe(STARTUP_WAIT_LOOP % 'coroutine.wrap(waiter)()', *self.OPTS)
        line = stalled_line(err)
        self.assertIsNotNone(line, err[-1500:])
        self.assertIn('[where] the run was stopped in program 3, not placed: on a thread whose call chain starts there', err)
        self.assertIn('so whether it ran under a program it made on another interpreter (2) is not known', line)
        self.assertIn('with nothing that places them under a program of another interpreter, the first interpreter is kept',
                      line)
        self.assertIsNotNone(dump)
        from devm import pipeline
        interps, callers, notes = probe_evidence(dump, err)
        self.assertEqual(callers[3], 2)
        self.assertEqual(pipeline.stopped_under(notes), pipeline.UNPLACED)
        self.assertEqual(pipeline.script_votes(interps, [2], notes, callers), ({0: 1}, 'unplaced'))
        self.assertEqual(pipeline.script_programs(interps, [2], notes, callers), ([], [2]))
        self.assertEqual(pipeline.helpers_reason(interps, [2], notes, callers),
                         'the run was stopped in program 3, a program of the first interpreter, on a thread whose start is not '
                         'known (its call chain starts there and shows no program of another interpreter): without evidence '
                         'for another interpreter the first one is kept, not')


class Report(unittest.TestCase):
    STALL = ('[stalled] the script did not end: it was stopped 91 s after its first line of output, '
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

    ENTRY = ('[entry] program 2 is taken for the script: the run did not end, and the first program called it at step 301 '
             'and ran no new instruction after that; the script\'s output came from program(s) 2, which run under it.  The '
             'first program\'s code before that call is not in the result: Luraph\'s start-up code, and any part of the '
             'script\'s top level that Luraph put in that program (--root 1 lifts the first program)')

    def test_entry_is_partial(self):
        """A result lifted from the program the start-up code entered lacks the first program's code before the
        call: never `complete`, and the report says what is missing and how to get it."""
        notes = ['[oracle] script returned after 1200 instructions', self.ENTRY]
        lines = run.report('local x = 1\nprint(x)\n', notes=notes, parse=(True, None, ''))
        self.assertEqual(lines[0], 'result: PARTIAL')
        self.assertIn('  the result is program 2 on its own, which the VM\'s first program called at the end of its run: the '
                      'first program\'s code before that call is not in it (Luraph\'s start-up code, and any part of the '
                      'script\'s top level Luraph put there); --root 1 lifts the first program', lines)
        # ... also read back off the head of a file devm wrote
        head = run.run_report(notes)
        self.assertIn(run.ENTRY_LINE + self.ENTRY[len('[entry] '):], head)
        text = '-- x.lua, %s.  How the run went and what this file lacks:\n%s\nlocal x = 1\nprint(x)\n' % (
            run.HEAD_MARK, '\n'.join('--   ' + ln for ln in head + lines))
        again = run.report(text)
        self.assertEqual(again[0], 'result: PARTIAL')
        self.assertTrue(any(ln.startswith('  the result is program 2 on its own') for ln in again), again)

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
        self.assertTrue(last.startswith('result: PARTIAL: the script did not end: it was stopped 91 s after its first line of output'), last)
        self.assertIn('code it would load or fetch later is missing', last)
        self.assertTrue(last.endswith('(devm)'), last)

    def test_entry_result_says_so(self):
        self.fake_devm([Report.STALL, run.ENTRY_LINE + Report.ENTRY[len('[entry] '):], 'result: PARTIAL'])
        res = allinone.run_devm('in.lua', os.path.join(self.tmp.name, 'o.lua'), work=os.path.join(self.tmp.name, 'w'))
        self.assertFalse(res['complete'])
        self.assertIn('the result is program 2, without the code of the VM\'s first program before it called it (--root 1 '
                      'lifts that program)', res['why'])

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
