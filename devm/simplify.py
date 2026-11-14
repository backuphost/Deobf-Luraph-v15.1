"""Whole-program clean-up on the register-level CFGs (after unflattening, before naming).

Luraph hides constants behind a table that is passed to the script body, caches every library
function in a local, and leaves dead assignments from its dissolved state machines.  The passes
here undo that:

  * known values flow from a function into the closures it creates (through upvalues) and into a
    closure called with a constant table;
  * locals that only ever hold a constant or a library function are replaced by that value;
  * assignments nothing reads are removed.
"""
import copy

from .ircfg import map_expr, map_stmt, walk_expr, stmt_exprs, expr_reads

STD_LIBS = {'string', 'table', 'math', 'bit32', 'buffer', 'coroutine', 'task', 'os', 'utf8', 'debug', 'vector'}
STD_FUNCS = {'assert', 'error', 'gcinfo', 'getfenv', 'getmetatable', 'next', 'newproxy', 'print', 'rawequal',
             'rawget', 'rawset', 'rawlen', 'select', 'setfenv', 'setmetatable', 'tonumber', 'tostring',
             'type', 'typeof', 'ipairs', 'pairs', 'pcall', 'xpcall', 'unpack', 'require', 'loadstring',
             'warn', 'tick', 'time', 'wait', 'spawn', 'delay'}


def is_lib(e):
    """A library table: string, math, ... or a Roblox datatype library / Enum (capitalised global)."""
    if e[0] == 'global':
        return e[1] in STD_LIBS or (e[1][:1].isupper() and e[1] not in ('_G', '_VERSION'))
    if e[0] == 'index' and e[2][0] == 'k' and isinstance(e[2][1], str):
        # Enum.KeyCode.X style chains are constants as well
        return e[1][0] in ('global', 'index') and is_lib(e[1]) and _root_global(e) == 'Enum'
    return False


def _root_global(e):
    while e[0] == 'index':
        e = e[1]
    return e[1] if e[0] == 'global' else None


def is_stable(e, assigned_globals):
    """Expressions that can stand in for a local holding them: constants, globals nobody assigns,
    and fields of library tables."""
    t = e[0]
    if t == 'k':
        return not isinstance(e[1], str) or len(e[1]) <= 200
    if t == 'kfn':
        return True
    if t == 'global':
        return e[1] not in assigned_globals
    if t == 'index':
        return e[2][0] == 'k' and isinstance(e[2][1], str) and is_lib(e[1]) and _root_global(e) not in assigned_globals
    return False


def is_pure(e):
    """No side effects and cannot fail: safe to delete when the result is unused."""
    for x in walk_expr(e, []):
        t = x[0]
        if t in ('k', 'kfn', 'r', 'up', 'global', 'varargn', 'arg', 'env', 'newtable', 'closure', 'selfarg',
                 'nargs', 'ktable'):
            continue
        if t == 'table':
            if x[2] is not None:
                return False
            continue
        if t == 'index':
            if x[2][0] == 'k' and (is_lib(x[1]) or x[1][0] == 'ktable'):
                continue
            return False
        if t in ('and', 'or'):
            continue
        if t == 'un' and x[1] == 'not':
            continue
        return False
    return True


def rewrite(cfg, fn):
    for b in cfg.blocks.values():
        b.stmts = [map_stmt(st, fn) for st in b.stmts]
        b.term = map_stmt(b.term, fn)


def all_reads(cfg):
    for b in cfg.blocks.values():
        for st in b.stmts + [b.term]:
            for e in stmt_exprs(st)[0]:
                yield e


def closures_in(cfg):
    for e in all_reads(cfg):
        for x in walk_expr(e, []):
            if x[0] == 'closure':
                yield x


def reg_defs(cfg):
    """register -> number of statements that write it"""
    n = {}
    for b in cfg.blocks.values():
        for st in b.stmts + [b.term]:
            for w in stmt_exprs(st)[1]:
                if w[0] == 'r':
                    n[w[1]] = n.get(w[1], 0) + 1
    return n


class Program:
    def __init__(self, cfgs, root):
        self.cfgs = cfgs
        self.root = root
        self.sites = {}         # proto -> [(parent proto, caps)]
        self.order = []
        self._scan()

    def _scan(self):
        self.sites = {}
        for key, cfg in self.cfgs.items():
            for c in closures_in(cfg):
                self.sites.setdefault(c[1], []).append((key, c[2]))
        # parents before children
        seen = set()
        order = []
        work = [self.root]
        while work:
            k = work.pop(0)
            if k in seen or k not in self.cfgs:
                continue
            seen.add(k)
            order.append(k)
            for c in closures_in(self.cfgs[k]):
                work.append(c[1])
        self.order = order

    def assigned_globals(self):
        out = set()
        for cfg in self.cfgs.values():
            for b in cfg.blocks.values():
                for st in b.stmts:
                    if st[0] == 'set' and st[1][0] == 'global':
                        out.add(st[1][1])
                    elif st[0] == 'setindex' and st[1][0] == 'env':
                        out.add('*')
        return out

    def written_upvalues(self):
        """proto -> upvalue indices it (or a closure it creates) assigns"""
        wr = {k: set() for k in self.cfgs}
        for k, cfg in self.cfgs.items():
            for b in cfg.blocks.values():
                for st in b.stmts:
                    if st[0] == 'set' and st[1][0] == 'up':
                        wr[k].add(st[1][1])
        changed = True
        while changed:
            changed = False
            for k, cfg in self.cfgs.items():
                for c in closures_in(cfg):
                    child = wr.get(c[1])
                    if not child:
                        continue
                    for i, cap in enumerate(c[2]):
                        if cap[0] == 'up' and i in child and cap[1] not in wr[k]:
                            wr[k].add(cap[1])
                            changed = True
        return wr

    def ref_written_regs(self, key, wr):
        """registers of proto `key` that a closure it creates may assign through a reference"""
        out = set()
        for c in closures_in(self.cfgs[key]):
            child = wr.get(c[1], ())
            for i, cap in enumerate(c[2]):
                if cap[0] == 'ref' and i in child:
                    out.add(cap[1])
        return out

    # ------------------------------------------------------------------ alias propagation
    def propagate(self):
        """Replace locals/upvalues that only ever hold a stable value by that value."""
        ag = self.assigned_globals()
        if '*' in ag:
            return 0
        wr = self.written_upvalues()
        known_up = {}
        total = 0
        for key in self.order:
            cfg = self.cfgs[key]
            ups = known_up.get(key, {})
            count = [0]
            if ups:
                def sub_up(e, ups=ups):
                    if e[0] == 'up' and e[1] in ups:
                        count[0] += 1
                        return ups[e[1]]
                    return e
                rewrite(cfg, sub_up)
            stable = self._stable_regs(cfg, key, ag, wr)
            if stable:
                def sub_reg(e, stable=stable):
                    if e[0] == 'r' and e[1] in stable:
                        count[0] += 1
                        return stable[e[1]]
                    return e
                rewrite(cfg, sub_reg)
            total += count[0]
            for c in closures_in(cfg):
                child = c[1]
                if len(self.sites.get(child, ())) != 1 or child not in self.cfgs:
                    continue
                cw = wr.get(child, ())
                d = known_up.setdefault(child, {})
                for i, cap in enumerate(c[2]):
                    if i in cw:
                        continue
                    if cap[0] in ('ref', 'val') and cap[1] in stable:
                        d[i] = stable[cap[1]]
                    elif cap[0] == 'up' and cap[1] in ups:
                        d[i] = ups[cap[1]]
        return total

    def _stable_regs(self, cfg, key, ag, wr):
        entry = cfg.blocks[cfg.entry]
        cfg.compute_preds()
        if entry.preds:
            return {}
        defs = reg_defs(cfg)
        bad = self.ref_written_regs(key, wr)
        stable = {}
        read = set()
        for st in entry.stmts:
            reads, writes = stmt_exprs(st)

            def sub(e):
                if e[0] == 'r' and e[1] in stable:
                    return stable[e[1]]
                return e
            if st[0] == 'set' and st[1][0] == 'r':
                r = st[1][1]
                e = map_expr(st[2], sub)
                if (defs.get(r) == 1 and r not in read and r not in bad and not (isinstance(r, int) and r <= cfg.nparams)
                        and is_stable(e, ag)):
                    stable[r] = e
            for e in reads:
                read |= expr_reads(e)[0]
        return stable

    # ------------------------------------------------------------------ constant table argument
    def inline_const_table(self):
        """`local T = {consts..., [k] = function(p) ... end}; return T[k](T)` -> the function body
        with every p[c] replaced by its constant."""
        root = self.cfgs[self.root]
        entry = root.blocks[root.entry]
        cand = None
        defined = set()
        for i, st in enumerate(entry.stmts):
            if st[0] == 'set' and st[1][0] == 'r' and st[2][0] == 'table' and st[2][2] is None and len(st[2][1]) >= 4:
                cand = (i, st, set(defined))
            for w in stmt_exprs(st)[1]:
                if w[0] == 'r':
                    defined.add(w[1])
        if cand is None:
            return False
        idx, st, defined = cand
        treg = st[1]
        items = {}
        fkey = None
        for k, v in st[2][1]:
            if k is None or k[0] != 'k':
                return False
            if v[0] == 'closure':
                if fkey is not None:
                    return False
                fkey = (k, v)
                continue
            if v[0] == 'r':
                if v[1] in defined:
                    continue        # a real local: leave accesses to this slot alone
                v = ('k', None)     # never assigned: nil
            elif not is_pure(v):
                continue
            items[k[1]] = v
        if fkey is None:
            return False
        body_key = fkey[1][1]
        if len(self.sites.get(body_key, ())) != 1 or body_key not in self.cfgs:
            return False
        # the table must only be used as  T[fkey](T)
        call = ('call', ('index', treg, fkey[0]), [treg], None)
        uses = 0
        for b in root.blocks.values():
            for s2 in b.stmts + [b.term]:
                for e in stmt_exprs(s2)[0]:
                    for x in walk_expr(e, []):
                        if x == treg:
                            uses += 1
        ncalls = sum(1 for e in all_reads(root) for x in walk_expr(e, []) if x == call)
        if ncalls != 1 or uses != 2:
            return False
        trial = copy.deepcopy(self.cfgs)
        marker = ('ktable', 0)
        state = {'ok': True}

        def subst(cfg, target):
            """Replace `target` by the table inside cfg; fail on any use other than T[const]."""
            def fn(e):
                if e[0] == 'index' and e[1] == target and e[2][0] == 'k' and e[2][1] in items:
                    return items[e[2][1]]
                return e
            rewrite(cfg, fn)
            for e in all_reads(cfg):
                for x in walk_expr(e, []):
                    if x == target:
                        state['ok'] = False
            for b in cfg.blocks.values():
                for s2 in b.stmts:
                    if target in stmt_exprs(s2)[1]:
                        state['ok'] = False

        def descend(key, target):
            cfg = trial[key]
            # closures capturing the table get it as an upvalue
            for c in list(closures_in(cfg)):
                for i, cap in enumerate(c[2]):
                    hit = (target[0] == 'r' and cap[0] in ('ref', 'val') and cap[1] == target[1]) or \
                          (target[0] == 'up' and cap[0] == 'up' and cap[1] == target[1])
                    if hit:
                        if len(self.sites.get(c[1], ())) != 1 or c[1] not in trial:
                            state['ok'] = False
                            return
                        descend(c[1], ('up', i))
            subst(cfg, target)

        body = trial[body_key]
        if body.nparams < 1:
            return False
        descend(body_key, ('r', 1))
        if not state['ok']:
            return False
        # root: drop the table, call the function directly
        troot = trial[self.root]
        tentry = troot.blocks[troot.entry]
        del tentry.stmts[idx]
        clo = fkey[1]

        def fix_call(e):
            if e == call:
                return ('call', clo, [], None)
            return e
        rewrite(troot, fix_call)
        if body.nparams == 1:
            body.nparams = 0
        self.cfgs.clear()
        self.cfgs.update(trial)
        self._scan()
        return True

    # ------------------------------------------------------------------ unused captures
    def prune_captures(self):
        """Stop capturing variables the closure no longer mentions (their uses were replaced by
        constants), so the variable itself can go."""
        used = {}
        for k, cfg in self.cfgs.items():
            u = set()
            for b in cfg.blocks.values():
                for st in b.stmts + [b.term]:
                    reads, writes = stmt_exprs(st)
                    for w in writes:
                        if w[0] == 'up':
                            u.add(w[1])
                    for e in reads:
                        for x in walk_expr(e, []):
                            if x[0] == 'up':
                                u.add(x[1])
            used[k] = u
        changed = True
        while changed:
            changed = False
            for k, cfg in self.cfgs.items():
                for c in closures_in(cfg):
                    cu = used.get(c[1])
                    if cu is None:
                        continue
                    for i, cap in enumerate(c[2]):
                        if cap[0] == 'up' and i in cu and cap[1] not in used[k]:
                            used[k].add(cap[1])
                            changed = True
        n = [0]
        for k, cfg in self.cfgs.items():
            def fn(e):
                if e[0] == 'closure' and e[1] in used:
                    cu = used[e[1]]
                    caps = tuple(c if (i in cu or c[0] == 'pad') else ('pad', 0) for i, c in enumerate(e[2]))
                    if caps != e[2]:
                        n[0] += 1
                        return ('closure', e[1], caps)
                return e
            rewrite(cfg, fn)
        return n[0]

    # ------------------------------------------------------------------ dead stores
    def dead_stores(self):
        n = 0
        for cfg in self.cfgs.values():
            n += dead_stores(cfg)
        return n

    def run(self):
        self.dead_stores()
        self.inline_const_table()
        for _ in range(12):
            a = self.propagate()
            c = self.prune_captures()
            b = self.dead_stores()
            if not a and not b and not c:
                break
        for cfg in self.cfgs.values():
            cfg.refold()


def dead_stores(cfg):
    """Remove pure assignments to registers nothing reads afterwards."""
    from .ircfg import cap_step
    removed = 0
    cap = cfg.captured_ref
    changed = True
    while changed:
        changed = False
        live_in, live_out = cfg.liveness()
        cin = cfg.captured_open() if cap else {}
        for bid, b in cfg.blocks.items():
            live = set(live_out[bid])
            for e in stmt_exprs(b.term)[0]:
                live |= expr_reads(e)[0]
            if b.term[0] == 'forbr':
                live -= set(b.term[2])
            # registers whose by-reference capture is open before each statement: a store to one
            # of those is visible to the closure even when this function never reads it again
            opened = []
            cur = set(cin.get(bid, ()))
            for st in b.stmts:
                opened.append(cur)
                cur = cap_step(cur, st)
            out = []
            for i in range(len(b.stmts) - 1, -1, -1):
                st = b.stmts[i]
                reads, writes = stmt_exprs(st)
                if st[0] == 'set' and st[1][0] == 'r' and st[1][1] not in live and st[1][1] not in opened[i] \
                        and is_pure(st[2]) and not _captures_self(st):
                    removed += 1
                    changed = True
                    continue
                for w in writes:
                    if w[0] == 'r':
                        live.discard(w[1])
                for e in reads:
                    live |= expr_reads(e)[0]
                out.append(st)
            out.reverse()
            b.stmts = out
    return removed


def _captures_self(st):
    e = st[2]
    return e[0] == 'closure' and any(c[0] == 'ref' and c[1] == st[1][1] for c in e[2])


def drop_unused(cfg):
    """After naming: remove pure assignments to variables that are never read."""
    removed = 0
    while True:
        used = set()
        for b in cfg.blocks.values():
            for st in b.stmts + [b.term]:
                for e in stmt_exprs(st)[0]:
                    for x in walk_expr(e, []):
                        if x[0] == 'r':
                            used.add(x[1])
            if b.term[0] == 'forbr':
                used |= set(b.term[2])
        n = 0
        for b in cfg.blocks.values():
            keep = []
            for st in b.stmts:
                if st[0] == 'set' and st[1][0] == 'r' and st[1][1] not in used and is_pure(st[2]):
                    n += 1
                    continue
                keep.append(st)
            b.stmts = keep
        removed += n
        if not n:
            return removed


def simplify(cfgs, root):
    Program(cfgs, root).run()
