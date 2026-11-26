"""Readability rewrites on the structured AST (after structuring, before `local` insertion).

All rewrites preserve behaviour:
  * `x = A; if x then x = B end; if not x then x = C end`  ->  `x = A and B or C`
  * a value that is assigned once and used once in the very next statement is inlined
  * variables that only ever hold nil are replaced by nil
  * bare `return` in tail position is dropped, negated if/else is flipped
"""
import re

from .ircfg import walk_expr, map_expr

BLOCK_KINDS = ('if', 'while', 'repeat', 'fornum', 'forin')


def sub_blocks(st):
    t = st[0]
    if t == 'if':
        return [st[2]] + ([st[3]] if st[3] else [])
    if t in ('while',):
        return [st[2]]
    if t == 'repeat':
        return [st[1]]
    if t in ('fornum', 'forin'):
        return [st[3]]
    return []


def head_exprs(st):
    """Expressions evaluated by the statement itself (not by nested blocks), in order."""
    t = st[0]
    if t == 'assign':
        out = []
        for d in st[1]:
            if d[0] != 'r':
                out.append(d)
        return out + list(st[2])
    if t in ('call', 'call_discard'):
        return [st[1]]
    if t == 'return':
        return list(st[1])
    if t == 'if' or t == 'while':
        return [st[1]]
    if t == 'repeat':
        return [st[2]]
    if t in ('fornum', 'forin'):
        return list(st[2])
    if t == 'setlist':
        return [st[1]] + list(st[3]) + ([st[4]] if st[4] else [])
    if t == 'prep':
        return list(st[2])
    return []


def set_head_exprs(st, fn):
    t = st[0]
    if t == 'assign':
        st[1] = [d if d[0] == 'r' else map_expr(d, fn) for d in st[1]]
        st[2] = [map_expr(v, fn) for v in st[2]]
    elif t in ('call', 'call_discard'):
        st[1] = map_expr(st[1], fn)
    elif t == 'return':
        st[1] = [map_expr(v, fn) for v in st[1]]
    elif t == 'if' or t == 'while':
        st[1] = map_expr(st[1], fn)
    elif t == 'repeat':
        st[2] = map_expr(st[2], fn)
    elif t in ('fornum', 'forin'):
        st[2] = [map_expr(v, fn) for v in st[2]]
    elif t == 'setlist':
        st[1] = map_expr(st[1], fn)
        st[3] = [map_expr(v, fn) for v in st[3]]
        if st[4]:
            st[4] = map_expr(st[4], fn)


def defs_of(st):
    t = st[0]
    if t == 'assign':
        return [d[1] for d in st[1] if d[0] == 'r']
    if t == 'fornum':
        return [st[1]]
    if t == 'forin':
        return list(st[1])
    return []


class Counts:
    def __init__(self, ast):
        self.defs = {}
        self.uses = {}
        self.caps = {}
        self.scan(ast)

    def scan(self, block):
        for st in block:
            for n in defs_of(st):
                self.defs[n] = self.defs.get(n, 0) + 1
            for e in head_exprs(st):
                for x in walk_expr(e, []):
                    if x[0] == 'r':
                        self.uses[x[1]] = self.uses.get(x[1], 0) + 1
                    elif x[0] == 'closure':
                        for c in x[2]:
                            if isinstance(c[1], tuple) and c[1][0] == 'r':
                                self.caps[c[1][1]] = self.caps.get(c[1][1], 0) + 1
            for b in sub_blocks(st):
                self.scan(b)


def has(e, kinds):
    return any(x[0] in kinds for x in walk_expr(e, []))


# ------------------------------------------------------------------ passes
def strip_tail_returns(block):
    """A bare `return` at the end of a function (or of an if-arm in tail position) is noise."""
    while block and block[-1][0] == 'return' and not block[-1][1]:
        block.pop()
    if block and block[-1][0] == 'if':
        st = block[-1]
        strip_tail_returns(st[2])
        if st[3]:
            strip_tail_returns(st[3])
            if not st[3]:
                st[3] = None
        if not st[2] and st[3]:
            st[1] = ('un', 'not', st[1])
            st[2], st[3] = st[3], None


def is_assign_to(st, name):
    return st[0] == 'assign' and len(st[1]) == 1 and st[1][0] == ('r', name) and len(st[2]) == 1


def and_or(block):
    for st in block:
        for b in sub_blocks(st):
            and_or(b)
    i = 0
    while i < len(block) - 1:
        a, b = block[i], block[i + 1]
        if a[0] == 'assign' and len(a[1]) == 1 and a[1][0][0] == 'r' and len(a[2]) == 1 and b[0] == 'if' and not b[3] \
                and len(b[2]) == 1 and is_assign_to(b[2][0], a[1][0][1]) and a[2][0][0] not in ('closure', 'va', 'mvall'):
            x = a[1][0][1]
            val = b[2][0][2][0]
            if val[0] in ('closure',) or any(y == ('r', x) for y in walk_expr(val, [])):
                i += 1
                continue
            if b[1] == ('r', x):
                a[2] = [('and', a[2][0], val)]
                del block[i + 1]
                continue
            if b[1] == ('un', 'not', ('r', x)):
                a[2] = [('or', a[2][0], val)]
                del block[i + 1]
                continue
        i += 1


# Two more rewrites, written for Luraph v15.2 output (register set-up that both arms of an `if`
# repeat, and the `x = c or E` diamond).  Off by default: the committed v15.1 results are compared
# text for text by tests/regress.py; run.py switches them on for the v15.2 generation.
MERGE_ARMS = False


def _mentions(node, name):
    """Does the variable occur anywhere in the fragment: read, written, captured, loop variable?
    (A string constant that happens to spell the name counts too: the answer errs towards yes.)"""
    if isinstance(node, str):
        return node == name
    if isinstance(node, (list, tuple)):
        return any(_mentions(x, name) for x in node)
    return False


def _escapes(block):
    """break / continue / return anywhere inside: what follows the block is then not always reached"""
    for st in block:
        if st[0] in ('break', 'continue', 'return', 'goto'):
            return True
        for x in st[1:]:
            if isinstance(x, list) and x and isinstance(x[0], list) and _escapes(x):
                return True
    return False


def _plain_cond(c):
    return c[0] in ('r', 'k') or c[0] == 'un' and c[1] == 'not' and c[2][0] in ('r', 'k')


def sink_common(block, caps):
    """`if c then x = K; A else x = K; B end`  ->  `if c then A else B end; x = K`

    for a variable that neither arm uses otherwise and a value that neither arm changes (a constant
    or another variable): set-up both paths share, moved next to the statement that uses it.
    `caps`: variables some closure captured (a call in an arm could read or write those)."""
    for st in block:
        for b in sub_blocks(st):
            sink_common(b, caps)
    i = 0
    while i < len(block):
        st = block[i]
        i += 1
        if st[0] != 'if' or not st[2] or not st[3] or _escapes(st[2]) or _escapes(st[3]):
            continue
        moved = []
        for s in list(st[2]):
            if not (s[0] == 'assign' and len(s) == 3 and len(s[1]) == 1 and s[1][0][0] == 'r'
                    and len(s[2]) == 1 and s[2][0][0] in ('k', 'r')):
                continue
            x, v = s[1][0][1], s[2][0]
            if caps.get(x) or v[0] == 'r' and (v[1] == x or caps.get(v[1])):
                continue
            if sum(1 for o in st[2] if o == s) != 1 or sum(1 for o in st[3] if o == s) != 1:
                continue
            rest = [o for o in st[2] if o != s] + [o for o in st[3] if o != s]
            if _mentions(rest, x):
                continue
            if v[0] == 'r' and v in _targets(rest, set()):
                continue
            if len(st[2]) == 1 and len(st[3]) == 1 and not _plain_cond(st[1]):
                continue        # (the test would be left with nothing to guard: keep it as it is)
            st[2] = [o for o in st[2] if o != s]
            st[3] = [o for o in st[3] if o != s]
            moved.append(s)
        if not moved:
            continue
        at = i          # (the statement after the `if`)
        if not st[2] and not st[3]:
            del block[i - 1]
            at = i - 1
        elif not st[2]:
            c = st[1]
            st[1] = c[2] if c[0] == 'un' and c[1] == 'not' else ('un', 'not', c)
            st[2], st[3] = st[3], None
        elif not st[3]:
            st[3] = None
        block[at:at] = moved
        i = at + len(moved)


def copy_diamonds(block):
    """`if c then x = c else x = E end` -> `x = c or E`;  `if c then x = E else x = c end` ->
    `x = c and E`  (c a plain variable; with `not c` the arms change places).  Exact: the variable
    is read once more at most, which nothing can observe."""
    for st in block:
        for b in sub_blocks(st):
            copy_diamonds(b)
    for i, st in enumerate(block):
        if st[0] != 'if' or not st[3] or len(st[2]) != 1 or len(st[3]) != 1:
            continue
        c, T, F = st[1], st[2][0], st[3][0]
        if c[0] == 'un' and c[1] == 'not':
            c, T, F = c[2], F, T
        if c[0] != 'r' or not (T[0] == 'assign' and len(T) == 3 and len(T[1]) == 1 and T[1][0][0] == 'r' and len(T[2]) == 1):
            continue
        x = T[1][0]
        if not (is_assign_to(F, x[1]) and len(F) == 3):
            continue
        a, b = T[2][0], F[2][0]
        if a == c and b[0] not in ('closure', 'mvall'):
            block[i] = ['assign', [x], [('or', c, b)]]
        elif b == c and a[0] not in ('closure', 'mvall'):
            block[i] = ['assign', [x], [('and', c, a)]]


def flip_negated(block):
    for st in block:
        for b in sub_blocks(st):
            flip_negated(b)
        if st[0] == 'if' and st[3]:
            c = st[1]
            if c[0] == 'un' and c[1] == 'not':
                st[1] = c[2]
                st[2], st[3] = st[3], st[2]
            elif c[0] == 'bin' and c[1] == '~=':
                st[1] = ('bin', '==', c[2], c[3])
                st[2], st[3] = st[3], st[2]


def nil_vars(ast):
    """Variables whose only assignment is `x = nil` (never captured for writing) are just nil."""
    cnt = Counts(ast)
    cand = set()

    def find(block):
        for st in block:
            if st[0] == 'assign' and len(st[1]) == 1 and st[1][0][0] == 'r' and st[2] == [('k', None)]:
                n = st[1][0][1]
                if cnt.defs.get(n) == 1 and not cnt.caps.get(n):
                    cand.add(n)
            for b in sub_blocks(st):
                find(b)

    find(ast)
    # a register that is read but never written holds nil
    for n, u in cnt.uses.items():
        if u and not cnt.defs.get(n) and not cnt.caps.get(n) and re.match(r'^v\d+$', str(n)):
            cand.add(n)
    if not cand:
        return

    def fn(e):
        if e[0] == 'r' and e[1] in cand:
            return ('k', None)
        return e

    def rewrite(block):
        keep = []
        for st in block:
            if st[0] == 'assign' and len(st[1]) == 1 and st[1][0][0] == 'r' and st[1][0][1] in cand:
                continue
            set_head_exprs(st, fn)
            for b in sub_blocks(st):
                rewrite(b)
            keep.append(st)
        block[:] = keep

    rewrite(ast)


def events(e, out):
    """Evaluation-order list of leaves and of the operations that can have side effects."""
    t = e[0]
    if t == 'bin':
        events(e[2], out); events(e[3], out)
    elif t == 'un':
        events(e[2], out)
    elif t in ('and', 'or'):
        events(e[1], out); events(e[2], out)
    elif t == 'index':
        events(e[1], out); events(e[2], out)
        out.append(('index!',))
    elif t == 'method':
        events(e[1], out)
        out.append(('index!',))
    elif t == 'call':
        events(e[1], out)
        for a in e[2]:
            events(a, out)
        if e[3]:
            events(e[3], out)
        out.append(('call!',))
    elif t == 'table':
        for k, v in e[1]:
            if k is not None:
                events(k, out)
            events(v, out)
        if e[2]:
            events(e[2], out)
    else:
        out.append(e)
    return out


def inline_single_use(ast):
    changed = True
    rounds = 0
    while changed and rounds < 20:
        changed = False
        rounds += 1
        cnt = Counts(ast)

        def run(block):
            nonlocal changed
            i = 0
            while i < len(block) - 1:
                a, b = block[i], block[i + 1]
                ok = a[0] == 'assign' and len(a[1]) == 1 and a[1][0][0] == 'r' and len(a[2]) == 1
                if ok:
                    x = a[1][0][1]
                    E = a[2][0]
                    ok = cnt.defs.get(x) == 1 and cnt.uses.get(x) == 1 and not cnt.caps.get(x) \
                        and E[0] not in ('closure', 'va', 'mvall', 'table', 'newtable') and b[0] != 'while' \
                        and b[0] != 'repeat' and b[0] != 'prep' and x not in defs_of(b)
                if ok:
                    heads = head_exprs(b)
                    ev = []
                    for h in heads:
                        events(h, ev)
                    if ('r', x) not in ev:
                        ok = False
                    else:
                        before = ev[:ev.index(('r', x))]
                        impure_E = has(E, ('call',))
                        if impure_E and any(v in (('call!',), ('index!',)) for v in before):
                            ok = False
                        if has(E, ('index', 'global', 'up')) and any(v == ('call!',) for v in before):
                            ok = False
                if ok:
                    set_head_exprs(b, lambda e, x=x, E=E: E if e == ('r', x) else e)
                    del block[i]
                    changed = True
                    cnt.uses[x] = 0
                    if i > 0:
                        i -= 1
                    continue
                i += 1
            for st in block:
                for sb in sub_blocks(st):
                    run(sb)

        run(ast)


def _count(node, target):
    """occurrences of the tuple `target` anywhere inside an AST fragment"""
    if node == target:
        return 1
    if isinstance(node, (list, tuple)):
        return sum(_count(x, target) for x in node)
    return 0


def _has_call(node):
    if isinstance(node, tuple) and node and node[0] in ('call', 'closure'):
        return node[0] == 'call' or _has_call(list(node[1:]))
    if isinstance(node, (list, tuple)):
        return any(_has_call(x) for x in node)
    return False


def _targets(node, out):
    """registers assigned anywhere inside a statement"""
    if isinstance(node, list) and node and isinstance(node[0], str):
        if node[0] == 'assign':
            out.update(d for d in node[1] if d[0] == 'r')
        elif node[0] == 'fornum':
            out.add(('r', node[1]))
        elif node[0] == 'forin':
            out.update(('r', v) for v in node[1])
    if isinstance(node, list):
        for x in node:
            _targets(x, out)
    return out


def fold_packs(root):
    """This VM keeps the open results of a call in a table until the instruction that uses them:

        t = table.pack(g(x))                         f(a, b, g(x))
        n = t.n; t.n = 3 + n - 1
        table.move(t, 1, n, 3, t)             ->
        t[1] = a; t[2] = b
        f(unpack(t, 1, t.n))

    and `table.move(t, 1, t.n, k, list)` where the results end a table constructor."""
    import copy

    def unpack_of(T):
        return ('call', ('kfn', 'unpack'), [T, ('k', 1), ('index', T, ('k', 'n'))], None)

    def try_fold(lst, i):
        st = lst[i]
        T = st[1][0]
        call = st[2][0]
        items, tail = list(call[2]), call[3]
        total = _count(root, T)
        if total == 1 and not _has_call(items) and (tail is None or tail == ('va',)):
            del lst[i]          # (a function that never looks at its `...`)
            return True
        movable = not _has_call(items) and (tail is None or tail == ('va',)) and \
            all(e[0] in ('k', 'r') for e in items)
        reads = set()
        for e in items + ([tail] if tail else []):
            walk_expr(e, out := [])
            reads.update(x for x in out if x[0] == 'r')
        consumed = 1
        prefix = {}
        K = None
        nvar = None
        drop = [i]
        U = unpack_of(T)
        j = i + 1
        work = lst
        while j < len(work):
            x = work[j]
            t = x[0]
            if t == 'assign' and len(x[1]) == 1 and x[1][0][0] == 'r' and x[2] == [('index', T, ('k', 'n'))] and nvar is None:
                nvar = x[1][0]
                if _count(root, nvar) != 3:
                    return False
                consumed += 1
                drop.append(j)
            elif t == 'assign' and nvar is not None and x[1] == [('index', T, ('k', 'n'))] and len(x[2]) == 1 \
                    and x[2][0][0] == 'bin' and x[2][0][1] == '-' and x[2][0][3] == ('k', 1) and x[2][0][2][0] == 'bin' \
                    and x[2][0][2][1] == '+' and x[2][0][2][2][0] == 'k' and x[2][0][2][3] == nvar:
                K = x[2][0][2][2][1]
                consumed += 1
                drop.append(j)
            elif t == 'call' and K is not None and x[1] == ('call', ('kfn', 'table.move'), [T, ('k', 1), nvar, ('k', K), T], None):
                consumed += 2
                drop.append(j)
            elif t == 'setlist' and K is not None and x[1] == T and x[4] is None:
                for n, v in enumerate(x[3]):
                    prefix[x[2] + n] = v
                consumed += 1
                drop.append(j)
            elif t == 'assign' and K is not None and len(x[1]) == 1 and x[1][0][0] == 'index' and x[1][0][1] == T \
                    and x[1][0][2][0] == 'k' and isinstance(x[1][0][2][1], int) and len(x[2]) == 1 and _count(x[2], T) == 0:
                prefix[x[1][0][2][1]] = x[2][0]
                consumed += 1
                drop.append(j)
            elif _count(x, T):
                if K is not None and sorted(prefix) != list(range(1, K)):
                    return False
                final = [prefix[n] for n in sorted(prefix)] + items
                if t == 'if' and x[1] == T and consumed + 1 < total:
                    # (a table is never nil or false)
                    work = work[:j] + list(x[2]) + work[j + 1:]
                    consumed += 1
                    continue
                y = copy.copy(x)
                if t == 'call' and y[1][0] == 'call' and y[1][1] == ('kfn', 'table.move') and len(y[1][2]) == 5 \
                        and y[1][2][:3] == [T, ('k', 1), ('index', T, ('k', 'n'))] and y[1][2][3][0] == 'k' and _count(y[1][2][4], T) == 0:
                    y = ['setlist', y[1][2][4], y[1][2][3][1], final, tail]
                    consumed += 2
                    # ... of a table that was created empty just before: its constructor
                    prev = next((n for n in range(j - 1, -1, -1) if n not in drop), None)
                    if prev is not None and y[2] == 1 and work[prev][0] == 'setlist' and work[prev][1] == y[1] \
                            and not work[prev][3] and work[prev][4] is None:
                        drop.append(prev)
                        prev = next((n for n in range(prev - 1, -1, -1) if n not in drop), None)
                    if prev is not None and y[2] == 1 and work[prev][0] == 'assign' and work[prev][1] == [y[1]] \
                            and len(work[prev][2]) == 1 and work[prev][2][0][0] == 'newtable' and y[1][0] == 'r':
                        work[prev] = ['assign', [y[1]], [('table', [(None, v) for v in final], tail)]]
                        if consumed != total:
                            return False
                        drop.append(j)
                        lst[:] = [w for n, w in enumerate(work) if n not in drop]
                        return True
                else:
                    hits = [0]

                    def fn(e):
                        if e[0] == 'call' and e[3] == U:
                            hits[0] += 1
                            return ('call', e[1], list(e[2]) + final, tail)
                        if e[0] == 'table' and e[2] == U:
                            hits[0] += 1
                            return ('table', list(e[1]) + [(None, v) for v in final], tail)
                        return e
                    if t == 'return' and y[1] and y[1][-1] == U:
                        y[1] = list(y[1][:-1]) + final + ([tail] if tail else [])
                        hits[0] += 1
                    set_head_exprs(y, fn)
                    if hits[0] != 1:
                        return False
                    consumed += 2
                if consumed != total:
                    return False
                work[j] = y
                lst[:] = [w for n, w in enumerate(work) if n not in drop]
                return True
            else:
                if not movable and _has_call(x):
                    return False
                if reads & _targets(x, set()):
                    return False
            j += 1
        return False

    def defs(T):
        n = [0]

        def go(node):
            if isinstance(node, list):
                if node and node[0] == 'assign' and T in node[1]:
                    n[0] += 1
                for x in node:
                    go(x)
        go(root)
        return n[0]

    def walk(lst):
        i = 0
        while i < len(lst):
            st = lst[i]
            if st[0] == 'assign' and len(st[1]) == 1 and st[1][0][0] == 'r' and len(st[2]) == 1 and st[2][0][0] == 'call' \
                    and st[2][0][1] == ('kfn', 'table.pack') and defs(st[1][0]) == 1:
                # `x = t` copies of the table first
                T = st[1][0]
                for j in range(i + 1, len(lst)):
                    x = lst[j]
                    if x[0] == 'assign' and x[2] == [T] and len(x[1]) == 1 and x[1][0][0] == 'r' and defs(x[1][0]) == 1 \
                            and _count(root, T) == 2:
                        X = x[1][0]
                        del lst[j]
                        for k in range(j, len(lst)):
                            def ren(e, X=X, T=T):
                                return T if e == X else e
                            set_head_exprs(lst[k], ren)
                        break
                if _count(root, ('r', '\0')) == 0 and try_fold(lst, i):
                    continue
            for b in sub_blocks(st):
                walk(b)
            i += 1
    walk(root)
    return root


def beautify(ast):
    fold_packs(ast)
    strip_tail_returns(ast)
    nil_vars(ast)
    if MERGE_ARMS:
        sink_common(ast, Counts(ast).caps)
        copy_diamonds(ast)
    and_or(ast)
    inline_single_use(ast)
    and_or(ast)
    flip_negated(ast)
    return ast
