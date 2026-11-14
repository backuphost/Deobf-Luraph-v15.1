"""Locate the Luraph interpreter inside a (decompiled) script and patch in dump hooks."""
import json
import os
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
        self.toplevel = set()       # ids of functions not nested in another function (no upvalues)
        self.def_scope = {}         # func id -> names visible where the function expression is
        self.func_exprs = {}
        self.capture_all = False
        self.captures = {}          # func id -> upvalue names recorded at closure creation
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
                if not fstack:
                    self.toplevel.add(f.id)
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
                if not fstack:
                    self.toplevel.add(f.id)
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

        chain, loops = max(best.values(), key=size)
        # the VM closure is the innermost vararg function around the dispatch loop
        while len(chain) > 1 and not chain[-1].vararg:
            chain = chain[:-1]
        self.chain = chain
        self.interp = chain[-1]
        self.loops = loops
        self.visible = self._visible_names()
        self._plan_captures()

    def _visible_names(self):
        """Names in scope where the interpreter closure is defined."""
        target = self.interp
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
                self.def_scope[f.id] = list(scope)
                self.func_exprs[f.id] = f
                if f is target:
                    found.extend(scope)
                    if not self.capture_all:
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
        if self.capture_all:
            self.capture_all = False
            return None
        seen = []
        for n in found:
            if n != '_' and n not in seen:
                seen.append(n)
        return seen

    def _plan_captures(self):
        """Closures of the loader (e.g. the lazy-constant __index) are needed by the lifter with
        their upvalues; choose which function expressions to record and with which names."""
        self.capture_all = True
        self._visible_names()
        inside = set()

        def mark(f):
            inside.add(f.id)
            for g in funcs_in(f.body):
                mark(g)

        mark(self.interp)
        self.inside_interp = inside
        chain_ids = {f.id for f in self.chain}
        for fid, f in self.func_exprs.items():
            if fid in self.toplevel or fid in inside or fid in chain_ids:
                continue
            used = names_in(f.body)
            names = []
            for n in self.def_scope.get(fid, ()):
                if n != '_' and n in used and n not in names:
                    names.append(n)
            if len(names) > 180:
                continue
            self.captures[fid] = names

    # -- patching
    @property
    def factory_style(self):
        """True when there is a closure layer between the proto-level function and the interpreter."""
        return len(self.chain) >= 3 and not self._is_proto_table_style()

    def _is_proto_table_style(self):
        parent = self.chain[-2]
        return len([p for p in parent.params if p != '_']) == 2 and parent.body and parent.body[-1][0] == 'return' \
            and parent.body[-1][1] and parent.body[-1][1][0][0] == 'func' and parent.body[-1][1][0][1] is self.interp

    def patched_source(self):
        src = self.src
        edits = []   # (pos, text)
        names = self.visible
        lst = '{' + ','.join('"%s"' % n for n in names) + '}'
        if self.factory_style:
            outer = self.chain[-3]
            mid = self.chain[-2]
            oparams = [p for p in outer.params if p != '_']
            olst = '{' + ','.join('"%s"' % n for n in oparams) + '}'
            edits.append((outer.body_pos, 'local __pid = __LPH_PROTO(%s,%d,%s);' % (olst, len(oparams), ','.join(oparams))))
            # wrap the mid closure expression
            start = mid.kw_pos
            end = mid.end_pos
            edits.append((start, '__LPH_REG(__pid,'))
            edits.append((end, ')'))
            edits.append((self.interp.body_pos, 'do local __r = __LPH_RUN(%s,%d,%s,__pid) if __r then return __r(...) end end;' % (lst, len(names), ','.join(names))))
        else:
            edits.append((self.interp.body_pos, 'do local __r = __LPH_RUN(%s,%d,%s) if __r then return __r(...) end end;' % (lst, len(names), ','.join(names))))
        for fid, names in self.captures.items():
            f = self.func_exprs[fid]
            nl = '{' + ','.join('"%s"' % n for n in names) + '}'
            edits.append((f.kw_pos, '__LPH_CAP(%d,%s,' % (fid, nl)))
            # the getter shares the closure's upvalues, so the dump sees their values at dump time
            edits.append((f.end_pos, ',function() return %s end)' % ','.join(names) if names else ',nil)'))
        edits.sort(key=lambda x: -x[0])
        for pos, text in edits:
            src = src[:pos] + text + ' ' + src[pos:]
        return src


def funcs_in(node):
    """Function objects directly reachable inside an AST fragment (not descending into them)."""
    out = []
    stack = [node]
    while stack:
        x = stack.pop()
        if isinstance(x, Func):
            out.append(x)
        elif isinstance(x, (tuple, list)):
            stack.extend(x)
    return out


def names_in(node):
    """Every identifier mentioned in an AST fragment, nested functions included."""
    out = set()
    stack = [node]
    while stack:
        x = stack.pop()
        if isinstance(x, Func):
            stack.append(x.body)
        elif isinstance(x, tuple):
            if len(x) == 2 and x[0] == 'name' and isinstance(x[1], str):
                out.add(x[1])
            else:
                stack.extend(x)
        elif isinstance(x, list):
            stack.extend(x)
    return out


def find_luau():
    cands = [os.environ.get('LUAU_BIN'), os.path.join(HERE, '..', 'bin', 'luau'), 'luau']
    import shutil
    for c in cands:
        if c and (os.path.isfile(c) or shutil.which(c)):
            return c
    raise RuntimeError('luau binary not found; build it with: python devm/build_luau.py (or set LUAU_BIN)')


def check_luau():
    """The harness needs a Luau CLI with a few extra builtins (see devm/build_luau.py)."""
    luau = find_luau()
    probe = os.path.join(HERE, '_probe.lua')
    with open(probe, 'w') as f:
        f.write('assert(readfile and writefile and appendfile and ewrite and exitafter and cwrap and loadstring and setfenv)')
    try:
        r = subprocess.run([luau, probe], capture_output=True, text=True, timeout=30)
    finally:
        os.remove(probe)
    if r.returncode != 0:
        raise RuntimeError('%s lacks the builtins the devm harness needs; build it with: python devm/build_luau.py' % luau)
    return luau


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
