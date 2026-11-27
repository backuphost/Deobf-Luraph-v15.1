"""Checks of devm's printer (devm/structure.py, Decompiler) on hand-written syntax trees.

    python3 tests/devm_print.py            (from the repository root; seconds)

* A function the protected code makes afresh and calls on the spot at several places (Luraph's VM
  makes a new closure of one program at each call) is printed once, as a local function at the top
  of the result, and called by name (structure.HOIST_SHARED) -- only where that cannot change what
  the code does: it captures nothing, every place calls it on the spot, and every place passes the
  same large literal numbers.  A function such a function calls on the spot is defined before it.
  The two printings do the same thing when run (bin/luau, on the printed text of these trees).
* It is left where it is when it captures a variable, when one place keeps it instead of calling
  it, when the places pass different large literals, when it is made at one place only, when the
  `-- proto N` heads are asked for (DEVM_PROTO_IDS: opaque.relift_params reads the text) and with
  HOIST_SHARED off.
"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from devm import structure, vmmodel  # noqa: E402


def have_luau():
    try:
        return bool(vmmodel.find_luau())
    except RuntimeError:
        return False


class Trees(structure.Decompiler):
    """A Decompiler whose programs are given as syntax trees: {key: (number of parameters, body)},
    the body a function of (a name for a new local, the parameter names) -> statements."""

    def __init__(self, trees, root=1):
        super().__init__({k: None for k in trees}, root)
        self.trees = trees

    def proto_ast(self, key):
        n, body = self.trees[key]
        params = [self.namer.param() for _ in range(n)]
        self.stats['protos'] += 1
        return params, False, body(self.namer.var, params)


def call(f, *args):
    return ('call', f, list(args), None)


def k(v):
    return ('k', v)


def g(name):
    return ('global', name)


def r(name):
    return ('r', name)


KEY = 363320037


def helper(new, p):
    # function(a, b) local t = a .. "-" .. b; return t end
    t = new()
    return [['local', [t], [('bin', '..', ('bin', '..', r(p[0]), k('-')), r(p[1]))]], ['return', [r(t)]]]


def root_two_calls(caps=(), keys=(KEY, KEY), stored=False):
    def body(new, p):
        a, b = new(), new()
        out = [['local', [a], [call(('closure', 2, caps), k('x'), k(2), k(keys[0]))]]]
        if stored:
            f = new()
            out += [['local', [f], [('closure', 2, caps)]], ['local', [b], [call(r(f), k('y'), k(1), k(keys[1]))]]]
        else:
            out += [['local', [b], [call(('closure', 2, caps), k('y'), k(1), k(keys[1]))]]]
        out.append(['call', call(g('print'), r(a), r(b))])
        return out
    return body


def run_lua(text):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'case.lua')
        with open(path, 'w') as f:
            f.write(text)
        p = subprocess.run([vmmodel.find_luau(), path], capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout, p.stderr


class Shared(unittest.TestCase):
    def setUp(self):
        self.keep = structure.HOIST_SHARED, os.environ.pop('DEVM_PROTO_IDS', None)

    def tearDown(self):
        structure.HOIST_SHARED = self.keep[0]
        if self.keep[1] is not None:
            os.environ['DEVM_PROTO_IDS'] = self.keep[1]

    def printed(self, trees):
        return Trees(trees).run()

    def inline(self, trees):
        structure.HOIST_SHARED = False
        try:
            return Trees(trees).run()
        finally:
            structure.HOIST_SHARED = True

    def test_printed_once(self):
        trees = {1: (0, root_two_calls()), 2: (2, helper)}
        text = self.printed(trees)
        self.assertEqual(text.count('function('), 0, text)
        self.assertEqual(text.count('local function '), 1, text)
        self.assertNotIn('(function', text)
        lines = text.split('\n')
        self.assertTrue(lines[0].startswith('-- (the protected code makes this function afresh'), text)
        name = lines[1][len('local function '):].split('(')[0]
        self.assertTrue(lines[1].startswith('local function %s(' % name), text)
        self.assertEqual(text.count('%s(' % name), 3, text)       # the definition and two calls
        self.assertIn('= %s("x", 2, %d)' % (name, KEY), text)
        old = self.inline(trees)
        self.assertEqual(old.count('(function('), 2, old)
        if have_luau():
            self.assertEqual(run_lua(text), run_lua(old))
            self.assertEqual(run_lua(text)[1], 'x-2\ty-1\n')

    def test_a_function_it_calls_is_defined_first(self):
        def outer(new, p):
            # function(a) return inner(a, "!") .. inner("?", a) end, inner made on the spot each time
            return [['return', [('bin', '..', call(('closure', 3, ()), r(p[0]), k('!')),
                                 call(('closure', 3, ()), k('?'), r(p[0])))]]]

        def root(new, p):
            a, b = new(), new()
            return [['local', [a], [call(('closure', 2, ()), k('x'))]],
                    ['local', [b], [call(('closure', 2, ()), k('y'))]],
                    ['call', call(g('print'), r(a), r(b))]]
        trees = {1: (0, root), 2: (1, outer), 3: (2, helper)}
        text = self.printed(trees)
        self.assertNotIn('(function', text)
        defs = [ln for ln in text.split('\n') if ln.startswith('local function ')]
        self.assertEqual(len(defs), 2, text)
        inner = defs[0][len('local function '):].split('(')[0]
        self.assertIn(inner + '(', text.split(defs[1])[1].split('\nend')[0])     # the second one calls the first
        if have_luau():
            self.assertEqual(run_lua(text), run_lua(self.inline(trees)))
            self.assertEqual(run_lua(text)[1], 'x-!?-x\ty-!?-y\n')

    def test_left_alone(self):
        cases = {
            'captures a variable': root_two_calls(caps=(('ref', ('r', 'v9')),)),
            'kept by one place': root_two_calls(stored=True),
            'different large literals': root_two_calls(keys=(KEY, KEY + 2)),
        }
        for why, root in cases.items():
            text = self.printed({1: (0, root), 2: (2, helper)})
            self.assertFalse(text.startswith('-- (the protected code makes'), (why, text))
            self.assertNotIn('local function', text, why)

        def once(new, p):
            a = new()
            return [['local', [a], [call(('closure', 2, ()), k('x'), k(1))]], ['call', call(g('print'), r(a))]]
        self.assertEqual(self.printed({1: (0, once), 2: (2, helper)}).count('(function('), 1)

    def test_switched_off(self):
        trees = {1: (0, root_two_calls()), 2: (2, helper)}
        os.environ['DEVM_PROTO_IDS'] = '1'
        try:
            self.assertEqual(self.printed(trees).count('(function('), 2)
        finally:
            del os.environ['DEVM_PROTO_IDS']
        self.assertEqual(self.inline(trees).count('(function('), 2)


if __name__ == '__main__':
    unittest.main()
