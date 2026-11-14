"""Drives the partial evaluator over every instruction of every proto -> per-proto IR graph."""
import os

from . import pe as P
from .pe import (PE, Scope, LFn, LuaTable, MultiTail, Sym, LinSym, NeedFork, ForOp, LoopHead, PEError,
                 REGS, UPVALS)


class Node:
    __slots__ = ('key', 'tree', 'exposed', 'written', 'snap_in', 'error')

    def __init__(self, key):
        self.key = key
        self.tree = None
        self.exposed = set()
        self.written = set()
        self.snap_in = None
        self.error = None


class ProtoIR:
    def __init__(self, key):
        self.key = key
        self.entry = None
        self.nodes = {}
        self.children = []
        self.conflicts = []
        self.errors = []


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

    def register(self, key):
        """Make a proto liftable on its own (without having met the closure that creates it)."""
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

    def known_after(self, tree, known):
        """[(goto target, registers known to hold program tables)] for every exit of an instruction."""
        out = []
        by_path = self.dump.by_path

        def walk(tree, known):
            stmts, term = tree
            known = dict(known)
            for st in stmts:
                if st[0] == 'set' and st[1][0] == 'r':
                    e = st[2]
                    if e[0] == 'outer' and e[1] in by_path:
                        known[st[1][1]] = by_path[e[1]]
                    elif e[0] == 'k' and isinstance(e[1], P.KNum):
                        known[st[1][1]] = e[1]
                    elif e[0] == 'constfn':
                        known[st[1][1]] = P.ConstFn(e[1], e[2])
                    else:
                        known.pop(st[1][1], None)
                elif st[0] == 'setmulti':
                    for n in [n for n in known if not isinstance(n, int) or n >= st[1]]:
                        del known[n]
            if term[0] == 'br':
                walk(term[2], known)
                walk(term[3], known)
            elif term[0] == 'forbr':
                regs = (term[2] + 3,) if term[1] == 'num' else tuple(term[2])
                for n in regs:
                    known.pop(n, None)
                walk(term[3], known)
                walk(term[4], known)
            elif term[0] == 'goto':
                out.append((term, known))

        walk(tree, known)
        return out

    def run_node(self, interp, key, snap):
        prefix = 'L%dP%d' % key if key[0] else 'P%d' % key[1]
        paths = []
        todo = [[]]
        exposed, written = set(), set()
        while todo:
            d = todo.pop()
            if os.environ.get('DEVM_TRACE_FOROP'):
                self.pe.trace = []
            try:
                ir, path, end, ev, ex, wr = self.run_once(interp, (key[0], snap), d, None, prefix)
            except NeedFork:
                todo.append(d + [False])
                todo.append(d + [True])
                if len(d) > 12:
                    raise PEError('too many symbolic branches')
                continue
            except ForOp:
                if os.environ.get('DEVM_TRACE_FOROP') and self.pe.trace:
                    print('FOROP-TRIGGER', key)
                    for t in self.pe.trace[-6:]:
                        print('   ', t[:1500])
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
        if os.environ.get('DEVM_TRACE_FOROP'):
            self.pe.trace = []
        irT, _, endT, evT, ex, wr = self.run_once(interp, (key[0], snap), [], True, prefix)
        if os.environ.get('DEVM_TRACE_FOROP') and endT[0] != 'goto':
            print('FOROP', key, endT[:2])
            print('   IR', irT[:6], 'snapnum', {k: v for k, v in snap.items() if isinstance(v, (int, float))})
            for t in self.pe.trace[-60:]:
                print('   ', t)
            self.pe.trace = None
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
        self.pe.keyfn_used = pir.keyfns = set()
        interp = self.instantiate(key)
        _, _, end, _, _, _ = self.run_once(interp, None, [], None, 'entry')
        if end[0] != 'goto':
            raise PEError('interpreter returned before reaching dispatch loop')
        entry, snap = end[1], end[2]
        pir.entry = entry
        work = [(entry, snap, {})]
        nq0 = len(self.queue)
        while work:
            key2, snap, known = work.pop()
            if key2 in pir.nodes:
                node = pir.nodes[key2]
                for name, v in node.snap_in.items():
                    if name in snap and not same_value(v, snap[name]):
                        pir.conflicts.append((key2, name, v, snap[name]))
                continue
            node = Node(key2)
            node.snap_in = snap
            pir.nodes[key2] = node
            self.pe.regknown = known
            try:
                node.tree, node.exposed, node.written = self.run_node(interp, key2, snap)
            except PEError as e:
                if os.environ.get('DEVM_TRACE_ERR'):
                    print('PEERR', key, key2, e, {k: (v.path if hasattr(v, 'path') else v) for k, v in known.items()})
                    print('   partial IR', self.pe.ir[-8:])
                node.error = str(e)
                node.tree = ([('error', str(e))], ('ret', [], None))
                pir.errors.append((key2, str(e)))
                continue
            for end, kn in self.known_after(node.tree, known):
                work.append((end[1], end[2], kn))
        self.pe.regknown = {}
        pir.children = self.queue[nq0:]
        self.check_conflicts(pir)
        return pir

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
        # tables handed to the wrapper's enclosing functions are loader state, not program data
        program = set()
        stack = [self.base.vars[self.proto_param]]
        while stack:
            t = stack.pop()
            if id(t) in program:
                continue
            program.add(id(t))
            stack.extend(x for x in t.h.values() if isinstance(x, LuaTable))
        for f in model.chain[:-2]:
            for p in f.params:
                v = self.base.vars.get(p)
                if isinstance(v, LuaTable) and v.path and id(v) not in program:
                    self.pe.volatile.add(v.path)
                    for sub in v.h.values():
                        if isinstance(sub, LuaTable) and sub.path and id(sub) not in program:
                            self.pe.volatile.add(sub.path)
        self.root_key = self.base.vars[self.proto_param].id
        self.proto_objs[self.root_key] = self.base.vars[self.proto_param]

    def register(self, key):
        self.proto_objs[key] = self.dump.tables[key]

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
