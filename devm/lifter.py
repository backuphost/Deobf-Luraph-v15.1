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
ABS = '__truth__'         # ... and: frozenset of (register, 'T...' | 'F'), see known_after
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
        self.temp_vals = {}
        self.up_nums = {}
        self.cap_src = {}           # function key -> {upvalue index: where its creator takes it from}
        self.cap_vals = {}          # (function key, register) -> number last stored in a shared variable
        self.all_up_nums = {}
        self.cap_hist = {}          # (function key, register) -> every number stored there
        self.cap_pick = {}          # ... and the one to use instead of the last (lift_all)
        self.lifting_key = None
        self.folds = {}
        self.opaque_in = {}
        self.sticky = {}
        self.cur_node = None
        self.param_vals = {}        # parameter index -> opaque number every caller passes
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
            pc = lh.snapshot[pcv]
            if isinstance(pc, float) and not isinstance(pc, P.UpNum) and pc.is_integer():
                # a jump operand that a key-derived patch put there: an instruction number all the same
                pc = lh.snapshot[pcv] = int(pc)
            end = ('goto', (lh.loop, pc), lh.snapshot)
        finally:
            pe.track = False
        pe.finish_closures()
        return list(pe.ir), list(pe.path), end, list(pe.events), set(pe.exposed), set(pe.written)

    def known_after(self, tree, known):
        """[(goto target, registers known to hold program tables)] for every exit of an instruction."""
        out = []
        brs = []
        cmps = []
        memo = {}
        by_path = self.dump.by_path

        def walk(tree, known, temps=None, calls=None, tabs=None):
            stmts, term = tree
            known = dict(known)
            temps = dict(temps or ())
            # Truth values that are certain: 'T...' cannot be nil or false here, 'F' is.  Luraph's
            # opaque predicates test a value it knows the truth of (a number just computed, an
            # object just indexed), usually through two `not`s; the dead side leaves the code
            # in a state no real path has, so it must not be followed.
            ab = dict(known.get(ABS, ()))
            tabs = dict(tabs or ())         # the same for the instruction's temporaries
            torig = {}                      # temporary -> register it was read from

            def absval(e):
                if e[0] == 't':
                    return tabs.get(e[1])
                if e[0] == 'r':
                    return ab.get(e[1])
                if e[0] == 'k':
                    return 'F' if e[1] is None or e[1] is False else 'T'
                if e[0] in ('newtable', 'closure', 'constfn'):
                    return 'T'
                if e[0] == 'kfn':
                    return 'T:' + str(e[1])
                if e[0] == 'un' and e[1] == 'not':
                    v = absval(e[2])
                    if v is not None and v[1:2] == 'b':
                        # (known from a branch only: see below)
                        return ('F' if v[0] == 'T' else 'T') + 'b' + v[2:] + 'n'
                    return None if v is None else ('F' if v[0] == 'T' else 'T')
                if e[0] == 'un' and e[1] in ('#', '-'):
                    return 'T'
                if e[0] == 'bin' and e[1] in ('+', '-', '*', '/', '%', '^', '..', '//'):
                    return 'T'
                return None

            def decided(term):
                """True / False when the branch has one outcome, else None.  The outcome of an
                opaque predicate does not depend on the path: once seen, it holds for the
                instruction also where the value's history is not known (a join of paths)."""
                v = absval(term[1]) or ''
                if v[1:2] == 'b' and not v.endswith('nn'):
                    v = ''          # an ordinary second test of a value: both sides are real
                v = v[:1]
                slot = (self.cur_node, len(brs))
                brs.append(term)
                live = None if v not in ('T', 'F') else v == 'T'
                if live is not None:
                    for arm, side in ((term[2], False), (term[3], True)):
                        if arm[0] and arm[0][0] == ('cold',) and live == side:
                            pass
                    if (term[2][0] and term[2][0][0] == ('cold',) and live) or \
                            (term[3][0] and term[3][0][0] == ('cold',) and not live):
                        return None         # (the traced run says otherwise)
                    if self.sticky.get(slot, live) != live:
                        self.sticky[slot] = 'conflict'
                        return None
                    self.sticky[slot] = live
                    return live
                prev = self.sticky.get(slot)
                return prev if isinstance(prev, bool) else None

            def live_side(term):
                if id(term) not in memo:
                    memo[id(term)] = decided(term)
                return memo[id(term)]

            def used(e):
                # indexing or calling it worked: it is not nil or false
                if e[0] == 't' and e[1] in torig:
                    ab[torig[e[1]]] = ab.get(torig[e[1]]) if (ab.get(torig[e[1]]) or '')[:1] == 'T' else 'T'
                    tabs[e[1]] = tabs.get(e[1]) if (tabs.get(e[1]) or '')[:1] == 'T' else 'T'
                elif e[0] == 'r':
                    ab[e[1]] = ab.get(e[1]) if (ab.get(e[1]) or '')[:1] == 'T' else 'T'

            def num(e):
                if e[0] == 'k':
                    return e[1] if P.is_num(e[1]) and not isinstance(e[1], bool) else None
                if e[0] == 'r':
                    v = known.get(e[1])
                    return v if P.is_num(v) and not isinstance(v, bool) else None
                if e[0] == 't':
                    # (a temporary of an earlier instruction: the value of an interpreter local)
                    return temps[e[1]] if e[1] in temps else self.temp_vals.get(e[1])
                if e[0] in ('up', 'upcell'):
                    return self.up_nums.get(e[1])
                if e[0] == 'mv' and e[2] == 1:
                    return calls.get(e[1])
                if e[0] == 'bin' and e[1] in ('+', '-', '*', '%', '//'):
                    x, y = num(e[2]), num(e[3])
                    if x is None or y is None or (e[1] in ('%', '//') and y == 0):
                        return None
                    r = x + y if e[1] == '+' else x - y if e[1] == '-' else x * y if e[1] == '*' else \
                        x % y if e[1] == '%' else x // y
                    if abs(r) >= 2 ** 53:
                        return None
                    if isinstance(x, P.UpNum) or isinstance(y, P.UpNum):
                        return P.UpNum(r)
                    return P.norm(r)
                return None

            calls = dict(calls or ())
            truth = {}
            cabs = {}
            for st in stmts:
                if st[0] == 'set' and isinstance(st[2], tuple):
                    e = st[2]
                    if e[0] in ('index', 'method') and isinstance(e[1], tuple):
                        used(e[1])
                    v = cabs.get(e[1]) if e[0] == 'mv' and e[2] == 1 else absval(e)
                    if v is None and e[0] == 'bin' and e[1] in ('==', '~='):
                        # a guard on an opaque number whose value is known
                        x, y = num(e[2]), num(e[3])
                        magic = any(q[0] == 'k' and P.is_num(q[1]) and not isinstance(q[1], bool)
                                    and abs(q[1]) >= 2 ** 24 for q in (e[2], e[3]))
                        slot = (self.cur_node, 'cmp', len(cmps))
                        cmps.append(e)
                        if x is not None and y is not None:
                            v = 'T' if (x == y) == (e[1] == '==') else 'F'
                            if magic and self.cur_node is not None:
                                # (the outcome of a guard is the same on every path: it is kept for
                                # the paths on which the number's history is not known)
                                self.sticky[slot] = v if self.sticky.get(slot, v) == v else 'conflict'
                        elif magic and self.sticky.get(slot) in ('T', 'F'):
                            v = self.sticky[slot]
                    if st[1][0] == 't':
                        if v is not None:
                            tabs[st[1][1]] = v
                        if e[0] == 'r':
                            torig[st[1][1]] = e[1]
                    elif st[1][0] == 'r':
                        if v is not None and st[1][1] not in known.get(CAP, ()):
                            ab[st[1][1]] = v
                        else:
                            ab.pop(st[1][1], None)
                elif st[0] == 'call':
                    fn = absval(st[2]) if isinstance(st[2], tuple) else None
                    if isinstance(st[2], tuple):
                        used(st[2])
                    name = fn[2:] if fn and fn.startswith('T:') else ''
                    if name.startswith(('math.', 'bit32.')) or name in ('tostring', 'type', 'typeof', 'os.clock', 'os.time',
                                                                       'tick', 'string.format', 'string.lower',
                                                                       'string.upper', 'string.sub', 'string.rep',
                                                                       'string.len', 'table.concat'):
                        cabs[st[1]] = 'T'
                    if not name:
                        for r in known.get(CAP, ()):
                            ab.pop(r, None)
                elif st[0] in ('setindex',) and isinstance(st[1], tuple):
                    used(st[1])
                elif st[0] == 'setmulti':
                    for n in [n for n in ab if not isinstance(n, int) or n >= st[1]]:
                        del ab[n]
                elif st[0] == 'params' and self.param_vals:
                    # opaque numbers the callers pass (see opaque.relift_params)
                    for i, v in self.param_vals.items():
                        if i < st[2]:
                            known[st[1] + i] = P.norm(v)
                if st[0] == 'call' and st[2][0] == 'kfn' and str(st[2][1]).startswith('bit32.') and st[4] is None:
                    # arithmetic on the opaque state numbers, done with the bit library
                    args = [num(a) for a in st[3]]
                    if args and all(a is not None for a in args):
                        from . import ircfg
                        v = ircfg._opaque_eval(('call', st[2], [('k', a) for a in args], None), {})
                        if v is not None:
                            calls[st[1]] = P.UpNum(v) if any(isinstance(a, P.UpNum) for a in args) else P.norm(v)
                if st[0] == 'set' and st[1][0] == 't' and isinstance(st[2], tuple) and st[2][0] == 'bin' \
                        and st[2][1] in ('==', '~='):
                    x, y = num(st[2][2]), num(st[2][3])
                    if x is not None and y is not None:
                        truth[st[1][1]] = (x == y) == (st[2][1] == '==')
                if st[0] == 'set' and st[1][0] == 't' and isinstance(st[2], tuple):
                    v = num(st[2])
                    if v is not None:
                        temps[st[1][1]] = v
                        self.temp_vals[st[1][1]] = v     # (a temporary can outlive the instruction
                                                         # as the value of an interpreter local)
                if st[0] == 'set' and isinstance(st[2], tuple) and st[2][0] == 'closure':
                    # where the new function's upvalues come from: opaque numbers among them are
                    # needed to lift it when it never ran (see instantiate)
                    src = self.cap_src.setdefault(st[2][1], {})
                    for i, c in enumerate(st[2][2]):
                        if c[0] in ('ref', 'val') and not isinstance(c[1], tuple):
                            v = known.get(c[1])
                            v = v if P.is_num(v) and not isinstance(v, bool) else None
                            if c[0] == 'val':
                                if v is not None:
                                    src[i] = ('num', v)
                            else:
                                src[i] = ('reg', self.lifting_key, c[1])
                                if v is not None:
                                    # (a number stored after the capture counts: see below)
                                    self.cap_vals.setdefault((self.lifting_key, c[1]), v)
                                    h = self.cap_hist.setdefault((self.lifting_key, c[1]), [])
                                    if v not in h:
                                        h.append(v)
                        elif c[0] == 'up':
                            src[i] = ('up', self.lifting_key, c[1])
                    refs = {c[1] for c in st[2][2] if c[0] == 'ref' and not isinstance(c[1], tuple)}
                    if refs:
                        known[CAP] = known.get(CAP, frozenset()) | refs
                        for r in refs:
                            # (a number stays known until something is called: see below)
                            if not (P.is_num(known.get(r)) and not isinstance(known.get(r), bool)):
                                known.pop(r, None)
                if st[0] == 'call' and any(a == ('regs',) for a in st[3]):
                    # the register array itself escaped into a call: anything may have changed
                    known = {CAP: known[CAP]} if CAP in known else {}
                if st[0] == 'call' and not (st[2][0] == 'kfn' and str(st[2][1]).startswith('bit32.')):
                    # a variable shared with a closure can change whenever something is called
                    # (not an opaque state number: closures only scramble those on dead paths)
                    for r in known.get(CAP, ()):
                        v = known.get(r)
                        if not (P.is_num(v) and not isinstance(v, bool) and float(v).is_integer()
                                and abs(v) >= 2 ** 24):
                            known.pop(r, None)
                if st[0] == 'set' and st[1][0] == 'r' and st[1][1] in known.get(CAP, ()):
                    # ... and holds what was just put there until then (numbers only: the opaque
                    # state numbers are such variables)
                    v = num(st[2]) if isinstance(st[2], tuple) else None
                    if v is not None:
                        known[st[1][1]] = v
                        self.cap_vals[(self.lifting_key, st[1][1])] = v
                        h = self.cap_hist.setdefault((self.lifting_key, st[1][1]), [])
                        if v not in h:
                            h.append(v)
                    else:
                        known.pop(st[1][1], None)
                    continue
                if st[0] == 'set' and st[1][0] == 'r':
                    e = st[2]
                    if e[0] == 'arg' and self.param_vals and (e[1] - 1) in self.param_vals:
                        known[st[1][1]] = P.norm(self.param_vals[e[1] - 1])
                    elif e[0] == 'outer' and e[1] in by_path:
                        known[st[1][1]] = by_path[e[1]]
                    elif e[0] == 'k' and (P.is_num(e[1]) or isinstance(e[1], bool)):
                        known[st[1][1]] = e[1]
                    elif e[0] == 'constfn':
                        known[st[1][1]] = P.ConstFn(e[1], e[2])
                    elif e[0] in ('t', 'bin', 'up', 'upcell', 'mv') and num(e) is not None:
                        known[st[1][1]] = num(e)
                    else:
                        known.pop(st[1][1], None)
                elif st[0] == 'setmulti':
                    for n in [n for n in known if n != CAP and (not isinstance(n, int) or n >= st[1])]:
                        del known[n]
            if term[0] == 'br' and term[1][0] == 't' and term[1][1] in truth:
                # (the not-a-number test inside a 32-bit multiply: only one side happens)
                n0 = len(out)
                walk(term[2] if truth[term[1][1]] else term[3], known, temps, calls)
                taken = {tuple(t[1]): kn for t, kn in out[n0:] if t[0] == 'goto'}
                # the other side is still part of the instruction's tree: where it leads must
                # exist, with the registers as the real side leaves them
                n1 = len(out)
                walk(term[3] if truth[term[1][1]] else term[2], known, temps, calls)
                out[n1:] = [(t, taken.get(tuple(t[1]), kn) if t[0] == 'goto' else kn) for t, kn in out[n1:]]
            elif term[0] == 'br' and self.end_state and self.cur_node is not None and live_side(term) is not None:
                # an opaque predicate: only its live side exists (see lift_proto)
                live = live_side(term)
                self.folds[id(term)] = live
                known[ABS] = frozenset(ab.items())
                walk(term[2] if live else term[3], known, temps, calls, tabs)
            elif term[0] == 'br':
                # Inside `if x then`, x is not nil or false.  That alone decides nothing, but
                # Luraph follows such a test with `not not x` and a branch on it, whose dead side
                # applies a decryption layer that the real path never applies.
                reg = torig.get(term[1][1]) if term[1][0] == 't' else None
                for arm, tv in ((term[2], 'Tb'), (term[3], 'Fb')):
                    ab2 = dict(ab)
                    if reg is not None and reg not in known.get(CAP, ()) and ab2.get(reg) is None:
                        ab2[reg] = tv
                    known[ABS] = frozenset(ab2.items())
                    walk(arm, known, temps, calls, tabs)
            elif term[0] == 'forbr':
                for n in ((term[2] + 3,) if term[1] == 'num' else tuple(term[2])):
                    ab.pop(n, None)
                known[ABS] = frozenset(ab.items())
                regs = (term[2] + 3,) if term[1] == 'num' else tuple(term[2])
                for n in regs:
                    known.pop(n, None)
                walk(term[3], known)
                walk(term[4], known)
            elif term[0] == 'goto':
                if ab:
                    known[ABS] = frozenset(ab.items())
                else:
                    known.pop(ABS, None)
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
            if not self.pe.use_upnum and (self.up_nums or self.temp_vals) and (
                    'self-modifying' in str(e) or 'out of range' in str(e) or 'evaluator failure' in str(e)):
                # a key mixed from an opaque number kept in an upvalue: use the value it had
                return self._upnum_retry(interp, node, key2, snap, known, indirect, pkey)
            return str(e)
        finally:
            self.pe.use_numknown = self.constprop
            self.pe.frozen = False
            self.pe.journal = None
            self.pe.protect = None
        if any(not isinstance(end[1][1], int) for end, kn in self.known_after(node.tree, known)):
            if not self.pe.use_upnum and (self.up_nums or self.temp_vals):
                r = self._upnum_retry(interp, node, key2, snap, known, indirect, pkey)
                if r is None:
                    return None
            return SYMPC
        return None

    def _upnum_retry(self, interp, node, key2, snap, known, indirect, pkey):
        """Lift again with the numbers upvalues held at the end of the run (see pe.UpNum)."""
        self.pe.use_upnum = True
        snap2 = dict(snap)
        for name, v in snap.items():
            if isinstance(v, Sym) and isinstance(v.e, tuple) and v.e[0] == 't' and v.e[1] in self.temp_vals:
                snap2[name] = P.KNum(self.temp_vals[v.e[1]])
        try:
            return self.lift_node(interp, node, key2, snap2, known, indirect, pkey, numknown=True)
        finally:
            self.pe.use_upnum = False

    # ---- whole proto
    def lift_proto(self, key):
        self.lifting_key = key
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
            if key2 in pir.nodes and pathmode and pir.nodes[key2].error and relifts.get(('err', key2), 0) < 6 \
                    and (key, key2[0], key2[1]) not in self.oracle:
                # The instruction did not lift on the path that got here first.  That path may have
                # been the dead side of an opaque predicate, which leaves the code in a state no
                # real path has: try again with the state of this one.
                relifts[('err', key2)] = relifts.get(('err', key2), 0) + 1
                del pir.nodes[key2]
                pir.errors = [e for e in pir.errors if e[0] != key2]
            if key2 in pir.nodes and pathmode and not pir.nodes[key2].error:
                # ... also when the failure is a few straight-line instructions further on: they are
                # lifted again from here, on this path
                chain, x = [key2], key2
                while len(chain) <= 8:
                    ends = [e for e in tree_ends(pir.nodes[x].tree)]
                    if len(ends) != 1 or ends[0][0] != 'goto' or ends[0][1] not in pir.nodes or ends[0][1] in chain:
                        break
                    x = ends[0][1]
                    chain.append(x)
                    if pir.nodes[x].error:
                        break
                if pir.nodes[x].error and x != key2 and relifts.get(('err', x), 0) < 6 \
                        and (key, x[0], x[1]) not in self.oracle:
                    relifts[('err', x)] = relifts.get(('err', x), 0) + 1
                    for c in chain:
                        del pir.nodes[c]
                    pir.errors = [e for e in pir.errors if e[0] not in chain]
            if key2 in pir.nodes:
                node = pir.nodes[key2]
                if self.constprop:
                    # constant propagation: a register is only "known" at an instruction if every
                    # path agrees on it; when a later path disagrees the instruction is re-lifted
                    old = node_in[key2]
                    meet = {r: v for r, v in old.items()
                            if r != CAP and r in known and same_const(known[r], v)}
                    meet.pop(ABS, None)
                    both = old.get(ABS, frozenset()) & known.get(ABS, frozenset())
                    if both:
                        meet[ABS] = both
                    cap = old.get(CAP, frozenset()) | known.get(CAP, frozenset())
                    for r in cap:
                        meet.pop(r, None)
                    if cap:
                        meet[CAP] = cap
                    def firm(d):
                        # (what a branch implies is not a reason to lift an instruction again)
                        return frozenset(x for x in d.get(ABS, ()) if x[1][1:2] != 'b')
                    if len({r for r in meet if r != ABS}) == len({r for r in old if r != ABS}) \
                            and meet.get(CAP) == old.get(CAP) and firm(meet) == firm(old):
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
            rec = self.oracle.get((key, key2[0], key2[1])) if pathmode else None
            if rec and not rec.get('p'):
                # an interpreter local that holds a run-time number here (a key taken from an opaque
                # parameter): the traced run shows which.  Kept for the instructions that did not
                # run and need it (see _upnum_retry).
                for name, v in rec['v'].items():
                    cur = snap.get(name)
                    if isinstance(cur, Sym) and isinstance(cur.e, tuple) and cur.e[0] == 't' and P.is_num(v):
                        self.temp_vals.setdefault(cur.e[1], P.norm(v))
            node_in[key2] = known
            keep = self.opaque_in.get((key, key2)) if pathmode else None
            if keep and any(r not in known and r not in known.get(CAP, ()) for r in keep):
                # An instruction that consumed opaque state numbers (a guard, a decryption step)
                # sees the same numbers on every real path.  Where a later path arrives without
                # knowing them (a join with something that lost track), it gets them back.
                known = dict(known)
                for r, v in keep.items():
                    if r not in known and r not in known.get(CAP, ()):
                        known[r] = v
            node = Node(key2)
            node.snap_in = snap
            pir.nodes[key2] = node
            self.pe.known_reads = node.kreads = set()
            self.pe.write_log = wlog = [] if ppath and not self.end_state else None
            err = self.lift_node(interp, node, key2, snap, known, indirect, key)
            self.pe.write_log = None
            if pathmode and err is None and node.kreads and node.tree is not None and all(
                    st[0] == 'set' and st[1][0] == 'r' and st[2][0] == 'k' and isinstance(st[2][1], bool)
                    for st in node.tree[0]) and node.tree[1][0] == 'goto':
                big = {r: known[r] for r in node.kreads if isinstance(r, int) and P.is_num(known.get(r))
                       and not isinstance(known.get(r), bool) and abs(known[r]) >= 2 ** 24}
                if big:
                    self.opaque_in.setdefault((key, key2), {}).update(big)
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
            if err is not None and hint is not None and hint.get('final') and self.end_state:
                # Still not liftable, but the instruction ran: its operands as they were when it
                # last executed are on record.  (They were put there by a patch whose key the
                # lifter could not follow.)
                saved = []
                arrays = self.records[key - 1][1]

                def poke(t, k, val):
                    if isinstance(t, LuaTable) and t.h.get(k) != val:
                        saved.append((t, k, t.h.get(k)))
                        t.h[k] = val
                # what the instruction changed in its function's code when it first ran (a decryption
                # step whose key the lifter could not follow) ...
                info = (self.dump.data.get('snaps') or {}).get(str(key))
                sv = hint.get('sv')
                if info and isinstance(sv, int) and sv < len(info['vers']):
                    for ti, k, v in info['vers'][sv]:
                        poke(arrays.get(info['names'][ti - 1]), int(k), P.norm(v))
                # ... and its own operands as they were when it last executed
                for part in hint['final'].split():
                    name, _, val = part.partition('=')
                    try:
                        poke(arrays.get(name), key2[1], P.norm(float(val)))
                    except ValueError:
                        continue
                if saved:
                    node2 = Node(key2)
                    node2.snap_in = snap
                    self.pe.known_reads = node2.kreads = set()
                    err2 = self.lift_node(interp, node2, key2, snap, known, indirect, key)
                    if err2 is None:
                        node = node2
                        pir.nodes[key2] = node
                        err = None
                        pir.oracle_used.add(key2)
                        self.journals.setdefault(key2[:2], [])[:0] = saved
                    else:
                        self.pe.known_reads = node.kreads
                        for t, k, old in reversed(saved):
                            if old is None:
                                t.h.pop(k, None)
                            else:
                                t.h[k] = old
                    if os.environ.get('DEVM_TRACE_ERR'):
                        print('RECORDED state', key, key2, 'resolved' if err2 is None else err2)
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
            self.folds = {}
            self.cur_node = (key, key2)
            ends = self.known_after(node.tree, known)
            if self.folds and pathmode:
                node.tree = fold_tree(node.tree, self.folds)
                self.folds = {}
                self.cur_node = None
                ends = self.known_after(node.tree, known)
            self.cur_node = None
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


def fold_tree(tree, folds):
    """Copy of an instruction tree with decided branches (by identity of the `br`) replaced by their live side."""
    stmts, term = tree
    if term[0] == 'br':
        if id(term) in folds:
            s2, t2 = fold_tree(term[2] if folds[id(term)] else term[3], folds)
            return (list(stmts) + [x for x in s2 if x != ('cold',)], t2)
        return (stmts, ('br', term[1], fold_tree(term[2], folds), fold_tree(term[3], folds)))
    if term[0] == 'forbr':
        return (stmts, ('forbr', term[1], term[2], fold_tree(term[3], folds), fold_tree(term[4], folds)))
    return tree


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

    def lift_all(self, root_key, redo_into=None, param_vals=None):
        """Every proto is lifted against a pristine copy of the dumped VM state.
        (redo_into: protos already lifted, of which root_key is lifted again with the opaque
        numbers its callers pass, param_vals = {proto: {parameter index: number}}.)

        Lifting evaluates instructions that modify state shared by all protos (one-shot decryption
        of pooled constants inside the bytecode buffer).  A proto that is still partly encrypted
        (hash-keyed code that has not run yet) lifts garbage on those paths, and garbage writes
        must not leak into the constants of the other protos."""
        queue = [root_key]
        done = dict(redo_into or ())
        param_vals = param_vals or {}
        while queue:
            k = queue.pop(0)
            if k in done and k != root_key:
                continue
            def attempt(pick=None):
                fresh = FactoryLifter(self.model, P.Dump(self.dump.data, self.model), self.log)
                fresh.param_vals = dict(param_vals.get(k) or ())
                fresh.dump.server = self.dump.server
                fresh.pe.keyfn_override = self.pe.keyfn_override
                fresh.oracle = self.oracle
                fresh.end_state = self.end_state
                fresh.entry_override = self.entry_override
                fresh.cap_src, fresh.cap_vals, fresh.all_up_nums = self.cap_src, self.cap_vals, self.all_up_nums
                fresh.cap_hist = self.cap_hist
                fresh.cap_pick = pick or {}
                return fresh.lift_proto(k)

            pir = attempt()
            if pir.errors and not any((k, e[0][0], e[0][1]) in (self.oracle or ()) for e in pir.errors):
                # A function that never ran, lifted with the opaque numbers its creator shares
                # with it.  The creator rewrites such a variable several times and which value
                # the function sees depends on when it is called: try the others.
                tries = 0
                for src in list(self.cap_src.get(k, {}).values()):
                    if src[0] != 'reg' or not pir.errors:
                        continue
                    reg = (src[1], src[2])
                    for v in list(self.cap_hist.get(reg, ())):
                        if v == self.cap_vals.get(reg) or tries >= 8 or not pir.errors:
                            continue
                        tries += 1
                        alt = attempt({reg: v})
                        if len(alt.errors) < len(pir.errors):
                            pir = alt
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
        # numbers held by closed upvalues at the end of the run (see pe.UpNum)
        self.up_nums = {}
        c0, c1 = self.model.cells
        for i, cell in (pe.upvals_list or {}).items():
            if isinstance(i, int) and isinstance(cell, LuaTable):
                box = cell.h.get(c0)
                v = box.h.get(cell.h.get(c1)) if isinstance(box, LuaTable) else None
                if P.is_num(v) and float(v).is_integer() and abs(v) >= 65536:
                    self.up_nums[i - self.model.up_base] = P.UpNum(v)
            elif isinstance(i, int) and P.is_num(cell) and not isinstance(cell, bool) \
                    and float(cell).is_integer() and abs(cell) >= 65536:
                self.up_nums[i - self.model.up_base] = P.UpNum(cell)       # (captured by value)
        # ... and, for a function that was never created in the run, the numbers its creator would
        # have handed it
        for i, src in self.cap_src.get(key, {}).items():
            if i in self.up_nums:
                continue
            if src[0] == 'num':
                v = src[1]
            elif src[0] == 'reg':
                v = self.cap_pick.get((src[1], src[2]), self.cap_vals.get((src[1], src[2])))
            else:
                v = self.all_up_nums.get(src[1], {}).get(src[2])
            if v is not None and P.is_num(v) and float(v).is_integer() and abs(v) >= 65536:
                self.up_nums[i] = P.UpNum(v)
        self.all_up_nums[key] = dict(self.up_nums)
        for i, p in enumerate(mid.params):
            sc2.vars[p] = None
        if up is not None:
            sc2.vars[mid.params[up]] = UPVALS
        r = pe.exec_block(mid.body, sc2)
        if not r or r[0] != 'return' or not isinstance(r[1][0], LFn):
            raise PEError('wrapper did not return the interpreter closure')
        return r[1][0]
