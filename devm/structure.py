import os
"""CFG -> structured Lua AST -> source text."""
import math
import re

from . import beautify
from . import ircfg as ircfg_mod
from .ircfg import prepass as ircfg_prepass
from .ircfg import closes, Block, ProtoCFG, walk_expr, map_expr, map_stmt, stmt_exprs, negate, expr_reads


DUP_LIMIT = 40
DEPTH_LIMIT = 400


class Unstructured(Exception):
    pass


# ------------------------------------------------------------------ variables (webs)
class Namer:
    def __init__(self):
        self.n = 0
        self.p = 0

    def var(self):
        self.n += 1
        return 'v%d' % self.n

    def param(self):
        self.p += 1
        return 'p%d' % self.p


def assign_variables(cfg, namer, coalesce=False):
    """Split registers into webs and rename ('r', n) -> ('r', 'name')."""
    blocks = cfg.blocks
    order = rpo(cfg)
    # definition sites
    def_ids = {}
    parent = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def newdef(key):
        parent[key] = key
        return key

    for i in range(1, cfg.nparams + 1):
        newdef(('param', i))
    # gen/kill per block
    gen = {}
    kills = {}
    capgen = {}
    for bid in order:
        b = blocks[bid]
        g = {}
        for si, st in enumerate(b.stmts + [b.term]):
            for w in stmt_exprs(st)[1]:
                if w[0] == 'r':
                    d = newdef((bid, si, w[1]))
                    g[w[1]] = d
        gen[bid] = g
    # reaching definitions: IN[b][reg] = set of defs
    IN = {bid: {} for bid in order}
    entry_in = {cfg.param_regs[i - 1]: {('param', i)} for i in range(1, cfg.nparams + 1)}
    IN[cfg.entry] = entry_in
    OUT = {}
    changed = True
    while changed:
        changed = False
        for bid in order:
            b = blocks[bid]
            if bid == cfg.entry:
                inn = {k: set(v) for k, v in entry_in.items()}
            else:
                inn = {}
            for p in b.preds:
                po = OUT.get(p)
                if po:
                    for r, ds in po.items():
                        s = inn.get(r)
                        if s is None:
                            inn[r] = set(ds)
                        else:
                            s |= ds
            out = dict(inn)
            for r, d in gen[bid].items():
                out[r] = {d}
            IN[bid] = inn
            if OUT.get(bid) != out:
                OUT[bid] = out
                changed = True
    # captured-open analysis
    cap = cfg.captured_ref
    CIN = {bid: set() for bid in order}
    COUT = {}
    if cap:
        changed = True
        while changed:
            changed = False
            for bid in order:
                b = blocks[bid]
                cur = set()
                for p in b.preds:
                    cur |= COUT.get(p, set())
                CIN[bid] = set(cur)
                for st in b.stmts + [b.term]:
                    if st[0] == 'close':
                        cur = {r for r in cur if not closes(st, r)}
                    for e in stmt_exprs(st)[0]:
                        for x in walk_expr(e, []):
                            if x[0] == 'closure':
                                for c in x[2]:
                                    if c[0] == 'ref':
                                        cur.add(c[1])
                if COUT.get(bid) != cur:
                    COUT[bid] = cur
                    changed = True
    # pass 1: unions
    undef_webs = {}

    def use(cur, r, selfdef=None):
        ds = cur.get(r)
        if not ds:
            key = ('undef', r)
            if key not in parent:
                newdef(key)
            cur[r] = {key}
            return
        ds = list(ds)
        for d in ds[1:]:
            union(ds[0], d)

    for bid in order:
        b = blocks[bid]
        cur = {r: set(ds) for r, ds in IN[bid].items()}
        copen = set(CIN[bid])
        for si, st in enumerate(b.stmts + [b.term]):
            reads, writes = stmt_exprs(st)
            selfcap = set()
            if st[0] == 'set' and st[2][0] == 'closure' and st[1][0] == 'r':
                for c in st[2][2]:
                    if c[0] == 'ref' and c[1] == st[1][1]:
                        selfcap.add(c[1])
            if st[0] == 'close':
                copen = {r for r in copen if not closes(st, r)}
            for e in reads:
                for x in walk_expr(e, []):
                    if x[0] == 'r':
                        use(cur, x[1])
                    elif x[0] == 'closure':
                        for c in x[2]:
                            if c[0] in ('ref', 'val') and c[1] not in selfcap:
                                use(cur, c[1])
                            if c[0] == 'ref':
                                copen.add(c[1])
            for w in writes:
                if w[0] == 'r':
                    r = w[1]
                    d = (bid, si, r)
                    if r in copen and r not in selfcap and cur.get(r):
                        for od in cur[r]:
                            union(d, od)
                    elif r in selfcap and False:
                        pass
                    cur[r] = {d}
    if coalesce:
        # one variable per VM register (except by-reference captured ones)
        first = {}
        for d in list(parent):
            r = d[2] if len(d) == 3 else d[1]
            if d[0] == 'param':
                r = cfg.param_regs[d[1] - 1]        # (the register the parameter arrives in)
            if r in cap:
                continue
            if r in first:
                union(first[r], d)
            else:
                first[r] = d
    # names
    names = {}

    def name_of(d):
        root = find(d)
        nm = names.get(root)
        if nm is None:
            nm = namer.var()
            names[root] = nm
        return nm

    params = []
    for i in range(1, cfg.nparams + 1):
        nm = namer.param()
        names[find(('param', i))] = nm
        params.append(nm)
    # pass 2: rewrite
    for bid in order:
        b = blocks[bid]
        cur = {r: set(ds) for r, ds in IN[bid].items()}
        new = []
        allst = b.stmts + [b.term]
        for si, st in enumerate(allst):
            reads, writes = stmt_exprs(st)

            def ren(x, cur=cur, si=si, st=st):
                if x[0] == 'r':
                    ds = cur.get(x[1])
                    if not ds:
                        key = ('undef', x[1])
                        if key not in parent:
                            newdef(key)
                        ds = {key}
                    return ('r', name_of(next(iter(ds))))
                if x[0] == 'closure':
                    caps = []
                    for c in x[2]:
                        if c[0] in ('ref', 'val'):
                            r = c[1]
                            if st[0] == 'set' and st[1] == ('r', r) and c[0] == 'ref':
                                caps.append((c[0], ('r', name_of((bid, si, r)))))
                            else:
                                ds = cur.get(r)
                                if not ds:
                                    key = ('undef', r)
                                    if key not in parent:
                                        newdef(key)
                                    ds = {key}
                                caps.append((c[0], ('r', name_of(next(iter(ds))))))
                        else:
                            caps.append(c)
                    return ('closure', x[1], tuple(caps))
                return x

            st2 = map_stmt(st, ren)
            # rename destinations
            if st[0] == 'set' and st[1][0] == 'r':
                st2 = ('set', ('r', name_of((bid, si, st[1][1]))), st2[2])
            elif st[0] == 'multiset':
                st2 = ('multiset', [('r', name_of((bid, si, w[1]))) if w[0] == 'r' else w for w in st[1]], st2[2])
            elif st[0] == 'forbr':
                st2 = ('forbr', st[1], tuple(name_of((bid, si, r)) for r in st[2]), st[3], st[4])
            for w in writes:
                if w[0] == 'r':
                    cur[w[1]] = {(bid, si, w[1])}
            new.append(st2)
        b.stmts = new[:-1]
        b.term = new[-1]
    cfg.params = params
    return params


def rpo(cfg):
    seen = set()
    post = []
    stack = [(cfg.entry, iter(cfg.blocks[cfg.entry].succs()))]
    seen.add(cfg.entry)
    while stack:
        node, it = stack[-1]
        adv = False
        for s in it:
            if s not in seen:
                seen.add(s)
                stack.append((s, iter(cfg.blocks[s].succs())))
                adv = True
                break
        if not adv:
            post.append(node)
            stack.pop()
    post.reverse()
    return post


def _sccs(nodes, succs):
    """Strongly connected components (Tarjan, iterative) of the subgraph induced by `nodes`."""
    index, low, on, stack, out = {}, {}, set(), [], []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(succs(root)))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            adv = False
            for w in it:
                if w not in nodes:
                    continue
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(succs(w))))
                    adv = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if adv:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = set()
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.add(w)
                    if w == v:
                        break
                out.append(comp)
    return out


def make_reducible(cfg, limit=300):
    """Node splitting for cycles with more than one entry.

    Unflattening a state machine can leave a loop that is entered both at its head and in its
    middle (the flattener shares the loop-step code of the first and of later iterations).  The
    part of the cycle between a secondary entry and the head is copied, so that jumps from outside
    run the copy and join the loop at its head."""
    from .ircfg import Block
    blocks = cfg.blocks
    nsplit = 0

    def succs(n):
        return blocks[n].succs()

    for _ in range(limit):
        cfg.compute_preds()
        order = rpo(cfg)
        idx = {b: i for i, b in enumerate(order)}
        todo = [set(order)]
        split = None
        while todo and split is None:
            nodes = todo.pop()
            for comp in _sccs(sorted(nodes, key=idx.get), succs):
                if len(comp) == 1:
                    n = next(iter(comp))
                    if n not in succs(n):
                        continue
                entries = {n for n in comp if n == cfg.entry or any(p not in comp and p in idx for p in blocks[n].preds)}
                if not entries:
                    continue
                head = min(entries, key=idx.get)
                if len(entries) > 1:
                    split = (comp, head, entries - {head})
                    break
                todo.append(comp - {head})
        if split is None:
            return
        comp, head, others = split
        for e in sorted(others, key=idx.get):
            region = set()
            stack = [e]
            while stack:
                x = stack.pop()
                if x in region or x == head or x not in comp:
                    continue
                region.add(x)
                stack.extend(succs(x))
            nsplit += 1
            names = {n: (n, 'dup', nsplit) for n in region}
            for n in region:
                nb = Block(names[n])
                nb.stmts = list(blocks[n].stmts)
                nb.term = blocks[n].term
                blocks[names[n]] = nb
            for n in region:
                for t in set(blocks[n].succs()):
                    if t in names:
                        blocks[names[n]].retarget(t, names[t])
            for p in list(blocks[e].preds):
                if p not in comp and p in idx:
                    blocks[p].retarget(e, names[e])
            if cfg.entry == e:
                cfg.entry = names[e]
            if len(blocks) > 60000:
                raise Unstructured('irreducible control flow (splitting explodes)')
    raise Unstructured('irreducible control flow')


def _all_exprs(e):
    out = []
    walk_expr(e, out)
    return out


# statements a join block may have to get a copy per way in (see Structurer._split_small_joins)
SMALL_JOIN = int(os.environ.get('DEVM_SMALL_JOIN', '2'))
SPLIT_ROUNDS = int(os.environ.get('DEVM_SPLIT_ROUNDS', '6'))


class EnclosingFollow(Exception):
    def __init__(self, target):
        Exception.__init__(self, 'jump to an enclosing join')
        self.target = target


# ------------------------------------------------------------------ structuring
class Structurer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.blocks = cfg.blocks
        self.ndup = 0
        make_reducible(cfg)
        cfg.compute_preds()
        self.order = rpo(cfg)
        self.rpo_idx = {b: i for i, b in enumerate(self.order)}
        self._dominators()
        self._loops()
        self.small_join = 3 if ircfg_mod.DROP_OPAQUE else SMALL_JOIN
        for _ in range(60 if ircfg_mod.DROP_OPAQUE else SPLIT_ROUNDS):
            if not self._split_small_joins():
                break
            cfg.compute_preds()
            self.order = rpo(cfg)
            self.rpo_idx = {b: i for i, b in enumerate(self.order)}
            self._dominators()
            self._loops()
        self.emitted = {}
        self.emit_log = []
        self.follows = []
        self.depth = 0
        self.pending = {}
        self.njumps = 0
        self.live_in, _ = cfg.liveness()

    def _dominators(self):
        idx = self.rpo_idx
        order = self.order
        idom = {order[0]: order[0]}

        def inter(a, b):
            while a != b:
                while idx[a] > idx[b]:
                    a = idom[a]
                while idx[b] > idx[a]:
                    b = idom[b]
            return a

        changed = True
        while changed:
            changed = False
            for b in order[1:]:
                new = None
                for p in self.blocks[b].preds:
                    if p in idom:
                        new = p if new is None else inter(p, new)
                if idom.get(b) != new:
                    idom[b] = new
                    changed = True
        self.idom = idom

    def _split_small_joins(self):
        """A branch whose arms meet at two join points needs a block that is left early (and a flag
        when the exits nest).  When one of the join points is tiny (`x = {}` and on to the other
        one: the shared else of `a and b or c`), every way into it gets its own copy instead."""
        blocks = self.blocks
        done = 0
        for b in list(self.order):
            ms = self.merge_children(b)
            if len(ms) < 2:
                continue
            for m in ms:
                blk = blocks[m]
                if m in self.loop_nodes or blk.term[0] not in ('goto', 'ret') or len(blk.stmts) > self.small_join:
                    continue
                if blk.term[0] == 'goto' and blk.term[1] == m:
                    continue
                if any(x[0] == 'closure' for st in blk.stmts for e in stmt_exprs(st)[0] for x in _all_exprs(e)):
                    continue
                if self.small_join <= 2 and any(st[0] != 'set' for st in blk.stmts):
                    continue
                preds = sorted(blk.preds, key=lambda x: self.rpo_idx.get(x, 0))
                if len(preds) < 2 or len(preds) > 4 or self.ndup + len(preds) > 400:
                    continue
                for n, pr in enumerate(preds[1:]):
                    self.ndup += 1
                    nb = Block((m, 'copy', self.ndup))
                    nb.stmts = list(blk.stmts)
                    nb.term = blk.term
                    blocks[nb.id] = nb
                    blocks[pr].retarget(m, nb.id)
                done += 1
                break
            if done:
                break
        return done

    def dominates(self, a, b):
        while True:
            if a == b:
                return True
            nb = self.idom[b]
            if nb == b:
                return False
            b = nb

    def _loops(self):
        self.loop_nodes = {}
        self.back_preds = {}
        for b in self.order:
            for s in self.blocks[b].succs():
                if self.dominates(s, b):
                    self.back_preds.setdefault(s, set()).add(b)
                elif self.rpo_idx[s] <= self.rpo_idx[b]:
                    raise Unstructured('irreducible edge')
        for h, latches in self.back_preds.items():
            nodes = {h}
            stack = list(latches)
            while stack:
                x = stack.pop()
                if x in nodes:
                    continue
                nodes.add(x)
                stack.extend(self.blocks[x].preds)
            self.loop_nodes[h] = nodes
        # if-follow candidates: forward in-degree
        self.fwd_preds = {}
        for b in self.order:
            n = 0
            for p in self.blocks[b].preds:
                if not (b in self.back_preds and p in self.back_preds[b]):
                    n += 1
            self.fwd_preds[b] = n
        self.children = {}
        for b, d in self.idom.items():
            if b != d:
                self.children.setdefault(d, []).append(b)

    def if_follow(self, b):
        best = None
        for c in self.children.get(b, ()):
            if self.fwd_preds[c] >= 2:
                if best is None or self.rpo_idx[c] > self.rpo_idx[best]:
                    best = c
        return best

    def merge_children(self, b):
        """Join points immediately dominated by b, in reverse post-order."""
        ms = [c for c in self.children.get(b, ()) if self.fwd_preds[c] >= 2]
        ms.sort(key=self.rpo_idx.get)
        return ms

    def emit_merges(self, cond, t, f, merges, stop, ctx, out):
        """A branch whose arms meet at several join points m1 < m2 < ... (reverse post-order).

        Every join point follows a block that can be left early:

            repeat                  -- leaving this block continues at m2
                repeat              -- leaving this block continues at m1
                    if cond then ... else ... end
                until true
                <m1>
            until true
            <m2>

        so no join point has to be emitted more than once."""
        cs = []
        c = ctx
        for m in reversed(merges):
            c = (None, m, c)
            cs.append(c)
        cs.reverse()
        T = self.arm(t, merges[0], cs[0])
        F = self.arm(f, merges[0], cs[0])
        cur = [make_if(cond, T, F)]
        for j, m in enumerate(merges):
            nxt = []
            if has_break(cur) or id(cs[j]) in self.pending:
                nxt.append(['repeat', cur, ('k', True), None])
            else:
                nxt.extend(cur)
            self.after_loop(cs[j], nxt)
            if j + 1 == len(merges):
                out.extend(nxt)
                return self.jump(m, stop, ctx, out)
            nb = self.jump(m, merges[j + 1], cs[j + 1], nxt)
            if nb is not None:
                self.build(nb, merges[j + 1], cs[j + 1], nxt)
            cur = nxt

    # -- building
    def run(self):
        return self.build(self.cfg.entry, None, None)

    def jump(self, x, stop, ctx, out):
        if x == stop:
            return None
        if ctx is not None:
            if x == ctx[0]:
                out.append(['continue'])
                return None
            if x == ctx[1]:
                out.append(['break'])
                return None
            c = ctx[2]
            through = [ctx]
            while c is not None:
                if x == c[0] or x == c[1]:
                    # continue / break of an enclosing loop: Luau has no labelled jumps, so leave the
                    # inner loops with a flag that each of them checks right after its `end`
                    self.njumps += 1
                    flag = ('r', 'exit%d' % self.njumps)
                    for l in through:
                        self.pending.setdefault(id(l), []).append(
                            (flag, ('continue' if x == c[0] else 'break') if l[2] is c else 'break'))
                    out.append(['assign', [flag], [('k', True)]])
                    out.append(['break'])
                    return None
                through.append(c)
                c = c[2]
        if x in self.follows:
            raise EnclosingFollow(x)
        return x

    def after_loop(self, lctx, out):
        """Checks for the multi-level exits that pass through the loop just emitted."""
        for flag, kind in self.pending.pop(id(lctx), ()):
            out.append(make_if(flag, [[kind]], []))

    def build(self, b, stop, ctx, out=None):
        if out is None:
            out = []
        self.depth += 1
        if self.depth > DEPTH_LIMIT:
            raise Unstructured('too deep')
        while b is not None:
            n = self.emitted.get(b, 0) + 1
            self.emitted[b] = n
            self.emit_log.append(b)
            if n > DUP_LIMIT:
                raise Unstructured('block duplicated too often')
            if b in self.loop_nodes or self.blocks[b].term[0] == 'forbr':
                b = self.emit_loop(b, stop, ctx, out)
            else:
                b = self.emit_block(b, stop, ctx, out)
        self.depth -= 1
        return out

    def arm(self, x, stop, ctx):
        tmp = []
        nb = self.jump(x, stop, ctx, tmp)
        if nb is not None:
            self.build(nb, stop, ctx, tmp)
        return tmp

    def conv_stmts(self, b, out, skip_last_prep=False):
        for st in self.blocks[b].stmts:
            t = st[0]
            if t == 'set':
                out.append(['assign', [st[1]], [st[2]]])
            elif t == 'setindex':
                out.append(['assign', [('index', st[1], st[2])], [st[3]]])
            elif t == 'callstmt':
                out.append(['call', st[1]])
            elif t == 'multiset':
                out.append(['assign', list(st[1]), [st[2]]])
            elif t == 'close':
                pass
            elif t == 'error':
                out.append(['comment', 'LIFT ERROR: ' + st[1]])
            elif t == 'trap':
                out.append(['comment', 'tamper response (never taken in the traced run)'])
            elif t == 'cold':
                pass
            elif t == 'setlist':
                out.append(['setlist', st[1], st[2], st[3], st[4]])
            elif t in ('numforprep', 'genforprep'):
                out.append(['prep', t, st[1]])
            else:
                out.append(['comment', 'unknown IR ' + repr(st)[:80]])

    def emit_block(self, b, stop, ctx, out):
        blk = self.blocks[b]
        self.conv_stmts(b, out)
        term = blk.term
        if term[0] == 'goto':
            return self.jump(term[1], stop, ctx, out)
        if term[0] == 'ret':
            vals = list(term[1])
            if term[2]:
                vals.append(term[2])
            out.append(['return', vals])
            return None
        if term[0] == 'br':
            _, c, t, f = term
            if t == f:
                return self.jump(t, stop, ctx, out)
            merges = self.merge_children(b)
            if len(merges) >= 2:
                return self.emit_merges(c, t, f, merges, stop, ctx, out)
            follow = self.if_follow(b)
            if follow is not None:
                mark = (len(self.emit_log), self.njumps, {k: list(v) for k, v in self.pending.items()}, self.depth)
                self.follows.append(follow)
                try:
                    T = self.arm(t, follow, ctx)
                    F = self.arm(f, follow, ctx)
                    out.append(make_if(c, T, F))
                except EnclosingFollow as ex:
                    if ex.target != follow:
                        raise
                    # Something nested inside the arms jumps straight to the join (the later
                    # alternatives of `a or b or c`): the arms become a block that can be left
                    # early, instead of each such jump dragging a copy of the join along.
                    for x in self.emit_log[mark[0]:]:
                        self.emitted[x] -= 1
                    del self.emit_log[mark[0]:]
                    self.njumps = mark[1]
                    self.pending = mark[2]
                    self.depth = mark[3]
                    while self.follows and self.follows[-1] != follow:
                        self.follows.pop()
                    self.follows.pop()
                    lctx = (None, follow, ctx)
                    T = self.arm(t, follow, lctx)
                    F = self.arm(f, follow, lctx)
                    out.append(['repeat', [make_if(c, T, F)], ('k', True), None])
                    self.after_loop(lctx, out)
                    return self.jump(follow, stop, ctx, out)
                finally:
                    if self.follows and self.follows[-1] == follow:
                        self.follows.pop()
                return self.jump(follow, stop, ctx, out)
            T = self.arm(t, stop, ctx)
            F = self.arm(f, stop, ctx)
            if terminates(T) and not (terminates(F) and size(F) < size(T)):
                out.append(make_if(c, T, []))
                out.extend(F)
            elif terminates(F):
                out.append(make_if(negate(c), F, []))
                out.extend(T)
            else:
                out.append(make_if(c, T, F))
            return None
        raise Unstructured('bad terminator %r' % (term[0],))

    def emit_loop(self, h, stop, ctx, out):
        blk = self.blocks[h]
        nodes = self.loop_nodes.get(h, {h})
        term = blk.term
        if term[0] == 'forbr':
            self.conv_stmts(h, out)
            follow = term[4]
            lctx = (h, follow, ctx)
            prep = None
            if out and out[-1][0] == 'prep':
                prep = out.pop()
            body = self.arm(term[3], h, lctx)
            if prep is None:
                out.append(['comment', 'for-loop without visible setup'])
                exprs = [('opaque', '?')]
                kind = term[1]
            else:
                exprs = list(prep[2])
                kind = 'num' if prep[1] == 'numforprep' else 'gen'
            if kind == 'num':
                if len(exprs) == 3 and exprs[2] == ('k', 1):
                    exprs = exprs[:2]
                out.append(['fornum', term[2][0], exprs, body, h])
            else:
                # R[A], R[A+1], R[A+2] = call   immediately before
                if out and out[-1][0] == 'assign' and len(out[-1][1]) == 3 and list(out[-1][1]) == exprs:
                    exprs = out.pop()[2]
                else:
                    while len(exprs) > 1 and exprs[-1] == ('k', None):
                        exprs.pop()
                out.append(['forin', list(term[2]), exprs, body, h])
            self.after_loop(lctx, out)
            return self.jump(follow, stop, ctx, out)
        # exits
        targets = {}
        for n in nodes:
            for s in self.blocks[n].succs():
                if s not in nodes:
                    targets[s] = targets.get(s, 0) + 1
        follow = None
        if targets:
            follow = min(targets, key=lambda x: self.rpo_idx[x])
        lctx = (h, follow, ctx)
        if term[0] == 'br' and not blk.stmts and ((term[2] in nodes) != (term[3] in nodes)):
            inside = term[2] if term[2] in nodes else term[3]
            outside = term[3] if term[2] in nodes else term[2]
            if outside == follow:
                cond = term[1] if inside == term[2] else negate(term[1])
                body = self.arm(inside, h, lctx)
                out.append(['while', cond, body, h])
                self.after_loop(lctx, out)
                return self.jump(follow, stop, ctx, out)
            # the loop test leaves to somewhere other than the loop's follow block: general loop
        body = []
        nb = self.emit_block(h, h, lctx, body)
        if nb is not None:
            self.build(nb, h, lctx, body)
        out.append(['while', ('k', True), body, h])
        self.after_loop(lctx, out)
        if follow is None:
            return None
        return self.jump(follow, stop, ctx, out)


def make_if(c, T, F):
    if not T and F:
        return ['if', negate(c), F, None]
    return ['if', c, T, F if F else None]


def size(stmts):
    """Number of statements, nested ones included."""
    n = 0
    for st in stmts:
        n += 1
        for x in st[1:]:
            if isinstance(x, list) and x and isinstance(x[0], list):
                n += size(x)
    return n


def terminates(stmts):
    if not stmts:
        return False
    last = stmts[-1]
    if last[0] in ('return', 'break', 'continue'):
        return True
    if last[0] == 'if' and last[3]:
        return terminates(last[2]) and terminates(last[3])
    return False


# ------------------------------------------------------------------ AST clean-up
def attach_preps(stmts):
    """A for loop whose setup instruction did not end up right in front of it: a few register
    copies in between, or one setup per path that reaches the loop."""
    def preps_in(st, found):
        """the setups inside a statement, with the list each sits in"""
        t = st[0]
        subs = []
        if t == 'if':
            subs = [st[2], st[3] or []]
        elif t in ('while',):
            subs = [st[2]]
        elif t in ('repeat', 'do'):
            subs = [st[1]]
        for sub in subs:
            for x in sub:
                if x[0] == 'prep':
                    found.append((sub, x))
                else:
                    preps_in(x, found)

    def walk(lst):
        i = 0
        while i < len(lst):
            st = lst[i]
            t = st[0]
            if t == 'if':
                walk(st[2])
                if st[3]:
                    walk(st[3])
            elif t == 'while':
                walk(st[2])
            elif t in ('repeat', 'do'):
                walk(st[1])
            elif t in ('fornum', 'forin'):
                walk(st[3])
            if t == 'comment' and st[1] == 'for-loop without visible setup' and i + 1 < len(lst) \
                    and lst[i + 1][0] in ('fornum', 'forin') and lst[i + 1][2] == [('opaque', '?')]:
                loop = lst[i + 1]
                j = i - 1
                found = []
                while j >= 0:
                    x = lst[j]
                    if x[0] == 'prep':
                        found = [(lst, x)]
                        break
                    if x[0] in ('if', 'while', 'repeat', 'do'):
                        preps_in(x, found)
                        break
                    if x[0] in ('fornum', 'forin', 'return', 'break', 'continue') or "'call'" in repr(x):
                        break
                    j -= 1
                kind = 'numforprep' if loop[0] == 'fornum' else 'genforprep'
                if found and all(f[1][1] == kind and f[1][2] == found[0][1][2] for f in found):
                    exprs = list(found[0][1][2])
                    # R[A], R[A+1], R[A+2] = call   right before every setup
                    befores = []
                    for sub, x in found:
                        k = next(n for n, y in enumerate(sub) if y is x)
                        b = sub[k - 1] if k else None
                        if b is not None and b[0] == 'assign' and len(b[1]) == 3 and list(b[1]) == exprs \
                                and (len(found) == 1 or k == len(sub) - 1):
                            befores.append((sub, b))
                    if kind == 'genforprep' and len(befores) == len(found) and all(b[1][2] == befores[0][1][2] for b in befores):
                        exprs = befores[0][1][2]
                        for sub, b in befores:
                            sub[:] = [y for y in sub if y is not b]
                    for sub, x in found:
                        sub[:] = [y for y in sub if y is not x]
                    if kind == 'numforprep':
                        if len(exprs) == 3 and exprs[2] == ('k', 1):
                            exprs = exprs[:2]
                    else:
                        exprs = list(exprs) if isinstance(exprs, (list, tuple)) else [exprs]
                        while len(exprs) > 1 and exprs[-1] == ('k', None):
                            exprs.pop()
                    loop[2] = list(exprs)
                    i = next(n for n, y in enumerate(lst) if y is st)
                    del lst[i]
                    continue
            i += 1
    walk(stmts)
    return stmts


def cleanup(stmts, in_loop_tail=False):
    """Cosmetic rewrites on the structured AST."""
    out = []
    for st in stmts:
        t = st[0]
        if t == 'if':
            st[2] = cleanup(st[2])
            if st[3]:
                st[3] = cleanup(st[3])
        elif t in ('while',):
            st[2] = cleanup(strip_tail_continue(st[2]))
            body = st[2]
            if st[1] == ('k', True) and body:
                first = body[0]
                last = body[-1]
                if first[0] == 'if' and not first[3] and len(first[2]) == 1 and first[2][0][0] == 'break':
                    st[1] = negate(first[1])
                    st[2] = body[1:]
                elif last[0] == 'if' and not last[3] and len(last[2]) == 1 and last[2][0][0] == 'break' \
                        and not has_continue(body[:-1]):
                    st = ['repeat', body[:-1], last[1], st[3]]
        elif t in ('fornum', 'forin'):
            st[3] = cleanup(strip_tail_continue(st[3]))
        out.append(st)
    return out


def has_break(stmts):
    """True if a `break` in stmts would leave the construct that directly encloses them."""
    for st in stmts:
        if st[0] == 'break':
            return True
        if st[0] == 'if':
            if has_break(st[2]) or (st[3] and has_break(st[3])):
                return True
    return False


def has_continue(stmts):
    for st in stmts:
        if st[0] == 'continue':
            return True
        if st[0] == 'if':
            if has_continue(st[2]) or (st[3] and has_continue(st[3])):
                return True
    return False


def strip_tail_continue(stmts):
    if not stmts:
        return stmts
    last = stmts[-1]
    if last[0] == 'continue':
        return strip_tail_continue(stmts[:-1])
    if last[0] == 'if':
        last[2] = strip_tail_continue(last[2])
        if last[3]:
            last[3] = strip_tail_continue(last[3])
            if not last[3]:
                last[3] = None
        if not last[2] and last[3]:
            last[1] = negate(last[1])
            last[2], last[3] = last[3], None
        if not last[2] and not last[3]:
            return stmts[:-1] + [['call_discard', last[1]]] if expr_reads(last[1])[2] else stmts[:-1]
    return stmts


# ------------------------------------------------------------------ declarations
def stmt_vars(st, deep=True):
    """variable names occurring in a statement (optionally including nested blocks)."""
    names = []
    t = st[0]

    def ex(e):
        for x in walk_expr(e, []):
            if x[0] == 'r':
                names.append(x[1])

    if t == 'assign':
        for d in st[1]:
            ex(d)
        for v in st[2]:
            ex(v)
    elif t in ('call', 'call_discard'):
        ex(st[1])
    elif t == 'return':
        for v in st[1]:
            ex(v)
    elif t == 'if':
        ex(st[1])
        if deep:
            for s in st[2]:
                names.extend(stmt_vars(s))
            for s in st[3] or ():
                names.extend(stmt_vars(s))
    elif t == 'while':
        ex(st[1])
        if deep:
            for s in st[2]:
                names.extend(stmt_vars(s))
    elif t == 'do':
        for s in st[1]:
            names.extend(stmt_vars(s))
    elif t in ('local', 'localfunction'):
        if t == 'local':
            for v in st[2]:
                ex(v)
        else:
            ex(st[2])
    elif t == 'repeat':
        ex(st[2])
        if deep:
            for s in st[1]:
                names.extend(stmt_vars(s))
    elif t == 'fornum' or t == 'forin':
        for e in st[2]:
            ex(e)
        if deep:
            for s in st[3]:
                names.extend(stmt_vars(s))
    elif t == 'setlist':
        ex(st[1])
        for v in st[3]:
            ex(v)
        if st[4]:
            ex(st[4])
    elif t == 'prep':
        for e in st[2]:
            ex(e)
    return names


def declare(root, params, live_in):
    """Insert `local` declarations. root: list of statements."""
    occ = {}        # name -> list of paths; path = tuple of (block_obj_id, index)
    block_info = {}  # id(block) -> (block, parent path, loop header or None)
    forvars = {}

    def visit(block, path, loop):
        block_info[id(block)] = (block, path, loop)
        for i, st in enumerate(block):
            here = path + ((id(block), i),)
            for nm in stmt_vars(st, deep=False):
                occ.setdefault(nm, []).append(here)
            t = st[0]
            if t == 'if':
                visit(st[2], here, None)
                if st[3]:
                    visit(st[3], here, None)
            elif t == 'while':
                visit(st[2], here, st[3])
            elif t == 'repeat':
                visit(st[1], here, st[3])
            elif t == 'fornum':
                forvars[st[1]] = id(st[3])
                visit(st[3], here, st[4])
            elif t == 'forin':
                for v in st[1]:
                    forvars[v] = id(st[3])
                visit(st[3], here, st[4])

    visit(root, (), None)
    pset = set(params)
    inserts = {}    # id(block) -> {index: [names]}
    localize = {}   # (id(block), index) -> set(names)
    for nm, paths in occ.items():
        if nm in pset or nm in forvars:
            continue
        common = paths[0]
        for p in paths[1:]:
            n = min(len(common), len(p))
            k = 0
            while k < n and common[k] == p[k]:
                k += 1
            if k < n and common[k][0] == p[k][0]:
                common = common[:k] + ((common[k][0], min(common[k][1], p[k][1])),)
            else:
                common = common[:k]
        if not common:
            common = ((id(root), 0),)
        # loop-carried values must be declared outside the loop
        for depth in range(1, len(common)):
            loop = block_info[common[depth][0]][2]
            if loop is not None and nm in live_in.get(loop, ()):
                common = common[:depth]
                break
        bid_, idx = common[-1]
        blk = block_info[bid_][0]
        st = blk[idx] if idx < len(blk) else None
        if st is not None and st[0] == 'assign' and all(d[0] == 'r' for d in st[1]) and ('r', nm) in st[1] \
                and any(p == common for p in paths):
            localize.setdefault((bid_, idx), set()).add(nm)
        else:
            inserts.setdefault(bid_, {}).setdefault(idx, []).append(nm)
    # apply
    for (bid_, idx), names in localize.items():
        blk = block_info[bid_][0]
        st = blk[idx]
        targets = [d[1] for d in st[1]]
        if all(t in names for t in targets):
            used_in_rhs = set()
            for v in st[2]:
                for x in walk_expr(v, []):
                    if x[0] == 'r':
                        used_in_rhs.add(x[1])
            if len(targets) == 1 and targets[0] in used_in_rhs and st[2][0][0] == 'closure':
                blk[idx] = ['localfunction', targets[0], st[2][0]]
            elif used_in_rhs & set(targets):
                inserts.setdefault(bid_, {}).setdefault(idx, []).extend(targets)
            else:
                blk[idx] = ['local', targets, st[2]]
        else:
            inserts.setdefault(bid_, {}).setdefault(idx, []).extend(sorted(names))
    for bid_, d in inserts.items():
        blk = block_info[bid_][0]
        for idx in sorted(d, reverse=True):
            blk.insert(idx, ['local', sorted(set(d[idx]), key=natural), []])


def tighten_scopes(stmts):
    """Wrap short-lived locals in do-blocks so a function stays under Luau's 200-local limit."""
    for st in stmts:
        t = st[0]
        if t == 'if':
            tighten_scopes(st[2])
            if st[3]:
                tighten_scopes(st[3])
        elif t == 'while':
            tighten_scopes(st[2])
        elif t == 'repeat':
            tighten_scopes(st[1])
        elif t in ('fornum', 'forin'):
            tighten_scopes(st[3])
    changed = True
    while changed:
        changed = False
        uses = [set(stmt_vars(st)) for st in stmts]
        for i in range(len(stmts) - 1, -1, -1):
            st = stmts[i]
            if st[0] not in ('local', 'localfunction'):
                continue
            names = set(st[1]) if st[0] == 'local' else {st[1]}
            last = i
            for j in range(i + 1, len(stmts)):
                if uses[j] & names:
                    last = j
            if last == len(stmts) - 1 or last - i > 40:
                continue
            # locals declared inside [i, last] must not be used afterwards
            inner = set()
            for k in range(i, last + 1):
                if stmts[k][0] == 'local':
                    inner |= set(stmts[k][1])
                elif stmts[k][0] == 'localfunction':
                    inner.add(stmts[k][1])
            later = set()
            for k in range(last + 1, len(stmts)):
                later |= uses[k]
            if inner & later:
                continue
            if any(s2[0] in ('break', 'continue', 'return') for s2 in stmts[i:last + 1]) and False:
                continue
            stmts[i:last + 1] = [['do', stmts[i:last + 1]]]
            changed = True
            break


def section_scopes(stmts, limit=150):
    """Keep a long function under Luau's 200-local limit the way its author did: consecutive
    sections in `do ... end`, each with the locals nothing after it uses, and the long-lived
    locals (services, shared state, functions used throughout) at the function's own level."""
    n = len(stmts)
    decl, last = {}, {}
    for i, st in enumerate(stmts):
        for name in (st[1] if st[0] == 'local' else [st[1]] if st[0] == 'localfunction' else ()):
            decl.setdefault(name, i)
    if len(decl) <= limit:
        return False
    for j, st in enumerate(stmts):
        for name in stmt_vars(st):
            if name in decl and j >= decl[name]:
                last[name] = j
    for name in decl:
        last.setdefault(name, decl[name])
    by_span = sorted(decl, key=lambda x: (decl[x] - last[x], decl[x]))
    outer = set()

    def components():
        ivs = sorted((decl[x], last[x]) for x in decl if x not in outer)
        comps = []
        for a, b in ivs:
            if comps and a <= comps[-1][1]:
                comps[-1][1] = max(comps[-1][1], b)
                comps[-1][2] += 1
            else:
                comps.append([a, b, 1])
        return comps

    comps = components()
    k = 0
    while comps and len(outer) + max(c[2] for c in comps) > limit and k < len(by_span) and len(outer) < limit - 20:
        # (several at a time: one long-lived local rarely splits a section on its own)
        for x in by_span[k:k + 4]:
            outer.add(x)
        k += 4
        comps = components()
    # A function-level local declared inside a section needs a forward declaration.  Where a
    # few section locals are all that span such declarations, they move up instead.
    budget = 186 - len(outer) - max([c[2] for c in comps] or [0])
    bars = sorted({decl[x] for x in outer})
    import bisect
    for a, b, cnt in sorted(comps, key=lambda c: -(bisect.bisect_right(bars, c[1]) - bisect.bisect_left(bars, c[0]))):
        inside = bisect.bisect_right(bars, b) - bisect.bisect_left(bars, a)
        if not inside:
            break
        cross = []
        for x in decl:
            if x not in outer and a <= decl[x] <= b:
                i = bisect.bisect_right(bars, decl[x])
                if i < len(bars) and bars[i] <= last[x]:
                    cross.append(x)
        if cross and len(cross) <= budget and len(cross) < inside:
            outer.update(cross)
            budget -= len(cross)
    comps = components()
    if os.environ.get('DEVM_TRACE_LOCALS'):
        print('sections: %d locals, %d at function level, %d sections, largest %d' % (
            len(decl), len(outer), len(comps), max([c[2] for c in comps] or [0])))
    if not comps or len(outer) + max(c[2] for c in comps) > limit + 40:
        return False
    out = []
    pos = 0
    for a, b, cnt in comps:
        out.extend(stmts[pos:a])
        block = []
        for st in stmts[a:b + 1]:
            names = st[1] if st[0] == 'local' else [st[1]] if st[0] == 'localfunction' else ()
            if names and any(x in outer for x in names):
                # declared inside the section but needed after it
                out.append(['local', list(names), []])
                if st[0] == 'local':
                    if st[2]:
                        block.append(['assign', [('r', x) for x in names], list(st[2])])
                else:
                    block.append(['assign', [('r', st[1])], [st[2]]])
            else:
                block.append(st)
        if b > a and cnt >= 1:
            out.append(['do', block])
        else:
            out.extend(block)
        pos = b + 1
    out.extend(stmts[pos:])
    # forward declarations of one section on as few lines as it takes
    merged = []
    for st in out:
        if st[0] == 'local' and not st[2] and merged and merged[-1][0] == 'local' and not merged[-1][2] \
                and len(merged[-1][1]) + len(st[1]) <= 10:
            merged[-1][1].extend(st[1])
        else:
            merged.append(st)
    stmts[:] = merged
    return True


def max_locals(stmts, base=0):
    """Upper bound of simultaneously declared locals."""
    cur = base
    best = base
    for st in stmts:
        t = st[0]
        if t == 'local':
            cur += len(st[1])
        elif t == 'localfunction':
            cur += 1
        elif t == 'if':
            best = max(best, max_locals(st[2], cur), max_locals(st[3], cur) if st[3] else 0)
        elif t == 'while':
            best = max(best, max_locals(st[2], cur))
        elif t == 'do':
            best = max(best, max_locals(st[1], cur))
        elif t == 'repeat':
            best = max(best, max_locals(st[1], cur))
        elif t == 'fornum':
            best = max(best, max_locals(st[3], cur + 4))
        elif t == 'forin':
            best = max(best, max_locals(st[3], cur + 3 + len(st[1])))
        best = max(best, cur)
    return best


def natural(s):
    return [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', s)]


# ------------------------------------------------------------------ printer
PREC = {
    'or': 1, 'and': 2, '<': 3, '>': 3, '<=': 3, '>=': 3, '~=': 3, '==': 3, '..': 4,
    '+': 5, '-': 5, '*': 6, '/': 6, '//': 6, '%': 6, 'unary': 7, '^': 8,
}
RIGHT = {'..', '^'}
IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
KEYWORDS = {'and', 'break', 'do', 'else', 'elseif', 'end', 'false', 'for', 'function', 'if', 'in', 'local',
            'nil', 'not', 'or', 'repeat', 'return', 'then', 'true', 'until', 'while', 'continue'}


def lua_string(s):
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == '\\':
            out.append('\\\\')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif 32 <= o < 127:
            out.append(ch)
        else:
            out.append('\\%03d' % o)
    out.append('"')
    return ''.join(out)


def decode_utf8ish(s):
    """Strings are latin-1 views of bytes; show valid UTF-8 as-is."""
    try:
        b = s.encode('latin-1')
        u = b.decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        return None
    if all(ch.isprintable() or ch in '\n\r\t' for ch in u):
        return u
    return None


def lua_string_pretty(s):
    if all(ord(c) < 127 for c in s):
        return lua_string(s)
    u = decode_utf8ish(s)
    if u is None:
        return lua_string(s)
    out = ['"']
    for ch in u:
        if ch == '"':
            out.append('\\"')
        elif ch == '\\':
            out.append('\\\\')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        else:
            out.append(ch)
    out.append('"')
    return ''.join(out)


def lua_number(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    if v != v:
        return '(0/0)'
    if v == math.inf:
        return 'math.huge'
    if v == -math.inf:
        return '-math.huge'
    if v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    r = repr(v)
    return r


class Printer:
    def __init__(self, decomp, upnames):
        self.d = decomp
        self.upnames = upnames
        self.lines = []

    def expr(self, e, prec=0):
        t = e[0]
        if t == 'k':
            v = e[1]
            if v is None:
                return 'nil'
            if isinstance(v, str):
                return lua_string_pretty(v)
            s = lua_number(v)
            if s.startswith('-') and prec > PREC['unary']:
                return '(' + s + ')'
            return s
        if t == 'r':
            return str(e[1])
        if t == 'up' or t == 'upcell':
            if e[1] < len(self.upnames):
                return self.upnames[e[1]]
            return 'UPVAL_%d' % e[1]
        if t == 'global':
            return e[1] if IDENT.match(e[1]) and e[1] not in KEYWORDS else 'getfenv()[%s]' % lua_string(e[1])
        if t == 'kfn':
            return e[1]
        if t == 'env':
            return 'getfenv()'
        if t == 'va':
            return '...'
        if t == 'varargn':
            return '(select(%d, ...))' % e[1]
        if t == 'arg':
            return 'ARG_%d' % e[1]
        if t == 'nargs':
            return 'select("#", ...)'
        if t == 'index' and e[1] == ('env',) and e[2][0] == 'k' and isinstance(e[2][1], str) \
                and IDENT.match(e[2][1]) and e[2][1] not in KEYWORDS:
            return e[2][1]
        if t == 'index' or t == 'method':
            obj = self.prefix(e[1])
            k = e[2] if t == 'index' else ('k', e[2])
            if k[0] == 'k' and isinstance(k[1], str) and IDENT.match(k[1]) and k[1] not in KEYWORDS:
                return obj + '.' + k[1]
            return obj + '[' + self.expr(k) + ']'
        if t == 'call':
            f = e[1]
            args = list(e[2])
            if f[0] == 'index' and args and args[0] == f[1] and f[1][0] in ('r', 'up') and f[2][0] == 'k' \
                    and isinstance(f[2][1], str) and IDENT.match(f[2][1]) and f[2][1] not in KEYWORDS:
                head = self.prefix(f[1]) + ':' + f[2][1]
                args = args[1:]
            elif f[0] == 'method' and args and args[0][0] == 'selfarg':
                head = self.prefix(f[1]) + ':' + f[2]
                args = args[1:]
            else:
                head = self.prefix(f)
            parts = [self.expr(a) for a in args]
            if e[3]:
                parts.append(self.expr(e[3]))
            elif args and args[-1][0] in ('call', 'va'):
                parts[-1] = '(' + parts[-1] + ')'
            return head + '(' + ', '.join(parts) + ')'
        if t == 'bin':
            op = e[1]
            p = PREC[op]
            if op in RIGHT:
                s = self.expr(e[2], p + 1) + ' ' + op + ' ' + self.expr(e[3], p)
            else:
                s = self.expr(e[2], p) + ' ' + op + ' ' + self.expr(e[3], p + 1)
            return '(' + s + ')' if p < prec else s
        if t == 'and' or t == 'or':
            p = PREC[t]
            s = self.expr(e[1], p) + ' ' + t + ' ' + self.expr(e[2], p + 1)
            return '(' + s + ')' if p < prec else s
        if t == 'un':
            op = e[1]
            p = PREC['unary']
            inner = self.expr(e[2], p)
            s = ('not ' + inner) if op == 'not' else (op + inner)
            if op == '-' and inner.startswith('-'):
                s = '-(' + inner + ')'
            return '(' + s + ')' if p < prec else s
        if t == 'newtable':
            return '{}'
        if t == 'table':
            return self.table(e)
        if t == 'closure':
            return self.d.closure_text(e, self)
        if t == 'selfarg':
            return 'nil --[[self]]'
        if t == 'regs':
            return '({})'
        if t == 'outer':
            return e[1]
        if t == 'constfn':
            return '(function() return %s end)' % lua_number(e[1])
        if t == 'opaque':
            return 'nil --[[%s]]' % e[1]
        if t == 'forval':
            return 'nil --[[forval]]'
        return 'nil --[[?%s]]' % t

    def prefix(self, e):
        s = self.expr(e, 9)
        if e[0] in ('r', 'up', 'upcell', 'global', 'index', 'call', 'method', 'kfn', 'env', 'outer'):
            return s
        if e[0] == 'k' and isinstance(e[1], str):
            return '(' + s + ')'
        if s.startswith('('):
            return s
        return '(' + s + ')'

    def table(self, e):
        items = []
        n = 0
        for k, v in e[1]:
            if k is None:
                items.append(self.expr(v))
            elif k[0] == 'k' and isinstance(k[1], str) and IDENT.match(k[1]) and k[1] not in KEYWORDS:
                items.append('%s = %s' % (k[1], self.expr(v)))
            else:
                items.append('[%s] = %s' % (self.expr(k), self.expr(v)))
        if e[2]:
            items.append(self.expr(e[2]))
        elif e[1] and e[1][-1][0] is None and e[1][-1][1][0] in ('call', 'va'):
            items[-1] = '(' + items[-1] + ')'
        if not items:
            return '{}'
        one = '{ ' + ', '.join(items) + ' }'
        if len(one) <= 100 and '\n' not in one:
            return one
        ind = '\t' * (self.level + 1)
        return '{\n' + ''.join(ind + it.replace('\n', '\n') + ',\n' for it in items) + '\t' * self.level + '}'

    level = 0

    def emit(self, text):
        if text.startswith('('):
            # a statement that starts with `(` would continue the previous one; Luau has no empty
            # statement, so at the start of a block (nothing to continue) there is no `;`
            prev = self.lines[-1].strip() if self.lines else ''
            if prev and not re.search(r'(\b(do|then|else|repeat)|\))$', prev) or prev.endswith(')') and not re.search(r'function\b[^()]*\([^()]*\)$', prev):
                text = ';' + text
        self.lines.append('\t' * self.level + text)

    def block(self, stmts):
        self.level += 1
        for st in stmts:
            self.stmt(st)
        self.level -= 1

    def target(self, d):
        return self.expr(d)

    def stmt(self, st):
        t = st[0]
        if t == 'assign':
            if len(st[1]) == 1 and len(st[2]) == 1:
                d, v = st[1][0], st[2][0]
                if v[0] == 'bin' and v[2] == d and v[1] in ('+', '-', '*', '/', '//', '%', '^', '..') and d[0] in ('r', 'up', 'index'):
                    self.emit('%s %s= %s' % (self.target(d), v[1], self.expr(v[3])))
                    return
            self.emit('%s = %s' % (', '.join(self.target(d) for d in st[1]), ', '.join(self.expr(v) for v in st[2])))
        elif t == 'local':
            if st[2]:
                self.emit('local %s = %s' % (', '.join(st[1]), ', '.join(self.expr(v) for v in st[2])))
            else:
                self.emit('local %s' % ', '.join(st[1]))
        elif t == 'localfunction':
            text = self.d.closure_text(st[2], self, name=st[1])
            self.emit('local ' + text)
        elif t == 'call':
            text = self.expr(st[1])
            self.emit(text)
        elif t == 'call_discard':
            self.emit('local _ = ' + self.expr(st[1]))
        elif t == 'return':
            self.emit('return' + (' ' + ', '.join(self.expr(v) for v in st[1]) if st[1] else ''))
        elif t == 'break':
            self.emit('break')
        elif t == 'continue':
            self.emit('continue')
        elif t == 'comment':
            self.emit('-- ' + st[1])
        elif t == 'if':
            self.emit('if %s then' % self.expr(st[1]))
            self.block(st[2])
            els = st[3]
            while els:
                if len(els) == 1 and els[0][0] == 'if':
                    self.emit('elseif %s then' % self.expr(els[0][1]))
                    self.block(els[0][2])
                    els = els[0][3]
                else:
                    self.emit('else')
                    self.block(els)
                    break
            self.emit('end')
        elif t == 'while':
            self.emit('while %s do' % self.expr(st[1]))
            self.block(st[2])
            self.emit('end')
        elif t == 'do':
            self.emit('do')
            self.block(st[1])
            self.emit('end')
        elif t == 'repeat':
            self.emit('repeat')
            self.block(st[1])
            self.emit('until %s' % self.expr(st[2]))
        elif t == 'fornum':
            bounds = [self.expr(e) for e in st[2]]
            if len(bounds) < 2:
                bounds.append('nil --[[bounds not recovered]]')      # keeps the text parseable
            self.emit('for %s = %s do' % (st[1], ', '.join(bounds)))
            self.block(st[3])
            self.emit('end')
        elif t == 'forin':
            self.emit('for %s in %s do' % (', '.join(st[1]), ', '.join(self.expr(e) for e in st[2])))
            self.block(st[3])
            self.emit('end')
        elif t == 'setlist':
            tbl = self.prefix(st[1])
            for i, v in enumerate(st[3]):
                self.emit('%s[%d] = %s' % (tbl, st[2] + i, self.expr(v)))
            if st[4]:
                self.emit('for __i, __v in { %s } do %s[%d + __i] = __v end' % (self.expr(st[4]), tbl, st[2] + len(st[3]) - 1))
        elif t == 'prep':
            # (an operand can print over several lines: a table or a function literal)
            text = ', '.join(self.expr(e) for e in st[2])
            self.emit('-- for-loop setup: ' + re.sub(r'\s*\n\s*', ' ', text))
        else:
            self.emit('-- ?? ' + repr(st)[:100])


FLAT_FALLBACK = True


def flat_ast(st, why, params=()):
    """Fallback for a function the structurer gives up on: a state machine with one `if` per basic
    block, in reverse post-order.  `state` holds the number of the block to run next.  For-loops
    keep the VM's own form: the setup is shown as a comment, the step-and-test as FOR_NEXT(var)."""
    order = sorted(st.blocks, key=lambda b: st.rpo_idx.get(b, 1 << 30))
    num = {b: i + 1 for i, b in enumerate(order)}
    state = ('r', 'state')

    def go(target):
        return ['assign', [state], [('k', num[target])]]

    names = set()
    body = []
    for b in order:
        blk = st.blocks[b]
        out = []
        st.conv_stmts(b, out)
        for x in out:
            if x[0] == 'assign':
                names.update(t[1] for t in x[1] if t[0] == 'r' and isinstance(t[1], str))
        term = blk.term
        if term[0] == 'goto':
            out.append(go(term[1]))
        elif term[0] == 'ret':
            vals = list(term[1])
            if term[2]:
                vals.append(term[2])
            out.append(['return', vals])
        elif term[0] == 'br':
            if term[2] == term[3]:
                out.append(go(term[2]))
            else:
                out.append(['if', term[1], [go(term[2])], [go(term[3])]])
        elif term[0] == 'forbr':
            cond = ('call', ('global', 'FOR_NEXT'), [('r', x) for x in term[2]], None)
            names.update(x for x in term[2] if isinstance(x, str))
            out.append(['if', cond, [go(term[3])], [go(term[4])]])
        else:
            out.append(['comment', 'unknown terminator ' + repr(term[0])])
        body.append(['if', ('bin', '==', state, ('k', num[b])), out, None])
    # every variable lives across iterations of the dispatch loop, so all are declared up front
    # (more than Luau's 200 locals in a big function: the listing is for reading, not for running)
    decl = sorted(names - set(params), key=natural)
    head = [['comment', 'control flow not structured (%s): %d basic blocks as a state machine' % (why, len(order))]]
    for i in range(0, len(decl), 12):
        head.append(['local', decl[i:i + 12], []])
    return head + [
            ['local', ['state'], [('k', num[st.cfg.entry])]],
            ['while', ('k', True), body, None]]


class Decompiler:
    def __init__(self, protos, root_key, native_text=None):
        self.protos = protos
        self.root_key = root_key
        self.namer = Namer()
        self.native_text = native_text
        self.stats = {'protos': 0, 'unstructured': 0, 'warnings': []}
        self.done = {}

    def proto_ast(self, key):
        """(params, vararg, ast)"""
        pir = self.protos[key]
        for coalesce in (False, True):
            cfg = ProtoCFG(pir)
            params = assign_variables(cfg, self.namer, coalesce)
            st = Structurer(cfg)
            try:
                ast = st.run()
            except (Unstructured, RecursionError) as ex:
                if not FLAT_FALLBACK:
                    raise
                # control flow that does not reduce to loops and ifs: one arm per basic block
                self.stats['flat'] = self.stats.get('flat', 0) + 1
                ast = flat_ast(Structurer(cfg), str(ex), params)
                break
            ast = cleanup(attach_preps(ast))
            ast = beautify.beautify(ast)
            declare(ast, params, st.live_in)
            n0 = max_locals(ast) + len(params)
            if n0 <= 180:
                break
            # too many locals for one Luau function: first try do-blocks around the short-lived
            # ones (every variable keeps its own name); sharing variables is the last resort
            if not (ircfg_mod.DROP_OPAQUE and section_scopes(ast) and max_locals(ast) + len(params) <= 195):
                tighten_scopes(ast)
            if os.environ.get('DEVM_TRACE_LOCALS'):
                print('locals', key, 'coalesce' if coalesce else 'plain', n0, '->', max_locals(ast) + len(params))
            if max_locals(ast) + len(params) <= int(os.environ.get('DEVM_LOCALS_MAX', '195')):
                break
        self.stats['protos'] += 1
        self.stats['warnings'].extend(cfg.warnings)
        return params, cfg.vararg, ast

    def closure_text(self, e, printer, name=None):
        key = e[1]
        caps = e[2]
        upnames = []
        for c in caps:
            if c[0] in ('ref', 'val'):
                upnames.append(c[1][1] if isinstance(c[1], tuple) else 'REG_%s' % (c[1],))
            elif c[0] == 'const' and hasattr(c[1], 'text'):
                upnames.append(c[1].text)
            elif c[0] == 'const' and hasattr(c[1], 'name') and not isinstance(c[1], (str, int, float, bool)):
                upnames.append(c[1].name)
            elif c[0] == 'const':
                upnames.append(lua_number(c[1]) if isinstance(c[1], (int, float)) and not isinstance(c[1], bool) else ('true' if c[1] is True else 'false' if c[1] is False else lua_string(c[1])))
            else:
                upnames.append(printer.upnames[c[1]] if c[1] < len(printer.upnames) else 'UPVAL_%d' % c[1])
        if isinstance(key, tuple) and key[0] == 'native':
            body = self.native_text(key[1], upnames, printer.level) if self.native_text else None
            if body is None:
                return 'function(...) --[[ native function L%d ]] end' % key[1]
            return body if name is None else body.replace('function', 'function ' + name, 1)
        try:
            params, vararg, ast = self.proto_ast(key)
        except Unstructured as ex:
            self.stats['unstructured'] += 1
            return 'function(...) --[[ UNSTRUCTURED: %s ]] end' % ex
        p = Printer(self, upnames)
        p.level = printer.level
        plist = list(params) + (['...'] if vararg else [])
        head = 'function%s(%s)' % (' ' + name if name else '', ', '.join(plist))
        if os.environ.get('DEVM_PROTO_IDS'):
            head += ' -- proto %s' % (key,)
        p.block(ast)
        if len(p.lines) > 4000 and sum(1 for ln in p.lines if re.match(r'\s*(local exit\d+|if exit\d+ then|exit\d+ = true)', ln)) * 20 > len(p.lines):
            # control flow that could not be untangled: thousands of lines of flag juggling help nobody
            self.stats['unstructured'] += 1
            return 'function(...) --[[ UNSTRUCTURED: %d lines of unrecovered control flow omitted ]] end' % len(p.lines)
        return head + '\n' + '\n'.join(p.lines) + '\n' + '\t' * printer.level + 'end'

    def run(self):
        if ircfg_mod.DROP_OPAQUE:
            ircfg_prepass(self.protos)
        params, vararg, ast = self.proto_ast(self.root_key)
        p = Printer(self, [])
        p.level = -1
        p.block(ast)
        return '\n'.join(p.lines) + '\n'
