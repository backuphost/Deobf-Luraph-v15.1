"""Checks for devm on the Luraph v15.1 builds of the public sample corpus (the v15.2 generation without
wrapper keys: one interpreter, the main function behind jump-register hubs and block-wise decryption).

    python3 tests/devm_v15_corpus.py            (from the repository root; seconds, needs bin/luau)

* the traced run's snapshots (`harness.lua` codeVersion / writeSnaps): a proto's own operand array seen
  through a lazy view of it (`P = setmetatable({[0] = K}, decode)`) is not recorded as a table shared by
  all protos, and a shared table carries its identity (its table id in the dump);
* `lifter.shared_snapshot_tables`: a shared entry is matched by that identity (also under another
  name), never to a proto's own code array, and entries of runs recorded without identities keep the
  old test;
* `lifter.hub_line`: the instructions without effect in front of a jump through a register belong to
  that jump (one copy per jump target), and nothing else does.

    LURAPH_CORPUS=/path/to/obfuscator-samples/Luraph DEVM_V15_CORPUS_FULL=1 python3 tests/devm_v15_corpus.py Corpus

runs the smallest v15.1 file of the corpus that is not a container through the traced run and the
lifter (10 minutes and more) and checks that the instructions the main function executed are lifted.
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


def corpus_file():
    """The smallest file of <corpus>/v15.1 that is a VM object itself (not a compressed container)."""
    d = os.path.join(CORPUS or '', 'v15.1')
    if not os.path.isdir(d):
        return None
    best = None
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        with open(p, encoding='latin-1') as f:
            head = f.read(4096)
        if 'setmetatable(' not in head or 'return(function()local' in head.replace(' ', ''):
            continue
        if best is None or os.path.getsize(p) < os.path.getsize(best):
            best = p
    return best


@unittest.skipUnless(CORPUS and os.environ.get('DEVM_V15_CORPUS_FULL') and have_luau(),
                     'set LURAPH_CORPUS and DEVM_V15_CORPUS_FULL=1 (10 minutes and more)')
class Corpus(unittest.TestCase):
    def test_main_function_is_lifted(self):
        """The traced run returns, its snapshots name no proto's own array as shared, and the lifter
        reaches the instructions the main function executed (it reached 8 to 119 of 8,000 to 11,000)."""
        from devm import normalize, pipeline
        path = corpus_file()
        if path is None:
            self.skipTest('no v15.1 VM file under LURAPH_CORPUS')
        with open(path, encoding='latin-1') as f:
            src = f.read()
        src, _n = normalize.inline_protected_raw(src)
        src = normalize.reflow(src)
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            stage = pipeline.Stage(src, tmp, 'sample', log=logs.append, oneline=True)
            self.assertTrue(stage.model.factory_style)
            self.assertFalse(stage.model.keyed_wrappers)
            stage.force_root = None
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
