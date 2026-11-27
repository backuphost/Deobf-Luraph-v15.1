"""Locate the Luraph interpreter inside a (decompiled) script and patch in dump hooks."""
import atexit
import json
import os
import re
import subprocess
import sys
import time

from . import luparse
from .luparse import Func

HERE = os.path.dirname(os.path.abspath(__file__))


def is_dispatch_loop(st, assigned=False):
    """`while true do local OP[, X] = CODE[PC]; if ...`.

    assigned=True tests for the variant in which the opcode variable is a local of the interpreter
    declared before the loop (`while true do OP = CODE[PC]; if ...`, seen in Luraph v15.2: one mode
    per interpreter can have this form).  It is only accepted inside a function that also has a
    loop of the first form (see Model._find): on its own the shape is too common in ordinary code."""
    if st[0] != 'while' or st[1] != ('true',):
        return False
    body = st[2]
    if len(body) < 2:
        return False
    s0 = body[0]
    if assigned:
        if s0[0] != 'assign' or len(s0[1]) < 1 or len(s0[2]) != 1 or any(t[0] != 'name' for t in s0[1]):
            return False
    elif s0[0] != 'local' or len(s0[1]) < 1 or len(s0[2]) != 1:
        return False
    e = s0[2][0]
    if e[0] != 'index' or e[1][0] != 'name' or e[2][0] != 'name':
        return False
    if body[1][0] == 'if' or _outlined_test(body, s0):
        return True
    # one table per instruction (Luraph v14.0 - v14.2): local I = CODE[PC]; local OP = I[n]; PC = PC + 1; if
    # (normalize.normalise_heads writes every spelling of the head in this form)
    if assigned:
        return False
    return _insn_table_head(body, s0, e)


def _insn_table_head(body, s0, e):
    if len(body) < 4 or s0[0] != 'local' or len(s0[1]) != 1:
        return False
    s1, s2 = body[1], body[2]
    if s1[0] != 'local' or len(s1[1]) != 1 or len(s1[2]) != 1:
        return False
    e1 = s1[2][0]
    if e1[0] != 'index' or e1[1] != ('name', s0[1][0]) or e1[2][0] != 'num':
        return False
    if s2[0] != 'assign' or len(s2[1]) != 1 or s2[1][0] != e[2]:
        return False
    return body[3][0] == 'if'


def is_insn_table_loop(st):
    """True for a dispatch loop of the one-table-per-instruction form (see is_dispatch_loop)."""
    body = st[2]
    return _insn_table_head(body, body[0], body[0][2][0])


def _outlined_test(body, s0):
    """`local OP = CODE[PC]; local T = OP >= 10; if T then ...`: the dispatch tree of an interpreter
    whose tests are each computed into a local first (the second interpreter of the v15.1 corpus
    builds; the first one of the same file has the usual form)."""
    if len(body) < 3 or s0[0] != 'local':
        return False
    s1, s2 = body[1], body[2]
    if s1[0] != 'local' or len(s1[1]) != 1 or len(s1[2]) != 1 or s2[0] != 'if' or not s2[1]:
        return False
    t = s1[2][0]
    if t[0] != 'bin' or t[1] not in ('<', '<=', '>', '>=', '==', '~='):
        return False
    return ('name', s0[1][0]) in (t[2], t[3]) and s2[1][0][0] == ('name', s1[1][0])


# the canonical heads (normalize.normalise_heads): `local OP = CODE[PC]; if`, the one-table-per-instruction
# form `local I = CODE[PC]; local OP = I[n]; PC = PC + 1; if`, and the outlined first test
# `local OP = CODE[PC]; local T = OP >= 10; if T`
LOOP_HEAD_RE = re.compile(r'\bwhile\s+true\s+do(\s+)(?:local\s+)?\w+(?:\s*,\s*\w+)*\s*=\s*\w+\[\w+\]\s*;?\s*'
                          r'(?:local\s+\w+\s*=\s*\w+\[\w+\]\s*;?\s*\w+\s*=\s*\w+\s*\+\s*1\s*;?\s*'
                          r'|local\s+\w+\s*=\s*\w+\s*(?:[<>]=?|[~=]=)\s*\w+\s*;?\s*)?if\b')
_NUM = r'(0[xX][0-9a-fA-F_]+|0[bB][01_]+|\d[\d_]*)'
CELL_RE = re.compile(r'\b(\w+)\[' + _NUM + r'\]\[(\w+)\[' + _NUM + r'\]\]')


def _int(text):
    t = text.replace('_', '')
    if t[:2] in ('0x', '0X'):
        return int(t, 16)
    if t[:2] in ('0b', '0B'):
        return int(t[2:], 2)
    return int(t)


class Interp:
    """One interpreter closure: its enclosing functions, dispatch loops and visible names."""
    func = None
    chain = None
    loops = None
    visible = None
    index = 0


class Model:
    """Static description of where the interpreter lives."""

    def __init__(self, src, oneline=False):
        self.src = src
        self.oneline = oneline      # src is a reflowed copy of a single-line script
        self.body, self.funcs = luparse.parse(src)
        # stable function ids (source order); the harness reports closures by id when lines are useless
        self.funcs_by_id = sorted((f for f in self.funcs if hasattr(f, 'kw_pos')), key=lambda f: f.kw_pos)
        for i, f in enumerate(self.funcs_by_id):
            f.fid = i
        self.interp = None          # Func: the closure that contains the dispatch loop(s)
        self.chain = []             # enclosing Funcs, outermost first, ending with interp
        self.visible = []           # names visible at interp definition
        self.loops = []             # dispatch loop statements
        self.func_by_line = {}
        # decompiler output uses `_` for discarded values (possibly several per scope); minified
        # originals use it as an ordinary identifier
        self.underscore_is_name = bool(re.search(r'[^\w]_\s*[\[(.]', src))
        for f in self.funcs:
            self.func_by_line.setdefault(f.line, []).append(f)
        self._find()

    # -- generic walker
    def _find(self):
        best = {}
        late = []       # dispatch loops whose opcode variable is declared outside the loop
        order = {}      # id(loop statement) -> position in source order

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
                if fstack and is_dispatch_loop(st):
                    best.setdefault(fstack[-1].id, (list(fstack), []))[1].append(st)
                    order[id(st)] = len(order)
                elif fstack and is_dispatch_loop(st, assigned=True):
                    late.append((list(fstack), st))
                    order[id(st)] = len(order)
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
        for fstack, st in late:
            # only next to a loop of the usual form, in the same function
            if fstack[-1].id in best:
                best[fstack[-1].id][1].append(st)
        self.toplevel = set()
        self._mark_toplevel()

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
            # source order: loop_positions() pairs them with the loop heads found in the text
            loops.sort(key=lambda l: order[id(l)])
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
        self.up_base = self._upvalue_base()

    def _mark_toplevel(self):
        """Functions that are not nested in another function have no upvalues to worry about."""
        nested = set()
        spans = sorted((f.kw_pos, f.end_pos, f.id) for f in self.funcs if hasattr(f, 'kw_pos'))
        stack = []
        for start, end, fid in spans:
            while stack and stack[-1] <= start:
                stack.pop()
            if stack:
                nested.add(fid)
            stack.append(end)
        self.toplevel = {f.id for f in self.funcs if f.id not in nested}

    def meta_closures(self):
        """Closures installed as metamethods (`__index = function ...`): the lifter must be able to
        call them, so their captured variables are recorded at run time."""
        out = []
        for f in self.funcs:
            head = self.src[max(0, f.kw_pos - 24):f.kw_pos]
            if re.search(r'__(index|newindex|call)\s*=\s*$', head) and f.id not in self.toplevel:
                out.append(f)
        return out

    def _upvalue_base(self):
        """Index of the first entry of an upvalue list (0 or 1), read off the non-virtualised
        functions, which unpack their upvalue list with literal indices."""
        from . import luprint
        lo = None
        for f in self.funcs:
            shape = luprint.native_shape(f)
            if shape and shape[1]:
                m = min(shape[1].values())
                lo = m if lo is None else min(lo, m)
        if lo in (0, 1):
            return lo
        return 1 if re.search(r'for (\w+)=1,\w+,1 do local \w+=\(\1-1\)\*3', self.src) else 0

    def _cell_layout(self):
        """(box key, slot key) of upvalue cells: the interpreter reads them as cell[a][cell[b]]."""
        counts = {}
        for it in self.interps:
            for m in CELL_RE.finditer(self.src, it.func.body_pos, it.func.end_pos):
                if m.group(1) == m.group(3):
                    k = (_int(m.group(2)), _int(m.group(4)))
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
                if st[2] is target:
                    found.extend(scope)
                    return True
                return walk_block(st[2].body, scope + st[2].params)
            if t == 'funcstmt':
                if st[2] is target:
                    found.extend(scope)
                    return True
                return walk_block(st[2].body, scope + st[2].params)
            if t == 'return':
                return walk_exprs(st[1], scope)
            return False

        walk_block(self.body, [])
        seen = []
        for n in found:
            if (n != '_' or self.underscore_is_name) and n not in seen:
                seen.append(n)
        return seen

    # -- patching
    @property
    def factory_style(self):
        """True when there is a closure layer between the proto-level function and the interpreter."""
        return len(self.chain) >= 3 and not self._is_proto_table_style()

    @staticmethod
    def _maker_body(parent):
        """Statements of a closure maker, looking through a junk test around its tail
        (`if J == 230 then else <unpack>; ...; return F end`): the body plus the arms of a
        trailing `if`, as one list."""
        out = list(parent.body)
        for _ in range(3):
            if not out or out[-1][0] != 'if':
                break
            st = out.pop()
            arms = [b for _c, b in st[1]] + ([st[2]] if st[2] else [])
            arms = [b for b in arms if b]
            if not arms:
                break
            # the arm that does the work is the longest one
            out.extend(max(arms, key=len))
        return out

    def _is_proto_table_style(self, it=None):
        """wrap(proto, upvalues[, junk ...]) -> function(...): the closure maker of the proto-table
        layout (every Luraph v14 build).  It unpacks the fields of the proto into locals and
        returns the interpreter closure; a junk third parameter, a junk constant among the
        unpacked values, a junk test around the body and `return (name)` are decoration."""
        chain = it.chain if it is not None else self.chain
        interp = chain[-1]
        parent = chain[-2]
        body = self._maker_body(parent)
        if len(parent.params) < 2 or not body or body[-1][0] != 'return' or not body[-1][1]:
            return False
        ret = body[-1][1][0]
        while ret[0] == 'paren':
            ret = ret[1]
        if ret[0] == 'func':
            if ret[1] is not interp:
                return False
        elif ret[0] != 'name':
            # raw layout: `Z = (function(...) ... end); return Z`
            return False
        # wrap(proto, upvalues) unpacks the proto table into locals; a factory's upvalue closure that
        # happens to take two parameters (function(H, d) local d, p, ... = L, D, ...) does not
        return self._unpacked_param(parent, body) is not None

    @staticmethod
    def _unpacked_param(parent, body):
        """The parameter whose fields a `local a, b, ... = P[1], P[2], ...` statement of the closure
        maker reads, or None.  Other values in the statement are decoration or per-closure state
        (a junk constant, a fresh table); the fields of the parameter must be the majority."""
        counts = {}
        for st in body:
            if st[0] != 'local' or not st[2]:
                continue
            hits = {}
            other = 0
            for e in st[2]:
                while e[0] == 'paren':
                    e = e[1]
                if e[0] == 'index' and e[1][0] == 'name' and e[1][1] in parent.params:
                    hits[e[1][1]] = hits.get(e[1][1], 0) + 1
                elif e[0] != 'num':
                    other += 1
            if hits and (other == 0 or max(hits.values()) >= 2 and max(hits.values()) > other):
                for k, v in hits.items():
                    counts[k] = counts.get(k, 0) + v
        return max(counts, key=counts.get) if counts else None

    def wrapper_numbers(self, it):
        """{index of a wrapper parameter: [interpreter locals it is copied into]}.

        Luraph v15.2 hands the wrapper (the function between the proto-level factory and the
        interpreter closure) per-closure numbers besides the upvalue list, e.g.
        `function(Z,Z,Z,J,i,U) ... return function(...) ... W,Q,Wh,mh = m,J,Z,E`: Z and J are keys
        the instructions of that closure are decrypted with, `i` is the upvalue list.  Found from
        the interpreter's own set-up statements (the ones in front of / around its dispatch
        loops): a visible wrapper parameter that is copied into a local there as a bare name is
        such a number.  Empty for builds whose wrapper only takes the upvalue list (v15.1)."""
        if len(it.chain) < 3:
            return {}
        mid = it.chain[-2]
        shadowed = set(mid.body[0][1]) if mid.body and mid.body[0][0] == 'local' else set()
        visible = {p: i for i, p in enumerate(mid.params)
                   if p not in mid.params[i + 1:] and p not in shadowed and (p != '_' or self.underscore_is_name)}
        out = {}
        hidden = set()      # names redeclared by the interpreter before the copy

        def scan(block, depth):
            for st in block:
                if st[0] in ('local', 'assign'):
                    targets = st[1] if st[0] == 'local' else [t[1] if t[0] == 'name' else None for t in st[1]]
                    for name, e in zip(targets, st[2]):
                        if name and e[0] == 'name' and e[1] in visible and e[1] not in hidden:
                            out.setdefault(visible[e[1]], [])
                            if name not in out[visible[e[1]]]:
                                out[visible[e[1]]].append(name)
                    if st[0] == 'local':
                        hidden.update(n for n in st[1] if n in visible)
                elif st[0] == 'if' and depth < 3:
                    for _c, b in st[1]:
                        scan(b, depth + 1)
                    if st[2]:
                        scan(st[2], depth + 1)
        scan(it.func.body, 0)
        return out

    @property
    def keyed_wrappers(self):
        """True for builds whose closure wrappers take per-closure keys (Luraph v15.2, see
        wrapper_numbers).  The passes written for that generation are switched on by this, so
        that what v15.1 inputs go through stays as it was."""
        v = getattr(self, '_keyed_wrappers', None)
        if v is None:
            v = self._keyed_wrappers = any(self.wrapper_numbers(it) for it in self.interps)
        return v

    def _mid_is_statement(self, mid):
        """True when the middle closure is declared with `local function`."""
        return self.src[mid.kw_pos - 6:mid.kw_pos].strip() == 'local' or \
            self.src[max(0, mid.kw_pos - 16):mid.kw_pos].rstrip().endswith('local')

    def loop_positions(self):
        """Source offset of the first statement inside every dispatch loop, in self.loops order."""
        out = []
        for it in self.interps:
            found = [mm.end(1) for mm in LOOP_HEAD_RE.finditer(self.src, it.func.body_pos, it.func.end_pos)]
            if len(found) != len(it.loops):
                raise RuntimeError('cannot locate the dispatch loops of interpreter %d in the source' % it.index)
            out.extend(found)
        return out

    def regs_name(self, it):
        """Name of the interpreter's register array: the local initialised with `create(nregs)`.
        Luraph v14.4 and later call the allocator through the loader's state table
        (`local R = STATE[n](nregs)`, `(STATE[n][m](nregs))`): accepted when no local of the prologue
        has the bare-name form."""
        def strip(e):
            while e[0] == 'paren':
                e = e[1]
            return e
        for indexed in (False, True):
            for st in it.func.body[:16]:
                if st[0] != 'local':
                    continue
                for name, e in zip(st[1], st[2]):
                    if indexed:
                        e = strip(e)
                    if e[0] != 'call' or len(e[2]) != 1 or e[2][0][0] != 'name':
                        continue
                    f = e[1]
                    if indexed:
                        f = strip(f)
                        while f[0] == 'index' and strip(f[2])[0] == 'num':
                            f = strip(f[1])
                    if f[0] == 'name':
                        return name
        return 'nil'

    def step_vars(self):
        """{loop index: [interpreter locals in scope at the dispatch loop]} for the oracle run."""
        def contains(node, target):
            if node is target:
                return True
            if isinstance(node, (tuple, list)):
                return any(contains(x, target) for x in node)
            return False

        out = {}
        li = 0
        for it in self.interps:
            regs = self.regs_name(it)
            for l in it.loops:
                names = []
                for st in it.func.body:
                    if contains(st, l):
                        break
                    if st[0] == 'local':
                        for n in st[1]:
                            if n not in names and n != regs and (n != '_' or self.underscore_is_name):
                                names.append(n)
                out[li] = names
                li += 1
        return out

    def cond_sites(self):
        """[(start, end, kind)] of every branch condition inside an interpreter that reads a register.

        kind ranks how likely the condition is a decision of the *program* rather than part of a
        flattened state machine: 1 truth test, 2 register == register, 3 register == something,
        4 register < register, 5 register < immediate (what state dispatch looks like)."""
        toks = luparse.tokenize(self.src)
        out = []
        for it in self.interps:
            regs = self.regs_name(it)
            if regs == 'nil':
                continue
            lo, hi = it.func.body_pos, it.func.end_pos
            uses = re.compile(r'\b%s\[' % re.escape(regs))
            stack = []
            for i, (kind, val, _line, pos) in enumerate(toks):
                if pos < lo:
                    continue
                if pos >= hi:
                    break
                if kind == 'kw' and val in ('if', 'elseif'):
                    stack.append(pos + len(val))
                elif kind == 'kw' and val == 'then' and stack:
                    start = stack.pop()
                    text = self.src[start:pos]
                    n = len(uses.findall(text))
                    if not n:
                        continue
                    if re.search(r'==|~=', text):
                        k = 2 if n >= 2 else 3
                    elif re.search(r'[<>]', text):
                        k = 4 if n >= 2 else 5
                    else:
                        k = 1
                    out.append((start, pos, k))
        return out

    def proto_param(self, it=None):
        """Proto-table layout: the wrap parameter that receives the proto table."""
        wrap = (it.chain if it is not None else self.chain)[-2]
        counts = {}
        for st in self._maker_body(wrap):
            if st[0] != 'local':
                continue
            for e in st[2]:
                while e[0] == 'paren':
                    e = e[1]
                if e[0] == 'index' and e[1][0] == 'name' and e[1][1] in wrap.params:
                    counts[e[1][1]] = counts.get(e[1][1], 0) + 1
        return max(counts, key=counts.get) if counts else None

    def loopless_closures(self):
        """[(closure maker, function)]: vararg functions made directly by a closure maker that hold no
        dispatch loop.  Luraph can compile a proto into an interpreter of its own without a loop
        (straight-line code for exactly that proto); such a function is entered like any
        interpreter closure, but the model has nothing to lift it with."""
        out = []
        seen = set()
        for it in self.interps:
            if len(it.chain) < 2:
                continue
            wrap = it.chain[-2]
            if id(wrap) in seen or not hasattr(wrap, 'kw_pos'):
                continue
            seen.add(id(wrap))
            inside = [f for f in self.funcs_by_id if f is not wrap and wrap.body_pos <= f.kw_pos < wrap.end_pos]
            for f in inside:
                if not f.vararg or id(f) in self.interp_funcs:
                    continue
                if any(g is not f and g.body_pos <= f.kw_pos < g.end_pos for g in inside):
                    continue        # nested deeper
                out.append((wrap, f))
        return out

    def shadowed_maker_names(self):
        """[(function id of a closure maker, [its top-level locals that redeclare a name of the
        enclosing loader])] for the proto-table layout (see normalize.unshadow)."""
        out = []
        seen = set()
        for it in self.interps:
            if len(it.chain) < 2:
                continue
            wrap = it.chain[-2]
            if id(wrap) in seen or not hasattr(wrap, 'fid'):
                continue
            seen.add(id(wrap))
            outer = set(self._visible_names(wrap)) - set(wrap.params)
            clash = []
            for st in wrap.body:
                if st[0] == 'local':
                    clash.extend(n for n in st[1] if n in outer and n not in clash)
            if clash:
                out.append((wrap.fid, clash))
        return out

    @property
    def insn_tables(self):
        """True when the dispatch loops fetch one table per instruction (Luraph v14.0 - v14.2)."""
        return any(is_insn_table_loop(l) for l in self.loops)

    def first_program_call(self):
        """Proto-table layout: where the loader runs the VM's first program.

        Luraph v14 loaders end with `P = WRAP(P, U)(<ten or more arguments>); return WRAP(P, U)`: the
        first program is Luraph's own init (environment checks, lazy-constant set-up); it gets
        the deserializer, the stream readers and the closure maker itself as arguments and returns
        the proto of the script.  Returns (start, end) source offsets of the call
        `WRAP(P, U)(...)`, or None when the text has no such statement or more than one."""
        toks = [t for t in luparse.tokenize(self.src) if t[0] != 'eof']
        n = len(toks)

        def tv(j):
            return toks[j][1] if j < n else None

        def tk(j):
            return toks[j][0] if j < n else None

        def ref(j):
            """`name` or `name[<number>]` at j: (key, next index)"""
            if tk(j) != 'name':
                return None, j
            if tk(j + 1) == 'op' and tv(j + 1) == '[' and tk(j + 2) == 'num' and tk(j + 3) == 'op' and tv(j + 3) == ']':
                return (tv(j), toks[j + 2][1]), j + 4
            return (tv(j), None), j + 1

        found = []
        for i in range(1, n - 8):
            if not (tk(i) == 'op' and tv(i) == '=' and tk(i - 1) == 'name'):
                continue
            if i >= 2 and tk(i - 2) == 'op' and tv(i - 2) in ('.', ',', ':'):
                continue
            target = tv(i - 1)
            j = i + 1
            while tk(j) == 'op' and tv(j) == '(':
                j += 1
            start = j
            wrap, j = ref(j)
            if wrap is None or not (tk(j) == 'op' and tv(j) == '(' and tk(j + 1) == 'name' and tv(j + 1) == target
                                    and tk(j + 2) == 'op' and tv(j + 2) == ','):
                continue
            ups, j = ref(j + 3)
            if ups is None or not (tk(j) == 'op' and tv(j) == ')' and tk(j + 1) == 'op' and tv(j + 1) == '('):
                continue
            # the argument list: balanced up to its closing parenthesis
            j += 2
            depth = 0
            commas = 0
            has_wrap = False
            while j < n:
                k, v = tk(j), tv(j)
                if k == 'op' and v in ('(', '[', '{'):
                    depth += 1
                elif k == 'op' and v in (')', ']', '}'):
                    if depth == 0:
                        break
                    depth -= 1
                elif k == 'op' and v == ',' and depth == 0:
                    commas += 1
                elif k == 'kw' and v in ('function', 'end', 'do', 'then', 'local', 'return') or k == 'op' and v == ';':
                    commas = -100
                    break
                if depth == 0 and k == 'name' and ref(j)[0] == wrap:
                    has_wrap = True
                j += 1
            if commas < 5 or not has_wrap or j >= n:
                continue
            found.append((toks[start][3], toks[j][3] + 1))
        return found[0] if len(found) == 1 else None

    def patched_source(self, step=False, conds=False, flow=False, stepvars=None, newproto=False, count=False):
        """The script with hooks inserted.

        Always: the interpreter-entry hook (inlined counter; __LPH_RUN is only called for stubbed
        protos and from the entry at which the VM state is to be dumped).
        flow:   per-instruction counter, ring of executed instructions and the branch-inversion
                flag, all inlined (no calls).
        conds:  with flow, the discovery variant: branch conditions additionally go through __LPH_C.
        step:   debugging aid, calls __LPH_STEP(pid, loop, pc, registers) for every instruction.
        newproto: proto-table layout, route A: the loader's first-program call goes through
                __LPH_MARK, which learns the proto that program returns; the entry hook of every
                interpreter calls __LPH_RUN when exactly that proto is entered (a comparison, no
                call: the init measures its own call stack).  A loader without such a call runs a
                single root program: the hook then calls __LPH_ENTER for protos that are not
                marked as part of the root program.
        count:  every dispatch loop counts its instructions in __LPH_N.s and calls __LPH_OVER once
                the budget __LPH_N.b is used up (inlined; used with newproto).
        """
        src = self.src
        edits = []   # (pos, text)
        factory = self.factory_style
        if count and not (step or flow):
            for p in self.loop_positions():
                edits.append((p, '__LPH_N.s=__LPH_N.s+1;if __LPH_N.s>__LPH_N.b then __LPH_OVER() end;'))
        if flow:
            for start, end, k in self.cond_sites():
                if conds:
                    edits.append((start, ' __LPH_C(%d,' % k))
                    edits.append((end, ')~=__LPH_N.f ' if k != 5 else ')'))
                elif k != 5:
                    edits.append((start, ' (('))
                    edits.append((end, ')and true or false)~=__LPH_N.f '))
        if step or flow:
            pos = self.loop_positions()
            li = 0
            for it in self.interps:
                regs = self.regs_name(it)
                for l in it.loops:
                    pcv = l[2][0][2][0][2][1]
                    text = ''
                    if flow:
                        text += ('__LPH_N.s=__LPH_N.s+1;__LPH_N.f=__LPH_X[__LPH_N.s]==true;'
                                 '__LPH_R[__LPH_N.s%%4096]=__pid*268435456+%d+%s;' % (li * 16777216, pcv))
                    if step:
                        names = (stepvars or {}).get(li)
                        if names:
                            text += '__LPH_STEP(__pid,%d,%s,%s,"%s",%s);' % (li, pcv, regs, ','.join(names), ','.join(names))
                        else:
                            text += '__LPH_STEP(__pid,%d,%s,%s);' % (li, pcv, regs)
                    edits.append((pos[li], text))
                    li += 1
        if not factory and (step or flow):
            # proto-table layout: the proto id the step hook reports is given out per closure made
            done = set()
            for it in self.interps:
                wrap = it.chain[-2] if len(it.chain) >= 2 else None
                if wrap is None or id(wrap) in done:
                    continue
                done.add(id(wrap))
                edits.append((wrap.body_pos, 'local __pid=__LPH_PID(%s);' % (self.proto_param(it) or 'nil')))
        if factory:
            for it in self.interps:
                outer, mid = it.chain[-3], it.chain[-2]
                names = it.visible + ['__pid']
                lst = '{' + ','.join('"%s"' % n for n in names) + '}'
                oparams = [p for p in outer.params if p != '_' or self.underscore_is_name]
                olst = '{' + ','.join('"%s"' % n for n in oparams) + '}'
                edits.append((outer.body_pos, 'local __pid = __LPH_PROTO(%d,%s,%d,%s);' % (
                    it.index, olst, len(oparams), ','.join(oparams))))
                if mid.name_stmt:
                    # `local function NAME(...) ... end`: register right after the declaration
                    edits.append((mid.end_pos, ' %s = __LPH_REG(__pid,%s)' % (mid.name_stmt, mid.name_stmt)))
                else:
                    edits.append((mid.kw_pos, '__LPH_REG(__pid,'))
                    edits.append((mid.end_pos, ')'))
                edits.append((it.func.body_pos,
                              '__LPH_N.c=__LPH_N.c+1;if __LPH_S[__pid]or __LPH_N.c>=__LPH_N.t then '
                              'local __r = __LPH_RUN(%s,%d,%s) if __r then return __r(...) end end;' % (
                                  lst, len(names), ','.join(names))))
        else:
            # every interpreter of the closure maker gets the entry hook: a proto runs on the
            # interpreter its kind field selects, not necessarily on the largest one
            mark = self.first_program_call() if newproto else None
            if mark:
                edits.append((mark[0], '__LPH_MARK('))
                edits.append((mark[1], ')'))
            captured = set()

            class _Loopless:
                """stands in for an Interp in the loop below"""

                def __init__(self, model, wrap, func, like):
                    self.func = func
                    self.chain = like.chain[:-1] + [func]
                    self.visible = model._visible_names(func)

            targets = list(self.interps)
            if newproto:
                # (the script's main function can be one of these: without the hook the script would
                # run to its end, or for ever, without the dump being taken)
                by_wrap = {id(it.chain[-2]): it for it in self.interps if len(it.chain) >= 2}
                targets += [_Loopless(self, wrap, f, by_wrap[id(wrap)]) for wrap, f in self.loopless_closures()]
            for it in targets:
                names = it.visible
                lst = '{' + ','.join('"%s"' % n for n in names) + '}'
                if newproto:
                    pp = self.proto_param(it) if len(it.chain) >= 2 else None
                    if pp is None:
                        continue
                    wrap = it.chain[-2]
                    if any(st[0] == 'local' and pp in st[1] for st in self._maker_body(wrap)):
                        # the closure maker redeclares the name of its proto parameter
                        if id(wrap) not in captured:
                            captured.add(id(wrap))
                            edits.append((wrap.body_pos, 'local __lph_p=%s;' % pp))
                        pp = '__lph_p'
                    if mark:
                        edits.append((it.func.body_pos, 'if %s==__LPH_N.w then local __r = __LPH_RUN(%s,%d,%s) if __r then return __r(...) end end;' % (
                            pp, lst, len(names), ','.join(names))))
                    else:
                        # a single root program (no first-program call): the hook fires for every
                        # proto the harness has not marked as part of the root program (a table
                        # read, no call), see __LPH_ENTER in harness.lua
                        edits.append((it.func.body_pos, 'if __LPH_T[%s]==nil then local __r = __LPH_ENTER(%s,%s,%d,%s) if __r then return __r(...) end end;' % (
                            pp, pp, lst, len(names), ','.join(names))))
                else:
                    edits.append((it.func.body_pos, 'do local __r = __LPH_RUN(%s,%d,%s) if __r then return __r(...) end end;' % (lst, len(names), ','.join(names))))
        for f in self.meta_closures():
            names = [n for n in self._visible_names(f) if n != '...'][:200]
            lst = '{' + ','.join('"%s"' % n for n in names) + '}'
            edits.append((f.kw_pos, '__LPH_CLOS(%s,%d,%s' % (lst, len(names), ''.join(n + ',' for n in names))))
            edits.append((f.end_pos, ')'))
        if self.oneline:
            for f in self.funcs_by_id:
                if f.name_stmt:
                    edits.append((f.end_pos, ' __LPH_F(%d,%s)' % (f.fid, f.name_stmt)))
                elif f.stmt_name:
                    # `function a.b() ... end` / `function a:b() ... end` is a statement: nothing
                    # to wrap; the closure is registered by reading the field it was stored in
                    edits.append((f.end_pos, ' __LPH_F(%d,%s)' % (f.fid, f.stmt_name)))
                elif self._is_expression(f):
                    edits.append((f.kw_pos, '__LPH_F(%d,' % f.fid))
                    edits.append((f.end_pos, ')'))
        if (step or newproto) and self.oneline:
            # Watchdog for the loader's own loops (deserialiser state machines are not VM code, so
            # the per-instruction hook never sees them): the harness sets __LPH_N.q while it calls
            # into the loader itself, and __LPH_W stops a loop that does not end.  The rounds are
            # also counted (__LPH_N.l, no call): every __LPH_N.k rounds __LPH_L looks at a traced run
            # that may not end (the script's functions Luraph keeps as plain Lua loop here too).
            spans = [(it.func.body_pos, it.func.end_pos) for it in self.interps]
            watch = ' if __LPH_N.q then __LPH_W() end;__LPH_N.l=__LPH_N.l+1;if __LPH_N.l>__LPH_N.k then __LPH_L() end;'
            toks = [t for t in luparse.tokenize(self.src) if t[0] != 'eof']
            heads = []
            for t in toks:
                if t[0] != 'kw':
                    continue
                if t[1] in ('while', 'for'):
                    heads.append(t[1])
                elif t[1] == 'do' and heads:
                    if heads.pop() == 'while' and not any(a <= t[3] < b for a, b in spans):
                        edits.append((t[3] + 2, watch))
                elif t[1] == 'repeat' and not any(a <= t[3] < b for a, b in spans):
                    edits.append((t[3] + 6, watch))
        edits.sort(key=lambda x: -x[0])
        for pos, text in edits:
            src = src[:pos] + text + ' ' + src[pos:]
        if self.oneline:
            from . import normalize
            src = normalize.unflow(src)
        return src

    def _is_expression(self, f):
        """False for `function a.b() end` statements (nothing to wrap)."""
        if f.stmt_name:
            return False
        head = self.src[max(0, f.kw_pos - 64):f.kw_pos].rstrip()
        return bool(head) and (head[-1] in '=,({[' or head.endswith('return') or head[-1] in '+-*/%^.<>~' or
                               head.endswith(' and') or head.endswith(' or') or head.endswith(' not'))


def find_luau():
    import platform
    cands = [os.environ.get('LUAU_BIN'), os.path.join(HERE, '..', 'bin', 'luau'),
             os.path.join(HERE, '..', 'bin', 'luau.' + platform.machine()), 'luau']
    import shutil
    for c in cands:
        if c and (os.path.isfile(c) or shutil.which(c)):
            return c
    raise RuntimeError('luau binary not found; set LUAU_BIN')


class PoolServer:
    """A finished harness run kept alive (harness.lua, serve=<dir>): decodes pool constants on request."""

    def __init__(self, proc, sdir):
        self.proc = proc
        self.dir = sdir
        self.seq = 0
        self.cache = {}
        self.dead = False
        self.data = None        # the dump this run produced (new protos and tables are added to it)
        self.tdiffs = []        # (table id, [(key, number)]) additions not yet applied to live models

    def decode(self, table_id, index, patches=(), pc=None):
        """the raw dump value (see pe.Dump.conv) of pool entry `index` of constant table `table_id`;
        patches = ((buffer path, offset, bytes), ...) are applied to the run's buffers for the call"""
        key = (table_id, index, patches, pc)
        if key in self.cache:
            return self.cache[key]
        v = None
        if not self.dead and self.proc.poll() is None:
            self.seq += 1
            tmp = os.path.join(self.dir, 'req.tmp')
            with open(tmp, 'w') as f:
                f.write('%d %d %d %d %d%s\n' % (self.seq, table_id, index, len(self.data['protos']) if self.data else 10 ** 9,
                                                len(self.data['tables']) if self.data else 10 ** 9,
                                                '' if pc is None else ' %d' % pc))
                for pt in patches:
                    if pt[0] == 'T':
                        f.write('T %s %d %r\n' % (pt[1], pt[2], pt[3]))
                    else:
                        f.write('P %s %d %s\n' % (pt[0], pt[1], pt[2].hex()))
            os.replace(tmp, os.path.join(self.dir, 'req'))
            resp = os.path.join(self.dir, 'resp')
            deadline = time.time() + 30
            head = '%d\n' % self.seq
            while time.time() < deadline:
                try:
                    with open(resp) as f:
                        text = f.read()
                except OSError:
                    text = ''
                if text.startswith(head) and text.endswith('\nEND'):
                    try:
                        ans = json.loads(text[len(head):-4])
                    except ValueError:
                        ans = None
                    if isinstance(ans, dict):
                        v = ans.get('v')
                        if self.data is not None:
                            # functions the loader built to answer: new protos and their tables
                            self.data['tables'].update(ans.get('tables') or {})
                            self.data['protos'].extend(ans.get('protos') or [])
                            # ... and the entries they added to tables the loader shares
                            for tid, ents in (ans.get('tdiff') or {}).items():
                                raw = self.data['tables'].get(tid)
                                if raw is None:
                                    continue
                                for k, x in ents:
                                    if 1 <= k <= len(raw['arr']):
                                        raw['arr'][k - 1] = x
                                    else:
                                        raw['hash'].append([k, x])
                                self.tdiffs.append((int(tid), ents))
                    break
                if self.proc.poll() is not None:
                    self.dead = True
                    break
                time.sleep(0.0005)
            else:
                self.dead = True
        self.cache[key] = v
        return v

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()


SERVER = None


def start_server(cmd, workdir, limit=1800):
    """Run the harness until its dump is written and leave it serving; returns its stderr so far
    (with a `[timeout]` line when it was stopped after `limit` seconds without a dump).  A script
    that does not end is normally stopped earlier by the harness itself, which then dumps and
    serves as at an end (STALL); `limit` is the last resort, and a killed run has no dump."""
    global SERVER
    if SERVER is not None:
        SERVER.close()
        SERVER = None
    sdir = os.path.join(workdir, 'serve')
    os.makedirs(sdir, exist_ok=True)
    for n in ('ready', 'req', 'resp'):
        if os.path.exists(os.path.join(sdir, n)):
            os.remove(os.path.join(sdir, n))
    errpath = os.path.join(sdir, 'stderr.txt')
    with open(errpath, 'w') as ef:
        proc = subprocess.Popen(cmd + ['serve=' + sdir], stdout=subprocess.DEVNULL, stderr=ef)
    deadline = time.time() + limit
    stopped = False
    try:
        while time.time() < deadline and proc.poll() is None and not os.path.exists(os.path.join(sdir, 'ready')):
            time.sleep(0.2)
    except BaseException:       # (devm itself is being stopped: do not leave the harness behind)
        proc.kill()
        raise
    if proc.poll() is None and os.path.exists(os.path.join(sdir, 'ready')):
        SERVER = PoolServer(proc, sdir)
        atexit.register(SERVER.close)
    elif proc.poll() is None:
        proc.kill()
        stopped = True
    with open(errpath, errors='replace') as f:
        text = f.read()
    if stopped:
        # (a script that waits for ever - for a key, an event, its own loop - and that the harness's
        # own watch did not stop (STALL) ends here)
        text += '\n[timeout] the run was stopped after %d s without reaching its end\n' % limit
    return text


# per-access lines of a traced run (each global read, call, new proto): thousands of them bury the reason
_TRACE_TAGS = ('[global] ', '[call] ', '[proto] ', '[gread] ', '[gwrite] ')
# the run's verdict lines: kept wherever they are
_VERDICT_TAGS = ('[oracle]', '[budget]', '[timeout]', '[stalled]', '[end]', '[error]', '[fail]')


# a line the harness wrote with a tag ("[script] print: ...", "[stubs] ..."); an error text has none
# (a chunk's own name in it is no tag: '[string "..."]:1: attempt to ...')
_TAGGED = re.compile(r'\[\w+\]')


def _clip(line, n):
    """line cut to at most n characters, the cut marked with '...'."""
    return line if len(line) <= n else line[:max(n - 3, 0)] + '...'


def _share(lengths, room):
    """The length lines of these lengths are cut to when they share `room` characters: the largest
    cap with sum(min(length, cap)) <= room (a line shorter than its share is kept whole and leaves
    the rest of its share to the others)."""
    rest, left = room, len(lengths)
    for n in sorted(lengths):
        share = rest // left
        if n > share:
            return share
        rest, left = rest - n, left - 1
    return max(lengths, default=0)


def harness_tail(stderr, limit=600):
    """The meaningful end of a failed harness run's stderr, as one line of at most `limit` characters
    for a failure message: the per-access trace lines and the harness's own traceback dropped; the
    last four verdict lines ([oracle], [budget], [timeout], [stalled], [end], [error], [fail]) and
    the error itself (the last line without a [tag]: the error and where it happened; else the last
    other line), always, whole when they fit and otherwise sharing the room (the longest cut first,
    each cut marked with '...'); then the other lines, latest first, while the whole stays within
    `limit`.  In their order, joined by ' | '.  (Those five lines need 27 characters at their
    shortest, '...' each and ' | ' between them: below that the joined line is cut to `limit`.)"""
    lines, skip = [], False
    for line in stderr.split('\n'):
        line = line.strip()
        if line == 'stacktrace:':
            skip = True         # (the frames that follow, up to an empty line, are the harness's own)
            continue
        if not line:
            skip = False
            continue
        if skip or line.startswith(_TRACE_TAGS):
            continue
        lines.append(line)
    must = [i for i, l in enumerate(lines) if l.startswith(_VERDICT_TAGS)][-4:]
    others = [i for i, l in enumerate(lines) if not l.startswith(_VERDICT_TAGS)]
    if others:
        plain = [i for i in others if not _TAGGED.match(lines[i])]
        last = (plain or others)[-1]
        others.remove(last)
        must.append(last)
    cap = _share([len(lines[i]) for i in must], limit - 3 * (len(must) - 1))
    keep = {i: _clip(lines[i], cap) for i in must}
    size = sum(len(t) + 3 for t in keep.values())
    for i in reversed(others):
        if size + len(lines[i]) > limit:
            break
        keep[i] = lines[i]
        size += len(lines[i]) + 3
    text = ' | '.join(keep[i] for i in sorted(keep)) or '(no output)'
    return text if len(text) <= limit else _clip(text, limit) if limit >= 3 else text[:max(limit, 0)]


# The traced run of a script that does not end (a key system waiting for its key, a GUI script in
# its event loop, `while true do task.wait() end`): the harness stops it and takes the end-state
# dump there (harness.lua, watch.tick).  Seconds of the run's processor time (/proc/self/stat; Luau's
# os.clock is a wall clock):
#   stall      after the script's own code started, this long without a VM instruction running
#              for the first time (or a program being made); 4 x this before it started ends the
#              run without a dump (Luraph's start-up code spinning: nothing of the script to lift)
#   stallwait  this much waiting simulated by the mock (task.wait, wait, an event's :Wait())
#              without new code
#   stallrun   this long after the script's own code started, new code or not
#   tamper     1: stop the run at the entry of Luraph's tamper response (a VM function handed two or
#              more code arrays of another program, harness.lua watch.entered), before it scrambles
#              them; 0: let it run (it then loops for ever and a time limit stops it)
# 0 switches one off.  devm.run --stall / --stall-wait / --stall-run / --no-tamper-stop set them.
STALL = {'stall': 90, 'stallwait': 600, 'stallrun': 600, 'tamper': 1}


def stall_options(options=(), **given):
    """the harness options of the watch on a run that does not end: STALL, with the values in
    `given` (None: the default) and those already in `options` taking precedence"""
    out = []
    for k, v in STALL.items():
        if any(str(o).startswith(k + '=') for o in options):
            continue
        v = given.get(k) if given.get(k) is not None else v
        out.append('%s=%d' % (k, max(0, int(v))))
    return out


def read_oracle(path):
    """The oracle file the harness writes (harness.lua writeOracle): {(pid, loop, pc): record}, with
    register numbers as ints, strings decoded and the successors as a set of (loop, pc)."""
    with open(path) as f:
        raw = json.load(f)
    out = {}
    for k, rec in raw.items():
        pid, loop, pc = (int(x) for x in k.split(':'))
        rec['r'] = {int(i): v for i, v in rec['r'].items()}
        # strings (hex) and booleans the registers held on every visit
        rec['s'] = {int(i): bytes.fromhex(v).decode('latin-1') if isinstance(v, str) else v
                    for i, v in (rec.get('s') or {}).items()}
        # registers that held the activation's own register array on every visit
        rec['a'] = {int(i) for i in rec.get('a') or ()}
        # calls handed the activation's register array that the run saw (h, of the n visits) and the
        # registers they changed (w)
        rec['h'] = int(rec.get('h') or 0)
        rec['w'] = {int(i) for i in rec.get('w') or ()}
        rec['succ'] = {tuple(x) for x in rec['succ']}
        out[(pid, loop, pc)] = rec
    return out


def run_oracle(model, workdir, tag, options=()):
    """Run the script to its end with per-instruction recording.  A script that does not end is
    stopped by the harness (STALL) and its state taken where it was; the note list then has the
    harness's `[stalled]` line, which says why and for how long.

    Returns (dump of the VM state at the end, oracle, note) where oracle maps (pid, loop, pc) of
    every executed instruction to a dict: r/v = numbers held by registers / interpreter locals on
    entry (same on every visit), s = strings and booleans held by registers on entry (same on every
    visit of the script's own run; not for code only the exercise pass ran), a = registers that held
    the activation's own register array on every visit (likewise), h / w = how many calls handed the
    activation's register array the run saw (a VM function as the callee) and the registers they
    changed, vers = [(step, instruction text)]
    per version executed, final = the instruction text at the end of the run."""
    patched = os.path.join(workdir, tag + '.step.lua')
    outjson = os.path.join(workdir, tag + '.end.json')
    with open(patched, 'w', encoding='latin-1') as f:
        f.write(model.patched_source(step=True, stepvars=model.step_vars()))
    for p in (outjson, outjson + '.oracle'):
        if os.path.exists(p):
            os.remove(p)
    # utf8.nfcnormalize / nfdnormalize data: the bytes of normalised strings steer the script
    nf = os.path.join(workdir, 'nfdata.lua')
    if not os.path.exists(nf):
        from . import unidata
        unidata.generate(nf)
    # (numeric options first: the harness reads `name=<digits>` options from the front of its list)
    cmd = [find_luau(), os.path.join(HERE, 'harness.lua'), '-a', patched, outjson, 'oracle=1', 'dumpend=1'] + \
        stall_options(options) + list(options)
    cmd += list(getattr(model, 'run_options', ()))      # carrier of a chunk the script loads itself (container.py)
    if model.oneline:
        # keep the run alive after the dump: the lifter asks it for lazily decoded constants
        stderr = start_server(cmd, workdir)
    else:
        stderr = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=1800).stderr

    class r:
        pass
    r.stderr = stderr
    note = [l for l in r.stderr.split('\n') if l.startswith('[oracle]')]
    note += [l for l in r.stderr.split('\n') if l.startswith(('[http]', '[env]', '[script]', '[stub] permissive', '[stalled]', '[effects]', '[under]', '[where]',
                                                                      '[started]', '[watch]'))]
    if not os.path.exists(outjson) or not os.path.exists(outjson + '.oracle'):
        raise RuntimeError('oracle run failed: ' + harness_tail(r.stderr))
    out = read_oracle(outjson + '.oracle')
    with open(outjson) as f:
        dump = json.load(f)
    if os.path.exists(outjson + '.snaps'):
        with open(outjson + '.snaps') as f:
            dump['snaps'] = json.load(f)        # code-state versions (harness codeVersion)
    return dump, out, note


def run_dump_script(model, workdir, tag, options=(), budget=12000000, timeout=150, calls=False):
    """Proto-table layout, route A: let the VM run its own first program (Luraph's init) under the
    harness and dump the state when the proto that program returned is entered, i.e. when the
    script's main function starts (patched_source(newproto=True), harness dumpnew=1).

    budget: VM instructions the run may execute before the harness stops it.  Inits seen hand over
    after 6 thousand to 0.9 million instructions; a failed check ends in Luraph's endless loop.

    calls: diagnosis run with the harness's call logging (calls=1).  The notes then get one line
    `[calls] ...` with the last mocked Roblox / executor calls the run made.  Call logging puts one
    more closure around every mock function, which a script can notice: not for the run a result
    is taken from.

    Returns (dump or None, notes): notes are the harness's lines about the run ([handover],
    [dumpnew], [budget], [http], ...) plus one line of ours when the run had to be stopped."""
    patched = os.path.join(workdir, tag + '.newproto.lua')
    outjson = os.path.join(workdir, tag + '.script.json')
    with open(patched, 'w', encoding='latin-1') as f:
        f.write(model.patched_source(newproto=True, count=True))
    if os.path.exists(outjson):
        os.remove(outjson)
    nf = os.path.join(workdir, 'nfdata.lua')
    if not os.path.exists(nf):
        from . import unidata
        unidata.generate(nf)
    cmd = [find_luau(), os.path.join(HERE, 'harness.lua'), '-a', patched, outjson, 'dumpnew=1', 'budget=%d' % budget]
    if model.insn_tables:
        cmd.append('lazyinsn=1')
    # the interpreters' function ids: the harness evaluates the init's key functions, which are
    # closures of the VM on this route (harness.lua findConstFns)
    cmd += ['interp=%d' % it.func.fid for it in model.interps if getattr(it.func, 'fid', None) is not None]
    if calls:
        cmd.append('calls=1')
    cmd += list(options) + list(getattr(model, 'run_options', ()))
    errpath = os.path.join(workdir, tag + '.newproto.err')
    notes = []
    with open(errpath, 'w') as ef:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=ef)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            notes.append('[timeout] the run was stopped after %d s' % timeout)
        except BaseException:
            proc.kill()
            raise
    with open(errpath, errors='replace') as f:
        stderr = f.read()
    keep = ('[root]', '[handover]', '[dumpnew]', '[dump]', '[keyfn]', '[budget]', '[http]', '[env]', '[carrier]', 'NOT REACHED', 'COMPILE ERROR', '[end]')
    notes = [l for l in stderr.split('\n') if l.startswith(keep)][:40] + notes
    if calls:
        # (calls made after the budget stop belong to the unwinding of the stopped run)
        made = [l[len('[call] '):] for l in stderr.split('[budget] ')[0].split('\n') if l.startswith('[call] ')]
        # (the harness logs the first 20000 calls only)
        notes.append('[calls] %d mocked calls%s; the last ones: %s' % (
            len(made), ' logged (there were more)' if len(made) >= 20000 else '',
            '; '.join(c[:90] for c in made[-6:]) or 'none'))
    if not os.path.exists(outjson):
        # how the run ended: the error message the harness printed in front of its `[end] error` line
        # (message, then `stacktrace:` with one line per frame and an empty line)
        lines = stderr.strip().split('\n')
        for i, l in enumerate(lines):
            if not l.startswith('[end] error'):
                continue
            j = i - 1
            while j > 0 and lines[j] != 'stacktrace:':
                j -= 1
            msg = lines[j - 1] if j > 0 else next((x for x in reversed(lines[:i]) if x.strip()), '')
            if msg.strip() and not msg.startswith(keep):
                notes.insert(max(0, len(notes) - 1), '[error] ' + msg[:300])
        if not notes:
            notes = ['[stderr] ' + l for l in lines[-3:] if l]
        return None, notes
    with open(outjson) as f:
        return f.read(), notes


def run_dump(model, workdir, tag, layers=(), timeout=600):
    """layers: already-patched outer sources (outermost first); model is the innermost layer."""
    os.makedirs(workdir, exist_ok=True)
    patched = os.path.join(workdir, tag + '.patched.lua')
    outjson = os.path.join(workdir, tag + '.dump.json')
    with open(patched, 'w', encoding='latin-1') as f:
        f.write(model.patched_source())
    if os.path.exists(outjson):
        os.remove(outjson)
    chain = list(layers) + [patched]
    try:
        r = subprocess.run([find_luau(), os.path.join(HERE, 'harness.lua'), '-a', chain[0], outjson] + chain[1:]
                           + list(getattr(model, 'run_options', ())),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # (subprocess.run has killed the harness)
        raise RuntimeError('dump failed: the run did not reach the first interpreter entry within %d s '
                           '(the loader itself waits or does not end)' % timeout)
    if not os.path.exists(outjson):
        raise RuntimeError('dump failed: ' + harness_tail(r.stderr))
    with open(outjson) as f:
        return json.load(f)
