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


def beautify(ast):
    strip_tail_returns(ast)
    nil_vars(ast)
    and_or(ast)
    inline_single_use(ast)
    and_or(ast)
    flip_negated(ast)
    return ast
