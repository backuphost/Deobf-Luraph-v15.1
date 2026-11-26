"""
Loop recognition for the devirtualizer, on the raw CFG (before expression
cleanup), where Luraph's loop protocol is still visible:

numeric for (FORPREP / FORLOOP handlers):
    prep:    Z_d = step + 0 ; P_d = limit + 0 ; a_d = start - Z_d ; goto H
    H:       a_d = a_d + Z_d
             if Z_d <= 0 then (if a_d >= P_d then rX = a_d; goto BODY else goto EXIT)
                         else (if a_d <= P_d then rX = a_d; goto BODY else goto EXIT)
generic for (coroutine-driven iterator):
    prep:    a_d = geniter(vm, f, s, ctl) ; goto H
    H:       T = a_d() ; if T[1] then rA = T[2]; rB = T[3]; goto BODY else goto EXIT

Both become a block of kind "for"/"forin" whose successors are [BODY, EXIT];
the structurer turns it into a `for` statement. Pseudo loop-state
assignments left over (saving/restoring the VM's loop stack) are dropped.

numeric for compiled to plain instructions (TABLE_FOR, Luraph v15.2): the
loop state is a table in an ordinary register, see try_table_numeric; it is
rewritten into the protocol above first.
"""
from luasym import Const, Reg, Pseudo, Bin, Un, Index, NewTable, TempVal, ClosureExpr, Multi

# recognize numeric for loops whose state is a table in a register (a front
# end switches it on: Luraph v15.2)
TABLE_FOR = False


def is_pseudo(e, name=None, depth=None):
    return isinstance(e, Pseudo) and (name is None or e.name == name) and (depth is None or e.depth == depth)


def recognize(entry, blocks, D):
    found = {"for": 0, "forin": 0}
    if TABLE_FOR:
        # innermost first: an outer loop is only taken once the loops in its
        # body no longer use the state register
        again = True
        while again:
            again = False
            for h in list(blocks.values()):
                if h.id in blocks and try_table_numeric(h, blocks, D, entry):
                    again = True
    for h in list(blocks.values()):
        if h.id not in blocks:
            continue
        if try_numeric(h, blocks, D):
            found["for"] += 1
        elif try_generic(h, blocks, D):
            found["forin"] += 1
        else:
            _run_once_forin(h, blocks, D)
    # drop VM loop bookkeeping (LPH_JIT loop variables stay while a loop
    # that was not recognized still reads them)
    read = set()
    for b in blocks.values():
        todo = [b.cond, getattr(b, "values", None)]
        for s in b.stmts:
            todo += [x for x in s.__dict__.values() if not isinstance(x, Pseudo)]
        while todo:
            x = todo.pop()
            if isinstance(x, Pseudo):
                read.add((x.name, x.depth))
            elif isinstance(x, (list, tuple)):
                todo += list(x)
            elif hasattr(x, "__dict__") and not isinstance(x, type):
                todo += list(x.__dict__.values())
    for b in blocks.values():
        b.stmts = [s for s in b.stmts if not (isinstance(s, D.Assign) and isinstance(s.target, Pseudo)
                                              and not (s.target.name.startswith("JIT")
                                                       and (s.target.name, s.target.depth) in read))]
    return found


def find_prep(h, blocks, D, names, depth):
    """Predecessor blocks of h (outside the loop) that assign the pseudo prep values."""
    preps = []
    for p in h.preds:
        pb = blocks[p]
        vals = {}
        for s in pb.stmts:
            if isinstance(s, D.Assign) and isinstance(s.target, Pseudo) and s.target.depth == depth:
                vals[s.target.name] = s.value
        if all(n in vals for n in names):
            preps.append((pb, vals))
    return preps


def _pure_moves(pre, D):
    return all(isinstance(s, D.Assign) and isinstance(s.target, (Reg, Pseudo))
               and isinstance(s.value, (Const, Reg, Pseudo)) for s in pre)


def _moves_to_latches(h, pb, pre, blocks):
    """Register moves at the loop head (before the iterator call / counter
    step) run before every header evaluation: once after the prep and at the
    end of every iteration that continues (a new block per back edge, so
    conditional latches work too)."""
    import copy
    import structure
    for lb in [blocks[p] for p in h.preds if p != pb.id]:
        nb = structure.Block(max(blocks) + 1)
        nb.stmts = [copy.copy(s) for s in pre]
        nb.kind = "goto"
        nb.succ = [h.id]
        nb.origin = lb.origin
        blocks[nb.id] = nb
        lb.succ = [nb.id if x == h.id else x for x in lb.succ]
    _recompute(blocks)


def try_numeric(h, blocks, D):
    if h.kind != "cond" or not h.stmts:
        return False
    # register resets Luraph puts before the counter step (`r2 = nil`)
    pre = h.stmts[:-1]
    s = h.stmts[-1]
    if not (isinstance(s, D.Assign) and is_pseudo(s.target) and isinstance(s.value, Bin) and s.value.op == "Add"
            and is_pseudo(s.value.a, s.target.name, s.target.depth) and is_pseudo(s.value.b)):
        return False
    if not _pure_moves(pre, D):
        # a loop whose body never comes back (it always returns): no back
        # edge, so the prep and the header are one straight-line block. It
        # runs at most once: plain ifs on the prep values, not a `for` (the
        # structurer copies a latch-less for loop without end)
        return _run_once_for(h, s, blocks, D)
    a, z = s.target, s.value.b
    d = a.depth
    c = h.cond
    if not (isinstance(c, Bin) and c.op == "CompareLe" and is_pseudo(c.a, z.name, d) and isinstance(c.b, Const)
            and c.b.v == 0):
        return False
    body = exit_ = var = None
    for side in h.succ:
        sb = blocks[side]
        if sb.kind != "cond" or sb.stmts:
            return False
        cc = sb.cond
        if not (isinstance(cc, Bin) and cc.op in ("CompareGe", "CompareLe") and is_pseudo(cc.a, a.name, d)
                and is_pseudo(cc.b)):
            return False
        lim = cc.b
        t, e = [blocks[x] for x in sb.succ]
        if not (t.kind == "goto" and len(t.stmts) == 1 and isinstance(t.stmts[0], D.Assign)
                and isinstance(t.stmts[0].target, Reg) and is_pseudo(t.stmts[0].value, a.name, d)):
            return False
        if var is not None and (var != t.stmts[0].target.n or body != t.succ[0] or exit_ != e.id):
            return False
        var, body, exit_ = t.stmts[0].target.n, t.succ[0], e.id
    preps = find_prep(h, blocks, D, (z.name, lim.name, a.name), d)
    if len(preps) > 1:
        names = (z.name, lim.name, a.name)
        nb = join_preps(h, blocks, [pb for pb, _ in preps],
                        lambda x: isinstance(x, D.Assign) and is_pseudo(x.target, None, d) and x.target.name in names)
        if nb is not None:
            preps = find_prep(h, blocks, D, names, d)
    if len(preps) != 1:
        return False
    pb, vals = preps[0]
    start = vals[a.name]
    step = strip_plus0(vals[z.name])
    limit = strip_plus0(vals[lim.name])
    # start - Z  ->  start (LPH_JIT loops compute start - step before the
    # push: the step's expression, or both folded to constants)
    if isinstance(start, Bin) and start.op == "Sub" and (is_pseudo(start.b, z.name, d) or
                                                        _same(start.b, step)):
        start = start.a
    elif isinstance(start, Const) and isinstance(step, Const) and _num(start.v) and _num(step.v):
        start = Const(start.v + step.v)
    else:
        return False
    for side in h.succ:
        sb = blocks[side]
        for x in sb.succ:
            blocks.pop(x, None) if blocks.get(x) is not None and blocks[x].kind == "goto" and \
                blocks[x].succ == [body] else None
        blocks.pop(side, None)
    h.stmts = []
    h.kind = "for"
    h.cond = None
    h.succ = [body, exit_]
    lst = LoopExprs([start, limit, step])
    h.values = (var, lst)
    place_prep(pb, lst, D, lambda s: isinstance(s, D.Assign) and isinstance(s.target, Pseudo)
               and s.target.depth == d and s.target.name in (z.name, lim.name, a.name))
    _recompute(blocks)
    if pre:
        import copy
        _moves_to_latches(h, pb, pre, blocks)
        pb.stmts += [copy.copy(x) for x in pre]
    return True


def _run_once_for(h, s, blocks, D):
    """try_numeric's header with the prep in the same block: substitute the
    prep values (start, limit, step) for the VM's loop slots in the header
    and its two compare blocks. Returns False (not a `for`)."""
    d = s.target.depth
    vals = {x.target.name: x.value for x in h.stmts[:-1]
            if isinstance(x, D.Assign) and is_pseudo(x.target, None, d)}
    a, z = s.target.name, s.value.b.name
    if a not in vals or z not in vals:
        return False
    step, start = strip_plus0(vals[z]), vals[a]
    if not (isinstance(start, Bin) and start.op == "Sub" and is_pseudo(start.b, z, d)):
        return False
    sub = {nm: strip_plus0(v) for nm, v in vals.items()}
    sub[a], sub[z] = start.a, step
    import codegen

    def fn(x):
        return sub.get(x.name) if isinstance(x, Pseudo) and x.depth == d and x.name in sub else None
    h.stmts = [x for x in h.stmts[:-1] if not (isinstance(x, D.Assign) and is_pseudo(x.target, None, d))]
    h.cond = codegen.map_expr(h.cond, fn)
    for side in h.succ:
        sb = blocks[side]
        if sb.cond is not None:
            sb.cond = codegen.map_expr(sb.cond, fn)
        for t in sb.succ:
            for st in blocks[t].stmts:
                if isinstance(st, D.Assign):
                    st.value = codegen.map_expr(st.value, fn)
    def const_of(x):
        # a register the block set to a constant (`r9 = 182`) before the prep
        if isinstance(x, Reg):
            for st in reversed(h.stmts):
                if isinstance(st, D.Assign) and isinstance(st.target, Reg) and st.target.n == x.n:
                    return st.value if isinstance(st.value, Const) else None
        return x
    cmp = blocks[h.succ[0]].cond
    lim, step, first = const_of(getattr(cmp, "b", None)), const_of(step), const_of(start.a)
    if all(isinstance(x, Const) and _num(x.v) for x in (first, step, lim)):
        # constant bounds: whether it runs is known (`for i = 1, 182`)
        side = blocks[h.succ[0] if step.v <= 0 else h.succ[1]]
        runs = first.v >= lim.v if step.v <= 0 else first.v <= lim.v
        h.kind, h.cond, h.succ = "goto", None, [side.succ[0] if runs else side.succ[1]]
        _recompute(blocks)
    return False


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# --------------------------------------------------------------------------
# numeric for with its state in a table register (Luraph v15.2)

def _regs(x):
    """Numbers of the registers read anywhere inside IR x."""
    out = set()
    st = [x]
    while st:
        v = st.pop()
        if isinstance(v, Reg):
            out.add(v.n)
        elif isinstance(v, (list, tuple)):
            st += list(v)
        elif isinstance(v, ClosureExpr):
            st += list(v.upvals)
        elif hasattr(v, "__dict__") and not isinstance(v, type):
            st += list(v.__dict__.values())
    return out


def _stmt_rw(s, D):
    """(registers a statement reads, the register it writes or None)."""
    if isinstance(s, D.Assign):
        if isinstance(s.target, Reg):
            return _regs(s.value), s.target.n
        return _regs(s.value) | _regs(s.target), None
    if isinstance(s, D.Close):
        return ({s.reg} if isinstance(s.reg, int) else _regs(s.reg)), None
    return _regs(list(s.__dict__.values())), None


def _block_tail_regs(b):
    out = _regs(b.cond) if b.cond is not None else set()
    if b.kind == "ret" and b.values is not None:
        out |= _regs(b.values)
    return out


def _live(blocks, start, reg, D):
    """Is register `reg` read before it is written on some path from block `start`?"""
    seen = set()
    st = [start]
    while st:
        x = st.pop()
        if x in seen or x not in blocks:
            continue
        seen.add(x)
        b = blocks[x]
        dead = False
        for s in b.stmts:
            rd, wr = _stmt_rw(s, D)
            if reg in rd:
                return True
            if wr == reg:
                dead = True
                break
        if dead:
            continue
        if reg in _block_tail_regs(b):
            return True
        st += b.succ
    return False


def _last_def(stmts, upto, reg, D):
    """(index, value) of the last assignment to register `reg` in stmts[:upto]."""
    for i in range(upto - 1, -1, -1):
        s = stmts[i]
        if isinstance(s, D.Assign) and isinstance(s.target, Reg) and s.target.n == reg:
            return i, s.value
    return None, None


def _unwritten(stmts, lo, hi, reg, D):
    return not any(isinstance(s, D.Assign) and isinstance(s.target, Reg) and s.target.n == reg
                   for s in stmts[lo:hi])


def try_table_numeric(h, blocks, D, entry):
    """Luraph v15.2 has no for-loop instructions: a numeric `for` is plain
    code around a state table kept in one register (T), with the slots in a
    different order at every loop:

        prep:  F = {}; F[ks] = step; F[ki] = start - step; F[kl] = limit;
               F[klink] = T; F[..] = ..; T = F
        H:     i = T[ki]; st = T[ks]; lim = T[kl]; i = i + st
               neg = st <= 0; pos = not neg; ge = i >= lim; le = i <= lim
               if neg then (le = pos; if ge then goto J2 else goto J1)
                      else (ge = neg; goto J1)
        J1:    ge = le
        J2:    T[ki] = i; if ge then goto BODY else goto EXIT
        after the loop (EXIT and where its `break`s go):  T = T[klink]

    The branch web is followed symbolically (flag moves, the store, tests of
    flags), so it may be merged or laid out differently; it must go to BODY
    exactly when (neg and ge) or (not neg and le). A side that ends in an
    error block before the store (a path the front end cut) is accepted when
    the step is a constant of the other sign. The loop is rewritten into the
    FORPREP/FORLOOP protocol of try_numeric (pseudo registers); the table,
    its push and its pops go. Not taken (left as it is) when anything else
    uses T or the flag registers, when the loop has no back edge, or when the
    prep is not one straight block."""
    if h.kind != "cond" or len(h.stmts) < 8 or not isinstance(h.cond, Reg):
        return False
    pat, pre = h.stmts[-8:], h.stmts[:-8]
    if not all(isinstance(s, D.Assign) and isinstance(s.target, Reg) for s in pat):
        return False
    if not _pure_moves(pre, D):
        return False
    slots = {}
    T = None
    for s in pat[:3]:
        v = s.value
        if not (isinstance(v, Index) and isinstance(v.obj, Reg) and isinstance(v.key, Const) and _num(v.key.v)):
            return False
        if T is None:
            T = v.obj.n
        if T != v.obj.n or s.target.n in slots or s.target.n == T:
            return False
        slots[s.target.n] = v.key.v
    v = pat[3].value
    if not (isinstance(v, Bin) and v.op == "Add" and isinstance(v.a, Reg) and isinstance(v.b, Reg)
            and pat[3].target.n == v.a.n and v.a.n in slots and v.b.n in slots and v.a.n != v.b.n):
        return False
    ri, rs = v.a.n, v.b.n
    rl = [r for r in slots if r not in (ri, rs)][0]
    ki, ks, kl = slots[ri], slots[rs], slots[rl]
    if len({ki, ks, kl}) != 3 or any(isinstance(s.target, Reg) and s.target.n == T for s in pre):
        return False

    def is_bin(e, op, a, b=None):
        return (isinstance(e, Bin) and e.op == op and isinstance(e.a, Reg) and e.a.n == a
                and (isinstance(e.b, Reg) and e.b.n == b if b is not None
                     else isinstance(e.b, Const) and _num(e.b.v) and e.b.v == 0))
    rneg, rpos, rge, rle = (s.target.n for s in pat[4:])
    if not (is_bin(pat[4].value, "CompareLe", rs)
            and isinstance(pat[5].value, Un) and pat[5].value.op == "Not"
            and isinstance(pat[5].value.a, Reg) and pat[5].value.a.n == rneg
            and is_bin(pat[6].value, "CompareGe", ri, rl) and is_bin(pat[7].value, "CompareLe", ri, rl)):
        return False
    if ({rneg, rpos} & {ri, rl, T} or rge in (ri, rl, T, rneg) or rle in (ri, T, rneg) or rpos == rneg
            or h.cond.n != rneg):
        return False
    env0 = {}
    for r, sym in ((rneg, "neg"), (rpos, "pos"), (rge, "ge"), (rle, "le")):
        env0[r] = sym

    # the loop: what the header reaches and what reaches the header
    def sweep(start, nxt):
        seen = set()
        st = list(start)
        while st:
            x = st.pop()
            if x in seen or x == h.id or x not in blocks:
                continue
            seen.add(x)
            st += nxt(blocks[x])
        return seen
    body = sweep(h.succ, lambda b: b.succ) & sweep(h.preds, lambda b: b.preds)
    body.add(h.id)
    preps = [p for p in h.preds if p not in body]
    if len(preps) != 1 or len(preps) == len(h.preds):
        return False
    pb = blocks[preps[0]]
    if pb.kind != "goto" or pb.succ != [h.id] or pb.id == h.id:
        return False

    # the prep: F = {} ... F[k] = v ... T = F
    ps = pb.stmts
    j = None
    for i in range(len(ps) - 1, -1, -1):
        rd, wr = _stmt_rw(ps[i], D)
        if wr == T:
            j = i
            break
        if T in rd:
            return False
    if j is None or not isinstance(ps[j].value, Reg):
        return False
    F = ps[j].value.n
    if F == T or any(F in _stmt_rw(x, D)[0] or _stmt_rw(x, D)[1] == F for x in ps[j + 1:]):
        return False
    a0, v0 = _last_def(ps, j, F, D)
    if a0 is None or not isinstance(v0, NewTable) or v0.items:
        return False
    stores = {}
    for i in range(a0 + 1, j):
        x = ps[i]
        if (isinstance(x, D.Assign) and isinstance(x.target, Index) and isinstance(x.target.obj, Reg)
                and x.target.obj.n == F and isinstance(x.target.key, Const) and _num(x.target.key.v)
                and isinstance(x.value, (Reg, Const)) and x.target.key.v not in stores
                and not (isinstance(x.value, Reg) and x.value.n == F)):
            stores[x.target.key.v] = (i, x.value)
            continue
        rd, wr = _stmt_rw(x, D)
        if F in rd or wr == F or T in rd or wr == T:
            return False
    if not all(k in stores for k in (ki, ks, kl)):
        return False
    links = [k for k, (_, x) in stores.items() if isinstance(x, Reg) and x.n == T]
    if len(links) != 1 or links[0] in (ki, ks, kl):
        return False
    klink = links[0]

    def value_at_push(k):
        """The value stored in slot k as an expression that can be evaluated
        at the push (a constant, or the register if it still holds it), and
        what defined it in this block when that is a constant or one `x + y`
        / `x - y` of constants and registers unchanged since."""
        i, x = stores[k]
        if isinstance(x, Const):
            return x, x
        if not _unwritten(ps, i, j, x.n, D):
            return None, None
        d, dv = _last_def(ps, i, x.n, D)
        if isinstance(dv, Const) and _num(dv.v):
            return dv, dv
        if isinstance(dv, Bin) and dv.op in ("Add", "Sub") and all(
                isinstance(o, Const) or (isinstance(o, Reg) and _unwritten(ps, d, j, o.n, D)) for o in (dv.a, dv.b)):
            return x, dv
        return x, None
    step, step_def = value_at_push(ks)
    limit, limit_def = value_at_push(kl)
    first, first_def = value_at_push(ki)
    if step is None or limit is None or first is None:
        return False
    # (`x + 0`, the VM's number coercion of a bound: try_numeric strips it)
    if isinstance(limit_def, Bin) and limit_def.op == "Add" and isinstance(limit_def.b, Const) \
            and limit_def.b.v == 0 and isinstance(limit_def.a, Reg):
        limit = limit_def
    if isinstance(step_def, Bin) and step_def.op == "Add" and isinstance(step_def.b, Const) \
            and step_def.b.v == 0 and isinstance(step_def.a, Reg):
        step = step_def
    # slot ki holds start - step
    sv = stores[ks][1]
    if isinstance(first_def, Bin) and first_def.op == "Sub" and (
            (isinstance(sv, Reg) and isinstance(first_def.b, Reg) and first_def.b.n == sv.n)
            or (isinstance(step, Const) and isinstance(first_def.b, Const) and first_def.b.v == step.v)):
        start = first_def.a
    elif isinstance(first, Const) and isinstance(step, Const) and _num(first.v) and _num(step.v):
        start = Const(first.v + step.v)
    else:
        return False
    step_sign = None
    if isinstance(step, Const) and _num(step.v):
        step_sign = "neg" if step.v <= 0 else "pos"

    # the branch web
    store_stmts = []
    results = []

    def follow(bid, env, assume, stored, depth):
        if depth > 12:
            return False
        hops = 0
        while True:
            hops += 1
            if bid == h.id or bid not in blocks or hops > 40:
                return False
            b = blocks[bid]
            k = 0
            for x in b.stmts:
                if not isinstance(x, D.Assign):
                    break
                if (isinstance(x.target, Reg) and isinstance(x.value, Reg) and x.value.n in env
                        and x.target.n not in (ri, T)):
                    env = dict(env)
                    env[x.target.n] = env[x.value.n]
                elif (not stored and isinstance(x.target, Index) and isinstance(x.target.obj, Reg)
                      and x.target.obj.n == T and isinstance(x.target.key, Const) and x.target.key.v == ki
                      and isinstance(x.value, Reg) and x.value.n == ri):
                    stored = True
                    store_stmts.append(x)
                else:
                    break
                k += 1
            web_test = b.kind == "cond" and isinstance(b.cond, Reg) and b.cond.n in env
            if k < len(b.stmts) or not (b.kind == "goto" or web_test):
                # not (only) web code: BODY / EXIT behind the store, else a cut path
                if k == 0 and stored:
                    results.append((assume, bid, env))
                    return True
                if k == len(b.stmts) and b.kind == "error" and not stored:
                    results.append((assume, None, env))
                    return True
                return False
            if b.kind == "goto":
                bid = b.succ[0]
                continue
            sym = env[b.cond.n]
            val = assume.get("neg") if sym == "neg" else (not assume["neg"]) if sym == "pos" else assume.get(sym)
            if val is None:
                return all(follow(b.succ[0 if v else 1], env, dict(assume, **{sym: v}), stored, depth + 1)
                           for v in (True, False))
            bid = b.succ[0 if val else 1]
    if not (follow(h.succ[0], env0, {"neg": True}, False, 0) and follow(h.succ[1], env0, {"neg": False}, False, 0)):
        return False
    body_id = exit_id = None
    flag_regs = {rs, rl}
    for assume, bid, env in results:
        if bid is None:
            if step_sign is None or (step_sign == "neg") == assume["neg"]:
                return False
            continue
        test = "ge" if assume["neg"] else "le"
        if test not in assume:
            return False
        flag_regs |= set(env)
        if assume[test]:
            if body_id not in (None, bid):
                return False
            body_id = bid
        else:
            if exit_id not in (None, bid):
                return False
            exit_id = bid
    if body_id is None or exit_id is None or body_id == exit_id or body_id not in body or exit_id in body:
        return False
    flag_regs -= {ri, T}
    if any(_live(blocks, body_id, r, D) or _live(blocks, exit_id, r, D) for r in flag_regs):
        return False
    if _live(blocks, exit_id, ri, D) or _live(blocks, h.id, F, D):
        return False

    # nothing else in the loop may use T
    own = {id(x) for x in pat} | {id(x) for x in store_stmts}
    for x in body:
        b = blocks[x]
        for st_ in b.stmts:
            if id(st_) in own:
                continue
            rd, wr = _stmt_rw(st_, D)
            if T in rd or wr == T:
                return False
        if T in _block_tail_regs(b) or (x != h.id and b.kind in ("for", "forin")
                                         and T in _regs(list(b.values[1]))):
            return False
    # behind the loop T is first popped (T = T[klink]) or never used again
    pops = []
    seen = set()
    st = [s for x in body for s in blocks[x].succ if s not in body]
    while st:
        x = st.pop()
        if x in seen:
            continue
        seen.add(x)
        if x in body or x == pb.id or x not in blocks:
            return False
        b = blocks[x]
        hit = False
        for st_ in b.stmts:
            rd, wr = _stmt_rw(st_, D)
            if T in rd or wr == T:
                v = st_.value if isinstance(st_, D.Assign) else None
                if not (wr == T and isinstance(v, Index) and isinstance(v.obj, Reg) and v.obj.n == T
                        and isinstance(v.key, Const) and v.key.v == klink):
                    return False
                pops.append((b, st_))
                hit = True
                break
        if hit:
            continue
        if T in _block_tail_regs(b):
            return False
        st += b.succ
    # (a pop that can be reached without going through the loop is not this loop's)
    out = set()
    st = [entry]
    while st:
        x = st.pop()
        if x in out or x == h.id or x not in blocks:
            continue
        out.add(x)
        st += blocks[x].succ
    if any(b.id in out for b, _ in pops) or body_id in out:
        return False

    # rewrite: the FORPREP / FORLOOP protocol of try_numeric
    import structure
    d = h.id
    Z, P, A = Pseudo("Z", d), Pseudo("P", d), Pseudo("a", d)
    drop = {id(ps[a0]), id(ps[j])} | {id(ps[i]) for i, _ in stores.values()}
    new = []
    for x in ps:
        if x is ps[j]:
            new += [D.Assign(Z, step), D.Assign(P, limit), D.Assign(A, Bin("Sub", start, Z))]
        elif id(x) not in drop:
            new.append(x)
    pb.stmts = new
    for b, st_ in pops:
        b.stmts = [x for x in b.stmts if x is not st_]
    h.stmts = pre + [D.Assign(A, Bin("Add", A, Z))]
    h.cond = Bin("CompareLe", Z, Const(0))
    sides = []
    for op in ("CompareGe", "CompareLe"):
        sb = structure.Block(max(blocks) + 1)
        blocks[sb.id] = sb
        tb = structure.Block(max(blocks) + 1)
        blocks[tb.id] = tb
        sb.kind, sb.cond, sb.succ = "cond", Bin(op, A, P), [tb.id, exit_id]
        tb.stmts, tb.succ = [D.Assign(Reg(ri), A)], [body_id]
        sb.origin = tb.origin = h.origin
        sides.append(sb.id)
    h.succ = sides
    _recompute(blocks)
    return True


def _same(a, b):
    import ir
    return ir.fmt_expr(a) == ir.fmt_expr(b)


def strip_plus0(e):
    if isinstance(e, Bin) and e.op == "Add" and isinstance(e.b, Const) and e.b.v == 0:
        return e.a
    return e


def try_generic(h, blocks, D):
    if h.kind != "cond" or not h.stmts or not isinstance(h.stmts[-1], D.CallStmt):
        return False
    call = h.stmts[-1]
    # register moves before the iterator call (Luraph resets registers there)
    # run before every call: equivalently once before the loop and at the end
    # of every iteration that continues (the latches), as long as they are pure
    pre = h.stmts[:-1]
    if not _pure_moves(pre, D):
        return False
    if not (is_pseudo(call.fn, "a") or is_pseudo(call.fn)) or call.args.items or call.args.tail is not None:
        return False
    it = call.fn
    if not (isinstance(h.cond, TempVal) and h.cond.t == call.t and h.cond.i == 1):
        return False
    bb = blocks[h.succ[0]]
    vars_ = []
    rest = []
    for s in bb.stmts:
        if not rest and isinstance(s, D.Assign) and isinstance(s.target, Reg) and isinstance(s.value, TempVal) \
                and s.value.t == call.t and s.value.i == len(vars_) + 2:
            vars_.append(s.target.n)
        else:
            rest.append(s)
    if not vars_:
        return False
    # the rest of the body must not use the temp any more
    preps = []
    for p in h.preds:
        pb = blocks[p]
        for s in pb.stmts:
            if isinstance(s, D.Assign) and isinstance(s.target, Pseudo) and s.target.name == it.name and \
                    s.target.depth == it.depth and isinstance(s.value, D.GenIter):
                preps.append((pb, s))
    if len(preps) > 1:
        def is_it(x):
            return isinstance(x, D.Assign) and isinstance(x.target, Pseudo) and x.target.name == it.name \
                and x.target.depth == it.depth and isinstance(x.value, D.GenIter)
        nb = join_preps(h, blocks, [pb for pb, _ in preps], is_it)
        if nb is not None:
            preps = [(nb, nb.stmts[-1])]
    if len(preps) != 1:
        return False
    pb, ps = preps[0]
    args = ps.value.args
    items = list(args.items[1:]) if args is not None else []   # drop the VM object
    if pre:
        # the iterator expressions are evaluated before the moves (loop prep first)
        written = {s.target.n for s in pre if isinstance(s.target, Reg)}
        if any(isinstance(x, Reg) and x.n in written for e in (args.items if args else []) for x in _walk(e)):
            return False
    bb.stmts = rest
    if pre:
        _moves_to_latches(h, pb, pre, blocks)
    h.stmts = []
    h.kind = "forin"
    h.cond = None
    if args is not None and args.tail is not None:
        import codegen
        items.append(codegen.TailRef(args.tail))      # for k, v in pairs(t): f, s, ctl from a call
    lst = LoopExprs(items)
    h.values = (vars_, lst)
    place_prep(pb, lst, D, lambda s: s is ps)
    if pre:
        import copy
        pb.stmts += [copy.copy(s) for s in pre]
    return True


def _run_once_forin(h, blocks, D):
    """try_generic's header with the loop's prep in the same block: the body
    always leaves (returns), so there is no back edge and the loop runs at
    most once (the generic counterpart of _run_once_for). The loop state is
    a hidden VM slot (Luraph: a coroutine made from f, s, ctl and called per
    iteration, giving "more?" and the loop variables), which has no name in
    Luau: left alone, its call is printed as a call of an undefined name.
    The one call of it is the first step of the iteration: `f(s, ctl)` gives
    the variables, and "more?" is `first variable ~= nil`. Returns False
    (not a `for`: the structurer needs a back edge for one)."""
    if h.kind != "cond" or not h.stmts or not isinstance(h.stmts[-1], D.CallStmt):
        return False
    call = h.stmts[-1]
    if not is_pseudo(call.fn) or call.args.items or call.args.tail is not None:
        return False
    if not (isinstance(h.cond, TempVal) and h.cond.t == call.t and h.cond.i == 1):
        return False
    at = None
    for i, s in enumerate(h.stmts[:-1]):
        if isinstance(s, D.Assign) and is_pseudo(s.target, call.fn.name, call.fn.depth):
            at = i if isinstance(s.value, D.GenIter) else None
    if at is None or not _pure_moves(h.stmts[at + 1:-1], D):
        return False
    args = h.stmts[at].value.args
    if args is None or args.tail is not None or len(args.items) != 4:
        return False        # (the VM object, f, s, ctl)
    f, s_, ctl = args.items[1:]
    t = call.t
    # every use of the call's values: values[1] is "more?", values[i + 1] the i-th variable
    todo = []
    for b in blocks.values():
        todo += [(b, "stmts"), (b, "cond"), (b, "values")]
    seen = set()
    uses = []           # (holder, key): holder.key / holder[key] is a TempVal of t
    while todo:
        holder, key = todo.pop()
        x = holder[key] if isinstance(holder, list) else getattr(holder, key, None)
        if x is None or isinstance(x, (str, int, float, bytes, bool)):
            continue
        if isinstance(x, TempVal):
            if x.t == t:
                uses.append((holder, key))
            continue
        if isinstance(x, tuple):
            if any(isinstance(y, TempVal) and y.t == t for y in x):
                return False    # (not rewritable in place)
            x = list(x)
        if id(x) in seen:
            continue
        seen.add(id(x))
        if isinstance(x, list):
            todo += [(x, i) for i in range(len(x))]
            continue
        if type(x).__name__ in ("LTable", "Scope", "LuaFunc"):
            continue
        if getattr(x, "t", None) == t and x is not call:
            return False        # (the values as a list: `T[i...]`, their count)
        d = getattr(x, "__dict__", None)
        if d is not None:
            todo += [(x, k) for k in d]
    for holder, key in uses:
        x = holder[key] if isinstance(holder, list) else getattr(holder, key)
        new = Bin("CompareNe", TempVal(t, 1), Const(None)) if x.i == 1 else TempVal(t, x.i - 1)
        if isinstance(holder, list):
            holder[key] = new
        else:
            setattr(holder, key, new)
    h.stmts = h.stmts[:at] + [D.CallStmt(t, f, Multi([s_, ctl]))] + h.stmts[at + 1:-1]
    return False


def _walk(e):
    st = [e]
    while st:
        x = st.pop()
        yield x
        if hasattr(x, "__dict__"):
            st += [v for v in x.__dict__.values() if hasattr(v, "__dict__") and not isinstance(v, type)]


class LoopExprs(list):
    """The expressions of a for header (start, limit, step / the iterator
    triple). They are evaluated once, before the loop: the prep block holds a
    ForPrep statement that owns them (uses, renaming, inlining), the header
    keeps this same list for rendering."""


def place_prep(pb, lst, D, is_prep):
    """Put ForPrep(lst) in the prep block after its last loop-prep statement."""
    at = None
    for i, s in enumerate(pb.stmts):
        if is_prep(s):
            at = i
    pb.stmts.insert(len(pb.stmts) if at is None else at + 1, D.ForPrep(lst))


def join_preps(h, blocks, pbs, is_prep):
    """Several predecessors of loop header h end with the same prep
    statements (the header was reached with two front end states, merged
    later): move the prep into one new block that they all go to. Returns
    that block, or None if the preps differ or are not the blocks' last
    statements before a plain jump to h."""
    import ir
    import structure
    tails = []
    for pb in pbs:
        if pb.kind != "goto" or pb.succ != [h.id]:
            return None
        k = len(pb.stmts)
        while k > 0 and is_prep(pb.stmts[k - 1]):
            k -= 1
        if k == len(pb.stmts) or any(is_prep(x) for x in pb.stmts[:k]):
            return None
        tails.append(k)
    texts = {tuple(ir.fmt_stmt(x) for x in pb.stmts[k:]) for pb, k in zip(pbs, tails)}
    if len(texts) != 1:
        return None
    nb = structure.Block(max(blocks) + 1)
    nb.stmts = list(pbs[0].stmts[tails[0]:])
    nb.kind = "goto"
    nb.succ = [h.id]
    nb.origin = pbs[0].origin
    blocks[nb.id] = nb
    for pb, k in zip(pbs, tails):
        del pb.stmts[k:]
        pb.succ = [nb.id]
    _recompute(blocks)
    return nb


def _recompute(blocks):
    for b in blocks.values():
        b.preds = []
    for b in blocks.values():
        for s in b.succ:
            if s in blocks:
                blocks[s].preds.append(b.id)
