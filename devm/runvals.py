"""Values of the traced run where the lifted code would show something else.

A VM function can hand its own register array to a call (`f(..., R, n)` in the interpreter, printed
`f(..., ({}), n)`): the function called can then write any register of its caller.  Luraph builds
do this with routines of the build: a string decoder on another interpreter is handed an encrypted
constant, the caller's register array and the number of a register, and stores the plain string
there.  The code that follows reads that register.  Nothing in the lifted code of the caller writes
it, so the text shows the register's earlier value (the encrypted constant) wherever the script
reads it after the call.

The traced run says what the registers held.  The harness records, for every instruction that ran,
the numbers, strings and booleans each register held on entry when they were the same on every
visit (harness.lua, the oracle's `r` and `s`; not for code only the exercise or probe pass ran), and
for an instruction whose call entered a VM function, which of the caller's registers had changed
when the caller went on (`w`, and `h`: at how many of the visits that was seen).  For an
instruction I whose tree calls a function with the register array, and whose every execution went
on to the same instruction J (the successors the run took), a register r

  * that I's own statements after the call do not write (call results, later statements),
  * that no function of the program captures by reference (a variable a closure shares changes
    through the closure, which the text shows),
  * that the code reads after the call before writing it again (Live),
  * that J saw holding one value V at every visit,
  * and that did not hold V already before the call (I's entry value, or a constant I itself
    stored there before the call)

was given V by the call: `R[r] = V` goes in right after the call statement -- but only when what
the call is handed is the same at every execution (Flow.fixed): the function called is one the
program makes, and every argument besides the register array is a constant.  A value the script
read at run time (a global, an argument, a result) can differ at another execution, however often
this run saw the same one.

Where the run does not prove a value for a register the call may have set -- one it changed to
different values at different visits, one the call names (a constant register number handed with
the register array) or the run saw it change, with no one value after the call, or any of these
when the inputs are not constants -- `R[r] = nil --[[...]]` (MARK) goes in after the call: the text
does not show the earlier value as the current one, and the call counts as not shown (notes:
`[runvals-missing]`, the result is PARTIAL).  The same after a call that did not run or went on to
more than one instruction, for the registers it names.  A register that held no one value before
the call nor after it, that the call does not name and that the run did not see it change (a
callee that is no VM function is not seen), is left as the code has it.  Registers nothing reads
afterwards are dropped again with the other dead stores (ircfg.ProtoCFG._dead_stores).

A call of a helper of the build (a program the pipeline names one) whose results go straight to
registers is shown the same way: `R[a] = helper(enc)` becomes `helper(enc); R[a] = V` when the next
instruction saw V in R[a] at every visit and the helper is handed constants.  A function of the
script keeps its call result: there the text is the script's own code, whatever value it had.

Calls are never dropped: whether a helper has effects besides what it wrote is not known (its own
lift can be incomplete).  pipeline labels the helpers of the build in the text (labels, with the
evidence that set them aside) and puts a note in the report (notes).
"""
from . import pe as P

# statement kinds of a raw instruction tree that write no register (besides 'set' and 'setmulti',
# which say which)
_NO_REG_WRITE = ('setindex', 'call', 'close', 'setlist', 'cold', 'vararg', 'error')


def _same(a, b):
    """the same Lua value (no number is a boolean, 1 and 1.0 are one number)"""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    return P.is_num(a) and P.is_num(b) and float(a) == float(b)


def _values(rec):
    """{register: value} of an oracle record: numbers, strings and booleans held at every visit"""
    out = dict(rec.get('r') or {})
    out.update(rec.get('s') or {})
    return out


def _writes(st):
    """registers a raw statement writes: a set, 'all' (registers from a base up: ('from', base)), or
    None for a statement whose writes are not known here"""
    if st[0] == 'set':
        return {st[1][1]} if st[1][0] == 'r' else set()
    if st[0] == 'setmulti':
        return ('from', st[1])
    if st[0] in _NO_REG_WRITE:
        return set()
    return None


def _hits(w, r):
    if w is None:
        return True
    if isinstance(w, tuple):
        return isinstance(r, int) and r >= w[1]
    return r in w


def _nested(tree):
    """every statement of the arms of a tree's branch terminator"""
    out = []
    term = tree[1]
    arms = (term[2], term[3]) if term[0] == 'br' else (term[3], term[4]) if term[0] == 'forbr' else ()
    for arm in arms:
        out.extend(arm[0])
        out.extend(_nested(arm))
    return out


class Flow:
    """What a register or temporary of one lifted program holds, followed back through the
    statements of an instruction and of the instructions before it, as long as each has a single
    predecessor (no join of paths, where the value could come from either side)."""

    LIMIT = 256

    def __init__(self, pir):
        from .lifter import tree_ends
        self.pir = pir
        # temporary -> (instruction, index of its statement or None in a branch arm, expression);
        # None for a name set in more than one place (the copies of an instruction lifted for
        # several paths name their temporaries alike)
        self.temps = {}
        self.preds = {}

        def note(tid, where):
            self.temps[tid] = where if tid not in self.temps else None
        for key, node in pir.nodes.items():
            if node.tree is None:
                continue
            for i, st in enumerate(node.tree[0]):
                if st[0] == 'set' and st[1][0] == 't':
                    note(st[1][1], (key, i, st[2]))
            for st in _nested(node.tree):
                if st[0] == 'set' and st[1][0] == 't':
                    note(st[1][1], (key, None, st[2]))
            for end in tree_ends(node.tree):
                if end[0] == 'goto':
                    self.preds.setdefault(end[1], set()).add(key)
        # registers that hold the register array somewhere in the program, also as a copy of one
        # that does (a call handed one of them may write registers)
        self.regs_holders = set()
        sets = [st for node in pir.nodes.values() if node.tree is not None
                for st in list(node.tree[0]) + _nested(node.tree) if st[0] == 'set' and st[1][0] == 'r']
        while True:
            more = {st[1][1] for st in sets if st[1][1] not in self.regs_holders and self._is_regs_expr(st[2])}
            if not more:
                break
            self.regs_holders |= more

    def tdef(self, tid, key=None, idx=None):
        """(instruction, index, expression) of the statement that sets temporary tid: the one in front
        of statement idx of instruction key if there is one, else the only one in the program"""
        node = self.pir.nodes.get(key) if key is not None and idx is not None else None
        if node is not None and node.tree is not None:
            stmts = node.tree[0]
            for j in range(min(idx, len(stmts)) - 1, -1, -1):
                if stmts[j][0] == 'set' and stmts[j][1] == ('t', tid):
                    return key, j, stmts[j][2]
        return self.temps.get(tid)

    def _plain(self, e, depth=0):
        """e with temporaries replaced by what they were set to (within the program's statements)"""
        while e[0] == 't' and depth < 16:
            d = self.temps.get(e[1])
            if d is None:
                break
            e, depth = d[2], depth + 1
        return e

    def _is_regs_expr(self, e):
        e = self._plain(e)
        return e == ('regs',) or (e[0] == 'r' and e[1] in self.regs_holders)

    def handed_regs(self, stmts, i, key, rec):
        """True when call statement i of instruction key is handed the register array: an argument
        that stands for it (followed back through the code), or one read from a register that held
        the activation's own register array at every run of the instruction (the oracle's `a`,
        record `rec`)"""
        return any(self.is_regs_arg(a, key, i, rec, stmts) for a in stmts[i][3])

    def may_write_regs(self, st):
        """a call that may be handed the register array (directly, or a register that holds it
        somewhere in the program)"""
        return st[0] == 'call' and any(self._is_regs_expr(a) for a in st[3])

    # spare(key, r): True when the run saw every call handed the register array in instruction key
    # leave register r alone (show_run_values sets it from the oracle's `h` / `w`)
    spare = None

    def reaching(self, r, key, idx):
        """[(instruction, index, expression)]: every statement that can have written register r last
        before top-level statement idx of instruction key (None: the end of the instruction), over all
        paths into it; None where a path starts at the program's entry, or passes a statement of unknown
        effect or a call that may write registers (unless spare says the run saw it leave r alone)"""
        # (the instruction itself is walked again, as a whole, when a path leads back into it)
        out, seen, todo = [], set(), [(key, idx)]
        while todo:
            k, i = todo.pop()
            node = self.pir.nodes.get(k)
            if node is None or node.tree is None:
                return None
            stmts = node.tree[0]

            def blocks(st):
                return _hits(_writes(st), r) or (self.may_write_regs(st) and not (self.spare and self.spare(k, r)))
            if i is None:
                if any(blocks(st) for st in _nested(node.tree)):
                    return None
                i = len(stmts)
            for j in range(i - 1, -1, -1):
                st = stmts[j]
                if st[0] == 'set' and st[1] == ('r', r):
                    out.append((k, j, st[2]))
                    break
                if blocks(st):
                    return None
            else:
                ps = self.preds.get(k, ())
                if not ps:
                    return None
                for q in ps:
                    if q not in seen:
                        seen.add(q)
                        todo.append((q, None))
                if len(seen) > self.LIMIT:
                    return None
        return out

    def fixed(self, e, key, idx, depth=0):
        """What the expression e (read by top-level statement idx of instruction key) is at every
        execution, when the code alone says so: a constant, a function the program makes (`('closure',
        proto)`; what it captures is not followed) or a library function, or arithmetic, comparison or
        logic on constants -- followed back through temporaries and through every definition of a
        register that can reach the statement (all of them alike).  None for anything else (a value
        read at run time: a global, an argument, a result, a table field)."""
        if depth > 24:
            return None
        t = e[0]
        if t in ('k', 'kfn'):
            return e
        if t == 'closure':
            return ('closure', e[1])
        if t == 't':
            d = self.tdef(e[1], key, idx)
            return None if d is None or d[1] is None else self.fixed(d[2], d[0], d[1], depth + 1)
        if t == 'r':
            defs = self.reaching(e[1], key, idx)
            if not defs:
                return None
            got = [self.fixed(d[2], d[0], d[1], depth + 1) for d in defs]
            return got[0] if got[0] is not None and all(g == got[0] for g in got) else None
        if t in ('bin', 'and', 'or'):
            x, y = (e[2], e[3]) if t == 'bin' else (e[1], e[2])
            a, b = self.fixed(x, key, idx, depth + 1), self.fixed(y, key, idx, depth + 1)
            if a is None or b is None or a[0] == 'closure' or b[0] == 'closure':
                return None
            return ('bin', e[1], a, b) if t == 'bin' else (t, a, b)
        if t == 'un':
            a = self.fixed(e[2], key, idx, depth + 1)
            return None if a is None or a[0] == 'closure' else (t, e[1], a)
        return None

    def is_regs_arg(self, a, key, idx, rec, stmts):
        """the call argument a (of call statement idx of instruction key) stands for the register array:
        in the code, or read from a register that held the activation's own register array at every run
        of the instruction (the oracle's `a`, record `rec`)"""
        if self.value(a, key, idx) == ('regs',):
            return True
        if rec is None or rec.get('p') or not rec.get('a'):
            return False
        e, at = a, idx
        while e[0] == 't':
            d = self.tdef(e[1], key, at)
            if d is None or d[0] != key or d[1] is None:
                break
            e, at = d[2], d[1]
        return e[0] == 'r' and e[1] in rec['a'] and not any(_hits(_writes(x), e[1]) for x in stmts[:at])

    def value(self, e, key, idx, depth=0):
        """the expression e (read by top-level statement idx of instruction key) stands for, with
        temporaries and registers replaced by their definitions; None where that is not known"""
        if depth > 24:
            return None
        if e[0] == 't':
            d = self.tdef(e[1], key, idx)
            if d is None or d[1] is None:
                return None
            return self.value(d[2], d[0], d[1], depth + 1)
        if e[0] == 'r':
            got = self.last_write(e[1], key, idx)
            if got is None:
                return None
            return self.value(got[2], got[0], got[1], depth + 1)
        return e

    def last_write(self, r, key, idx):
        """(instruction, index, expression) of the statement that last wrote register r before
        top-level statement idx of instruction key (None: the end of the instruction), on the path
        of single predecessors; None where a join, a call that may write registers or a statement of
        unknown effect comes first"""
        seen = set()
        while True:
            node = self.pir.nodes.get(key)
            if node is None or node.tree is None:
                return None
            stmts = node.tree[0]
            if idx is None:
                # (the way out of the instruction passes the arms of its branch, if any)
                if any(_hits(_writes(st), r) or self.may_write_regs(st) for st in _nested(node.tree)):
                    return None
                idx = len(stmts)
            for j in range(idx - 1, -1, -1):
                st = stmts[j]
                w = _writes(st)
                if st[0] == 'set' and st[1] == ('r', r):
                    return key, j, st[2]
                if _hits(w, r) or self.may_write_regs(st):
                    return None
            ps = self.preds.get(key, ())
            if len(ps) != 1 or key in seen or len(seen) > self.LIMIT:
                return None
            seen.add(key)
            key, idx = next(iter(ps)), None


def captured(pir):
    """registers a function made in this program captures by reference"""
    out = set()

    def walk(x):
        if isinstance(x, tuple):
            if len(x) == 3 and x[0] == 'closure' and isinstance(x[2], tuple):
                for c in x[2]:
                    if isinstance(c, tuple) and len(c) >= 2 and c[0] == 'ref' and not isinstance(c[1], tuple):
                        out.add(c[1])
            for y in x:
                walk(y)
        elif isinstance(x, list):
            for y in x:
                walk(y)
    for node in pir.nodes.values():
        if node.tree is not None:
            walk(node.tree[0])
            walk(node.tree[1][:2] if node.tree[1][0] in ('ret', 'br') else ())
            walk(_nested(node.tree))
    return out


def _expr_reads(x, out):
    """add the registers the expression (or statement part) x reads to out"""
    if isinstance(x, tuple):
        if len(x) == 2 and x[0] == 'r' and isinstance(x[1], int):
            out.add(x[1])
            return
        if len(x) == 3 and x[0] == 'closure' and isinstance(x[2], tuple):
            for c in x[2]:
                if isinstance(c, tuple) and len(c) >= 2 and isinstance(c[1], int):
                    out.add(c[1])
                else:
                    _expr_reads(c, out)
            return
        for y in x:
            _expr_reads(y, out)
    elif isinstance(x, list):
        for y in x:
            _expr_reads(y, out)


def _loop_reads(x, out):
    # (the register-level loop statements name a base register: it and the three around it)
    for a in (x if isinstance(x, (list, tuple)) else (x,)):
        if isinstance(a, int) and not isinstance(a, bool):
            out.update(range(a - 3, a + 4))
        else:
            _expr_reads(a, out)


def _stmt_reads(st):
    out = set()
    k = st[0]
    if k == 'set':
        _expr_reads(st[2], out)
        if st[1][0] not in ('r', 't'):
            _expr_reads(st[1], out)
    elif k in ('numforprep', 'numforloop', 'genforprep', 'genforloop'):
        _loop_reads(st[1:], out)
    elif k not in ('setmulti', 'close', 'forpop'):
        _expr_reads(st[1:], out)
    return out


def _tree_reads(t, out):
    """every register a statement of the tree (its arms included) or its terminator reads"""
    stmts, term = t
    for st in stmts:
        out |= _stmt_reads(st)
    k = term[0]
    if k == 'br':
        _expr_reads(term[1], out)
        _tree_reads(term[2], out)
        _tree_reads(term[3], out)
    elif k == 'forbr':
        _loop_reads(term[2], out)
        _tree_reads(term[3], out)
        _tree_reads(term[4], out)
    elif k != 'goto':
        _expr_reads(term[1:], out)
    return out


class Live:
    """Which registers the code reads after a point before writing them again (liveness over the
    instructions of one lifted program).  A register that is not read after a call cannot show a
    stale value there.  Only top-level `R[r] = ...` statements count as writes (the arms of a
    branch, a call handed the register array, `setmulti` may or may not write); an instruction
    that did not lift reads everything."""

    def __init__(self, pir):
        from .lifter import tree_ends
        self.pir = pir
        self.succ, self.use, self.dfn = {}, {}, {}
        for key, node in pir.nodes.items():
            if node.tree is None:
                continue
            self.succ[key] = [end[1] for end in tree_ends(node.tree) if end[0] == 'goto']
            self.use[key], self.dfn[key] = self._ud(node.tree[0], node.tree)
        preds = {}
        for k, ss in self.succ.items():
            for q in ss:
                preds.setdefault(q, set()).add(k)
        self.inn = {k: set(u) for k, u in self.use.items()}
        work, queued = list(self.use), set(self.use)
        while work:
            k = work.pop()
            queued.discard(k)
            out = self._out(k)
            if out is None:
                continue
            new = self.use[k] | (out - self.dfn[k])
            if new != self.inn[k]:
                self.inn[k] = new
                for p in preds.get(k, ()):
                    if p not in queued:
                        queued.add(p)
                        work.append(p)

    @staticmethod
    def _ud(stmts, tree):
        use, dfn = set(), set()
        for st in stmts:
            use |= _stmt_reads(st) - dfn
            if st[0] == 'set' and st[1][0] == 'r':
                dfn.add(st[1][1])
        rest = set()
        _tree_reads(([], tree[1]), rest)
        return use | (rest - dfn), dfn

    def _out(self, key):
        out = set()
        for q in self.succ.get(key, ()):
            if q not in self.inn:
                return None        # (an instruction that did not lift, or none at all: anything is read)
            out |= self.inn[q]
        return out

    def after(self, key, ci):
        """the registers read after top-level statement ci of instruction key before they are written
        again, None when that is not known (all of them may be)"""
        node = self.pir.nodes.get(key)
        if node is None or node.tree is None:
            return None
        out = self._out(key)
        if out is None:
            return None
        use, dfn = self._ud(node.tree[0][ci + 1:], node.tree)
        return use | (out - dfn)


class Site:
    """a call whose effect on its caller's registers the lifted code does not show: a call handed
    the caller's register array, or a call of a helper of the build whose results go to registers"""
    __slots__ = ('pid', 'key', 'callee', 'kind', 'shown', 'unproved', 'ran', 'fixed', 'marked')

    def __init__(self, pid, key, callee, kind):
        self.pid, self.key, self.callee, self.kind = pid, key, callee, kind
        self.shown = {}         # register -> the value the run saw after the call
        # registers the call changed (or may have set: the ones it names, the ones the run saw it
        # change) for which the run proves no value: no one value after the call, or inputs that are
        # not constants (MARK goes in after a call handed the register array)
        self.unproved = set()
        self.ran = False        # the instruction ran and went on to a single instruction that ran
        self.fixed = False      # the function called is one the program makes, handed constants only
        self.marked = set()     # (a call that did not run: the registers given MARK after it)

    def __repr__(self):
        return 'Site(%s %s %s callee=%s shown=%s unproved=%s ran=%s fixed=%s)' % (
            self.pid, self.key, self.kind, self.callee, sorted(self.shown), sorted(self.unproved), self.ran, self.fixed)


def _after(rec, oracle, pid, term):
    """the oracle record of the one instruction every execution of an instruction (record `rec`)
    went on to, when that is the instruction its tree jumps to; else None"""
    if rec is None or rec.get('p') or term[0] != 'goto':
        return None
    succ = rec.get('succ') or ()
    if len(succ) != 1:
        return None
    nxt = tuple(next(iter(succ)))
    if tuple(term[1][:2]) != nxt:
        return None
    rec2 = oracle.get((pid,) + nxt)
    return None if rec2 is None or rec2.get('p') else rec2


def _mv_reads(x, cid, acc):
    """collect the ('mv', cid, i) atoms read anywhere in x"""
    if isinstance(x, tuple):
        if len(x) == 3 and x[0] == 'mv' and x[1] == cid:
            acc.append(x)
            return
        for y in x:
            _mv_reads(y, cid, acc)
    elif isinstance(x, list):
        for y in x:
            _mv_reads(y, cid, acc)


def _const(v):
    return ('k', P.norm(v) if P.is_num(v) and not isinstance(v, bool) else v)


# what stands in the text for a register that a call handed the register array may have set, where the
# run does not prove one value for it (printed `nil --[[...]]`, see structure.Expr)
MARK = 'set by a call handed the register array: value not shown'


def _seen_writes(rec):
    """the registers the run saw the calls handed the register array in an instruction (record rec)
    change, when it saw every such call (harness.lua orcPend: the callee a VM function), else None"""
    if rec is None or rec.get('p') or not rec.get('h') or rec['h'] != rec['n']:
        return None
    return rec.get('w') or set()


def _spares(rec, r):
    w = _seen_writes(rec)
    return w is not None and r not in w


def _named(flow, st, key, ci, regs_args):
    """the registers a call names: the integer constants handed after the register array (`f(enc, R, n)`)"""
    out = set()
    if not regs_args:
        return out
    for a in st[3][regs_args[0] + 1:]:
        v = flow.fixed(a, key, ci)
        if v is not None and v[0] == 'k' and P.is_num(v[1]) and not isinstance(v[1], bool) \
                and float(v[1]).is_integer() and 0 <= v[1] < 1 << 20:
            out.add(int(v[1]))
    return out


def show_run_values(protos, oracle, helpers=()):
    """Put the values the run proves where the lifted code would show something else (see the
    module's text): after every call handed its caller's register array, and in place of the
    results of a call of a helper of the build (`helpers`: program numbers) that go to registers
    (`R[n] = helper(...)` becomes `helper(...); R[n] = V`, for every result the run proves).
    Where the run proves no value for a register such a call may have set, `R[r] = nil --[[...]]`
    (MARK) goes in after the call instead, so that the text does not show the earlier value as the
    current one.  Changes the instruction trees of `protos` in place and returns the call sites
    found, [Site]."""
    helpers = set(helpers or ())
    sites = []
    for pid, pir in protos.items():
        if not isinstance(pid, int):
            continue
        flow = None
        caps = None
        for key, node in list(pir.nodes.items()):
            if node.tree is None or node.error or not node.tree[0]:
                continue
            stmts, term = node.tree
            calls = [i for i, st in enumerate(stmts) if st[0] == 'call']
            if not calls:
                continue
            if flow is None:
                flow = Flow(pir)
                flow.spare = lambda k, r, pid=pid: _spares(oracle.get((pid, k[0], k[1])), r)
            rec = oracle.get((pid, key[0], key[1]))
            found = [i for i in calls if flow.handed_regs(stmts, i, key, rec)]
            if found:
                ci = found[-1]
                kind = 'regs'
            else:
                ci = None
                for i in calls:
                    f = flow.value(stmts[i][2], key, i)
                    if f is not None and f[0] == 'closure' and f[1] in helpers and isinstance(stmts[i][5], int) \
                            and stmts[i][5] > 0:
                        ci = i
                if ci is None:
                    continue
                kind = 'result'
            st = stmts[ci]
            f = flow.value(st[2], key, ci)
            site = Site(pid, key, f[1] if f is not None and f[0] == 'closure' and isinstance(f[1], int) else None, kind)
            sites.append(site)
            # What the call is handed decides what it writes: only a function the program makes (a
            # closure), handed constants besides the register array, does the same at every execution.
            # A value read at run time (a global, an argument, a result) can differ at another one, even
            # where it was the same at every visit of this run.
            regs_args = [j for j, a in enumerate(st[3]) if kind == 'regs' and flow.is_regs_arg(a, key, ci, rec, stmts)]
            fn = flow.fixed(st[2], key, ci)
            site.fixed = fn is not None and fn[0] in ('closure', 'kfn') and \
                all(j in regs_args or (flow.fixed(a, key, ci) or ('closure',))[0] != 'closure' for j, a in enumerate(st[3]))
            if site.callee is None and fn is not None and fn[0] == 'closure' and isinstance(fn[1], int):
                site.callee = fn[1]
            named = _named(flow, st, key, ci, regs_args)
            after, known = [], True
            for x in stmts[ci + 1:]:
                w = _writes(x)
                if w is None:
                    known = False
                    break
                after.append(w)
            if caps is None:
                caps, live = captured(pir), Live(pir)
            reads = live.after(key, ci)

            def free(r):
                # (a register the text could show a value for right after the call: not captured by a
                # closure of the program, not written again by the instruction's own statements after it,
                # and read after the call before it is written again)
                return isinstance(r, int) and known and r not in caps and not any(_hits(w, r) for w in after) \
                    and (reads is None or r in reads)
            # the registers the run saw the call change (at some visit), when it saw every call
            # (harness.lua orcPend: a VM function as the callee), else None
            written = _seen_writes(rec)
            rec2 = _after(rec, oracle, pid, term) if len(found) <= 1 else None
            site.ran = rec2 is not None and known
            if not site.ran:
                if kind == 'regs':
                    # (what the call may have set shows as not shown, not as the value from before it)
                    site.marked = {r for r in named | (written or set()) if free(r)}
                    if site.marked:
                        node.tree = (stmts[:ci + 1] + [('set', ('r', r), ('opaque', MARK)) for r in sorted(site.marked)]
                                     + stmts[ci + 1:], term)
                continue
            before, seen = _values(rec), _values(rec2)
            if kind == 'result':
                # every result goes straight to a register, which nothing after it in the
                # instruction writes again: each is shown as the value the run saw there
                uses = []
                _mv_reads(list(stmts[ci + 1:]) + [term] + _nested(node.tree), st[1], uses)
                sets = [(j, x) for j, x in enumerate(stmts) if j > ci and x[0] == 'set' and x[1][0] == 'r'
                        and x[2][0] == 'mv' and x[2][1] == st[1]]
                if not sets or len(sets) != len(uses):
                    site.ran = False
                    continue
                new = {}
                for j, x in sets:
                    r = x[1][1]
                    later = [_writes(y) for y in stmts[j + 1:]]
                    if not site.fixed or r in caps or any(_hits(w, r) for w in later) or r not in seen:
                        site.unproved.add(r)
                        continue
                    new[j] = ('set', x[1], _const(seen[r]))
                    site.shown[r] = seen[r]
                if site.unproved:
                    # (the text keeps `R[a] = helper(...)`, the script's code as it is)
                    site.shown = {}
                    continue
                stmts = list(stmts)
                for j, x in new.items():
                    stmts[j] = x
                stmts[ci] = st[:5] + (0,)
                node.tree = (stmts, term)
                continue
            put = []
            for r in sorted(set(before) | set(seen) | named | (written or set()), key=lambda x: (not isinstance(x, int), x)):
                if not free(r):
                    continue
                # (the call names it, or the run saw the call change it)
                may = r in named or (written is not None and r in written)
                if written is not None and not may:
                    continue        # (the run saw every call leave it alone)
                own = [x for x in stmts[:ci] if _hits(_writes(x), r)]
                old = before.get(r)
                if own:
                    last = own[-1]
                    if not (last[0] == 'set' and last[1] == ('r', r) and last[2][0] == 'k'):
                        # (the instruction computes the register itself before the call: its text stands,
                        # unless the call may write it, which the run then does not show)
                        if may:
                            site.unproved.add(r)
                        continue
                    old = last[2][1]
                if r in seen:
                    v = seen[r]
                    if old is not None and _same(old, v):
                        continue        # (it held that value before the call)
                    if site.fixed:
                        put.append(('set', ('r', r), _const(v)))
                        site.shown[r] = v
                    else:
                        site.unproved.add(r)
                elif own or r in before or may:
                    # one value before the call (or one the call may set) and none after it: the call
                    # changed it, and the run does not show to what
                    site.unproved.add(r)
                # (else: no one value before or after the call, and nothing says the call sets it)
            put += [('set', ('r', r), ('opaque', MARK)) for r in sorted(site.unproved)]
            if put:
                node.tree = (stmts[:ci + 1] + put + stmts[ci + 1:], term)
    return sites


def _helper(k, callers=None):
    """what set program k aside as a helper of the build, in words: the evidence, not a verdict (the
    pipeline's choice puts the script on another interpreter than k's; k's own code is not looked at)"""
    c = (callers or {}).get(k)
    return 'set aside as a helper of the build (on another interpreter than the program taken for the script%s)' % (
        '; first called by program %d' % c if isinstance(c, int) and c > 0 else '')


def _who(callee, helpers, callers=None):
    if callee in helpers:
        return 'program %d, %s,' % (callee, _helper(callee, callers))
    return 'program %d' % callee if callee is not None else 'a function'


def notes(sites, helpers=(), callers=None):
    """Run notes on the call sites show_run_values found: `[runvals] ...` where the values are
    shown (not a defect), `[runvals-missing] ...` where the run proved no value for something a
    call wrote or may have written (a register it names or the run saw it change, printed as MARK;
    or a call that did not run): the result is PARTIAL, see run.report.  `callers`: {program: the
    program that first called it}, for the words on a helper of the build."""
    helpers = set(helpers or ())
    groups = {}
    for s in sites:
        groups.setdefault((s.callee, s.kind), []).append(s)
    out = []
    for (callee, kind), group in sorted(groups.items(), key=lambda kv: (kv[0][0] is None, kv[0][0] or 0, kv[0][1])):
        shown = [s for s in group if s.shown]
        # (one register the run does not prove is enough: the site is not complete, whatever else it shows)
        missing = [s for s in group if not s.ran or s.unproved]
        if not shown and not missing:
            continue
        who = _who(callee, helpers, callers)
        if kind == 'regs':
            what = 'are handed the register array of the code that calls them (printed `({})`) and write into its variables'
        else:
            what = 'return values that the code that calls them keeps in variables'
        if shown:
            out.append('[runvals] %d call(s) of %s %s: after %s the text shows the values they %s as the run saw them '
                       '(%d value(s))' % (len(group), who, what, 'each of them' if len(shown) == len(group) else
                                          '%d of them' % len(shown), 'wrote' if kind == 'regs' else 'returned',
                                          sum(len(s.shown) for s in shown)))
        if missing:
            there = ('after those calls a variable the call may have set is printed `nil --[[%s]]` (where the call names '
                     'it or the run saw the call change it), and a constant handed to the call stays as it was (encrypted)'
                     % MARK if kind == 'regs' else
                     'there the text keeps the call, and the constants handed to it stay as they were (encrypted)')
            out.append('[runvals-missing] %d call(s) of %s %s, and for %s the run does not show one value of what they %s '
                       '(the call did not run, it went on to more than one instruction, what followed it held different '
                       'values at different times, or what it is handed is not constant): %s' % (
                           len(group), who, what, 'all of them' if len(missing) == len(group) else
                           '%d of them' % len(missing), 'wrote' if kind == 'regs' else 'returned', there))
    return out


def labels(sites, helpers, protos, callers=None):
    """{program: comment} for the helpers of the build that are in the result: printed as the first
    line of their body (structure.Decompiler.labels), so that a routine the pipeline set aside is not
    read as the script's code.  The words give the evidence (_helper), not a verdict on the code."""
    out = {}
    for k in sorted(set(helpers or ()) & set(protos)):
        text = 'program %d: %s' % (k, _helper(k, callers))
        mine = [s for s in sites if s.callee == k]
        if any(s.kind == 'regs' for s in mine):
            text += '.  It writes into the variables of the code that calls it (it is handed that code\'s register array, ' \
                    'printed `({})`)'
        if any(s.shown for s in mine):
            text += '; after its calls the text shows what it wrote or returned as the run saw it'
        out[k] = text
    return out
