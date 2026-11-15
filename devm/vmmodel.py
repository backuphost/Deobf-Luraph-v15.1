"""Locate the Luraph interpreter inside a (decompiled) script and patch in dump hooks."""
import json
import os
import re
import subprocess
import sys

from . import luparse
from .luparse import Func

HERE = os.path.dirname(os.path.abspath(__file__))


def is_dispatch_loop(st):
    if st[0] != 'while' or st[1] != ('true',):
        return False
    body = st[2]
    if len(body) < 2:
        return False
    s0 = body[0]
    if s0[0] != 'local' or len(s0[1]) != 1 or len(s0[2]) != 1:
        return False
    e = s0[2][0]
    if e[0] != 'index' or e[1][0] != 'name' or e[2][0] != 'name':
        return False
    return body[1][0] == 'if'


CELL_RE = re.compile(r'\b(\w+)\[(\d+)\]\[(\w+)\[(\d+)\]\]')


class Interp:
    """One interpreter closure: its enclosing functions, dispatch loops and visible names."""
    func = None
    chain = None
    loops = None
    visible = None
    index = 0


class Model:
    """Static description of where the interpreter lives."""

    def __init__(self, src):
        self.src = src
        self.body, self.funcs = luparse.parse(src)
        self.interp = None          # Func: the closure that contains the dispatch loop(s)
        self.chain = []             # enclosing Funcs, outermost first, ending with interp
        self.visible = []           # names visible at interp definition
        self.loops = []             # dispatch loop statements
        self.func_by_line = {}
        for f in self.funcs:
            self.func_by_line.setdefault(f.line, []).append(f)
        self._find()

    # -- generic walker
    def _find(self):
        best = {}

        def walk_block(block, fstack):
            for st in block:
                walk_stmt(st, fstack)

        def walk_expr(e, fstack):
            if not isinstance(e, tuple):
                return
            t = e[0]
            if t == 'func':
                f = e[1]
                walk_block(f.body, fstack + [f])
            elif t in ('index', 'bin'):
                walk_expr(e[-2], fstack)
                walk_expr(e[-1], fstack)
            elif t in ('un', 'paren'):
                walk_expr(e[-1], fstack)
            elif t == 'call':
                walk_expr(e[1], fstack)
                for a in e[2]:
                    walk_expr(a, fstack)
            elif t == 'mcall':
                walk_expr(e[1], fstack)
                for a in e[3]:
                    walk_expr(a, fstack)
            elif t == 'table':
                for _, k, v in e[1]:
                    if k is not None:
                        walk_expr(k, fstack)
                    walk_expr(v, fstack)
            elif t == 'ifexpr':
                walk_expr(e[1], fstack)
                walk_expr(e[2], fstack)
                walk_expr(e[3], fstack)

        def walk_stmt(st, fstack):
            t = st[0]
            if t == 'while':
                if is_dispatch_loop(st) and fstack:
                    best.setdefault(fstack[-1].id, (list(fstack), []))[1].append(st)
                walk_expr(st[1], fstack)
                walk_block(st[2], fstack)
            elif t == 'local':
                for e in st[2]:
                    walk_expr(e, fstack)
            elif t == 'assign':
                for e in st[1]:
                    walk_expr(e, fstack)
                for e in st[2]:
                    walk_expr(e, fstack)
            elif t == 'opassign':
                walk_expr(st[2], fstack)
                walk_expr(st[3], fstack)
            elif t == 'callstmt':
                walk_expr(st[1], fstack)
            elif t == 'do':
                walk_block(st[1], fstack)
            elif t == 'repeat':
                walk_block(st[1], fstack)
                walk_expr(st[2], fstack)
            elif t == 'if':
                for c, b in st[1]:
                    walk_expr(c, fstack)
                    walk_block(b, fstack)
                if st[2]:
                    walk_block(st[2], fstack)
            elif t == 'fornum':
                walk_expr(st[2], fstack)
                walk_expr(st[3], fstack)
                if st[4]:
                    walk_expr(st[4], fstack)
                walk_block(st[5], fstack)
            elif t == 'forin':
                for e in st[2]:
                    walk_expr(e, fstack)
                walk_block(st[3], fstack)
            elif t in ('localfunc', 'funcstmt'):
                f = st[2]
                walk_block(f.body, fstack + [f])
            elif t == 'return':
                for e in st[1]:
                    walk_expr(e, fstack)

        walk_block(self.body, [])
        if not best:
            raise RuntimeError('no Luraph dispatch loop found')

        def size(item):
            chain, loops = item
            f = chain[-1]
            return f.end_line - f.line

        # The VM closure is the outermost vararg function around a dispatch loop (newer builds run
        # the loop inside a nested protected-call closure). A script can contain several
        # interpreters; group the loops per interpreter.
        groups = {}
        for chain, loops in best.values():
            cut = None
            for i, f in enumerate(chain):
                if f.vararg:
                    cut = i
                    break
            if cut is None:
                continue
            chain = chain[:cut + 1]
            g = groups.setdefault(chain[-1].id, (chain, []))
            g[1].extend(loops)
        if not groups:
            raise RuntimeError('no Luraph interpreter closure found')
        self.interps = []
        for chain, loops in sorted(groups.values(), key=lambda g: g[0][-1].line):
            it = Interp()
            it.chain = chain
            it.func = chain[-1]
            it.loops = loops
            it.index = len(self.interps)
            it.visible = self._visible_names(it.func)
            self.interps.append(it)
        main = max(self.interps, key=lambda it: it.func.end_line - it.func.line)
        self.chain = main.chain
        self.interp = main.func
        self.visible = main.visible
        self.loops = [l for it in self.interps for l in it.loops]
        self.interp_funcs = {id(it.func): it for it in self.interps}
        self.cells = self._cell_layout()

    def _cell_layout(self):
        """(box key, slot key) of upvalue cells: the interpreter reads them as cell[a][cell[b]]."""
        counts = {}
        for it in self.interps:
            for m in CELL_RE.finditer(self.src, it.func.body_pos, it.func.end_pos):
                if m.group(1) == m.group(3):
                    k = (int(m.group(2)), int(m.group(4)))
                    counts[k] = counts.get(k, 0) + 1
        if not counts:
            return (1, 3)
        return max(counts, key=counts.get)

    def _visible_names(self, target=None):
        """Names in scope where the interpreter closure is defined."""
        target = target or self.interp
        found = []

        def walk_block(block, scope):
            scope = list(scope)
            for st in block:
                if walk_stmt(st, scope):
                    return True
            return False

        def walk_exprs(es, scope):
            for e in es:
                if walk_expr(e, scope):
                    return True
            return False

        def walk_expr(e, scope):
            if not isinstance(e, tuple):
                return False
            t = e[0]
            if t == 'func':
                f = e[1]
                if f is target:
                    found.extend(scope)
                    return True
                return walk_block(f.body, scope + f.params)
            if t in ('index', 'bin'):
                return walk_expr(e[-2], scope) or walk_expr(e[-1], scope)
            if t in ('un', 'paren'):
                return walk_expr(e[-1], scope)
            if t == 'call':
                return walk_expr(e[1], scope) or walk_exprs(e[2], scope)
            if t == 'mcall':
                return walk_expr(e[1], scope) or walk_exprs(e[3], scope)
            if t == 'table':
                for _, k, v in e[1]:
                    if k is not None and walk_expr(k, scope):
                        return True
                    if walk_expr(v, scope):
                        return True
                return False
            if t == 'ifexpr':
                return walk_exprs(e[1:], scope)
            return False

        def walk_stmt(st, scope):
            t = st[0]
            if t == 'local':
                r = walk_exprs(st[2], scope)
                scope.extend(st[1])
                return r
            if t == 'assign':
                return walk_exprs(st[1], scope) or walk_exprs(st[2], scope)
            if t == 'opassign':
                return walk_expr(st[2], scope) or walk_expr(st[3], scope)
            if t == 'callstmt':
                return walk_expr(st[1], scope)
            if t == 'do':
                return walk_block(st[1], scope)
            if t == 'while':
                return walk_expr(st[1], scope) or walk_block(st[2], scope)
            if t == 'repeat':
                return walk_block(st[1], scope) or walk_expr(st[2], scope)
            if t == 'if':
                for c, b in st[1]:
                    if walk_expr(c, scope) or walk_block(b, scope):
                        return True
                return bool(st[2]) and walk_block(st[2], scope)
            if t == 'fornum':
                if walk_exprs([x for x in st[2:5] if x], scope):
                    return True
                return walk_block(st[5], scope + [st[1]])
            if t == 'forin':
                return walk_exprs(st[2], scope) or walk_block(st[3], scope + st[1])
            if t == 'localfunc':
                scope.append(st[1])
                return walk_block(st[2].body, scope + st[2].params)
            if t == 'funcstmt':
                return walk_block(st[2].body, scope + st[2].params)
            if t == 'return':
                return walk_exprs(st[1], scope)
            return False

        walk_block(self.body, [])
        seen = []
        for n in found:
            if n != '_' and n not in seen:
                seen.append(n)
        return seen

    # -- patching
    @property
    def factory_style(self):
        """True when there is a closure layer between the proto-level function and the interpreter."""
        return len(self.chain) >= 3 and not self._is_proto_table_style()

    def _is_proto_table_style(self):
        parent = self.chain[-2]
        return len([p for p in parent.params if p != '_']) == 2 and parent.body and parent.body[-1][0] == 'return' \
            and parent.body[-1][1] and parent.body[-1][1][0][0] == 'func' and parent.body[-1][1][0][1] is self.interp

    def _mid_is_statement(self, mid):
        """True when the middle closure is declared with `local function`."""
        return self.src[mid.kw_pos - 6:mid.kw_pos].strip() == 'local' or \
            self.src[max(0, mid.kw_pos - 16):mid.kw_pos].rstrip().endswith('local')

    def patched_source(self):
        src = self.src
        edits = []   # (pos, text)
        if self.factory_style:
            for it in self.interps:
                outer, mid = it.chain[-3], it.chain[-2]
                names = it.visible + ['__pid']
                lst = '{' + ','.join('"%s"' % n for n in names) + '}'
                oparams = [p for p in outer.params if p != '_']
                olst = '{' + ','.join('"%s"' % n for n in oparams) + '}'
                edits.append((outer.body_pos, 'local __pid = __LPH_PROTO(%d,%s,%d,%s);' % (
                    it.index, olst, len(oparams), ','.join(oparams))))
                if mid.name_stmt:
                    # `local function NAME(...) ... end`: register right after the declaration
                    edits.append((mid.end_pos, ' __LPH_REG(__pid,%s);' % mid.name_stmt))
                else:
                    edits.append((mid.kw_pos, '__LPH_REG(__pid,'))
                    edits.append((mid.end_pos, ')'))
                edits.append((it.func.body_pos, 'do local __r = __LPH_RUN(%s,%d,%s) if __r then return __r(...) end end;' % (
                    lst, len(names), ','.join(names))))
        else:
            names = self.visible
            lst = '{' + ','.join('"%s"' % n for n in names) + '}'
            edits.append((self.interp.body_pos, 'do local __r = __LPH_RUN(%s,%d,%s) if __r then return __r(...) end end;' % (lst, len(names), ','.join(names))))
        edits.sort(key=lambda x: -x[0])
        for pos, text in edits:
            src = src[:pos] + text + ' ' + src[pos:]
        return src


def find_luau():
    cands = [os.environ.get('LUAU_BIN'), os.path.join(HERE, '..', 'bin', 'luau'), 'luau']
    import shutil
    for c in cands:
        if c and (os.path.isfile(c) or shutil.which(c)):
            return c
    raise RuntimeError('luau binary not found; set LUAU_BIN')


def run_dump(model, workdir, tag, layers=()):
    """layers: already-patched outer sources (outermost first); model is the innermost layer."""
    os.makedirs(workdir, exist_ok=True)
    patched = os.path.join(workdir, tag + '.patched.lua')
    outjson = os.path.join(workdir, tag + '.dump.json')
    with open(patched, 'w', encoding='latin-1') as f:
        f.write(model.patched_source())
    if os.path.exists(outjson):
        os.remove(outjson)
    chain = list(layers) + [patched]
    r = subprocess.run([find_luau(), os.path.join(HERE, 'harness.lua'), '-a', chain[0], outjson] + chain[1:],
                       capture_output=True, text=True, timeout=600)
    if not os.path.exists(outjson):
        raise RuntimeError('dump failed: ' + r.stderr[-2000:])
    with open(outjson) as f:
        return json.load(f)
