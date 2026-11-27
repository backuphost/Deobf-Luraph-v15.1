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
  helper is labelled as a helper of the build when the pipeline names it one, and the report has a
  note line.  The same for a helper whose result the program keeps in a register
  (`R[a] = helper(x)`: the call stays, the value is the run's).
* Nothing is put where the value differed between executions, where the call did not run, or where
  the call went on to more than one instruction; the run notes then say what is not shown and the
  result is PARTIAL.  A function that is not a helper keeps its call result.
"""
import json
import os
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
        self.assertIn('-- program %d: a helper of the build' % self.m.helper, text)
        self.assertIn('It writes into the variables of the code that calls it', text)
        self.assertEqual(len(notes), 1, notes)
        self.assertTrue(notes[0].startswith('[runvals] 1 call(s) of program %d, a helper of the build' % self.m.helper), notes)
        self.assertIn('the text shows the values they wrote as the run saw them (1 value(s))', notes[0])
        lines = run.run_report(notes)
        self.assertTrue(any(ln.startswith('  note: 1 call(s) of program %d, a helper of the build' % self.m.helper)
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
        self.assertRegex(text, r'local (\w+) = "%s"\n(?:.*\n)*.*print\(\1 \.\. " world"\)' % ENC)
        self.assertEqual(len(notes), 1, notes)
        self.assertTrue(notes[0].startswith('[runvals-missing] 1 call(s) of program %d, a helper of the build' % self.m.helper))
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
        self.assertIn('-- program %d: a helper of the build' % self.m.helper, text)
        self.assertTrue(notes[0].startswith('[runvals] 1 call(s) of program %d, a helper of the build' % self.m.helper), notes)
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
