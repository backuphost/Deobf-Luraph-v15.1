"""Drives the partial evaluator over every instruction of every proto -> per-proto IR graph."""
from . import pe as P
from .pe import (PE, Scope, LFn, LuaTable, MultiTail, Sym, LinSym, NeedFork, ForOp, LoopHead, PEError,
                 REGS, UPVALS)


class Node:
    __slots__ = ('key', 'tree', 'raw_tree', 'exposed', 'written', 'snap_in', 'error', 'kdeps')

    def __init__(self, key):
        self.key = key
        self.tree = None
        self.exposed = set()
        self.written = set()
        self.snap_in = None
        self.error = None
        self.raw_tree = None
        self.kdeps = None


class ProtoIR:
    def __init__(self, key):
        self.key = key
        self.entry = None
        self.nodes = {}
        self.children = []
        self.conflicts = []
        self.errors = []
        self.cjumps = {}
        self.tv = {}
        self.kconf = set()


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
    def lift_proto(self, key):
        pir = ProtoIR(key)
        self.pe.cur_proto = key
        interp = self.instantiate(key)
        self.pe.kregs = {}
        self.pe.tv = pir.tv
        _, _, end, _, _, _ = self.run_once(interp, None, [], None, 'entry')
        if end[0] != 'goto':
            raise PEError('interpreter returned before reaching dispatch loop')
        entry, snap = end[1], end[2]
        pir.entry = entry
        nq0 = len(self.queue)
        self.tracked = set()
        for _ in range(12):
            before = set(self.tracked)
            self.walk(pir, interp, entry, snap)
            if self.tracked == before:
                break
        # computed jumps that never met a constant
        for k, cj in pir.cjumps.items():
            node = pir.nodes[k]
            if any(e[0] == 'goto' and not isinstance(e[1][1], int) for e in tree_ends(node.tree)):
                msg = 'computed jump with no known target'
                node.error = msg
                node.tree = _map_leaves(node.tree, lambda t: ([('error', msg)], ('ret', [], None))
                                        if t[0] == 'goto' and not isinstance(t[1][1], int) else ([], t))
                pir.errors.append((k, msg))
        pir.children = self.queue[nq0:]
        self.check_conflicts(pir)
        return pir

    def walk(self, pir, interp, entry, snap):
        """Explore the proto's control flow, lifting each instruction on first reach.

        Constants are tracked along every path; the tracked registers (return addresses for
        computed jumps, decryption keys) are part of the visit signature, so code reached with a
        different return address is walked again and every return target is found."""
        visited = set()
        work = [(entry, snap, {})]
        confl = set()
        while work:
            key2, snap, kregs = work.pop()
            sig = (key2, frozenset((r, kregs[r]) for r in self.tracked if r in kregs))
            if sig in visited:
                continue
            visited.add(sig)
            node = pir.nodes.get(key2)
            if node is None:
                node = Node(key2)
                node.snap_in = snap
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
                self.tracked |= set(node.kdeps)
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
            changed = False
            for li, (path, term) in enumerate(tree_paths(node.raw_tree)):
                if term[0] != 'goto':
                    continue
                regs2 = dict(kregs)
                P.k_transfer(path, regs2, pir.tv)
                tgt = term[1][1]
                if isinstance(tgt, int):
                    work.append((term[1], term[2], regs2))
                    continue
                # computed jump: pc = f(register)
                val = P.k_eval(tgt.e, regs2, pir.tv) if isinstance(tgt, Sym) else P.NOTK
                cj = pir.cjumps.setdefault(key2, {})
                cases = cj.setdefault(li, {})
                if not isinstance(val, int) or isinstance(val, bool):
                    continue
                info = computed_expr(path, tgt.e)
                cond = None
                if info is not None and info[0] in regs2:
                    reg = info[0]
                    self.tracked.add(reg)
                    cond = ('bin', '==', ('r', reg), ('k', regs2[reg]))
                    del regs2[reg]      # the return address is consumed by the jump
                pcv = self.pe.pcvars[id(self.model.loops[term[1][0]])]
                snap2 = dict(term[2])
                snap2[pcv] = val
                tkey = (term[1][0], val)
                if tkey not in cases:
                    cases[tkey] = (cond, snap2)
                    changed = True
                work.append((tkey, snap2, regs2))
            if changed:
                node.tree = self.rebuild_computed(node.raw_tree, pir.cjumps[key2])

    @staticmethod
    def rebuild_computed(raw, cj):
        """Replace each symbolic goto by a chain of tests, one per return address seen."""
        counter = [-1]

        def leaf(term):
            counter[0] += 1
            cases = cj.get(counter[0])
            if term[0] != 'goto' or not cases:
                return ([], term)
            tail = None
            for tkey, (cond, snap2) in reversed(list(cases.items())):
                arm = ([], ('goto', tkey, snap2))
                if tail is None or cond is None:
                    tail = arm
                else:
                    tail = ([], ('br', cond, arm, tail))
            return tail

        return _map_leaves(raw, leaf)

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
        succ = {k: [e[1] for e in tree_ends(n.tree) if e[0] == 'goto'] for k, n in pir.nodes.items()}
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
                self.proto_objs[k] = proto
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
