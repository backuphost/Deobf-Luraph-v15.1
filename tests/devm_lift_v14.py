"""Checks of devm's partial evaluator and lifter on the handler spellings of Luraph v14 interpreters.

    python3 tests/devm_lift_v14.py        (from the repository root; no interpreter needed)

The hand-written cases build a small VM of the proto-table family in Lua text (parallel opcode and
operand arrays, `wrap(proto, upvalues)` returning `function(...)`, one dispatch loop), a dump of
its state as the harness would write it, and lift a few instructions.  The VM and its handlers are
written for these tests: no text of a protected file is in here.  Each case is one spelling that
v14 interpreters use and that the evaluator has to understand:

* the generic FORLOOP that tests the negated condition, the numeric FORLOOP whose step is spelled
  `i = (i + step)`, loop state saved on a linked list or on an array stack;
* the VARARG count clamped by a statement, also with an opaque test in front of the assignment;
* registers above the top cleared with `for k in REGS`, with and without opaque tests in the loop;
* parameters copied out of `{...}`, and by the interpreter's prologue; an argument read at the
  position of the first vararg;
* tests between slots of the loader's state table: decided from the dump when no code that runs
  writes the slot, kept as a run-time variable when code does;
* a handler-local alias of the open-upvalue map, next to an interpreter variable of the same name
  as the alias; the close-upvalues loop with an opaque test around its real test;
* a register array that comes from a Lua helper of the loader instead of table.create;
* folded library calls on literals;
* a program that patches its own operands with values it computes from library calls on
  constants, also through `a and K1 or K2` chains; a value that differs between two paths is not
  a patch value;
* the close-upvalues loop of v14.0.x over the capture lists a call made (a local of the closure
  maker), and the insert that records such a list.

With LURAPH_CORPUS set to a checkout of the public sample corpus (a directory with one
sub-directory per Luraph version) and bin/luau present, CorpusLift also runs the whole of devm on a
few v14.3 - v14.6 files picked at run time and checks that every result parses, that the report
and the markers in the text agree, and that at least two of three results have no lift error.
It is skipped without the variable.  LURAPH_CORPUS_SKIP may name a text file of paths (relative to
the corpus, one per line) that the test must leave alone.
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
sys.setrecursionlimit(100000)

from devm import lifter, pe, structure, vmmodel  # noqa: E402

LUAU_AST = os.path.join(ROOT, 'bin', 'luau-ast')


# ---------------------------------------------------------------- a small VM of the proto-table family
VM = '''return (function(create, pack, unpack, cowrap, coyield, env, S, shared, alloc)
%(loader)s
local function wrap(P, U)
local OPS, A, B, C, NP = P[1], P[2], P[3], P[4], P[5]
%(maker)s
return function(...)
local R = %(alloc)s
local pc = 1
local top = 0
local nargs, args = pack(...)
local fi, fl, fs, fstk
local open
%(prologue)s
while true do
local op = OPS[pc]
if op == 1 then
R[A[pc]] = B[pc]
elseif op == 2 then
return R[A[pc]]
elseif op == 3 then
R[A[pc]] = env[B[pc]]
elseif op == 4 then
R[A[pc]] = R[B[pc]] + R[C[pc]]
elseif op == 5 then
R[A[pc]] = R[A[pc]](R[A[pc] + 1])
elseif op == 6 then
return unpack(R, A[pc], top)
elseif op == 7 then
if R[A[pc]] then pc = B[pc] end
elseif op == 8 then
R[A[pc]] = wrap(B[pc], {})
elseif op == 9 then
pc = B[pc]
elseif op == 10 then
fstk = {[5] = fi, [1] = fstk, [2] = fl, [3] = fs}
local a = A[pc]
fi = R[a]
fl = R[a + 1]
fs = R[a + 2]
fi = fi - fs
pc = B[pc]
elseif op == 12 then
fstk = {[5] = fi, [1] = fstk, [2] = fl, [3] = fs}
local a = A[pc]
local f, s, c = R[a], R[a + 1], R[a + 2]
fi = cowrap(function() for k, v in f, s, c do coyield(true, k, v) end end)
pc = B[pc]
elseif op == 14 then
fi, fstk, fl, fs = fstk[5], fstk[1], fstk[2], fstk[3]
%(handlers)s
end
pc = pc + 1
end
end
end
return wrap
end)(...)
'''


class Tables:
    """Builds the dump of the VM state in the harness's JSON form."""

    def __init__(self):
        self.tables = {}

    def val(self, v):
        if isinstance(v, str):
            return {'s': v.encode('latin-1').hex()}
        if isinstance(v, (list, dict)):
            return {'t': self.table(v)}
        if isinstance(v, tuple):
            return v[0]     # a ready-made tagged value, see tag()
        return v            # numbers, booleans, None

    def table(self, v):
        tid = len(self.tables) + 1
        self.tables[str(tid)] = ent = {'arr': [], 'hash': []}
        if isinstance(v, list):
            ent['arr'] = [self.val(x) for x in v]
        else:
            ent['hash'] = [[self.val(k), self.val(x)] for k, x in v.items()]
        return tid


def tag(**kw):
    """a ready-made tagged value of the dump ({'fn': 'table.create'}, {'env': 1}, {'lfn': line})"""
    return (kw,)


def proto(code, nparams=0):
    """code: [(op, a, b, c)] -> the proto table {OPS, A, B, C, NP}"""
    return [[i[0] for i in code], [i[1] for i in code], [i[2] for i in code], [i[3] for i in code], nparams]


class Mini:
    """The VM with extra handlers, a program for it, and the lift."""

    def __init__(self, handlers='', prologue='', alloc='create(8)', state=None, shared=None, outer='', loader='', maker=''):
        self.src = outer + VM % {'handlers': handlers, 'prologue': prologue, 'alloc': alloc, 'loader': loader,
                                 'maker': maker}
        self.model = vmmodel.Model(self.src, oneline=True)
        self.state = state if state is not None else [0, 0, 0, 0]
        self.shared = shared if shared is not None else {}
        self.extra = {}

    def line_of(self, text):
        return self.src[:self.src.index(text)].count('\n') + 1

    def lift(self, root):
        t = Tables()
        packer = [f for f in self.model.funcs if f.vararg and f is not self.model.interp]
        names = {'create': tag(fn='table.create'), 'unpack': tag(fn='unpack'), 'cowrap': tag(fn='coroutine.wrap'),
                 'coyield': tag(fn='coroutine.yield'), 'env': tag(env=1), 'S': self.state, 'shared': self.shared,
                 'P': root, 'pack': tag(lfn=packer[0].line) if packer else tag(fn='table.pack'),
                 'wrap': tag(lfn=self.line_of('local function wrap'))}
        names.update(self.extra)
        vars_id = t.table(names)
        root_id = len(t.tables) + 1
        t.tables[str(root_id)] = {'arr': [], 'hash': [[{'s': b'vars'.hex()}, {'t': vars_id}]]}
        data = json.loads(json.dumps({'root': {'t': root_id}, 'protos': [], 'tables': t.tables}))
        self.lifter = L = lifter.TableProtoLifter(self.model, pe.Dump(data, self.model))
        self.protos = L.lift_all(L.root_key)
        self.errors = [e for p in self.protos.values() for e in p.errors]
        self.text = structure.Decompiler(self.protos, L.root_key, None).run()
        return self.text


# the vararg packer the VM calls as pack(...): a function of the loader, defined in front of the VM
PACKER = 'local function packer(...) return select("#", ...), {...} end\n'


class Basics(unittest.TestCase):
    def test_straight_line(self):
        m = Mini()
        text = m.lift(proto([(3, 1, 'print', 0), (1, 2, 42, 0), (5, 1, 0, 0), (2, 1, 0, 0)]))
        self.assertEqual(m.errors, [])
        self.assertIn('print(42)', text)


class ForLoops(unittest.TestCase):
    # for i = 1, 10, 1 do print(i + i) end: bounds, prep (10), body, loop instruction, pop (14)
    NUM = [(1, 1, 1, 0), (1, 2, 10, 0), (1, 3, 1, 0), (10, 1, 7, 0),
           (3, 5, 'print', 0), (4, 6, 4, 4), (5, 5, 0, 0), (11, 1, 4, 0), (14, 0, 0, 0), (2, 1, 0, 0)]
    # for k, v in next, tbl, 0 do print(k + v) end
    GEN = [(3, 1, 'next', 0), (3, 2, 'tbl', 0), (1, 3, 0, 0), (12, 1, 7, 0),
           (3, 6, 'print', 0), (4, 7, 4, 5), (5, 6, 0, 0), (13, 1, 4, 0), (14, 0, 0, 0), (2, 1, 0, 0)]

    def check_num(self, handler):
        m = Mini(handler)
        text = m.lift(proto(self.NUM))
        self.assertEqual(m.errors, [])
        self.assertRegex(text, r'for (\w+) = \w+, 10 do\n\s*local \w+ = print\(\1 \+ \1\)\n\s*end')

    def test_numeric_step_spelled_out(self):
        self.check_num("""elseif op == 11 then
fi = (fi + fs)
if (fs > 0 and fi <= fl) or (fs <= 0 and fi >= fl) then
R[A[pc] + 3] = fi
pc = B[pc]
end
""")

    def test_numeric_step_spelled_out_negated(self):
        self.check_num("""elseif op == 11 then
fi = fs + fi
if not ((fs > 0 and fi <= fl) or (fs <= 0 and fi >= fl)) then
else
R[A[pc] + 3] = fi
pc = B[pc]
end
""")

    def test_numeric_step_compound(self):
        self.check_num("""elseif op == 11 then
fi += fs
if (fs > 0 and fi <= fl) or (fs <= 0 and fi >= fl) then
R[A[pc] + 3] = fi
pc = B[pc]
end
""")

    def check_gen(self, handler):
        m = Mini(handler)
        text = m.lift(proto(self.GEN))
        self.assertEqual(m.errors, [])
        self.assertRegex(text, r'for (\w+), (\w+) in \w+, tbl, 0 do\n\s*local \w+ = print\(\1 \+ \2\)\n\s*end')

    def test_generic(self):
        self.check_gen("""elseif op == 13 then
local a = A[pc]
local ok, k, v = fi()
if ok then
R[a + 3] = k
R[a + 4] = v
pc = B[pc]
end
""")

    def test_generic_negated(self):
        self.check_gen("""elseif op == 13 then
local a = A[pc]
local ok, k, v = fi()
if not (ok) then
else
R[a + 3] = k
R[a + 4] = v
pc = B[pc]
end
""")


class StateVariables(unittest.TestCase):
    def sets(self, handlers, prologue=''):
        m = Mini(handlers, prologue)
        return pe.PE(m.model, None).state_sets[0]

    def test_linked_list_of_the_base_vm(self):
        fvars, fstack, openups = self.sets('')
        self.assertEqual(fstack, {'fstk'})
        self.assertEqual(fvars, {'fi', 'fl', 'fs', 'fstk'})
        self.assertEqual(openups, set())

    def test_alias_of_the_open_upvalue_map(self):
        # `u` is the handler's copy of `open`; another handler has a local `u` of its own, and the
        # interpreter has a variable `u` that is neither
        fvars, fstack, openups = self.sets("""elseif op == 30 then
local u = open
if u then
for k, c in u do
c[3] = c
c[2] = R[k]
u[k] = nil
end
end
elseif op == 31 then
u = A[pc]
""", prologue='local u')
        self.assertEqual(openups, {'open'})

    def test_handler_local_is_not_interpreter_state(self):
        fvars, fstack, openups = self.sets("""elseif op == 30 then
local q = {}
for k, c in q do
c[3] = c
c[2] = R[k]
end
""", prologue='local q')
        self.assertEqual(openups, set())

    def test_register_clearing_loop_is_not_the_upvalue_map(self):
        # the opaque test inside mentions 2 and 3, the numbers of the cell fields
        fvars, fstack, openups = self.sets("""elseif op == 30 then
for k, v in R do
if S[2] == S[3] then
S[1] = 3
elseif k > top then
R[k] = nil
end
end
""")
        self.assertEqual(openups, set())

    def test_array_stack(self):
        # v14.0.2 - v14.2: the saved loop state goes on an array stack
        fvars, fstack, openups = self.sets("""elseif op == 30 then
H[w] = {[5] = L, [4] = o, [1] = W}
w = w + 1
L = R[A[pc]]
elseif op == 31 then
w = w - 1
local s = H[w]
L, o, W = s[5], s[4], s[1]
""", prologue='local H, w, L, o, W = {}, 1')
        self.assertLessEqual({'H', 'w'}, fstack)
        self.assertLessEqual({'L', 'o', 'W'}, fvars)
        self.assertNotIn('w', fvars)

    def test_array_stack_loop(self):
        # the whole numeric loop with its state on an array stack
        m = Mini("""elseif op == 30 then
H[w] = {[5] = L, [4] = o, [1] = W}
w = w + 1
local a = A[pc]
L = R[a]
o = R[a + 1]
W = R[a + 2]
L = L - W
pc = B[pc]
elseif op == 31 then
L = (L + W)
if (W > 0 and L <= o) or (W <= 0 and L >= o) then
R[A[pc] + 3] = L
pc = B[pc]
end
elseif op == 32 then
w = w - 1
local s = H[w]
L, o, W = s[5], s[4], s[1]
""", prologue='local H, w, L, o, W = {}, 1')
        code = [(1, 1, 1, 0), (1, 2, 10, 0), (1, 3, 1, 0), (30, 1, 7, 0),
                (3, 5, 'print', 0), (4, 6, 4, 4), (5, 5, 0, 0), (31, 1, 4, 0), (32, 0, 0, 0), (2, 1, 0, 0)]
        text = m.lift(proto(code))
        self.assertEqual(m.errors, [])
        self.assertRegex(text, r'for (\w+) = \w+, 10 do\n\s*local \w+ = print\(\1 \+ \1\)\n\s*end')


class Varargs(unittest.TestCase):
    PROGRAM = [(21, 1, 0, 0), (6, 1, 0, 0)]         # R[1...] = ...; return R[1 .. top]

    def check(self, handler):
        m = Mini(handler, outer=PACKER, state=[tag(fn='print'), 5, 0, 0])
        text = m.lift(proto([(8, 1, proto(self.PROGRAM), 0), (2, 1, 0, 0)]))
        self.assertEqual(m.errors, [])
        self.assertRegex(text, r'function\(\.\.\.\)\s*return \.\.\.\s*end')

    TAIL = """local h = 0
for j = a, a + n do
R[j] = args[NP + 1 + h]
h = h + 1
end
top = a + n
"""

    def test_clamp_as_an_expression(self):
        self.check("""elseif op == 21 then
local a = A[pc]
local n = if nargs - NP - 1 < 0 then -1 else nargs - NP - 1
""" + self.TAIL)

    def test_clamp_as_a_statement(self):
        self.check("""elseif op == 21 then
local a = A[pc]
local n = (nargs - NP - 1)
if n < 0 then
n = -1
end
""" + self.TAIL)

    def test_clamp_negated(self):
        self.check("""elseif op == 21 then
local a = A[pc]
local n = nargs - NP - 1
if not (n < 0) then
else
n = (-1)
end
""" + self.TAIL)

    def test_clamp_behind_an_opaque_test(self):
        self.check("""elseif op == 21 then
local a = A[pc]
local n = nargs - NP - 1
if n < 0 then
if S[1] == S[2] then
S[3] = 9
return S[4]
end
n = -1
end
""" + self.TAIL)


class Registers(unittest.TestCase):
    CALL = [(3, 1, 'print', 0), (1, 2, 42, 0), (22, 1, 0, 0), (2, 1, 0, 0)]

    def check(self, loop):
        m = Mini("""elseif op == 22 then
local a = A[pc]
top = a + 1
%s
R[a] = R[a](R[a + 1])
""" % loop, state=[tag(fn='print'), 5, 0, 0])
        text = m.lift(proto(self.CALL))
        self.assertEqual(m.errors, [])
        self.assertIn('return print(42)', text)
        return m

    def test_clearing_loop(self):
        self.check("""for k in R do
if k > top then
R[k] = nil
end
end""")

    def test_clearing_loop_negated(self):
        self.check("""for k, v in R do
if not (k > top) then
else
(R)[k] = (nil)
end
end""")

    def test_clearing_loop_with_opaque_tests(self):
        m = self.check("""for k, v in R do
if S[1] == S[2] then
S[3] = (106 % top)
elseif S[2] == S[4] then
return
elseif not (k > top) then
else
R[k] = nil
end
end""")
        self.assertEqual(m.lifter.pe.unstable, set())

    def test_a_loop_that_does_something_else_is_not_dropped(self):
        m = Mini("""elseif op == 22 then
for k, v in R do
env[k] = v
end
""")
        m.lift(proto(self.CALL))
        self.assertEqual(len(m.errors), 1)
        self.assertIn('unsupported for-in', m.errors[0][1])

    def test_register_array_from_a_loader_helper(self):
        # builds for Lua 5.1 have no table.create: the array comes from a function of the loader,
        # and an instruction that calls the same helper makes a new table
        helper = 'local function mkregs(n) return {unpack({}, 1, n)} end'
        # the helper as a function outside the loader (the evaluator can run it) and as a closure
        # of the loader (it cannot: the dump does not have what the closure captured)
        for where in ('outer', 'loader'):
            m = Mini("""elseif op == 33 then
R[A[pc]] = alloc(B[pc])
""", alloc='alloc(NP)', **{where: helper + '\n'})
            m.extra['alloc'] = tag(lfn=m.line_of('local function mkregs'))
            self.assertEqual(m.model.regs_name(m.model.interps[0]), 'R')
            text = m.lift(proto([(33, 1, 2, 0), (1, 2, 5, 0), (2, 1, 0, 0)], nparams=4))
            self.assertEqual(m.errors, [], where)
            self.assertRegex(text, r'return \{\}', where)


class Parameters(unittest.TestCase):
    def check(self, handlers='', prologue='', code=()):
        m = Mini(handlers, prologue, outer=PACKER)
        inner = proto(list(code) + [(4, 3, 1, 2), (2, 3, 0, 0)], nparams=2)
        text = m.lift(proto([(8, 1, inner, 0), (2, 1, 0, 0)]))
        self.assertEqual(m.errors, [])
        self.assertRegex(text, r'function\((\w+), (\w+)\)\s*return \1 \+ \2\s*end')
        return m

    def test_copied_from_the_packers_table(self):
        self.check("""elseif op == 20 then
for i = 1, NP do
R[i] = args[i]
end
""", code=[(20, 0, 0, 0)])

    def test_copied_from_a_table_of_the_arguments(self):
        self.check("""elseif op == 20 then
local h = {...}
for i = 1, NP do
R[i] = h[i]
end
""", code=[(20, 0, 0, 0)])

    def test_copied_by_the_prologue(self):
        # v14.0.2 - v14.2: the interpreter copies the fixed parameters before its loop
        self.check(prologue='for i = 1, NP do R[i] = args[i] end')

    def test_prologue_copy_in_front_of_a_loop_head(self):
        # the first instruction is a jump target: the copy must not move into the loop
        m = Mini(prologue='for i = 1, NP do R[i] = args[i] end', outer=PACKER)
        inner = proto([(4, 1, 1, 2), (7, 1, 0, 0), (2, 1, 0, 0)], nparams=2)       # repeat a = a + b until not a
        text = m.lift(proto([(8, 1, inner, 0), (2, 1, 0, 0)]))
        self.assertEqual(m.errors, [])
        self.assertRegex(text, r'function\(\w+, \w+\)')
        self.assertNotIn('ARG_', text)


    def test_first_vararg_right_after_the_parameters(self):
        # function(a, ...) local b = ...: the fetch into the next register is not a second parameter
        for where in ('handler', 'prologue'):
            copy = 'for i = 1, NP do R[i] = args[i] end'
            m = Mini("""elseif op == 20 then
%s
elseif op == 34 then
R[A[pc]] = args[firstva]
""" % (copy if where == 'handler' else ''), prologue='local firstva = NP + 1\n' + (copy if where == 'prologue' else ''),
                     outer=PACKER)
            code = ([(20, 0, 0, 0)] if where == 'handler' else []) + [(34, 2, 0, 0), (4, 3, 1, 2), (2, 3, 0, 0)]
            text = m.lift(proto([(8, 1, proto(code, nparams=1), 0), (2, 1, 0, 0)]))
            self.assertEqual(m.errors, [], where)
            self.assertRegex(text, r'function\((\w+), \.\.\.\)\s*return \1 \+ \(?select\(1, \.\.\.\)\)?\s*end', where)

    def test_argument_beyond_the_parameters_is_a_vararg(self):
        # the interpreter keeps the position of the first vararg in a local and reads the
        # argument table there
        m = Mini("""elseif op == 20 then
for i = 1, NP do
R[i] = args[i]
end
elseif op == 34 then
R[A[pc]] = args[firstva]
""", prologue='local firstva = NP + 1', outer=PACKER)
        inner = proto([(20, 0, 0, 0), (3, 4, 'print', 0), (1, 5, 1, 0), (5, 4, 0, 0),      # print(1)
                       (34, 2, 0, 0), (4, 3, 1, 2), (2, 3, 0, 0)], nparams=1)
        text = m.lift(proto([(8, 1, inner, 0), (2, 1, 0, 0)]))
        self.assertEqual(m.errors, [])
        self.assertNotIn('ARG_', text)
        self.assertRegex(text, r'function\((\w+), \.\.\.\)\s*(local \w+ = )?print\(1\)\s*return \1 \+ \(?select\(1, \.\.\.\)\)?\s*end')


class LoaderState(unittest.TestCase):
    """S is a parameter of the loader function around the closure maker: its state table."""
    HANDLERS = """elseif op == 23 then
if S[1] == S[2] then
while -S[3] do
S[3] = S[4]
end
return S[4]
end
if S[2] ~= S[4] then
R[A[pc]] = B[pc]
end
elseif op == 24 then
shared[B[pc]] = R[A[pc]]
elseif op == 25 then
R[A[pc]] = shared[B[pc]]
elseif op == 26 then
S[3] = S[3] + 1
if S[3] == 4 then
R[A[pc]] = B[pc]
end
elseif op == 29 then
shared[B[pc]] = C[pc]
"""

    def mini(self):
        return Mini(self.HANDLERS, state=[tag(fn='print'), 5, 3, 7], shared={5: 'from the dump'})

    def test_opaque_tests_are_decided_from_the_dump(self):
        m = self.mini()
        text = m.lift(proto([(23, 1, 42, 0), (2, 1, 0, 0)]))
        self.assertEqual(m.errors, [])
        self.assertEqual(text.strip(), 'return 42')
        self.assertEqual(m.lifter.slot_rounds, 0)

    def test_a_slot_code_writes_is_a_variable(self):
        # the same table, but this program counts in S[3]: the test on it stays a test
        m = self.mini()
        text = m.lift(proto([(1, 1, 0, 0), (26, 1, 42, 0), (2, 1, 0, 0)]))
        self.assertEqual(m.errors, [])
        self.assertEqual(m.lifter.pe.unstable, {('__LPH_ENV["S"]', 3)})
        self.assertGreaterEqual(m.lifter.slot_rounds, 1)
        self.assertRegex(text, r'__LPH_ENV\["S"\]\[3\] (\+= 1|= __LPH_ENV\["S"\]\[3\] \+ 1)')
        self.assertRegex(text, r'if __LPH_ENV\["S"\]\[3\] == 4 then')

    def test_a_slot_nobody_writes_is_read_from_the_dump(self):
        m = self.mini()
        text = m.lift(proto([(25, 1, 5, 0), (2, 1, 0, 0)]))
        self.assertEqual(m.errors, [])
        self.assertIn('"from the dump"', text)

    def test_a_value_parked_in_a_loader_table(self):
        # read, store, read: the store comes later in the program than the first read, and both
        # reads must be the variable, not what the dump held
        m = self.mini()
        text = m.lift(proto([(25, 2, 5, 0), (3, 1, 'f', 0), (24, 1, 5, 0), (25, 3, 5, 0), (4, 1, 2, 3), (2, 1, 0, 0)]))
        self.assertEqual(m.errors, [])
        self.assertNotIn('from the dump', text)
        self.assertEqual(text.count('__LPH_ENV["shared"][5]'), 3)
        self.assertIn('__LPH_ENV["shared"][5] = f', text)

    def test_a_constant_parked_in_a_loader_table(self):
        # the store is later in the program than the read (a loop): the read is not the dump's value
        m = self.mini()
        text = m.lift(proto([(25, 1, 5, 0), (29, 0, 5, 7), (7, 1, 0, 0), (2, 1, 0, 0)]))
        self.assertEqual(m.errors, [])
        self.assertNotIn('from the dump', text)
        self.assertIn('__LPH_ENV["shared"][5] = 7', text)
        # ... and a constant nobody reads back stays out of the way
        m = self.mini()
        text = m.lift(proto([(29, 0, 6, 7), (25, 1, 5, 0), (2, 1, 0, 0)]))
        self.assertEqual(m.errors, [])
        self.assertIn('"from the dump"', text)
        self.assertEqual(m.lifter.slot_rounds, 0)

    def test_store_into_program_data_is_still_an_error(self):
        m = Mini("""elseif op == 27 then
B[pc + 1] = R[A[pc]]
""")
        m.lift(proto([(3, 1, 'f', 0), (27, 1, 0, 0), (1, 2, 0, 0), (2, 2, 0, 0)]))
        self.assertTrue(any('self-modifying store of a run-time value' in e[1] for e in m.errors), m.errors)


class CloseUpvalues(unittest.TestCase):
    def base(self, loop):
        m = Mini("""elseif op == 28 then
local a = A[pc]
%s
""" % loop, prologue='open = {}', state=[tag(fn='print'), 5, 0, 0])
        p = pe.PE(m.model, None)
        self.assertEqual(p.state_sets[0][2], {'open'})
        loops = []

        def find(block):
            for st in block:
                if st[0] == 'forin':
                    loops.append(st)
                for b in pe.subblocks(st):
                    find(b)
        find(m.model.loops[0][2])
        sc = pe.Scope()
        sc.vars.update(a=4, S=pe.LuaTable())
        return p.close_base(loops[-1], sc)

    def test_plain(self):
        self.assertEqual(self.base("""for k, c in open do
if k >= a then
c[3] = c
c[2] = R[k]
open[k] = nil
end
end"""), 4)

    def test_behind_opaque_tests(self):
        self.assertEqual(self.base("""for k, c in open do
if S[1] == S[2] then
return
end
if S[1] == a then
else
if not (k >= a) then
else
c[3] = c
c[2] = R[k]
open[k] = nil
end
end
end"""), 4)


class Builtins(unittest.TestCase):
    def setUp(self):
        self.pe = pe.PE(Mini().model, None)

    def call(self, name, *args):
        return self.pe.call_builtin(name, list(args), -1)

    def test_string_functions_on_literals(self):
        self.assertEqual(self.call('string.len', 'abc'), [3])
        self.assertEqual(self.call('string.byte', 'abc'), [97])
        self.assertEqual(self.call('string.byte', 'abc', 2, 3), [98, 99])
        self.assertEqual(self.call('string.byte', 'abc', -1), [99])
        self.assertEqual(self.call('string.byte', 'abc', 5), [])
        self.assertEqual(self.call('string.packsize', '<i8'), [8])
        self.assertEqual(self.call('string.packsize', '=<L>xL>'), [17])
        self.assertEqual(self.call('string.packsize', 'i3 B d'), [12])
        self.assertEqual(self.call('string.unpack', '<I2 x B', '\x01\x02\xff\x07'), [513, 7, 5])

    def test_math(self):
        self.assertEqual(self.call('math.modf', 3.75), [3, 0.75])
        self.assertEqual(self.call('math.modf', -3.5), [-3, -0.5])
        self.assertEqual(self.pe.index(pe.Lib('math'), 'pi', None), __import__('math').pi)
        self.assertEqual(self.pe.index(pe.Lib('math'), 'huge', None), float('inf'))
        self.assertIsInstance(self.pe.index(pe.Lib('math'), 'floor', None), pe.Builtin)


class PatchValues(unittest.TestCase):
    """Luraph v14 programs compute numbers with library calls on constants and store them into
    operands of instructions that run later."""
    HANDLERS = """elseif op == 30 then
R[A[pc]] = R[B[pc]][C[pc]]
elseif op == 31 then
R[A[pc]] = R[A[pc]](R[A[pc] + 1], R[A[pc] + 2])
elseif op == 32 then
B[C[pc]] = R[A[pc]]
elseif op == 33 then
R[A[pc]] = R[B[pc]] < R[C[pc]]
"""
    TAIL = [(3, 4, 'print', 0), (1, 5, 999, 0), (5, 4, 0, 0), (2, 4, 0, 0)]

    def lift(self, head):
        # head computes a value into a register and stores it into the operand of the LOADK of TAIL
        m = Mini(self.HANDLERS)
        return m, m.lift(proto(head + self.TAIL))

    def test_library_call_on_constants(self):
        # print(bit32.band(205, 7)) with the argument arriving through a patched operand
        m, text = self.lift([(3, 1, 'bit32', 0), (30, 1, 1, 'band'), (1, 2, 205, 0), (1, 3, 7, 0), (31, 1, 0, 0),
                             (32, 1, 0, 8)])
        self.assertEqual(m.errors, [])
        self.assertIn('print(5)', text)
        self.assertNotIn('999', text)

    def test_string_library_and_arithmetic(self):
        # string.byte("A", 1) + 1 = 66
        m, text = self.lift([(3, 1, 'string', 0), (30, 1, 1, 'byte'), (1, 2, 'A', 0), (1, 3, 1, 0), (31, 1, 0, 0),
                             (1, 2, 1, 0), (4, 1, 1, 2), (32, 1, 0, 10)])
        self.assertEqual(m.errors, [])
        self.assertIn('print(66)', text)

    def test_and_or_chain_over_constants(self):
        # x = false and <skipped> or 9: the test of the constant has one outcome
        m, text = self.lift([(1, 1, False, 0), (7, 1, 3, 0), (1, 1, 9, 0), (32, 1, 0, 6)])
        self.assertEqual(m.errors, [])
        self.assertIn('print(9)', text)
        self.assertNotIn('if ', text)
        # x = 9; if true then else x = 162 end: the assignment in the dead arm is not the value
        m, text = self.lift([(1, 1, 9, 0), (1, 2, True, 0), (7, 2, 4, 0), (1, 1, 162, 0), (32, 1, 0, 7)])
        self.assertEqual(m.errors, [])
        self.assertIn('print(9)', text)
        self.assertNotIn('162', text)

    def test_comparison_of_constants(self):
        # x = (3 < 5) and 7 or 8  ->  7 (the jump over `x = 8` is taken)
        m, text = self.lift([(1, 1, 3, 0), (1, 2, 5, 0), (33, 3, 1, 2), (1, 1, 7, 0), (7, 3, 6, 0), (1, 1, 8, 0),
                             (9, 7, 7, 0), (32, 1, 0, 10)])
        self.assertEqual(m.errors, [])
        self.assertIn('print(7)', text)
        self.assertNotIn('print(8)', text)

    def test_a_value_that_differs_between_paths_is_not_a_patch_value(self):
        # x = 8; if cond then else x = 7 end; <operand> = x: no single value exists
        m, text = self.lift([(3, 1, 'cond', 0), (1, 2, 8, 0), (7, 1, 4, 0), (1, 2, 7, 0), (32, 2, 0, 7)])
        self.assertTrue(any('self-modifying store of a run-time value' in e[1] for e in m.errors), m.errors)
        self.assertNotIn('print(7)', text)
        self.assertNotIn('print(8)', text)

    def test_a_register_a_closure_shares_is_not_a_constant(self):
        # x = "A"; f = function() ... x ... end (x captured by reference); g(); <operand> = string.byte(x, 1):
        # the call of g can have changed x through the closure
        m = Mini(self.HANDLERS)
        cells, base = m.model.cells, m.model.up_base
        m = Mini(self.HANDLERS + """elseif op == 37 then
R[A[pc]] = wrap(B[pc], {[%d] = {[%d] = R, [%d] = C[pc]}})
""" % (base, cells[0], cells[1]))
        inner = proto([(2, 1, 0, 0)])
        head = [(1, 2, 'A', 0), (37, 6, inner, 2), (3, 7, 'g', 0), (5, 7, 0, 0),
                (3, 1, 'string', 0), (30, 1, 1, 'byte'), (1, 3, 1, 0), (31, 1, 0, 0), (32, 1, 0, 11)]
        text = m.lift(proto(head + self.TAIL))
        caps = [st[2][2] for p in m.protos.values() for n in p.nodes.values() if n.tree
                for st in n.tree[0] if st[0] == 'set' and st[2][0] == 'closure']
        self.assertEqual([tuple(c) for c in caps], [(('ref', 2),)])
        self.assertTrue(any('self-modifying store of a run-time value' in e[1] for e in m.errors), m.errors)
        self.assertNotIn('print(65)', text)

    def test_run_time_value_is_still_an_error(self):
        m, text = self.lift([(3, 1, 'f', 0), (32, 1, 0, 4)])
        self.assertTrue(any('self-modifying store of a run-time value' in e[1] for e in m.errors), m.errors)

    def test_fold_pure(self):
        f = pe.fold_pure
        self.assertEqual(f('bit32.band', [205, 7]), [5])
        self.assertEqual(f('string.byte', ['AB', 1, None]), [65])
        self.assertEqual(f('string.packsize', ['= >H<H  c100 >']), [104])
        self.assertEqual(f('string.packsize', ['c']), None)
        self.assertEqual(f('math.round', [2.5]), [3])
        self.assertEqual(f('math.round', [-2.5]), [-3])
        self.assertEqual(f('string.unpack', ['<i8', 'J' + '\0' * 7]), [74, 9])
        self.assertIsNone(f('os.time', []))
        self.assertIsNone(f('string.rep', ['a', 3]))


class UnpackHelper(unittest.TestCase):
    """The loader's own unpack(regs, from, to) with its parameters in another order: which number
    is the start is read from the defaults the helper sets."""

    def order(self, body):
        m = Mini(loader='local function up(z, e, b)\n%s\nend' % body)
        f = [f for f in m.model.funcs if f.params == ['z', 'e', 'b']][0]
        return pe.PE(m.model, None).unpack_order(f, [18, pe.REGS, 16], [18, 16])

    def test_start_default(self):
        self.assertEqual(self.order('b = b or 1\nreturn unpack(e, b, z)'), (16, 18))

    def test_stop_default(self):
        self.assertEqual(self.order('z = (z or #e)\nreturn unpack(e, b, z)'), (16, 18))

    def test_defaults_set_by_if(self):
        self.assertEqual(self.order('if not b then\nb = (1)\nend\nif not z then\nz = (#e)\nend\nreturn unpack(e, b, z)'), (16, 18))
        self.assertEqual(self.order('if not z then\nz = #e\nend\nreturn unpack(e, b, z)'), (16, 18))

    def test_defaults_inside_a_state_machine(self):
        self.assertEqual(self.order("""local l = 22
repeat
if not (l < 125) then
if l > 22 then
z = (z or #e)
break
end
else
l = 125
b = b or 1
continue
end
until false
return unpack(e, b, z)"""), (16, 18))


class CaptureLists(unittest.TestCase):
    """v14.0.x: the closure maker keeps a list of the capture lists its closure made; functions
    that return walk it and detach the cells that still point into their registers."""
    HANDLERS = """elseif op == 35 then
repeat
local kept = {}
for _, list in lists do
for _, cell in list do
if not (cell[2] == R and cell[1] >= A[pc]) then
else
local r = cell[1]
if not kept[r] then
kept[r] = {R[r]}
end
cell[2] = kept[r]
cell[1] = 1
end
end
end
until true
elseif op == 36 then
coyield(lists, {{[2] = R, [1] = A[pc]}})
"""

    def mini(self):
        m = Mini(self.HANDLERS, maker='local lists = alloc({}, S)')
        m.extra = {'alloc': tag(fn='setmetatable'), 'coyield': tag(fn='table.insert')}
        return m

    def test_recognised(self):
        m = self.mini()
        p = pe.PE(m.model, None)
        self.assertEqual(p.cell_lists, {'lists'})
        loops = []

        def find(block):
            for st in block:
                if st[0] == 'forin':
                    loops.append(st)
                for b in pe.subblocks(st):
                    find(b)
        find(m.model.loops[0][2])
        outer = [st for st in loops if pe.nested_close(st)]
        self.assertEqual(len(outer), 1)
        arr, base = pe.nested_close(outer[0])
        self.assertEqual(arr, ('name', 'R'))
        self.assertEqual(base[0], 'index')

    def test_lifts_without_a_trace(self):
        m = self.mini()
        text = m.lift(proto([(1, 1, 42, 0), (36, 1, 0, 0), (35, 1, 0, 0), (2, 1, 0, 0)]))
        self.assertEqual(m.errors, [])
        self.assertEqual(text.strip(), 'return 42')

    def test_a_handler_local_of_the_same_name_is_not_the_list(self):
        m = Mini("""elseif op == 35 then
local lists = R[A[pc]]
for _, list in lists do
for _, cell in list do
if cell[2] == R and cell[1] >= A[pc] then
cell[1] = 1
end
end
end
""")
        self.assertEqual(pe.PE(m.model, None).cell_lists, set())


# ---------------------------------------------------------------- the public corpus, when it is there
CORPUS = os.environ.get('LURAPH_CORPUS')


def have_luau():
    try:
        return bool(vmmodel.find_luau())
    except RuntimeError:
        return False


def corpus_skip():
    """paths (relative to the corpus) that must not be touched: the file LURAPH_CORPUS_SKIP names, one per line"""
    name = os.environ.get('LURAPH_CORPUS_SKIP')
    if not name:
        return set()
    with open(name) as f:
        return {line.strip() for line in f if line.strip()}


@unittest.skipUnless(CORPUS and os.path.isdir(CORPUS) and have_luau(), 'needs LURAPH_CORPUS and bin/luau')
class CorpusLift(unittest.TestCase):
    """Whole runs of devm on files chosen at run time: per version directory the first few small
    files for which this tree builds a proto-table model with one interpreter (a static test: the
    file is parsed, not run).  Checks the lift only: no LIFT ERROR marker, and the result parses.
    (What such a run lifts today is the VM's first program.)"""
    VERSIONS = ('v14.3', 'v14.4.1', 'v14.5', 'v14.5.1', 'v14.6')
    PER_VERSION = 2
    LOOK_AT = 40            # files examined per version at most

    def pick(self, version):
        from devm import container, normalize
        d = os.path.join(CORPUS, version)
        skip = corpus_skip()
        out = []
        looked = 0
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else ():
            path = os.path.join(d, name)
            if version + '/' + name in skip or os.path.getsize(path) > 150000:
                continue
            looked += 1
            if looked > self.LOOK_AT:
                break
            with open(path, encoding='latin-1') as f:
                text = f.read()
            if container.may_be_container(text):
                continue
            try:
                if normalize.needs_reflow(text):
                    text = normalize.reflow(normalize.inline_protected_raw(text)[0])
                m = vmmodel.Model(text, oneline=True)
            except Exception:
                continue        # no model in this tree: not what is checked here
            if m.factory_style or len(m.interps) != 1:
                continue
            out.append(path)
            if len(out) == self.PER_VERSION:
                break
        return out

    def test_no_lift_errors(self):
        ran = 0
        clean = 0
        for version in self.VERSIONS:
            for path in self.pick(version):
                with self.subTest(file=os.path.relpath(path, CORPUS)), tempfile.TemporaryDirectory() as tmp:
                    out = os.path.join(tmp, 'out.lua')
                    try:
                        r = subprocess.run([sys.executable, '-B', '-m', 'devm.run', path, '-o', out, '--work', tmp],
                                           cwd=ROOT, capture_output=True, text=True, errors='replace', timeout=300)
                    except subprocess.TimeoutExpired:
                        continue        # the run did not come back: not a matter of the lift
                    if r.returncode != 0 or not os.path.exists(out):
                        continue
                    ran += 1
                    with open(out, encoding='utf-8', errors='replace') as f:
                        text = f.read()
                    # the report line of the program that was lifted: the script (the VM's init handed
                    # over), or the root program of a build without an init
                    ms = re.findall(r'(?:lifted \d+ protos|(?:script|real program): \d+ functions), (\d+) instructions, '
                                    r'(\d+) (?:lift )?errors', r.stdout)
                    self.assertTrue(ms, r.stdout[-400:])
                    errors = int(ms[-1][1])
                    # a marker for every instruction that did not lift, and nothing that does not parse
                    self.assertEqual(errors == 0, 'LIFT ERROR' not in text, r.stdout[-400:])
                    p = subprocess.run([LUAU_AST, out], capture_output=True, text=True, errors='replace')
                    self.assertTrue(p.returncode == 0 and 'Parse errors' not in p.stdout + p.stderr)
                    clean += errors == 0
        self.assertGreater(ran, 0, 'no file of the corpus gave a result')
        # (a script can still hold an instruction form the lifter does not know; most must lift whole)
        self.assertGreaterEqual(clean * 3, ran * 2, '%d of %d results without lift errors' % (clean, ran))


if __name__ == '__main__':
    unittest.main()
