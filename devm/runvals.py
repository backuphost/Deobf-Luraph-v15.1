"""Values of the traced run where the lifted code would show something else.

A VM function can hand its own register array to a call (`f(..., R, n)` in the interpreter, printed
`f(..., ({}), n)`): the function called can then write any register of its caller.  Luraph builds
do this with routines of the build: a string decoder on another interpreter is handed an encrypted
constant, the caller's register array and the number of a register, and stores the plain string
there.  The code that follows reads that register.  Nothing in the lifted code of the caller writes
it, so the text shows the register's earlier value (the encrypted constant) wherever the script
reads it after the call.

Such a text is never complete.  What a call handed the register array changes is shown only as far
as the run saw it, whatever is proved below: a register the call does not name and the run did not
see it change keeps its value from before the call in the text, and the marks described below are best
effort: synthetic probes still find cases they miss (a register array handed through a temporary in an
arm of a branch, a change that a later statement of the same instruction also writes, a shared
variable a function of the program assigns at some other time, a later call of the same instruction
that changes the register again).  So every call handed the array (a
site found below, or a call the code shows handed it anywhere: handed) gives the run note
`[runvals-handed]` (handed_note), and run.report counts the printed `({})` in the text as well: either
alone makes the result PARTIAL, also where every value the call changed is shown and proved, and
also where this module did not run (the proto-table layout has no traced run) or found no site.

The traced run says what the registers held.  The harness records, for every instruction that ran,
the numbers, strings and booleans each register held on entry when they were the same on every
visit (harness.lua, the oracle's `r` and `s`; not for code only the exercise or probe pass ran), and
for an instruction whose call entered a VM function, which of the caller's registers had changed
when the caller went on (`w`, and `h`: at how many of the visits that was seen).  Where every visit
saw a VM function entered (`h` == `n`), a register in `w` is one the call may have set, whether or
not the record covers everything the instruction's calls did: `w` only ever adds a register to those,
it never leaves one out (another call, or a callee that is no VM function, changes registers the
harness does not see change).  That the run saw a call leave a register alone counts for the proof of
another call (Flow.spare) only where the instruction's one call is the one handed the register array,
calls a closure of the program and meets the rules below (_trusted).  For an instruction I whose tree
calls a function with the register array, and whose every execution went on to the same instruction J
(the successors the run took), a register r

  * that I's own statements after the call do not write before reading it (call results, later
    statements; one they read first is marked: J saw what they made of it, not what the call left),
  * that no function of the program captures by reference (a variable a closure shares changes
    through the closure, whose text shows it; but one the call names or the run saw it change --
    by `w`, else by the values I and J saw -- is marked, unless the call does not name it, a function
    of the program assigns it through its upvalue, and that function can have made the change: the
    function called is that function itself, makes it or shares it (through pcall / xpcall: the
    function handed to them), or is not resolved and a function that assigns the variable ran (the
    run has a record of its statement that assigns it).  A library function called is not that
    function, nor is a resolved one that does not assign it: another function that assigns the
    variable, or one that never ran, is not the call's doing: _exempt),
  * that the code reads after the call before writing it again (Live),
  * that J saw holding one value V at every visit,
  * and that did not hold V already before the call (I's entry value, or a constant I itself
    stored there before the call)

was given V by the call: `R[r] = V --[[the run's value after the call]]` (PROVED) goes in right after
the call statement -- but only when what the call writes is the same at every execution of the
call, as far as the code shows (_Proof):

  1. the function called is a closure the program makes (Flow.fixed; a library function handed the
     register array is not followed);
  2. every argument besides the register array is a constant or a standard library function (not
     another function of the interpreter's environment, such as os.clock), followed back through
     every definition that can reach the call; a register a function of the program shares by
     reference and assigns through its upvalue is not, wherever that function is called;
  3. every variable the closure captures holds a constant, a standard library function or a closure
     that meets these rules, and nothing changes it after the closure is made;
  4. the closure's lifted code, its nested functions included, reads no global but the standard
     library's tables and functions (not a random source, nor coroutine.yield, nor a field the library
     does not have, also not through rawget / rawset on a table it did not make, called directly or
     through pcall / xpcall, nor through getmetatable of a value it did not make), changes no variable
     it shares, and only stores into the register array it is handed (never reads it).

and, for all the calls of the program together (_settle): every call handed the program's register
array meets these rules, and the array reaches no other code (_escape: a call that may be handed it
but is not followed, the array kept in a table or a variable, stored into through a register).  A
call that fails a rule can keep the array and change any register later -- through any call, or a
metamethod -- which no statement shows, so `fixed`, which follows the statements, proves nothing
in that program.

A value the script read at run time (a global, an argument, a result) can differ at another
execution, however often this run saw the same one.  When a rule fails, nothing is substituted.
When no rule fails but the check cannot be completed, the run's value is shown with a comment
right at it that says what is assumed (every such comment begins with ASSUMED_HEAD), the notes say so
(`[runvals-assumed]`) and the result is PARTIAL: that the value depends only on what the call is
handed is then assumed, not proved.  That is the case where the code of the function called that ran
has lift errors or parts that did not lift (or lifted with holes): ASSUMED; where it uses tostring,
next, pairs, table.foreach or string.format, whose result can differ for the same arguments, or is
handed one of them, or shares a variable that holds one: ASSUMED_LIB; where the program that makes
the call has instructions that can be reached and did not lift (or lifted with an error), or a
variable the call depends on is shared with a function that did not all lift: ASSUMED_CALLER; where
another call handed the same register array is only assumed to meet the rules: ASSUMED_OTHER.  No
value this module puts in is printed bare: `('runval', V, comment)`, printed `V --[[comment]]` by
structure.Expr, with PROVED or one of the ASSUMED comments; both are literally true (V is what the
run had in that register right after the call, at every visit).

Where the run does not prove a value for a register the call may have set -- one it changed to
different values at different visits, one the call names (a constant register number handed with
the register array) or the run saw it change (`w`), with no one value after the call, or any of
these when a rule above fails -- `R[r] = nil --[[...]]` (MARK) goes in after the call: the text does
not show the earlier value as the current one, and the call counts as not shown (notes:
`[runvals-missing]`, the result is PARTIAL).  The same after a call that did not run or went on to
more than one instruction, and after every call of an instruction with several calls handed the
register array, for the registers it names and those the run saw any of them change.  The same, too,
after the calls that are no site of an instruction's own (_other_calls): a call the code shows handed
the register array in an arm of a branch (the MARK goes in the arm, right after it) or inside another
value, for the registers it names and those the run saw the instruction's calls change; and, in an
instruction with no call handed the register array, a call after which the run saw a variable change
that no statement of the instruction writes after its first call and no function that shares it and
assigns it through its upvalue in a statement the run executed can have changed (only the register
array reaches a variable nothing shares: the call or code it ran had it, kept somewhere the code does
not show, as `{R}`, which lifts as a value the lift could not describe).  A register
that held no one value before the call nor after it, that the call does not name and that the run did
not see it change (a callee that is no VM function is not seen), is left as the code has it (best
effort, said by the `[runvals-handed]` note); so is a change the instruction's own statements after its
first call can explain, after a call the code does not show handed the register array, and everything
after a call that fails a rule but changed nothing the code reads afterwards, as far as the run saw:
that call gets no mark and no note of its own (its failure makes the other values of the program not
proved).  Registers nothing reads afterwards are dropped again with the other dead stores
(ircfg.ProtoCFG._dead_stores).

A call of a helper of the build (a program the pipeline names one) whose results go straight to
registers is shown the same way: `R[a] = helper(enc)` becomes `helper(enc); R[a] = V --[[...]]`
when the next instruction saw V in R[a] at every visit and the rules above hold.  A function of the
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
    if st[0] == 'params' and isinstance(st[1], int) and isinstance(st[2], int):
        # (the function's fixed parameters copied into registers base .. base + n - 1)
        return set(range(st[1], st[1] + st[2]))
    return None


def _writes_any(st):
    """_writes, with the loop statements' registers (a base and the three around it, as _loop_reads)"""
    w = _writes(st)
    if w is None and st[0] in ('numforprep', 'numforloop', 'genforprep', 'genforloop'):
        out = set()
        _loop_reads(st[1:], out)
        return out
    if w is None and st[0] in ('forpop', 'vararg'):
        return set()
    return w


# what stands in the text next to a value the run saw where its proof could not be completed
# (printed `V --[[...]]`, see structure.Expr): every such comment begins with ASSUMED_HEAD, and says
# what could not be checked: the code of the function called that ran did not all lift (ASSUMED);
# it uses a library function whose result can depend on an object's identity or on the order of a
# table's keys (ASSUMED_LIB); the code that makes the call did not all lift (ASSUMED_CALLER); another
# call handed the same register array could not be checked in full (ASSUMED_OTHER)
ASSUMED_HEAD = "the run's value; assumed to depend only on the call's inputs: "
ASSUMED = ASSUMED_HEAD + "the function called was not fully lifted"
ASSUMED_LIB = ASSUMED_HEAD + "the function called uses tostring, next, pairs, table.foreach or string.format"
ASSUMED_CALLER = ASSUMED_HEAD + "the calling code was not fully lifted"
ASSUMED_OTHER = ASSUMED_HEAD + "another call handed the register array was not fully checked"
_COMMENT = {'lift': ASSUMED, 'lib': ASSUMED_LIB, 'caller': ASSUMED_CALLER, 'other': ASSUMED_OTHER}
# ... and next to a value whose proof was completed: no value show_run_values puts in is printed bare.  Both
# kinds of comment are literally true: the value is the one the run had in that variable right after the call
# (the next instruction saw it at every visit, and nothing between the call and that instruction writes it)
PROVED = "the run's value after the call"

# what a called function may read and still do the same at every execution: the standard library's
# tables and functions (the members of the tables listed: a name the library does not have is a place
# where a script can keep a value), but not a source of random numbers or of the calling context
_LIB_TABLES = frozenset(('string', 'table', 'math', 'bit32', 'utf8', 'buffer', 'coroutine'))
_LIB_MEMBERS = {
    'string': frozenset('byte char dump find format gfind gmatch gsub len lower match pack packsize rep reverse split '
                        'sub unpack upper'.split()),
    'table': frozenset('clear clone concat create find foreach foreachi freeze getn insert isfrozen maxn move pack '
                       'remove setn sort unpack'.split()),
    'math': frozenset('abs acos asin atan atan2 ceil clamp cos cosh deg exp floor fmod frexp huge ldexp lerp log log10 '
                      'map max maxinteger min mininteger modf noise pi pow rad random randomseed round sign sin sinh '
                      'sqrt tan tanh tointeger type ult'.split()),
    'bit32': frozenset('arshift band bnot bor btest bxor byteswap countlz countrz extract lrotate lshift replace rrotate '
                       'rshift'.split()),
    'utf8': frozenset('char charpattern codepoint codes graphemes len nfcnormalize nfdnormalize offset'.split()),
    'buffer': frozenset('copy create fill fromstring len readbits readf32 readf64 readi16 readi32 readi8 readstring '
                        'readu16 readu32 readu8 tostring writebits writef32 writef64 writei16 writei32 writei8 '
                        'writestring writeu16 writeu32 writeu8'.split()),
    'coroutine': frozenset('close create isyieldable resume running status wrap yield'.split()),
}
_LIB_FUNCS = frozenset(('select', 'type', 'typeof', 'tostring', 'tonumber', 'pcall', 'xpcall', 'error', 'assert',
                        'next', 'pairs', 'ipairs', 'rawget', 'rawset', 'rawequal', 'rawlen', 'setmetatable',
                        'getmetatable', 'unpack'))
# (coroutine.yield: what it returns is whatever the code that resumes the function hands it)
_NOT_LIB = frozenset(('math.random', 'math.randomseed', 'coroutine.running', 'coroutine.isyieldable',
                      'coroutine.yield'))
# ... of which these can give another result at another execution for the same arguments: `tostring`
# of a table or a function is its address (`string.format` with %s or %p the same), and `next`,
# `pairs` and `table.foreach` go through a table's keys in the order they are stored in: a function
# called that uses one of them, is handed one or shares a variable that holds one is taken to do the
# same at every execution only as an assumption (ASSUMED_LIB)
_IDENTITY = frozenset(('tostring', 'next', 'pairs', 'string.format', 'table.foreach'))
# library functions that read or change a field of the table they are handed without its metatable: on a
# library table that is a field the library may not have (a place a script can keep a value)
_RAW = frozenset(('rawget', 'rawset'))


def _lib_name(name):
    """a standard library table or function that is the same at every execution"""
    if not isinstance(name, str) or name in _NOT_LIB:
        return False
    if name in _LIB_FUNCS:
        return True
    head, dot, member = name.partition('.')
    return head in _LIB_TABLES and (not dot or member in _LIB_MEMBERS[head])


def _all_stmts(tree):
    """the statements of a tree, those of its branch arms included"""
    return list(tree[0]) + _nested(tree)


def _closures(x, acc):
    """collect the ('closure', proto, caps) expressions anywhere in x (a statement or expression)"""
    if isinstance(x, tuple):
        if len(x) == 3 and x[0] == 'closure' and isinstance(x[2], tuple):
            acc.append(x)
        for y in x:
            _closures(y, acc)
    elif isinstance(x, list):
        for y in x:
            _closures(y, acc)
    return acc


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
    # leave register r alone (show_run_values sets it from the oracle's `h` / `w`, see _trusted)
    spare = None
    # {register: True / False / None}: whether a function of the program assigns to the variable it
    # shares with the register (a closure that captures it by reference; None: not known)
    ref_assigned = {}
    # a set while a call is checked (show_run_values): the reasons `fixed` met why its answer is only
    # assumed (a variable shared with a function that did not all lift)
    unsure = None
    _wlocs = None

    def _write_index(self):
        """{register: {(instruction, index or None)}} of the statements that write it, [(instruction,
        index or None, base)] of the statements that write every register from a base up, and the
        statements whose writes are not known or that may hand the register array to a call"""
        if self._wlocs is None:
            regs, frm, other = {}, [], []
            for k, node in self.pir.nodes.items():
                if node.tree is None:
                    other.append((k, None, None))
                    continue
                spots = [(j, st) for j, st in enumerate(node.tree[0])] + [(None, st) for st in _nested(node.tree)]
                for j, st in spots:
                    w = _writes_any(st)
                    if w is None or self.may_write_regs(st):
                        other.append((k, j, st))
                    if isinstance(w, tuple):
                        frm.append((k, j, w[1]))
                    elif w:
                        for r in w:
                            regs.setdefault(r, set()).add((k, j))
            self._wlocs = (regs, frm, other)
        return self._wlocs

    def written_elsewhere(self, r, defs):
        """True when a statement of the program other than `defs` ({(instruction, index)}) may write
        register r (an instruction that did not lift, a statement of unknown effect, a call that may
        be handed the register array unless the run saw it leave r alone)"""
        regs, frm, other = self._write_index()
        if any(loc not in defs for loc in regs.get(r, ())):
            return True
        if any(isinstance(r, int) and r >= b for _k, _j, b in frm):
            return True
        for k, _j, st in other:
            if st is None or _writes_any(st) is None or not (self.spare and self.spare(k, r)):
                return True
        return False

    def capture(self, c, key, idx, depth=0):
        """What capture c of a function made by top-level statement idx of instruction key holds at
        every execution, when the code alone says so (as `fixed`): a constant, a standard library
        function, or a function the program makes (`fixed` of its own captures); None otherwise.  A
        variable shared by reference must also not change after the function is made: neither the
        program (no other statement that may write the register) nor a function through its upvalue."""
        if c[0] == 'const':
            v = c[1]
            if v is None or isinstance(v, (bool, str)) or P.is_num(v):
                return _const(v)
            name = v.name if isinstance(v, P.Builtin) else v.text if isinstance(v, P.GlobalPath) else None
            return ('kfn', name) if _lib_name(name) else None
        if c[0] not in ('val', 'ref') or not isinstance(c[1], int):
            return None         # (an upvalue of the function that makes it, or a name: not followed)
        # (`fixed` also says no for a variable a function assigns through its upvalue)
        v = self.fixed(('r', c[1]), key, idx, depth + 1)
        if v is None or c[0] == 'val':
            return v
        defs = self.reaching(c[1], key, idx)
        if defs is None or self.written_elsewhere(c[1], {(d[0], d[1]) for d in defs}):
            return None
        return v

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

    def fixed(self, e, key, idx, depth=0, loose=False):
        """What the expression e (read by top-level statement idx of instruction key) is at every
        execution, when the code alone says so: a constant, a function the program makes (`('closure',
        proto, captures)`, with what each capture holds by `capture`, None for one that is not
        fixed) or a standard library function (_lib_name: not another function of the interpreter's
        environment), or arithmetic, comparison or logic on constants -- followed back
        through temporaries and through every definition of a register that can reach the statement
        (all of them alike).  None for anything else (a value read at run time: a global, an argument,
        a result, a table field), and for a register a function of the program shares by reference and
        assigns through its upvalue (a call of that function changes it, wherever it is called; when
        that is not known, the answer is only assumed: `unsure`).  `loose`: without that last rule (what
        the code puts there, for the register numbers a call names)."""
        if depth > 24:
            return None
        t = e[0]
        if t == 'k':
            return e
        if t == 'kfn':
            # (a standard library table or function only: another function of the interpreter's environment, such
            # as os.clock, can give something else at every execution)
            return e if _lib_name(e[1]) else None
        if t == 'closure':
            caps = e[2] if len(e) > 2 and isinstance(e[2], tuple) else ()
            return ('closure', e[1], tuple(self.capture(c, key, idx, depth + 1) if isinstance(c, tuple) and c else None
                                           for c in caps))
        if t == 't':
            d = self.tdef(e[1], key, idx)
            return None if d is None or d[1] is None else self.fixed(d[2], d[0], d[1], depth + 1, loose)
        if t == 'r':
            if not loose and e[1] in self.ref_assigned:
                if self.ref_assigned[e[1]] is True:
                    return None
                if self.ref_assigned[e[1]] is None and self.unsure is not None:
                    self.unsure.add('a function that shares a variable the call depends on (by reference) did not all '
                                    'lift, so whether it changes that variable is not known')
            defs = self.reaching(e[1], key, idx)
            if not defs:
                return None
            got = [self.fixed(d[2], d[0], d[1], depth + 1, loose) for d in defs]
            return got[0] if got[0] is not None and all(g == got[0] for g in got) else None
        if t in ('bin', 'and', 'or'):
            x, y = (e[2], e[3]) if t == 'bin' else (e[1], e[2])
            a, b = self.fixed(x, key, idx, depth + 1, loose), self.fixed(y, key, idx, depth + 1, loose)
            if a is None or b is None or a[0] == 'closure' or b[0] == 'closure':
                return None
            return ('bin', e[1], a, b) if t == 'bin' else (t, a, b)
        if t == 'un':
            a = self.fixed(e[2], key, idx, depth + 1, loose)
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
    __slots__ = ('pid', 'key', 'callee', 'kind', 'shown', 'unproved', 'ran', 'fixed', 'assumed', 'why', 'marked',
                 'comment', 'call')

    def __init__(self, pid, key, callee, kind, call=None):
        self.pid, self.key, self.callee, self.kind = pid, key, callee, kind
        self.call = call        # the call's id (its statement's second item)
        self.shown = {}         # register -> the value the run saw after the call
        # registers the call changed (or may have set: the ones it names, the ones the run saw it
        # change) for which the run proves no value: no one value after the call, or what the call
        # writes is not shown to be the same at every execution (MARK goes in after a call handed
        # the register array)
        self.unproved = set()
        self.ran = False        # the instruction ran and went on to a single instruction that ran
        # what the call writes is the same at every execution of the call (_Proof: a closure of the
        # program, handed constants besides the register array, capturing only fixed values, whose
        # code reads nothing else), proved or assumed
        self.fixed = False
        # ... assumed only: the check could not be completed (the code of the function called that
        # ran did not all lift, ...); the values shown carry the comment `comment` (ASSUMED, ...)
        self.assumed = False
        self.comment = None
        self.why = None         # why it is not proved (or only assumed), in words
        self.marked = set()     # (a call that did not run: the registers given MARK after it)

    def __repr__(self):
        return 'Site(%s %s %s callee=%s shown=%s unproved=%s ran=%s fixed=%s%s%s)' % (
            self.pid, self.key, self.kind, self.callee, sorted(self.shown), sorted(self.unproved), self.ran, self.fixed,
            ' assumed' if self.assumed else '', ' why=%r' % self.why if self.why else '')


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


def _idents(v, out):
    """add the library functions of _IDENTITY that a value Flow.fixed gave holds (not inside a function it
    makes: _Proof.check looks at those) to out"""
    if isinstance(v, tuple) and v:
        if v[0] == 'kfn':
            if v[1] in _IDENTITY:
                out.add(v[1])
        elif v[0] != 'closure':
            for y in v[1:]:
                _idents(y, out)
    return out


# what stands in the text for a register that a call handed the register array may have set, where the
# run does not prove one value for it (printed `nil --[[...]]`, see structure.Expr)
MARK = 'set by a call handed the register array: value not shown'


def _seen_writes(rec):
    """the registers the run saw the calls handed the register array in an instruction (record rec)
    change, when it saw every such call (harness.lua orcPend: the callee a VM function), else None"""
    if rec is None or rec.get('p') or not rec.get('h') or rec['h'] != rec['n']:
        return None
    return rec.get('w') or set()


def _named(flow, st, key, ci, regs_args):
    """the registers a call names: the integer constants handed after the register array (`f(enc, R, n)`)"""
    out = set()
    if not regs_args:
        return out
    for a in st[3][regs_args[0] + 1:]:
        v = flow.fixed(a, key, ci, loose=True)
        if v is not None and v[0] == 'k' and P.is_num(v[1]) and not isinstance(v[1], bool) \
                and float(v[1]).is_integer() and 0 <= v[1] < 1 << 20:
            out.add(int(v[1]))
    return out


def _call_status(flow, proof, stmts, key, ci, rec, kind):
    """(status, why, kind of assumption, the function called as Flow.fixed has it, the positions of the
    arguments that are the register array) of call statement ci of instruction key: whether what the
    call writes is the same at every execution as far as the code shows (rules 1 to 4 of the module's
    text; status 'proved', 'assumed' or None)"""
    st = stmts[ci]
    regs_args = [j for j, a in enumerate(st[3]) if kind == 'regs' and flow.is_regs_arg(a, key, ci, rec, stmts)]
    flow.unsure = set()
    try:
        fn = flow.fixed(st[2], key, ci)
        if fn is None or fn[0] != 'closure' or not isinstance(fn[1], int):
            status, why, akind = proof.check(fn, regs_args)       # (it says why: rule 1)
        elif st[4] or not all(j in regs_args or (flow.fixed(a, key, ci) or ('closure',))[0] != 'closure'
                              for j, a in enumerate(st[3])):
            status, why, akind = None, 'what it is handed is not constant', None
        else:
            status, why, akind = proof.check(fn, regs_args)
            # (handed tostring, next, ...: what the function called makes of it can differ for the same arguments)
            ident = set()
            for j, a in enumerate(st[3]):
                if j not in regs_args:
                    _idents(flow.fixed(a, key, ci), ident)
            if status == 'proved' and ident:
                status, why, akind = 'assumed', 'it is handed %s, whose result can differ between executions for the ' \
                    'same arguments' % ', '.join(sorted(ident)), 'lib'
        if status == 'proved' and flow.unsure:
            status, why, akind = 'assumed', sorted(flow.unsure)[0], 'caller'
    finally:
        flow.unsure = None
    return status, why, akind, fn, regs_args


def _trusted(flow, pir, pid, oracle, proof):
    """{instruction: the registers its call changed}: the instructions of program pid whose record of
    what a call changed in its caller's registers (`w`) covers everything the call did.  The harness
    sees a call only by the VM function it enters, so `w` counts only where the instruction's one call
    is the one handed the register array, every visit saw a VM function entered (`h` == `n`), and the
    function called is a closure of the program, followed back through every definition that can reach
    the call (Flow.fixed).  (For what a call may have set, show_run_values takes `w` wherever `h` == `n`,
    covered or not, and only to add registers: what the run saw a call leave alone is used here alone.)

    Flow.spare (the run saw the call at an instruction leave a register alone) is set here, for the
    instructions whose call meets rules 1 to 4 (_call_status): only for such a call does what the run
    saw hold at every execution.  In a loop that holds when the other such calls (this one included)
    did what the run saw at the visits before: the instructions are taken all together and dropped
    until the rest hold (by induction over the run, the first visit at which one did not hold would
    come after visits at which all of them did)."""
    cand = {}
    for key, node in pir.nodes.items():
        if node.tree is None or node.error:
            continue
        rec = oracle.get((pid, key[0], key[1]))
        w = _seen_writes(rec)
        if w is None:
            continue
        stmts = node.tree[0]
        if sum(1 for st in _all_stmts(node.tree) if st[0] == 'call') != 1:
            continue
        ci = next((i for i, st in enumerate(stmts) if st[0] == 'call'), None)
        if ci is None or not flow.handed_regs(stmts, ci, key, rec):
            continue
        cand[key] = (ci, w, rec)
    good = set(cand)
    flow.spare = lambda k, r: k in good and r not in cand[k][1]
    while good:
        drop = set()
        for k in good:
            ci, _w, rec = cand[k]
            if _call_status(flow, proof, pir.nodes[k].tree[0], k, ci, rec, 'regs')[0] is None:
                drop.add(k)
        if not drop:
            break
        good -= drop
    out = {}
    for k, (ci, w, _rec) in cand.items():
        f = flow.fixed(pir.nodes[k].tree[0][ci][2], k, ci)
        if f is not None and f[0] == 'closure' and isinstance(f[1], int):
            out[k] = w
    return out


class _Holds:
    """Which registers may hold the program's own register array, at each instruction's entry: a forward
    analysis over the jumps of the lifted instructions (a register set from the array, or from a register
    or a temporary that may hold it, holds it after that; one set from anything else does not).  A
    statement in the arm of a branch may or may not run: what it sets is added, nothing is taken away.
    Flow.regs_holders says which registers hold it somewhere; this says where."""

    def __init__(self, flow, pir):
        from .lifter import tree_ends
        self.flow = flow
        succ = {k: [e[1] for e in tree_ends(n.tree) if e[0] == 'goto'] for k, n in pir.nodes.items()
                if n.tree is not None}
        self.inn = {k: set() for k in succ}
        work = list(succ)
        while work:
            k = work.pop()
            out = self.walk(pir.nodes[k].tree, set(self.inn[k]))
            for q in succ[k]:
                if q in self.inn and not out <= self.inn[q]:
                    self.inn[q] |= out
                    work.append(q)

    def holds(self, e, state, temps):
        """may the expression e be the register array (state: the registers that may hold it; temps: the
        temporaries of the instruction so far, True for one that may)"""
        if e == ('regs',):
            return True
        if e[0] == 'r':
            return e[1] in state
        if e[0] == 't':
            if e[1] in temps:
                return temps[e[1]]
            p = self.flow._plain(e)
            return p == ('regs',) or p[0] == 'r' and p[1] in state
        return False

    def inside(self, x, state, temps):
        """does x (an expression, or a list of them) hold the register array anywhere but as the table of a
        field it reads: as a value, or in a variable a function made there shares"""
        if isinstance(x, list):
            return any(self.inside(y, state, temps) for y in x)
        if not isinstance(x, tuple) or not x or not isinstance(x[0], str):
            return False
        if self.holds(x, state, temps):
            return True
        if x[0] == 'index' and len(x) == 3 and isinstance(x[1], tuple) and self.holds(x[1], state, temps):
            return self.inside(x[2], state, temps)          # (a register read through the array)
        if x[0] == 'closure' and len(x) == 3 and isinstance(x[2], tuple):
            return any(isinstance(c, tuple) and len(c) == 2 and c[0] in ('ref', 'val') and c[1] in state for c in x[2])
        return any(self.inside(y, state, temps) for y in x[1:])

    def walk(self, tree, state, visit=None, top=True):
        """the registers that may hold the array after the tree (its arms: either may run), from `state`
        at its start; visit(statement, index, state, temps, top) before each statement, and
        visit(None, terminator, state, temps, top) for the terminator"""
        stmts, term = tree
        temps = {}
        for j, st in enumerate(stmts):
            if visit:
                visit(st, j, state, temps, top)
            if st[0] == 'set' and st[1][0] == 'r':
                if self.holds(st[2], state, temps):
                    state.add(st[1][1])
                elif top:
                    state.discard(st[1][1])
            elif st[0] == 'set' and st[1][0] == 't':
                temps[st[1][1]] = self.holds(st[2], state, temps)
        if visit:
            visit(None, term, state, temps, top)
        arms = (term[2], term[3]) if term[0] == 'br' else (term[3], term[4]) if term[0] == 'forbr' else ()
        out = set(state)
        for arm in arms:
            out |= self.walk(arm, set(state), visit, False)
        return out


def _escape(flow, pir, at):
    """why the register array of program pir may reach code whose effect on its registers is not
    followed, or None: a call that may be handed it and is not one of the calls found (`at`: {(instruction,
    index of the call)}; a call in the arm of a branch, say), the array kept in a table, a global or a
    variable a function shares, handed on inside another value, or returned, or stored into through a
    register that holds it (a write the text does not show as one).  Where a register may hold it is
    _Holds'.  Instructions that did not lift, or lifted with an error, are _incomplete's."""
    holds = _Holds(flow, pir)
    found = []

    def visit(k):
        def check(st, j, state, temps, top):
            if found:
                return
            h, why = holds, None
            if st is None:
                if h.inside(_term_exprs(j), state, temps):
                    why = 'the register array is returned or handed on'
            elif st[0] == 'call':
                if any(h.holds(a, state, temps) for a in st[3]) and not (top and (k, j) in at):
                    why = 'a call that may be handed the register array is not one whose writes are followed'
                elif h.inside([st[2]] + [a for a in st[3] if not h.holds(a, state, temps)] + ([st[4]] if st[4] else []),
                              state, temps):
                    why = 'the register array is handed on inside another value'
            elif st[0] == 'set':
                if st[1][0] in ('r', 't'):
                    if not h.holds(st[2], state, temps) and h.inside(st[2], state, temps):
                        why = 'the register array is kept in another value'
                elif h.inside([st[1], st[2]], state, temps):
                    why = 'the register array is kept in a variable, or stored into through one'
            elif st[0] == 'setindex':
                if h.holds(st[1], state, temps):
                    why = 'the code stores into the register array through a variable'
                elif h.inside([st[1], st[2], st[3]], state, temps):
                    why = 'the register array is kept in a table'
            elif h.inside(list(st[1:]), state, temps):
                why = 'the register array is kept or handed on'
            if why:
                found.append(why + ' (instruction %s)' % (k,))
        return check
    for k, node in pir.nodes.items():
        if node.tree is None or node.error or k not in holds.inn:
            continue
        holds.walk(node.tree, set(holds.inn[k]), visit(k))
        if found:
            return found[0]
    return None


def _incomplete(pir):
    """why the code of program pir cannot all be followed, or None: instructions that can be reached from
    its entry along the jumps of the lifted ones and did not lift, lifted with a lift error, or are not
    there (what they do to the registers, which functions they make and which variables those share,
    is not known)"""
    from .lifter import tree_ends
    entry = getattr(pir, 'entry', None)
    todo = [entry] if entry in pir.nodes else list(pir.nodes)
    seen = set(todo)
    missing = errors = 0
    while todo:
        k = todo.pop()
        node = pir.nodes.get(k)
        if node is None or node.tree is None:
            missing += 1
            continue
        if node.error:
            errors += 1
        for end in tree_ends(node.tree):
            if end[0] == 'goto' and end[1] not in seen:
                seen.add(end[1])
                todo.append(end[1])
    if not (missing or errors):
        return None
    what = [('%d did not lift' % missing) if missing else '', ('%d lifted with a lift error' % errors) if errors else '']
    return 'the code that makes the call did not all lift: of the instructions of its function that can be reached, %s' % (
        ', '.join(x for x in what if x))


def _settle(flow, pir, psites, at):
    """what holds for the call sites of one program (psites) together.  A call handed the register array
    that fails a rule can keep the array and change any register later, through any call or a table's
    metamethod, which no statement shows: then nothing in the program is proved (`fixed` follows the
    code alone).  The same where the array reaches such code otherwise (_escape).  Where the code of the
    program cannot all be followed (_incomplete), or another call handed the array is only assumed to
    meet the rules, a value shown is assumed, not proved."""
    failed = [s for s in psites if s.kind == 'regs' and not s.fixed]
    if failed:
        f = failed[0]
        reason = 'the register array is also handed to a call that is not shown to give the same at every execution ' \
                 '(at instruction %s: %s), which could keep it and change any variable later' % (f.key, f.why)
    else:
        reason = _escape(flow, pir, at)
        if reason:
            reason += ': code that is not followed could change any variable'
    if reason:
        for s in psites:
            if s.fixed:
                s.fixed = s.assumed = False
                s.comment, s.why = None, reason
        return
    gap = _incomplete(pir)
    for s in psites:
        if not s.fixed or s.assumed:
            continue
        other = next((o for o in psites if o is not s and o.kind == 'regs' and o.assumed), None)
        if gap:
            s.assumed, s.comment, s.why = True, ASSUMED_CALLER, gap
        elif other is not None:
            s.assumed, s.comment = True, ASSUMED_OTHER
            s.why = 'another call handed the register array (at instruction %s) is only assumed to give the same at ' \
                    'every execution (%s)' % (other.key, other.why)


def _stmt_parts(st):
    """(expressions a statement reads, the table it stores into or None)"""
    k = st[0]
    if k == 'set':
        return [st[2]], None
    if k == 'setindex':
        return [st[2], st[3]], st[1]
    if k == 'call':
        return [st[2], list(st[3])] + ([st[4]] if st[4] else []), None
    return [x for x in st[1:] if isinstance(x, (tuple, list))], None


def _term_exprs(term):
    """the expressions a terminator reads (its arms are trees of their own)"""
    if term[0] == 'br':
        return [term[1]]
    if term[0] in ('goto', 'forbr'):
        return []
    return [x for x in term[1:] if isinstance(x, (tuple, list))]


def _term_reads(term):
    """the registers a terminator itself reads (not its arms)"""
    out = set()
    if term[0] == 'br':
        _expr_reads(term[1], out)
    elif term[0] == 'forbr':
        _loop_reads(term[2], out)
    elif term[0] != 'goto':
        _expr_reads(term[1:], out)
    return out


def _scan(e, acc):
    """what an expression reads that is not the same at every execution: acc['bad'] gets the reasons
    (a global that is not the standard library's, the environment, a value of Luraph's loader),
    acc['holes'] is set for a value the lift could not describe, acc['va'] for the varargs, acc['args']
    gets the argument numbers read, acc['t'] the temporaries read, acc['closures'] the functions made,
    acc['ident'] the library functions of _IDENTITY it uses"""
    if isinstance(e, list):
        for x in e:
            _scan(x, acc)
        return
    if not isinstance(e, tuple) or not e or not isinstance(e[0], str):
        return
    t = e[0]
    if t == 'k':
        return
    if t == 'global':
        if not _lib_name(e[1]):
            acc['bad'].add('it reads the global %s' % e[1])
        elif e[1] in _IDENTITY:
            acc['ident'].add(e[1])
        return
    if t == 'env':
        acc['bad'].add('it reads the environment')
        return
    if t == 'outer':
        acc['bad'].add('it reads a value of Luraph\'s loader')
        return
    if t == 'kfn':
        if not _lib_name(e[1]):
            acc['bad'].add('it calls %s' % e[1])
        elif e[1] in _IDENTITY:
            acc['ident'].add(e[1])
        return
    if t in ('opaque', 'forval'):
        acc['holes'] = True
        return
    if t in ('va', 'varargn'):
        acc['va'] = True
        return
    if t == 'arg':
        acc['args'].add(e[1])
        return
    if t == 't':
        acc['t'].add(e[1])
        return
    if t == 'index' and len(e) == 3 and isinstance(e[1], tuple) and e[1][:1] in (('global',), ('env',)):
        lib = e[1][1] if e[1][0] == 'global' else None
        k = e[2]
        if e[1][0] == 'env' and k[0] == 'k':
            if not _lib_name(k[1]):
                acc['bad'].add('it reads the global %s' % (k[1],))
            elif k[1] in _IDENTITY:
                acc['ident'].add(k[1])
            return
        if isinstance(lib, str) and lib in _LIB_TABLES:
            if k[0] == 'k':
                if not isinstance(k[1], str) or '%s.%s' % (lib, k[1]) in _NOT_LIB:
                    acc['bad'].add('it reads %s[%r]' % (lib, k[1]))
                elif k[1] not in _LIB_MEMBERS[lib]:
                    acc['bad'].add('it reads %s.%s, which the standard library does not have (a script can keep a '
                                   'value there)' % (lib, k[1]))
                elif '%s.%s' % (lib, k[1]) in _IDENTITY:
                    acc['ident'].add('%s.%s' % (lib, k[1]))
                return
            if any(n.startswith(lib + '.') for n in _NOT_LIB):
                acc['bad'].add('it reads a field of %s that is not constant' % lib)
                return
            _scan(k, acc)
            return
    if t == 'closure':
        acc['closures'].append(e)
        return
    if t in ('index', 'method') and len(e) == 3 and (e[2] == 'format' or e[2] == ('k', 'format')):
        acc['ident'].add('string.format')       # (`s:format(...)`, `s.format`)
    for x in e[1:]:
        _scan(x, acc)


def _indexes(x, acc):
    """collect the ('index', base, ('k', name)) expressions in x whose base is a register or a temporary"""
    if isinstance(x, list):
        for y in x:
            _indexes(y, acc)
    elif isinstance(x, tuple) and x and isinstance(x[0], str):
        if x[0] == 'index' and len(x) == 3 and isinstance(x[1], tuple) and x[1][:1] in (('r',), ('t',)) \
                and isinstance(x[2], tuple) and x[2][:1] == ('k',) and isinstance(x[2][1], str):
            acc.append(x)
        if x[0] != 'closure':
            for y in x[1:]:
                _indexes(y, acc)
    return acc


class _Snap:
    """a program's instructions as lifted, for Flow (show_run_values changes the trees as it goes)"""

    class Node:
        __slots__ = ('tree', 'error')

        def __init__(self, tree, error):
            self.tree, self.error = tree, error

    def __init__(self, trees):
        self.nodes = {k: _Snap.Node(t, e) for k, (t, e) in trees.items()}


class _Proof:
    """Whether what a call writes into its caller's registers (or returns) is the same at every
    execution of the call, from the code of the function called (rules 3 and 4 of the module's
    text; rules 1 and 2 are the call site's, see show_run_values).  check() gives 'proved',
    'assumed' (no rule fails, but the code of a function to check that ran has lift errors, parts
    that did not lift, or values the lift could not describe: kind 'lift'; or it uses a library
    function of _IDENTITY: kind 'lib') or None (a rule fails), the reason, and that kind."""
    DEPTH = 6

    def __init__(self, protos, oracle):
        self.oracle = oracle
        self.trees = {pid: {k: (n.tree, n.error) for k, n in pir.nodes.items()} for pid, pir in protos.items()}
        self._ran = None
        self._assigns = {}
        self._bodies = {}

    def ran(self, pid):
        """the instructions of program pid the script's run executed, or None when the run has no record
        of the program (then every instruction counts as one that may have run)"""
        if self._ran is None:
            self._ran = {}
            for k, rec in self.oracle.items():
                if isinstance(k, tuple) and len(k) == 3 and not (rec or {}).get('p'):
                    self._ran.setdefault(k[0], set()).add((k[1], k[2]))
        return self._ran.get(pid)

    def assigns_up(self, key, i, depth=0):
        """does function `key` (or a function it makes) assign to its upvalue i?  True, False, or None
        (not known: an instruction that did not lift)"""
        memo = (key, i)
        if memo in self._assigns:
            return self._assigns[memo]
        if key not in self.trees or depth > self.DEPTH:
            return None
        self._assigns[memo] = False         # (a function that makes itself)
        out = False
        for tree, err in self.trees[key].values():
            if tree is None:
                out = None if out is False else out
                continue
            if err:
                out = None if out is False else out
            for st in _all_stmts(tree):
                if st[0] == 'set' and st[1] == ('up', i) or st[0] == 'setindex' and st[1] == ('upcell', i):
                    out = True
                for cl in _closures(st, []):
                    for j, c in enumerate(cl[2]):
                        if c == ('up', i) or c == ('upcell', i):
                            a = self.assigns_up(cl[1], j, depth + 1)
                            out = True if a is True or out is True else None if a is None or out is None else False
        self._assigns[memo] = out
        return out

    def ref_assigned(self, pid):
        """{register: True / False / None}: for every register of program pid a function made there
        captures by reference, whether one of them assigns to that variable through its upvalue"""
        out = {}
        for tree, err in self.trees.get(pid, {}).values():
            if tree is None:
                continue
            for st in _all_stmts(tree):
                for cl in _closures(st, []):
                    for j, c in enumerate(cl[2]):
                        if isinstance(c, tuple) and len(c) == 2 and c[0] == 'ref' and isinstance(c[1], int):
                            a, b = self.assigns_up(cl[1], j), out.get(c[1], False)
                            out[c[1]] = True if a is True or b is True else None if a is None or b is None else False
        return out

    def assign_ran(self, key, i, depth=0):
        """did the run execute a statement of function `key` (or of a function it makes that shares the variable)
        that assigns to its upvalue i?  (an instruction the run has a record of; no record of the program: no)"""
        memo = ('ran', key, i)
        if memo in self._assigns:
            return self._assigns[memo]
        if key not in self.trees or depth > self.DEPTH:
            return False
        self._assigns[memo] = False         # (a function that makes itself)
        ran = self.ran(key) or set()
        out = False
        for k, (tree, _err) in self.trees[key].items():
            if tree is None:
                continue
            for st in _all_stmts(tree):
                if k in ran and (st[0] == 'set' and st[1] == ('up', i)
                                 or st[0] == 'setindex' and st[1] == ('upcell', i)):
                    out = True
                for cl in _closures(st, []):
                    for j, c in enumerate(cl[2]):
                        if (c == ('up', i) or c == ('upcell', i)) and self.assign_ran(cl[1], j, depth + 1):
                            out = True
        self._assigns[memo] = out
        return out

    def ref_assign_ran(self, pid):
        """{register: True}: the registers of program pid that a function made there captures by reference and
        assigns to through its upvalue in a statement the run executed (assign_ran)"""
        out = {}
        for tree, _err in self.trees.get(pid, {}).values():
            if tree is None:
                continue
            for st in _all_stmts(tree):
                for cl in _closures(st, []):
                    for j, c in enumerate(cl[2]):
                        if isinstance(c, tuple) and len(c) == 2 and c[0] == 'ref' and isinstance(c[1], int) \
                                and c[1] not in out and self.assign_ran(cl[1], j):
                            out[c[1]] = True
        return out

    def check(self, val, regs_pos=(), depth=0):
        """val: what Flow.fixed says the function called is; regs_pos: the positions of its arguments
        that are the caller's register array"""
        if val is None or val[0] != 'closure' or not isinstance(val[1], int):
            return None, 'the function called is not shown to be one function the program makes' if val is None or \
                val[0] != 'kfn' else 'the function called is a library function (%s), whose writes are not followed' % (
                    val[1],), None
        if depth > self.DEPTH:
            return None, 'the functions it captures are nested too deep to follow', None
        later = None        # (why the captures are only assumed, and of which kind)
        for c in (val[2] if len(val) > 2 else ()):
            if c is None:
                return None, 'a variable the function called shares (an upvalue) is not shown to hold the same value at ' \
                             'every execution', None
            if c[0] == 'closure':
                s, why, kind = self.check(c, (), depth + 1)
                if s is None:
                    return None, why, None
                if s == 'assumed' and later is None:
                    later = (why, kind)
            elif later is None and _idents(c, set()):
                later = ('a variable it shares holds %s' % ', '.join(sorted(_idents(c, set()))), 'lib')
        s, why, kind = self.body(val[1], tuple(sorted(regs_pos)), depth)
        if s is None:
            return None, why, None
        if s == 'incomplete':
            return 'assumed', why, kind
        if later is not None:
            return ('assumed',) + later
        return 'proved', None, None

    def body(self, key, regs_pos, depth):
        """('ok' | 'incomplete' | None, why, kind of what is incomplete) for the lifted code of function
        `key` and of the functions it makes: rule 4 of the module's text"""
        memo = (key, regs_pos)
        if memo in self._bodies:
            got = self._bodies[memo]
            return ('ok', None, None) if got is None else got        # (in progress: a function that makes itself)
        if not isinstance(key, int):
            return 'incomplete', 'a function it makes is one of Luraph\'s loader, whose code is not lifted', 'lift'
        if key not in self.trees:
            return 'incomplete', 'program %s was not lifted' % (key,), 'lift'
        if depth > self.DEPTH:
            return None, 'the functions it makes are nested too deep to follow', None
        self._bodies[memo] = None
        got = self._body(key, regs_pos, depth)
        self._bodies[memo] = got
        return got

    def _body(self, key, regs_pos, depth):
        nodes = self.trees[key]
        ran = self.ran(key)
        incomplete = []         # [(why, kind)]
        idents = set()
        # (instructions that ran -- all of them where the run has no record of the program -- that did not
        # lift, that lifted with an error, or with a value the lift could not describe)
        missing = {k for k in ran if k not in nodes or nodes[k][0] is None} if ran else set()
        errors, holes = set(), set()
        # the registers that hold the caller's register array it is handed (its parameters at those
        # positions), or that array among its varargs
        pregs, invar = set(), False
        if regs_pos:
            params = [st for t, _e in nodes.values() if t for st in t[0] if st[0] == 'params']
            copies = [st for t, _e in nodes.values() if t for st in t[0]
                      if st[0] == 'set' and st[1][0] == 'r' and st[2][0] == 'arg']
            for j in regs_pos:
                got = [st[1] + j for st in params if isinstance(st[1], int) and isinstance(st[2], int) and j < st[2]]
                got += [st[1][1] for st in copies if st[2][1] == j + 1]
                if got:
                    pregs.update(got)
                else:
                    invar = True
        flow = None
        bad, alias, uses = set(), set(), set()

        def stands_for(e, k, at):
            # what e (read at top-level statement `at` of instruction k) stands for, through the function's own
            # variables where the code shows it
            nonlocal flow
            if e[0] in ('r', 't'):
                if flow is None:
                    flow = Flow(_Snap(nodes))
                return flow.value(e, k, at)
            return e

        def name_of(e):
            if e is None:
                return None
            if e[0] in ('global', 'kfn'):
                return e[1]
            if e[0] == 'index' and len(e) == 3 and e[1] == ('env',) and isinstance(e[2], tuple) and e[2][0] == 'k':
                return e[2][1]
            return None
        for k, (tree, err) in nodes.items():
            here = ran is None or k in ran
            if tree is None:
                if here:
                    missing.add(k)
                continue
            if err and here:
                errors.add(k)
            trees = [tree]
            while trees:
                t = trees.pop()
                stmts, term = t
                if term[0] == 'br':
                    trees += [term[2], term[3]]
                elif term[0] == 'forbr':
                    trees += [term[3], term[4]]
                top = t is tree
                for idx, st in enumerate(stmts):
                    if st[0] == 'error':
                        if here:
                            errors.add(k)
                        continue
                    acc = {'bad': bad, 'holes': False, 'va': False, 'args': set(), 't': uses, 'closures': [],
                           'ident': set()}
                    exprs, target = _stmt_parts(st)
                    _scan(exprs, acc)
                    if target is not None:
                        if target[0] == 'upcell':
                            bad.add('it changes a variable it shares with other functions (its result can depend on '
                                    'earlier calls)')
                        elif target[0] == 't':
                            pass        # (counted below, with the temporaries that stand for the register array)
                        elif not (target[0] in ('r', 'global', 'outer') or target == ('env',)):
                            _scan(target, acc)
                    if st[0] == 'set' and st[1][0] == 'up':
                        bad.add('it changes a variable it shares with other functions (its result can depend on '
                                'earlier calls)')
                    if st[0] == 'vararg':
                        acc['va'] = True
                    # rawget / rawset on a table the function did not make itself: a library table has fields the
                    # library does not have only where a script keeps a value (`rawget(string, "n")`, also called
                    # through pcall / xpcall); getmetatable of a value it did not make reaches a library table past
                    # the check of its members (`getmetatable("").__index` is the string table)
                    at = idx if top else len(tree[0])
                    if st[0] == 'call':
                        fname, fargs = name_of(stands_for(st[2], k, at)), st[3]
                        if fname in ('pcall', 'xpcall') and fargs:
                            fname, fargs = name_of(stands_for(fargs[0], k, at)), fargs[1 if fname == 'pcall' else 2:]
                        if fname in _RAW or fname == 'getmetatable':
                            t0 = stands_for(fargs[0], k, at) if fargs else None
                            if t0 is None or t0[0] != 'newtable':
                                bad.add('it calls rawget or rawset on a table it did not make (on a library table that '
                                        'reads a field the library does not have, where a script can keep a value)'
                                        if fname in _RAW else 'it calls getmetatable on a value it did not make '
                                        '(through it a library table is reached past the check of its members)')
                    # (a field of a library table read through a variable that holds the table)
                    for ix in _indexes(exprs, []):
                        if flow is None:
                            flow = Flow(_Snap(nodes))
                        lib = flow.value(ix[1], k, idx if top else len(tree[0]))
                        if lib is not None and lib[0] == 'global' and lib[1] in _LIB_TABLES:
                            _scan(('index', lib, ix[2]), acc)
                    if acc['holes'] and here:
                        holes.add(k)
                    if here:
                        idents |= acc['ident']
                    if invar and acc['va']:
                        bad.add('it reads its varargs, which hold the register array it is handed')
                    if acc['args'] & {j + 1 for j in regs_pos} and not (st[0] == 'set' and st[1][0] == 'r'
                                                                         and st[2][0] == 'arg'):
                        bad.add('it reads the register array it is handed')
                    for cl in acc['closures']:
                        for j, c in enumerate(cl[2]):
                            if isinstance(c, tuple) and len(c) == 2 and c[0] in ('ref', 'val') and c[1] in pregs:
                                bad.add('a function it makes captures the register array it is handed')
                            elif isinstance(c, tuple) and len(c) == 2 and c[0] in ('up', 'upcell'):
                                a = self.assigns_up(cl[1], j)
                                if a is True:
                                    bad.add('a function it makes changes a variable it shares (its result can depend on '
                                            'earlier calls)')
                                elif a is None:
                                    incomplete.append(('a function it makes did not all lift', 'lift'))
                        s, why, kind = self.body(cl[1], (), depth + 1)
                        if s is None:
                            bad.add(why)
                        elif s == 'incomplete':
                            incomplete.append((why, kind))
                    if not pregs:
                        continue
                    hit = _stmt_reads(st) & pregs
                    if not hit:
                        continue
                    if st[0] == 'set' and st[1][0] == 't' and st[2][0] == 'r' and st[2][1] in pregs:
                        if flow is None:
                            flow = Flow(_Snap(nodes))
                        if self._maybe_param(flow, k, idx if top else len(tree[0]), st[2][1]):
                            alias.add(st[1][1])
                        continue
                    if st[0] == 'setindex' and st[1][0] == 'r' and st[1][1] in pregs:
                        other = set()
                        _expr_reads([st[2], st[3]], other)
                        hit = other & pregs
                        if not hit:
                            continue
                    if flow is None:
                        flow = Flow(_Snap(nodes))
                    if any(self._maybe_param(flow, k, idx if top else len(tree[0]), r) for r in hit):
                        bad.add('it reads the register array it is handed')
                acc = {'bad': bad, 'holes': False, 'va': False, 'args': set(), 't': uses, 'closures': [],
                       'ident': set()}
                _scan(_term_exprs(term), acc)
                if acc['holes'] and here:
                    holes.add(k)
                if here:
                    idents |= acc['ident']
                if invar and acc['va']:
                    bad.add('it reads its varargs, which hold the register array it is handed')
                if acc['args'] & {j + 1 for j in regs_pos}:
                    bad.add('it reads the register array it is handed')
                if pregs:
                    hit = _term_reads(term) & pregs
                    if hit:
                        if flow is None:
                            flow = Flow(_Snap(nodes))
                        if any(self._maybe_param(flow, k, len(tree[0]), r) for r in hit):
                            bad.add('it reads the register array it is handed')
        if alias & uses:
            bad.add('it reads the register array it is handed')
        if bad:
            return None, '; '.join(sorted(bad)), None
        if missing or errors or holes:
            what = [('%d did not lift' % len(missing)) if missing else '',
                    ('%d lifted with a lift error' % len(errors)) if errors else '',
                    ('%d with a value the lift could not describe' % len(holes - errors)) if holes - errors else '']
            incomplete.insert(0, ('the code of program %s that ran did not all lift: of its instructions%s, %s' % (
                key, ' that ran' if ran else ' (the run has no record of which ran)', ', '.join(x for x in what if x)),
                'lift'))
        if idents:
            incomplete.append(('program %s uses %s, whose result can differ between executions for the same arguments (the '
                               'address of a table or a function, the order of a table\'s keys)' % (
                                   key, ', '.join(sorted(idents))), 'lib'))
        # (what did not lift comes first: it is what the comment at a value says)
        incomplete.sort(key=lambda x: x[1] != 'lift')
        if incomplete:
            return ('incomplete',) + incomplete[0]
        return 'ok', None, None

    @staticmethod
    def _maybe_param(flow, k, idx, r):
        """can register r still hold the parameter it got at the function's entry at top-level statement
        idx of instruction k?  (every definition that can reach it is a statement of the function: no)"""
        return flow.reaching(r, k, idx) is None


def _closures_of(flow, e, key, idx, depth=0):
    """[(closure expression as the code makes it, its instruction, its statement index)]: the functions of the
    program the expression e (read by top-level statement idx of instruction key) can be, followed through
    temporaries and every definition of a register that can reach the statement; None where it can be anything
    else (a value read at run time, a library function, an upvalue: not resolved)"""
    if depth > 16 or not isinstance(e, tuple) or not e:
        return None
    if e[0] == 'closure':
        return [(e, key, idx)] if len(e) > 2 and isinstance(e[2], tuple) and isinstance(e[1], int) else None
    if e[0] == 't':
        d = flow.tdef(e[1], key, idx)
        return None if d is None or d[1] is None else _closures_of(flow, d[2], d[0], d[1], depth + 1)
    if e[0] == 'r':
        defs = flow.reaching(e[1], key, idx)
        if not defs:
            return None
        out = []
        for d in defs:
            got = _closures_of(flow, d[2], d[0], d[1], depth + 1)
            if got is None:
                return None
            out += got
        return out
    return None


def _assigns_shared(flow, proof, made, r, key, idx, depth=0):
    """does the function `made` (from _closures_of; the call is at top-level statement idx of instruction key)
    assign the variable it shares with register r through its upvalue -- itself or a function it makes
    (_Proof.assigns_up), or a function it shares (a closure the code shows in a variable it captures)?"""
    cl, mk, mi = made
    if depth > 4:
        return False
    for j, c in enumerate(cl[2]):
        if isinstance(c, tuple) and len(c) == 2 and c == ('ref', r) and proof.assigns_up(cl[1], j) is True:
            return True
    for c in cl[2]:
        if isinstance(c, tuple) and len(c) == 2 and c[0] in ('ref', 'val') and isinstance(c[1], int) and c[1] != r:
            # (a variable shared by reference holds at the call what the call sees; one copied, what it held when
            # the function was made)
            inner = _closures_of(flow, ('r', c[1]), key, idx) if c[0] == 'ref' else _closures_of(flow, ('r', c[1]), mk, mi)
            if inner and any(_assigns_shared(flow, proof, x, r, key, idx, depth + 1) for x in inner):
                return True
    return False


def _callee_closures(flow, st, key, idx):
    """the functions of the program a call statement st calls (read at top-level statement idx of instruction key;
    the number of its statements for one in an arm), as _closures_of: None where that is not resolved (a value read
    at run time, an upvalue); for pcall and xpcall, the function handed to them; [] for another library function
    (whose own code is no function of the program: never the one that assigns a variable the program shares)"""
    f = flow.value(flow._plain(st[2]), key, idx)
    if f is not None and f[0] == 'kfn':
        if f[1] in ('pcall', 'xpcall') and st[3]:
            return _closures_of(flow, flow._plain(st[3][0]), key, idx)
        return []
    return _closures_of(flow, flow._plain(st[2]), key, idx)


def _exempt(flow, proof, st, key, idx, r, named, ran):
    """A register a function of the program shares by reference changes through that function, whose text shows
    the change: after call statement st (at top-level statement idx of instruction key) it is not marked when the
    call does not name it (`named`: it writes a register it names through the register array), a function of the
    program assigns the shared variable (flow.ref_assigned), and the function called is that function itself (or
    makes one, or shares a closure that is one: _assigns_shared; through pcall / xpcall, the function handed to
    them), or is not resolved and a function that assigns the variable ran (`ran`: _Proof.ref_assign_ran; the
    function called may be that one).  Not for a library function called (its code is not the program's), nor for
    a resolved function that does not assign it (another function that assigns the variable is not the call's
    doing)."""
    if flow.ref_assigned.get(r) is not True or r in named:
        return False
    cls = _callee_closures(flow, st, key, idx)
    if cls is None:
        return bool(ran.get(r))
    return bool(cls) and all(_assigns_shared(flow, proof, cl, r, key, idx) for cl in cls)


def _has_regs(x):
    """does x (a statement, an expression, a tree) hold the ('regs',) atom anywhere"""
    if isinstance(x, tuple):
        return x == ('regs',) or any(_has_regs(y) for y in x)
    if isinstance(x, list):
        return any(_has_regs(y) for y in x)
    return False


def handed(protos, sites=()):
    """{(program, instruction, call id)}: the calls handed the register array of the code that calls them -- every
    site of kind 'regs' that show_run_values found (also one the run's record `a` alone showed handed it), and every
    call of a program that its code shows handed it, also where show_run_values does not look (an arm of a branch,
    an instruction that lifted with an error): an argument that stands for the array (Flow.value), or that holds it
    inside.  What such a call changes in its caller's variables is shown only as far as the run saw it, so a text
    with one is never complete (handed_note; run.report also counts the printed form, `({})`, in the text, which
    covers the array held in a variable wherever it goes)."""
    out = {(s.pid, s.key, s.call) for s in sites if s.kind == 'regs'}
    for pid, pir in protos.items():
        if not _has_regs([n.tree for n in pir.nodes.values() if n.tree is not None]):
            continue        # (the register array comes into a program's code only as ('regs',))
        flow = Flow(pir)
        for key, node in pir.nodes.items():
            if node.tree is None:
                continue
            stmts = node.tree[0]
            spots = [(i, st) for i, st in enumerate(stmts)] + [(len(stmts), st) for st in _nested(node.tree)]
            for i, st in spots:
                if st[0] == 'call' and any(_has_regs(flow._plain(a)) or flow.value(a, key, i) == ('regs',)
                                           for a in st[3]):
                    out.add((pid, key, st[1]))
    return out


def handed_note(protos, sites=()):
    """the run note on the calls handed their caller's register array (handed), or None: the result is PARTIAL
    (run.report), however many of the values they changed the text shows"""
    n = len(handed(protos, sites))
    if not n:
        return None
    return '[runvals-handed] %d call(s) are handed the variables of the code that calls them (its register array, ' \
           'printed `({})` or kept in a variable): what they change is shown only as far as the run saw it -- after ' \
           'them the text shows the value the run had, with a comment that says so, or `nil --[[%s]]`, for a ' \
           'variable the call names or the run saw change; this marking is best effort (it can miss a variable ' \
           'handed through a temporary in an arm of a branch, a change that a later statement of the same ' \
           'instruction also writes, and a variable that a function of the program shares and assigns), and a ' \
           'variable the call does not name and the run did not see change keeps its value from before the call, ' \
           'so any of them can show a value from before the call' % (n, MARK)


def show_run_values(protos, oracle, helpers=()):
    """Put the values the run proves where the lifted code would show something else (see the
    module's text): after every call handed its caller's register array, and in place of the
    results of a call of a helper of the build (`helpers`: program numbers) that go to registers
    (`R[n] = helper(...)` becomes `helper(...); R[n] = V`, for every result the run proves).
    Where the run proves no value for a register such a call may have set, `R[r] = nil --[[...]]`
    (MARK) goes in after the call instead, so that the text does not show the earlier value as the
    current one.  Every value put in carries a comment: PROVED, or the ASSUMED comment that says what
    could not be checked.  Changes the instruction trees of `protos` in place and returns the call
    sites found, [Site]; whatever they show, a call handed the register array leaves the result
    PARTIAL (handed_note)."""
    helpers = set(helpers or ())
    sites = []
    proof = _Proof(protos, oracle)
    for pid, pir in protos.items():
        if not isinstance(pid, int):
            continue
        flow = None
        todo = []       # (instruction, kind, the indexes of its calls found, its oracle record)
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
                found = [ci]
                kind = 'result'
            todo.append((key, kind, found, rec))
        if not todo:
            # (no site: the other calls handed the register array, and the calls the run saw change a variable that no
            # statement shows changing)
            sites += _other_calls(pid, pir, oracle, flow, proof, set())
            continue
        # (what the sites of the program need, once per program, from the code as lifted: the trees
        # change below)
        flow.ref_assigned = proof.ref_assigned(pid)
        ran = proof.ref_assign_ran(pid)
        # (Flow.spare: what the run saw a call that meets the rules leave alone)
        _trusted(flow, pir, pid, oracle, proof)
        entries, psites = [], []
        for key, kind, found, rec in todo:
            stmts = pir.nodes[key].tree[0]
            here = []
            for ci in found:
                st = stmts[ci]
                f = flow.value(st[2], key, ci)
                site = Site(pid, key, f[1] if f is not None and f[0] == 'closure' and isinstance(f[1], int) else None, kind,
                            st[1])
                psites.append(site)
                # What the call is handed decides what it writes: a closure the program makes, handed
                # constants besides the register array, whose captures and code read nothing that can
                # differ at another execution (_Proof).  A value read at run time (a global, an argument,
                # a result) can differ at another one, even where it was the same at every visit of this run.
                status, site.why, akind, fn, regs_args = _call_status(flow, proof, stmts, key, ci, rec, kind)
                if site.callee is None and fn is not None and fn[0] == 'closure' and isinstance(fn[1], int):
                    site.callee = fn[1]
                site.fixed = status is not None
                site.assumed = status == 'assumed'
                site.comment = _COMMENT.get(akind, ASSUMED) if site.assumed else None
                here.append((site, ci, _named(flow, st, key, ci, regs_args)))
            entries.append((key, kind, rec, here))
        _settle(flow, pir, psites, {(key, ci) for key, kind, _f, _r in todo if kind == 'regs' for ci in _f})
        sites += psites
        caps, live = captured(pir), Live(pir)
        for key, kind, rec, here in entries:
            node = pir.nodes[key]
            stmts, term = node.tree
            # the registers the run saw the calls handed the register array in this instruction change (at some
            # visit), where it saw every such call enter a VM function (h == n), whether or not that record covers
            # everything the calls did: only ever a register the call may have set, never a reason to leave one out
            # (a call that is no VM function changes registers the harness does not see change)
            seenw = (_seen_writes(rec) or set()) if kind == 'regs' else set()

            def first_touch(r, ci):
                # what the instruction's own statements after the call do to r first: 'read' (also one
                # whose writes are not known), 'write', or None
                for x in stmts[ci + 1:]:
                    if r in _stmt_reads(x):
                        return 'read'
                    w = _writes(x)
                    if w is None:
                        return 'read'
                    if _hits(w, r):
                        return 'write'
                return None

            def place(r, ci, reads):
                # where r stands after call ci: 'dead' (nothing reads it after the call before writing it
                # again), 'rewritten' (the instruction sets it again before reading it), 'captured' (a
                # function of the program shares it), 'reread' (the instruction reads it after the call:
                # the next instruction's record holds what it made of it), or 'free'
                if not isinstance(r, int) or (reads is not None and r not in reads):
                    return 'dead'
                first = first_touch(r, ci)
                if first == 'write':
                    return 'rewritten'
                if r in caps:
                    return 'captured'
                return 'reread' if first == 'read' else 'free'

            def exempt(r, ci, named):
                # (a shared register whose change the text shows through the function that assigns it: _exempt)
                return _exempt(flow, proof, stmts[ci], key, ci, r, named, ran)

            def markable(r, ci, reads, named):
                # (a register the call may have set, after a call whose effect the run does not show)
                p = place(r, ci, reads)
                return p in ('free', 'reread') or p == 'captured' and not exempt(r, ci, named)

            def insert(stmts, at):
                # at: {index of a call: statements to put right after it}
                out = []
                for i, x in enumerate(stmts):
                    out.append(x)
                    out += at.get(i, [])
                return out

            if kind == 'regs' and len(here) > 1:
                # several calls handed the register array in one instruction: the next instruction's record
                # does not say which of them wrote what; after each, the registers it names are not shown
                at = {}
                for site, ci, named in here:
                    reads = live.after(key, ci)
                    # (also every register the run saw one of them change: which one did is not known)
                    site.marked = {r for r in named | seenw if markable(r, ci, reads, named)}
                    if site.marked:
                        at[ci] = [('set', ('r', r), ('opaque', MARK)) for r in sorted(site.marked)]
                if at:
                    node.tree = (insert(stmts, at), term)
                continue
            site, ci, named = here[0]
            st = stmts[ci]
            known = all(_writes(x) is not None for x in stmts[ci + 1:])
            reads = live.after(key, ci)
            rec2 = _after(rec, oracle, pid, term)
            site.ran = rec2 is not None and known
            if not site.ran:
                if kind == 'regs':
                    # (what the call may have set shows as not shown, not as the value from before it; also before
                    # statements whose writes are not known: one that writes the register again writes over MARK)
                    site.marked = {r for r in named | seenw if markable(r, ci, reads, named)}
                    if site.marked:
                        node.tree = (insert(stmts, {ci: [('set', ('r', r), ('opaque', MARK))
                                                         for r in sorted(site.marked)]}), term)
                continue
            before, seen = _values(rec), _values(rec2)

            def shown(v):
                # (never bare: the comment says whether the value is proved or assumed, see PROVED)
                return ('runval', _const(v)[1], (site.comment or ASSUMED) if site.assumed else PROVED)
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
                    new[j] = ('set', x[1], shown(seen[r]))
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
            for r in sorted(set(before) | set(seen) | named | seenw, key=lambda x: (not isinstance(x, int), x)):
                p = place(r, ci, reads)
                if p in ('dead', 'rewritten'):
                    continue
                # (the call names it, or the run saw the call change it)
                may = r in named or r in seenw
                own = [x for x in stmts[:ci] if _hits(_writes(x), r)]
                if p == 'captured':
                    # (changed through the register array, a function that shares the variable shows the old
                    # value; changed by a function through its upvalue, that function's text shows it: exempt)
                    if not may:
                        # (what the register held before the call and after it says whether it changed; one value
                        # on one side only counts as a change)
                        had, old = r in before, before.get(r)
                        if own:
                            last = own[-1]
                            had = last[0] == 'set' and last[1] == ('r', r) and last[2][0] == 'k'
                            old = last[2][1] if had else None
                        may = had != (r in seen) or had and not _same(old, seen[r])
                    if may and not exempt(r, ci, named):
                        site.unproved.add(r)
                    continue
                if p == 'reread':
                    # (the next instruction saw what the instruction made of it after the call, not what the
                    # call left: what the call left is not shown)
                    if may or own or r in before:
                        site.unproved.add(r)
                    continue
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
                        put.append(('set', ('r', r), shown(v)))
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
                node.tree = (insert(stmts, {ci: put}), term)
        sites += _other_calls(pid, pir, oracle, flow, proof, {(s.key, s.call) for s in psites if s.kind == 'regs'})
    return sites


def _call_spots(tree, path=(), acc=None):
    """[(path, index, statement)]: the call statements of a tree, those of its branch arms included; path: the
    positions in the terminators that lead to the arm (() for the top level)"""
    acc = [] if acc is None else acc
    for i, st in enumerate(tree[0]):
        if st[0] == 'call':
            acc.append((path, i, st))
    term = tree[1]
    for j in ((2, 3) if term[0] == 'br' else (3, 4) if term[0] == 'forbr' else ()):
        _call_spots(term[j], path + (j,), acc)
    return acc


def _arm(tree, path):
    for j in path:
        tree = tree[1][j]
    return tree


def _put_after(tree, path, at):
    """the tree with the statements at[i] put right after statement i of the statement list at `path`"""
    if not path:
        out = []
        for i, x in enumerate(tree[0]):
            out.append(x)
            out += at.get(i, [])
        return out, tree[1]
    term = list(tree[1])
    term[path[0]] = _put_after(term[path[0]], path[1:], at)
    return tree[0], tuple(term)


def _first_touch(stmts, r):
    """what the statements do to register r first: 'read' (also one whose writes are not known), 'write', or None"""
    for x in stmts:
        if r in _stmt_reads(x):
            return 'read'
        w = _writes(x)
        if w is None:
            return 'read'
        if _hits(w, r):
            return 'write'
    return None


def _other_calls(pid, pir, oracle, flow, proof, done):
    """MARK after the calls of program pid that are not sites (`done`: (instruction, call id) of the sites handed the
    register array, which show_run_values has dealt with), in every instruction that lifted:

      * a call the code shows handed the register array (an argument that stands for it or holds it inside, as
        handed), also one in an arm of a branch: a register it names, or that the run saw the instruction's calls
        change (`w`, where every visit saw them enter a function of the VM), and that the code reads afterwards gets
        `R[r] = nil --[[MARK]]` right after the call (in its arm), unless the instruction's own statements write it
        again first, or a function of the program shares it and its change is that function's (_exempt);
      * in an instruction with no call handed the register array, a register the run saw its calls change (`w`, as
        above) that no statement of the instruction writes after its first call, and that no function of the
        program that shares it and assigns it through its upvalue in a statement the run executed can have changed:
        nothing in the text changes it (the change went through a register array the code does not show handed:
        kept in a table, a value the lift could not describe), and it gets the MARK after each of the instruction's
        calls where the code reads it afterwards.

    Returns a Site for every call given a MARK (kind 'regs' for a call handed the register array, 'seen' for one the
    run alone shows changing a variable): notes() counts them as calls whose effect the run does not show."""
    out = []
    caps = live = ran = None
    for key, node in list(pir.nodes.items()):
        if node.tree is None or node.error:
            continue
        spots = _call_spots(node.tree)
        if not spots:
            continue
        rec = oracle.get((pid, key[0], key[1]))
        seenw = _seen_writes(rec) or set()
        if flow is None:
            flow = Flow(pir)
        top = node.tree[0]
        handed_here = any((key, st[1]) in done for _p, _i, st in spots)
        work = []
        for path, i, st in spots:
            if (key, st[1]) in done:
                continue
            at = i if not path else len(top)
            pos = [j for j, a in enumerate(st[3]) if _has_regs(flow._plain(a)) or flow.value(a, key, at) == ('regs',)
                   or not path and flow.is_regs_arg(a, key, i, rec, top)]
            handed_here = handed_here or bool(pos)
            if pos or seenw:
                work.append((path, i, st, at, pos))
        if not work:
            continue
        # (record only: the registers a statement of the instruction writes after its first call -- what the run saw
        # change may be their doing -- or None where a statement's writes are not known)
        later = set()
        if not handed_here:
            first_path, first_i, _st = spots[0]
            arm0 = _arm(node.tree, first_path)
            for x in list(arm0[0][first_i + 1:]) + _nested(arm0):
                w = _writes_any(x)
                if w is None:
                    later = None
                    break
                later |= {r for r in seenw if _hits(w, r)}
            if later is None or not seenw - later:
                continue
        if caps is None:
            caps, live = captured(pir), Live(pir)
            if not flow.ref_assigned:
                flow.ref_assigned = proof.ref_assigned(pid)
            ran = proof.ref_assign_ran(pid)
        lout = live._out(key)
        edits = {}
        for path, i, st, at, pos in work:
            if not pos and handed_here:
                continue        # (what the run saw change is the doing of the calls handed the register array)
            named = set()
            if pos:
                for a in st[3][pos[0] + 1:]:
                    e = flow._plain(a)
                    v = e if e[0] == 'k' else None if path else flow.fixed(a, key, at, loose=True)
                    if v is not None and v[0] == 'k' and P.is_num(v[1]) and not isinstance(v[1], bool) \
                            and float(v[1]).is_integer() and 0 <= v[1] < 1 << 20:
                        named.add(int(v[1]))
            arm = _arm(node.tree, path)
            rest = arm[0][i + 1:]
            after = None
            marked = set()
            for r in sorted(named | seenw):
                if not pos and r in later:
                    continue
                touch = _first_touch(rest, r)
                if touch == 'write':
                    continue
                if touch is None and lout is not None and r not in lout:
                    if after is None:
                        after = _tree_reads(([], arm[1]), set())
                    if r not in after:
                        continue        # (nothing reads it afterwards)
                if r in caps:
                    if pos and _exempt(flow, proof, st, key, at, r, named, ran):
                        continue
                    if not pos and flow.ref_assigned.get(r) is True and \
                            (ran.get(r) or _exempt(flow, proof, st, key, at, r, named, ran)):
                        continue
                marked.add(r)
            if not marked:
                continue
            edits.setdefault(path, {})[i] = [('set', ('r', r), ('opaque', MARK)) for r in sorted(marked)]
            f = flow.value(flow._plain(st[2]), key, at)
            site = Site(pid, key, f[1] if f is not None and f[0] == 'closure' and isinstance(f[1], int) else None,
                        'regs' if pos else 'seen', st[1])
            site.marked = marked
            if not pos:
                site.why = 'the call is not shown handed the register array, and the run saw it change a variable no ' \
                           'statement shows changing'
            elif path:
                site.why = 'the call is in an arm of a branch, where the run values do not follow a call'
            else:
                site.why = 'the call is handed the register array inside another value, which the run values do not ' \
                           'follow'
            out.append(site)
        tree = node.tree
        for path, at_ in edits.items():
            tree = _put_after(tree, path, at_)
        node.tree = tree
    return out


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
    shown (each with its comment; how many are proved and how many assumed), `[runvals-missing] ...`
    where the run proved no value for something a
    call wrote or may have written (a register it names or the run saw it change, printed as MARK;
    or a call that did not run): the result is PARTIAL, see run.report; `[runvals-assumed] ...` where
    values are shown whose proof could not be completed (printed with the comment ASSUMED): also
    PARTIAL.  `callers`: {program: the program that first called it}, for the words on a helper of
    the build."""
    helpers = set(helpers or ())
    groups = {}
    for s in sites:
        groups.setdefault((s.callee, s.kind), []).append(s)
    out = []
    for (callee, kind), group in sorted(groups.items(), key=lambda kv: (kv[0][0] is None, kv[0][0] or 0, kv[0][1])):
        shown = [s for s in group if s.shown]
        # (one register the run does not prove is enough: the site is not complete, whatever else it shows)
        missing = [s for s in group if not s.ran or s.unproved]
        assumed = [s for s in shown if s.assumed]
        if not shown and not missing:
            continue
        who = _who(callee, helpers, callers)
        if kind == 'regs':
            what = 'are handed the register array of the code that calls them (printed `({})`) and write into its variables'
        elif kind == 'seen':
            what = 'change variables of the code that calls them, as the run saw, that no statement of the text ' \
                   'changes (they are not shown handed its register array)'
        else:
            what = 'return values that the code that calls them keeps in variables'
        if shown:
            out.append('[runvals] %d call(s) of %s %s: after %s the text shows the values they %s as the run saw them '
                       '(%d value(s), each with a comment: %d proved, --[[%s]], %d assumed)' % (
                           len(group), who, what, 'each of them' if len(shown) == len(group) else
                           '%d of them' % len(shown), 'wrote' if kind != 'result' else 'returned',
                           sum(len(s.shown) for s in shown), sum(len(s.shown) for s in shown if not s.assumed), PROVED,
                           sum(len(s.shown) for s in shown if s.assumed)))
        if assumed:
            whys = sorted({s.why for s in assumed if s.why})
            comments = sorted({s.comment or ASSUMED for s in assumed})
            out.append('[runvals-assumed] %d call(s) of %s %s: the %d value(s) the text shows after %s are the run\'s, '
                       'assumed, not proved, to depend only on what the call is handed (%s, so what it reads could not all '
                       'be checked); each of them carries %s' % (
                           len(group), who, what, sum(len(s.shown) for s in assumed),
                           'each of them' if len(assumed) == len(group) else '%d of them' % len(assumed),
                           '; '.join(whys[:2]) or 'the code of the function called that ran did not all lift',
                           'the comment --[[%s]]' % comments[0] if len(comments) == 1 else
                           'one of the comments %s' % ', '.join('--[[%s]]' % c for c in comments)))
        if missing:
            there = ('after those calls a variable the call may have set is printed `nil --[[%s]]` (where the call names '
                     'it or the run saw the call change it), and a constant handed to the call stays as it was (encrypted)'
                     % MARK if kind != 'result' else
                     'there the text keeps the call, and the constants handed to it stay as they were (encrypted)')
            whys = sorted({s.why for s in missing if s.why and not s.fixed})
            out.append('[runvals-missing] %d call(s) of %s %s, and for %s the run does not show one value of what they %s '
                       '(the call did not run, it went on to more than one instruction or shares it with another such call, '
                       'what followed it held different values at different times, or what it is handed is not constant or '
                       'not shown to give the same at every execution%s): %s' % (
                           len(group), who, what, 'all of them' if len(missing) == len(group) else
                           '%d of them' % len(missing), 'wrote' if kind != 'result' else 'returned',
                           ('; here: ' + '; '.join(whys[:2])) if whys else '', there))
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
