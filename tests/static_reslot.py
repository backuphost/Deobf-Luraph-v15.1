#!/usr/bin/env python3
"""Slot names of reslot.py (comment form and --inline) must never change what a program does.

    python3 tests/static_reslot.py [--n 300] [--seed 1]

Random small programs are built around a slot table `local T = {}`: locals that shadow each
other, slot writes (once, twice, inside loops and functions), slot reads at every nesting
depth, reassigned locals, tables that escape.  Each program is run by bin/luau three times:
as written, with slot comments, and with `--inline` substitution.  All three must print the
same lines.  The directed cases are the shapes that went wrong before (a substituted name
captured by an inner local or by a parameter of the same name).

A comment cannot change what a program prints, so the comment form gets a second check: is
the comment TRUE?  `T[N --[[name]]]` says that the only assignment to slot N the visible code
makes is `T[N] = name`.  The program is run once more with T replaced by a proxy that records
the line of the statement that wrote each slot last; at every commented read that line must
be the line of `T[N] = name` (or the slot is still unwritten).  The generator writes T
through other names as well (`local alias = T`, a function parameter, a table that holds T,
a computed index, a method of a visible object): a comment on such a table would be caught.
The ALIAS cases are the same shapes written out, plus the ones a proxy cannot see
(table.insert, rawset): there the table must get no slot name at all.

Needs bin/luau and bin/luau-ast (./setup.sh).  Exit status 0 when everything passes.
"""
import os
import random
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'libs'))
sys.path.insert(0, ROOT)
import luaubin                 # noqa: E402
import reslot                  # noqa: E402

DIRECTED = {
    'shadow_block': '''
local B = {}
local s = "outer"
B[1] = s
do
    local s = "inner"
    print(B[1], s)
end
print(B[1])
''',
    'shadow_param': '''
local B = {}
local s = "outer"
B[1] = s
local function f(s) return B[1] end
print(f("param"), B[1])
''',
    'shadow_for': '''
local B = {}
local i = "outer"
B[2] = i
for i = 1, 2 do print(B[2], i) end
for i, v in ipairs({"x"}) do print(B[2], i, v) end
''',
    'shadow_later_same_scope': '''
local B = {}
local s = "first"
B[1] = s
local s = "second"
print(B[1], s)
''',
    'written_twice': '''
local B = {}
local s = "one"
B[1] = s
print(B[1])
B[1] = nil
print(B[1])
''',
    'write_in_loop': '''
local B = {}
for k = 1, 2 do
    local s = "it" .. k
    B[1] = s
    print(B[1])
end
print(B[1])
''',
    'escapes': '''
local B = {}
local s = "one"
B[1] = s
local function poke(t) t[1] = "poked" end
poke(B)
print(B[1])
''',
    'method_self': '''
local B = {}
local obj = {name = "outer"}
function obj:set() B[1] = self end
function obj:get() local inner = {name = "inner"} function inner:read() return B[1] end return inner:read() end
obj:set()
print(obj:get().name)
''',
}

# The slot table is written under another name: no read of it may get a slot name.
ALIAS = {
    'alias_local': 'local alias = C\nalias[1] = "changed"',
    'alias_assign': 'local alias\nalias = C\nalias[1] = "changed"',
    'alias_param': 'local function poke(t) t[1] = "changed" end\npoke(C)',
    'alias_pcall': 'pcall(function(t) t[1] = "changed" end, C)',
    'alias_boxed': 'local box = {C}\nbox[1][1] = "changed"',
    'alias_field': 'local holder = {}\nholder.t = C\nholder.t[1] = "changed"',
    'alias_method': 'local o = {}\nfunction o:set(l) l[1][1] = "changed" end\no:set({C})',
    'alias_insert': 'table.remove(C, 1)\ntable.insert(C, "changed")',
    'alias_rawset': 'rawset(C, 1, "changed")',
    'alias_computed': 'local i = 1\nC[i] = "changed"',
    'alias_returned': 'local function get() return C end\nget()[1] = "changed"',
    'alias_reassigned': 'C = {}\nC[2] = "other"',
    'alias_upvalue_copy': 'local function grab() local c2 = C; c2[1] = "changed" end\ngrab()',
}
ALIAS_FRAME = 'local C = {}\nlocal w = "first"\nC[1] = w\n%s\nprint(C[1], w)\n'
# Shapes that keep the name: T is only indexed, or handed to a method of a parameter in a
# capture list (how Luraph gives captured variables to a closure the VM creates).
KEEP = {
    'keep_plain': 'local C = {}\nlocal w = "first"\nC[1] = w\nprint(C[1], w)\n',
    'keep_dynamic_read': 'local C = {}\nlocal w = "first"\nC[1] = w\nlocal i = 1\nprint(C[i], C[1], w)\n',
    'keep_string_key': 'local C = {}\nlocal w = "first"\nC[1] = w\nC.note = "x"\nprint(C[1], w)\n',
    'keep_handed_to_param': ('local function part(W)\n    local C = {}\n    local w = "first"\n    C[1] = w\n'
                             '    local f = W:kk(1, 2, {[1] = C})\n    print(C[1], w)\nend\n'
                             'part({kk = function() end})\n'),
}

NAMES = ['a', 'b', 's']

# one line: line numbers of the program stay what they were
WATCH = ('local __last = {} local function __watch() local store = {} return setmetatable({}, {__index = store, '
         '__newindex = function(_, k, v) store[k] = v; __last[k] = debug.info(2, "l") end, '
         '__len = function() return #store end}) end '
         'local function __chk(v, n, line) if __last[n] ~= nil and __last[n] ~= line then '
         'print("WRONG COMMENT: slot " .. n .. " was last written at line " .. tostring(__last[n]) '
         '.. ", the comment names the assignment at line " .. line) end return v end ')


def comment_truth_program(text, commented):
    """`text`: a generated program (slot table `T` declared on line 1, one statement per
    line); `commented`: reslot's comment form of it.  Returns the program that checks every
    slot comment while it runs, or None when there is no comment to check."""
    sites = {}
    for i, ln in enumerate(text.split('\n'), 1):
        m = re.match(r'\s*T\[(\d+)\] = ', ln)
        if m:
            sites.setdefault(int(m.group(1)), []).append(i)
    count = [0]

    def sub(m):
        n = int(m.group(1))
        count[0] += 1
        return '__chk(T[%d], %d, %d)' % (n, n, sites[n][0])
    body = re.sub(r'T\[(\d+) --\[\[\w+\]\]\]', sub, commented)
    first, rest = body.split('\n', 1)
    if not count[0] or first != 'local T = {}':
        return None
    return WATCH + 'local T = __watch()\n' + rest


class Gen:
    def __init__(self, rng):
        self.rng = rng
        self.lit = 0
        self.fn = 0
        self.written = set()
        self.lines = []

    def emit(self, depth, text):
        self.lines.append('    ' * depth + text)

    def value(self):
        self.lit += 1
        return '"v%d"' % self.lit

    def block(self, depth, scope, budget):
        """scope: names visible here (the generator only uses visible names)."""
        rng = self.rng
        scope = list(scope)
        for _ in range(rng.randint(2, 6)):
            if budget[0] <= 0:
                return
            budget[0] -= 1
            r = rng.random()
            slot = rng.choice(sorted(self.written)) if (self.written and rng.random() < 0.8) \
                else rng.randint(1, 6)
            if r < 0.20:
                nm = rng.choice(NAMES)
                self.emit(depth, 'local %s = %s' % (nm, self.value()))
                if nm not in scope:
                    scope.append(nm)
            elif r < 0.38 and scope:
                # mostly a slot that was not written yet: single-assignment slots are the
                # ones reslot names
                free = [k for k in range(1, 7) if k not in self.written]
                if free and rng.random() < 0.85:
                    slot = rng.choice(free)
                self.written.add(slot)
                self.emit(depth, 'T[%d] = %s' % (slot, rng.choice(scope)))
            elif r < 0.58:
                extra = (', ' + rng.choice(scope)) if scope and rng.random() < 0.5 else ''
                self.emit(depth, 'print(T[%d]%s)' % (slot, extra))
            elif r < 0.66 and depth < 4:
                self.emit(depth, 'do')
                self.block(depth + 1, scope, budget)
                self.emit(depth, 'end')
            elif r < 0.72 and depth < 4:
                self.emit(depth, 'for i = 1, 2 do')
                self.block(depth + 1, scope + ['i'], budget)
                self.emit(depth, 'end')
            elif r < 0.80 and depth < 4:
                self.fn += 1
                name = 'fn%d' % self.fn
                param = rng.choice(NAMES + ['p'])
                self.emit(depth, 'local function %s(%s)' % (name, param))
                self.block(depth + 1, scope + [param], budget)
                self.emit(depth + 1, 'return T[%d]' % slot)
                self.emit(depth, 'end')
                self.emit(depth, 'print(%s(%s))' % (name, self.value()))
            elif r < 0.86 and depth < 4:
                self.emit(depth, 'if T[%d] then' % slot)
                self.block(depth + 1, scope, budget)
                self.emit(depth, 'end')
            elif r < 0.88 and scope:
                self.emit(depth, '%s = %s' % (rng.choice(scope), self.value()))
            elif r < 0.90:
                self.emit(depth, 'T[%d] = nil' % slot)
            elif r < 0.935:
                # T written under another name (one line each, so that the line of the write
                # is known): every one of these must take the slot names of T away
                v = self.value()
                self.emit(depth, rng.choice((
                    'local alias = T; alias[%d] = %s',
                    'local function pk(t) t[%d] = %s end pk(T)',
                    'local box = {T}; box[1][%d] = %s',
                    'local _ = (function(t) t[%d] = %s end)(T)',
                    'pcall(function(t) t[%d] = %s end, T)',
                    'local o = {} function o:set(l) l[1][%d] = %s end o:set({T})',
                    'local idx = %d; T[idx] = %s',
                )) % (slot, v))
            elif r < 0.95:
                self.emit(depth, 'print(#T, T[%d] == nil)' % slot)
            else:
                self.emit(depth, 'print(T[%d] == nil, type(T[%d]))' % (slot, slot))

    def program(self):
        self.emit(0, 'local T = {}')
        self.block(0, [], [self.rng.randint(8, 30)])
        self.emit(0, 'print(T[1], T[2], T[3], T[4], T[5], T[6])')
        return '\n'.join(self.lines) + '\n'


def run(luau, text, tmpdir):
    path = os.path.join(tmpdir, 'p.lua')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)
    r = subprocess.run([luau, path], capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout + r.stderr.replace(path, 'p.lua')


def main():
    n, seed = 300, 1
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == '--n':
            n = int(argv[i + 1])
        elif a == '--seed':
            seed = int(argv[i + 1])
    luau, ast = luaubin.find_luau(), luaubin.find_luau_ast()
    if not luau or not ast:
        print('needs luau and luau-ast: ' + luaubin.MISSING_HINT)
        return 2
    failures = checks = 0
    inlined = commented = truth_runs = 0

    def check(name, cond, detail=''):
        nonlocal failures, checks
        checks += 1
        if not cond:
            failures += 1
            print('FAIL', name, detail)

    rng = random.Random(seed)
    programs = list(DIRECTED.items())
    for k in range(n):
        programs.append(('random%d' % k, Gen(rng).program()))

    with tempfile.TemporaryDirectory(prefix='static_reslot_') as tmp:
        for name, text in programs:
            rc0, out0 = run(luau, text, tmp)
            check(name + ': original runs', rc0 == 0, out0[:200] + '\n' + text)
            if rc0 != 0:
                continue
            for inline in (False, True):
                tag = ' (--inline)' if inline else ' (comments)'
                try:
                    new, stats = reslot.process_text(text, ast_bin=ast, inline=inline)
                except Exception as e:      # noqa: BLE001
                    check(name + ': reslot runs' + tag, False, repr(e))
                    continue
                inlined += stats['inline']
                commented += stats['comment']
                rc1, out1 = run(luau, new, tmp)
                check(name + ': prints the same' + tag, (rc1, out1) == (rc0, out0),
                      '\n--- input\n%s--- output\n%s--- printed %r, expected %r' % (text, new, out1, out0))
                if inline and stats.get('inline_rejected'):
                    check(name + ': no substitution had to be taken back', False,
                          stats['inline_rejected'])
                if not inline and name.startswith('random'):
                    prog = comment_truth_program(text, new)
                    if prog is not None:
                        truth_runs += 1
                        rc2, out2 = run(luau, prog, tmp)
                        check(name + ': every slot comment names the assignment that wrote the slot',
                              (rc2, out2) == (rc0, out0),
                              '\n--- input\n%s--- commented\n%s--- printed %r, expected %r'
                              % (text, new, out2, out0))

    # the shapes that went wrong before: nothing may be substituted where the name is shadowed
    new, stats = reslot.process_text(DIRECTED['shadow_block'], ast_bin=ast, inline=True)
    check('shadow_block: inner read untouched', 'print(B[1], s)' in new and stats['inline'] == 1, new)
    new, stats = reslot.process_text(DIRECTED['shadow_param'], ast_bin=ast, inline=True)
    check('shadow_param: read under the parameter untouched', 'return B[1] end' in new, new)
    new, stats = reslot.process_text(DIRECTED['shadow_block'], ast_bin=ast, inline=False)
    check('shadow_block: no misleading comment', 'print(B[1], s)' in new, new)
    new, stats = reslot.process_text(DIRECTED['written_twice'], ast_bin=ast, inline=True)
    check('written_twice: no name at all', new == DIRECTED['written_twice'], new)
    new, stats = reslot.process_text(DIRECTED['escapes'], ast_bin=ast, inline=True)
    check('escapes: no name at all (the table is passed to a function that writes it)',
          stats['inline'] == 0 and stats['comment'] == 0 and new == DIRECTED['escapes'], new)

    # a table that is written under another name gets no slot name, in either form
    with tempfile.TemporaryDirectory(prefix='static_reslot_') as tmp:
        for name, middle in sorted(ALIAS.items()):
            text = ALIAS_FRAME % middle
            rc0, out0 = run(luau, text, tmp)
            check(name + ': original runs', rc0 == 0, out0[:200])
            for inline in (False, True):
                new, stats = reslot.process_text(text, ast_bin=ast, inline=inline)
                check('%s: no slot name%s' % (name, ' (--inline)' if inline else ''),
                      new == text and stats['comment'] == 0 and stats['inline'] == 0, new)
        for name, text in sorted(KEEP.items()):
            rc0, out0 = run(luau, text, tmp)
            check(name + ': original runs', rc0 == 0, out0[:200])
            new, stats = reslot.process_text(text, ast_bin=ast, inline=False)
            check(name + ': the slot read is named', stats['comment'] >= 1 and 'C[1 --[[w]]]' in new, new)
            rc1, out1 = run(luau, new, tmp)
            check(name + ': prints the same', (rc1, out1) == (rc0, out0), new)
            new, stats = reslot.process_text(text, ast_bin=ast, inline=True)
            rc1, out1 = run(luau, new, tmp)
            check(name + ': prints the same (--inline)', (rc1, out1) == (rc0, out0), new)
            if name == 'keep_handed_to_param':
                check(name + ': not substituted (a closure the VM creates may write the slot)',
                      stats['inline'] == 0 and stats['comment'] >= 1, new)

    print('%d programs (%d random); %d substitutions and %d comments made; '
          '%d programs had comments and were run with the write-recording proxy'
          % (len(programs), n, inlined, commented, truth_runs))
    print('%d checks, %d failure(s)' % (checks, failures))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
