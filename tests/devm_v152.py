"""Checks for devm's Luraph v15.2 support that run in seconds (no full devirtualization).

    python3 tests/devm_v152.py            (from the repository root; needs bin/luau and bin/luau-ast)

* the compressed container of examples/v15.2/sample.luau is recognised and unwrapped;
* the model of the VM chunk has all 17 dispatch loops in 4 interpreters, and knows which wrapper
  parameters of the interpreter that runs carry per-closure numbers;
* the instrumented copies of the v15.2 chunk and of examples/layer3.lua (v15.1, has
  `function a.b()` statements) parse;
* the harness runs a chunk through a carrier: loadstring substitution, chunk name, arguments;
* `structure.fit_scopes` keeps a function with hundreds of locals under the limit with sections
  side by side, and every use stays inside the scope of its declaration;
* the v15.2-only rewrites (`beautify.copy_diamonds`, `sink_common`, nested table layout) do what
  they say, leave alone what they must, and are off unless run.py switches them on;
* the report: the VM table gets a name, a marker's own text is not counted twice, and the report
  that main() writes at the head of a result is not taken for part of it.

    DEVM_V152_FULL=1 python3 tests/devm_v152.py V152Full Layer3Full     (about 2 minutes each)

run the whole pipeline on the sample and on examples/layer3.lua + blob4.bin and check what the
results must contain (the loader's checks and its key-server request; the v15.1 script's plain
Lua), that they parse and that the flattened main function came out as structured code.
"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.setrecursionlimit(100000)

from devm import container, normalize, structure, vmmodel  # noqa: E402

SAMPLE = os.path.join(ROOT, 'examples', 'v15.2', 'sample.luau')
LAYER3 = os.path.join(ROOT, 'examples', 'layer3.lua')
LUAU_AST = os.path.join(ROOT, 'bin', 'luau-ast')


def have_luau():
    try:
        return bool(vmmodel.find_luau())
    except RuntimeError:
        return False


def parses(path):
    out = subprocess.run([LUAU_AST, path], capture_output=True, text=True, errors='replace').stdout
    return 'Parse errors' not in out[:4000]


def model_of(text):
    text, _ = normalize.inline_protected_raw(text)
    return vmmodel.Model(normalize.reflow(text), oneline=True)


@unittest.skipUnless(have_luau() and os.path.exists(SAMPLE), 'needs bin/luau and the v15.2 sample')
class V152(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        with open(SAMPLE, encoding='latin-1') as f:
            cls.src = f.read()
        cls.carrier = container.unwrap(SAMPLE, cls.tmp.name, 'sample', log=lambda *a: None)
        with open(cls.carrier.chunk_src, encoding='latin-1') as f:
            cls.chunk = f.read()
        cls.model = model_of(cls.chunk)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_container(self):
        self.assertTrue(container.may_be_container(self.src))
        self.assertEqual(self.carrier.chunk_name, 'Luraph')
        self.assertEqual(len(self.chunk), 238585)
        self.assertEqual(os.path.getsize(self.carrier.chunk_arg), 782283)
        self.assertFalse(container.may_be_container(self.chunk))

    def test_model(self):
        m = self.model
        self.assertEqual([len(it.loops) for it in m.interps], [4, 6, 3, 4])
        self.assertEqual(len(m.loop_positions()), 17)
        self.assertTrue(m.factory_style)
        # the interpreter every proto of the sample runs on: function(Z,Z,Z,J,i,U), keys Z and J
        last = m.interps[-1]
        self.assertEqual(sorted(m.wrapper_numbers(last)), [2, 3])
        self.assertTrue(m.keyed_wrappers)      # switches the v15.2 passes on (lifter, run.py)

    @unittest.skipUnless(os.path.exists(LUAU_AST), 'needs bin/luau-ast')
    def test_instrumented_chunk_parses(self):
        path = os.path.join(self.tmp.name, 'step.lua')
        with open(path, 'w', encoding='latin-1') as f:
            f.write(self.model.patched_source(step=True, stepvars=self.model.step_vars()))
        self.assertTrue(parses(path))


@unittest.skipUnless(os.path.exists(LAYER3) and os.path.exists(LUAU_AST), 'needs examples/layer3.lua and bin/luau-ast')
class Layer3(unittest.TestCase):
    def test_instrumented_copy_parses(self):
        with open(LAYER3, encoding='latin-1') as f:
            m = model_of(f.read())
        self.assertEqual([len(it.loops) for it in m.interps], [4, 4])
        self.assertEqual([m.wrapper_numbers(it) for it in m.interps], [{}, {}])
        self.assertFalse(m.keyed_wrappers)     # v15.1: the v15.2 passes stay off
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'step.lua')
            with open(path, 'w', encoding='latin-1') as f:
                f.write(m.patched_source(step=True, stepvars=m.step_vars()))
            self.assertTrue(parses(path))


@unittest.skipUnless(have_luau(), 'needs bin/luau')
class CarrierRun(unittest.TestCase):
    def test_loadstring_substitution(self):
        """A carrier loads chunk text A; the harness compiles text B in its place, under the chunk
        name the carrier gave and with the carrier's argument."""
        with tempfile.TemporaryDirectory() as tmp:
            def put(name, text):
                p = os.path.join(tmp, name)
                with open(p, 'w') as f:
                    f.write(text)
                return p
            orig = put('orig.lua', 'return function() return "original" end')
            patched = put('patched.lua', 'local b = ... return function() ewrite("[t] " .. debug.info(1, "s") .. " " .. buffer.len(b) .. "\\n") end')
            arg = put('arg.bin', 'abcde')
            c = container.stand_in(orig, arg, tmp, 't')
            out = os.path.join(tmp, 'out.trace')
            r = subprocess.run([vmmodel.find_luau(), os.path.join(ROOT, 'devm', 'harness.lua'), '-a', patched, out] + c.harness_options(),
                               capture_output=True, text=True, errors='replace', timeout=60)
            self.assertIn('[t] [string "Luraph"] 5', r.stderr)


def scope_errors(stmts, scope=frozenset()):
    """uses of a variable outside the scope of its declaration, in a structure.py statement list"""
    bad = []
    scope = set(scope)
    for st in stmts:
        t = st[0]
        if t in ('local', 'localfunction'):
            bad += [n for n in structure.stmt_vars(st) if n not in scope]
            scope.update(structure._decl_names(st))
            continue
        if t == 'do':
            bad += scope_errors(st[1], scope)
            continue
        bad += [n for n in structure.stmt_vars(st, deep=False) if n not in scope]
        for body, _extra in structure._bodies(st):
            bad += scope_errors(body, scope)
    return bad


def depth(stmts):
    return 1 + max([depth(b) for st in stmts for b, _ in structure._bodies(st)] or [0])


class FitScopes(unittest.TestCase):
    def chain(self, n, keep=()):
        """local v1 = 0; local v2 = v1 + 1; ...; every 25th statement is an `if` with locals of its own"""
        out = [['local', ['v0'], [('k', 0)]]]
        for i in range(1, n):
            prev = ('r', 'v%d' % (i - 1))
            if i % 25 == 0:
                out.append(['if', prev, [['local', ['w%d_%d' % (i, j)], [prev]] for j in range(30)], None])
                out.append(['local', ['v%d' % i], [prev]])
            else:
                out.append(['local', ['v%d' % i], [('bin', '+', prev, ('r', 'v%d' % keep[i % len(keep)]) if keep else ('k', 1))]])
        out.append(['return', [('r', 'v%d' % (n - 1))]])
        return out

    def test_chain(self):
        ast = self.chain(600)
        self.assertGreater(structure.max_locals(ast), 600)
        structure.fit_scopes(ast, 0, 190)
        self.assertLessEqual(structure.max_locals(ast), 190)
        self.assertEqual(scope_errors(ast), [])
        self.assertLessEqual(depth(ast), 3)

    def test_long_lived(self):
        # a few locals of the first section are used throughout: they are declared ahead of it
        ast = self.chain(500, keep=(3, 7, 11))
        structure.fit_scopes(ast, 0, 190)
        self.assertLessEqual(structure.max_locals(ast), 190)
        self.assertEqual(scope_errors(ast), [])

    def test_small_function_untouched(self):
        ast = self.chain(100)
        before = repr(ast)
        structure.fit_scopes(ast, 0, 190)
        self.assertEqual(repr(ast), before)


class MergeArms(unittest.TestCase):
    """beautify.copy_diamonds / sink_common (on for v15.2 output only: beautify.MERGE_ARMS)"""

    @staticmethod
    def asg(x, v):
        return ['assign', [('r', x)], [v]]

    def test_copy_diamond(self):
        from devm import beautify
        c, e = ('r', 'c'), ('call', ('global', 'f'), [], None)
        for cond, t, f, want in (
                (c, c, e, ('or', c, e)),                    # if c then x = c else x = f() end
                (c, e, c, ('and', c, e)),                   # if c then x = f() else x = c end
                (('un', 'not', c), e, c, ('or', c, e)),     # if not c then x = f() else x = c end
                (('un', 'not', c), c, e, ('and', c, e))):
            ast = [['if', cond, [self.asg('x', t)], [self.asg('x', f)]]]
            beautify.copy_diamonds(ast)
            self.assertEqual(ast, [['assign', [('r', 'x')], [want]]])
        # not a copy of the tested variable, two targets, a test that is not a plain variable: untouched
        for ast in ([['if', c, [self.asg('x', ('k', 1))], [self.asg('x', ('k', 2))]]],
                    [['if', c, [self.asg('x', c)], [self.asg('y', e)]]],
                    [['if', ('bin', '<', c, ('k', 1)), [self.asg('x', c)], [self.asg('x', e)]]]):
            before = repr(ast)
            beautify.copy_diamonds(ast)
            self.assertEqual(repr(ast), before)

    def test_sink_common(self):
        from devm import beautify
        c = ('r', 'c')
        call = lambda *a: ['call', ('call', ('global', 'g'), list(a), None)]        # noqa: E731
        ast = [['if', c, [self.asg('k', ('k', 'Body')), self.asg('v', c)], [self.asg('k', ('k', 'Body')), self.asg('v', ('global', 'KEY'))]],
               call(('r', 'k'), ('r', 'v'))]
        beautify.sink_common(ast, {})
        self.assertEqual(ast[1], self.asg('k', ('k', 'Body')))
        self.assertEqual(ast[0], ['if', c, [self.asg('v', c)], [self.asg('v', ('global', 'KEY'))]])
        # an arm that is left empty goes away; both empty: the test of a plain variable goes too
        ast = [['if', c, [self.asg('k', ('k', 1))], [self.asg('k', ('k', 1)), call()]]]
        beautify.sink_common(ast, {})
        self.assertEqual(ast, [['if', ('un', 'not', c), [call()], None], self.asg('k', ('k', 1))])
        ast = [['if', c, [self.asg('k', ('k', 1))], [self.asg('k', ('k', 1))]]]
        beautify.sink_common(ast, {})
        self.assertEqual(ast, [self.asg('k', ('k', 1))])
        # kept where moving could be seen: the arm reads the variable, an arm leaves early, a
        # closure captured it, the copied variable changes in an arm, the test has an effect
        keep = (
            [['if', c, [self.asg('k', ('k', 1)), call(('r', 'k'))], [self.asg('k', ('k', 1))]]],
            [['if', c, [self.asg('k', ('k', 1)), ['break']], [self.asg('k', ('k', 1)), call()]]],
            [['if', c, [self.asg('k', ('r', 'y')), self.asg('y', ('k', 2))], [self.asg('k', ('r', 'y')), call()]]],
            [['if', ('call', ('global', 'f'), [], None), [self.asg('k', ('k', 1))], [self.asg('k', ('k', 1))]]],
        )
        for ast in keep:
            before = repr(ast)
            beautify.sink_common(ast, {})
            self.assertEqual(repr(ast), before)
        ast = [['if', c, [self.asg('k', ('k', 1)), call()], [self.asg('k', ('k', 1))]]]
        before = repr(ast)
        beautify.sink_common(ast, {'k': 1})
        self.assertEqual(repr(ast), before)

    def test_off_by_default(self):
        from devm import beautify
        self.assertFalse(beautify.MERGE_ARMS)
        self.assertFalse(structure.NEST_ITEMS)
        c = ('r', 'c')
        ast = [['if', c, [self.asg('x', c)], [self.asg('x', ('k', 2))]], ['return', [('r', 'x')]]]
        before = repr(ast)
        beautify.beautify(ast)
        self.assertEqual(repr(ast), before)


class TableLayout(unittest.TestCase):
    def test_nested_items(self):
        """An item of a multi-line table that spans lines itself sits one level inside the braces."""
        inner = ('table', [(None, ('k', 'x' * 30)) for _ in range(5)], None)
        outer = ('table', [(('k', 'a'), inner), (('k', 'b'), ('k', 1))], None)
        p = structure.Printer(None, [])
        p.level = 1
        old = structure.NEST_ITEMS
        try:
            structure.NEST_ITEMS = True
            lines = p.table(outer).split('\n')
        finally:
            structure.NEST_ITEMS = old
        self.assertEqual(lines[0], '{')
        self.assertEqual(lines[1], '\t\ta = {')
        self.assertTrue(lines[2].startswith('\t\t\t"xxx'))
        self.assertEqual(lines[-3], '\t\t},')
        self.assertEqual(lines[-2], '\t\tb = 1,')
        self.assertEqual(lines[-1], '\t}')


class Report(unittest.TestCase):
    def test_vm_object(self):
        from devm import run
        text = ('local a = __LPH_PROTOS[1]["self"]["Z"]\n__LPH_PROTOS[1]["self"][120] = a\n'
                '-- LIFT ERROR: self-modifying store of a run-time value into __LPH_PROTOS[23]["vars"]["R"][-2][556]\n')
        out = run.name_vm_object(text)
        self.assertIn('local a = LuraphVM.Z\nLuraphVM[120] = a\n', out)
        self.assertTrue(out.startswith('-- LuraphVM: '))
        rep = '\n'.join(run.report(out))
        self.assertIn('result: PARTIAL', rep)
        self.assertIn('1 LIFT ERROR markers', rep)
        self.assertIn('2 uses of LuraphVM', rep)
        self.assertNotIn('__LPH_PROTOS', rep)       # (the marker's own text is not a second finding)
        self.assertEqual(run.name_vm_object('local a = 1\n'), 'local a = 1\n')

    def test_head_is_not_counted(self):
        """A file written by main() starts with the report of its run: reading it again gives the
        same findings, and a marker in the first line of a result is never taken for a head."""
        from devm import run
        body = '-- LIFT ERROR: call of foreign VM closure\n'
        first = run.report(body)
        self.assertIn('1 LIFT ERROR markers', '\n'.join(first))
        head = '-- x.lua, %s.  How the run went and what this file lacks:\n' % run.HEAD_MARK + ''.join('--   %s\n' % ln for ln in first)
        self.assertEqual(run.report(head + body), first)
        self.assertIn('result: complete', run.report(head + 'local a = 1\n')[0])


@unittest.skipUnless(os.environ.get('DEVM_V152_FULL') and have_luau() and os.path.exists(SAMPLE), 'set DEVM_V152_FULL=1 (about 2 minutes)')
class V152Full(unittest.TestCase):
    def test_pipeline(self):
        from devm import run
        with tempfile.TemporaryDirectory() as tmp:
            logs = []
            text = run.deobfuscate(SAMPLE, os.path.join(tmp, 'w'), log=logs.append)
            if vmmodel.SERVER is not None:
                vmmodel.SERVER.close()
            out = os.path.join(tmp, 'out.lua')
            with open(out, 'w', encoding='utf-8') as f:
                f.write(text)
            self.assertTrue(parses(out))
        log = '\n'.join(logs)
        self.assertIn('0 in code that ran', log)
        # the loader inside the sample: its checks in order, then the key-server request (the
        # warning of the request check is printed in the arm after the request it guards)
        marks = ['PC-R01', 'PC-R02', 'PC-E01', 'PC-E02', 'PC-E03',
                 'api.jnkie.com/api/v1/luascripts/precheck/', 'SCRIPT_KEY',
                 'Request compatibility check failed', 'Key verification unavailable']
        pos = [text.find(m) for m in marks]
        self.assertTrue(all(p >= 0 for p in pos), dict(zip(marks, pos)))
        self.assertEqual(pos, sorted(pos))
        # one structured function, not a chain of nested blocks or a state machine
        lines = text.split('\n')
        self.assertLess(len(lines), 8000)
        self.assertLess(max(len(ln) - len(ln.lstrip('\t')) for ln in lines), 30)
        self.assertNotIn('not structured', text)
        self.assertLess(text.count('UPVAL_'), 100)      # (closures get their captured variables: about 40 uses are left, inside native functions; several hundred before)


@unittest.skipUnless(os.environ.get('DEVM_V152_FULL') and have_luau() and os.path.exists(LAYER3), 'set DEVM_V152_FULL=1 (about 2 minutes)')
class Layer3Full(unittest.TestCase):
    def test_pipeline(self):
        """The v15.1 chunk with its buffer: the script is plain Lua kept in the VM object, created
        by the payload through a four-parameter wrapper; statements that start with a parenthesis
        must not merge with the line before."""
        from devm import run
        with tempfile.TemporaryDirectory() as tmp:
            logs = []
            text = run.deobfuscate(LAYER3, os.path.join(tmp, 'w'), log=logs.append,
                                   chunk_arg=os.path.join(ROOT, 'examples', 'blob4.bin'))
            if vmmodel.SERVER is not None:
                vmmodel.SERVER.close()
            out = os.path.join(tmp, 'out.lua')
            with open(out, 'w', encoding='utf-8') as f:
                f.write(text)
            self.assertTrue(parses(out))
        self.assertGreater(text.count('\n'), 10000)
        self.assertIn('game:GetService("CoreGui")', text)
        self.assertLess(text.count('LIFT ERROR'), 10)


if __name__ == '__main__':
    unittest.main()
