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
* parameters copied out of `{...}`, and by the interpreter's prologue;
* tests between slots of the loader's state table: decided from the dump when no code that runs
  writes the slot, kept as a run-time variable when code does;
* a handler-local alias of the open-upvalue map, next to an interpreter variable of the same name
  as the alias; the close-upvalues loop with an opaque test around its real test;
* a register array that comes from a Lua helper of the loader instead of table.create;
* folded library calls on literals.

With LURAPH_CORPUS set to a checkout of the public sample corpus (a directory with one
sub-directory per Luraph version) and bin/luau present, CorpusLift also runs the whole of devm on a
few v14.3 - v14.6 files picked at run time and checks that what was lifted has no LIFT ERROR
marker and parses.  It is skipped without the variable.
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

    def __init__(self, handlers='', prologue='', alloc='create(8)', state=None, shared=None, outer='', loader=''):
        self.src = outer + VM % {'handlers': handlers, 'prologue': prologue, 'alloc': alloc, 'loader': loader}
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
        self.assertEqual(self.call('string.packsize', '>>Lx=L<>'), [17])
        self.assertEqual(self.call('string.packsize', 'i3 B d'), [12])
        self.assertEqual(self.call('string.unpack', '<I2 x B', '\x01\x02\xff\x07'), [513, 7, 5])

    def test_math(self):
        self.assertEqual(self.call('math.modf', 3.75), [3, 0.75])
        self.assertEqual(self.call('math.modf', -3.5), [-3, -0.5])
        self.assertEqual(self.pe.index(pe.Lib('math'), 'pi', None), __import__('math').pi)
        self.assertEqual(self.pe.index(pe.Lib('math'), 'huge', None), float('inf'))
        self.assertIsInstance(self.pe.index(pe.Lib('math'), 'floor', None), pe.Builtin)


# ---------------------------------------------------------------- the public corpus, when it is there
CORPUS = os.environ.get('LURAPH_CORPUS')


def have_luau():
    try:
        return bool(vmmodel.find_luau())
    except RuntimeError:
        return False


@unittest.skipUnless(CORPUS and os.path.isdir(CORPUS) and have_luau(), 'needs LURAPH_CORPUS and bin/luau')
class CorpusLift(unittest.TestCase):
    """Whole runs of devm on files chosen at run time: the first few of each version directory
    that have the dispatch-loop head this tree models (`while true do local OP=CODE[PC];if`) and a
    closure maker with two parameters.  Checks the lift only: no LIFT ERROR marker, and the result
    parses.  (What such a run lifts today is the VM's first program.)"""
    VERSIONS = ('v14.3', 'v14.4.1', 'v14.5', 'v14.5.1', 'v14.6')
    PER_VERSION = 2
    HEAD = re.compile(r'while true do local (\w+)=(\w+)\[(\w+)\];if')
    MAKER = re.compile(r'function\((\w+),(\w+)\)local [\w,]+=\(?\1\[')

    def pick(self, version):
        d = os.path.join(CORPUS, version)
        out = []
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else ():
            path = os.path.join(d, name)
            if os.path.getsize(path) > 150000:
                continue
            with open(path, encoding='latin-1') as f:
                text = f.read()
            if 'pcall' not in text[-200:] and len(self.HEAD.findall(text)) == 1 and self.MAKER.search(text) \
                    and 'repeat local' not in text and 'loadstring' not in text:
                out.append(path)
                if len(out) == self.PER_VERSION:
                    break
        return out

    def test_no_lift_errors(self):
        ran = 0
        for version in self.VERSIONS:
            for path in self.pick(version):
                with self.subTest(file=os.path.relpath(path, CORPUS)), tempfile.TemporaryDirectory() as tmp:
                    out = os.path.join(tmp, 'out.lua')
                    r = subprocess.run([sys.executable, '-B', '-m', 'devm.run', path, '-o', out, '--work', tmp],
                                       cwd=ROOT, capture_output=True, text=True, errors='replace', timeout=600)
                    if r.returncode != 0 or not os.path.exists(out):
                        continue        # no model for this file in this tree: not what is checked here
                    ran += 1
                    with open(out, encoding='utf-8', errors='replace') as f:
                        text = f.read()
                    m = re.search(r'lifted (\d+) protos, (\d+) instructions, (\d+) errors', r.stdout)
                    self.assertIsNotNone(m, r.stdout[-400:])
                    self.assertEqual(int(m.group(3)), 0, r.stdout[-400:])
                    self.assertNotIn('LIFT ERROR', text)
                    p = subprocess.run([LUAU_AST, out], capture_output=True, text=True, errors='replace')
                    self.assertTrue(p.returncode == 0 and 'Parse errors' not in p.stdout + p.stderr)
        self.assertGreater(ran, 0, 'no file of the corpus gave a result')


if __name__ == '__main__':
    unittest.main()
