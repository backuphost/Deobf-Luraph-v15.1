"""IR graph -> basic blocks with folded expression trees and named variables."""
from .lifter import tree_ends

PURE_LEAF = ('k', 'kfn', 'env', 'opaque', 'regs', 'arg', 'varargn', 'nargs', 'va', 'selfarg', 'v', 'forval', 'constfn',
             'outer', 'upcell')


import os
CELLS = (1, 3)        # upvalue cell layout of the VM being lifted (set by the lifter)
DEBUG_UNFLATTEN = bool(os.environ.get('DEVM_DEBUG_UNFLATTEN'))


# Undo state-machine control flow (see ProtoCFG._unflatten).  Scripts protected from source have
# none in their payload, and there the pass only splits loops on the script's own boolean flags.
UNFLATTEN = True


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
        return (t, [map_expr(v, fn) for v in st[1]])
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


_CMPS = ('<', '<=', '>', '>=', '==', '~=')


def _isnum(e):
    return e[0] == 'k' and isinstance(e[1], (int, float)) and not isinstance(e[1], bool)


def _isboolexpr(e):
    if (e[0] == 'bin' and e[1] in _CMPS) or (e[0] == 'un' and e[1] == 'not'):
        return True
    # lookup in a {[true] = false, [false] = true} style table: Luraph's spelling of `not`
    return e[0] == 'index' and _isboolexpr(e[2])


def _reads_of(st):
    """[(register, used only in a comparison against a numeric constant)] for a statement."""
    out = []

    def scan(e):
        t = e[0]
        if t == 'r':
            out.append((e[1], False))
        elif t == 'bin':
            if e[1] in _CMPS:
                a, b = e[2], e[3]
                if a[0] == 'r' and _isnum(b):
                    out.append((a[1], True))
                    return
                if b[0] == 'r' and _isnum(a):
                    out.append((b[1], True))
                    return
            scan(e[2]); scan(e[3])
        elif t == 'un':
            scan(e[2])
        elif t in ('index', 'and', 'or'):
            scan(e[1]); scan(e[2])
        elif t == 'call':
            scan(e[1])
            for a in e[2]:
                scan(a)
            if e[3]:
                scan(e[3])
        elif t == 'method':
            scan(e[1])
        elif t == 'closure':
            for c in e[2]:
                if isinstance(c[1], tuple):
                    scan(c[1])
                elif c[0] in ('ref', 'val'):
                    out.append((c[1], False))
        elif t == 'table':
            for k, v in e[1]:
                if k is not None:
                    scan(k)
                scan(v)
            if e[2]:
                scan(e[2])

    for e in stmt_exprs(st)[0]:
        scan(e)
    return out


def _writes_of(st):
    return [w[1] for w in stmt_exprs(st)[1] if w[0] == 'r']


def rpo_order(cfg):
    seen = set()
    post = []
    stack = [(cfg.entry, iter(cfg.blocks[cfg.entry].succs()))]
    seen.add(cfg.entry)
    while stack:
        bid, it = stack[-1]
        adv = False
        for s in it:
            if s not in seen:
                seen.add(s)
                stack.append((s, iter(cfg.blocks[s].succs())))
                adv = True
                break
        if not adv:
            post.append(bid)
            stack.pop()
    post.reverse()
    return post


# function key -> {index of an upvalue that holds an opaque state number: its value when the function
# runs, if known} (filled in by the parent)
OPAQUE_UPS = {}
# function key -> indexes of the upvalues that hold a function captured by value
FUNCTION_UPS = {}


def _opaque_eval(e, ups):
    """Value of an expression over opaque state numbers, None when it is not determined."""
    if e[0] == 'k':
        return e[1] if _isnum(e) else None
    if e[0] == 'up':
        return ups.get(e[1])
    if e[0] == 'bin':
        x, y = _opaque_eval(e[2], ups), _opaque_eval(e[3], ups)
        if x is None or y is None:
            return None
        try:
            return {'+': x + y, '-': x - y, '*': x * y}[e[1]] if e[1] != '%' else x % y
        except (KeyError, ZeroDivisionError):
            return None
    if e[0] == 'call' and e[1][0] == 'kfn':
        args = [_opaque_eval(x, ups) for x in e[2]]
        if any(x is None or x != int(x) for x in args):
            return None
        args = [int(x) & 0xFFFFFFFF for x in args]
        name = str(e[1][1])
        if name == 'bit32.bxor':
            r = 0
            for x in args:
                r ^= x
            return r
        if name == 'bit32.band':
            r = 0xFFFFFFFF
            for x in args:
                r &= x
            return r
        if name == 'bit32.bor':
            r = 0
            for x in args:
                r |= x
            return r
        if name == 'bit32.bnot' and len(args) == 1:
            return ~args[0] & 0xFFFFFFFF
        if name == 'bit32.lshift' and len(args) == 2:
            return (args[0] << args[1]) & 0xFFFFFFFF if args[1] < 32 else 0
        if name == 'bit32.rshift' and len(args) == 2:
            return args[0] >> args[1] if args[1] < 32 else 0
    return None


class ProtoCFG:
    def __init__(self, pir):
        self.pir = pir
        self.blocks = {}
        self.entry = None
        self.nparams = 0
        self.vararg = False
        self.captured_ref = set()
        self.warnings = []
        self.param_regs = None
        self._build()
        # explicit parameter block (register base, count) emitted by the function prologue; the
        # prologue need not be the first instruction
        for b in [self.blocks[self.entry]] + [x for x in self.blocks.values() if x is not self.blocks[self.entry]]:
            found = False
            for i, st in enumerate(b.stmts):
                if st[0] == 'params':
                    self.param_regs = list(range(st[1], st[1] + st[2]))
                    self.nparams = st[2]
                    del b.stmts[i]
                    found = True
                    break
            if found:
                break
        self._thread()
        self._to_exprs()
        if self._thread_same_test():
            self._thread()
        self._dead_stores()
        if self._thread_same_test():
            self._thread()
        self._detect_params()
        self._cut_cold_magic()
        if UNFLATTEN and not os.environ.get('DEVM_NO_UNFLATTEN'):
            self._unflatten()
        for _ in range(4):
            self._fold_regs()
            self._merge_tables()
        self._merge_conds()
        self._fold_regs()
        if self._thread_same_test():
            self._thread()
            self._merge_conds()
            self._fold_regs()
        if self._cut_cold_edges():
            self._dead_stores()
            for _ in range(2):
                self._fold_regs()
                self._merge_tables()
            self._merge_conds()
            self._fold_regs()
        if self._collapse_bool_diamonds():
            self._thread()
            self._dead_stores()
            self._fold_regs()
            for _ in range(12):
                if not self._recover_for_loops():
                    break
                for b in self.blocks.values():
                    b.stmts = [st for st in b.stmts if not (st[0] == 'set' and st[1] == st[2])]
                self._thread()
                self._dead_stores()
                self._fold_regs()
        if self._thread_same_test():
            self._thread()
            self._merge_conds()
            self._fold_regs()
        if self._unthread_tests():
            self._thread()
            self._merge_conds()
            self._fold_regs()
        if self._unsplice():
            self._table_aliases()
            for _ in range(2):
                self._fold_regs()
                self._merge_tables()
        self._open_upcells()
        self._dead_stores()

    def _table_aliases(self):
        """`B = {}; A = B; A[1] = x; A[2] = y` fills B: the copy the interpreter works on is
        replaced by the table itself."""
        _, live_out = self.liveness()
        done = 0
        for b in self.blocks.values():
            i = 0
            while i < len(b.stmts):
                st = b.stmts[i]
                i += 1
                if not (st[0] == 'set' and st[1][0] == 'r' and st[2][0] == 'r' and st[1] != st[2]):
                    continue
                A, B = st[1], st[2]
                fresh = False
                for j in range(i - 2, -1, -1):
                    if B[1] in _writes_of(b.stmts[j]):
                        fresh = b.stmts[j][0] == 'set' and b.stmts[j][2][0] in ('newtable', 'table')
                        break
                if not fresh:
                    continue
                j = i
                ok = True
                while j < len(b.stmts):
                    sj = b.stmts[j]
                    if sj[0] in ('setlist', 'setindex') and sj[1] == A:
                        rest = [e for e in stmt_exprs(sj)[0][1:]]
                        if any(A[1] in expr_reads(e)[0] for e in rest):
                            ok = False
                            break
                        j += 1
                        continue
                    break
                if not ok or j == i:
                    continue
                # the copy must not be looked at again
                alive = A[1] in live_out.get(b.id, ())
                for sk in b.stmts[j:] + [b.term]:
                    rd, wr = stmt_exprs(sk)
                    if any(A[1] in expr_reads(e)[0] for e in rd):
                        alive = True
                        break
                    if A in wr:
                        alive = False
                        break
                if alive:
                    continue
                for k in range(i, j):
                    sk = b.stmts[k]
                    b.stmts[k] = (sk[0], B) + tuple(sk[2:])
                del b.stmts[i - 1]
                i -= 1
                done += 1
        return done

    def _unsplice(self):
        """Calls that pass on all results of another call.  The interpreter does this with a
        packed table:

            R = table.pack(g(...))                       R = table.pack(g(...))
            f(unpack(R, 1, R.n))                         T = R.n;  R.n = K + T - 1
                                                         table.move(R, 1, T, K, R)
                                                         R[1] = a1 ... R[K-1] = a(K-1)
                                                         f(unpack(R, 1, R.n))

        which is `f(g(...))` and `f(a1, ..., g(...))`; `table.move(R, 1, R.n, K, D)` is the tail of
        a table constructor."""
        PACK, MOVE, UNPACK = ('kfn', 'table.pack'), ('kfn', 'table.move'), ('kfn', 'unpack')
        done = 0
        _, live_out = self.liveness()

        def pure(e):
            return not any(x[0] in ('call', 'closure') for x in walk_expr(e, []))

        for b in self.blocks.values():
            i = 0
            while i < len(b.stmts):
                st = b.stmts[i]
                i += 1
                if not (st[0] == 'set' and st[1][0] == 'r' and st[2][0] == 'call' and st[2][1] == PACK
                        and not st[2][2] and len(st[2]) > 3 and st[2][3] is not None):
                    continue
                R, M = st[1], st[2][3]
                mreads = expr_reads(M)[0]
                use = ('call', UNPACK, [R, ('k', 1), ('index', R, ('k', 'n'))], None)
                T, K, moved, prefix, drop = None, 1, False, {}, [i - 1]
                result = None
                j = i
                while j <= len(b.stmts):
                    sj = b.stmts[j] if j < len(b.stmts) else b.term
                    last = j == len(b.stmts)
                    if sj[0] == 'set' and sj[2] == ('index', R, ('k', 'n')) and T is None and sj[1][0] == 'r':
                        T = sj[1]
                        drop.append(j)
                    elif (sj[0] == 'setindex' and sj[1] == R and sj[2] == ('k', 'n') and T is not None and K == 1
                          and sj[3][0] == 'bin' and sj[3][1] == '-' and sj[3][3] == ('k', 1) and sj[3][2][0] == 'bin'
                          and sj[3][2][1] == '+' and _isnum(sj[3][2][2]) and sj[3][2][3] == T):
                        K = int(sj[3][2][2][1])
                        drop.append(j)
                    elif (sj[0] == 'callstmt' and sj[1][0] == 'call' and sj[1][1] == MOVE and len(sj[1][2]) == 5
                          and sj[1][2][0] == R and sj[1][2][1] == ('k', 1) and sj[1][2][4] == R
                          and T is not None and sj[1][2][2] == T and sj[1][2][3] == ('k', K) and not moved):
                        moved = True
                        drop.append(j)
                    elif (sj[0] == 'callstmt' and sj[1][0] == 'call' and sj[1][1] == MOVE and len(sj[1][2]) == 5
                          and sj[1][2][0] == R and sj[1][2][1] == ('k', 1) and sj[1][2][2] == ('index', R, ('k', 'n'))
                          and _isnum(sj[1][2][3]) and sj[1][2][4] != R and T is None and K == 1
                          and R[1] not in expr_reads(sj[1][2][4])[0]):
                        # the tail of a table constructor
                        result = ('setlist', sj[1][2][4], int(sj[1][2][3][1]), [], M)
                        break
                    elif (sj[0] == 'setindex' and sj[1] == R and _isnum(sj[2]) and moved and 1 <= sj[2][1] < K
                          and R[1] not in expr_reads(sj[3])[0]):
                        prefix[int(sj[2][1])] = sj[3]
                        drop.append(j)
                    elif (sj[0] == 'setlist' and sj[1] == R and sj[4] is None and moved
                          and all(1 <= sj[2] + n < K and R[1] not in expr_reads(v)[0] for n, v in enumerate(sj[3]))):
                        for n, v in enumerate(sj[3]):
                            prefix[sj[2] + n] = v
                        drop.append(j)
                    else:
                        found = [0]

                        def fix(x):
                            if x[0] == 'call' and len(x) > 3 and x[3] == use:
                                found[0] += 1
                                return ('call', x[1], list(x[2]) + [prefix[n] for n in range(1, K)], M)
                            return x
                        complete = (K == 1 and T is None) or (moved and all(n in prefix for n in range(1, K)))
                        if sj[0] == 'ret' and sj[2] == use and complete:
                            result = ('ret', list(sj[1]) + [prefix[n] for n in range(1, K)], M)
                            break
                        new = map_stmt(sj, fix) if complete and sj[0] != 'ret' else sj
                        if found[0] == 1:
                            rest = [x for e in stmt_exprs(new)[0] for x in walk_expr(e, [])]
                            if R in rest and not any(x == M for x in rest):
                                break
                            result = new
                            break
                        # anything else in between must not disturb the packed call or the table
                        rd, wr = stmt_exprs(sj)
                        regs = set()
                        for e in rd:
                            regs |= expr_reads(e)[0]
                        if last or R[1] in regs or (T is not None and T[1] in regs) or not all(pure(e) for e in rd) \
                                or sj[0] not in ('set', 'setindex', 'setlist') \
                                or any(w[0] == 'r' and (w[1] in mreads or w == R or w == T) for w in wr):
                            break
                    j += 1
                if result is None:
                    continue
                # the table (and the saved count) must be gone afterwards
                after = b.stmts[j + 1:] + ([b.term] if j < len(b.stmts) else [])
                alive = False
                for x in (R, T):
                    if x is None:
                        continue
                    dead = False
                    for sk in after:
                        rd, wr = stmt_exprs(sk)
                        if any(x[1] in expr_reads(e)[0] for e in rd):
                            alive = True
                            break
                        if x in wr:
                            dead = True
                            break
                    if not dead and not alive and x[1] in live_out.get(b.id, ()):
                        alive = True
                if alive:
                    continue
                if j < len(b.stmts):
                    b.stmts[j] = result
                else:
                    b.term = result
                for d in sorted(drop, reverse=True):
                    del b.stmts[d]
                done += 1
                i = max(0, i - 1)
        return done

    def _reaching(self):
        """(block order, IN): IN[b][reg] = frozenset of definitions (bid, stmt index | 'T') or
        ('ENTRY', reg) reaching the start of block b."""
        blocks = self.blocks
        order = rpo_order(self)
        gen = {}
        for bid in order:
            g = {}
            b = blocks[bid]
            for i, st in enumerate(b.stmts):
                for r in _writes_of(st):
                    g[r] = frozenset([(bid, i)])
            for r in _writes_of(b.term):
                g[r] = frozenset([(bid, 'T')])
            gen[bid] = g
        self.compute_preds()
        universe = set()
        for bid in order:
            universe.update(gen[bid])
        IN = {bid: {} for bid in order}
        OUT = {bid: dict(gen[bid]) for bid in order}
        changed = True
        while changed:
            changed = False
            for bid in order:
                b = blocks[bid]
                inn = {}
                preds = [p for p in b.preds if p in OUT]
                for pr in preds:
                    for r, ds in OUT[pr].items():
                        cur = inn.get(r)
                        inn[r] = ds if cur is None else (cur | ds)
                if bid == self.entry:
                    # parameters / never-assigned registers
                    for r in universe:
                        inn[r] = inn.get(r, frozenset()) | frozenset([('ENTRY', r)])
                if inn != IN[bid]:
                    IN[bid] = inn
                    out = dict(inn)
                    out.update(gen[bid])
                    if out != OUT[bid]:
                        OUT[bid] = out
                    changed = True
        return order, IN

    def _dead_stores(self):
        """Drop register definitions that nothing reads (Luraph pads the code with them, and the
        loads feeding its self-modifying patches become dead once the patches are applied)."""
        # (a closure nothing reads is a function the script defines and never calls: that is the
        # author's code, not padding, and it stays)
        PURE = ('k', 'outer', 'constfn', 'upcell', 'r', 'kfn', 'global', 'env', 'newtable', 'opaque')
        captured = set()
        for b in self.blocks.values():
            for st in b.stmts + [b.term]:
                for e in stmt_exprs(st)[0]:
                    for x in walk_expr(e, []):
                        if x[0] == 'closure':
                            for c in x[2]:
                                if c[0] == 'ref' and not isinstance(c[1], tuple):
                                    captured.add(c[1])
        while True:
            order, IN = self._reaching()
            used = set()
            for bid in order:
                b = self.blocks[bid]
                cur = dict(IN[bid])
                for i, st in list(enumerate(b.stmts)) + [('T', b.term)]:
                    for r, _ in _reads_of(st):
                        used.update(cur.get(r, ()))
                    for r in _writes_of(st):
                        cur[r] = frozenset([(bid, i)])
            removed = False
            for bid in order:
                b = self.blocks[bid]
                keep = []
                for i, st in enumerate(b.stmts):
                    if st[0] == 'set' and st[1][0] == 'r' and st[2][0] in PURE and (bid, i) not in used \
                            and st[1][1] not in captured:
                        removed = True
                        continue
                    keep.append(st)
                b.stmts = keep
            if not removed:
                break

    # -- control-flow unflattening
    def _unflatten(self):
        # A state register is sometimes loaded with a run-time value that is only truth-tested
        # (`state = t[x]; if state then state = 3 else state = 7 end`).  Splitting on such values is
        # tried first; if a state compare then meets a boolean the assumption was wrong.
        if self._unflatten_(True) is False:
            self._unflatten_(False)

    def _unflatten_(self, split_any):
        """Remove Luraph's state-machine obfuscation.

        A "state" register only ever receives numeric constants (or is the control variable of a
        numeric for loop with constant bounds) and is only ever compared against numeric constants.
        Such registers carry no data, so the CFG is re-explored with their values known: every
        comparison folds, the dispatch branches disappear and constant-bound dispatch loops unroll.
        """
        blocks = self.blocks
        isnum, reads_of, writes_of = _isnum, _reads_of, _writes_of
        CMPS = _CMPS

        # ---- numeric for loops with constant bounds
        prep_info = {}          # (bid, idx) -> (base, init, limit, step, [idx of the constant sets])
        prep_bases = {}
        for bid, b in blocks.items():
            for i, st in enumerate(b.stmts):
                if st[0] != 'numforprep':
                    continue
                es = st[1]
                if not (len(es) == 3 and all(e[0] == 'r' and isinstance(e[1], int) for e in es)):
                    continue
                base = es[0][1]
                vals, idxs = [], []
                for e in es:
                    found = None
                    for j in range(i - 1, -1, -1):
                        sj = b.stmts[j]
                        if e[1] in writes_of(sj):
                            if sj[0] == 'set' and isnum(sj[2]):
                                found = (sj[2][1], j)
                            break
                    if found is None:
                        break
                    vals.append(found[0])
                    idxs.append(found[1])
                ok = len(vals) == 3 and vals[2] != 0 and (vals[1] - vals[0]) / vals[2] < 256
                prep_bases.setdefault(base, []).append(ok)
                if ok:
                    prep_info[(bid, i)] = (base, vals[0], vals[1], vals[2], idxs)
        for_heads = {}          # base -> [bid]
        for bid, b in blocks.items():
            if b.term[0] == 'forbr' and b.term[1] == 'num' and isinstance(b.term[2][0], int):
                for_heads.setdefault(b.term[2][0] - 3, []).append(bid)

        order, IN = self._reaching()

        # ---- webs
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        bad = set()
        used = set()
        defkind = {}
        for bid in order:
            b = blocks[bid]
            cur = dict(IN[bid])
            for i, st in list(enumerate(b.stmts)) + [('T', b.term)]:
                rs = reads_of(st)
                if i == 'T' and st[0] == 'br':
                    c = st[1]
                    while c[0] == 'un' and c[1] == 'not':
                        c = c[2]
                    if c[0] == 'r':
                        rs = [(c[1], True)]      # truth test of a state register
                for r, iscmp in rs:
                    ds = cur.get(r) or frozenset([('ENTRY', r)])
                    first = None
                    for d in ds:
                        find(d)
                        if first is None:
                            first = d
                        else:
                            union(first, d)
                    if iscmp:
                        used.add(first)
                    else:
                        bad.add(first)
                        if DEBUG_UNFLATTEN:
                            if str(r) == os.environ.get('DEVM_DEBUG_REG'):
                                print('bad use of', r, bid, i, repr(st)[:200], sorted(ds, key=repr)[:6])
                            for d in ds:
                                if d[0] != 'ENTRY' and d[1] != 'T':
                                    sd = blocks[d[0]].stmts[d[1]]
                                    if sd[0] == 'set' and isnum(sd[2]) and sd[2][1] > 2:
                                        print('bad use', r, bid, i, repr(st)[:200], d, sd)
                d = (bid, i)
                ws = writes_of(st)
                for r in ws:
                    cur[r] = frozenset([d])
                if ws:
                    find(d)
                    if st[0] == 'set' and (isnum(st[2]) or (st[2][0] == 'k' and st[2][1] in (None, True, False))):
                        defkind[d] = 'const'
                    elif st[0] == 'set' and _isboolexpr(st[2]):
                        defkind[d] = 'bool'      # state = (condition): explored once per outcome
                    elif st[0] == 'forbr' and st[1] == 'num' and isinstance(st[2][0], int) \
                            and prep_bases.get(st[2][0] - 3) and all(prep_bases[st[2][0] - 3]):
                        defkind[d] = 'for'
                    elif st[0] == 'set' and split_any and st[1][0] == 'r':
                        defkind[d] = 'split'
                    else:
                        defkind[d] = 'other'
        if split_any:
            # only webs that also receive numeric constants are state machines
            has_const = {find(d) for d, k in defkind.items() if k == 'const'}
            for d, k in defkind.items():
                if k == 'split' and find(d) not in has_const:
                    defkind[d] = 'other'
        badroots = {find(d) for d in bad}
        for d in list(parent):
            if d[0] == 'ENTRY' or defkind.get(d, 'other') == 'other':
                badroots.add(find(d))
        usedroots = {find(d) for d in used}
        if DEBUG_UNFLATTEN and os.environ.get('DEVM_DEBUG_REG'):
            rr = int(os.environ['DEVM_DEBUG_REG'])
            groups = {}
            for d in parent:
                groups.setdefault(find(d), []).append(d)
            for root, ds in groups.items():
                regs = set()
                for d in ds:
                    if d[0] == 'ENTRY':
                        regs.add(d[1])
                    else:
                        st = blocks[d[0]].term if d[1] == 'T' else blocks[d[0]].stmts[d[1]]
                        regs |= set(writes_of(st))
                if rr in regs:
                    kinds = {}
                    for d in ds:
                        kinds.setdefault('ENTRY' if d[0] == 'ENTRY' else defkind.get(d, 'other'), []).append(d)
                    print('web', len(ds), {k: len(v) for k, v in kinds.items()}, 'badread', root in {find(d) for d in bad},
                          'used', root in usedroots, 'others', kinds.get('other', [])[:4], kinds.get('ENTRY', [])[:3])
        state_defs = {d for d in parent if d[0] != 'ENTRY' and find(d) not in badroots and find(d) in usedroots}
        if not state_defs:
            return
        unroll = {base for base, heads in for_heads.items() if all((h, 'T') in state_defs for h in heads)}
        # blocks from which a loop header is reachable without re-entering through its setup
        prep_blocks = {}
        for (bid, i), info in prep_info.items():
            prep_blocks.setdefault(info[0], set()).add(bid)
        for_live = {}
        for base in unroll:
            alive = set()
            stack = list(for_heads[base])
            stops = prep_blocks.get(base, set())
            while stack:
                x = stack.pop()
                if x in alive:
                    continue
                alive.add(x)
                for pr in blocks[x].preds:
                    if pr not in stops:
                        stack.append(pr)
            for_live[base] = alive

        live_in, _ = self.liveness()

        def fold(e):
            t = e[0]
            if t == 'bin' and e[1] in CMPS and isnum(e[2]) and isnum(e[3]):
                a, b = e[2][1], e[3][1]
                op = e[1]
                return ('k', a < b if op == '<' else a <= b if op == '<=' else a > b if op == '>' else
                        a >= b if op == '>=' else a == b if op == '==' else a != b)
            if t == 'un' and e[1] == 'not' and e[2][0] == 'k':
                return ('k', e[2][1] is None or e[2][1] is False)
            if t in ('and', 'or') and e[1][0] == 'k':
                truthy = not (e[1][1] is None or e[1][1] is False)
                if t == 'and':
                    return e[2] if truthy else e[1]
                return e[1] if truthy else e[2]
            return e

        new_blocks = {}
        ids = {}
        counter = [0]
        work = []
        limit = 40 * len(blocks) + 2000

        def node(bid, env, start=0):
            keep = {}
            li = live_in.get(bid, ())
            for k, v in env.items():
                if isinstance(k, tuple):
                    if bid in for_live.get(k[1], ()):
                        keep[k] = v
                elif start or k in li:
                    keep[k] = v
            key = (bid, start, tuple(sorted(keep.items(), key=repr)))
            nid = ids.get(key)
            if nid is None:
                if not keep and not start:
                    nid = bid
                else:
                    counter[0] += 1
                    nid = (bid, 'u', counter[0])
                ids[key] = nid
                work.append((nid, bid, keep, start))
            return nid

        entry = node(self.entry, {})
        while work:
            nid, bid, env, start = work.pop()
            if len(ids) > limit:
                self.warnings.append('unflatten: state explosion, pass skipped')
                return
            b = blocks[bid]
            nb = Block(nid)
            new_blocks[nid] = nb
            env = dict(env)

            def sub(e, env=env):
                def fn(x):
                    if x[0] == 'r' and x[1] in env:
                        return ('k', env[x[1]])
                    return fold(x)
                return map_expr(e, fn)

            origin = []
            split = False
            for i, st in enumerate(b.stmts):
                if i < start:
                    continue
                d = (bid, i)
                if d in prep_info and prep_info[d][0] in unroll:
                    base, init, lim, step, idxs = prep_info[d]
                    env[('for', base)] = (init, lim, step)
                    drop = set(idxs)
                    keep = [(o, s2) for o, s2 in zip(origin, nb.stmts) if o not in drop]
                    origin = [o for o, _ in keep]
                    nb.stmts = [s2 for _, s2 in keep]
                    continue
                if d in state_defs:
                    if defkind[d] in ('bool', 'split'):
                        c = sub(st[2]) if env else st[2]
                        if c[0] != 'k':
                            envT, envF = dict(env), dict(env)
                            envT[st[1][1]] = True
                            envF[st[1][1]] = False
                            nb.term = ('br', c, node(bid, envT, i + 1), node(bid, envF, i + 1))
                            split = True
                            break
                        env[st[1][1]] = c[1]
                        continue
                    env[st[1][1]] = st[2][1]
                    continue
                st2 = map_stmt(st, lambda x: ('k', env[x[1]]) if x[0] == 'r' and x[1] in env else fold(x)) if env else st
                for r in writes_of(st):
                    env.pop(r, None)
                nb.stmts.append(st2)
                origin.append(i)
            t = b.term
            if split:
                pass
            elif t[0] == 'goto':
                nb.term = ('goto', node(t[1], env))
            elif t[0] == 'br':
                c = sub(t[1]) if env else t[1]
                if c[0] == 'k':
                    truthy = not (c[1] is None or c[1] is False)
                    nb.term = ('goto', node(t[2] if truthy else t[3], env))
                else:
                    nb.term = ('br', c, node(t[2], env), node(t[3], env))
            elif t[0] == 'forbr':
                base = t[2][0] - 3 if t[1] == 'num' and isinstance(t[2][0], int) else None
                if base is not None and ('for', base) in env and (bid, 'T') in state_defs:
                    cur, lim, step = env[('for', base)]
                    if (step > 0 and cur <= lim) or (step < 0 and cur >= lim):
                        env2 = dict(env)
                        env2[t[2][0]] = cur
                        env2[('for', base)] = (cur + step, lim, step)
                        nb.term = ('goto', node(t[3], env2))
                    else:
                        env2 = dict(env)
                        del env2[('for', base)]
                        nb.term = ('goto', node(t[4], env2))
                else:
                    for r in t[2]:
                        env.pop(r, None)
                    nb.term = ('forbr', t[1], t[2], node(t[3], env), node(t[4], env))
            elif t[0] == 'ret':
                nb.term = map_stmt(t, lambda x: ('k', env[x[1]]) if x[0] == 'r' and x[1] in env else fold(x)) if env else t
            else:
                nb.term = t
        if split_any:
            # A state compare (state <= n) that received a boolean cannot execute: it only shows up
            # on paths the split made look possible (the value had just been tested the other way).
            # Such blocks are dead ends; branches into them are resolved towards the other side.
            def boolcmp(e):
                return e[0] == 'bin' and e[1] in CMPS and e[1] not in ('==', '~=') and (
                    (e[2][0] == 'k' and isinstance(e[2][1], bool)) or (e[3][0] == 'k' and isinstance(e[3][1], bool)))
            dead = set()
            for nb in new_blocks.values():
                if nb.term[0] == 'br' and boolcmp(nb.term[1]) and all(st[0] == 'cold' for st in nb.stmts):
                    dead.add(nb.id)
                else:
                    for st in nb.stmts + [nb.term]:
                        for e in stmt_exprs(st)[0]:
                            if any(boolcmp(x) for x in walk_expr(e, [])):
                                if DEBUG_UNFLATTEN:
                                    print('unflatten: boolean reached a state compare in', nb.id, repr(st)[:200])
                                return False
            changed = True
            while changed:
                changed = False
                for nb in new_blocks.values():
                    if nb.id in dead:
                        continue
                    t = nb.term
                    if t[0] == 'br' and (t[2] in dead) != (t[3] in dead):
                        nb.term = ('goto', t[3] if t[2] in dead else t[2])
                        changed = True
                    elif (t[0] == 'br' and t[2] in dead and t[3] in dead) or (t[0] == 'goto' and t[1] in dead):
                        if any(st[0] != 'cold' for st in nb.stmts):
                            return False
                        dead.add(nb.id)
                        changed = True
            if entry in dead:
                return False
            for d in dead:
                del new_blocks[d]
        self.blocks = new_blocks
        self.entry = entry
        self._thread()

    def _opaque_numbers(self):
        """Registers and upvalues that only ever hold Luraph's opaque state numbers: set to large
        integer constants and to arithmetic over themselves, nothing else.  Returns
        (opaque(block, index, expr) -> bool, registers)."""
        blocks = self.blocks
        ups = OPAQUE_UPS.get(self.pir.key, {})
        params = set(self.param_regs or ())

        def local(b, i, e, depth=0):
            """e with registers replaced by what the block assigned to them before statement i"""
            if e[0] == 'r':
                for j in range(i - 1, -1, -1):
                    st = b.stmts[j]
                    if e[1] in _writes_of(st):
                        if st[0] == 'set' and st[1] == e and depth < 12:
                            return local(b, j, st[2], depth + 1)
                        return None
                return e
            if e[0] == 'k':
                return e if _isnum(e) else None
            if e[0] == 'up':
                return e
            if e[0] == 'bin' and e[1] in ('+', '-', '*', '%'):
                x, y = local(b, i, e[2], depth), local(b, i, e[3], depth)
                return None if x is None or y is None else ('bin', e[1], x, y)
            if e[0] == 'call' and e[1][0] == 'kfn' and str(e[1][1]).startswith('bit32.') and len(e) > 3 and e[3] is None:
                args = [local(b, i, x, depth) for x in e[2]]
                return None if any(x is None for x in args) else ('call', e[1], args, None)
            return None

        # a parameter counts when the function only does arithmetic with it
        plain = set()

        def reads(e, allowed):
            if e[0] == 'r':
                if not allowed:
                    plain.add(e[1])
            elif e[0] == 'bin' and e[1] in ('+', '-', '*', '%', '==', '~='):
                reads(e[2], allowed)
                reads(e[3], allowed)
            elif e[0] == 'un' and e[1] == 'not':
                reads(e[2], allowed)
            elif e[0] == 'call' and e[1][0] == 'kfn' and str(e[1][1]).startswith('bit32.') and len(e) > 3 and e[3] is None:
                for x in e[2]:
                    reads(x, allowed)
            else:
                sub = []
                walk_expr(e, sub)
                plain.update(x[1] for x in sub if x[0] == 'r')

        def copy_ok(b, i, m):
            """the copy made by statement i is itself only used for arithmetic and tests"""
            for st in b.stmts[i + 1:] + [b.term]:
                for e in stmt_exprs(st)[0]:
                    if e == ('r', m):
                        return False
                    sub = []
                    walk_expr(e, sub)
                    if ('r', m) in sub:
                        before = set(plain)
                        reads(e, st[0] in ('set', 'br'))
                        bad = m in plain and m not in before
                        if m not in before:
                            plain.discard(m)
                        if bad:
                            return False
                if m in _writes_of(st):
                    return True
            return True
        for b in blocks.values():
            for i, st in enumerate(b.stmts + [b.term]):
                for e in stmt_exprs(st)[0]:
                    if e[0] == 'r' and st[0] == 'set' and st[1][0] == 'r' and i < len(b.stmts):
                        if not copy_ok(b, i, st[1][1]):
                            plain.add(e[1])
                    else:
                        reads(e, st[0] in ('set', 'br'))
        order, IN = self._reaching()
        memo = {}

        def def_ok(d):
            if d[0] == 'ENTRY':
                return d[1] in params and d[1] not in plain
            if d[1] == 'T':
                return False
            if d in memo:
                return memo[d]
            memo[d] = True
            st = blocks[d[0]].stmts[d[1]]
            res = st[0] == 'set' and ok(local(blocks[d[0]], d[1], st[2]), d[0])
            memo[d] = res
            return res

        def ok(e, bid):
            if e is None:
                return False
            if e[0] == 'k':
                return True
            if e[0] == 'r':
                ds = IN.get(bid, {}).get(e[1]) or [('ENTRY', e[1])]
                return all(def_ok(d) for d in ds)
            if e[0] == 'up':
                return e[1] in ups
            if e[0] == 'bin':
                return ok(e[2], bid) and ok(e[3], bid)
            if e[0] == 'call':
                return all(ok(x, bid) for x in e[2])
            return False

        def opaque(b, i, e):
            e = local(b, i, e)
            return e is not None and e[0] != 'k' and ok(e, b.id)

        def value(b, i, e):
            """the number e holds before statement i, when the block alone determines it"""
            e = local(b, i, e)
            return None if e is None else _opaque_eval(e, ups)

        def scrambles(b):
            """the block updates an opaque number: what the losing side of a guard starts with"""
            for i, st in enumerate(b.stmts):
                if st[0] != 'set':
                    continue
                if st[1][0] == 'up' and st[1][1] in ups:
                    return True
                if st[1][0] == 'r':
                    e = local(b, i, st[2])
                    if e is not None and e[0] != 'k' and ok(e, b.id):
                        sub = []
                        walk_expr(e, sub)
                        if st[1] in sub:
                            return True
            return False

        # what the functions created here capture
        for b in blocks.values():
            for i, st in enumerate(b.stmts):
                if st[0] == 'set' and isinstance(st[2], tuple) and st[2][0] == 'closure':
                    got = {}
                    fns = set()
                    for n, c in enumerate(st[2][2]):
                        if c[0] == 'val':
                            # a function captured by value stays a function
                            m = c[1][1] if isinstance(c[1], tuple) else c[1]
                            for j in range(i - 1, -1, -1):
                                if m in _writes_of(b.stmts[j]):
                                    sj = b.stmts[j]
                                    if sj[0] == 'set' and isinstance(sj[2], tuple) and sj[2][0] == 'closure':
                                        fns.add(n)
                                    break
                            else:
                                ds = IN.get(b.id, {}).get(m) or ()
                                if ds and all(d[0] != 'ENTRY' and d[1] != 'T' and blocks[d[0]].stmts[d[1]][0] == 'set'
                                              and isinstance(blocks[d[0]].stmts[d[1]][2], tuple)
                                              and blocks[d[0]].stmts[d[1]][2][0] == 'closure' for d in ds):
                                    fns.add(n)
                        elif c[0] not in ('ref', 'const') and c[1] in FUNCTION_UPS.get(self.pir.key, ()):
                            fns.add(n)
                    FUNCTION_UPS[st[2][1]] = fns
                    for n, c in enumerate(st[2][2]):
                        if c[0] in ('val', 'ref'):
                            m = c[1][1] if isinstance(c[1], tuple) else c[1]
                            v = local(b, i, ('r', m))
                            if v is None or not ok(v, b.id):
                                continue
                            # a shared variable is updated right after the function is made
                            got[n] = value(b, i if c[0] == 'val' else len(b.stmts), ('r', m))
                        elif c[0] != 'const' and c[1] in ups:
                            got[n] = ups[c[1]]
                    OPAQUE_UPS[st[2][1]] = got
        return opaque, value, scrambles

    def _cut_cold_magic(self):
        """Key checks (`x == 149143557`: one of Luraph's opaque state numbers compared against a
        large magic number): one side is the tamper response, and the check itself is not part of
        the program.  The losing side is the one the traced run never took; in code that never ran
        it is the side that scrambles the number, or the one the number's value rules out.  The
        check is removed before the state machine is unflattened, because the losing side usually
        corrupts the state register on purpose.  A test of anything else
        (`game.PlaceId == 7465136166`) is the program's own."""
        blocks = self.blocks
        self.compute_preds()
        opaque, value, scrambles = self._opaque_numbers()
        cut = 0
        for b in list(blocks.values()):
            if b.term[0] != 'br' or b.term[2] == b.term[3]:
                continue
            e = b.term[1]
            flip = False
            pos = len(b.stmts)
            while True:
                while e[0] == 'un' and e[1] == 'not':
                    e = e[2]
                    flip = not flip
                if e[0] != 'r':
                    break
                for j in range(pos - 1, -1, -1):
                    st = b.stmts[j]
                    if e[1] in _writes_of(st):
                        e = st[2] if st[0] == 'set' and st[1] == e else ('k', None)
                        pos = j
                        break
                else:
                    break
            if e[0] == 'upcell' and e[1] in FUNCTION_UPS.get(self.pir.key, ()):
                # `if f then` on a function captured by value: an opaque predicate, always true
                keep = b.term[3] if flip else b.term[2]
                if not (blocks[keep].stmts and blocks[keep].stmts[0][0] == 'cold'):
                    b.term = ('goto', keep)
                    cut += 1
                continue
            if not (e[0] == 'bin' and e[1] in ('==', '~=')):
                continue
            consts = [x[1] for x in (e[2], e[3]) if _isnum(x)]
            if len(consts) != 1 or abs(consts[0]) < 65536 or consts[0] != round(consts[0]):
                continue
            other = e[3] if _isnum(e[2]) else e[2]
            n = len(b.stmts)
            if not opaque(b, n, other):
                continue
            arms = (b.term[2], b.term[3])
            coldside = [sid for sid in arms if blocks[sid].stmts and blocks[sid].stmts[0][0] == 'cold'
                        and len(blocks[sid].preds) == 1]
            v = value(b, n, other)
            known = None if v is None else arms[0 if ((v == consts[0]) == (e[1] == '==')) != flip else 1]
            if len(coldside) == 1:
                lose = coldside[0]
            else:
                scr = [sid for sid in arms if len(blocks[sid].preds) == 1 and scrambles(blocks[sid])]
                if len(scr) == 1 and (known is None or known != scr[0]):
                    lose = scr[0]
                elif not scr and known is not None:
                    lose = arms[1] if known == arms[0] else arms[0]
                else:
                    continue
            b.term = ('goto', arms[1] if lose == arms[0] else arms[0])
            cut += 1
        if cut:
            self._prune()
            self.compute_preds()
        return cut

    def _cut_cold_edges(self):
        """Opaque predicates.  Luraph guards its code with tests of the form `key == 1234` whose
        failing side jumps to some unrelated block (so a wrong key derails the program).  Those
        jumps turn straight-line code into huge bogus loops.  An exit that the traced run never
        took (marked ('cold',) by the lifter) is cut when the test compares a register with a
        numeric constant and the target is not simply the join point after the test."""
        blocks = self.blocks
        # tests that have one outcome whatever the program does: a key register (it held a single
        # number throughout the traced run) against a constant, or any register against a constant
        # that is a hair off an integer (225.00000000000492: never equal, by construction)
        keyregs = getattr(self.pir, 'keyregs', None) or {}
        folded = 0
        for b in blocks.values():
            if b.term[0] != 'br':
                continue
            e = b.term[1]
            flip = False
            while e[0] == 'un' and e[1] == 'not':
                e = e[2]
                flip = not flip
            if not (e[0] == 'bin' and e[1] in ('==', '~=')):
                continue
            if e[2][0] == 'r' and _isnum(e[3]):
                reg, c = e[2][1], e[3][1]
            elif e[3][0] == 'r' and _isnum(e[2]):
                reg, c = e[3][1], e[2][1]
            else:
                continue
            if reg in keyregs:
                equal = keyregs[reg] == c
            elif c != round(c) and abs(c - round(c)) < 1e-6:
                equal = False
            else:
                continue
            truth = (equal if e[1] == '==' else not equal) != flip
            b.term = ('goto', b.term[2] if truth else b.term[3])
            folded += 1
        if folded:
            self._prune()
        cold = [b for b in blocks.values() if any(st[0] == 'cold' for st in b.stmts)]
        if not cold:
            if folded:
                self._thread()
            return folded
        self.compute_preds()
        # immediate post-dominators (Cooper-Harvey-Kennedy on the reversed graph)
        EXIT = ('EXIT',)
        rsucc = {bid: list(b.preds) for bid, b in blocks.items()}
        rsucc[EXIT] = [bid for bid, b in blocks.items() if not b.succs()]
        order, seen, stack = [], {EXIT}, [(EXIT, iter(rsucc[EXIT]))]
        while stack:
            n, it = stack[-1]
            for x in it:
                if x not in seen:
                    seen.add(x)
                    stack.append((x, iter(rsucc[x])))
                    break
            else:
                order.append(n)
                stack.pop()
        order.reverse()
        idx = {n: i for i, n in enumerate(order)}
        ipdom = {EXIT: EXIT}
        changed = True
        while changed:
            changed = False
            for n in order[1:]:
                new = None
                for p in (blocks[n].succs() or [EXIT]):
                    if p in ipdom:
                        if new is None:
                            new = p
                        else:
                            a, b2 = p, new
                            while a != b2:
                                while idx[a] > idx[b2]:
                                    a = ipdom[a]
                                while idx[b2] > idx[a]:
                                    b2 = ipdom[b2]
                            new = a
                if new is not None and ipdom.get(n) != new:
                    ipdom[n] = new
                    changed = True

        def postdominates(t, b):
            x = ipdom.get(b)
            while x is not None and x != EXIT:
                if x == t:
                    return True
                x = ipdom.get(x)
            return False

        for c in cold:
            only = len(c.stmts) == 1 and c.term[0] == 'goto' and len(c.preds) == 1
            c.stmts = [st for st in c.stmts if st[0] != 'cold']
            if not only:
                continue
            pred = blocks[next(iter(c.preds))]
            if pred.term[0] != 'br':
                continue
            e = pred.term[1]
            while e[0] == 'un' and e[1] == 'not':
                e = e[2]
            if not (e[0] == 'bin' and e[1] in ('==', '~=') and (
                    (e[2][0] == 'r' and _isnum(e[3])) or (e[3][0] == 'r' and _isnum(e[2])))):
                continue
            target = c.term[1]
            if postdominates(target, pred.id):
                continue
            c.stmts = [('trap', target)]
            c.term = ('ret', [], None)
            folded += 1
        self._thread()
        return folded + len(cold)

    def _collapse_bool_diamonds(self):
        """Turn a web of tiny blocks that only shuffle booleans between registers back into one
        expression.

        `c = (a and g) or (not a and l)` (the continuation test of a numeric for loop, which
        Luraph spells out when it flattens the loop) is compiled into a handful of blocks that copy
        truth values around and branch on them, with shared tails.  Starting at a branch, every
        path through such blocks is followed symbolically; if all of them arrive at the same block,
        the branch is replaced by plain assignments of the values the registers end up with."""
        blocks = self.blocks

        def small(b):
            if len(b.stmts) > 3 or b.term[0] not in ('goto', 'br'):
                return False
            for st in b.stmts:
                if st[0] != 'set' or st[1][0] != 'r':
                    return False
                e = st[2]
                if not (e[0] == 'r' or (e[0] == 'k' and (e[1] is None or isinstance(e[1], bool)))):
                    return False
            if b.term[0] == 'br':
                c = b.term[1]
                while c[0] == 'un' and c[1] == 'not':
                    c = c[2]
                if c[0] != 'r':
                    return False
            return True

        def boolish(reg, head):
            for st in reversed(head.stmts):
                if reg in _writes_of(st):
                    return st[0] == 'set' and st[1] == ('r', reg) and _isboolexpr(st[2])
            return False

        done = 0
        for hid in list(blocks):
            head = blocks.get(hid)
            if head is None or head.term[0] != 'br':
                continue
            cond = head.term[1]
            if cond[0] != 'r':
                continue
            # negations known at the end of the head: R6 = not R4
            neg = {}
            for st in head.stmts:
                for w in _writes_of(st):
                    neg.pop(w, None)
                    for k in [k for k, v in neg.items() if v == w]:
                        del neg[k]
                if st[0] == 'set' and st[1][0] == 'r' and st[2][0] == 'un' and st[2][1] == 'not' and st[2][2][0] == 'r' \
                        and st[2][2][1] != st[1][1]:
                    neg[st[1][1]] = st[2][2][1]

            def learn(facts, reg, val):
                facts = dict(facts)
                facts[reg] = val
                for k, v in neg.items():
                    if v == reg:
                        facts[k] = not val
                    if k == reg:
                        facts[v] = not val
                return facts

            results = []        # (facts, env, exit block)
            ok = True
            work = [(head.term[2], {}, learn({}, cond[1], True), 0), (head.term[3], {}, learn({}, cond[1], False), 0)]
            while work and ok:
                bid, env, facts, depth = work.pop()
                b = blocks[bid]
                if bid == hid or depth > 10 or len(results) + len(work) > 12:
                    ok = False
                    break
                if not small(b):
                    results.append((facts, env, bid))
                    continue
                env = dict(env)
                for st in b.stmts:
                    e = st[2]
                    env[st[1][1]] = env.get(e[1], e) if e[0] == 'r' else e
                if b.term[0] == 'goto':
                    work.append((b.term[1], env, facts, depth + 1))
                    continue
                c = b.term[1]
                flip = False
                while c[0] == 'un':
                    c = c[2]
                    flip = not flip
                v = env.get(c[1], c)
                if v[0] == 'k':
                    truth = not (v[1] is None or v[1] is False)
                elif v[1] in facts:
                    truth = facts[v[1]]
                else:
                    for outcome in (True, False):
                        work.append((b.term[2] if outcome != flip else b.term[3], env, learn(facts, v[1], outcome), depth + 1))
                    continue
                work.append((b.term[2] if truth != flip else b.term[3], env, facts, depth + 1))
            if not ok or len(results) < 2 or len({r[2] for r in results}) != 1:
                continue
            exit_ = results[0][2]
            if exit_ in (head.term[2], head.term[3]) and all(not r[1] for r in results):
                continue
            regs = set()
            for _, env, _ in results:
                regs.update(env)
            a = cond[1]
            sets = {}
            for x in sorted(regs, key=repr):
                side = {}
                for want in (True, False):
                    paths = [(f, env.get(x, ('r', x))) for f, env, _ in results if f[a] == want]
                    vals = {v for _, v in paths}
                    if len(vals) == 1:
                        v = next(iter(vals))
                        if v[0] == 'r' and v[1] in paths[0][0] and all(v[1] in f for f, _ in paths) \
                                and len({f[v[1]] for f, _ in paths}) == 1 and boolish(v[1], head):
                            v = ('k', paths[0][0][v[1]])
                        side[want] = v
                        continue
                    # the value follows the truth of one register the paths forked on
                    cons = []
                    for f, v in paths:
                        if v[0] == 'r' and v[1] in f and boolish(v[1], head):
                            v = ('k', f[v[1]])
                        cons.append((f, v))
                    found = None
                    if all(v[0] == 'k' and isinstance(v[1], bool) for _, v in cons):
                        keys = set.intersection(*[set(f) for f, _ in cons]) - {a}
                        for z in sorted(keys, key=repr):
                            if not boolish(z, head):
                                continue
                            if all(f[z] == v[1] for f, v in cons):
                                found = ('r', z)
                            elif all(f[z] != v[1] for f, v in cons):
                                found = ('un', 'not', ('r', z))
                            if found:
                                break
                    if found is None:
                        side = None
                        break
                    side[want] = found
                if side is None:
                    sets = None
                    break
                vt, vf = side[True], side[False]
                if vt == vf:
                    e = vt
                elif vt == ('k', True) and vf == ('k', False):
                    e = ('r', a) if boolish(a, head) else None
                elif vt == ('k', False) and vf == ('k', True):
                    e = ('un', 'not', ('r', a))
                else:
                    def isbool(v):
                        return (v[0] == 'k' and isinstance(v[1], bool)) or (v[0] == 'r' and boolish(v[1], head)) or v[0] == 'un'
                    e = None
                    if isbool(vt):
                        if vt == ('k', False):
                            e = ('and', ('un', 'not', ('r', a)), vf)
                        elif vf == ('k', False) and isbool(vt):
                            e = ('and', ('r', a), vt)
                        else:
                            e = ('or', ('and', ('r', a), vt), ('and', ('un', 'not', ('r', a)), vf))
                if e is None:
                    sets = None
                    break
                if e != ('r', x):
                    sets[x] = e
            if sets is None:
                continue
            # order: an assignment must run before the registers it reads are overwritten
            pending = dict(sets)
            ordered = []
            while pending:
                progress = False
                for x in sorted(pending, key=repr):
                    others = [y for y in pending if y != x]
                    if not any(x in [r for r, _ in _reads_of(('set', ('r', y), pending[y]))] for y in others):
                        ordered.append(('set', ('r', x), pending.pop(x)))
                        progress = True
                        break
                if not progress:
                    break
            if pending:
                continue
            head.stmts.extend(ordered)
            head.term = ('goto', exit_)
            done += 1
        if done:
            self._prune()
            self.compute_preds()
        return done

    def _recover_for_loops(self):
        """Numeric for loops that Luraph rewrote to keep their state in a table.

            S = { ..., step, limit, start - step, ... }      -- pushed before the loop (one slot links
            while true do                                    --  to the state of the enclosing loop)
                local i = S[ki] + S[ks]
                S[ki] = i
                if not ((S[ks] <= 0 and i >= S[kl]) or (S[ks] > 0 and i <= S[kl])) then break end
                ...
            end
            S = S[link]                                      -- popped afterwards

        becomes `for i = start, limit, step do ... end`."""
        blocks = self.blocks
        self.compute_preds()
        done = 0
        for hid in sorted(blocks, key=repr):
            head = blocks.get(hid)
            if head is None or head.term[0] != 'br' or len(head.preds) < 2:
                continue
            env = {}

            def sub(e):
                return map_expr(e, lambda x: env.get(x[1], x) if x[0] == 'r' else x)

            store = None
            ok = True
            for st in head.stmts:
                if st[0] == 'set' and st[1][0] == 'r':
                    env[st[1][1]] = sub(st[2])
                elif st[0] == 'setindex' and store is None:
                    store = (sub(st[1]), sub(st[2]), sub(st[3]))
                else:
                    ok = False
                    break
            if not ok or store is None:
                continue
            cond = sub(head.term[1])
            body, exit_ = head.term[2], head.term[3]
            while cond[0] == 'un' and cond[1] == 'not':
                cond = cond[2]
                body, exit_ = exit_, body
            S, key, ival = store
            if S[0] != 'r' or key[0] != 'k' or ival[0] != 'bin' or ival[1] != '+':
                continue
            step = ival[3]
            if ival[2] != ('index', S, key) or step[0] != 'index' or step[1] != S or step[2][0] != 'k':
                continue
            if not (cond[0] == 'or' and cond[1][0] == 'and' and cond[2][0] == 'and'):
                continue
            A, G = cond[1][1], cond[1][2]
            nA, L = cond[2][1], cond[2][2]
            if A != ('bin', '<=', step, ('k', 0)) or nA != ('un', 'not', A):
                continue
            if not (G[0] == 'bin' and G[1] == '>=' and G[2] == ival and L == ('bin', '<=', ival, G[3])):
                continue
            lim = G[3]
            if lim[0] != 'index' or lim[1] != S or lim[2][0] != 'k':
                continue
            s = S[1]
            ki, ks, kl = key[1], step[2][1], lim[2][1]
            ivars = [r for r, v in env.items() if v == ival]
            if not ivars or body == hid or exit_ == hid:
                continue
            # loop body: everything that reaches the head again
            inside = set()
            stack = [body]
            while stack:
                x = stack.pop()
                if x in inside or x == hid:
                    continue
                inside.add(x)
                stack.extend(blocks[x].succs())
            latches = {p for p in head.preds if p in inside}
            entries = [p for p in head.preds if p not in inside]
            if not latches or not entries or hid in entries:
                continue
            # registers the head computes besides the loop variable must be dead afterwards
            live_in, _ = self.liveness()
            others = set(env) - set(ivars)
            if others & (set(live_in.get(body, ())) | set(live_in.get(exit_, ()))):
                continue
            # the state table: built at the end of every entry block
            pushes = {}
            for p in entries:
                pb = blocks[p]
                idx = None
                for i in range(len(pb.stmts) - 1, -1, -1):
                    if s in _writes_of(pb.stmts[i]):
                        idx = i
                        break
                if idx is None:
                    break
                st = pb.stmts[idx]
                if not (st[0] == 'set' and st[2][0] == 'table' and not st[2][2]):
                    break
                items = {}
                n = 0
                for k, v in st[2][1]:
                    if k is None:
                        n += 1
                        items[n] = v
                    elif k[0] == 'k':
                        items[k[1]] = v
                need = [items.get(ki, ('k', None)), items.get(kl, ('k', None)), items.get(ks, ('k', None))]
                reads = set()
                for e in need:
                    reads |= expr_reads(e)[0]
                    if expr_reads(e)[2]:
                        reads = None
                        break
                if reads is None:
                    break
                later = pb.stmts[idx + 1:]
                if any(set(_writes_of(x)) & reads for x in later) or pb.term[0] not in ('goto', 'br'):
                    break
                if any(expr_reads(e)[1] for e in need) and any(stmt_effects(x)[1] for x in later):
                    break
                pushes[p] = (idx, items, need)
            if len(pushes) != len(entries):
                continue
            # every other use of the table must read a slot of it (the pop `S = S[link]`, or the next
            # push taking over the link) or be the push of a nested loop, which stores S as its link
            order, IN = self._reaching()
            mine = {(p, idx) for p, (idx, _, _) in pushes.items()}
            first = next(iter(pushes.values()))[1]
            S_ = ('r', s)

            def slot_reads(st):
                """slots read as S[k] by a statement, or None if S is used in any other way."""
                slots = []
                ok_ = True
                for e in stmt_exprs(st)[0]:
                    n_direct = sum(1 for x in walk_expr(e, []) if x == S_)
                    n_slot = sum(1 for x in walk_expr(e, []) if x[0] == 'index' and x[1] == S_ and x[2][0] == 'k')
                    if n_direct != n_slot:
                        ok_ = False
                    for x in walk_expr(e, []):
                        if x[0] == 'index' and x[1] == S_ and x[2][0] == 'k':
                            slots.append(x[2][1])
                return slots if ok_ else None

            pops = []
            bad = False
            for bid in order:
                b = blocks[bid]
                cur = IN[bid].get(s, frozenset())
                for i, st in list(enumerate(b.stmts)) + [('T', b.term)]:
                    reads = [r for r, _ in _reads_of(st)]
                    if s in reads and bid != hid and cur & mine:
                        slots = slot_reads(st)
                        if not cur <= mine:
                            bad = True
                        elif slots is not None and st[0] != 'setindex':
                            pops.append((bid, i, slots))
                        elif st[0] == 'set' and st[1] == S_ and st[2][0] == 'table' \
                                and sum(1 for x in walk_expr(st[2], []) if x == S_) == 1 \
                                and any(v == S_ for _, v in st[2][1]):
                            pass
                        else:
                            bad = True
                    if s in _writes_of(st):
                        cur = frozenset([(bid, i)])
                if bad:
                    break
            if bad:
                continue
            # the slots read afterwards must hold the same simple value whichever entry was taken
            link = {}
            for _, _, slots in pops:
                for k in slots:
                    vals = {repr(items.get(k, ('k', None))) for _, items, _ in pushes.values()}
                    v = first.get(k, ('k', None))
                    if len(vals) != 1 or not (v[0] == 'k' or v == S_ or (v[0] == 'index' and v[1] == S_ and v[2][0] == 'k')):
                        bad = True
                    link[k] = v
            if bad:
                continue
            # ---- rewrite
            for bid, i, slots in pops:
                def fix(x):
                    if x[0] == 'index' and x[1] == S_ and x[2][0] == 'k':
                        return link[x[2][1]]
                    return x
                if i == 'T':
                    blocks[bid].term = map_stmt(blocks[bid].term, fix)
                else:
                    blocks[bid].stmts[i] = map_stmt(blocks[bid].stmts[i], fix)
            ivar = ivars[0]
            for n, (p, (idx, items, need)) in enumerate(sorted(pushes.items(), key=repr)):
                pb = blocks[p]
                del pb.stmts[idx]
                start, limit, step0 = need
                if _isnum(start) and _isnum(step0):
                    init = ('k', start[1] + step0[1])
                elif start == ('k', 0):
                    init = step0
                else:
                    init = ('bin', '+', start, step0)
                if limit[0] == 'bin' and limit[1] == '+' and limit[3] == ('k', 0):
                    limit = limit[2]
                pre = Block((hid, 'for', n))
                pre.stmts = [('numforprep', [init, limit, step0])]
                pre.term = ('goto', hid)
                blocks[pre.id] = pre
                pb.retarget(hid, pre.id)
                if self.entry == hid:
                    self.entry = pre.id
            head.stmts = []
            head.term = ('forbr', 'num', (ivar,), body, exit_)
            if len(ivars) > 1:
                nb = Block((hid, 'forvars'))
                nb.stmts = [('set', ('r', r), ('r', ivar)) for r in ivars[1:]]
                nb.term = ('goto', body)
                blocks[nb.id] = nb
                head.term = ('forbr', 'num', (ivar,), nb.id, exit_)
            self.compute_preds()
            done += 1
        return done

    def _open_upcells(self):
        """cell[1][cell[3]] (an upvalue cell opened by generic table instructions) -> the upvalue."""
        box, slot = CELLS

        def is_part(e, k):
            return e[0] == 'index' and e[1][0] == 'upcell' and e[2] == ('k', k)

        def fix(e):
            if e[0] == 'index' and is_part(e[1], box) and is_part(e[2], slot) and e[1][1] == e[2][1]:
                return ('up', e[1][1][1])
            return e

        def fix_stmt(st):
            st = map_stmt(st, fix)
            if st[0] == 'setindex' and is_part(st[1], box) and is_part(st[2], slot) and st[1][1] == st[2][1]:
                return ('set', ('up', st[1][1][1]), st[3])
            return st

        for b in self.blocks.values():
            b.stmts = [fix_stmt(st) for st in b.stmts]
            b.term = map_stmt(b.term, fix)

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
        # the loop instruction of a for-loop can exist twice (one copy tests on entry, one closes the
        # body): identical empty copies are one block
        same = {}
        for b in sorted(blocks.values(), key=lambda b: str(b.id)):
            if b.term[0] == 'forbr' and not b.stmts:
                first = same.setdefault(repr(b.term), b.id)
                if first != b.id:
                    fwd[b.id] = first
        if any(blocks[k].term[0] == 'forbr' for k in fwd):
            for b in blocks.values():
                for s in list(b.succs()):
                    if s in fwd and blocks[s].term[0] == 'forbr':
                        b.retarget(s, fwd[s])
            if self.entry in fwd and blocks[self.entry].term[0] == 'forbr':
                self.entry = fwd[self.entry]
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

    def _unthread_tests(self):
        """`if c then goto T end; work; if c then goto T else goto F end` is compiled (or obfuscated)
        with the second test folded into the first: `if c then goto T else work; goto F end`, next
        to another copy of the test for the paths that skip the first one.  The arm goes back
        through that copy, so T and F each have one way in."""
        blocks = self.blocks
        self.compute_preds()
        changed = False
        for y in list(blocks.values()):
            if y.stmts or y.term[0] != 'br' or len(y.term) != 4 or y.term[2] == y.term[3]:
                continue
            c = y.term[1]
            cr, mem, impure = expr_reads(c)
            if impure or mem:
                continue
            for x in list(blocks.values()):
                if x is y or x.term[0] != 'br' or len(x.term) != 4 or x.term[1] != c or x.term[2] == x.term[3]:
                    continue
                for same, other in ((2, 3), (3, 2)):
                    if x.term[same] != y.term[same] or x.term[other] == y.term[other]:
                        continue
                    arm = blocks.get(x.term[other])
                    if arm is None or arm is y or arm is x or arm.preds != {x.id} or arm.term != ('goto', y.term[other]):
                        continue
                    if any(set(_writes_of(st)) & cr for st in arm.stmts) or (cr & self.captured_ref and any(
                            expr_reads(e)[2] for st in arm.stmts for e in stmt_exprs(st)[0])):
                        continue
                    t = list(x.term)
                    t[same] = y.id
                    x.term = tuple(t)
                    arm.term = ('goto', y.id)
                    changed = True
                    break
        if changed:
            self.compute_preds()
        return changed

    def _thread_same_test(self):
        """`if r then A else goto B end` where B is nothing but `if r then C else D end`: on that
        edge r is known, so it goes straight to D (to C from the true arm).  Value-producing
        and/or chains (`x = a and b and c; if x then`) re-test the register at their join."""
        if os.environ.get('DEVM_NO_SAMETEST'):
            return False
        blocks = self.blocks

        def test(c):
            neg = False
            while c[0] == 'un' and c[1] == 'not':
                c = c[2]
                neg = not neg
            return (c, neg) if c[0] == 'r' else (None, False)

        def known_edge(q, target, reg):
            if q.term[0] != 'br' or q.term[2] == q.term[3]:
                return False
            r, _ = test(q.term[1])
            return r == reg and target in (q.term[2], q.term[3])

        changed = False
        self.compute_preds()
        for _ in range(8):
            again = False
            for b in list(blocks.values()):
                if b.id not in blocks or b.term[0] != 'br':
                    continue
                reg, neg = test(b.term[1])
                if reg is None:
                    continue
                arms = [b.term[2], b.term[3]]
                for i in (0, 1):
                    truth = (i == 0) != neg          # value of the register on this edge
                    seen = set()
                    while arms[i] in blocks and arms[i] not in seen:
                        seen.add(arms[i])
                        t = blocks[arms[i]]
                        if t.stmts or t.term[0] != 'br' or t.id == b.id:
                            break
                        reg2, neg2 = test(t.term[1])
                        if reg2 != reg:
                            break
                        # only when the join then disappears: if something else still reaches it
                        # with the value unknown (`x = a and b or c`), the join is the shape the
                        # and/or recovery looks for and stays as it is
                        if not all(known_edge(blocks[q], t.id, reg) for q in t.preds if q in blocks):
                            break
                        arms[i] = t.term[2] if truth != neg2 else t.term[3]
                if arms != [b.term[2], b.term[3]]:
                    b.term = ('br', b.term[1], arms[0], arms[1]) + tuple(b.term[4:])
                    again = changed = True
                # the same when the arm does some work first but is only reached from here and
                # leaves the register alone: its own test of the register is already decided
                if arms[0] != arms[1] and not os.environ.get('DEVM_NO_ARMTEST'):
                    for i in (0, 1):
                        truth = (i == 0) != neg
                        t = blocks.get(arms[i])
                        if t is None or t.id == b.id or t.term[0] != 'br' or t.preds != {b.id}:
                            continue
                        reg2, neg2 = test(t.term[1])
                        if reg2 is None or reg[1] in self.captured_ref or reg2[1] in self.captured_ref:
                            continue
                        # which registers hold the tested value at the end of the arm
                        same = {reg[1]}
                        bad = False
                        for st in t.stmts:
                            if st[0] in ('setmulti', 'multiset'):
                                bad = True
                                break
                            ws = _writes_of(st)
                            if st[0] == 'set' and st[1][0] == 'r' and isinstance(st[2], tuple) and st[2][0] == 'r' \
                                    and st[2][1] in same:
                                same.add(st[1][1])
                            else:
                                same.difference_update(ws)
                        if bad or reg2[1] not in same:
                            continue
                        t.term = ('goto', t.term[2] if truth != neg2 else t.term[3])
                        again = changed = True
            if again:
                self._prune()
                self.compute_preds()
            if not again:
                break
        # tail merging: a block ending in the very test an empty block consists of goes there
        # instead (`if a then x = f() end; if p then` lifted with the second test in both arms)
        empties = {}
        for t in blocks.values():
            if os.environ.get('DEVM_NO_TAILMERGE'):
                break
            if not t.stmts and t.term[0] == 'br' and len(t.term) == 4:
                empties.setdefault(repr(t.term[1:]), t.id)
        for b in blocks.values():
            if b.term[0] == 'br' and len(b.term) == 4 and b.stmts:
                e = empties.get(repr(b.term[1:]))
                if e is not None and e != b.id:
                    b.term = ('goto', e)
                    changed = True
        if changed:
            self._prune()
            self.compute_preds()
        return changed

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
                    new.append((t, [('r', a), ('r', a + 1), ('r', a + 2)]))
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
        if self.param_regs is None:
            self.nparams = n
            b.stmts = b.stmts[n:]
            self.param_regs = list(range(1, n + 1))
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
                        if any(x[0] == 'closure' and any(c[0] in ('ref', 'val') and c[1] == n for c in x[2])
                               for e in reads for x in walk_expr(e, [])):
                            # captured by a closure: the register itself must keep its value
                            break
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
                        if any(n in expr_reads(e)[0] for e in reads):
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
