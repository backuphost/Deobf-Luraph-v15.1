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
visit (harness.lua, the oracle's `r` and `s`; not for code only the exercise or probe pass ran).
For an instruction I whose tree calls a function with the register array, and whose every execution
went on to the same instruction J (the successors the run took), a register r

  * that I's own statements after the call do not write (call results, later statements),
  * that no function of the program captures by reference (a variable a closure shares changes
    through the closure, which the text shows),
  * that J saw holding one value V at every visit,
  * and that did not hold V already before the call (I's entry value, or a constant I itself
    stored there before the call)

was given V by the call: `R[r] = V` goes in right after the call statement.  Nothing goes in where
the run did not prove a value (an instruction that did not run, an instruction followed by more than
one, a register that held different values at different visits).  Registers nothing reads
afterwards are dropped again with the other dead stores (ircfg.ProtoCFG._dead_stores).

A call of a helper of the build (a program the pipeline names one) whose results go straight to
registers is shown the same way: `R[a] = helper(enc)` becomes `helper(enc); R[a] = V` when the next
instruction saw V in R[a] at every visit.  A function of the script keeps its call result: there the
text is the script's own code, whatever value it had.

Calls are never dropped: whether a helper has effects besides what it wrote is not known (its own
lift can be incomplete).  pipeline labels the helpers of the build in the text (labels) and puts a
note in the report (notes).
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
        for a in stmts[i][3]:
            if self.value(a, key, i) == ('regs',):
                return True
            if rec is None or rec.get('p') or not rec.get('a'):
                continue
            e, at = a, i
            while e[0] == 't':
                d = self.tdef(e[1], key, at)
                if d is None or d[0] != key or d[1] is None:
                    break
                e, at = d[2], d[1]
            if e[0] == 'r' and e[1] in rec['a'] and not any(_hits(_writes(x), e[1]) for x in stmts[:at]):
                return True
        return False

    def may_write_regs(self, st):
        """a call that may be handed the register array (directly, or a register that holds it
        somewhere in the program)"""
        return st[0] == 'call' and any(self._is_regs_expr(a) for a in st[3])

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


class Site:
    """a call whose effect on its caller's registers the lifted code does not show: a call handed
    the caller's register array, or a call of a helper of the build whose results go to registers"""
    __slots__ = ('pid', 'key', 'callee', 'kind', 'shown', 'unproved', 'ran')

    def __init__(self, pid, key, callee, kind):
        self.pid, self.key, self.callee, self.kind = pid, key, callee, kind
        self.shown = {}         # register -> the value the run saw after the call
        self.unproved = set()   # registers the call changed to no single value the run recorded
        self.ran = False        # the instruction ran and went on to a single instruction that ran

    def __repr__(self):
        return 'Site(%s %s %s callee=%s shown=%s unproved=%s ran=%s)' % (
            self.pid, self.key, self.kind, self.callee, sorted(self.shown), sorted(self.unproved), self.ran)


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


def show_run_values(protos, oracle, helpers=()):
    """Put the values the run proves where the lifted code would show something else (see the
    module's text): after every call handed its caller's register array, and in place of the
    results of a call of a helper of the build (`helpers`: program numbers) that go to registers
    (`R[n] = helper(...)` becomes `helper(...); R[n] = V`, for every result the run proves).
    Changes the instruction trees of `protos` in place and returns the call sites found, [Site]."""
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
            if len(found) > 1:
                continue
            rec2 = _after(rec, oracle, pid, term)
            if rec2 is None:
                continue
            after = []
            for x in stmts[ci + 1:]:
                w = _writes(x)
                if w is None:
                    break
                after.append(w)
            else:
                site.ran = True
            if not site.ran:
                continue
            if caps is None:
                caps = captured(pir)
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
                    if r in caps or any(_hits(w, r) for w in later) or r not in seen:
                        site.unproved.add(r)
                        continue
                    new[j] = ('set', x[1], _const(seen[r]))
                    site.shown[r] = seen[r]
                if site.unproved:
                    site.shown = {}
                    continue
                stmts = list(stmts)
                for j, x in new.items():
                    stmts[j] = x
                stmts[ci] = st[:5] + (0,)
                node.tree = (stmts, term)
                continue
            put = []
            for r in sorted(set(before) | set(seen), key=lambda x: (not isinstance(x, int), x)):
                if not isinstance(r, int) or r in caps or any(_hits(w, r) for w in after):
                    continue
                own = [x for x in stmts[:ci] if _hits(_writes(x), r)]
                if r not in seen:
                    if r in before and not own:
                        # (held one value at every visit of the call, none at the next instruction:
                        # changed by the call)
                        site.unproved.add(r)
                    continue
                v, old = seen[r], before.get(r)
                if own:
                    last = own[-1]
                    if not (last[0] == 'set' and last[1] == ('r', r) and last[2][0] == 'k'):
                        continue        # (the instruction computes the register itself before the call: its text stands)
                    old = last[2][1]
                if old is not None and _same(old, v):
                    continue
                put.append(('set', ('r', r), _const(v)))
                site.shown[r] = v
            if put:
                node.tree = (stmts[:ci + 1] + put + stmts[ci + 1:], term)
    return sites


def _who(callee, helpers):
    if callee in helpers:
        return 'program %d, a helper of the build (Luraph\'s routine on another interpreter than the script\'s program, ' \
               'not the script\'s code)' % callee
    return 'program %d' % callee if callee is not None else 'a function'


def notes(sites, helpers=()):
    """Run notes on the call sites show_run_values found: `[runvals] ...` where the values are
    shown (not a defect), `[runvals-missing] ...` where the run proved no value for what a call
    wrote (the text shows the earlier value there: the result is PARTIAL, see run.report)."""
    helpers = set(helpers or ())
    groups = {}
    for s in sites:
        groups.setdefault((s.callee, s.kind), []).append(s)
    out = []
    for (callee, kind), group in sorted(groups.items(), key=lambda kv: (kv[0][0] is None, kv[0][0] or 0, kv[0][1])):
        shown = [s for s in group if s.shown]
        missing = [s for s in group if not s.shown and (not s.ran or s.unproved)]
        if not shown and not missing:
            continue
        who = _who(callee, helpers)
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
            there = ('there the text shows the value the variable had before the call, and a constant handed to the call '
                     'stays as it was (encrypted)' if kind == 'regs' else
                     'there the text keeps the call, and the constants handed to it stay as they were (encrypted)')
            out.append('[runvals-missing] %d call(s) of %s %s, and for %s the run shows no single value of what they %s '
                       '(code that did not run, or values that differed between runs of the call): %s' % (
                           len(group), who, what, 'all of them' if len(missing) == len(group) else
                           '%d of them' % len(missing), 'wrote' if kind == 'regs' else 'returned', there))
    return out


def labels(sites, helpers, protos):
    """{program: comment} for the helpers of the build that are in the result: printed as the first
    line of their body (structure.Decompiler.labels), so that a routine of the build is never read
    as the script's code"""
    out = {}
    for k in sorted(set(helpers or ()) & set(protos)):
        text = 'program %d: a helper of the build (Luraph\'s routine on another interpreter than the script\'s program), ' \
               'not the script\'s code' % k
        mine = [s for s in sites if s.callee == k]
        if any(s.kind == 'regs' for s in mine):
            text += '.  It writes into the variables of the code that calls it (it is handed that code\'s register array, ' \
                    'printed `({})`)'
        if any(s.shown for s in mine):
            text += '; after its calls the text shows what it wrote or returned as the run saw it'
        out[k] = text
    return out
