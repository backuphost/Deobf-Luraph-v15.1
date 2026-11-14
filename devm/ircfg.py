"""IR graph -> basic blocks with folded expression trees and named variables."""
from .lifter import tree_ends

PURE_LEAF = ('k', 'kfn', 'env', 'opaque', 'regs', 'arg', 'varargn', 'nargs', 'va', 'selfarg', 'v', 'forval')


class Block:
    __slots__ = ('id', 'stmts', 'term', 'preds')

    def __init__(self, id):
        self.id = id
        self.stmts = []
        self.term = None
        self.preds = set()

    def succs(self):
        t = self.term
        if t[0] == 'goto':
            return [t[1]]
        if t[0] == 'br':
            return [t[2], t[3]]
        if t[0] == 'forbr':
            return [t[3], t[4]]
        return []

    def retarget(self, old, new):
        t = self.term
        if t[0] == 'goto':
            if t[1] == old:
                self.term = ('goto', new)
        elif t[0] == 'br':
            self.term = ('br', t[1], new if t[2] == old else t[2], new if t[3] == old else t[3])
        elif t[0] == 'forbr':
            self.term = ('forbr', t[1], t[2], new if t[3] == old else t[3], new if t[4] == old else t[4])


# ------------------------------------------------------------------ expression utilities
def map_expr(e, fn):
    """Bottom-up rewrite of an expression tree."""
    t = e[0]
    if t in ('bin',):
        e = ('bin', e[1], map_expr(e[2], fn), map_expr(e[3], fn))
    elif t == 'un':
        e = ('un', e[1], map_expr(e[2], fn))
    elif t in ('index', 'and', 'or'):
        e = (t, map_expr(e[1], fn), map_expr(e[2], fn))
    elif t == 'call':
        e = ('call', map_expr(e[1], fn), [map_expr(a, fn) for a in e[2]], map_expr(e[3], fn) if e[3] else None)
    elif t == 'method':
        e = ('method', map_expr(e[1], fn), e[2])
    elif t == 'closure':
        e = ('closure', e[1], tuple((c[0], map_expr(c[1], fn)) if isinstance(c[1], tuple) else c for c in e[2]))
    elif t == 'table':
        e = ('table', [(map_expr(k, fn) if k is not None else None, map_expr(v, fn)) for k, v in e[1]],
             map_expr(e[2], fn) if e[2] else None)
    return fn(e)


def walk_expr(e, out):
    """Collect every sub-expression (pre-order) into out."""
    out.append(e)
    t = e[0]
    if t == 'bin':
        walk_expr(e[2], out); walk_expr(e[3], out)
    elif t == 'un':
        walk_expr(e[2], out)
    elif t in ('index', 'and', 'or'):
        walk_expr(e[1], out); walk_expr(e[2], out)
    elif t == 'call':
        walk_expr(e[1], out)
        for a in e[2]:
            walk_expr(a, out)
        if e[3]:
            walk_expr(e[3], out)
    elif t == 'method':
        walk_expr(e[1], out)
    elif t == 'closure':
        for c in e[2]:
            if isinstance(c[1], tuple):
                walk_expr(c[1], out)
    elif t == 'table':
        for k, v in e[1]:
            if k is not None:
                walk_expr(k, out)
            walk_expr(v, out)
        if e[2]:
            walk_expr(e[2], out)
    return out


def stmt_exprs(st):
    """(reads, writes): expression slots of a statement. writes are dst atoms."""
    t = st[0]
    if t == 'set':
        return [st[2]], [st[1]]
    if t == 'setindex':
        return [st[1], st[2], st[3]], []
    if t == 'callstmt':
        return [st[1]], []
    if t == 'multiset':
        return [st[2]], list(st[1])
    if t == 'setlist':
        return [st[1]] + list(st[3]) + ([st[4]] if st[4] else []), []
    if t in ('numforprep', 'genforprep'):
        return list(st[1]), []
    if t == 'ret':
        return list(st[1]) + ([st[2]] if st[2] else []), []
    if t == 'br':
        return [st[1]], []
    if t == 'forbr':
        return [], [('r', n) for n in st[2]]
    return [], []


def map_stmt(st, fn):
    """Rewrite read-expressions of a statement / terminator."""
    t = st[0]
    if t == 'set':
        return ('set', st[1], map_expr(st[2], fn))
    if t == 'setindex':
        return ('setindex', map_expr(st[1], fn), map_expr(st[2], fn), map_expr(st[3], fn))
    if t == 'callstmt':
        return ('callstmt', map_expr(st[1], fn))
    if t == 'calldef':
        return ('calldef', st[1], map_expr(st[2], fn))
    if t == 'multiset':
        return ('multiset', st[1], map_expr(st[2], fn))
    if t == 'setlist':
        return ('setlist', map_expr(st[1], fn), st[2], [map_expr(v, fn) for v in st[3]],
                map_expr(st[4], fn) if st[4] else None)
    if t in ('numforprep', 'genforprep'):
        return (t, [map_expr(v, fn) for v in st[1]]) + tuple(st[2:])
    if t == 'ret':
        return ('ret', [map_expr(v, fn) for v in st[1]], map_expr(st[2], fn) if st[2] else None)
    if t == 'br':
        return ('br', map_expr(st[1], fn), st[2], st[3])
    return st


def expr_reads(e):
    """registers / flags read by an expression."""
    regs = set()
    mem = False
    impure = False
    for x in walk_expr(e, []):
        t = x[0]
        if t == 'r':
            regs.add(x[1])
        elif t in ('index', 'up', 'global', 'un', 'bin', 'method'):
            mem = True
        elif t == 'call':
            impure = True
            mem = True
        elif t == 'closure':
            for c in x[2]:
                if c[0] in ('ref', 'val') and not isinstance(c[1], tuple):
                    regs.add(c[1])
    return regs, mem, impure


def stmt_effects(st):
    """(regs written, writes memory/has side effects)"""
    t = st[0]
    regs = set()
    impure = False
    reads, writes = stmt_exprs(st)
    for w in writes:
        if w[0] == 'r':
            regs.add(w[1])
        else:
            impure = True
    if t in ('setindex', 'callstmt', 'setlist', 'multiset', 'calldef'):
        impure = True
    for r in reads:
        for x in walk_expr(r, []):
            if x[0] == 'call':
                impure = True
    return regs, impure


def blocks_move(info, sj):
    """True when an expression with read-info `info` cannot be moved past statement sj."""
    regs, mem, impure = info
    wregs, wimp = stmt_effects(sj)
    if wregs & regs:
        return True
    if wimp and (mem or impure):
        return True
    if impure:
        if sj[0] == 'close':
            return False
        if sj[0] != 'set':
            return True
        for e in stmt_reads_raw(sj):
            if expr_reads(e)[1]:
                return True
    return False


def count_uses(e, pred, acc):
    for x in walk_expr(e, []):
        if pred(x):
            acc[x] = acc.get(x, 0) + 1


class ProtoCFG:
    def __init__(self, pir):
        self.pir = pir
        self.blocks = {}
        self.entry = None
        self.nparams = 0
        self.vararg = False
        self.captured_ref = set()
        self.warnings = []
        self.for_base = {}      # loop-test block -> base register of the loop (matches its setup)
        self._build()
        self._thread()
        self._to_exprs()
        self._detect_params()
        for _ in range(4):
            self._fold_regs()
            self._merge_tables()
        self._merge_conds()
        self._fold_regs()

    # -- stage A
    def _build(self):
        counter = [0]

        def make(tree, bid):
            b = Block(bid)
            self.blocks[bid] = b
            stmts, term = tree
            b.stmts = list(stmts)
            if term[0] == 'goto':
                b.term = ('goto', term[1])
            elif term[0] == 'ret':
                b.term = ('ret', list(term[1]), term[2])
            elif term[0] in ('br', 'forbr'):
                subs = []
                for sub in (term[2], term[3]) if term[0] == 'br' else (term[3], term[4]):
                    if not sub[0] and sub[1][0] == 'goto':
                        subs.append(sub[1][1])
                    else:
                        counter[0] += 1
                        sid = (bid, 'sub', counter[0])
                        make(sub, sid)
                        subs.append(sid)
                if term[0] == 'br':
                    b.term = ('br', term[1], subs[0], subs[1])
                else:
                    info = term[2]
                    regs = (info + 3,) if term[1] == 'num' else tuple(info)
                    b.term = ('forbr', term[1], regs, subs[0], subs[1])
                    ints = [r for r in regs if isinstance(r, int)]
                    if ints:
                        self.for_base[bid] = min(ints) - 3
            else:
                raise ValueError(term[0])

        for key, node in self.pir.nodes.items():
            make(node.tree, key)
        self.entry = self.pir.entry

    def compute_preds(self):
        for b in self.blocks.values():
            b.preds = set()
        for b in self.blocks.values():
            for s in b.succs():
                self.blocks[s].preds.add(b.id)

    def _thread(self):
        blocks = self.blocks
        # skip empty forwarding blocks
        fwd = {}
        for b in blocks.values():
            if not b.stmts and b.term[0] == 'goto' and b.term[1] != b.id:
                fwd[b.id] = b.term[1]

        def resolve(x):
            seen = set()
            while x in fwd and x not in seen:
                seen.add(x)
                x = fwd[x]
            return x

        for b in blocks.values():
            for s in list(b.succs()):
                r = resolve(s)
                if r != s:
                    b.retarget(s, r)
        self.entry = resolve(self.entry)
        self._prune()
        # merge linear chains
        changed = True
        while changed:
            changed = False
            self.compute_preds()
            for b in list(blocks.values()):
                if b.id not in blocks:
                    continue
                while b.term[0] == 'goto':
                    s = blocks[b.term[1]]
                    if s.id == b.id or s.id == self.entry or len(s.preds) != 1:
                        break
                    b.stmts.extend(s.stmts)
                    b.term = s.term
                    del blocks[s.id]
                    for x in b.succs():
                        blocks[x].preds.discard(s.id)
                        blocks[x].preds.add(b.id)
                    changed = True
        self.compute_preds()

    def _prune(self):
        seen = set()
        stack = [self.entry]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            stack.extend(self.blocks[x].succs())
        for k in list(self.blocks):
            if k not in seen:
                del self.blocks[k]

    # -- stage C: temporaries -> expression trees
    def _to_exprs(self):
        # 1. normalise calls and special statements
        tcount = {}
        for b in self.blocks.values():
            for st in b.stmts + [b.term]:
                for e in stmt_reads_raw(st):
                    count_uses(e, lambda x: x[0] in ('t', 'mv'), tcount)
        tails = {}
        for b in self.blocks.values():
            new = []
            for st in b.stmts:
                t = st[0]
                if t == 'call':
                    _, cid, f, args, tail, nres = st
                    tl = None
                    if tail is not None:
                        tl = ('va',) if tail == 'va' else ('mvall', tail)
                    new.append(('calldef', cid, ('call', f, list(args), tl)))
                elif t == 'setmulti':
                    if st[2] == 'va':
                        self.vararg = True
                    continue
                elif t == 'setlist':
                    tl = None
                    if st[4] is not None:
                        tl = ('va',) if st[4] == 'va' else ('mvall', st[4])
                    new.append(('setlist', st[1], st[2], list(st[3]), tl))
                elif t == 'numforprep' or t == 'genforprep':
                    a = st[1]
                    new.append((t, [('r', a), ('r', a + 1), ('r', a + 2)], a))
                elif t == 'forpop':
                    continue
                else:
                    new.append(st)
            b.stmts = new
            if b.term[0] == 'ret':
                tail = b.term[2]
                tl = None
                if tail is not None:
                    tl = ('va',) if tail == 'va' else ('mvall', tail)
                b.term = ('ret', list(b.term[1]), tl)
        # SELF pattern: t = X; R[A+1] = t; t2 = t[k]; R[A] = t2
        for b in self.blocks.values():
            s = b.stmts
            i = 0
            while i + 2 < len(s):
                a, c, d = s[i], s[i + 1], s[i + 2]
                if (a[0] == 'set' and a[1][0] == 'r' and a[2][0] == 't' and c[0] == 'set' and c[1][0] == 't'
                        and c[2][0] == 'index' and c[2][1] == a[2] and c[2][2][0] == 'k'
                        and isinstance(c[2][2][1], str) and d[0] == 'set' and d[1][0] == 'r' and d[2] == c[1]
                        and d[1][1] == a[1][1] - 1):
                    s[i:i + 3] = [a, ('set', d[1], ('index', a[1], c[2][2]))]
                    tcount[a[2]] = tcount.get(a[2], 0) - 1
                    tcount.pop(c[1], None)
                i += 1
        # 2. forward substitution of single-assignment temporaries
        self.tmp_n = 0
        for b in self.blocks.values():
            self._inline_block(b, tcount)
        # leftover temporaries become pseudo registers
        def fix(e):
            if e[0] == 't':
                return ('r', 't' + str(e[1]))
            if e[0] == 'mvall':
                self.warnings.append('unresolved multi-value %s' % (e[1],))
                return ('opaque', 'multi_%s' % (e[1],))
            if e[0] == 'mv':
                return ('r', 'c%s_%d' % (e[1], e[2]))
            return e
        for b in self.blocks.values():
            out = []
            for st in b.stmts:
                if st[0] == 'calldef':
                    # results still referenced through mv(...)
                    cid = st[1]
                    n = max([k[2] for k in tcount if k[0] == 'mv' and k[1] == cid and tcount[k] > 0] or [0])
                    if n == 0:
                        out.append(('callstmt', map_expr(st[2], fix)))
                    else:
                        out.append(('multiset', [('r', 'c%s_%d' % (cid, i)) for i in range(1, n + 1)], map_expr(st[2], fix)))
                    continue
                st = map_stmt(st, fix)
                if st[0] == 'set' and st[1][0] == 't':
                    st = ('set', ('r', 't' + str(st[1][1])), st[2])
                out.append(st)
            b.stmts = out
            b.term = map_stmt(b.term, fix)
        # group  R[a], R[a+1].. = call
        for b in self.blocks.values():
            self._group_multiset(b)

    def _inline_block(self, b, tcount):
        stmts = b.stmts + [b.term]
        changed = True
        while changed:
            changed = False
            i = 0
            while i < len(stmts) - 1:
                st = stmts[i]
                key = None
                if st[0] == 'set' and st[1][0] == 't':
                    key = st[1]
                    expr = st[2]
                    single = tcount.get(key, 0) == 1
                    dup = expr[0] in ('k', 'kfn', 'env', 'arg', 'varargn', 'selfarg') or (expr[0] == 'global' and False)
                    if tcount.get(key, 0) == 0:
                        if expr_reads(expr)[2]:
                            i += 1
                            continue
                        del stmts[i]
                        changed = True
                        continue
                elif st[0] == 'calldef':
                    cid = st[1]
                    expr = st[2]
                    used = [k for k in tcount if k[0] == 'mv' and k[1] == cid and tcount[k] > 0]
                    if used == [('mv', cid, 1)] and tcount[used[0]] == 1:
                        key = ('mv', cid, 1)
                    else:
                        key = ('mvall', cid)
                    single = True
                    dup = False
                else:
                    i += 1
                    continue
                regs, mem, impure = expr_reads(expr)
                # find uses going forward
                j = i + 1
                done = False
                remaining = tcount.get(key, 1) if key[0] != 'mvall' else 1
                moved = False
                while j < len(stmts):
                    sj = stmts[j]
                    uses = 0
                    for e in stmt_reads_raw(sj):
                        for x in walk_expr(e, []):
                            if x == key:
                                uses += 1
                    if uses:
                        if single or dup or expr[0] == 'r':
                            if uses > 1 and not (dup or expr[0] == 'r'):
                                break
                            stmts[j] = map_stmt(sj, lambda x, key=key, expr=expr: expr if x == key else x)
                            remaining -= uses
                            moved = True
                            if remaining <= 0 or single:
                                done = True
                                break
                        else:
                            break
                    if blocks_move((regs, mem, impure), sj):
                        break
                    j += 1
                if done:
                    del stmts[i]
                    if key[0] != 'mvall':
                        tcount[key] = 0
                    changed = True
                    continue
                if moved:
                    tcount[key] = remaining
                i += 1
        b.term = stmts[-1]
        b.stmts = stmts[:-1]

    def _group_multiset(self, b):
        out = []
        s = b.stmts
        i = 0
        while i < len(s):
            st = s[i]
            if st[0] == 'multiset':
                names = [d[1] for d in st[1]]
                dsts = list(st[1])
                j = i + 1
                k = 0
                while j < len(s) and k < len(names) and s[j][0] == 'set' and s[j][2] == ('r', names[k]) and s[j][1][0] == 'r':
                    dsts[k] = s[j][1]
                    k += 1
                    j += 1
                if k == len(names):
                    out.append(('multiset', dsts, st[2]))
                    i = j
                    continue
            out.append(st)
            i += 1
        b.stmts = out

    # -- parameters
    def _detect_params(self):
        b = self.blocks[self.entry]
        n = 0
        while n < len(b.stmts):
            st = b.stmts[n]
            if st[0] == 'set' and st[1][0] == 'r' and st[2] == ('arg', n + 1) and st[1][1] == n + 1:
                n += 1
            else:
                break
        self.nparams = n
        b.stmts = b.stmts[n:]
        for blk in self.blocks.values():
            for st in blk.stmts + [blk.term]:
                for e in stmt_exprs(st)[0]:
                    for x in walk_expr(e, []):
                        if x[0] in ('va', 'varargn'):
                            self.vararg = True
                        elif x[0] == 'closure':
                            for c in x[2]:
                                if c[0] == 'ref':
                                    self.captured_ref.add(c[1])

    # -- liveness of registers
    def liveness(self):
        use, defs = {}, {}
        for b in self.blocks.values():
            u, d = set(), set()
            for st in b.stmts + [b.term]:
                reads, writes = stmt_exprs(st)
                for e in reads:
                    for r in expr_reads(e)[0]:
                        if r not in d:
                            u.add(r)
                for w in writes:
                    if w[0] == 'r':
                        d.add(w[1])
            use[b.id], defs[b.id] = u, d
        live_in = {k: set() for k in self.blocks}
        live_out = {k: set() for k in self.blocks}
        changed = True
        order = list(self.blocks)
        while changed:
            changed = False
            for k in reversed(order):
                b = self.blocks[k]
                out = set()
                for s in b.succs():
                    out |= live_in[s]
                inn = use[k] | (out - defs[k])
                if out != live_out[k] or inn != live_in[k]:
                    live_out[k], live_in[k] = out, inn
                    changed = True
        return live_in, live_out

    # -- stage C3: fold single-use registers
    def captured_open(self):
        """CIN[b]: registers whose by-reference capture is still open at block entry."""
        cap = self.captured_ref
        CIN = {k: set() for k in self.blocks}
        if not cap:
            return CIN
        COUT = {}
        self.compute_preds()
        changed = True
        while changed:
            changed = False
            for bid, b in self.blocks.items():
                cur = set()
                for p in b.preds:
                    cur |= COUT.get(p, set())
                CIN[bid] = set(cur)
                for st in b.stmts + [b.term]:
                    cur = cap_step(cur, st)
                if COUT.get(bid) != cur:
                    COUT[bid] = cur
                    changed = True
        return CIN

    def _fold_regs(self):
        live_in, live_out = self.liveness()
        cap = self.captured_ref
        CIN = self.captured_open()

        def is_captured(b, stmts, i, j, n):
            """register n captured-open at stmt i, or captured by reference in stmts (i, j]"""
            if n not in cap:
                return False
            cur = set(CIN[b.id])
            for k in range(0, i):
                cur = cap_step(cur, stmts[k])
            if n in cur:
                return True
            for k in range(i, min(j + 1, len(stmts))):
                for e in stmt_exprs(stmts[k])[0]:
                    for x in walk_expr(e, []):
                        if x[0] == 'closure' and any(c[0] == 'ref' and c[1] == n for c in x[2]):
                            return True
            return False

        for b in self.blocks.values():
            stmts = b.stmts + [b.term]
            changed = True
            while changed:
                changed = False
                i = 0
                while i < len(stmts) - 1:
                    st = stmts[i]
                    if not (st[0] == 'set' and st[1][0] == 'r'):
                        i += 1
                        continue
                    n = st[1][1]
                    expr = st[2]
                    if expr[0] == 'closure' and any(c[0] == 'ref' and c[1] == n for c in expr[2]):
                        i += 1
                        continue
                    regs, mem, impure = expr_reads(expr)
                    j = i + 1
                    target = None
                    selfcall = False
                    while j < len(stmts):
                        sj = stmts[j]
                        reads, writes = stmt_exprs(sj)
                        uses = sum(1 for e in reads for x in walk_expr(e, []) if x == ('r', n))
                        if sj[0] == 'forbr':
                            uses = 0
                        if uses:
                            if uses == 1:
                                target = j
                            elif uses == 2 and _self_call(sj, n):
                                target = j
                                selfcall = True
                            break
                        if n in stmt_effects(sj)[0]:
                            break
                        if blocks_move((regs, mem, impure), sj):
                            break
                        j += 1
                    if target is None or is_captured(b, stmts, i, target, n):
                        i += 1
                        continue
                    # R[n] must be dead after the use
                    dead = None
                    k = target
                    wregs, _ = stmt_effects(stmts[target])
                    if n in wregs:
                        dead = True
                    k = target + 1
                    while dead is None and k < len(stmts):
                        reads, writes = stmt_exprs(stmts[k])
                        if any(x == ('r', n) for e in reads for x in walk_expr(e, [])):
                            dead = False
                            break
                        if any(w == ('r', n) for w in writes):
                            dead = True
                            break
                        k += 1
                    if dead is None:
                        dead = n not in live_out[b.id]
                    if not dead:
                        i += 1
                        continue
                    sj = stmts[target]
                    if selfcall:
                        def mk(x, n=n, expr=expr):
                            if x[0] == 'call' and x[1][0] == 'index' and x[1][1] == ('r', n) and x[2] and x[2][0] == ('r', n):
                                return ('call', ('method', expr, x[1][2][1]), [('selfarg',)] + list(x[2][1:]), x[3])
                            return x
                        stmts[target] = map_stmt(sj, mk)
                    else:
                        stmts[target] = map_stmt(sj, lambda x, n=n, expr=expr: expr if x == ('r', n) else x)
                    del stmts[i]
                    changed = True
                    if i > 0:
                        i -= 1
            b.term = stmts[-1]
            b.stmts = stmts[:-1]

    # -- table constructors
    def _merge_tables(self):
        for b in self.blocks.values():
            s = b.stmts
            i = 0
            while i < len(s):
                st = s[i]
                if st[0] == 'set' and st[1][0] == 'r' and st[2][0] in ('newtable', 'table'):
                    dst = st[1]
                    if st[2][0] == 'newtable':
                        items, tail = [], None
                    else:
                        items, tail = list(st[2][1]), st[2][2]
                    j = i + 1
                    npos = sum(1 for k, v in items if k is None)
                    while j < len(s) and tail is None:
                        sj = s[j]
                        if sj[0] == 'setindex' and sj[1] == dst and sj[2][0] == 'k' and not _mentions(sj[3], dst) \
                                and not _mentions(sj[2], dst):
                            if isinstance(sj[2][1], str) or True:
                                items.append((sj[2], sj[3]))
                                j += 1
                                continue
                        if sj[0] == 'setlist' and sj[1] == dst and sj[2] == npos + 1 \
                                and not any(_mentions(v, dst) for v in sj[3]):
                            for v in sj[3]:
                                items.append((None, v))
                                npos += 1
                            tail = sj[4]
                            j += 1
                            continue
                        break
                    if j > i + 1:
                        s[i:j] = [('set', dst, ('table', items, tail))]
                i += 1

    # -- compound conditions
    def _merge_conds(self):
        blocks = self.blocks
        changed = True
        while changed:
            changed = False
            self.compute_preds()
            for b in list(blocks.values()):
                if b.id not in blocks or b.term[0] != 'br':
                    continue
                _, c1, t, f = b.term
                for which in (0, 1):
                    x = blocks.get(t if which == 0 else f)
                    other = f if which == 0 else t
                    if x is None or x.id == b.id or x.stmts or x.term[0] != 'br' or len(x.preds) != 1 or x.id == self.entry:
                        continue
                    _, c2, t2, f2 = x.term
                    if which == 0:
                        # c1 true -> test c2
                        if f2 == other:
                            b.term = ('br', ('and', c1, c2), t2, other)
                        elif t2 == other:
                            b.term = ('br', ('and', c1, negate(c2)), f2, other)
                        else:
                            continue
                    else:
                        if t2 == other:
                            b.term = ('br', ('or', c1, c2), other, f2)
                        elif f2 == other:
                            b.term = ('br', ('or', c1, negate(c2)), other, t2)
                        else:
                            continue
                    del blocks[x.id]
                    changed = True
                    break
                if changed:
                    break
        self.compute_preds()


def cap_step(cur, st):
    if st[0] == 'close':
        cur = {r for r in cur if not (isinstance(r, int) and r >= st[1])}
    for e in stmt_exprs(st)[0]:
        for x in walk_expr(e, []):
            if x[0] == 'closure':
                for c in x[2]:
                    if c[0] == 'ref':
                        cur = cur | {c[1]}
    return cur


def _self_call(st, n):
    """statement contains  R[n][k](R[n], ...)  and no other use of R[n]"""
    for e in stmt_exprs(st)[0]:
        for x in walk_expr(e, []):
            if x[0] == 'call' and x[1][0] == 'index' and x[1][1] == ('r', n) and x[1][2][0] == 'k' \
                    and isinstance(x[1][2][1], str) and x[2] and x[2][0] == ('r', n):
                return True
    return False


def _mentions(e, target):
    return any(x == target for x in walk_expr(e, []))


NEG = {'==': '~=', '~=': '==', '<': '>=', '>=': '<', '<=': '>', '>': '<='}


def negate(c):
    if c[0] == 'un' and c[1] == 'not':
        return c[2]
    if c[0] == 'bin' and c[1] in ('==', '~='):
        return ('bin', NEG[c[1]], c[2], c[3])
    if c[0] == 'k' and isinstance(c[1], bool):
        return ('k', not c[1])
    if c[0] == 'and':
        return ('or', negate(c[1]), negate(c[2]))
    if c[0] == 'or':
        return ('and', negate(c[1]), negate(c[2]))
    return ('un', 'not', c)


def stmt_reads_raw(st):
    """Read expressions for raw IR statements (before normalisation) and normalised ones."""
    t = st[0]
    if t == 'call':
        return [st[2]] + list(st[3])
    if t == 'calldef':
        return [st[2]]
    if t == 'goto' or t == 'forbr':
        return []
    if t == 'ret':
        return list(st[1]) + ([st[2]] if isinstance(st[2], tuple) else [])
    if t == 'setlist':
        return [st[1]] + list(st[3]) + ([st[4]] if isinstance(st[4], tuple) else [])
    if t in ('numforprep', 'genforprep'):
        return list(st[1]) if isinstance(st[1], list) else []
    return stmt_exprs(st)[0]
