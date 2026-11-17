"""Drives the partial evaluator over every instruction of every proto -> per-proto IR graph."""
import os
import struct

from . import opaque
from . import pe as P
from .pe import (PE, Scope, LFn, LuaTable, MultiTail, Sym, LinSym, NeedFork, ForOp, LoopHead, PEError,
                 REGS, UPVALS)


def ircfg_cells(cells):
    from . import ircfg
    ircfg.CELLS = cells


class Node:
    __slots__ = ('key', 'tree', 'exposed', 'written', 'snap_in', 'error', 'kreads')

    def __init__(self, key):
        self.key = key
        self.tree = None
        self.exposed = set()
        self.written = set()
        self.snap_in = None
        self.error = None
        self.kreads = set()


class ProtoIR:
    def __init__(self, key):
        self.key = key
        self.entry = None
        self.nodes = {}
        self.children = []
        self.conflicts = []
        self.errors = []
        self.oracle_used = set()
        self.keyregs = {}           # register -> the only number it ever held in the traced run


CAP = '__captured__'      # key in a known-register map: registers captured by reference
SYMPC = 'jump to a run-time computed instruction'


def same_const(a, b):
    if a is b:
        return True
    if type(a) != type(b) and not (P.is_num(a) and P.is_num(b)):
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if P.is_num(a):
        return a == b
    if isinstance(a, P.ConstFn):
        return a.k == b.k and a.value == b.value
    return False


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
        ircfg_cells(model.cells)
        self.pe = PE(model, dump)
        self.log = log or (lambda *a: None)
        self.protos = {}
        self.queue = []
        self.proto_objs = {}
        self.pe.on_closure = self.on_closure
        self.blocked = {}           # proto -> instruction -> jump targets not to follow (opaque.prune_ir)
        self.constprop = False      # lift-time constant propagation (needed by factory-style VMs)
        self.oracle = {}            # (pid, loop, pc) -> what the harness saw when the instruction ran
        self.end_state = False      # the dump was taken at the end of the run (see PE.frozen)
        self.journals = {}
        self.executed_pcs = {}
        self.entry_override = {}    # proto key -> (loop, pc): first instruction of the program proper

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
        pe.regdirty = set()
        pe.regdirty_all = False
        pe.reg_reads = []
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
            temps = {}

            def num(e):
                if e[0] == 'k':
                    return e[1] if P.is_num(e[1]) and not isinstance(e[1], bool) else None
                if e[0] == 'r':
                    v = known.get(e[1])
                    return v if P.is_num(v) and not isinstance(v, bool) else None
                if e[0] == 't':
                    return temps.get(e[1])
                if e[0] == 'bin' and e[1] in ('+', '-', '*'):
                    x, y = num(e[2]), num(e[3])
                    if x is None or y is None:
                        return None
                    r = x + y if e[1] == '+' else x - y if e[1] == '-' else x * y
                    return P.norm(r) if abs(r) < 2 ** 53 else None
                return None

            for st in stmts:
                if st[0] == 'set' and st[1][0] == 't' and isinstance(st[2], tuple):
                    v = num(st[2])
                    if v is not None:
                        temps[st[1][1]] = v
                if st[0] == 'set' and isinstance(st[2], tuple) and st[2][0] == 'closure':
                    refs = {c[1] for c in st[2][2] if c[0] == 'ref' and not isinstance(c[1], tuple)}
                    if refs:
                        known[CAP] = known.get(CAP, frozenset()) | refs
                        for r in refs:
                            known.pop(r, None)
                if st[0] == 'call' and any(a == ('regs',) for a in st[3]):
                    # the register array itself escaped into a call: anything may have changed
                    known = {CAP: known[CAP]} if CAP in known else {}
                if st[0] == 'set' and st[1][0] == 'r' and st[1][1] in known.get(CAP, ()):
                    continue
                if st[0] == 'set' and st[1][0] == 'r':
                    e = st[2]
                    if e[0] == 'outer' and e[1] in by_path:
                        known[st[1][1]] = by_path[e[1]]
                    elif e[0] == 'k' and (P.is_num(e[1]) or isinstance(e[1], bool)):
                        known[st[1][1]] = e[1]
                    elif e[0] == 'constfn':
                        known[st[1][1]] = P.ConstFn(e[1], e[2])
                    elif e[0] in ('t', 'bin') and num(e) is not None:
                        known[st[1][1]] = num(e)
                    else:
                        known.pop(st[1][1], None)
                elif st[0] == 'setmulti':
                    for n in [n for n in known if n != CAP and (not isinstance(n, int) or n >= st[1])]:
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
        tn = os.environ.get('DEVM_TRACE_NODE')
        if tn and tn == '%s,%s' % key[:2]:
            self.pe.trace = []
            try:
                return self.run_node_(interp, key, snap)
            finally:
                print('TRACE NODE', key)
                for t in self.pe.trace[-int(os.environ.get('DEVM_TRACE_N', '40')):]:
                    print('   ', t[:int(os.environ.get('DEVM_TRACE_W', '260'))])
                self.pe.trace = None
        return self.run_node_(interp, key, snap)

    def run_node_(self, interp, key, snap):
        prefix = ('L%dP%d' % key[:2] if key[0] else 'P%d' % key[1]) + ('' if len(key) == 2 else 'v%s' % (key[2],))
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
        all_reads = list(self.pe.reg_reads)
        if os.environ.get('DEVM_TRACE_FOROP') and endT[0] != 'goto':
            print('FOROP', key, endT[:2])
            print('   IR', irT[:6], 'snapnum', {k: v for k, v in snap.items() if isinstance(v, (int, float))})
            for t in self.pe.trace[-60:]:
                print('   ', t)
            self.pe.trace = None
        irF, _, endF, evF, _, _ = self.run_once(interp, (key[0], snap), [], False, prefix)
        kinds = {e[0] for e in evT}
        reads = [s[2][1] for s in irT if s[0] == 'set' and s[2][0] == 'r']
        if not reads:
            # bounds that are known constants are read without leaving a trace in the IR
            reads = all_reads
        writes = [s[1][1] for s in irT if s[0] == 'set' and s[1][0] == 'r']
        if endT[0] != 'goto':
            raise PEError('for-op ended with %r' % (endT[0],))
        if 'push' in kinds and 'wrap' in kinds:
            return (([('genforprep', min(reads))], endT)), ex, wr
        if 'push' in kinds:
            return (([('numforprep', min(reads))], endT)), ex, wr
        if 'fop' in kinds:
            writesF = [s[1][1] for s in irF if s[0] == 'set' and s[1][0] == 'r']
            if not writes and writesF and endF[0] == 'goto':
                # the handler tests the negated condition (`if not continue then else R[A+3] = i ...`):
                # the permissive "false" run is the one that stays in the loop
                endT, endF = endF, endT
                writes = writesF
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

    def lift_node(self, interp, node, key2, snap, known, indirect, pkey, numknown=False):
        """Lift one instruction into node.tree; returns None or the error text."""
        self.pe.regknown = known
        if self.end_state:
            jkey = key2[:2]
            undo(self.journals.pop(jkey, ()))
            self.pe.frozen = (pkey, key2[0], key2[1]) in self.oracle
            self.pe.journal = self.journals[jkey] = []
            if pkey not in self.executed_pcs:
                self.executed_pcs[pkey] = {k[2] for k in self.oracle if k[0] == pkey}
            self.pe.protect = ('__LPH_PROTOS[%s]["vars"]' % (pkey,), self.executed_pcs[pkey])
            if self.pe.replay_tables:
                self.pe.protect = None      # replayed from the initial code: every write happens in order
        base_numknown = self.constprop or numknown
        self.pe.use_numknown = base_numknown if numknown else self.pe.use_numknown
        try:
            # an instruction may decrypt/rewrite itself and ask to be executed again
            retried = False
            for _ in range(8):
                w0 = self.pe.model_writes
                node.tree, node.exposed, node.written = self.run_node(interp, key2, snap)
                stmts, term = node.tree
                if any(e[0] == 'goto' and not isinstance(e[1][1], int) for e in tree_ends(node.tree)) \
                        and not retried:
                    retried = True
                    # jump through a register (Luraph routes many jumps through one trampoline
                    # instruction): retry with the constants known to be in registers, and from
                    # now on keep one copy of this instruction per jump target
                    regs = {s[2][1] for s in node.tree[0] if s[0] == 'set' and s[2][0] == 'r'}
                    self.pe.use_numknown = True
                    try:
                        node.tree, node.exposed, node.written = self.run_node(interp, key2[:2], snap)
                    finally:
                        self.pe.use_numknown = base_numknown
                    stmts, term = node.tree
                    if len(regs) == 1 and not numknown:
                        indirect[key2[:2]] = next(iter(regs))
                if not (term[0] == 'goto' and term[1] == key2[:2] and not stmts and self.pe.model_writes != w0):
                    break
                snap = term[2]
                if not numknown:
                    node.snap_in = snap
        except (PEError, TypeError, ValueError, KeyError, IndexError, AttributeError, ZeroDivisionError,
                OverflowError, struct.error) as e:
            if not isinstance(e, PEError):
                e = PEError('evaluator failure: %s: %s' % (type(e).__name__, e))
            if self.end_state:
                undo(self.journals.pop(key2[:2], ()))
            if 'self-modifying store of a run-time value' in str(e) and not numknown:
                # the patch value was computed by earlier instructions from plain constants
                # (a counter kept in an operand slot): retry with those register constants
                return self.lift_node(interp, node, key2, snap, known, indirect, pkey, numknown=True)
            return str(e)
        finally:
            self.pe.use_numknown = self.constprop
            self.pe.frozen = False
            self.pe.journal = None
            self.pe.protect = None
        if any(not isinstance(end[1][1], int) for end, kn in self.known_after(node.tree, known)):
            return SYMPC
        return None

    # ---- whole proto
    def lift_proto(self, key):
        pir = ProtoIR(key)
        self.pe.keyfn_used = pir.keyfns = set()
        indirect = {}       # (loop, pc) of a jump-through-register instruction -> register
        interp = self.instantiate(key)
        _, _, end, _, _, _ = self.run_once(interp, None, [], None, 'entry')
        if end[0] != 'goto':
            raise PEError('interpreter returned before reaching dispatch loop')
        entry, snap = end[1], end[2]
        if key in self.entry_override:
            entry, ovars = self.entry_override[key]
            pcv = self.pe.pcvars[id(self.model.loops[entry[0]])]
            snap = dict(snap)
            for name, v in ovars.items():
                if name in snap and (snap[name] is None or P.is_num(snap[name])):
                    snap[name] = P.norm(v)
            snap[pcv] = entry[1]
        pir.entry = entry
        work = [(entry, snap, {}, None, None, None)]
        # Traced lifts: the writes instructions make to the code (decrypt a block, use it, encrypt
        # it again) belong to the path they are on.  Every queued instruction remembers its path
        # as a chain of write sets, and the model is moved to that state before it is lifted.
        pathmode = self.end_state
        cur_path = [None]

        snap_cells = {}
        replay_ids = set()

        shared_ids = set()

        def snap_cell(sv):
            """path root for code-state version sv of this proto: the differences from the end state"""
            if sv in snap_cells:
                return snap_cells[sv]
            cell = None
            snaps = self.dump.data.get('snaps') or {}
            info = snaps.get(str(key))
            env = self.records[key - 1][1] if hasattr(self, 'records') and isinstance(key, int) else {}
            # the loader's shared tables (harness sharedBase)
            shared = []
            for ent in snaps.get('shared') or ():
                holder = env.get(ent['name'])
                obj = holder.h.get(ent['key']) if isinstance(holder, LuaTable) else None
                # (two interpreters can each have a table under the same name: the entry belongs
                # to this one only if its keys are this table's keys)
                if isinstance(obj, LuaTable) and ent['base'] and \
                        sum(1 for k in ent['base'] if P.is_num(obj.h.get(int(k)))) >= 0.95 * len(ent['base']):
                    shared_ids.add(id(obj))
                    shared.append((obj, ent['base']))
            if info and env:
                writes = []
                for obj, base in shared:
                    for k, v in base.items():
                        k, v = int(k), P.norm(v)
                        cur = obj.h.get(k)
                        if P.is_num(cur) and cur != v:
                            writes.append((obj, k, cur, v))
                    replay_ids.add(id(obj))
                for i, name in enumerate(info['names']):
                    obj = env.get(name)
                    if not isinstance(obj, LuaTable):
                        continue
                    state = {int(k): v for k, v in info['base'][i].items()}
                    for ver in info['vers'][:sv]:
                        for ti, k, v in ver:
                            if ti == i + 1:
                                state[int(k)] = v
                    for k, v in state.items():
                        v = P.norm(v)
                        cur = obj.h.get(k)
                        if P.is_num(cur) and cur != v:
                            writes.append((obj, k, cur, v))
                    replay_ids.add(id(obj))
                if writes:
                    cell = (None, writes)
            snap_cells[sv] = cell
            return cell

        def put(obj, k, v):
            if k is None:
                obj.data[:] = v
            else:
                obj.set(k, v)

        def switch(to):
            a = []
            x = cur_path[0]
            while x is not None:
                a.append(x)
                x = x[0]
            b = []
            x = to
            while x is not None:
                b.append(x)
                x = x[0]
            inb = {id(c) for c in b}
            i = 0
            while i < len(a) and id(a[i]) not in inb:
                i += 1
            common = a[i] if i < len(a) else None
            j = 0
            while j < len(b) and b[j] is not common:
                j += 1
            for c in a[:i]:
                for obj, k, old, new in reversed(c[1]):
                    put(obj, k, old)
            for c in reversed(b[:j]):
                for obj, k, old, new in c[1]:
                    put(obj, k, new)
            cur_path[0] = to
        node_in = {}
        relifts = {}
        patched = {}
        pobj = getattr(self, 'proto_objs', {}).get(key)
        ppath = {id(v) for v in pobj.h.values() if isinstance(v, LuaTable)} if isinstance(pobj, LuaTable) else None
        self.pe.use_numknown = self.constprop
        nq0 = len(self.queue)
        if pathmode:
            # Code that changes while it runs (blocks decrypted before use): the walk starts from
            # the code as it was at the function's first instruction and replays every change
            # along the path being followed, for instructions that ran and those that did not.
            root_cell = snap_cell(0)
            self.pe.replay_tables = set(replay_ids)

            def live_patches():
                # shared-table entries the current path has changed (not the root cell's return
                # to the initial state): what the live run must see to decode for this path
                cells = []
                x = cur_path[0]
                while x is not None and x is not root_cell:
                    cells.append(x)
                    x = x[0]
                seen = {}
                for c in reversed(cells):
                    for obj, k, old, new in c[1]:
                        if id(obj) in shared_ids and obj.path and isinstance(k, int) and P.is_num(new):
                            seen[(obj.path, k)] = new
                return [('T', pth, k, v) for (pth, k), v in seen.items()]
            self.pe.live_table_patches = live_patches
            if root_cell is not None:
                work[0] = work[0][:5] + (root_cell,)
        while work:
            key2, snap, known, pred, pend, path = work.pop()
            if key2 in pir.nodes and self.constprop and len(key2) == 2:
                # a pure jump whose target comes out of a register is a trampoline shared by many
                # jump sites: keep one copy per target instead of merging the register away
                node = pir.nodes[key2]
                r = indirect.get(key2)
                if r is None and not node.tree[0] and node.tree[1][0] == 'goto' and len(node.kreads) == 1:
                    r = next(iter(node.kreads))
                    if P.is_num(node_in[key2].get(r)):
                        indirect[key2] = r
                    else:
                        r = None
                if r is not None and P.is_num(known.get(r)) and not same_const(known[r], node_in[key2].get(r)):
                    key2 = key2 + (known[r],)
                    if pred is not None:
                        pred.tree = retarget_tree(pred.tree, {id(pend): key2})
            if key2 in pir.nodes:
                node = pir.nodes[key2]
                if self.constprop:
                    # constant propagation: a register is only "known" at an instruction if every
                    # path agrees on it; when a later path disagrees the instruction is re-lifted
                    old = node_in[key2]
                    meet = {r: v for r, v in old.items()
                            if r != CAP and r in known and same_const(known[r], v)}
                    cap = old.get(CAP, frozenset()) | known.get(CAP, frozenset())
                    for r in cap:
                        meet.pop(r, None)
                    if cap:
                        meet[CAP] = cap
                    if len(meet) == len(old) and meet.get(CAP) == old.get(CAP):
                        continue
                    known = meet
                    snap = node.snap_in
                    relifts[key2] = relifts.get(key2, 0) + 1
                    if relifts[key2] > 60:
                        continue
                    del pir.nodes[key2]
                else:
                    for name, v in node.snap_in.items():
                        if name in snap and not same_value(v, snap[name]):
                            pir.conflicts.append((key2, name, v, snap[name]))
                    continue
            ran = pathmode and (key, key2[0], key2[1]) in self.oracle
            replay = bool(self.pe.replay_tables)
            if pathmode:
                # without recorded code versions an instruction that ran is lifted from the state
                # at the end of the run; otherwise every instruction from the state of its path
                switch(path if (replay or not ran) else None)
                self.journals.pop(key2[:2], None)       # (its earlier writes are not part of this state)
            node_in[key2] = known
            node = Node(key2)
            node.snap_in = snap
            pir.nodes[key2] = node
            self.pe.known_reads = node.kreads = set()
            self.pe.write_log = wlog = [] if ppath and not self.end_state else None
            err = self.lift_node(interp, node, key2, snap, known, indirect, key)
            self.pe.write_log = None
            if wlog and err is None:
                # the instruction rewrote instructions of its own function that were lifted
                # already (a jump that is patched after its first use): lift those again
                pcs = {k for oid, k in wlog if isinstance(k, int) and oid in ppath}
                for k2 in [k for k in pir.nodes if k[1] in pcs and k != key2]:
                    patched[k2] = patched.get(k2, 0) + 1
                    if patched[k2] > 8:
                        continue
                    old = pir.nodes.pop(k2)
                    pir.errors = [e for e in pir.errors if e[0] != k2]
                    work.insert(0, (k2, old.snap_in, node_in.get(k2, {}), None, None, None))
            hint = self.oracle.get((key, key2[0], key2[1])) if err and isinstance(key, int) else None
            if hint is not None and hint.get('p'):
                hint = None      # executed by a probe only: no real register values or branch decisions
            if hint is not None:
                # The instruction depends on a value that only exists at run time (a decryption key
                # for self-modifying code, a jump target mixed with a key).  Lift it with the numbers
                # the registers and interpreter locals actually held when the script ran.
                oregs, ovars = hint['r'], hint['v']
                known2 = dict(known)
                cap = known.get(CAP, ())
                for r, v in oregs.items():
                    if r not in known2 and r not in cap:
                        known2[r] = P.norm(v)
                snap2 = dict(snap)
                for name, v in ovars.items():
                    if isinstance(snap2.get(name), Sym):
                        snap2[name] = P.norm(v)
                node2 = Node(key2)
                node2.snap_in = snap
                self.pe.known_reads = node2.kreads = set()
                err2 = self.lift_node(interp, node2, key2, snap2, known2, indirect, key, numknown=True)
                if err2 is None:
                    node = node2
                    pir.nodes[key2] = node
                    err = None
                    pir.oracle_used.add(key2)
                    if os.environ.get('DEVM_TRACE_ERR'):
                        print('ORACLE resolved', key, key2)
                else:
                    self.pe.known_reads = node.kreads
                    if os.environ.get('DEVM_TRACE_ERR'):
                        print('ORACLE failed', key, key2, err2)
            if err is None and hint is None and isinstance(key, int):
                hint = self.oracle.get((key, key2[0], key2[1]))
            if err is None and hint is not None and not hint.get('p') and hint.get('succ') and node.tree[1][0] == 'br':
                node.tree = mark_cold(node.tree, hint['succ'])
            if err is not None:
                if os.environ.get('DEVM_TRACE_ERR'):
                    print('PEERR', key, key2, err, {k: (v.path if hasattr(v, 'path') else v) for k, v in known.items()})
                    print('   partial IR', self.pe.ir[:6], '...', self.pe.ir[-8:])
                stmts = list(node.tree[0]) if err == SYMPC and node.tree else []
                node.error = err
                node.tree = (stmts + [('error', err)], ('ret', [], None))
                pir.errors.append((key2, err))
                continue
            cut = self.blocked.get(key, {}).get(key2[:2])
            if cut:
                # opaque predicate resolved by an earlier pass: the junk arm is not lifted at all,
                # so that the instruction patches it contains cannot touch real code
                node.tree = opaque.cut_arms(node.tree, cut)
            ends = self.known_after(node.tree, known)
            if any(end[1] in indirect for end, kn in ends):
                # specialise jumps into a trampoline by the value it will jump through
                special = {}
                for end, kn in ends:
                    r = indirect.get(end[1])
                    if r is not None and P.is_num(kn.get(r)):
                        special[id(end)] = end[1] + (kn[r],)
                node.tree = retarget_tree(node.tree, special)
                ends = self.known_after(node.tree, known)
            if pathmode:
                first = {}
                for obj, k, old in self.journals.get(key2[:2]) or ():
                    first.setdefault((id(obj), k), (obj, k, old))
                writes = [(obj, k, old, bytes(obj.data) if k is None else obj.h.get(k)) for obj, k, old in first.values()]
                if writes and (replay or not ran):
                    path = (path, writes)
                    cur_path[0] = path
            if pathmode and len(ends) > 1:
                # Follow the exits the traced run took before the ones it never took: an untaken
                # exit can be the dead arm of an opaque predicate, whose code leaves the program
                # in a state no real path has, and it must not be first to reach shared code.
                rec = self.oracle.get((key, key2[0], key2[1]))
                taken = rec.get('succ') if rec else None

                def cold(item):
                    t = item[0][1]
                    if taken is not None:
                        return tuple(t[:2]) not in taken
                    return (key, t[0], t[1]) not in self.oracle
                ends = sorted(ends, key=lambda item: 0 if cold(item) else 1)
            for end, kn in ends:
                work.append((end[1], end[2], kn, node, end, path))
        self.pe.regknown = {}
        self.pe.use_numknown = False
        if self.constprop:
            # drop instructions that were only reachable under assumptions later withdrawn
            seen = set()
            stack = [pir.entry]
            while stack:
                k = stack.pop()
                if k in seen or k not in pir.nodes:
                    continue
                seen.add(k)
                for e in tree_ends(pir.nodes[k].tree):
                    if e[0] == 'goto':
                        stack.append(e[1])
            pir.nodes = {k: n for k, n in pir.nodes.items() if k in seen}
            pir.errors = [(k, n.error) for k, n in pir.nodes.items() if n.error]
        pir.children = self.queue[nq0:]
        self.check_conflicts(pir)
        if self.end_state and isinstance(key, int):
            # registers that held one and the same number in every instruction that ran: key material
            seen, count = {}, {}
            for (pid, _, _), rec in self.oracle.items():
                if pid != key:
                    continue
                for r, v in rec['r'].items():
                    count[r] = count.get(r, 0) + 1
                    if r not in seen:
                        seen[r] = v
                    elif seen[r] != v:
                        seen[r] = None
            pir.keyregs = {r: P.norm(v) for r, v in seen.items() if v is not None and count[r] >= 200}
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


def program_entries(model, oracle):
    """{pid: ((loop, pc), interpreter locals)} for protos that begin by decrypting themselves.

    The first instructions of such a proto rewrite its whole code and are then overwritten
    themselves, so in a dump taken at the end of the run they are garbage.  They are recognised
    by having been executed in a form that differs from the final one in more than the opcode
    (an instruction that merely retires itself only swaps its opcode for a no-op); the program
    proper starts at the first instruction executed after the last of them."""
    opname = [l[2][0][2][0][1][1] for l in model.loops]

    def fields(text):
        return dict(x.split('=') for x in text.split())

    last_stale = {}
    for (pid, loop, pc), rec in oracle.items():
        a, f = fields(rec['vers'][-1][1]), fields(rec['final'])
        diff = {n for n in a if f.get(n) != a[n]}
        if diff - {opname[loop]}:
            last_stale[pid] = max(last_stale.get(pid, 0), rec['last'])
    out = {}
    for pid, step in last_stale.items():
        # only protos whose very first instructions are stale
        start = min(oracle[k]['vers'][0][0] for k in oracle if k[0] == pid)
        k0 = [k for k in oracle if k[0] == pid and oracle[k]['vers'][0][0] == start][0]
        a, f = fields(oracle[k0]['vers'][-1][1]), fields(oracle[k0]['final'])
        if not ({n for n in a if f.get(n) != a[n]} - {opname[k0[1]]}):
            continue
        later = [(oracle[k]['vers'][0][0], k) for k in oracle if k[0] == pid and oracle[k]['vers'][0][0] > step]
        if later:
            _, k = min(later)
            out[pid] = ((k[1], k[2]), oracle[k]['v'])
    return out


def mark_cold(tree, taken):
    """Tag the exits of a conditional instruction that the traced run never took: ('cold',) is
    appended to the statements on that exit (see ProtoCFG._cut_cold_edges)."""
    stmts, term = tree
    if term[0] == 'br':
        return (stmts, ('br', term[1], mark_cold(term[2], taken), mark_cold(term[3], taken)))
    if term[0] == 'goto' and isinstance(term[1][1], int) and tuple(term[1][:2]) not in taken:
        return (list(stmts) + [('cold',)], term)
    return tree


def undo(journal):
    """Take back the writes an instruction made to the VM state model."""
    for obj, key, old in reversed(journal):
        if key is None:
            obj.data[:] = old
        else:
            obj.set(key, old)


def retarget_tree(tree, special):
    """Copy of an instruction tree with selected goto terminators (by identity) pointing elsewhere."""
    stmts, term = tree
    if term[0] == 'br':
        return (stmts, ('br', term[1], retarget_tree(term[2], special), retarget_tree(term[3], special)))
    if term[0] == 'forbr':
        return (stmts, ('forbr', term[1], term[2], retarget_tree(term[3], special), retarget_tree(term[4], special)))
    if term[0] == 'goto' and id(term) in special:
        return (stmts, ('goto', special[id(term)], term[2]))
    return tree


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
            if st[0] != 'local':
                continue
            for e in st[2]:
                if e[0] == 'paren':
                    e = e[1]
                if e[0] == 'index' and e[1][0] == 'name' and e[1][1] in self.wrap.params:
                    counts[e[1][1]] = counts.get(e[1][1], 0) + 1
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


class FactoryLifter(Lifter):
    """Family 2: factory(self, code arrays..., ...) -> wrap(upvalues) -> function(...).

    Every proto is turned into a closure factory while the bytecode is deserialized; the harness
    records the arguments of each factory call (see __LPH_PROTO), a proto is identified by that
    record's index.
    """

    def __init__(self, model, dump, log=None):
        super().__init__(model, dump, log)
        self.records = dump.proto_records()
        self.constprop = True
        root = dump.root_vars()
        self.root_key = root['__pid']
        for it in model.interps:
            # program data = everything reachable from the factory arguments except `self`
            pass
        # loader state lives in the VM's own table (first factory parameter)
        for idx, env in self.records:
            it = model.interps[idx]
            first = [p for p in it.chain[-3].params if p != '_' or model.underscore_is_name][0]
            v = env.get(first)
            if isinstance(v, LuaTable) and v.path:
                self.pe.volatile.add(v.path)

    def upvalue_param(self, mid):
        """Index of the wrapper parameter that receives the upvalue list.

        Minified builds repeat parameter names (the last one wins) and shadow the unused ones with
        the wrapper's first `local` statement; decompiler output marks unused ones with `_`."""
        shadowed = set()
        if mid.body and mid.body[0][0] == 'local':
            shadowed = set(mid.body[0][1])
        cands = [i for i, p in enumerate(mid.params)
                 if p not in shadowed and p not in mid.params[i + 1:] and (p != '_' or self.model.underscore_is_name)]
        return cands[0] if cands else None

    def proto_key(self, proto):
        if isinstance(proto, P.ProtoRef):
            return proto.pid
        raise PEError('unknown proto object %r' % (proto,))

    def register(self, key):
        pass

    def lift_all(self, root_key):
        """Every proto is lifted against a pristine copy of the dumped VM state.

        Lifting evaluates instructions that modify state shared by all protos (one-shot decryption
        of pooled constants inside the bytecode buffer).  A proto that is still partly encrypted
        (hash-keyed code that has not run yet) lifts garbage on those paths, and garbage writes
        must not leak into the constants of the other protos."""
        queue = [root_key]
        done = {}
        while queue:
            k = queue.pop(0)
            if k in done:
                continue
            fresh = FactoryLifter(self.model, P.Dump(self.dump.data, self.model), self.log)
            fresh.dump.server = self.dump.server
            fresh.pe.keyfn_override = self.pe.keyfn_override
            fresh.oracle = self.oracle
            fresh.end_state = self.end_state
            fresh.entry_override = self.entry_override
            pir = fresh.lift_proto(k)
            pir.keyfns = set(pir.keyfns)
            done[k] = pir
            queue.extend(c for c in pir.children if c not in done)
        self.protos = done
        return done

    def instantiate(self, key):
        idx, env = self.records[key - 1]
        it = self.model.interps[idx]
        outer, mid = it.chain[-3], it.chain[-2]
        pe = self.pe
        pe.set_interp(it)
        pe.seeking = False
        pe.perm = None
        pe.ir = []
        pe.wrap_func = None
        sc = Scope()
        for p in outer.params:
            if p != '_' or self.model.underscore_is_name:
                sc.vars[p] = env.get(p)
        sc.vars['__pid'] = key
        r = pe.exec_block(outer.body, sc)
        if not r or r[0] != 'return' or not isinstance(r[1][0], LFn) or r[1][0].func is not mid:
            raise PEError('factory did not return the wrapper closure')
        midfn = r[1][0]
        sc2 = Scope(midfn.scope)
        up = self.upvalue_param(mid)
        pe.upvals_list = self.dump.proto_upvals(key) if self.end_state else None
        for i, p in enumerate(mid.params):
            sc2.vars[p] = None
        if up is not None:
            sc2.vars[mid.params[up]] = UPVALS
        r = pe.exec_block(mid.body, sc2)
        if not r or r[0] != 'return' or not isinstance(r[1][0], LFn):
            raise PEError('wrapper did not return the interpreter closure')
        return r[1][0]
