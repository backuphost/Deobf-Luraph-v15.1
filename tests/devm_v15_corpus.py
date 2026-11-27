"""Checks for devm on the Luraph v15.0-v15.2 builds of the public sample corpus.  The v15.1 ones are the
v15.2 generation without wrapper keys: Luraph's start-up code on one interpreter (its main function
behind jump-register hubs and block-wise decryption), the script on a second one whose dispatch tree
computes every test into a local first.

    python3 tests/devm_v15_corpus.py            (from the repository root; seconds, needs bin/luau)

* the traced run's snapshots (`harness.lua` codeVersion / writeSnaps): a proto's own operand array seen
  through a lazy view of it (`P = setmetatable({[0] = K}, decode)`) is not recorded as a table shared by
  all protos, and a shared table carries its identity (its table id in the dump);
* `lifter.shared_snapshot_tables`: a shared entry is matched by that identity (also under another
  name), never to a proto's own code array, and entries of runs recorded without identities keep the
  old test;
* `lifter.hub_line`: the instructions without effect in front of a jump through a register belong to
  that jump (one copy per jump target), and nothing else does;
* a traced run that created no VM function ends in `run failed: the VM never started: ...`;
* a traced run that failed says why in its message (`vmmodel.harness_tail`: the harness's verdict and error
  lines, not its per-access trace lines), and `devm.run` prints that;
* the `--root` hint is given for a helper function (arithmetic only), not for every small script;
* `vmmodel.is_dispatch_loop`: `local OP = CODE[PC]; local T = OP >= 10; if T then` is a dispatch loop;
* `pe._classify_state_vars`: the open-upvalue map is found through a handler's local copy of it, and a
  dispatch test is not taken for it;
* `opaque._fold_call_keys`: Luraph's per-call key (a parameter that only comes back xor-ed as an extra
  first result) is removed where every caller is in view, and `run.report` does not say `complete`
  where it is left;
* `opaque._drop_blind_key_arith`, `_fold_type_tests`: key arithmetic on an unknown interpreter local
  that nothing reads, and the junk arm behind `type(0) == "buffer"`, are removed;
* `run.method_wrap_layout`: the v15.0 layout is recognised (devm refuses it at once).

    LURAPH_CORPUS=/path/to/obfuscator-samples/Luraph python3 tests/devm_v15_corpus.py Corpus

builds the model of the smallest v15.1 file of the corpus that is not a container (two interpreters)
and of the smallest v15.0 one (refused); with DEVM_V15_CORPUS_FULL=1 it also runs the v15.1 file through
the traced run and the lifter: the script is the first function on the second interpreter (minutes),
and Luraph's own main function lifts when asked for (`--root 1`; 10 minutes and more).
The corpus is not part of the repository; without LURAPH_CORPUS these checks are skipped.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.setrecursionlimit(200000)

from devm import lifter, pe, vmmodel  # noqa: E402

CORPUS = os.environ.get('LURAPH_CORPUS')


def have_luau():
    try:
        return bool(vmmodel.find_luau())
    except RuntimeError:
        return False


# Two protos of one interpreter.  Each has its operand array K and a lazy view P of it (key 0 holds
# the array); both hold the loader's table L, whose entry 6 is a table of numbers that instructions
# change in place.
SYNTHETIC = '''
local shared = {10, 20, 30}
local L = {[6] = shared}
local regs = {}
local K = {1, 2, 3}
local P = setmetatable({[0] = K}, {})
local pid = __LPH_PROTO(0, {"K", "P", "L"}, 3, K, P, L)
__LPH_STEP(pid, 0, 1, regs)
K[2] = 7
shared[1] = 11
__LPH_STEP(pid, 0, 2, regs)
local K2 = {4, 5, 6}
local P2 = setmetatable({[0] = K2}, {})
local pid2 = __LPH_PROTO(0, {"K", "P", "L"}, 3, K2, P2, L)
__LPH_STEP(pid2, 0, 1, regs)
K2[1] = 9
shared[2] = 21
__LPH_STEP(pid2, 0, 2, regs)
'''


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class Snapshots(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        sample = os.path.join(cls.tmp.name, 'sample.lua')
        with open(sample, 'w') as f:
            f.write(SYNTHETIC)
        out = os.path.join(cls.tmp.name, 'end.json')
        cls.proc = subprocess.run([vmmodel.find_luau(), os.path.join(ROOT, 'devm', 'harness.lua'), '-a', sample, out,
                                  'oracle=1', 'dumpend=1'], capture_output=True, text=True, errors='replace', timeout=120)
        cls.dump = cls.snaps = None
        if os.path.exists(out) and os.path.exists(out + '.snaps'):
            with open(out) as f:
                cls.dump = json.load(f)
            with open(out + '.snaps') as f:
                cls.snaps = json.load(f)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_own_array_is_not_shared(self):
        """Only L[6] is a shared table; K behind P[0] is followed per proto under its own name."""
        self.assertIsNotNone(self.snaps, self.proc.stderr[-2000:])
        shared = self.snaps['shared']
        self.assertEqual([(e['name'], e['key']) for e in shared], [('L', 6)])
        self.assertEqual(shared[0]['base'], {'1': 10, '2': 20})
        self.assertEqual(self.snaps['1']['names'], ['K'])
        self.assertEqual(self.snaps['1']['base'], [{'1': 1, '2': 2, '3': 3}])
        self.assertEqual(self.snaps['1']['vers'], [[[1, 2, 7]]])
        self.assertEqual(self.snaps['2']['vers'], [[[1, 1, 9]]])

    def test_shared_entry_names_its_table(self):
        """"tid" is the id the dump gives the table: the lifter finds exactly that table for both protos."""
        self.assertIsNotNone(self.snaps, self.proc.stderr[-2000:])
        ent = self.snaps['shared'][0]
        self.assertEqual(self.dump['tables'][str(ent['tid'])]['arr'], [11, 21, 30])
        d = pe.Dump(self.dump, None)
        records = d.proto_records()
        self.assertEqual(len(records), 2)
        for _interp, env in records:
            got = lifter.shared_snapshot_tables(env, self.snaps['shared'])
            self.assertEqual(len(got), 1)
            self.assertIs(got[0][0], env['L'].h[6])
            self.assertEqual(got[0][0].id, ent['tid'])


def table(tid, items=()):
    t = pe.LuaTable(tid)
    t.h.update(items)
    return t


class SharedSnapshotTables(unittest.TestCase):
    def test_identity_under_another_name(self):
        """Two interpreters hold the same loader table under different names (`Y[6]` here, `N[6]` there)."""
        shared = table(2, {i: i * 3 for i in range(1, 40)})
        env = {'N': table(10, {6: shared}), 'Y': table(11, {1: 5, 2: 6}), 'K': table(12, {1: 1})}
        ents = [{'name': 'N', 'key': 6, 'tid': 1, 'base': {'1': 7}}, {'name': 'Y', 'key': 6, 'tid': 2, 'base': {'3': 8}}]
        got = lifter.shared_snapshot_tables(env, ents)
        self.assertEqual([(t.id, b) for t, b in got], [(2, {'3': 8})])

    def test_identity_never_a_proto_variable(self):
        K = table(5, {1: 1, 2: 2})
        env = {'K': K, 'P': table(6, {0: K})}
        self.assertEqual(lifter.shared_snapshot_tables(env, [{'name': 'P', 'key': 0, 'tid': 5, 'base': {'1': 9}}]), [])

    def test_run_without_identities(self):
        """Old snapshot files: the name/key/coverage test stays, minus the proto's own arrays (every
        proto of the v15.1 corpus builds has its operand array under P[0])."""
        K = table(5, {i: i for i in range(1, 100)})
        other = table(7, {i: i for i in range(1, 100)})
        env = {'K': K, 'P': table(6, {0: K}), 'l': table(8, {6: other})}
        ents = [{'name': 'P', 'key': 0, 'base': {'1': 9, '2': 9}},        # another proto's base of ITS operand array
                {'name': 'l', 'key': 6, 'base': {'1': 9, '2': 9}},
                {'name': 'l', 'key': 6, 'base': {str(i): 1 for i in range(200, 240)}},     # keys of another table
                {'name': 'l', 'key': 6, 'base': {}}]
        got = lifter.shared_snapshot_tables(env, ents)
        self.assertEqual([(t.id, b) for t, b in got], [(7, {'1': 9, '2': 9})])


def node(key, target, stmts=(), kreads=(), error=None):
    n = lifter.Node(key)
    n.tree = (list(stmts), ('goto', target, {}))
    n.kreads = set(kreads)
    n.error = error
    return n


class HubLine(unittest.TestCase):
    def setUp(self):
        # (2, 57) .. (2, 59): retired instructions; (2, 60): pc = R44 + 1, lifted with R44 = 100
        self.nodes = {(2, 57): node((2, 57), (2, 58)), (2, 58): node((2, 58), (2, 59)), (2, 59): node((2, 59), (2, 60)),
                      (2, 60): node((2, 60), (2, 101), kreads={44}),
                      (2, 20): node((2, 20), (2, 57), stmts=[('set', ('r', 44), ('k', 100))])}
        self.node_in = {k: {44: 100} for k in self.nodes}

    def test_line_in_front_of_the_hub(self):
        indirect = {}
        self.assertEqual(lifter.hub_line(self.nodes, self.node_in, indirect, (2, 57)), 44)
        self.assertEqual(indirect, {(2, 57): 44, (2, 58): 44, (2, 59): 44, (2, 60): 44})

    def test_enters_the_line_anywhere(self):
        indirect = {(2, 60): 44}
        self.assertEqual(lifter.hub_line(self.nodes, self.node_in, indirect, (2, 59)), 44)
        self.assertEqual(indirect, {(2, 59): 44, (2, 60): 44})

    def test_jump_into_the_line_already_specialised(self):
        """The instruction in front of the hub was lifted when the hub was known: its jump points at
        the hub's copy for that target.  It still belongs to the line."""
        self.nodes[(2, 59)] = node((2, 59), (2, 60, 100))
        self.nodes[(2, 60, 100)] = node((2, 60, 100), (2, 101), kreads={44})
        self.node_in[(2, 60, 100)] = {44: 100}
        indirect = {(2, 60): 44}
        self.assertEqual(lifter.hub_line(self.nodes, self.node_in, indirect, (2, 58)), 44)
        self.assertEqual(indirect, {(2, 58): 44, (2, 59): 44, (2, 60): 44})

    def test_jump_into_the_copy_without_target(self):
        """The instruction in front of the hub was first reached on a path without the jump register:
        its jump points at the hub's copy for arrivals without a target, which carries the error.  A
        later arrival that has the register must still find the line."""
        self.nodes[(2, 59)] = node((2, 59), (2, 60, lifter.NO_TARGET))
        self.nodes[(2, 60, lifter.NO_TARGET)] = node((2, 60, lifter.NO_TARGET), (2, 60),
                                                     error='jump to a run-time computed instruction')
        indirect = {(2, 60): 44}
        self.assertEqual(lifter.hub_line(self.nodes, self.node_in, indirect, (2, 59)), 44)
        self.assertEqual(indirect, {(2, 59): 44, (2, 60): 44})
        indirect = {(2, 60): 44}
        self.assertEqual(lifter.hub_line(self.nodes, self.node_in, indirect, (2, 57)), 44)
        self.assertEqual(indirect, {(2, 57): 44, (2, 58): 44, (2, 59): 44, (2, 60): 44})

    def test_not_the_jump_itself_and_not_code(self):
        self.assertIsNone(lifter.hub_line(self.nodes, self.node_in, {}, (2, 60)))       # the caller's own case
        self.assertIsNone(lifter.hub_line(self.nodes, self.node_in, {}, (2, 20)))       # an instruction with an effect
        self.assertIsNone(lifter.hub_line(self.nodes, self.node_in, {}, (2, 99)))       # not lifted yet

    def test_line_must_end_in_a_register_jump(self):
        self.nodes[(2, 60)] = node((2, 60), (2, 61), stmts=[('set', ('r', 1), ('k', 2))])
        indirect = {}
        self.assertIsNone(lifter.hub_line(self.nodes, self.node_in, indirect, (2, 57)))
        self.assertEqual(indirect, {})
        # a jump whose register was not a number on the first arrival is no hub either
        self.nodes[(2, 60)] = node((2, 60), (2, 101), kreads={44})
        self.node_in[(2, 60)] = {}
        self.assertIsNone(lifter.hub_line(self.nodes, self.node_in, indirect, (2, 57)))

    def test_cycle_and_failed_instruction(self):
        self.nodes[(2, 59)] = node((2, 59), (2, 57))
        self.assertIsNone(lifter.hub_line(self.nodes, self.node_in, {}, (2, 57)))
        self.nodes[(2, 59)] = node((2, 59), (2, 60), error='register 11850 out of range')
        self.assertIsNone(lifter.hub_line(self.nodes, self.node_in, {}, (2, 57)))


class MissingTarget(unittest.TestCase):
    def test_jump_to_an_instruction_that_was_not_lifted(self):
        """The control-flow graph of a function gets a marked stub where a jump leads to an instruction
        the lifter did not produce (a KeyError in the structurer before: one such function among the
        169 of a corpus script stopped the whole result)."""
        from devm import ircfg
        pir = lifter.ProtoIR(36)
        n = lifter.Node((1, 1))
        n.tree = ([('set', ('r', 1), ('k', 5))], ('br', ('r', 1), ([], ('goto', (1, 2), {})), ([], ('ret', [], None))))
        pir.nodes[(1, 1)] = n
        pir.entry = (1, 1)
        cfg = ircfg.ProtoCFG(pir)
        self.assertTrue(any(st[0] == 'error' for b in cfg.blocks.values() for st in b.stmts))


class RootHint(unittest.TestCase):
    """The hint `the script is probably the first program; run again with --root N` is for a function
    on the second interpreter that is a helper of the build (arithmetic), not for a small script."""

    def protos(self, *stmts):
        from devm import pipeline
        pir = lifter.ProtoIR(34)
        n = lifter.Node((0, 1))
        n.tree = ([], ('br', ('t', 'c'), (list(stmts), ('goto', (0, 2), {})), ([], ('ret', [], None))))
        pir.nodes[(0, 1)] = n
        return pipeline._calls_out({34: pir})

    def test_small_script_calls_out(self):
        self.assertTrue(self.protos(('call', 'c1', ('t', 'P1_1'), [('k', 'hii')], None, 0)))
        self.assertTrue(self.protos(('call', 'c1', ('kfn', 'print'), [('k', 'hii')], None, 0)))

    def test_helper_is_arithmetic(self):
        self.assertFalse(self.protos(('call', 'c1', ('kfn', 'bit32.bxor'), [('r', 1), ('k', 5)], None, 1),
                                     ('set', ('r', 2), ('mv', 'c1', 1))))
        self.assertFalse(self.protos())


LAYER3 = os.path.join(ROOT, 'examples', 'layer3.lua')


@unittest.skipUnless(os.path.exists(LAYER3), 'needs examples/layer3.lua')
class VmNeverStarted(unittest.TestCase):
    def test_no_protos_is_a_message(self):
        """A traced run in which no VM function was created (a chunk without its buffer) ends with one
        line that says so, not with an IndexError."""
        from devm import normalize, pipeline
        with open(LAYER3, encoding='latin-1') as f:
            src = f.read()
        src, _n = normalize.inline_protected_raw(src)
        stage = pipeline.Stage(normalize.reflow(src), tempfile.gettempdir(), 'never', log=lambda *a: None, oneline=True)
        how = "script stopped: sample:3: invalid argument #1 to 'readu8' (buffer expected, got nil) after 0 instructions"
        keep = vmmodel.run_oracle
        vmmodel.run_oracle = lambda *a, **k: ({'root': {'t': 1}, 'protos': [], 'tables': {}}, {}, ['[oracle] ' + how])
        try:
            with self.assertRaises(RuntimeError) as cm:
                stage.run_traced()
        finally:
            vmmodel.run_oracle = keep
        self.assertTrue(str(cm.exception).startswith('run failed: the VM never started: ' + how), str(cm.exception))
        self.assertIn('--chunk-arg', str(cm.exception))


class RunFailedMessage(unittest.TestCase):
    """A traced run that failed: the message carries the end of the harness output that says why (its
    verdict and error lines), not the per-access trace lines of the run, and devm.run prints it."""
    STDERR = '\n'.join(['[global] Rect', '[global] TweenInfo', '[call] Instance.new("Frame")'] * 800 + [
        '[script] print: ' + 'x' * 300,
        '[oracle] script stopped: sample:3: attempt to call a nil value after 4096 instructions',
        'NOT REACHED: the script ended without entering the VM hook',
        '/w/devm/harness.lua:2178: hook not reached', '', 'stacktrace:', '[C] function error',
        '/w/devm/harness.lua:2178', ''])

    def test_tail(self):
        tail = vmmodel.harness_tail(self.STDERR)
        self.assertNotIn('[global]', tail)
        self.assertNotIn('[call]', tail)
        self.assertNotIn('[C] function', tail)
        self.assertIn('[oracle] script stopped: sample:3: attempt to call a nil value after 4096 instructions', tail)
        self.assertTrue(tail.endswith('/w/devm/harness.lua:2178: hook not reached'), tail)
        self.assertLessEqual(len(tail), 700)
        self.assertEqual(vmmodel.harness_tail(''), '(no output)')
        # (a verdict line far up is kept, the trace after it is not)
        self.assertEqual(vmmodel.harness_tail('[budget] stopped after 9 VM instructions\n' + '[global] a\n' * 50),
                         '[budget] stopped after 9 VM instructions')

    def test_main_prints_it(self):
        import contextlib
        import io
        from devm import run
        keep = run.deobfuscate

        def fail(*a, **k):
            raise RuntimeError('oracle run failed: ' + vmmodel.harness_tail(self.STDERR))
        run.deobfuscate = fail
        out = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
                src = os.path.join(d, 'x.lua')
                with open(src, 'w') as f:
                    f.write('return 1\n')
                with self.assertRaises(SystemExit) as cm:
                    run.main([src, '-o', os.path.join(d, 'x.out.lua'), '--work', os.path.join(d, 'w')])
        finally:
            run.deobfuscate = keep
        self.assertEqual(cm.exception.code, 1)
        line = out.getvalue().strip().split('\n')[-1]
        self.assertTrue(line.startswith('devm: run failed: oracle run failed: '), line)
        self.assertIn('attempt to call a nil value after 4096 instructions', line)
        self.assertIn('hook not reached', line)
        self.assertNotIn('[global]', line)


class OutlinedDispatch(unittest.TestCase):
    """The second interpreter of the v15.1 corpus builds computes every test of its dispatch tree into
    a local first.  The model did not take its loops for dispatch loops, so the interpreter that runs
    the script was never hooked and no function of the script was seen."""
    LOOP = 'while true do local V=S[H];\nlocal j=V>=10;\nif j then local j=V>=15;\nif j then x=1 else x=2 end else x=3 end;\nH+=1;\nend'

    def loop(self, text):
        from devm import luparse
        return luparse.parse(text)[0][0]

    def test_tests_through_a_local(self):
        self.assertTrue(vmmodel.is_dispatch_loop(self.loop(self.LOOP)))
        self.assertTrue(vmmodel.is_dispatch_loop(self.loop(self.LOOP.replace('V>=10', '10<=V'))))
        m = vmmodel.LOOP_HEAD_RE.search(self.LOOP)
        self.assertEqual(self.LOOP[m.end(1):m.end(1) + 13], 'local V=S[H];')
        # the usual form
        usual = 'while true do local V=S[H];\nif V>=10 then x=1 else x=3 end;\nH+=1;\nend'
        self.assertTrue(vmmodel.is_dispatch_loop(self.loop(usual)))
        self.assertTrue(vmmodel.LOOP_HEAD_RE.search(usual))

    def test_marks_the_generation(self):
        """run.py switches the structuring passes of the v15.2 generation on for these builds too."""
        import types
        from devm import run
        usual = 'while true do local V=S[H];\nif V>=10 then x=1 else x=3 end;\nH+=1;\nend'
        late = 'while true do V=S[H];\nif V>=10 then x=1 else x=3 end;\nH+=1;\nend'
        model = types.SimpleNamespace(keyed_wrappers=False, loops=[self.loop(usual), self.loop(late), self.loop(self.LOOP)])
        self.assertTrue(run.v152_generation(model))
        model.loops.pop()
        self.assertFalse(run.v152_generation(model))
        model.keyed_wrappers = True
        self.assertTrue(run.v152_generation(model))

    def test_other_loops_are_not(self):
        for body in ('local V=S[H];\nlocal j=W>=10;\nif j then x=1 end',         # the test is not of the opcode
                     'local V=S[H];\nlocal j=V>=10;\nif k then x=1 end',         # the `if` is not on that local
                     'local V=S[H];\nlocal j=V+10;\nif j then x=1 end',          # not a comparison
                     'local V=S[H];\nlocal j=V>=10;\nx=1'):
            self.assertFalse(vmmodel.is_dispatch_loop(self.loop('while true do ' + body + ' end')), body)


class OpenUpvalueMap(unittest.TestCase):
    """pe._classify_state_vars names the interpreter-level variable that holds the open-upvalue map.
    The interpreter that runs the script in the v15.1 corpus builds tests and indexes it through a
    handler-local copy whose name is often also the name of an unrelated interpreter variable (the
    dispatch mode in one file: the lifter then could not even enter the interpreter), and its dispatch
    tests look like the pattern too."""
    CLOSE = 'if %s then local T=%s[W];\nif T then T[4]=T;\nT[5]=R[W];\nT[6]=5;\n%s[W]=nil;\nend;\nend;\n'

    def openups(self, body):
        from devm import luparse
        loop = luparse.parse('while true do ' + body + ' end')[0][0]
        return pe.PE._classify_state_vars(pe.PE.__new__(pe.PE), [loop])[2]

    def test_interpreter_variable(self):
        self.assertEqual(self.openups('local V=S[H];\nif V>=10 then local W=A[H];\n' + self.CLOSE % ('Y', 'Y', 'Y') + 'end;\nH+=1;'), {'Y'})

    def test_through_a_local_copy(self):
        body = ('local V=S[H];\nlocal j=V>=10;\nif j then local j=V>=15;\nx[6]=4;\nx[5]=nil;\n'       # a dispatch test, not the map
                'else local C,W=e,A[H];\n' + self.CLOSE % ('C', 'C', 'C') + 'end;\nH+=1;')
        self.assertEqual(self.openups(body), {'e'})
        # a copy of a copy, and a local with a value of its own
        body = 'local V=S[H];\nlocal C,W=e,A[H];\ndo local D=C;\n' + self.CLOSE % ('D', 'D', 'D') + 'end;\nlocal Q=A[H];\n' + self.CLOSE % ('Q', 'Q', 'Q')
        self.assertEqual(self.openups(body), {'e'})

    def test_test_stored_by_assignment(self):
        self.assertEqual(self.openups('local V=S[H];\nW=V==208;\nif W then local T=1;\nx[6]=4;\nx[5]=nil;\nend;'), set())


class CallKeys(unittest.TestCase):
    """opaque._fold_call_keys on text as the structurer writes it (synthetic shapes and numbers)."""

    def test_function_called_on_the_spot(self):
        from devm import opaque
        # (the key is the last parameter here; 161803398 ^ 271828182 == 429336656)
        text = ('local v4 = (function(p1, p2)\n\twarn("status " .. p1)\n\treturn bit32.bxor(p2, 271828182)\n'
                'end)("ready", 161803398)\nif v4 == 429336656 then\n\treturn\nend\nlocal v5 = bit32.bxor(v4, 141421356)')
        self.assertEqual(opaque.strip(text), '(function(p1)\n\twarn("status " .. p1)\nend)("ready")')
        # behind another statement the call needs its `;`, and a test that does not hold is dropped
        text2 = 'print(1)\n' + text.replace('429336656', '429336657') + '\nprint(2)'
        self.assertEqual(opaque.strip(text2), 'print(1)\n;(function(p1)\n\twarn("status " .. p1)\nend)("ready")\nprint(2)')

    # (the key is the first parameter; 31415926 ^ 192837465 == 178333999, 14142135 ^ 192837465 == 195673582)
    NAMED = ('local v8 = function(p3, p4, p5)\n\tlocal v9 = p4[p5]\n\tif v9 == nil then\n\t\treturn bit32.bxor(p3, 192837465)\n\tend\n'
             '\treturn bit32.bxor(p3, 192837465), v9\nend\n'
             'local v10, v11 = v8(31415926, cfg, function(p6)\n\tprint(p6)\nend)\n'
             'if v10 ~= 178333999 then\n\terror("bad")\nend\nprint(v11)\n'
             'v8(27182818, cfg, "mode")\n'
             'while true do\n\tlocal v12 = v8(14142135, cfg, "size")\n\tif v12 == 195673582 then\n\t\tbreak\n\tend\n\tprint("never")\nend\nprint("after")')

    def test_function_called_by_name(self):
        from devm import opaque
        self.assertEqual(opaque.strip(self.NAMED), (
            'local v8 = function(p4, p5)\n\tlocal v9 = p4[p5]\n\tif v9 == nil then\n\t\treturn\n\tend\n\treturn v9\nend\n'
            'local v11 = v8(cfg, function(p6)\n\tprint(p6)\nend)\nprint(v11)\n'
            'v8(cfg, "mode")\n'
            'while true do\n\tv8(cfg, "size")\n\tbreak\nend\nprint("after")'))

    def test_left_alone(self):
        from devm import opaque
        for text in (self.NAMED.replace('v8(27182818, cfg, "mode")', 'return v8(27182818, cfg, "mode")'),     # the result travels on
                     self.NAMED.replace('v8(27182818, cfg, "mode")', 'v8(key, cfg, "mode")'),                 # not a literal
                     self.NAMED.replace('v8(27182818, cfg, "mode")', 'keep(v8)'),                             # the function travels
                     self.NAMED.replace('\tlocal v9 = p4[p5]', '\tlocal v9 = p4[p5 + p3]'),                 # the key is used
                     self.NAMED.replace('\t\treturn bit32.bxor(p3, 192837465)\n', '\t\treturn\n')):       # a way out without it
            self.assertEqual(opaque._fold_call_keys(text), text)

    def test_report(self):
        from devm import run
        left = 'local v8 = function(p3, p4)\n\tlocal v9 = p4.x\n\treturn bit32.bxor(p3, 192837465), v9\nend\nprint(v8)\n'
        lines = run.report(left)
        self.assertEqual(lines[0], 'result: PARTIAL')
        self.assertTrue(any('per-call' in ln for ln in lines[1:]), lines)
        self.assertTrue(run.report('print("x")\n')[0].startswith('result: complete'))

    def test_report_key_arithmetic_left(self):
        from devm import run
        text = 'local v3 = g()\nlocal v4 = bit32.band(v3, 255) % 4294967296\nlocal v5 = v4 - v4 % 1 + 246813579\nprint(1)\n'
        lines = run.report(text)
        self.assertEqual(lines[0], 'result: PARTIAL')
        self.assertTrue(any('key arithmetic' in ln for ln in lines[1:]), lines)
        self.assertTrue(run.report('local x = y - y % 1\nprint(x)\n')[0].startswith('result: complete'))      # the script's own names

    def test_report_of_a_run_that_returned(self):
        from devm import run
        text = 'local a = (nil)[1]\nlocal b = (nil)[2]\nprint(1)\n'
        self.assertTrue(any('most of the script did not run' in ln for ln in run.report(text)))
        got = run.report(text, returned=True)
        self.assertFalse(any('most of the script did not run' in ln for ln in got), got)
        self.assertTrue(any('went to the script\'s end' in ln for ln in got), got)
        head = '-- x.lua, %s.  How the run went and what this file lacks:\n--   traced run: the script returned (5 VM instructions)\n--   result: PARTIAL\n' % run.HEAD_MARK
        self.assertEqual(run.report(head + text), got)


class StartupOnly(unittest.TestCase):
    """The factory route lifts the VM's first program when the run made no program on another
    interpreter: Luraph's start-up code, perhaps with the script's top level in it.  Never complete."""

    def test_note(self):
        from devm import pipeline
        ran = ['[oracle] script returned after 900 instructions']
        n = pipeline.startup_note(ran + ['[script] print: hi', '[http] GET https://example.invalid/x (no network) [step 5]'], None, 1, 3)
        self.assertTrue(n.startswith('[startup] the run created no program on an interpreter other than the first one (3 modelled)'), n)
        self.assertIn('top level ran inside that program (the run shows 1 line(s) of output, 1 request(s) from it)', n)
        n = pipeline.startup_note(ran + ['[script] undefined globals read (nil, as in Roblox): x'], None, 1, 2)
        self.assertIn('shows nothing the script did', n)
        n = pipeline.startup_note(['[oracle] script stopped: boom after 5 instructions'], None, 1, 2)
        self.assertIn('stopped before the script showed anything', n)
        self.assertTrue(pipeline.startup_note(ran, 1, 1, 2).startswith('[startup] --root 1:'))

    def test_report(self):
        from devm import pipeline, run
        notes = ['[oracle] script returned after 900 instructions', pipeline.startup_note(['[script] print: hi'], None, 1, 2)]
        lines = run.report('print("hi")\n', notes=notes)
        self.assertEqual(lines[0], 'result: PARTIAL')
        self.assertTrue(any('start-up code' in ln for ln in lines[1:]), lines)
        rr = run.run_report(notes)
        self.assertTrue(any(ln.startswith(run.STARTUP_LINE) for ln in rr), rr)
        head = ['-- x.lua, %s.  How the run went and what this file lacks:' % run.HEAD_MARK] + ['--   ' + ln for ln in rr + lines]
        self.assertEqual(run.report('\n'.join(head) + '\nprint("hi")\n'), lines)


class HiddenErrors(unittest.TestCase):
    """Factory route: instructions that did not lift and have no marker in the text."""

    def test_count(self):
        from devm import run
        oracle = {(1, 1, 10): {}, (1, 1, 11): {}}
        errors = [(1, ((1, 10), 'bad A')), (1, ((1, 11), 'bad B')), (1, ((1, 12), 'bad B')), (2, ((1, 3), 'bad C'))]
        # nothing marked: two errors in code that ran, two in code that never did
        self.assertEqual(run.hidden_errors(errors, oracle, 'print(1)'), (2, 2))
        # one marker of B: which of the two it is is not known, the one that ran counts as hidden
        self.assertEqual(run.hidden_errors(errors, oracle, '-- LIFT ERROR: bad A\n-- LIFT ERROR: bad B\n'), (1, 1))
        self.assertEqual(run.hidden_errors(errors, oracle, '-- LIFT ERROR: bad A\n-- LIFT ERROR: bad B\n-- LIFT ERROR: bad B\n'), (0, 1))
        self.assertEqual(run.hidden_errors(errors, oracle, '-- LIFT ERROR: bad A\n-- LIFT ERROR: bad B\n-- LIFT ERROR: bad B\n'
                                           '-- LIFT ERROR: bad C\n'), (0, 0))

    def test_run_report(self):
        from devm import run
        self.assertEqual(run.run_report(['[cut] 3 instruction(s) that never ran did not lift']),
                         ['  note: 3 instruction(s) that never ran did not lift'])

    def test_head_line(self):
        """'up to N ... may have no marker' where errors with one message are marked only in part; the
        head line lists only messages of executed errors that may have no marker, and is not counted
        as one more marker (the result stays PARTIAL)."""
        from devm import run
        oracle = {(1, 1, 10): {}, (1, 1, 11): {}}
        errors = [(1, ((1, 10), 'bad A')), (1, ((1, 11), 'bad B')), (1, ((1, 12), 'bad B')), (2, ((1, 3), 'bad C'))]
        # nothing marked: exactly the two executed errors have none
        head, cold = run.hidden_head(errors, oracle, 'print(1)\n')
        self.assertEqual(cold, 2)
        self.assertTrue(head.startswith('-- LIFT ERROR: 2 instruction(s) that the traced run executed did not lift and have '
                                        'no marker in the text below.'), head)
        self.assertTrue(head.endswith('Their lift errors: bad A; bad B'), head)
        # A marked, B not: the executed B has none for sure
        body = 'print(1)\n-- LIFT ERROR: bad A\n'
        head, _ = run.hidden_head(errors, oracle, body)
        self.assertTrue(head.startswith('-- LIFT ERROR: 1 instruction(s) that the traced run executed'), head)
        self.assertTrue(head.endswith('Their lift errors: bad B'), head)
        # one marker of B for one executed and one cut B: which one has it is not known
        body = 'print(1)\n-- LIFT ERROR: bad A\n-- LIFT ERROR: bad B\n'
        head, cold = run.hidden_head(errors, oracle, body)
        self.assertEqual(cold, 1)       # (C; the B without a marker is counted as the executed one)
        self.assertTrue(head.startswith('-- LIFT ERROR: up to 1 instruction(s) that the traced run executed did not lift and '
                                        'may have no marker in the text below (possibly none:'), head)
        self.assertTrue(head.endswith('Their lift errors: bad B'), head)
        self.assertEqual(run.hidden_head(errors, oracle, body + '-- LIFT ERROR: bad B\n')[0], None)
        rep = run.report(head + '\n' + body)
        self.assertEqual(rep[0], 'result: PARTIAL')
        self.assertIn('  2 LIFT ERROR markers: an instruction could not be lifted; the code after it is missing', rep)
        self.assertTrue(any(ln.startswith('  up to 1 instruction(s) that the traced run executed did not lift and may have no '
                                          'LIFT ERROR marker') for ln in rep), rep)
        # a head line alone: PARTIAL, no marker count
        head, _ = run.hidden_head(errors, oracle, 'print(1)\n')
        rep = run.report(head + '\nprint(1)\n')
        self.assertEqual(rep[0], 'result: PARTIAL')
        self.assertFalse(any('LIFT ERROR markers' in ln for ln in rep), rep)
        self.assertTrue(any(ln.startswith('  2 instruction(s) that the traced run executed did not lift and have no LIFT '
                                          'ERROR marker') for ln in rep), rep)


class RootHintReport(unittest.TestCase):
    """pipeline's hint that the proto taken for the script is a helper of the build makes the result
    PARTIAL: it may not be the script."""
    HINT = ('[hint] the function taken for the script (proto 9, on the second interpreter) has only 12 instructions, while '
            'the first program executed 25000: the script is probably the first program; run again with --root 1')

    def test_partial(self):
        from devm import run
        notes = ['[oracle] script returned after 25012 instructions', self.HINT]
        lines = run.report('print(1)\n', notes=notes)
        self.assertEqual(lines[0], 'result: PARTIAL')
        self.assertTrue(any('may not be the script' in ln and '--root 1' in ln for ln in lines[1:]), lines)
        self.assertTrue(run.report('print(1)\n', notes=notes[:1])[0].startswith('result: complete'))
        # read off the head of a file main() wrote
        rr = run.run_report(notes)
        self.assertTrue(any(ln.startswith(run.HINT_LINE) for ln in rr), rr)
        head = ['-- x.lua, %s.  How the run went and what this file lacks:' % run.HEAD_MARK] + ['--   ' + ln for ln in rr + lines]
        self.assertEqual(run.report('\n'.join(head) + '\nprint(1)\n'), lines)


class BlindKeyArithmetic(unittest.TestCase):
    """What the interpreter that runs the script leaves in a v15.1 result: key arithmetic on an
    interpreter local the lifter has no value for, and junk arms behind `type(<number>) == "buffer"`."""
    TEXT = ('local v1 = f()\n'
            'local v2 = (73519286 + nil) % 4294967296\nlocal v3 = v2 - v2 % 1\nlocal v4 = bit32.bxor(v3, 55)\nprint(v4)\n'   # read by the script
            'local v5 = nil --[[X]] % 4294967296\nlocal v6 = v5 + v1\n'             # mixed with a real value
            'local v7 = (nil + 5) % 7\n'                                             # no large literal: not key arithmetic
            'local v8 = nil --[[OPENUPS]] % 4294967296\nlocal v9 = bit32.bxor(v8 - v8 % 1, 398765431)\n'
            'if type(0) == "buffer" then\n\tlocal v10 = (73519286 + nil) % 4294967296\n\tprint(v10)\nend\n'
            'if type(0) ~= "number" then\n\tprint(1)\nelseif type(5) == "number" then\n\tprint(2)\nend\n'
            'if type(x) == "number" then\n\tprint(3)\nend')

    def test_chain_nothing_reads(self):
        from devm import opaque
        self.assertEqual(opaque.strip(self.TEXT), (
            'local v1 = f()\n'
            'local v2 = (73519286 + nil) % 4294967296\nlocal v3 = v2 - v2 % 1\nlocal v4 = bit32.bxor(v3, 55)\nprint(v4)\n'
            'local v5 = nil --[[X]] % 4294967296\nlocal v6 = v5 + v1\n'
            'local v7 = (nil + 5) % 7\n'
            'print(2)\n'
            'if type(x) == "number" then\n\tprint(3)\nend'))

# The v15.0 shape: the function that makes a VM closure is a method of the VM object and a state machine.
V150 = '''return setmetatable({m=function(v,R,R,R,g,g)local a,I,C=nil,0,R[1];
while true do if I<=0 then I,a=1,function(...)local pc,r=1,{...};
while true do local op=C[pc];
if op<=1 then r[1]=op;
else return r[1];
end;
pc+=1;
end;
end;
else return a;
end;
end;
end},{}):m()(...);
'''


class MethodWrap(unittest.TestCase):
    def test_v150_layout(self):
        from devm import run
        self.assertTrue(run.method_wrap_layout(vmmodel.Model(V150, oneline=True)))
        # the same VM without the v15 object around it, and one whose maker returns the closure
        self.assertFalse(run.method_wrap_layout(vmmodel.Model(V150.replace('return setmetatable({', 'return({').replace('},{}):m', '}):m'), oneline=True)))

    def test_factory_layouts_are_not(self):
        from devm import normalize, run
        if not os.path.exists(LAYER3):
            self.skipTest('needs examples/layer3.lua')
        with open(LAYER3, encoding='latin-1') as f:
            src = f.read()
        src, _n = normalize.inline_protected_raw(src)
        self.assertFalse(run.method_wrap_layout(vmmodel.Model(normalize.reflow(src), oneline=True)))


def corpus_file(version='v15.1'):
    """The smallest file of <corpus>/<version> that is a VM object itself (not a compressed container)."""
    d = os.path.join(CORPUS or '', version)
    if not os.path.isdir(d):
        return None
    from devm import container
    for p in sorted((os.path.join(d, name) for name in os.listdir(d)), key=os.path.getsize):
        with open(p, encoding='latin-1') as f:
            src = f.read()
        if 'setmetatable(' in src[:4096] and not container.may_be_container(src):
            return p
    return None


def corpus_source(path):
    from devm import normalize
    with open(path, encoding='latin-1') as f:
        src = f.read()
    src, _n = normalize.inline_protected_raw(src)
    return normalize.reflow(src)


@unittest.skipUnless(CORPUS, 'set LURAPH_CORPUS')
class Corpus(unittest.TestCase):
    def test_models(self):
        """v15.1: start-up code and script each have their interpreter.  v15.0: refused at once."""
        from devm import run
        path = corpus_file()
        if path is None:
            self.skipTest('no v15.1 VM file under LURAPH_CORPUS')
        model = vmmodel.Model(corpus_source(path), oneline=True)
        self.assertTrue(model.factory_style)
        self.assertGreaterEqual(len(model.interps), 2)
        self.assertFalse(run.method_wrap_layout(model))
        path = corpus_file('v15.0')
        if path is not None:
            self.assertTrue(run.method_wrap_layout(vmmodel.Model(corpus_source(path), oneline=True)))

    @unittest.skipUnless(os.environ.get('DEVM_V15_CORPUS_FULL') and have_luau(), 'set DEVM_V15_CORPUS_FULL=1 (minutes)')
    def test_script_is_the_payload(self):
        """The traced run registers the script's functions (they run on the second interpreter) and the
        lifter takes the first of them as the script; nothing that ran is left unlifted."""
        from devm import pipeline
        path = corpus_file()
        if path is None:
            self.skipTest('no v15.1 VM file under LURAPH_CORPUS')
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            stage = pipeline.Stage(corpus_source(path), tmp, 'sample', log=logs.append, oneline=True)
            stage.force_root = None
            stage.harness_options = ['exercise=1', 'executor=1']
            try:
                text = stage.run_traced()
            finally:
                if vmmodel.SERVER is not None:
                    vmmodel.SERVER.close()
        self.assertTrue(any('[oracle] script' in ln for ln in logs), logs[-6:])
        self.assertIsNotNone(stage.payload_root, logs[-6:])
        self.assertGreater(stage.payload_root, 1)
        self.assertFalse([e for k, e in stage.errors if (k, e[0][0], e[0][1]) in stage.oracle], 'unlifted instructions that ran')
        self.assertNotIn('LIFT ERROR', text)
        self.assertTrue(text.strip())

    @unittest.skipUnless(os.environ.get('DEVM_V15_CORPUS_FULL') and have_luau(), 'set DEVM_V15_CORPUS_FULL=1 (10 minutes and more)')
    def test_main_function_is_lifted(self):
        """Asked for Luraph's own first program (--root 1): the traced run returns, its snapshots name
        no proto's own array as shared, and the lifter reaches the instructions the main function
        executed (it reached 8 to 119 of 8,000 to 11,000)."""
        from devm import pipeline
        path = corpus_file()
        if path is None:
            self.skipTest('no v15.1 VM file under LURAPH_CORPUS')
        src = corpus_source(path)
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            stage = pipeline.Stage(src, tmp, 'sample', log=logs.append, oneline=True)
            self.assertTrue(stage.model.factory_style)
            self.assertFalse(stage.model.keyed_wrappers)
            stage.force_root = 1
            stage.harness_options = ['exercise=1', 'executor=1']

            class NoText:        # (the lift is what is checked here; structuring takes as long again)
                def __init__(self, *a, **k):
                    self.stats = {'unstructured': 0}

                def run(self):
                    return ''
            from devm import structure
            keep = structure.Decompiler
            structure.Decompiler = NoText
            try:
                stage.run_traced()
            finally:
                structure.Decompiler = keep
                if vmmodel.SERVER is not None:
                    vmmodel.SERVER.close()
            shared = (stage.dump.data.get('snaps') or {}).get('shared') or []
        self.assertTrue(any('[oracle] script' in ln for ln in logs), logs[-6:])
        self.assertTrue(all('tid' in e for e in shared))
        root = stage.lifter.root_key
        env = stage.lifter.records[root - 1][1]
        own = {v.id for v in env.values() if isinstance(v, pe.LuaTable)}
        self.assertFalse([e for e in shared if e['tid'] in own])
        ran = {(k[1], k[2]) for k in stage.oracle if k[0] == root and not stage.oracle[k].get('p')}
        reached = {(k[0], k[1]) for k in stage.protos[root].nodes}
        self.assertGreater(len(ran), 1000)
        self.assertGreater(len(ran & reached), 0.95 * len(ran), '%d of %d executed instructions of the main function lifted' % (
            len(ran & reached), len(ran)))


if __name__ == '__main__':
    unittest.main()
