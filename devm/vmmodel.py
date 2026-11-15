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
    if s0[0] != 'local' or len(s0[1]) < 1 or len(s0[2]) != 1:
        return False
    e = s0[2][0]
    if e[0] != 'index' or e[1][0] != 'name' or e[2][0] != 'name':
        return False
    return body[1][0] == 'if'


LOOP_HEAD_RE = re.compile(r'\bwhile\s+true\s+do(\s+)local\s+\w+(?:\s*,\s*\w+)*\s*=\s*\w+\[\w+\]\s*;?\s*if\b')
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

    def _is_proto_table_style(self):
        parent = self.chain[-2]
        return len([p for p in parent.params if p != '_']) == 2 and parent.body and parent.body[-1][0] == 'return' \
            and parent.body[-1][1] and parent.body[-1][1][0][0] == 'func' and parent.body[-1][1][0][1] is self.interp

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
        """Name of the interpreter's register array: the local initialised with `create(nregs)`."""
        for st in it.func.body[:16]:
            if st[0] != 'local':
                continue
            for name, e in zip(st[1], st[2]):
                if e[0] == 'call' and e[1][0] == 'name' and len(e[2]) == 1 and e[2][0][0] == 'name':
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

    def patched_source(self, step=False, conds=False, flow=False, stepvars=None):
        """The script with hooks inserted.

        Always: the interpreter-entry hook (inlined counter; __LPH_RUN is only called for stubbed
        protos and from the entry at which the VM state is to be dumped).
        flow:   per-instruction counter, ring of executed instructions and the branch-inversion
                flag, all inlined (no calls).
        conds:  with flow, the discovery variant: branch conditions additionally go through __LPH_C.
        step:   debugging aid, calls __LPH_STEP(pid, loop, pc, registers) for every instruction.
        """
        src = self.src
        edits = []   # (pos, text)
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
                    edits.append((mid.end_pos, ' __LPH_REG(__pid,%s)' % mid.name_stmt))
                else:
                    edits.append((mid.kw_pos, '__LPH_REG(__pid,'))
                    edits.append((mid.end_pos, ')'))
                edits.append((it.func.body_pos,
                              '__LPH_N.c=__LPH_N.c+1;if __LPH_S[__pid]or __LPH_N.c>=__LPH_N.t then '
                              'local __r = __LPH_RUN(%s,%d,%s) if __r then return __r(...) end end;' % (
                                  lst, len(names), ','.join(names))))
        else:
            names = self.visible
            lst = '{' + ','.join('"%s"' % n for n in names) + '}'
            edits.append((self.interp.body_pos, 'do local __r = __LPH_RUN(%s,%d,%s) if __r then return __r(...) end end;' % (lst, len(names), ','.join(names))))
        for f in self.meta_closures():
            names = [n for n in self._visible_names(f) if n != '...'][:200]
            lst = '{' + ','.join('"%s"' % n for n in names) + '}'
            edits.append((f.kw_pos, '__LPH_CLOS(%s,%d,%s' % (lst, len(names), ''.join(n + ',' for n in names))))
            edits.append((f.end_pos, ')'))
        if self.oneline:
            for f in self.funcs_by_id:
                if f.name_stmt:
                    edits.append((f.end_pos, ' __LPH_F(%d,%s)' % (f.fid, f.name_stmt)))
                elif self._is_expression(f):
                    edits.append((f.kw_pos, '__LPH_F(%d,' % f.fid))
                    edits.append((f.end_pos, ')'))
        edits.sort(key=lambda x: -x[0])
        for pos, text in edits:
            src = src[:pos] + text + ' ' + src[pos:]
        if self.oneline:
            from . import normalize
            src = normalize.unflow(src)
        return src

    def _is_expression(self, f):
        """False for `function a.b() end` statements (nothing to wrap)."""
        head = self.src[max(0, f.kw_pos - 64):f.kw_pos].rstrip()
        return bool(head) and (head[-1] in '=,({[' or head.endswith('return') or head[-1] in '+-*/%^.<>~' or
                               head.endswith(' and') or head.endswith(' or') or head.endswith(' not'))


def find_luau():
    cands = [os.environ.get('LUAU_BIN'), os.path.join(HERE, '..', 'bin', 'luau'), 'luau']
    import shutil
    for c in cands:
        if c and (os.path.isfile(c) or shutil.which(c)):
            return c
    raise RuntimeError('luau binary not found; set LUAU_BIN')


def run_oracle(model, workdir, tag, options=()):
    """Run the script to its end with per-instruction recording.

    Returns (dump of the VM state at the end, oracle, note) where oracle maps (pid, loop, pc) of
    every executed instruction to a dict: r/v = numbers held by registers / interpreter locals on
    entry (same on every visit), vers = [(step, instruction text)] per version executed, final =
    the instruction text at the end of the run."""
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
    r = subprocess.run([find_luau(), os.path.join(HERE, 'harness.lua'), '-a', patched, outjson, 'oracle=1', 'dumpend=1'] +
                       list(options), capture_output=True, text=True, errors='replace', timeout=1800)
    note = [l for l in r.stderr.split('\n') if l.startswith('[oracle]')]
    note += [l for l in r.stderr.split('\n') if l.startswith('[http]')]
    if not os.path.exists(outjson) or not os.path.exists(outjson + '.oracle'):
        raise RuntimeError('oracle run failed: ' + r.stderr[-2000:])
    with open(outjson + '.oracle') as f:
        raw = json.load(f)
    out = {}
    for k, rec in raw.items():
        pid, loop, pc = (int(x) for x in k.split(':'))
        rec['r'] = {int(i): v for i, v in rec['r'].items()}
        rec['succ'] = {tuple(x) for x in rec['succ']}
        out[(pid, loop, pc)] = rec
    with open(outjson) as f:
        dump = json.load(f)
    return dump, out, note


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
