"""Checks of devm's run values (devm/runvals.py and the oracle of devm/harness.lua): where the
traced run proves what a register held and the lifted code would show something else, the text
shows the value the run saw.

    python3 tests/devm_runvals.py        (from the repository root; seconds; the harness checks need bin/luau)

Hand-written programs only: the small VM of tests/devm_lift_v14.py, programs written for these
tests, and harness probes that call the hooks of an instrumented VM the way the program's run
would (no text of a protected file is in here).

* The harness records, for every instruction that ran, the strings and booleans each register held
  at every visit (the oracle's `s`, next to the numbers of `r`); a register whose value differs
  between visits is dropped, a string longer than the limit is left out, and code only the
  exercise pass ran has no strings or booleans on record.
* A program hands its register array to a helper (`f(x, R, n)`, printed `f(x, ({}), n)`) that
  decodes a constant into one of the program's registers while the script runs (the probe does the
  decoding, as the run would).  The lifted text used to show the encrypted constant wherever the
  program read that register; now the decoded value the run saw is put right after the call.  The
  helper is labelled as set aside as a helper of the build when the pipeline names it one (the label
  gives the evidence: another interpreter, its first caller), and the report has a
  note line.  The same for a helper whose result the program keeps in a register
  (`R[a] = helper(x)`: the call stays, the value is the run's).
* Nothing is put where the value differed between executions, where the call did not run, or where
  the call went on to more than one instruction; the run notes then say what is not shown and the
  result is PARTIAL.  A function that is not a helper keeps its call result.
* A value is shown only where the code shows that every execution of the call writes the same
  (ProofOfTheCall): not when the helper reads a global, reads its caller's registers through the
  array, captures a value read at run time, or is a library function; a helper whose code that ran
  did not all lift gets the run's value with a comment that it is assumed, and the result is
  PARTIAL.  The run's record of what a call changed counts only where the instruction's one call is
  the one handed the array and calls a closure of the program (WhichRecordCovers, with the
  arguments the run cannot vouch for and two such calls in one instruction).  A register a function
  shares, or that the instruction reads again after the call, is marked unless that function's text
  or a plain store shows what it holds (SharedOrReadAgain).
* The proof follows the code: a register a function shares and assigns is no constant; a call
  handed the register array that fails a rule (or the array kept where other code reaches it) leaves
  nothing in that function proved; a shared register the run saw change is marked also where no
  record covers the call; a field the library does not have is not the library's; `tostring`,
  calling code that did not all lift, another call only assumed, and a variable shared with a
  function that did not all lift make a value assumed, said in the comment at it (WhatTheCodeShows).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))
sys.setrecursionlimit(100000)

from devm import run, runvals, structure, vmmodel  # noqa: E402
from devm_lift_v14 import Mini, PACKER, proto  # noqa: E402

HARNESS = os.path.join(ROOT, 'devm', 'harness.lua')


def have_luau():
    try:
        return bool(vmmodel.find_luau())
    except RuntimeError:
        return False


# ---------------------------------------------------------------- the program and its helper
# 20: R[A](R[B], R, C)  -- a call handed the register array and a register number
# 21: R[A] = R[B] .. R[C]
# 22: R[A][R[B]] = R[C]
# 23: R[A] = R[B]:reverse()
# 24: R[A] = R[A](R[B])
# 25: R[A] = R          -- the register array kept in a register
# 26: R[A](R[B], R[C], B)  -- ... and handed to a call from there
HANDLERS = """elseif op == 20 then
R[A[pc]](R[B[pc]], R, C[pc])
elseif op == 21 then
R[A[pc]] = R[B[pc]] .. R[C[pc]]
elseif op == 22 then
R[A[pc]][R[B[pc]]] = R[C[pc]]
elseif op == 23 then
R[A[pc]] = R[B[pc]]:reverse()
elseif op == 24 then
R[A[pc]] = R[A[pc]](R[B[pc]])
elseif op == 25 then
R[A[pc]] = R
elseif op == 26 then
R[A[pc]](R[B[pc]], R[C[pc]], B[pc])
"""
ENC, PLAIN = 'olleh', 'hello'       # what the program holds, and what the helper makes of it


def helper_into_regs():
    """function(s, R, n) R[n] = s:reverse() end"""
    return proto([(23, 4, 1, 0), (22, 2, 3, 4), (2, 1, 0, 0)], nparams=3)


def helper_returns():
    """function(s) return s:reverse() end"""
    return proto([(23, 2, 1, 0), (2, 2, 0, 0)], nparams=1)


# R1 = helper; R2 = ENC; R1(R2, R, 2); R3 = print; R5 = " world"; R4 = R2 .. R5; R3 = R3(R4); return R3
MAIN_REGS = [(8, 1, None, 0), (1, 2, ENC, 0), (20, 1, 2, 2), (3, 3, 'print', 0), (1, 5, ' world', 0),
             (21, 4, 2, 5), (5, 3, 0, 0), (2, 3, 0, 0)]
# R1 = helper; R2 = ENC; R1 = R1(R2); R3 = print; R5 = " world"; R4 = R1 .. R5; R3 = R3(R4); return R3
MAIN_RESULT = [(8, 1, None, 0), (1, 2, ENC, 0), (24, 1, 2, 0), (3, 3, 'print', 0), (1, 5, ' world', 0),
               (21, 4, 1, 5), (5, 3, 0, 0), (2, 3, 0, 0)]

# The same as MAIN_REGS, with the register array handed over from a register (R6) that was set before
# a join of two paths (3: `if R7 then` jumps over 4): the code alone does not say what R6 holds at
# the call, the run does.
MAIN_JOIN = [(8, 1, None, 0), (25, 6, 0, 0), (7, 7, 4, 0), (1, 8, 0, 0), (1, 2, ENC, 0), (26, 1, 2, 6),
             (3, 3, 'print', 0), (1, 5, ' world', 0), (21, 4, 2, 5), (5, 3, 0, 0), (2, 3, 0, 0)]


def lift(main, helper):
    m = Mini(HANDLERS, prologue='for i = 1, NP do R[i] = args[i] end', outer=PACKER)
    code = [(op, a, helper if op == 8 else b, c) for op, a, b, c in main]
    m.lift(proto(code))
    m.root = m.lifter.root_key
    m.helper = next(k for k in m.protos if k != m.root)
    return m


def text_of(m, labels=None):
    d = structure.Decompiler(m.protos, m.root, None)
    d.labels = labels or {}
    return d.run()


# ---------------------------------------------------------------- the run, as the harness records it
# A probe steps through the program's instructions with one register array, doing what each
# instruction and the helper do to it (the decoding happens here, at run time).  `order` is the
# list of instructions run; `decode(s, k)` is the helper's work at its k-th call.
PROBE = '''
local regs = {}
local pid = __LPH_PROTO(0, {"K"}, 1, {1})
local calls = 0
local function decode(s)
	calls += 1
	%(decode)s
end
local effect = {
	[1] = function() regs[1] = function() end end,
	[2] = function() regs[2] = "%(enc)s" end,
	[3] = function() %(call)s end,
	[4] = function() regs[3] = print end,
	[5] = function() regs[5] = " world" end,
	[6] = function() regs[4] = regs[%(src)d] .. regs[5] end,
	[7] = function() regs[3] = nil end,
	[8] = function() end,
}
for _, pc in {%(order)s} do
	__LPH_STEP(pid, 0, pc, regs)
	effect[pc]()
end
'''
PROBE_JOIN = '''
local regs = {}
local pid = __LPH_PROTO(0, {"K"}, 1, {1})
local function decode(s) return string.reverse(s) end
local effect = {
	[1] = function() regs[1] = function() end end,
	[2] = function() regs[6] = %(holder)s end,
	[3] = function() end,
	[4] = function() regs[8] = 0 end,
	[5] = function() regs[2] = "%(enc)s" end,
	[6] = function() regs[2] = decode(regs[2]) end,
	[7] = function() regs[3] = print end,
	[8] = function() regs[5] = " world" end,
	[9] = function() regs[4] = regs[2] .. regs[5] end,
	[10] = function() regs[3] = nil end,
	[11] = function() end,
}
for pc = 1, 11 do
	__LPH_STEP(pid, 0, pc, regs)
	effect[pc]()
end
'''
CALL_REGS = 'regs[2] = decode(regs[2])'       # the helper writes into the caller's register 2
CALL_RESULT = 'regs[1] = decode(regs[2])'     # the call's result goes to register 1
SAME = 'return string.reverse(s)'
DIFFERENT = 'return string.reverse(s) .. calls'


def probe_oracle(order, call=CALL_REGS, decode=SAME, src=2, *options, text=None):
    """the oracle of a probe run, {(pid, loop, pc): record}, and the harness's stderr"""
    if text is None:
        text = PROBE % {'decode': decode, 'enc': ENC, 'call': call, 'src': src, 'order': ', '.join(str(x) for x in order)}
    with tempfile.TemporaryDirectory() as d:
        src_path = os.path.join(d, 'sample.lua')
        with open(src_path, 'w') as f:
            f.write(text)
        out = os.path.join(d, 'end.json')
        r = subprocess.run([vmmodel.find_luau(), HARNESS, '-a', src_path, out, 'oracle=1', 'dumpend=1'] + list(options),
                           capture_output=True, text=True, errors='replace', timeout=120)
        if not os.path.exists(out + '.oracle'):
            raise AssertionError('no oracle: ' + r.stderr[-1500:])
        return vmmodel.read_oracle(out + '.oracle'), r.stderr


def rekey(oracle, pid):
    """the probe's program 1 is the lifted program `pid`"""
    return {(pid, loop, pc): rec for (_p, loop, pc), rec in oracle.items()}


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class Recording(unittest.TestCase):
    """harness.lua: the strings and booleans of the oracle"""

    def run_probe(self, body, *options):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'sample.lua')
            with open(src, 'w') as f:
                f.write(body)
            out = os.path.join(d, 'end.json')
            r = subprocess.run([vmmodel.find_luau(), HARNESS, '-a', src, out, 'oracle=1', 'dumpend=1'] + list(options),
                               capture_output=True, text=True, errors='replace', timeout=120)
            self.assertTrue(os.path.exists(out + '.oracle'), r.stderr[-1500:])
            with open(out + '.oracle') as f:
                raw = json.load(f)
            return vmmodel.read_oracle(out + '.oracle'), raw

    def test_kept_dropped_and_left_out(self):
        oracle, raw = self.run_probe('''
local regs = {}
local pid = __LPH_PROTO(0, {"K"}, 1, {1})
for i = 1, 3 do
	regs[1] = "same"
	regs[2] = "round " .. i
	regs[3] = true
	regs[4] = false
	regs[5] = string.rep("x", 5000)
	regs[6] = "a\\0b\\255"
	regs[7] = i
	regs[9] = regs
	regs[10] = {}
	regs[11] = i == 2 and {} or regs
	__LPH_STEP(pid, 0, 1, regs)
end
regs[8] = "once"
__LPH_STEP(pid, 0, 2, regs)
''')
        rec = oracle[(1, 0, 1)]
        self.assertEqual(rec['n'], 3)
        self.assertEqual(rec['s'], {1: 'same', 3: True, 4: False, 6: 'a\x00b\xff'})
        self.assertNotIn(7, rec['r'])           # (a number that differs is dropped as before)
        # the registers that held the activation's own register array at every visit
        self.assertEqual(rec['a'], {9})
        self.assertEqual(oracle[(1, 0, 2)]['s'][8], 'once')
        self.assertEqual(oracle[(1, 0, 2)]['s'][2], 'round 3')
        # (strings are written as hex, as the dump writes them)
        self.assertEqual(raw['1:0:1']['s']['1'], 'same'.encode().hex())

    def test_nothing_for_code_only_the_exercise_pass_ran(self):
        oracle, _raw = self.run_probe('''
local regs = {"script"}
local boot = __LPH_PROTO(0, {"K"}, 1, {1})
local pid = __LPH_PROTO(1, {"K"}, 1, {2})
local bind = __LPH_REG(pid, function() return function() local r = {"exercised", true}; __LPH_STEP(pid, 0, 7, r) end end)
local closure = bind()
__LPH_STEP(pid, 0, 1, regs)
''', 'exercise=1')
        self.assertEqual(oracle[(2, 0, 1)]['s'], {1: 'script'})
        self.assertIn((2, 0, 7), oracle)        # (it ran in the exercise pass ...)
        self.assertEqual(oracle[(2, 0, 7)]['s'], {})     # (... and its strings are not the script's)


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class HelperIntoRegisters(unittest.TestCase):
    """a helper handed the register array decodes a constant into a register of its caller"""

    def setUp(self):
        self.m = lift(MAIN_REGS, helper_into_regs())

    def show(self, order, decode=SAME, helpers=None):
        m = self.m
        oracle, _err = probe_oracle(order, CALL_REGS, decode)
        helpers = {m.helper} if helpers is None else helpers
        sites = runvals.show_run_values(m.protos, rekey(oracle, m.root), helpers)
        notes = runvals.notes(sites, helpers)
        text = text_of(m, runvals.labels(sites, helpers, m.protos))
        return sites, notes, text

    def test_before(self):
        # what the lift shows without the run's values: the encrypted constant is what is printed
        text = text_of(self.m)
        self.assertRegex(text, r'local (\w+) = "%s"\n(?:.*\n)*.*print\(\1 \.\. " world"\)' % ENC)
        self.assertNotIn(PLAIN, text)

    def test_value_the_run_saw(self):
        sites, notes, text = self.show(range(1, 9))
        self.assertEqual([(s.kind, s.callee, s.shown, s.ran) for s in sites], [('regs', self.m.helper, {2: PLAIN}, True)])
        # the call stays, with the encrypted constant it is handed; what it wrote comes after it
        self.assertIn('"%s"' % ENC, text)
        self.assertIn('"%s"' % PLAIN, text)
        self.assertLess(text.index('"%s"' % ENC), text.index('"%s"' % PLAIN))
        self.assertRegex(text, r'print\((?:"%s"|\w+) \.\. " world"\)' % PLAIN)
        self.assertNotRegex(text, r'local (\w+) = "%s"\n(?:.*\n)*.*print\(\1 \.\. " world"\)' % ENC)
        # the helper is labelled as the build's, not the script's code
        self.assertIn('-- program %d: set aside as a helper of the build (on another interpreter than the program taken for '
                      'the script)' % self.m.helper, text)
        self.assertIn('It writes into the variables of the code that calls it', text)
        self.assertEqual(len(notes), 1, notes)
        self.assertTrue(notes[0].startswith('[runvals] 1 call(s) of program %d, set aside as a helper of the build' % self.m.helper),
                        notes)
        self.assertIn('the text shows the values they wrote as the run saw them (1 value(s))', notes[0])
        lines = run.run_report(notes)
        self.assertTrue(any(ln.startswith('  note: 1 call(s) of program %d, set aside as a helper of the build' % self.m.helper)
                            for ln in lines), lines)
        report = run.report(text, notes=notes)
        self.assertEqual(report[0][:15], 'result: complet', report)

    def test_printed_program_does_what_the_run_did(self):
        # the printed text, run with the helper's decoding, prints what the probe's run printed
        sites, notes, text = self.show(range(1, 9))
        self.assertIn('print(', text)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'case.lua')
            with open(path, 'w') as f:
                f.write(text)
            r = subprocess.run([vmmodel.find_luau(), path], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), PLAIN + ' world', r.stderr)

    def test_values_that_differ_between_runs_of_the_call(self):
        # the call runs twice and decodes to something else each time: no value is put in
        sites, notes, text = self.show(list(range(1, 9)) + [2, 3, 4, 5, 6, 7, 8], decode=DIFFERENT)
        self.assertEqual(sites[0].shown, {})
        self.assertEqual(sites[0].unproved, {2})
        self.assertNotIn(PLAIN, text)
        # (what the call wrote is not shown as the value from before it, the encrypted constant)
        self.assertIn('print(nil --[[%s]] .. " world")' % runvals.MARK, text)
        self.assertNotRegex(text, r'print\((?:"%s"|\w+) \.\. " world"\)' % ENC)
        self.assertEqual(len(notes), 1, notes)
        self.assertTrue(notes[0].startswith('[runvals-missing] 1 call(s) of program %d, set aside as a helper of the build'
                                            % self.m.helper))
        self.assertIn('a constant handed to the call stays as it was (encrypted)', notes[0])
        lines = run.run_report(notes)
        self.assertTrue(any(ln.startswith(run.RUNVALS_LINE) for ln in lines), lines)
        report = run.report(text, notes=notes)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertTrue(any('values not shown' in ln for ln in report), report)
        # ... also read back off the head of a written file
        head = ['-- case.lua, %s.  How the run went and what this file lacks:' % run.HEAD_MARK]
        head += ['--   ' + ln for ln in lines + report]
        again = run.report('\n'.join(head) + '\n' + text)
        self.assertEqual(again[0], 'result: PARTIAL', again)

    def test_the_same_value_at_every_run_of_the_call(self):
        # the call runs twice and decodes to the same value both times: it is shown
        sites, notes, text = self.show(list(range(1, 9)) + [2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(sites[0].shown, {2: PLAIN})
        self.assertIn('"%s"' % PLAIN, text)

    def test_call_that_did_not_run(self):
        sites, notes, text = self.show([1, 2])
        self.assertEqual(sites[0].shown, {})
        self.assertFalse(sites[0].ran)
        self.assertNotIn(PLAIN, text)
        self.assertTrue(notes and notes[0].startswith('[runvals-missing] '), notes)
        # the register the call names is printed as not shown after it, not as the constant from before
        self.assertIn('print(%s .. " world")' % MARKED, text)
        self.assertEqual(sites[0].marked, {2})

    def test_call_followed_by_two_instructions(self):
        # after one run of the call the next instruction was another one (the probe jumps): the run
        # does not say what the call left for the instruction the text follows it with
        sites, notes, text = self.show(list(range(1, 9)) + [2, 3, 5, 6, 7, 8])
        self.assertFalse(sites[0].ran)
        self.assertNotIn(PLAIN, text)

    def test_not_a_helper_of_the_build(self):
        # a function the pipeline does not name a helper: the value is shown all the same (the call
        # wrote it), but no label calls the function the build's
        sites, notes, text = self.show(range(1, 9), helpers=set())
        self.assertIn('"%s"' % PLAIN, text)
        self.assertNotIn('a helper of the build', text)
        self.assertTrue(notes[0].startswith('[runvals] 1 call(s) of program %d are handed' % self.m.helper), notes)


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class RegisterArrayFromARegister(unittest.TestCase):
    """the register array reaches the call through a register set before a join: the run's record of
    which registers held the activation's own register array (the oracle's `a`) says it is handed"""

    def show(self, holder):
        m = lift(MAIN_JOIN, helper_into_regs())
        oracle, _err = probe_oracle(None, text=PROBE_JOIN % {'holder': holder, 'enc': ENC})
        sites = runvals.show_run_values(m.protos, rekey(oracle, m.root), {m.helper})
        return m, sites, text_of(m, runvals.labels(sites, {m.helper}, m.protos))

    def test_handed_over_from_a_register(self):
        m, sites, text = self.show('regs')
        self.assertEqual([(s.kind, s.shown) for s in sites], [('regs', {2: PLAIN})])
        self.assertRegex(text, r'print\((?:"%s"|\w+) \.\. " world"\)' % PLAIN)

    def test_another_table_in_that_register(self):
        # R6 holds some other table at the call: not the register array, not a site
        m, sites, text = self.show('{}')
        self.assertEqual(sites, [])
        self.assertNotIn(PLAIN, text)


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class HelperResult(unittest.TestCase):
    """a helper of the build returns the decoded constant, which the program keeps in a register"""

    def setUp(self):
        self.m = lift(MAIN_RESULT, helper_returns())

    def show(self, order, decode=SAME, helpers=None):
        m = self.m
        oracle, _err = probe_oracle(order, CALL_RESULT, decode, 1)
        helpers = {m.helper} if helpers is None else helpers
        sites = runvals.show_run_values(m.protos, rekey(oracle, m.root), helpers)
        return sites, runvals.notes(sites, helpers), text_of(m, runvals.labels(sites, helpers, m.protos))

    def test_value_the_run_saw(self):
        sites, notes, text = self.show(range(1, 9))
        self.assertEqual([(s.kind, s.shown) for s in sites], [('result', {1: PLAIN})])
        self.assertIn('"%s"' % PLAIN, text)
        self.assertRegex(text, r'print\((?:"%s"|\w+) \.\. " world"\)' % PLAIN)
        # the call stays (as a statement), handed the encrypted constant
        self.assertRegex(text, r'(?m)^\s*(?:\w+|\(function\(\w+\)(?:.|\n)*?end\))\("%s"\)$' % ENC)
        self.assertIn('-- program %d: set aside as a helper of the build' % self.m.helper, text)
        self.assertTrue(notes[0].startswith('[runvals] 1 call(s) of program %d, set aside as a helper of the build' % self.m.helper),
                        notes)
        self.assertIn('return values that the code that calls them keeps in variables', notes[0])

    def test_values_that_differ(self):
        sites, notes, text = self.show(list(range(1, 9)) + [2, 3, 4, 5, 6, 7, 8], decode=DIFFERENT)
        self.assertEqual(sites[0].shown, {})
        self.assertNotIn(PLAIN, text)
        self.assertTrue(notes[0].startswith('[runvals-missing] '), notes)
        self.assertIn('the text keeps the call, and the constants handed to it stay as they were (encrypted)', notes[0])

    def test_function_of_the_script_keeps_its_result(self):
        # not a helper of the build: `x = f(...)` is the script's own code and stays as it is
        sites, notes, text = self.show(range(1, 9), helpers=set())
        self.assertEqual(sites, [])
        self.assertEqual(notes, [])
        self.assertNotIn(PLAIN, text)


# ---------------------------------------------------------------- loops, run-time inputs, calls the run saw
# A loop: R1 = helper; R2 = ENC; [R9 = "x"]; R1(R2, R, 2); R3 = print; R4 = R2 .. R9; R3 = R3(R4); R6 = more;
# if R6 goto 2; return R3.  The helper decodes R2 (the same value every round) and also writes R9 (another
# value every round).
LOOP_TWO = [(8, 1, None, 0), (1, 2, ENC, 0), (1, 9, 'x', 0), (20, 1, 2, 2), (3, 3, 'print', 0), (21, 4, 2, 9),
            (5, 3, 0, 0), (3, 6, 'more', 0), (7, 6, 1, 0), (2, 3, 0, 0)]
# the same without `R9 = "x"`: R9 holds no one value before the call either
LOOP_TWO_NO_RESET = [(8, 1, None, 0), (1, 2, ENC, 0), (20, 1, 2, 2), (3, 3, 'print', 0), (21, 4, 2, 9),
                     (5, 3, 0, 0), (3, 6, 'more', 0), (7, 6, 1, 0), (2, 3, 0, 0)]
# a loop whose call decodes into R9, which nothing resets before the call:
# R1 = helper; R2 = ENC; R1(R2, R, 9); R3 = print; R5 = " world"; R4 = R9 .. R5; R3 = R3(R4); R6 = more; if R6 goto 2
LOOP_NINE = [(8, 1, None, 0), (1, 2, ENC, 0), (20, 1, 2, 9), (3, 3, 'print', 0), (1, 5, ' world', 0),
             (21, 4, 9, 5), (5, 3, 0, 0), (3, 6, 'more', 0), (7, 6, 1, 0), (2, 3, 0, 0)]
# the helper is handed a value read at run time (a global), not a constant: R2 = input; R1(R2, R, 2); ...
FROM_INPUT = [(8, 1, None, 0), (3, 2, 'input', 0), (20, 1, 2, 2), (3, 3, 'print', 0), (1, 5, ' world', 0),
              (21, 4, 2, 5), (5, 3, 0, 0), (2, 3, 0, 0)]
# ... and the same for a helper whose result is kept: R2 = input; R1 = R1(R2); ...
RESULT_FROM_INPUT = [(8, 1, None, 0), (3, 2, 'input', 0), (24, 1, 2, 0), (3, 3, 'print', 0), (1, 5, ' world', 0),
                     (21, 4, 1, 5), (5, 3, 0, 0), (2, 3, 0, 0)]

# A probe of such a program: `steps` are the instructions run, `effects` what each does to the registers
# (Lua statements).  With `vm` the helper's work is done by a function that runs as a VM function of its
# own (an activation with its own register array holding the caller's, as an interpreter's closure does):
# the harness then sees the call and records which of the caller's registers it changed (`h` / `w`).
PROBE_LOOP = '''
local regs = {}
local pid = __LPH_PROTO(0, {"K"}, 1, {1})
local hp = __LPH_PROTO(0, {"K"}, 1, {2})
local round = 0
local function work(s, R, n)
	%(work)s
end
local function helper(s, R, n)
	if %(vm)s then
		local hr = { s, R, n }
		__LPH_STEP(hp, 0, 1, hr)
	end
	work(s, R, n)
end
local effect = { %(effects)s }
for _, pc in {%(steps)s} do
	__LPH_STEP(pid, 0, pc, regs)
	if effect[pc] then effect[pc]() end
end
'''


def loop_oracle(effects, steps, work, vm):
    text = PROBE_LOOP % {'effects': ', '.join('[%d] = function() %s end' % kv for kv in sorted(effects.items())),
                         'steps': ', '.join(str(x) for x in steps), 'work': work, 'vm': 'true' if vm else 'false'}
    oracle, _err = probe_oracle(None, text=text)
    return oracle


def own(oracle, pid):
    """the probe's program 1 is the lifted program `pid` (the helper's own program, 2, is left out)"""
    return {(pid, loop, pc): rec for (p, loop, pc), rec in oracle.items() if p == 1}


def show(main, helper, oracle, helpers):
    m = lift(main, helper)
    helpers = {m.helper} if helpers else set()
    sites = runvals.show_run_values(m.protos, own(oracle, m.root), helpers)
    notes = runvals.notes(sites, helpers)
    text = text_of(m, runvals.labels(sites, helpers, m.protos))
    return m, sites, notes, text, run.report(text, notes=notes)


MARKED = 'nil --[[%s]]' % runvals.MARK
ROUNDS = [1] + [2, 3, 4, 5, 6, 7, 8, 9] * 3 + [10]
ROUNDS_NO_RESET = [1] + [2, 3, 4, 5, 6, 7, 8] * 3 + [9]


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class EveryRegisterProved(unittest.TestCase):
    """A call can write more than one register of its caller: one the run does not prove is enough for
    the call to count as not shown (PARTIAL), and that register is printed as not shown, not as the value
    it had before the call."""
    EFFECTS = {1: 'regs[1] = helper', 2: 'regs[2] = "%s"' % ENC, 3: 'regs[9] = "x"',
               4: 'regs[1](regs[2], regs, 2)', 5: 'regs[3] = print', 6: 'regs[4] = regs[2] .. regs[9]',
               7: 'regs[3] = nil', 8: 'regs[6] = round < 3'}
    WORK = 'round += 1; R[n] = string.reverse(s); R[9] = "r" .. round'

    def test_one_proved_one_not(self):
        # the run saw the helper (a VM function) change R2 and R9: R2 holds the same value after every
        # call and is shown, R9 a different one each round
        oracle = loop_oracle(self.EFFECTS, ROUNDS, self.WORK, vm=True)
        m, sites, notes, text, report = show(LOOP_TWO, helper_into_regs(), oracle, True)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertNotIn('"x")', text)
        self.assertEqual([n.split(' ')[0] for n in notes], ['[runvals]', '[runvals-missing]'], notes)
        self.assertIn('print("%s" .. %s)' % (PLAIN, MARKED), text)
        self.assertTrue(any('values not shown' in ln and '(1 in the text)' in ln for ln in report), report)
        rec = own(oracle, 4)[(4, 0, 4)]
        self.assertEqual((rec['h'], rec['n'], rec['w']), (3, 3, {2, 9}))
        self.assertEqual([(s.shown, s.unproved, s.ran, s.fixed) for s in sites], [({2: PLAIN}, {9}, True, True)])

    def test_without_the_record_of_the_call(self):
        # the helper's work done without a VM function (the verifier's probe): the run does not say what the
        # call changed, and in the loop the code does not say which function R1 holds at the call (the call
        # itself could change it): nothing is shown, both registers are marked
        oracle = loop_oracle(self.EFFECTS, ROUNDS, self.WORK, vm=False)
        m, sites, notes, text, report = show(LOOP_TWO, helper_into_regs(), oracle, False)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertNotIn('"x"', text.split('print(')[-1])
        self.assertIn('print(%s .. %s)' % (MARKED, MARKED), text)
        self.assertFalse(own(oracle, 4)[(4, 0, 4)].get('h'))
        self.assertEqual([(s.shown, s.unproved, s.fixed) for s in sites], [({}, {2, 9}, False)])

    def test_a_register_with_no_value_before_or_after(self):
        # R9 is not reset before the call: no one value before or after it.  Only the run's record of the
        # call (the helper a VM function) says the call changed it
        effects = dict(self.EFFECTS)
        del effects[3]
        effects = {k if k < 3 else k - 1: v for k, v in effects.items()}
        oracle = loop_oracle(effects, ROUNDS_NO_RESET, self.WORK, vm=True)
        m, sites, notes, text, report = show(LOOP_TWO_NO_RESET, helper_into_regs(), oracle, True)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertIn('print("%s" .. %s)' % (PLAIN, MARKED), text)
        self.assertEqual([(s.shown, s.unproved) for s in sites], [({2: PLAIN}, {9})])


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class RegisterTheCallNames(unittest.TestCase):
    """A call that ran, handed the register array and a register number, where the run shows no one value
    for that register after the call (nor before it): the call is reported (PARTIAL), and the register is
    printed as not shown, not as what the code had there before (here nothing: `nil .. " world"`)."""
    EFFECTS = {1: 'regs[1] = helper', 2: 'regs[2] = "%s"' % ENC, 3: 'regs[1](regs[2], regs, 9)',
               4: 'regs[3] = print', 5: 'regs[5] = " world"', 6: 'regs[4] = regs[9] .. regs[5]', 7: 'regs[3] = nil',
               8: 'regs[6] = round < 3'}
    WORK = 'round += 1; R[n] = string.reverse(s) .. round'

    def check(self, vm):
        oracle = loop_oracle(self.EFFECTS, ROUNDS_NO_RESET, self.WORK, vm=vm)
        m, sites, notes, text, report = show(LOOP_NINE, helper_into_regs(), oracle, vm)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertNotIn('print(nil .. " world")', text)
        self.assertIn('print(%s .. " world")' % MARKED, text)
        self.assertTrue(notes and notes[-1].startswith('[runvals-missing] 1 call(s)'), notes)
        self.assertEqual([(s.shown, s.unproved, s.ran) for s in sites], [({}, {9}, True)])

    def test_named_register(self):
        self.check(False)

    def test_the_run_saw_the_call_change_it(self):
        self.check(True)

    def test_a_register_nothing_reads_after_the_call(self):
        # the helper also stores a table in R5, which the next instructions set again before reading it:
        # nothing in the text can show a stale value of it, and nothing is reported for it
        effects = dict(HelperIntoRegisters_EFFECTS)
        oracle = loop_oracle(effects, range(1, 9), 'R[n] = string.reverse(s); R[5] = {}', vm=True)
        m, sites, notes, text, report = show(MAIN_REGS, helper_into_regs(), oracle, True)
        self.assertEqual(report[0][:15], 'result: complet', report)
        self.assertEqual([(s.shown, s.unproved) for s in sites], [({2: PLAIN}, set())])
        self.assertEqual(own(oracle, 4)[(4, 0, 3)]['w'], {2, 5})


# MAIN_REGS run once, as the probes of HelperIntoRegisters do it
HelperIntoRegisters_EFFECTS = {1: 'regs[1] = helper', 2: 'regs[2] = "%s"' % ENC, 3: 'regs[1](regs[2], regs, 2)',
                               4: 'regs[3] = print', 5: 'regs[5] = " world"', 6: 'regs[4] = regs[2] .. regs[5]',
                               7: 'regs[3] = nil'}


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class InputsAtRunTime(unittest.TestCase):
    """A helper handed a value the script read at run time (a global): the call ran once and the value
    after it was one value, but another execution can hand it something else.  Nothing is substituted:
    the value is printed as not shown (a call handed the register array) or the call is kept (a result),
    and the result is PARTIAL."""

    def test_register_array(self):
        effects = dict(HelperIntoRegisters_EFFECTS)
        effects[2] = 'regs[2] = "dlrow" .. tostring(os.clock() > -1)'
        oracle = loop_oracle(effects, range(1, 9), 'R[n] = string.reverse(s)', vm=True)
        m, sites, notes, text, report = show(FROM_INPUT, helper_into_regs(), oracle, True)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertNotIn('eurtworld', text)
        self.assertIn('print(%s .. " world")' % MARKED, text)
        self.assertIn('(input, ({}), 2)', text)
        self.assertIn('what it is handed is not constant', notes[-1])
        self.assertEqual([(s.shown, s.unproved, s.ran, s.fixed) for s in sites], [({}, {2}, True, False)])

    def test_constant_input_is_shown(self):
        # (the same probe with the constant: shown, complete)
        oracle = loop_oracle(HelperIntoRegisters_EFFECTS, range(1, 9), 'R[n] = string.reverse(s)', vm=True)
        m, sites, notes, text, report = show(MAIN_REGS, helper_into_regs(), oracle, True)
        self.assertEqual(report[0][:15], 'result: complet', report)
        self.assertEqual([(s.shown, s.fixed) for s in sites], [({2: PLAIN}, True)])

    def test_result_kept(self):
        text = PROBE % {'decode': SAME, 'enc': ENC, 'call': CALL_RESULT, 'src': 1, 'order': ', '.join(map(str, range(1, 9)))}
        text = text.replace('regs[2] = "%s"' % ENC, 'regs[2] = "dlrow" .. tostring(os.clock() > -1)')
        oracle, _err = probe_oracle(None, text=text)
        m, sites, notes, text, report = show(RESULT_FROM_INPUT, helper_returns(), oracle, True)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertNotIn('eurtworld', text)
        # (the call stays, handed the input: the script's code as it is)
        self.assertIn('(input) .. " world")', text)
        self.assertTrue(notes[-1].startswith('[runvals-missing] '), notes)
        self.assertEqual([(s.kind, s.shown, s.unproved, s.fixed) for s in sites], [('result', {}, {1}, False)])


# ---------------------------------------------------------------- what the function called reads, and which record covers it
# More handlers for the programs below:
# 27: R[A](R[B], R, C); R[A + 5] = R[A + 6]()    -- a call handed the register array, then a second call
# 28: R[A] = R[B][R[C]]
# 29: pcall(R[A], R[B], R, C)
# 30: R[A](R[B], R, C); R[9] = R[9] .. "z"       -- a statement after the call reads R9, then sets it
# 31: R[A](R[B], R, C); R[9] = "z"               -- ... sets R9 without reading it
# 32: R[A](R[B], R, C); R[A](R[B], R, C + 1)     -- two calls handed the register array
# 33: R[A] = R[R[B]]                             -- a register number known only at run time (does not lift)
# 37: R[A] = function made from proto B that shares register C (its upvalue 0)
# 39: upvalue B = R[A]
# 40: R[A] = upvalue B
# 41: R[A] = R[B][C]                             -- a constant key
# 43: R[A](R[B], R, R[C])                        -- a register number read from a register
# 44: setmetatable(R, R[A])
# 45: R[A] = R[A]()
# 46: R[A] = {}
MORE = """elseif op == 27 then
R[A[pc]](R[B[pc]], R, C[pc])
R[A[pc] + 5] = R[A[pc] + 6]()
elseif op == 28 then
R[A[pc]] = R[B[pc]][R[C[pc]]]
elseif op == 29 then
pcall(R[A[pc]], R[B[pc]], R, C[pc])
elseif op == 30 then
R[A[pc]](R[B[pc]], R, C[pc])
R[9] = R[9] .. "z"
elseif op == 31 then
R[A[pc]](R[B[pc]], R, C[pc])
R[9] = "z"
elseif op == 32 then
R[A[pc]](R[B[pc]], R, C[pc])
R[A[pc]](R[B[pc]], R, C[pc] + 1)
elseif op == 33 then
R[A[pc]] = R[R[B[pc]]]
elseif op == 41 then
R[A[pc]] = R[B[pc]][C[pc]]
elseif op == 43 then
R[A[pc]](R[B[pc]], R, R[C[pc]])
elseif op == 44 then
setmetatable(R, R[A[pc]])
elseif op == 45 then
R[A[pc]] = R[A[pc]]()
elseif op == 46 then
R[A[pc]] = {}
"""


def mini_more():
    m0 = Mini(HANDLERS, prologue='for i = 1, NP do R[i] = args[i] end', outer=PACKER)
    cells, base = m0.model.cells, m0.model.up_base
    shared = """elseif op == 37 then
R[A[pc]] = wrap(B[pc], {[%d] = {[%d] = R, [%d] = C[pc]}})
elseif op == 39 then
local c = U[%d + B[pc]]
c[%d][c[%d]] = R[A[pc]]
elseif op == 40 then
local c = U[%d + B[pc]]
R[A[pc]] = c[%d][c[%d]]
""" % (base, cells[0], cells[1], base, cells[0], cells[1], base, cells[0], cells[1])
    return Mini(HANDLERS + MORE + shared, prologue='for i = 1, NP do R[i] = args[i] end', outer=PACKER)


def show_more(main, oracle, helpers=True):
    """lift `main` (its first function made is the helper), show the run's values, and report"""
    m = mini_more()
    m.lift(proto(main))
    m.root = m.lifter.root_key
    m.helper = min((k for k in m.protos if k != m.root), default=None)
    hs = {m.helper} if helpers and m.helper is not None else set()
    sites = runvals.show_run_values(m.protos, own(oracle, m.root), hs)
    notes = runvals.notes(sites, hs)
    text = text_of(m, runvals.labels(sites, hs, m.protos))
    return m, sites, notes, text, run.report(text, notes=notes)


def helper_reads_global():
    """function(s, R, n) R[n] = s:reverse() .. input end"""
    return proto([(23, 4, 1, 0), (3, 5, 'input', 0), (21, 4, 4, 5), (22, 2, 3, 4), (2, 1, 0, 0)], nparams=3)


def helper_reads_reg7():
    """function(s, R, n) R[n] = s:reverse() .. R[7] end"""
    return proto([(23, 4, 1, 0), (1, 6, 7, 0), (28, 5, 2, 6), (21, 4, 4, 5), (22, 2, 3, 4), (2, 1, 0, 0)], nparams=3)


def helper_with_a_lift_error():
    """function(s, R, n) local x = s:reverse(); local y = <register n>; R[n] = x end: the second
    instruction reads a register whose number is known only at run time and does not lift"""
    return proto([(23, 4, 1, 0), (33, 5, 3, 0), (22, 2, 3, 4), (2, 1, 0, 0)], nparams=3)


def helper_sets_its_upvalue():
    """function(s, R, n) R[n] = s:reverse(); <upvalue 0> = R[n]'s value end (it shares the variable)"""
    return proto([(23, 4, 1, 0), (22, 2, 3, 4), (39, 4, 0, 0), (2, 1, 0, 0)], nparams=3)


RUN_TIME = 'tostring(os.clock() > -1)'      # a value the probe reads at run time ("true")
REVERSE = 'R[n] = string.reverse(s)'


def effects(*lines):
    return {i + 1: x for i, x in enumerate(lines)}


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class ProofOfTheCall(unittest.TestCase):
    """What a call writes is shown only when it is the same at every execution of the call as far as the
    code shows: the function called is a closure of the program, handed constants besides the register
    array, whose captures hold fixed values and whose code reads no global but the standard library's
    and never reads the register array it is handed.  The run shows one value in every probe here."""

    def unproved(self, main, eff, work, why):
        oracle = loop_oracle(eff, range(1, len(eff) + 2), work, vm=True)
        m, sites, notes, text, report = show_more(main, oracle)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertNotIn('"hellotrue"', text)
        self.assertNotIn('"%s"' % PLAIN, text.split('print(')[-1])
        self.assertIn('print(%s .. " world")' % MARKED, text)
        self.assertEqual([(s.shown, s.unproved, s.ran, s.fixed) for s in sites], [({}, {2}, True, False)])
        self.assertIn(why, sites[0].why)
        self.assertIn(why, notes[-1])
        return sites

    def test_helper_reads_a_global(self):
        # (the verifier's probe N1) handed constants only, but it reads a global itself
        main = [(8, 1, helper_reads_global(), 0), (1, 2, ENC, 0), (20, 1, 2, 2), (3, 3, 'print', 0), (1, 5, ' world', 0),
                (21, 4, 2, 5), (5, 3, 0, 0), (2, 3, 0, 0)]
        eff = effects('regs[1] = helper', 'regs[2] = "%s"' % ENC, 'regs[1](regs[2], regs, 2)', 'regs[3] = print',
                      'regs[5] = " world"', 'regs[4] = regs[2] .. regs[5]', 'regs[3] = nil')
        self.unproved(main, eff, 'R[n] = string.reverse(s) .. ' + RUN_TIME, 'it reads the global input')

    def test_helper_reads_the_register_array(self):
        # (N2) it reads another register of its caller through the array, one that holds a global
        main = [(8, 1, helper_reads_reg7(), 0), (3, 7, 'input', 0), (1, 2, ENC, 0), (20, 1, 2, 2), (3, 3, 'print', 0),
                (1, 5, ' world', 0), (21, 4, 2, 5), (5, 3, 0, 0), (2, 3, 0, 0)]
        eff = effects('regs[1] = helper', 'regs[7] = %s' % RUN_TIME, 'regs[2] = "%s"' % ENC, 'regs[1](regs[2], regs, 2)',
                      'regs[3] = print', 'regs[5] = " world"', 'regs[4] = regs[2] .. regs[5]', 'regs[3] = nil')
        self.unproved(main, eff, 'R[n] = string.reverse(s) .. R[7]', 'it reads the register array it is handed')

    def test_helper_captures_a_value_read_at_run_time(self):
        # (N3) the function called shares a variable that holds a global
        main = [(3, 7, 'input', 0), (37, 1, helper_into_regs(), 7), (1, 2, ENC, 0), (20, 1, 2, 2), (3, 3, 'print', 0),
                (1, 5, ' world', 0), (21, 4, 2, 5), (5, 3, 0, 0), (2, 3, 0, 0)]
        eff = effects('regs[7] = %s' % RUN_TIME, 'regs[1] = helper', 'regs[2] = "%s"' % ENC, 'regs[1](regs[2], regs, 2)',
                      'regs[3] = print', 'regs[5] = " world"', 'regs[4] = regs[2] .. regs[5]', 'regs[3] = nil')
        self.unproved(main, eff, 'R[n] = string.reverse(s) .. ' + RUN_TIME, 'an upvalue')

    def test_helper_captures_a_constant(self):
        # (control) the same with a constant in the shared variable: proved, shown, complete
        main = [(1, 7, 'k', 0), (37, 1, helper_into_regs(), 7), (1, 2, ENC, 0), (20, 1, 2, 2), (3, 3, 'print', 0),
                (1, 5, ' world', 0), (21, 4, 2, 5), (5, 3, 0, 0), (2, 3, 0, 0)]
        eff = effects('regs[7] = "k"', 'regs[1] = helper', 'regs[2] = "%s"' % ENC, 'regs[1](regs[2], regs, 2)',
                      'regs[3] = print', 'regs[5] = " world"', 'regs[4] = regs[2] .. regs[5]', 'regs[3] = nil')
        oracle = loop_oracle(eff, range(1, 10), REVERSE, vm=True)
        m, sites, notes, text, report = show_more(main, oracle)
        self.assertEqual(report[0][:15], 'result: complet', report)
        self.assertEqual([(s.shown, s.fixed, s.assumed) for s in sites], [({2: PLAIN}, True, False)])
        self.assertIn('print("%s" .. " world")' % PLAIN, text)

    def test_helper_with_a_lift_error(self):
        # no rule fails, but the helper's code did not all lift: the run's value is shown, said to be assumed at
        # the value, in a note, and the result is PARTIAL
        main = [(8, 1, helper_with_a_lift_error(), 0), (1, 2, ENC, 0), (20, 1, 2, 2), (3, 3, 'print', 0),
                (1, 5, ' world', 0), (21, 4, 2, 5), (5, 3, 0, 0), (2, 3, 0, 0)]
        oracle = loop_oracle(HelperIntoRegisters_EFFECTS, range(1, 9), REVERSE, vm=True)
        m, sites, notes, text, report = show_more(main, oracle)
        self.assertTrue(m.errors)
        self.assertEqual([(s.shown, s.unproved, s.fixed, s.assumed) for s in sites], [({2: PLAIN}, set(), True, True)])
        self.assertIn('print("%s" --[[%s]] .. " world")' % (PLAIN, runvals.ASSUMED), text)
        self.assertEqual([n.split(' ')[0] for n in notes], ['[runvals]', '[runvals-assumed]'], notes)
        self.assertIn('assumed, not proved, to depend only on what the call is handed', notes[1])
        self.assertIn('did not all lift', notes[1])
        self.assertEqual(report[0], 'result: PARTIAL', report)
        lines = run.run_report(notes)
        self.assertTrue(any(ln.startswith(run.RUNVALS_ASSUMED_LINE) for ln in lines), lines)
        # the assumption alone makes the result PARTIAL: from the notes, from the head of a written file, and from
        # the comment at the value
        plain = 'print("%s" --[[%s]] .. " world")\n' % (PLAIN, runvals.ASSUMED)
        self.assertEqual(run.report('print("x")\n', notes=[n for n in notes if 'assumed' in n.split(' ')[0]])[0],
                         'result: PARTIAL')
        head = ['-- case.lua, %s.  How the run went and what this file lacks:' % run.HEAD_MARK]
        head += ['--   ' + ln for ln in lines]
        again = run.report('\n'.join(head) + '\nprint("x")\n')
        self.assertEqual(again[0], 'result: PARTIAL', again)
        alone = run.report(plain)
        self.assertEqual(alone[0], 'result: PARTIAL', alone)
        self.assertTrue(any('1 value(s) in the text carry the comment' in ln for ln in alone), alone)

    def test_library_function_handed_the_register_array(self):
        # a library function handed the register array (table.move on it, say) is not followed: not proved
        m = lift(MAIN_REGS, helper_into_regs())
        node = m.protos[m.root].nodes[(0, 3)]
        stmts, term = node.tree
        node.tree = ([st if st[0] != 'call' else st[:2] + (('kfn', 'table.move'),) + st[3:] for st in stmts], term)
        rec = {'n': 1, 'p': 0, 'r': {}, 's': {2: ENC}, 'succ': {(0, 4)}}
        after = {'n': 1, 'p': 0, 'r': {}, 's': {2: PLAIN}, 'succ': {(0, 5)}}
        sites = runvals.show_run_values(m.protos, {(m.root, 0, 3): rec, (m.root, 0, 4): after}, {m.helper})
        self.assertEqual([(s.shown, s.unproved, s.fixed) for s in sites], [({}, {2}, False)])
        self.assertIn('library function (table.move)', sites[0].why)


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class WhichRecordCovers(unittest.TestCase):
    """The harness sees a call by the VM function it enters.  Its record of what changed (`w`) is used only
    where the instruction's one call is the one handed the register array and calls a closure of the program."""

    def test_no_vm_function_next_to_a_vm_function(self):
        # (N5) `decode(R2, R, 0)` (no VM function: it writes R2 and R9), then `R6 = vmfn()` in the same
        # instruction: the run saw one VM call (h == n), which changed R6 only.  R2 and R9 are not shown as
        # the values from before the call
        main = [(3, 1, 'decode', 0), (8, 7, proto([(1, 1, 1, 0), (2, 1, 0, 0)]), 0), (1, 2, ENC, 0), (1, 9, 'x', 0),
                (27, 1, 2, 0), (3, 3, 'print', 0), (21, 4, 2, 9), (5, 3, 0, 0), (2, 3, 0, 0)]
        eff = effects('regs[1] = function(s, R, n) R[2] = string.reverse(s); R[9] = "y" end',
                      'regs[7] = function() local hr = {}; __LPH_STEP(hp, 0, 1, hr); return 1 end',
                      'regs[2] = "%s"' % ENC, 'regs[9] = "x"', 'regs[1](regs[2], regs, 0); regs[6] = regs[7]()',
                      'regs[3] = print', 'regs[4] = regs[2] .. regs[9]', 'regs[3] = nil')
        oracle = loop_oracle(eff, range(1, 10), 'error("unused")', vm=True)
        rec = own(oracle, 4)[(4, 0, 5)]
        self.assertEqual((rec['h'], rec['n'], rec['w']), (1, 1, {6}))
        m, sites, notes, text, report = show_more(main, oracle, helpers=False)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertNotRegex(text, r'print\(\w+ \.\. "x"\)')
        self.assertIn('print(%s .. %s)' % (MARKED, MARKED), text)
        self.assertEqual([(s.unproved, s.ran, s.fixed) for s in sites], [({2, 9}, True, False)])

    def test_helper_called_through_pcall(self):
        # (N6) the call handed the register array is pcall's: the function called is not followed
        main = [(8, 1, helper_into_regs(), 0), (1, 2, ENC, 0), (29, 1, 2, 2), (3, 3, 'print', 0), (1, 5, ' world', 0),
                (21, 4, 2, 5), (5, 3, 0, 0), (2, 3, 0, 0)]
        eff = dict(HelperIntoRegisters_EFFECTS)
        eff[3] = 'pcall(regs[1], regs[2], regs, 2)'
        oracle = loop_oracle(eff, range(1, 9), REVERSE, vm=True)
        m, sites, notes, text, report = show_more(main, oracle)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertIn('print(%s .. " world")' % MARKED, text)
        self.assertEqual([(s.shown, s.unproved, s.fixed) for s in sites], [({}, {2}, False)])

    def test_argument_from_a_table(self):
        # (N8) the helper is handed a table field read at run time
        main = [(8, 1, helper_into_regs(), 0), (3, 7, 'tbl', 0), (1, 6, 'k', 0), (28, 2, 7, 6), (20, 1, 2, 2),
                (3, 3, 'print', 0), (1, 5, ' world', 0), (21, 4, 2, 5), (5, 3, 0, 0), (2, 3, 0, 0)]
        eff = effects('regs[1] = helper', 'regs[7] = {k = "%s"}' % ENC, 'regs[6] = "k"', 'regs[2] = regs[7][regs[6]]',
                      'regs[1](regs[2], regs, 2)', 'regs[3] = print', 'regs[5] = " world"', 'regs[4] = regs[2] .. regs[5]',
                      'regs[3] = nil')
        oracle = loop_oracle(eff, range(1, 11), REVERSE, vm=True)
        m, sites, notes, text, report = show_more(main, oracle)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertIn('print(%s .. " world")' % MARKED, text)
        self.assertEqual([(s.shown, s.unproved, s.fixed, s.why) for s in sites],
                         [({}, {2}, False, 'what it is handed is not constant')])

    def test_argument_set_on_one_path(self):
        # (N9) the argument is set on one of the two paths into the call (the run took that one)
        main = [(8, 1, helper_into_regs(), 0), (3, 6, 'flag', 0), (7, 6, 4, 0), (1, 2, ENC, 0), (20, 1, 2, 2),
                (3, 3, 'print', 0), (1, 5, ' world', 0), (21, 4, 2, 5), (5, 3, 0, 0), (2, 3, 0, 0)]
        eff = effects('regs[1] = helper', 'regs[6] = false', '', 'regs[2] = "%s"' % ENC, 'regs[1](regs[2], regs, 2)',
                      'regs[3] = print', 'regs[5] = " world"', 'regs[4] = regs[2] .. regs[5]', 'regs[3] = nil')
        oracle = loop_oracle(eff, range(1, 11), REVERSE, vm=True)
        m, sites, notes, text, report = show_more(main, oracle)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertIn('print(%s .. " world")' % MARKED, text)
        self.assertEqual([(s.shown, s.unproved, s.fixed) for s in sites], [({}, {2}, False)])

    def test_two_calls_handed_the_register_array(self):
        # one instruction hands the register array to two calls (into R2, then R3): each call is a site, and after
        # each the register it names is not shown
        main = [(8, 1, helper_into_regs(), 0), (1, 2, ENC, 0), (1, 3, ENC, 0), (32, 1, 2, 2), (3, 4, 'print', 0),
                (21, 5, 2, 3), (5, 4, 0, 0), (2, 4, 0, 0)]
        eff = effects('regs[1] = helper', 'regs[2] = "%s"' % ENC, 'regs[3] = "%s"' % ENC,
                      'regs[1](regs[2], regs, 2); regs[1](regs[2], regs, 3)', 'regs[4] = print',
                      'regs[5] = regs[2] .. regs[3]', 'regs[4] = nil')
        oracle = loop_oracle(eff, range(1, 9), REVERSE, vm=True)
        m, sites, notes, text, report = show_more(main, oracle)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertEqual([(s.ran, s.marked) for s in sites], [(False, {2}), (False, {3})])
        self.assertNotIn('"%s"' % ENC, text.split('print(')[-1])
        # (the second call is handed R2 as the first call left it: not shown either)
        self.assertRegex(text, r'local (\w+) = %s\n(?:.*\n)*.*print\(\1 \.\. %s\)' % (re.escape(MARKED), re.escape(MARKED)))


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class SharedOrReadAgain(unittest.TestCase):
    """A register the call may have set that a function of the program shares, or that the instruction's own
    statements after the call read before setting it again."""

    def test_captured_after_the_call(self):
        # (N4) a function made after the call shares R2, which the call wrote through the register array: no
        # function assigns it through its upvalue, so the text would show the old value: marked
        main = [(8, 1, helper_into_regs(), 0), (1, 2, ENC, 0), (20, 1, 2, 2), (37, 6, proto([(2, 1, 0, 0)]), 2),
                (3, 3, 'print', 0), (1, 5, ' world', 0), (21, 4, 2, 5), (5, 3, 0, 0), (2, 3, 0, 0)]
        eff = effects('regs[1] = helper', 'regs[2] = "%s"' % ENC, 'regs[1](regs[2], regs, 2)', 'regs[6] = function() end',
                      'regs[3] = print', 'regs[5] = " world"', 'regs[4] = regs[2] .. regs[5]', 'regs[3] = nil')
        oracle = loop_oracle(eff, range(1, 10), REVERSE, vm=True)
        m, sites, notes, text, report = show_more(main, oracle)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertNotRegex(text, r'local (\w+) = "%s"\n(?:.*\n)*.*print\(\1 \.\. " world"\)' % ENC)
        self.assertIn(MARKED, text)
        self.assertEqual([(s.shown, s.unproved) for s in sites], [({}, {2})])

    def test_changed_through_the_function_that_shares_it(self):
        # (control) the function called shares R2 and assigns it through its upvalue: its text shows the change,
        # and nothing is marked
        main = [(1, 2, ENC, 0), (37, 1, helper_sets_its_upvalue(), 2), (20, 1, 2, 2), (3, 3, 'print', 0),
                (1, 5, ' world', 0), (21, 4, 2, 5), (5, 3, 0, 0), (2, 3, 0, 0)]
        eff = effects('regs[2] = "%s"' % ENC, 'regs[1] = helper', 'regs[1](regs[2], regs, 2)', 'regs[3] = print',
                      'regs[5] = " world"', 'regs[4] = regs[2] .. regs[5]', 'regs[3] = nil')
        oracle = loop_oracle(eff, range(1, 9), REVERSE, vm=True)
        m, sites, notes, text, report = show_more(main, oracle)
        self.assertNotIn(MARKED, text)
        self.assertEqual([(s.shown, s.unproved) for s in sites], [({}, set())])
        self.assertEqual(notes, [])
        self.assertRegex(text, r'\w+ = p1:reverse\(\)')

    def test_read_again_in_the_same_instruction(self):
        # (N7) `R[A](R[B], R, 9); R[9] = R[9] .. "z"`: the next instruction saw what the instruction made of R9,
        # not what the call left
        main = [(8, 1, helper_into_regs(), 0), (1, 2, ENC, 0), (1, 9, 'x', 0), (30, 1, 2, 9), (3, 3, 'print', 0),
                (21, 4, 9, 9), (5, 3, 0, 0), (2, 3, 0, 0)]
        eff = effects('regs[1] = helper', 'regs[2] = "%s"' % ENC, 'regs[9] = "x"',
                      'regs[1](regs[2], regs, 9); regs[9] = regs[9] .. "z"', 'regs[3] = print',
                      'regs[4] = regs[9] .. regs[9]', 'regs[3] = nil')
        oracle = loop_oracle(eff, range(1, 9), REVERSE, vm=True)
        m, sites, notes, text, report = show_more(main, oracle)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertNotIn('"x" .. "z"', text)
        self.assertIn('%s .. "z"' % MARKED, text)
        self.assertEqual([(s.shown, s.unproved) for s in sites], [({}, {9})])

    def test_set_again_in_the_same_instruction(self):
        # (control) `R[A](R[B], R, 9); R[9] = "z"`: set again before anything reads it, nothing to mark
        main = [(8, 1, helper_into_regs(), 0), (1, 2, ENC, 0), (1, 9, 'x', 0), (31, 1, 2, 9), (3, 3, 'print', 0),
                (21, 4, 9, 9), (5, 3, 0, 0), (2, 3, 0, 0)]
        eff = effects('regs[1] = helper', 'regs[2] = "%s"' % ENC, 'regs[9] = "x"',
                      'regs[1](regs[2], regs, 9); regs[9] = "z"', 'regs[3] = print', 'regs[4] = regs[9] .. regs[9]',
                      'regs[3] = nil')
        oracle = loop_oracle(eff, range(1, 9), REVERSE, vm=True)
        m, sites, notes, text, report = show_more(main, oracle)
        self.assertEqual(report[0][:15], 'result: complet', report)
        self.assertNotIn(MARKED, text)
        self.assertEqual([(s.shown, s.unproved) for s in sites], [({}, set())])


def helper_reverse_plus_up():
    """function(s, R, n) R[n] = s:reverse() .. <upvalue 0> end"""
    return proto([(23, 4, 1, 0), (40, 5, 0, 0), (21, 4, 4, 5), (22, 2, 3, 4), (2, 1, 0, 0)], nparams=3)


def helper_calls_up():
    """function(s, R, n) R[n] = s:reverse() .. <upvalue 0>() end"""
    return proto([(23, 4, 1, 0), (40, 5, 0, 0), (45, 5, 0, 0), (21, 4, 4, 5), (22, 2, 3, 4), (2, 1, 0, 0)], nparams=3)


def helper_reads_a_library_field():
    """function(s, R, n) local t = string; R[n] = s:reverse() .. t.n end  (string.n: not the library's)"""
    return proto([(23, 4, 1, 0), (3, 6, 'string', 0), (41, 5, 6, 'n'), (21, 4, 4, 5), (22, 2, 3, 4), (2, 1, 0, 0)],
                 nparams=3)


def helper_tostring_of_a_table():
    """function(s, R, n) R[n] = s:reverse() .. tostring({}) end  (a table's address)"""
    return proto([(23, 4, 1, 0), (3, 5, 'tostring', 0), (46, 6, 0, 0), (5, 5, 0, 0), (21, 4, 4, 5), (22, 2, 3, 4),
                  (2, 1, 0, 0)], nparams=3)


def sets_its_upvalue_to_a_global():
    """function() <upvalue 0> = input end"""
    return proto([(3, 1, 'input', 0), (39, 1, 0, 0), (2, 1, 0, 0)])


# the encrypted constant printed as R2's value after the call
STALE = r'^local (\w+) = "%s"\n(?:.*\n)*.*print\(\1 \.\. " world"\)' % ENC
# ... and the variable a function shares printed as not shown
SHARED_MARKED = r'local (\w+) = %s\n(?:.*\n)*.*print\(\1 \.\. " world"\)' % re.escape(MARKED)
# print(R2 .. " world") (4 instructions, a return) and what the probe does for it
PRINT_R2 = [(3, 3, 'print', 0), (1, 5, ' world', 0), (21, 4, 2, 5), (5, 3, 0, 0), (2, 3, 0, 0)]
PRINT_R2_RUN = ('regs[3] = print', 'regs[5] = " world"', 'regs[4] = regs[2] .. regs[5]', 'regs[3] = nil')


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class WhatTheCodeShows(unittest.TestCase):
    """What a proof of a call follows is the program's code: a register changed by a function that shares it, a
    register array handed to a call that is not followed (it can keep the array and change any register later), a
    register the call changed that a function shares when no record of the run covers the call, a field the
    standard library does not have, and library functions whose result can differ for the same arguments."""

    def run_it(self, main, eff, work, helpers=True):
        oracle = loop_oracle(eff, range(1, len(eff) + 2), work, vm=True)
        return show_more(main, oracle, helpers)

    def not_shown(self, main, eff, work, wrong, helpers=True):
        # wrong: what the text must not show (a value as proved), or a pattern of the stale value printed
        m, sites, notes, text, report = self.run_it(main, eff, work, helpers)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        if wrong.startswith('^'):
            self.assertNotRegex(text, wrong[1:])
        else:
            self.assertNotIn(wrong, text)
        self.assertIn(MARKED, text)
        self.assertTrue(any(n.startswith('[runvals-missing] ') for n in notes), notes)
        return m, sites, notes, text

    def test_argument_changed_through_a_function_that_shares_it(self):
        # (the verifier's probe P12h) R5 = "abc"; a function that shares R5 sets it to a global and is called (a call
        # not handed the register array); then helper(R5, R, 2): what the helper is handed is not a constant
        main = [(1, 5, 'abc', 0), (37, 6, sets_its_upvalue_to_a_global(), 5), (8, 1, helper_into_regs(), 0),
                (45, 6, 0, 0), (20, 1, 5, 2)] + PRINT_R2
        eff = effects('regs[5] = "abc"', 'regs[6] = function() regs[5] = %s end' % RUN_TIME, 'regs[1] = helper',
                      'regs[6] = regs[6]()', 'regs[1](regs[5], regs, 2)', *PRINT_R2_RUN)
        m, sites, notes, text = self.not_shown(main, eff, REVERSE, '"eurt"', helpers=False)
        site = [s for s in sites if s.kind == 'regs'][0]
        self.assertEqual((site.shown, site.unproved, site.fixed), ({}, {2}, False))
        self.assertEqual(site.why, 'what it is handed is not constant')
        self.assertIn('print(%s .. ' % MARKED, text)     # (R5, the shared variable, is " world" by then)


    def test_another_call_handed_a_register_number_read_at_run_time(self):
        # (P5) helper("zzz", R, idx) with idx a global (this run: 9, which nothing reads), then helper(R5, R, 2) with
        # R5 = "abc": at another execution the first call can write R5 (or anything else)
        main = [(8, 1, helper_into_regs(), 0), (1, 5, 'abc', 0), (3, 7, 'idx', 0), (1, 8, 'zzz', 0), (43, 1, 8, 7),
                (20, 1, 5, 2)] + PRINT_R2
        eff = effects('regs[1] = helper', 'regs[5] = "abc"', 'regs[7] = 9', 'regs[8] = "zzz"',
                      'regs[1](regs[8], regs, regs[7])', 'regs[1](regs[5], regs, 2)', *PRINT_R2_RUN)
        m, sites, notes, text = self.not_shown(main, eff, REVERSE, '"cba"')
        self.assertEqual([(s.shown, s.fixed) for s in sites], [({}, False), ({}, False)])
        self.assertEqual(sites[0].why, 'what it is handed is not constant')
        self.assertIn('print(%s .. " world")' % MARKED, text)


    def test_shared_variable_and_another_call_handed_a_run_time_register_number(self):
        # (P5b) the helper shares R7 ("k"); another call handed the register array and a register number read at run
        # time can change R7 at another execution
        main = [(1, 7, 'k', 0), (37, 1, helper_reverse_plus_up(), 7), (8, 6, helper_into_regs(), 0), (3, 9, 'idx', 0),
                (1, 8, 'zzz', 0), (43, 6, 8, 9), (1, 2, ENC, 0), (20, 1, 2, 2)] + PRINT_R2
        eff = effects('regs[7] = "k"', 'regs[1] = helper', 'regs[6] = helper', 'regs[9] = 11', 'regs[8] = "zzz"',
                      'regs[6](regs[8], regs, regs[9])', 'regs[2] = "%s"' % ENC, 'regs[1](regs[2], regs, 2)',
                      *PRINT_R2_RUN)
        m, sites, notes, text = self.not_shown(main, eff, 'if n == 2 then R[n] = string.reverse(s) .. "k" else '
                                                          'R[n] = string.reverse(s) end', '"hellok"')
        self.assertEqual([(s.fixed, s.unproved) for s in sites], [(False, set()), (False, {2})])
        self.assertIn('print(%s .. " world")' % MARKED, text)


    def test_metatable_set_on_the_register_array(self):
        # (P6b) setmetatable(R, mt) (a library function handed the register array) before the helper is made: what the
        # helper stores goes through the metatable, which no statement shows
        main = [(3, 6, 'mt', 0), (44, 6, 0, 0), (8, 1, helper_into_regs(), 0), (1, 2, ENC, 0), (20, 1, 2, 9),
                (3, 3, 'print', 0), (1, 5, ' world', 0), (21, 4, 9, 5), (5, 3, 0, 0), (2, 3, 0, 0)]
        eff = effects('regs[6] = {__newindex = function(t, k, v) rawset(t, k, k == 9 and v .. %s or v) end}' % RUN_TIME,
                      'setmetatable(regs, regs[6])', 'regs[1] = helper', 'regs[2] = "%s"' % ENC,
                      'regs[1](regs[2], regs, 9)', 'regs[3] = print', 'regs[5] = " world"', 'regs[4] = regs[9] .. regs[5]',
                      'regs[3] = nil')
        m, sites, notes, text = self.not_shown(main, eff, REVERSE, '"hellotrue"')
        self.assertIn('library function (setmetatable)', sites[0].why)
        self.assertEqual((sites[1].fixed, sites[1].unproved), (False, {9}))
        self.assertIn('print(%s .. " world")' % MARKED, text)


    def test_register_array_kept_in_a_table(self):
        # the program keeps its register array in a table (through R6 = R): any code that reaches the table can change
        # any register later, which no statement shows
        main = [(8, 1, helper_into_regs(), 0), (25, 6, 0, 0), (3, 7, 'stash', 0), (1, 8, 'k', 0), (22, 7, 8, 6),
                (1, 2, ENC, 0), (20, 1, 2, 2)] + PRINT_R2
        eff = effects('regs[1] = helper', 'regs[6] = regs', 'regs[7] = {}', 'regs[8] = "k"', 'regs[7][regs[8]] = regs[6]',
                      'regs[2] = "%s"' % ENC, 'regs[1](regs[2], regs, 2)', *PRINT_R2_RUN)
        m, sites, notes, text = self.not_shown(main, eff, REVERSE, '"%s"' % PLAIN)
        self.assertEqual([(s.fixed, s.unproved) for s in sites], [(False, {2})])
        self.assertIn('the register array is kept in a table', sites[0].why)
        self.assertIn('print(%s .. " world")' % MARKED, text)

    def test_shared_register_changed_by_a_call_with_no_record(self):
        # (P11) decode(R2, R, 0), decode a global that is no VM function, writes R2; a function made after the call
        # shares R2: no record covers the call, but the run saw R2 change
        main = [(3, 1, 'decode', 0), (1, 2, ENC, 0), (20, 1, 2, 0), (37, 6, proto([(2, 1, 0, 0)]), 2)] + PRINT_R2
        eff = effects('regs[1] = function(s, R, n) R[2] = string.reverse(s) end', 'regs[2] = "%s"' % ENC,
                      'regs[1](regs[2], regs, 0)', 'regs[6] = function() end', *PRINT_R2_RUN)
        m, sites, notes, text = self.not_shown(main, eff, 'error("unused")', STALE, helpers=False)
        self.assertEqual([(s.unproved, s.fixed) for s in sites], [({2}, False)])
        self.assertRegex(text, SHARED_MARKED)

    def test_shared_register_changed_by_a_function_the_code_does_not_name(self):
        # (P11b) the same with a VM function read from a global: the harness saw it (`w`), but the code does not say
        # which function it is, so that record is not used
        main = [(3, 1, 'decode', 0), (1, 2, ENC, 0), (20, 1, 2, 0), (37, 6, proto([(2, 1, 0, 0)]), 2)] + PRINT_R2
        eff = effects('regs[1] = helper', 'regs[2] = "%s"' % ENC, 'regs[1](regs[2], regs, 0)', 'regs[6] = function() end',
                      *PRINT_R2_RUN)
        oracle = loop_oracle(eff, range(1, len(eff) + 2), 'R[2] = string.reverse(s)', vm=True)
        self.assertEqual(own(oracle, 4)[(4, 0, 3)]['w'], {2})
        m, sites, notes, text = self.not_shown(main, eff, 'R[2] = string.reverse(s)', STALE, helpers=False)
        self.assertEqual([(s.unproved, s.fixed) for s in sites], [({2}, False)])
        self.assertRegex(text, SHARED_MARKED)

    def test_field_the_library_does_not_have(self):
        # (P9) string.n, read through a variable that holds the string table: a script can keep a value there
        main = [(8, 1, helper_reads_a_library_field(), 0), (1, 2, ENC, 0), (20, 1, 2, 2)] + PRINT_R2
        eff = effects('regs[1] = helper', 'regs[2] = "%s"' % ENC, 'regs[1](regs[2], regs, 2)', *PRINT_R2_RUN)
        m, sites, notes, text = self.not_shown(main, eff, 'R[n] = string.reverse(s) .. tostring(string.n)', '"hellonil"')
        self.assertIn('string.n, which the standard library does not have', sites[0].why)
        self.assertIn('print(%s .. " world")' % MARKED, text)


    def test_captured_function_that_returns_a_constant(self):
        # (control, P1c) the helper calls a function it shares, which returns a constant: proved, shown, complete
        g = proto([(1, 1, 'k', 0), (2, 1, 0, 0)])
        main = [(8, 6, g, 0), (37, 1, helper_calls_up(), 6), (1, 2, ENC, 0), (20, 1, 2, 2)] + PRINT_R2
        eff = effects('regs[6] = function() end', 'regs[1] = helper', 'regs[2] = "%s"' % ENC, 'regs[1](regs[2], regs, 2)',
                      *PRINT_R2_RUN)
        m, sites, notes, text, report = self.run_it(main, eff, 'R[n] = string.reverse(s) .. "k"', helpers=False)
        self.assertEqual(report[0][:15], 'result: complet', report)
        self.assertIn('print("hellok" .. " world")', text)
        self.assertEqual([(s.fixed, s.assumed) for s in sites], [(True, False)])

    def test_tostring_is_an_assumption(self):
        # (P10) the helper appends tostring of a table it makes (an address, another at every execution): the value is
        # the run's, said at the value to be assumed, and the result is PARTIAL
        main = [(8, 1, helper_tostring_of_a_table(), 0), (1, 2, ENC, 0), (20, 1, 2, 2)] + PRINT_R2
        eff = effects('regs[1] = helper', 'regs[2] = "%s"' % ENC, 'regs[1](regs[2], regs, 2)', *PRINT_R2_RUN)
        m, sites, notes, text, report = self.run_it(main, eff, 'R[n] = string.reverse(s) .. tostring({})')
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertEqual([(s.fixed, s.assumed, s.comment) for s in sites], [(True, True, runvals.ASSUMED_LIB)])
        self.assertRegex(text, r'print\("hellotable: [^"]*" --\[\[%s\]\] \.\. " world"\)' % re.escape(runvals.ASSUMED_LIB))
        self.assertIn('uses tostring', notes[-1])
        self.assertIn('--[[%s]]' % runvals.ASSUMED_LIB, notes[-1])
        self.assertTrue(any('the check could not be completed' in ln and runvals.ASSUMED_LIB in ln for ln in report), report)
        self.assertEqual(run.report(text)[0], 'result: PARTIAL')

    def test_calling_code_that_did_not_all_lift(self):
        # the helper is proved, but an instruction of the calling function (on a path the run did not take) did not
        # lift: what it does to the registers is not known, and the value is assumed
        main = [(8, 1, helper_into_regs(), 0), (1, 2, ENC, 0), (20, 1, 2, 2), (3, 3, 'print', 0), (1, 5, ' world', 0),
                (21, 4, 2, 5), (5, 3, 0, 0), (3, 6, 'flag', 0), (7, 6, 11, 0), (2, 3, 0, 0), (3, 8, 'idx', 0),
                (33, 9, 8, 0), (2, 3, 0, 0)]
        eff = effects('regs[1] = helper', 'regs[2] = "%s"' % ENC, 'regs[1](regs[2], regs, 2)', 'regs[3] = print',
                      'regs[5] = " world"', 'regs[4] = regs[2] .. regs[5]', 'regs[3] = nil', 'regs[6] = false', '', '')
        oracle = loop_oracle(eff, range(1, 11), REVERSE, vm=True)
        m, sites, notes, text, report = show_more(main, oracle)
        self.assertTrue(m.errors)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertEqual([(s.shown, s.fixed, s.assumed, s.comment) for s in sites],
                         [({2: PLAIN}, True, True, runvals.ASSUMED_CALLER)])
        self.assertIn('"%s" --[[%s]]' % (PLAIN, runvals.ASSUMED_CALLER), text)
        self.assertIn('the code that makes the call did not all lift', notes[-1])

    def test_another_call_only_assumed(self):
        # two helpers handed the register array: the first did not all lift (assumed); the second is proved on its own,
        # but it relies on what the first does with the array: assumed too
        main = [(8, 1, helper_with_a_lift_error(), 0), (8, 7, helper_into_regs(), 0), (1, 2, ENC, 0), (20, 1, 2, 2),
                (1, 9, ENC, 0), (20, 7, 9, 9), (3, 3, 'print', 0), (21, 4, 2, 9), (5, 3, 0, 0), (2, 3, 0, 0)]
        eff = effects('regs[1] = helper', 'regs[7] = helper', 'regs[2] = "%s"' % ENC, 'regs[1](regs[2], regs, 2)',
                      'regs[9] = "%s"' % ENC, 'regs[7](regs[9], regs, 9)', 'regs[3] = print', 'regs[4] = regs[2] .. regs[9]',
                      'regs[3] = nil')
        oracle = loop_oracle(eff, range(1, 11), REVERSE, vm=True)
        m, sites, notes, text, report = show_more(main, oracle, helpers=False)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        self.assertEqual([(s.shown, s.assumed, s.comment) for s in sites],
                         [({2: PLAIN}, True, runvals.ASSUMED), ({9: PLAIN}, True, runvals.ASSUMED_OTHER)])
        self.assertIn('print("%s" --[[%s]] .. "%s" --[[%s]])' % (PLAIN, runvals.ASSUMED, PLAIN, runvals.ASSUMED_OTHER), text)
        self.assertTrue(any('one of the comments' in ln for ln in report), report)

    def test_variable_shared_with_a_function_that_did_not_all_lift(self):
        # the helper shares R7 ("k") by reference with another function whose code did not all lift: whether that
        # function changes R7 is not known, and the value is assumed
        main = [(1, 7, 'k', 0), (37, 6, helper_with_a_lift_error(), 7), (37, 1, helper_reverse_plus_up(), 7),
                (1, 2, ENC, 0), (20, 1, 2, 2)] + PRINT_R2
        eff = effects('regs[7] = "k"', 'regs[6] = function() end', 'regs[1] = helper', 'regs[2] = "%s"' % ENC,
                      'regs[1](regs[2], regs, 2)', *PRINT_R2_RUN)
        oracle = loop_oracle(eff, range(1, len(eff) + 2), 'R[n] = string.reverse(s) .. "k"', vm=True)
        m, sites, notes, text, report = show_more(main, oracle, helpers=False)
        self.assertEqual(report[0], 'result: PARTIAL', report)
        site = [s for s in sites if s.kind == 'regs'][0]
        self.assertEqual((site.shown, site.assumed, site.comment), ({2: 'hellok'}, True, runvals.ASSUMED_CALLER))
        self.assertIn('did not all lift, so whether it changes that variable is not known', site.why)


class Pure(unittest.TestCase):
    """runvals on hand-made oracle records (no interpreter needed)"""

    def test_same(self):
        self.assertTrue(runvals._same(1, 1.0))
        self.assertFalse(runvals._same(1, True))
        self.assertFalse(runvals._same('1', 1))
        self.assertTrue(runvals._same(False, False))
        self.assertFalse(runvals._same(None, False))

    def test_captured_register_is_left_alone(self):
        # a register a function of the program shares by reference changes through that function,
        # which the text shows: no value is put for it
        m = lift(MAIN_REGS, helper_into_regs())
        node = m.protos[m.root].nodes[(0, 1)]
        stmts, term = node.tree
        node.tree = (stmts + [('set', ('t', 'X1'), ('closure', m.helper, (('ref', 2),)))], term)
        rec = {'n': 1, 'p': 0, 'r': {}, 's': {2: ENC}, 'succ': {(0, 4)}}
        after = {'n': 1, 'p': 0, 'r': {}, 's': {2: PLAIN}, 'succ': {(0, 5)}}
        sites = runvals.show_run_values(m.protos, {(m.root, 0, 3): rec, (m.root, 0, 4): after}, {m.helper})
        self.assertEqual(sites[0].shown, {})

    def test_probe_records_are_not_the_run(self):
        m = lift(MAIN_REGS, helper_into_regs())
        rec = {'n': 1, 'p': 1, 'r': {}, 's': {2: ENC}, 'succ': {(0, 4)}}
        after = {'n': 1, 'p': 0, 'r': {}, 's': {2: PLAIN}, 'succ': {(0, 5)}}
        sites = runvals.show_run_values(m.protos, {(m.root, 0, 3): rec, (m.root, 0, 4): after}, {m.helper})
        self.assertFalse(sites[0].ran)
        self.assertEqual(sites[0].shown, {})


if __name__ == '__main__':
    unittest.main()
