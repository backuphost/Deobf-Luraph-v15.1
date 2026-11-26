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
  that main() writes at the head of a result is not taken for part of it; the tamper response
  function is labelled and its calls are counted;
* the model's roles of the wrapper parameters (keys, upvalue list) for all four interpreters of
  the v15.2 chunk, against how the interpreter bodies use them;
* `ircfg.ProtoCFG._recover_for_loops` on the v15.2 forms of table-state `for` loops (state table
  through a scratch register, bounds reused by the set-up, a bound that is a call, a loop left by
  a jump inside an enclosing loop), and that none of it happens without `ircfg.V152`;
* the tamper response: which registers and upvalues hold its closure, and that a block ends at
  its call (`ircfg.ProtoCFG._scan_traps`, `_trap_refs`, `_cut_after_traps`).

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
    r = subprocess.run([LUAU_AST, path], capture_output=True, text=True, errors='replace')
    return r.returncode == 0 and 'Parse errors' not in (r.stderr[:4000] + r.stdout[:4000])


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

    def test_wrapper_roles(self):
        """Wrapper parameters of all four interpreters: which carry per-closure key numbers and
        which is the upvalue list.  Checked against the interpreter bodies: a key is copied into a
        local that the code rotates and xors (`bxor(K, band(rshift(K, 29), 7))`), the upvalue
        list is indexed with an operand (`U[F[pc]]`).  Only the last interpreter is exercised by
        the sample's run; the others are read off the text."""
        import re
        from devm import lifter
        m = self.model
        stub = type('L', (), {'model': m})()
        want = [([0, 1], 2), ([0, 2, 3], 1), ([1], 0), ([2, 3], 4)]
        for it, (nums, up) in zip(m.interps, want):
            mid = it.chain[-2]
            got = m.wrapper_numbers(it)
            self.assertEqual(sorted(got), nums, 'interpreter %d' % it.index)
            self.assertEqual(lifter.FactoryLifter.upvalue_param(stub, mid), up, 'interpreter %d' % it.index)
            body = m.src[it.func.body_pos:it.func.end_pos]
            # every key local is used as the seed of the keyed decoders: f(K, g(h(K, 29), 7))
            keyed = [n for names in got.values() for n in names
                     if re.search(r'\w+\(%s,\s*\(?\w+\(\w+\(%s,\s*29\),\s*7\)' % (re.escape(n), re.escape(n)), body)]
            self.assertTrue(keyed, 'interpreter %d: no key local feeds a decoder' % it.index)

    def test_for_state(self):
        """The interpreter that runs keeps for-loop state in locals and saves them on a stack that
        goes through a scratch variable (`Xh = l; l, h = {[6]=Xh, [9]=A, ...}, X[W]`): the stack and
        the saved locals are for-loop state, the scratch variable is not."""
        from devm import pe
        P = pe.PE.__new__(pe.PE)
        fvars, fstack, _ = pe.PE._classify_state_vars(P, self.model.interps[-1].loops)
        self.assertEqual(len(fstack), 1)
        self.assertEqual(len(fvars), 5)
        self.assertTrue(fstack <= fvars)

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
        # for-loop state of the interpreter the script's functions run on (same form as v15.2)
        from devm import luprint, pe
        fvars, fstack, _ = pe.PE._classify_state_vars(pe.PE.__new__(pe.PE), m.interps[0].loops)
        self.assertEqual((len(fstack), len(fvars)), (1, 5))
        # the script's first plain-Lua function sits in a wrapper without parameters
        # (`function() return function() ... end end`): it is printed, not replaced by a stub
        first = [f for f in m.funcs if not f.params and luprint.native_shape(f) is None
                 and len(f.body) == 1 and f.body[0][0] == 'return' and len(f.body[0][1]) == 1 and f.body[0][1][0][0] == 'func']
        self.assertTrue(first)
        text = luprint.native_closure_text(first[0], [], 0, m.cells, m.up_base)
        self.assertTrue(text and text.startswith('function('))
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


class ConstIfs(unittest.TestCase):
    def fold(self, text):
        from devm import opaque
        while True:
            new = opaque._fold_const_ifs(text)
            if new == text:
                return text
            text = new

    def test_arm_that_leaves(self):
        """`if not false then return end` in front of more statements stays as it is: without the
        `if`, `return` would swallow the next statement (`return print(...)` parses)."""
        a = 'local f = function(...)\n\tif not false then\n\t\treturn\n\tend\n\tprint(1)\nend\n'
        self.assertEqual(self.fold(a), a)
        b = 'local f = function(...)\n\tif not false then\n\t\treturn\n\tend\nend\n'
        self.assertEqual(self.fold(b), 'local f = function(...)\n\treturn\nend\n')
        c = 'if false then\n\ta()\nelse\n\tb()\nend\nc()\n'
        self.assertEqual(self.fold(c), 'b()\nc()\n')


def misplaced_terminators(text):
    """Lines where return/break/continue is followed by another statement of the same block."""
    import re
    lines = text.split('\n')
    bad = []
    for i in range(len(lines) - 1):
        m = re.match(r'^(\t*)(return\b.*|break|continue)\s*$', lines[i])
        nxt = lines[i + 1]
        if m and nxt.strip() and len(nxt) - len(nxt.lstrip('\t')) >= len(m.group(1)) \
                and not lines[i].rstrip().endswith(('{', '(', ',')) and not re.search(r'function\([^)]*\)$', lines[i]):
            bad.append(i + 1)
    return bad


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

    def test_tamper_response(self):
        """A function that reads the operand arrays of a VM function is Luraph's tamper response:
        labelled (when structure.NOTE_TRAPS is on) and its calls counted by the report."""
        from devm import run
        node = type('N', (), {})
        trap, plain = type('P', (), {})(), type('P', (), {})()
        a, b = node(), node()
        a.tree = ([('set', ('r', 6), ('outer', '__LPH_PROTOS[6]["vars"]["R"][-2]'))], ('ret', [], None))
        b.tree = ([('set', ('r', 6), ('outer', '__LPH_PROTOS[1]["self"]["F"]'))], ('ret', [], None))
        trap.nodes, plain.nodes = {1: a}, {1: b}
        self.assertTrue(structure.loads_own_code(trap))
        self.assertFalse(structure.loads_own_code(plain))
        self.assertFalse(structure.NOTE_TRAPS)
        text = ('local v60 = function()\n\t-- %s\n\t-- LIFT ERROR: call of None\nend\n'
                'if not ok then\n\tv60()\nend\nv60()\nlocal x = v60\n' % structure.TRAP_NOTE)
        rep = run.report(text)
        self.assertIn("note: 2 calls of v60, Luraph's tamper response", '\n'.join(rep))
        self.assertNotIn('note:', '\n'.join(run.report('local v60 = function()\nend\nv60()\n')))

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


def for_cfg(entry_stmts, slots, body, after=(), outer=False):
    """A hand-made CFG with one table-state loop: entry block E, loop head H (state table in
    register 21; `slots` = (index slot, step slot, limit slot)), body B, exit X (pops the table
    through slot 4).  outer=True puts the loop inside another loop that the body can jump to."""
    from devm import ircfg
    ki, ks, kl = slots
    S = ('r', 21)

    def at(k):
        return ('index', S, ('k', k))
    cfg = ircfg.ProtoCFG.__new__(ircfg.ProtoCFG)
    cfg.pir = type('P', (), {'key': 0})()
    cfg.param_regs = None
    cfg.blocks = {}

    def block(name, stmts, term):
        b = ircfg.Block(name)
        b.stmts, b.term = list(stmts), term
        cfg.blocks[name] = b
    block('E', entry_stmts, ('goto', 'H'))
    block('H', [
        ('set', ('r', 10), at(ks)),
        ('set', ('r', 11), at(kl)),
        ('set', ('r', 5), ('bin', '+', at(ki), ('r', 10))),
        ('set', ('r', 10), ('bin', '<=', ('r', 10), ('k', 0))),
        ('set', ('r', 14), ('bin', '>=', ('r', 5), ('r', 11))),
        ('set', ('r', 11), ('bin', '<=', ('r', 5), ('r', 11))),
        ('set', ('r', 14), ('or', ('and', ('r', 10), ('r', 14)), ('and', ('un', 'not', ('r', 10)), ('r', 11)))),
        ('setindex', S, ('k', ki), ('r', 5)),
    ], ('br', ('r', 14), 'B', 'X'))
    pop = ('set', S, at(4))
    if outer:
        block('B', body, ('br', ('r', 30), 'X2', 'L'))
        block('L', [], ('goto', 'H'))
        block('X2', [pop], ('goto', 'O'))
        block('X', [pop] + list(after), ('goto', 'O'))
        block('O', [], ('br', ('r', 31), 'E', 'R'))
        block('R', [], ('ret', [('r', 6)], None))
    else:
        block('B', body, ('goto', 'H'))
        block('X', [pop] + list(after), ('ret', [('r', 6)], None))
    cfg.entry = 'E'
    cfg.compute_preds()
    return cfg


class ForState(unittest.TestCase):
    """Numeric `for` loops whose state Luraph keeps in a table, in the forms v15.2 writes
    (ircfg.ProtoCFG._recover_for_loops with ircfg.V152)."""

    def setUp(self):
        from devm import ircfg
        self.ircfg = ircfg
        ircfg.V152 = True

    def tearDown(self):
        self.ircfg.V152 = False

    BODY = [('set', ('r', 6), ('bin', '+', ('bin', '*', ('r', 6), ('k', 10)), ('r', 5)))]

    def scratch_entry(self):
        # T = {link .. first - step}; n = 0; S = T; b = c     (the bounds read n and b)
        return [
            ('set', ('r', 17), ('table', [(None, ('r', 37)), (None, ('k', 1)),
                                          (None, ('bin', '+', ('bin', '-', ('r', 23), ('k', 1)), ('k', 0))),
                                          (None, ('r', 21)),
                                          (None, ('bin', '-', ('bin', '+', ('r', 6), ('k', 1)), ('k', 1)))], None)),
            ('set', ('r', 6), ('k', 0)),
            ('set', ('r', 21), ('r', 17)),
            ('set', ('r', 23), ('r', 5)),
        ]

    def test_scratch_register_and_reused_bounds(self):
        cfg = for_cfg(self.scratch_entry(), (5, 2, 3), self.BODY)
        self.assertEqual(cfg._recover_for_loops(), 1)
        self.assertEqual(cfg.blocks['H'].term[:3], ('forbr', 'num', (5,)))
        entry = cfg.blocks['E'].stmts
        # both bounds are evaluated where the table was built, before `n = 0` and `b = c`
        self.assertEqual([st[2] for st in entry[:2]],
                         [('bin', '+', ('r', 6), ('k', 1)), ('bin', '-', ('r', 23), ('k', 1))])
        self.assertEqual(entry[2:], [('set', ('r', 6), ('k', 0)), ('set', ('r', 23), ('r', 5))])
        fresh = [st[1] for st in entry[:2]]
        self.assertTrue(all(r[1] > 37 for r in fresh) and fresh[0] != fresh[1])
        prep = [b for b in cfg.blocks.values() if b.stmts and b.stmts[0][0] == 'numforprep']
        self.assertEqual(len(prep), 1)
        self.assertEqual(prep[0].stmts[0][1], [fresh[0], fresh[1], ('k', 1)])
        # the pop of the state table is gone with the table
        self.assertEqual(cfg.blocks['X'].stmts, [('set', ('r', 21), ('r', 21))])

    def test_v151_form_only_without_the_flag(self):
        self.ircfg.V152 = False
        cfg = for_cfg(self.scratch_entry(), (5, 2, 3), self.BODY)
        before = repr(sorted((k, b.stmts, b.term) for k, b in cfg.blocks.items()))
        self.assertEqual(cfg._recover_for_loops(), 0)
        self.assertEqual(repr(sorted((k, b.stmts, b.term) for k, b in cfg.blocks.items())), before)
        # ... while the direct form (S = {...} as the last thing before the loop) is recovered
        direct = [('set', ('r', 21), ('table', [(None, ('k', None)), (None, ('k', 1)), (None, ('r', 23)),
                                                (None, ('r', 21)), (None, ('k', 0))], None))]
        cfg = for_cfg(direct, (5, 2, 3), self.BODY)
        self.assertEqual(cfg._recover_for_loops(), 1)
        prep = [b for b in cfg.blocks.values() if b.stmts and b.stmts[0][0] == 'numforprep']
        self.assertEqual(prep[0].stmts[0][1], [('k', 1), ('r', 23), ('k', 1)])

    def test_bound_is_a_call(self):
        call = ('call', ('r', 77), [('r', 15)], None)
        limit = ('bin', '+', ('bin', '+', ('r', 13), ('bin', '-', call, ('k', 9))), ('k', 0))
        entry = [('set', ('r', 21), ('table', [(None, ('r', 0)), (None, ('k', 1)), (None, limit), (None, ('r', 21)),
                                               (None, ('bin', '-', ('r', 86), ('k', 1)))], None))]
        cfg = for_cfg(entry, (5, 2, 3), self.BODY)
        self.assertEqual(cfg._recover_for_loops(), 1)
        prep = [b for b in cfg.blocks.values() if b.stmts and b.stmts[0][0] == 'numforprep']
        self.assertEqual(prep[0].stmts[0][1], [('r', 86), limit[2], ('k', 1)])
        # two calls among the bounds would be reordered: left alone
        entry = [('set', ('r', 21), ('table', [(None, ('r', 0)), (None, ('k', 1)), (None, limit), (None, ('r', 21)),
                                               (None, ('bin', '-', call, ('k', 1)))], None))]
        cfg = for_cfg(entry, (5, 2, 3), self.BODY)
        self.assertEqual(cfg._recover_for_loops(), 0)

    def test_loop_left_by_a_jump_inside_an_outer_loop(self):
        entry = [('set', ('r', 21), ('table', [(None, ('k', 1)), (None, ('k', 1)), (None, ('k', 3)), (None, ('r', 21)),
                                               (None, ('k', 0))], None))]
        cfg = for_cfg(entry, (5, 2, 3), self.BODY, outer=True)
        self.assertEqual(cfg._recover_for_loops(), 1)
        self.assertEqual(cfg.blocks['H'].term[:3], ('forbr', 'num', (5,)))
        self.ircfg.V152 = False
        cfg = for_cfg(entry, (5, 2, 3), self.BODY, outer=True)
        self.assertEqual(cfg._recover_for_loops(), 0)


def hand_cfg(blocks, entry, key=1):
    """A ProtoCFG from {name: (statements, terminator)}, without the passes of its constructor."""
    from devm import ircfg
    cfg = ircfg.ProtoCFG.__new__(ircfg.ProtoCFG)
    cfg.pir = type('P', (), {'key': key})()
    cfg.param_regs = None
    cfg.blocks = {}
    for name, (stmts, term) in blocks.items():
        b = ircfg.Block(name)
        b.stmts, b.term = list(stmts), term
        cfg.blocks[name] = b
    cfg.entry = entry
    cfg.compute_preds()
    return cfg


class TrapCalls(unittest.TestCase):
    """Luraph's tamper response (a function that loads its own VM instruction arrays): where the
    closure is kept, and that a block ends at its call (ircfg.V152)."""

    def setUp(self):
        from devm import ircfg
        self.ircfg = ircfg
        ircfg.V152 = True
        ircfg.TRAP_PROTOS = {6}
        ircfg._TRAP_SCAN.clear()
        ircfg.TRAP_REFS.clear()

    def tearDown(self):
        self.ircfg.V152 = False
        self.ircfg.TRAP_PROTOS = set()
        self.ircfg._TRAP_SCAN.clear()
        self.ircfg.TRAP_REFS.clear()

    TRAP = ('closure', 6, ())

    def test_where_the_closure_is_kept(self):
        ircfg = self.ircfg
        cfg = hand_cfg({
            # R145 = R13 copies a register nobody wrote yet (nil); R7 also receives a call result
            'E': ([('set', ('r', 145), ('r', 13)), ('set', ('r', 13), ('k', 0)), ('set', ('r', 7), self.TRAP),
                   ('set', ('r', 8), ('closure', 9, (('ref', 145), ('ref', 7))))], ('goto', 'B')),
            'B': ([('set', ('r', 145), self.TRAP), ('set', ('r', 7), ('call', ('r', 8), [], None))], ('ret', [], None)),
        }, 'E')
        cfg._scan_traps()
        self.assertEqual(ircfg._TRAP_SCAN[1][0], {145})
        # ... and from there into the upvalues of the functions that capture it
        ircfg._TRAP_SCAN[9] = (set(), [(10, (('up', 0),)), (11, (('up', 1),))], set())
        ircfg._TRAP_SCAN[10] = (set(), [], set())
        ircfg._TRAP_SCAN[11] = (set(), [], {0})
        self.assertEqual(ircfg._trap_refs(), {1: {('r', 145)}, 9: {('up', 0)}, 10: {('up', 0)}})
        # a function that assigns to the variable: it is not a constant, nothing is claimed
        ircfg._TRAP_SCAN[10] = (set(), [], {0})
        self.assertEqual(ircfg._trap_refs(), {})

    def test_block_ends_at_the_call(self):
        ircfg = self.ircfg
        ircfg.TRAP_REFS[1] = {('r', 145), ('up', 3)}
        call = ('callstmt', ('call', ('r', 145), [], None))
        cfg = hand_cfg({
            'A': ([], ('br', ('r', 1), 'T', 'J')),
            'T': ([call], ('goto', 'J')),                       # if not ok then tamper() end
            'J': ([], ('br', ('r', 2), 'T2', 'K')),
            # the callee comes out of an upvalue; behind the call a jump into unrelated code
            'T2': ([('set', ('r', 4), ('up', 3)), ('callstmt', ('call', ('r', 4), [], None)), ('set', ('r', 2), ('k', 5))],
                   ('goto', 'A')),
            'K': ([('callstmt', ('call', ('r', 9), [], None))], ('ret', [], None)),      # some other function
        }, 'A')
        self.assertEqual(cfg._cut_after_traps(), 1)
        self.assertEqual(cfg.blocks['T'].term, ('goto', 'J'))
        self.assertEqual(cfg.blocks['T2'].stmts[-1], ('trap', 'A', 'call'))
        self.assertEqual(len(cfg.blocks['T2'].stmts), 3)
        self.assertEqual(cfg.blocks['T2'].term, ('ret', [], None))
        self.assertEqual(cfg.blocks['K'].term, ('ret', [], None))
        self.assertEqual(len(cfg.blocks['K'].stmts), 1)
        # a function that does not hold the closure is left alone
        ircfg.TRAP_REFS.clear()
        cfg = hand_cfg({'A': ([call, ('set', ('r', 2), ('k', 5))], ('goto', 'A'))}, 'A')
        self.assertEqual(cfg._cut_after_traps(), 0)


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
        # generic for loops come out as loops (the walk over the VM table among them), the
        # v15.2 rewrites are on, and no block goes on after its return
        self.assertNotIn('forval', text)
        self.assertRegex(text, r'for \w+, \w+ in LuraphVM do')
        self.assertIn('getgenv().SCRIPT_KEY or SCRIPT_KEY', text)
        self.assertEqual(misplaced_terminators(text), [])
        # numeric for loops that v15.2 keeps in a state table are loops again (13 loop tests are
        # left as `x = t[i] + t[s]` arithmetic: 11 in functions that never ran and end in an
        # unlifted instruction, 2 in a plain-Lua helper of the VM object; 21 before), and no
        # state comparison is left over a boolean (`true <= 4`: the junk side of a key check)
        import re
        self.assertGreaterEqual(len(re.findall(r'(?m)^\s*for \w+ = ', text)), 20)
        self.assertLessEqual(len(re.findall(r'(?m)^.* = \w+ and \w+ or not \w+ and \w+$', text)), 14)
        self.assertIsNone(re.search(r'\b(?:true|false) <= \d', text))
        self.assertIn(structure.TRAP_NOTE, text)
        self.assertGreater(text.count(structure.TRAP_CALL_NOTE), 50)
        self.assertLessEqual(len(re.findall(r'exit\d+ = true', text)), 6)
        rep = '\n'.join(run.report(text))
        self.assertIn('result: PARTIAL', rep)
        self.assertIn('LIFT ERROR markers', rep)
        self.assertIn("Luraph's tamper response", rep)
        self.assertNotIn('__LPH_PROTOS', rep)


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
        # the script runs to its end (GuiService:GetGuiInset() gives two values); functions on the
        # VM get their captured variables and their for loops; no stub for the first plain function
        self.assertTrue(any('script returned' in ln for ln in logs), logs[-12:])
        self.assertLess(text.count('UPVAL_'), 20)
        self.assertNotIn('forval', text)
        self.assertNotIn('native function L', text)
        self.assertIn('for _, ', text)
        self.assertEqual(misplaced_terminators(text), [])


if __name__ == '__main__':
    unittest.main()
