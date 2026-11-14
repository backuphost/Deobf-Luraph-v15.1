"""Drives the partial evaluator over every instruction of every proto -> per-proto IR graph."""
from . import pe as P
from .pe import (PE, Scope, LFn, LuaTable, MultiTail, Sym, LinSym, NeedFork, ForOp, LoopHead, PEError,
                 REGS, UPVALS)


class Node:
    __slots__ = ('key', 'tree', 'raw_tree', 'exposed', 'written', 'snap_in', 'error', 'kdeps', 'kin', 'parent', 'kill', 'uses')

    def __init__(self, key):
        self.key = key
        self.tree = None
        self.exposed = set()
        self.written = set()
        self.snap_in = None
        self.error = None
        self.raw_tree = None
        self.kdeps = None
        self.kin = None
        self.parent = None
        self.kill = None
        self.uses = None


class ProtoIR:
    def __init__(self, key):
        self.key = key
        self.entry = None
        self.nodes = {}
        self.children = []
        self.conflicts = []
        self.errors = []
        self.cjumps = {}
        self.succ = {}          # instruction -> successor instructions seen under any state
        self.mono = None
        self.kconf = set()
        self.taken = set()      # (node key, leaf index) reached under some feasible state


def same_value(a, b):
    if a is b:
        return True
    if isinstance(a, Sym) and isinstance(b, Sym):
        return a.e == b.e
    if type(a) != type(b):
        return False
    if isinstance(a, (int, float, str, bool)):
        return a == b
    if isinstance(a, LinSym):
        return a == b
    if isinstance(a, (P.UpRef, P.UpBox, P.UpKey)):
        return a.n == b.n
    return False


class Lifter:
    def __init__(self, model, dump, log=None):
        self.model = model
        self.dump = dump
        self.pe = PE(model, dump)
        self.log = log or (lambda *a: None)
        self.protos = {}
        self.queue = []
        self.proto_objs = {}
        self.pe.on_closure = self.on_closure
        self.pe.capkinds = {}
        self.pe.cur_proto = None

    # ---- proto instantiation (family specific)
    def on_closure(self, proto):
        key = self.proto_key(proto)
        if key not in self.proto_objs:
            self.proto_objs[key] = proto
            self.queue.append(key)
        return key

    def proto_key(self, proto):
        if isinstance(proto, LuaTable):
            return proto.id
        if isinstance(proto, P.ProtoRef):
            return proto.pid
        raise PEError('unknown proto object %r' % (proto,))

    def instantiate(self, key):
        raise NotImplementedError

    # ---- running one instruction
    def run_once(self, interp, inject, decisions, perm, prefix):
        pe = self.pe
        pe.ir = []
        pe.tcount = 0
        pe.tprefix = prefix
        pe.events = []
        pe.pending_closures = []
        pe.path = []
        pe.dpos = 0
        pe.decisions = decisions
        pe.perm = perm
        pe.va_base = None
        pe.inject = inject
        pe.seeking = True
        pe.track = False
        pe.exposed = set()
        pe.written = set()
        pe.kcur = None
        pe.kpos = 0
        pe.tv = {}
        try:
            vals = pe.call_lfn(interp, [MultiTail('args')], -1)
            tail = None
            if vals and isinstance(vals[-1], MultiTail):
                tail = vals[-1].id
                vals = vals[:-1]
            end = ('ret', [pe.atom(v) for v in vals], tail)
        except LoopHead as lh:
            pcv = pe.pcvars[id(self.model.loops[lh.loop])]
            end = ('goto', (lh.loop, lh.snapshot[pcv]), lh.snapshot)
        finally:
            pe.track = False
        pe.finish_closures()
        return list(pe.ir), list(pe.path), end, list(pe.events), set(pe.exposed), set(pe.written)

    def run_node(self, interp, key, snap):
        prefix = 'L%dP%d' % key if key[0] else 'P%d' % key[1]
        paths = []
        todo = [[]]
        exposed, written = set(), set()
        while todo:
            d = todo.pop()
            try:
                ir, path, end, ev, ex, wr = self.run_once(interp, (key[0], snap), d, None, prefix)
            except NeedFork:
                todo.append(d + [False])
                todo.append(d + [True])
                if len(d) > 12:
                    raise PEError('too many symbolic branches')
                continue
            except ForOp:
                return self.run_forop(interp, key, snap, prefix)
            exposed |= ex
            written = set(wr) if not paths else (written & wr)
            paths.append((ir, path, end))
        return self.build_tree(paths, 0, 0), exposed, written

    def build_tree(self, paths, depth, start):
        if len(paths) == 1 and len(paths[0][1]) == depth:
            ir, path, end = paths[0]
            return (ir[start:], end)
        if any(len(p[1]) <= depth for p in paths):
            raise PEError('inconsistent fork replay (instruction changed VM state between runs)')
        idx, cond, _ = paths[0][1][depth]
        tp = [p for p in paths if p[1][depth][2]]
        fp = [p for p in paths if not p[1][depth][2]]
        for p in paths:
            if p[1][depth][0] != idx:
                raise PEError('inconsistent fork replay')
        prefix = paths[0][0][start:idx]
        return (prefix, ('br', cond, self.build_tree(tp, depth + 1, idx), self.build_tree(fp, depth + 1, idx)))

    def run_forop(self, interp, key, snap, prefix):
        irT, _, endT, evT, ex, wr = self.run_once(interp, (key[0], snap), [], True, prefix)
        irF, _, endF, evF, _, _ = self.run_once(interp, (key[0], snap), [], False, prefix)
        kinds = {e[0] for e in evT}
        reads = [s[2][1] for s in irT if s[0] == 'set' and s[2][0] == 'r']
        writes = [s[1][1] for s in irT if s[0] == 'set' and s[1][0] == 'r']
        if endT[0] != 'goto':
            raise PEError('for-op ended with %r' % (endT[0],))
        if 'push' in kinds and 'wrap' in kinds:
            return (([('genforprep', min(reads))], endT)), ex, wr
        if 'push' in kinds:
            return (([('numforprep', min(reads))], endT)), ex, wr
        if 'fop' in kinds:
            if endF[0] != 'goto' or not writes:
                raise PEError('bad numeric for loop op')
            return (([], ('forbr', 'num', min(writes) - 3, ([], endT), ([], endF)))), ex, wr
        if 'fcall' in kinds:
            if endF[0] != 'goto' or not writes:
                raise PEError('bad generic for loop op')
            return (([], ('forbr', 'gen', tuple(sorted(writes)), ([], endT), ([], endF)))), ex, wr
        if endF[0] != 'goto' or endF[1] != endT[1]:
            raise PEError('unrecognised for-state op')
        return (([('forpop',)], endT)), ex, wr

    # ---- whole proto
    STATE_LIMIT = 600       # distinct constant states per instruction before generalising
    FOR_UNROLL = 64         # constant state loops with at most this many iterations are followed

    def lift_proto(self, key):
        pir = ProtoIR(key)
        self.pe.cur_proto = key
        interp = self.instantiate(key)
        self.pe.kregs = {}
        _, _, end, _, _, _ = self.run_once(interp, None, [], None, 'entry')
        if end[0] != 'goto':
            raise PEError('interpreter returned before reaching dispatch loop')
        entry, snap = end[1], end[2]
        pir.entry = entry
        nq0 = len(self.queue)
        # need[n]: registers whose constant value decides something at instruction n (a folded
        # branch, a computed jump, a decryption key, a constant for loop); rel[n]: need propagated
        # backwards = what a visit must distinguish.  Iterate until the explored graph and the
        # relevance sets are stable.
        self.need = {}
        self.rel = {}
        self.generalized = set()
        self.preds = {}
        self.merge = {}         # for-loop head -> (first body instruction, loop variable register)
        self.inv = {}
        for _ in range(300):
            nn = len(pir.nodes)
            ng = len(self.generalized)
            self.rel_changed = False
            self.walk(pir, interp, entry, snap)
            if not self.rel_changed and len(pir.nodes) == nn and len(self.generalized) == ng:
                break
        pir.children = self.queue[nq0:]
        self.check_conflicts(pir)
        self.specialise(pir, entry)
        return pir

    def enter(self, key, kregs):
        """(signature, constants) for arriving at instruction `key` with constants `kregs`."""
        rel = self.rel.get(key, ())
        if rel and self.is_merge(key, kregs):
            # head of a real for loop: its hidden loop state cannot be duplicated, so all arrivals
            # share one copy that keeps only the constants they agree on
            inv = self.inv.get(key, {})
            kregs = dict(kregs)
            for r in rel:
                if r in kregs and (r not in inv or inv[r] != kregs[r]):
                    del kregs[r]
        if key in self.generalized and rel:
            kregs = dict(kregs)
            for r in rel:
                if r == FORSTK:
                    if FORSTK in kregs:
                        kregs[FORSTK] = tuple(None for _ in kregs[FORSTK])
                else:
                    kregs.pop(r, None)
        return frozenset((r, kregs[r]) for r in rel if r in kregs), kregs

    def is_merge(self, key, kregs):
        info = self.merge.get(key)
        if info is None:
            return False
        if info[1] is None or info[1] not in self.rel.get(info[0], ()):
            return True
        # the body branches on the loop variable: a state loop, followed iteration by iteration
        # (not merged) whenever this arrival knows its bounds
        stk = kregs.get(FORSTK, ())
        top = stk[-1] if stk else None
        return not (top is not None and top[3] + 3 == info[1])

    def walk(self, pir, interp, entry, snap):
        """Explore the proto's control flow, lifting each instruction on first reach.

        Constants are tracked along every path and branches they decide are followed one way
        only, which is what keeps the walk on feasible paths: the VM decrypts its bytecode as it
        runs, so executing an arm the real program never takes would corrupt later code."""
        visited = set()
        work = [(entry, snap, {}, None)]
        confl = set()
        visits = {}
        while work:
            key2, snap, kregs, parent = work.pop()
            if self.is_merge(key2, kregs):
                inv = self.inv.get(key2)
                if inv is None:
                    self.inv[key2] = dict(kregs)
                else:
                    drop = [r for r, v in inv.items() if r not in kregs or kregs[r] != v]
                    if drop:
                        for r in drop:
                            del inv[r]
                        self.rel_changed = True
            sig, kregs = self.enter(key2, kregs)
            if (key2, sig) in visited:
                continue
            visited.add((key2, sig))
            visits[key2] = visits.get(key2, 0) + 1
            if visits[key2] > self.STATE_LIMIT:
                self.generalized.add(key2)
            node = pir.nodes.get(key2)
            if node is None:
                node = Node(key2)
                node.snap_in = snap
                node.kin = kregs
                node.parent = parent
                pir.nodes[key2] = node
                pe = self.pe
                pe.kregs = kregs
                pe.kused = {}
                try:
                    node.tree, node.exposed, node.written = self.run_node(interp, key2, snap)
                    tries = 0
                    while self.is_self_rewrite(node.tree, key2) and tries < 16:
                        # the instruction decrypted itself in place and asked to be executed again
                        tries += 1
                        node.tree, node.exposed, node.written = self.run_node(interp, key2, snap)
                except PEError as e:
                    node.error = str(e)
                    node.tree = ([('error', str(e))], ('ret', [], None))
                    node.raw_tree = node.tree
                    pir.errors.append((key2, str(e)))
                    continue
                node.kdeps = dict(pe.kused)
                node.raw_tree = node.tree
            else:
                if key2 not in confl:
                    for name, v in node.snap_in.items():
                        if name in snap and not same_value(v, snap[name]):
                            confl.add(key2)
                            pir.conflicts.append((key2, name, v, snap[name]))
                for r, v in (node.kdeps or {}).items():
                    if kregs.get(r, v) != v and (key2, r) not in pir.kconf:
                        pir.kconf.add((key2, r))
                        pir.errors.append((key2, 'path-dependent VM state: R[%s] is %r or %r' % (r, v, kregs.get(r))))
                if node.error:
                    continue
            deps = set()
            succ = pir.succ.setdefault(key2, set())

            def leaf(li, regs2, path, term, td, key2=key2, succ=succ, deps=deps):
                if term[0] == 'goto':
                    tkey, snap2 = self.resolve_goto(path, term, regs2, td, deps)
                    if tkey is not None:
                        if tkey not in succ:
                            succ.add(tkey)
                            self.preds.setdefault(tkey, []).append(key2)
                            have = self.rel.get(tkey)
                            if have:
                                kl = self.kill_of(node)
                                rest = have - kl
                                for r in have & kl:
                                    rest = rest | node.uses.get(r, frozenset())
                                self.add_relevant(pir, key2, rest)
                        work.append((tkey, snap2, regs2, key2))
                return [], term

            folded = fold_tree(node.raw_tree, kregs, deps, leaf, Unroll(self.rel, self.FOR_UNROLL, True))
            info = forbr_info(folded)
            if info is not None and key2 not in self.merge:
                self.merge[key2] = info
                self.inv[key2] = dict(kregs)
                self.rel_changed = True
            if node.kdeps:
                deps.update(node.kdeps)
            if deps:
                self.add_relevant(pir, key2, deps)

    @staticmethod
    def kill_of(n):
        """Path-state entries the instruction always overwrites.  n.uses[x] says what the new
        value of x is computed from, so relevance can continue through the computation."""
        kl = n.kill
        if kl is None:
            kl = set()
            uses = {}
            td = {}
            for st in n.raw_tree[0]:
                if st[0] == 'set':
                    e = st[2]
                    deps = P.k_deps(e, td, set()) if isinstance(e, tuple) else set()
                    if st[1][0] == 't':
                        td[st[1]] = frozenset(deps)
                        if isinstance(e, tuple) and e[0] == 'bin' and e[1] in ('+', '-'):
                            td.setdefault(P.ADDITIVE, set()).add(st[1])
                        kl.add(st[1])
                    elif st[1][0] == 'r':
                        r = st[1][1]
                        if isinstance(e, tuple) and P.is_counter_update(r, e, td):
                            deps = set()
                        kl.add(r)
                        uses[r] = frozenset(deps)
                elif st[0] == 'call':
                    deps = set()
                    for a in st[3]:
                        P.k_deps(a, td, deps)
                    td[('mv', st[1], 1)] = frozenset(deps)
                    kl.add(('mv', st[1], 1))
            n.uses = uses
            kl = n.kill = frozenset(kl)
        return kl

    def add_relevant(self, pir, key, regs):
        """rel[key] |= regs, propagated backwards over the edges known so far."""
        work = [(key, regs)]
        while work:
            k, add = work.pop()
            have = self.rel.get(k)
            if have is None:
                have = self.rel[k] = set()
            add = add - have
            if not add:
                continue
            have |= add
            self.rel_changed = True
            for p in self.preds.get(k, ()):
                n = pir.nodes.get(p)
                if n is not None and n.raw_tree is not None:
                    kl = self.kill_of(n)
                    rest = add - kl
                    for r in add & kl:
                        rest = rest | n.uses.get(r, frozenset())
                    if rest:
                        work.append((p, rest))

    def resolve_goto(self, path, term, regs2, td, deps):
        """Target of a leaf goto; computed jumps (pc = f(register)) are resolved from constants."""
        tgt = term[1][1]
        if isinstance(tgt, int):
            return term[1], term[2]
        val = regs2.get(tgt.e, P.NOTK) if isinstance(tgt, Sym) else P.NOTK
        if not isinstance(val, int) or isinstance(val, bool):
            return None, None
        deps |= td.get(tgt.e, ())
        regs2.pop(tgt.e, None)
        info = computed_expr(path, tgt.e)
        if info is not None and info[0] in regs2:
            deps.add(info[0])
            del regs2[info[0]]      # the return address is consumed by the jump
        pcv = self.pe.pcvars[id(self.model.loops[term[1][0]])]
        snap2 = dict(term[2])
        snap2[pcv] = val
        return (term[1][0], val), snap2

    def specialise(self, pir, entry):
        """Replace the per-instruction graph by one node per (instruction, relevant constants).

        Every copy keeps only the arms its constants allow, so state-machine dispatchers, jump
        trampolines and constant-bound "for" state loops dissolve into direct control flow."""
        mono = pir.nodes
        pir.mono = mono
        ids = {}
        counts = {}
        nodes = {}
        work = []

        def state(key, kregs):
            sig, kregs = self.enter(key, kregs)
            sk = ids.get((key, sig))
            if sk is None:
                n = counts.get(key, 0)
                counts[key] = n + 1
                sk = ids[(key, sig)] = (key[0], key[1], n)
                work.append((sk, key, kregs))
            return sk

        pir.entry = state(entry, {})
        while work:
            sk, key, kregs = work.pop()
            m = mono.get(key)
            node = Node(sk)
            nodes[sk] = node
            if m is None:
                msg = 'instruction %s:%s was never lifted' % key
                node.error = msg
                node.tree = ([('error', msg)], ('ret', [], None))
                pir.errors.append((sk, msg))
                continue
            node.snap_in, node.exposed, node.written, node.error = m.snap_in, m.exposed, m.written, m.error
            node.kin = kregs
            if m.error:
                node.tree = m.tree
                continue

            def leaf(li, regs2, path, term, td):
                if term[0] != 'goto':
                    return [], term
                tkey, _ = self.resolve_goto(path, term, regs2, td, set())
                if tkey is None:
                    msg = 'computed jump with no known target'
                    pir.errors.append((sk, msg))
                    return [('error', msg)], ('ret', [], None)
                return [], ('goto', state(tkey, regs2))

            tree = fold_tree(m.raw_tree, kregs, set(), leaf, Unroll(self.rel, self.FOR_UNROLL, False))
            if sk[2]:
                tree = rename_temps(tree, '@%d' % sk[2])
            node.tree = tree
        # setups of loops that were followed iteration by iteration have no loop left to belong to
        kept = set()
        for n in nodes.values():
            for stmts, term in _subtrees(n.tree):
                if term[0] == 'forbr' and term[1] == 'num':
                    kept.add(term[2])
        for n in nodes.values():
            n.tree = _filter_stmts(n.tree, lambda st: not (st[0] == 'numforprep' and isinstance(st[1], int)
                                                           and st[1] not in kept))
        pir.nodes = nodes

    @staticmethod
    def is_self_rewrite(tree, key):
        stmts, term = tree
        return not stmts and term[0] == 'goto' and term[1] == key

    def check_conflicts(self, pir):
        """Keep only conflicts on VM temporaries that are actually live at the join."""
        if not pir.conflicts:
            return
        names = {c[1] for c in pir.conflicts}
        self.live = None
        succ = {k: list(pir.succ.get(k, ())) for k in pir.nodes}
        live = {k: set(n.exposed) & names for k, n in pir.nodes.items()}
        changed = True
        while changed:
            changed = False
            for k, n in pir.nodes.items():
                out = set()
                for s2 in succ[k]:
                    out |= live.get(s2, set())
                new = (n.exposed & names) | (out - n.written)
                if new != live[k]:
                    live[k] = new
                    changed = True
        pir.conflicts = [c for c in pir.conflicts if c[1] in live[c[0]]]
        pir.live = live

    def lift_all(self, root_key):
        self.queue.append(root_key)
        done = set()
        while self.queue:
            k = self.queue.pop(0)
            if k in done:
                continue
            done.add(k)
            self.protos[k] = self.lift_proto(k)
        return self.protos


def tree_ends(tree):
    stmts, term = tree
    if term[0] in ('br',):
        yield from tree_ends(term[2])
        yield from tree_ends(term[3])
    elif term[0] == 'forbr':
        yield from tree_ends(term[3])
        yield from tree_ends(term[4])
    else:
        yield term


class TableProtoLifter(Lifter):
    """Family 1: wrap(proto_table, upvalues) -> function(...)"""

    def __init__(self, model, dump, log=None):
        super().__init__(model, dump, log)
        self.wrap = model.chain[-2]
        self.pe.wrap_func = self.wrap
        self.base = Scope()
        for k, v in dump.root_vars().items():
            self.base.vars[k] = v
        # which wrap parameter is the proto?
        counts = {}
        for st in self.wrap.body:
            if st[0] == 'local' and len(st[2]) == 1 and st[2][0][0] == 'index' and st[2][0][1][0] == 'name':
                counts[st[2][0][1][1]] = counts.get(st[2][0][1][1], 0) + 1
        self.proto_param = max(counts, key=counts.get)
        self.root_key = self.base.vars[self.proto_param].id
        self.proto_objs[self.root_key] = self.base.vars[self.proto_param]

    def instantiate(self, key):
        proto = self.proto_objs[key]
        pe = self.pe
        pe.seeking = False
        pe.perm = None
        pe.ir = []
        args = []
        for p in self.wrap.params:
            if p == self.proto_param:
                args.append(proto)
            elif p == '_':
                args.append(None)
            else:
                args.append(UPVALS)
        sc = Scope(self.base)
        for p, a in zip(self.wrap.params, args):
            sc.vars[p] = a
        r = pe.exec_block(self.wrap.body, sc)
        if not r or r[0] != 'return' or not isinstance(r[1][0], LFn):
            raise PEError('wrap did not return the interpreter closure')
        return r[1][0]


class FactoryLifter(Lifter):
    """Family 2: factory(proto fields...) -> function(?, upvalues, ...) -> function(...)

    Every proto is the closure returned by the factory; child protos are created by the VM's own
    loader code, so they are discovered while lifting rather than read from the dump."""

    def __init__(self, model, dump, log=None):
        super().__init__(model, dump, log)
        self.outer = model.chain[-3]
        self.mid = model.chain[-2]
        pe = self.pe
        pe.factory = (self.outer, self.mid)
        self.keys = {}          # id(mid closure scope) -> key
        self.key_objs = {}
        self.pid_lfn = {}
        self.envs = dump.proto_envs()
        if not self.envs:
            raise PEError('no proto instantiation recorded')
        root = self.mid_for_pid(1)
        self.root_key = self.proto_key(root)
        self.proto_objs[self.root_key] = root

    def mid_for_pid(self, pid):
        f = self.pid_lfn.get(pid)
        if f is None:
            env = self.envs[pid - 1]
            args = [env.get(p) if p != '_' else None for p in self.outer.params]
            pe = self.pe
            pe.seeking = False
            pe.perm = None
            pe.ir = []
            r = pe.call_lfn(LFn(self.outer, pe.global_scope), args, 1)
            if not r or not isinstance(r[0], LFn) or r[0].func is not self.mid:
                raise PEError('factory did not return a proto closure')
            f = self.pid_lfn[pid] = r[0]
        return f

    def proto_key(self, proto):
        if isinstance(proto, P.ProtoRef):
            proto = self.mid_for_pid(proto.pid)
        if isinstance(proto, LFn) and proto.func is self.mid:
            k = self.keys.get(id(proto.scope))
            if k is None:
                k = self.keys[id(proto.scope)] = len(self.keys) + 1
                self.key_objs[k] = proto      # keeps the scope alive, so id() stays unique
            return k
        raise PEError('unknown proto object %r' % (proto,))

    def on_closure(self, proto):
        if isinstance(proto, P.ProtoRef):
            proto = self.mid_for_pid(proto.pid)
        return super().on_closure(proto)

    def instantiate(self, key):
        proto = self.proto_objs[key]
        pe = self.pe
        pe.seeking = False
        pe.perm = None
        pe.ir = []
        args = [None] * len(self.mid.params)
        # the upvalue table is the parameter the interpreter indexes
        args[self.upvalue_param()] = UPVALS
        pe.instantiating = True
        try:
            r = pe.call_lfn(proto, args, 1)
        finally:
            pe.instantiating = False
        if not r or not isinstance(r[0], LFn):
            raise PEError('proto closure did not return the interpreter')
        return r[0]

    def upvalue_param(self):
        named = [i for i, p in enumerate(self.mid.params) if p != '_']
        if len(named) == 1:
            return named[0]
        return 1 if len(self.mid.params) > 1 else 0


def tree_paths(tree, prefix=()):
    """(statements along the path, terminator) for every leaf of a node tree."""
    stmts, term = tree
    cur = list(prefix) + list(stmts)
    if term[0] == 'br':
        yield from tree_paths(term[2], cur)
        yield from tree_paths(term[3], cur)
    elif term[0] == 'forbr':
        regs = (term[2] + 3,) if term[1] == 'num' else tuple(term[2])
        cur2 = cur + [('clobber', regs)]
        yield from tree_paths(term[3], cur2)
        yield from tree_paths(term[4], cur2)
    else:
        yield cur, term


def computed_expr(stmts, target):
    """Express a symbolic jump target as an expression over exactly one register."""
    temps = {}
    written = set()
    for st in stmts:
        if st[0] == 'set':
            if st[1][0] == 't':
                temps[st[1]] = st[2]
            elif st[1][0] == 'r':
                written.add(st[1][1])
    regs = set()

    def expand(e, depth=0):
        if depth > 50:
            return None
        if e[0] == 't':
            if e not in temps:
                return None
            return expand(temps[e], depth + 1)
        if e[0] == 'r':
            regs.add(e[1])
            return e
        if e[0] == 'k':
            return e
        if e[0] == 'bin':
            a, b = expand(e[2], depth + 1), expand(e[3], depth + 1)
            if a is None or b is None:
                return None
            return ('bin', e[1], a, b)
        return None

    expr = expand(target)
    if expr is None or len(regs) != 1:
        return None
    reg = next(iter(regs))
    if reg in written:
        return None
    return reg, expr


def _map_leaves(tree, fn):
    """Rebuild a node tree, replacing each leaf terminator by fn(term) -> (stmts, term)."""
    stmts, term = tree
    if term[0] == 'br':
        return (stmts, ('br', term[1], _map_leaves(term[2], fn), _map_leaves(term[3], fn)))
    if term[0] == 'forbr':
        return (stmts, ('forbr', term[1], term[2], _map_leaves(term[3], fn), _map_leaves(term[4], fn)))
    extra, t2 = fn(term)
    return (list(stmts) + list(extra), t2)


FORSTK = '__for'    # pseudo register: stack of numeric-for frames (idx, limit, step, A) / None


class Unroll:
    """Policy for constant-bound numeric for loops.

    Luraph turns straight-line code into `for s = a, b, c do if s == ... end` state loops; those
    are followed iteration by iteration.  A loop counts as a state loop when its body branches on
    the loop variable, which is known once the body has been explored (rel)."""

    def __init__(self, rel, limit, probing):
        self.rel = rel
        self.limit = limit
        self.probing = probing

    def is_state_loop(self, body_key, reg):
        return body_key is not None and reg in self.rel.get(body_key, ())


def fold_tree(tree, regs, deps, leaf, unroll, prefix=None, counter=None, tv=None, td=None):
    """Specialise a node tree to the constants in `regs`.

    Branches the constants decide are replaced by the arm taken (their dependencies go to
    `deps`; deps=None disables folding).  leaf(index, regs, path, term, td) -> (stmts, term) is
    called for every remaining leaf; leaf indices count all leaves of the unfolded tree.
    Temporaries are local to the instruction except the ones its snapshot hands to the next
    instruction; those travel in `regs` under their own name."""
    if counter is None:
        counter = [-1]
        prefix = []
        tv = {}
        td = {}
    stmts, term = tree
    regs = dict(regs)
    out = []
    for st in stmts:
        t = st[0]
        if t == 'numforprep' and isinstance(st[1], int):
            a = st[1]
            frame = None
            init, limit, step = regs.get(a, P.NOTK), regs.get(a + 1, P.NOTK), regs.get(a + 2, P.NOTK)
            if deps is not None and all(P.is_num(x) for x in (init, limit, step)) and step != 0:
                trips = (limit - init) / step + 1
                if trips <= unroll.limit:
                    frame = (init - step, limit, step, a)
                    deps.update((a, a + 1, a + 2))
            regs[FORSTK] = regs.get(FORSTK, ()) + (frame,)
        elif t == 'genforprep':
            regs[FORSTK] = regs.get(FORSTK, ()) + (None,)
        elif t == 'forpop':
            stk = regs.get(FORSTK, ())
            if stk:
                regs[FORSTK] = stk[:-1]
        else:
            P.k_transfer((st,), regs, tv, None, td)
        out.append(st)
    cur = prefix + out
    if term[0] == 'br':
        c = P.k_eval(term[1], regs, tv) if deps is not None else P.NOTK
        if c is P.NOTK:
            a = fold_tree(term[2], regs, deps, leaf, unroll, cur, counter, dict(tv), dict(td))
            b = fold_tree(term[3], regs, deps, leaf, unroll, cur, counter, tv, td)
            return (out, ('br', term[1], a, b))
        P.k_deps(term[1], td, deps, regs)
        if P.truthy(c):
            sub = fold_tree(term[2], regs, deps, leaf, unroll, cur, counter, tv, td)
            counter[0] += count_leaves(term[3])
        else:
            counter[0] += count_leaves(term[2])
            sub = fold_tree(term[3], regs, deps, leaf, unroll, cur, counter, tv, td)
        return (out + list(sub[0]), sub[1])
    if term[0] == 'forbr':
        if term[1] == 'num':
            a = term[2]
            stk = regs.get(FORSTK, ())
            top = stk[-1] if stk else None
            tgt = term[3][1][1] if term[3][1][0] == 'goto' else None
            fake = top is not None and top[3] == a and deps is not None and unroll.is_state_loop(tgt, a + 3)
            if top is not None and top[3] == a and deps is not None and not fake and unroll.probing:
                # first look at a constant loop: run the body once with the first value to see
                # whether it branches on the loop variable (a state loop) or just uses it
                idx = P.norm(top[0] + top[2])
                regs2 = dict(regs)
                regs2[FORSTK] = stk[:-1] + (None,)
                regs2[a + 3] = idx
                cur2 = cur + [('clobber', (a + 3,))]
                x = fold_tree(term[3], regs2, deps, leaf, unroll, cur2, counter, dict(tv), dict(td))
                regs[FORSTK] = stk[:-1] + (None,)
                regs.pop(a + 3, None)
                y = fold_tree(term[4], regs, deps, leaf, unroll, cur2, counter, tv, td)
                return (out, ('forbr', term[1], term[2], x, y))
            if fake:
                deps.add(FORSTK)
                idx = P.norm(top[0] + top[2])
                if (idx <= top[1]) if top[2] > 0 else (idx >= top[1]):
                    regs[FORSTK] = stk[:-1] + ((idx, top[1], top[2], a),)
                    regs[a + 3] = idx
                    st = ('set', ('r', a + 3), ('k', idx))
                    sub = fold_tree(term[3], regs, deps, leaf, unroll, cur + [st], counter, tv, td)
                    counter[0] += count_leaves(term[4])
                    return (out + [st] + list(sub[0]), sub[1])
                counter[0] += count_leaves(term[3])
                sub = fold_tree(term[4], regs, deps, leaf, unroll, cur, counter, tv, td)
                return (out + list(sub[0]), sub[1])
            clob = (a + 3,)
        else:
            clob = tuple(term[2])
        for r in clob:
            regs.pop(r, None)
        cur2 = cur + [('clobber', clob)]
        x = fold_tree(term[3], regs, deps, leaf, unroll, cur2, counter, dict(tv), dict(td))
        y = fold_tree(term[4], regs, deps, leaf, unroll, cur2, counter, tv, td)
        return (out, ('forbr', term[1], term[2], x, y))
    counter[0] += 1
    # hand constant temporaries to the next instruction, drop the ones that end here
    live = {}
    if term[0] == 'goto' and len(term) > 2 and isinstance(term[2], dict):
        for v in term[2].values():
            if isinstance(v, Sym):
                c = P.k_eval(v.e, regs, tv)
                if c is not P.NOTK:
                    live[v.e] = c
                    if v.e not in regs:
                        td.setdefault(v.e, frozenset())
                    else:
                        td[v.e] = frozenset((v.e,))
    for k in [k for k in regs if isinstance(k, tuple)]:
        del regs[k]
    regs.update(live)
    extra, t2 = leaf(counter[0], regs, cur, term, td)
    return (out + list(extra), t2)


_TEMP_ID = None


def rename_temps(tree, suffix):
    """Give the temporaries / call ids of a duplicated node tree their own names."""
    import re
    global _TEMP_ID
    if _TEMP_ID is None:
        _TEMP_ID = re.compile(r'^(L\d+)?P\d+_c?\d+$|^entry_c?\d+$')

    def ren(x):
        if isinstance(x, str):
            return x + suffix if _TEMP_ID.match(x) else x
        if isinstance(x, tuple):
            if x and x[0] == 'k':
                return x
            if x and x[0] == 'goto':
                return x
            return tuple(ren(y) for y in x)
        if isinstance(x, list):
            return [ren(y) for y in x]
        return x

    return ren(tree)


def _subtrees(tree):
    yield tree
    term = tree[1]
    if term[0] == 'br':
        yield from _subtrees(term[2])
        yield from _subtrees(term[3])
    elif term[0] == 'forbr':
        yield from _subtrees(term[3])
        yield from _subtrees(term[4])


def _filter_stmts(tree, keep):
    stmts, term = tree
    stmts = [st for st in stmts if keep(st)]
    if term[0] == 'br':
        term = ('br', term[1], _filter_stmts(term[2], keep), _filter_stmts(term[3], keep))
    elif term[0] == 'forbr':
        term = ('forbr', term[1], term[2], _filter_stmts(term[3], keep), _filter_stmts(term[4], keep))
    return (stmts, term)


def forbr_info(tree):
    """(body target, loop variable register or None) of the for-loop test left in a tree."""
    term = tree[1]
    if term[0] == 'forbr':
        tgt = term[3][1][1] if term[3][1][0] == 'goto' else None
        return (tgt, term[2] + 3 if term[1] == 'num' else None)
    if term[0] == 'br':
        return forbr_info(term[2]) or forbr_info(term[3])
    return None


def count_leaves(tree):
    term = tree[1]
    if term[0] == 'br':
        return count_leaves(term[2]) + count_leaves(term[3])
    if term[0] == 'forbr':
        return count_leaves(term[3]) + count_leaves(term[4])
    return 1
