"""Reflow a minified Luraph script so that the rest of the pipeline can address functions by line.

Raw Luraph output is one enormous line; the model, the harness (debug.info line numbers) and the
lifter all identify closures by the line they start on.  Only whitespace between tokens is touched:
a line break after every `;` and in front of every `function` keyword.
"""
from . import luparse


def needs_reflow(src):
    return src.count('function') > 4 * (src.count('\n') + 1)


def reflow(src):
    toks = luparse.tokenize(src)
    out = []
    last = 0
    prev = None
    for kind, val, _line, pos in toks:
        if kind == 'eof':
            break
        if prev is not None:
            brk = (prev == ('op', ';')) or (kind == 'kw' and val == 'function')
            if brk:
                out.append(src[last:pos].rstrip(' \t'))
                if not out[-1].endswith('\n'):
                    out.append('\n')
                last = pos
        prev = (kind, val) if kind in ('op', 'kw') else (kind, None)
    out.append(src[last:])
    return ''.join(out)


def unflow(src):
    """Inverse of reflow for the copy that is executed: every line break between tokens becomes a
    space again.  Scripts check their own line numbers (error messages, debug.info), so the code
    has to run laid out exactly as it was shipped."""
    toks = luparse.tokenize(src)
    out = []
    last = 0
    prev_end = None
    for i, (kind, val, _line, pos) in enumerate(toks):
        if prev_end is not None and '\n' in src[prev_end:pos]:
            out.append(src[last:prev_end])
            out.append(' ')
            last = pos
        if kind == 'eof':
            break
        prev_end = _token_end(src, pos)
    out.append(src[last:])
    return ''.join(out)


def _token_end(src, pos):
    return luparse.TOKEN_RE.match(src, pos).end()


def inline_protected(src):
    """Payload interpreters run their dispatch loops inside pcall:

        local ok, a, b, c, d = pcall(function(...) <loops; `return a, b, c, d`> end, ...)
        if ok then <act on a, b, c, d: tail call / return values / return> end
        <re-raise the error with the script's line information>

    The lifter wants the loops in the interpreter function itself.  This rewrites the (indented,
    decompiler-style) source so that they are: the closure body is inlined, and each of its
    `return` statements becomes the success branch with the returned values bound.  Only the
    error path is lost (errors are no longer re-raised with line information).
    Returns (new source, number of interpreters rewritten)."""
    import re
    lines = src.split('\n')
    head = re.compile(r'^(\s*)local (\w+), (\w+), (\w+), (\w+), (\w+) = (\w+)\(function\(\.\.\.\)(: \([^)]*\))?$')
    count = 0
    i = 0
    while i < len(lines):
        m = head.match(lines[i])
        if not m:
            i += 1
            continue
        ind = m.group(1)
        names = m.group(3, 4, 5, 6)
        ok = m.group(2)
        # end of the closure
        e = i + 1
        while e < len(lines) and lines[e] != ind + 'end, ...)':
            e += 1
        if e >= len(lines):
            i += 1
            continue
        # success branch: `if ok then ... end` right after
        s = e + 1
        while s < len(lines) and not lines[s].strip():
            s += 1
        if s >= len(lines) or lines[s] != '%sif %s then' % (ind, ok):
            i = e + 1
            continue
        t = s + 1
        while t < len(lines) and lines[t] != ind + 'end':
            t += 1
        post = lines[s + 1:t]
        # the rest of the interpreter function (error path)
        f = t + 1
        fend = ind[:-4] + 'end'
        while f < len(lines) and lines[f] != fend:
            f += 1
        body = lines[i + 1:e]
        out = []
        nested = []          # indents of function literals open inside the body
        for ln in body:
            stripped = ln.strip()
            cur = len(ln) - len(ln.lstrip(' '))
            while nested and stripped.startswith('end') and cur == nested[-1]:
                nested.pop()
                break
            if re.search(r'\bfunction\b\s*[\w.:]*\(', ln) and not re.search(r'\bend\b[\s,)]*$', ln):
                nested.append(cur)
                out.append(ln)
                continue
            r = re.match(r'^(\s*)return\b\s*(.*)$', ln)
            if r and not nested:
                rind, exprs = r.group(1), r.group(2)
                if not exprs:
                    out.append(rind + 'return')
                    continue
                out.append(rind + 'do')
                out.append('%s    local %s = %s' % (rind, ', '.join(names), exprs))
                shift = len(rind) + 4 - (len(ind) + 4)
                for p in post:
                    if not p.strip():
                        out.append(p)
                    elif shift >= 0:
                        out.append(' ' * shift + p)
                    else:
                        out.append(p[-shift:])
                out.append(rind + 'end')
                continue
            out.append(ln)
        # the body sat one level deeper than the statement that held it
        out = [ln[4:] if ln.startswith(ind + '    ') or not ln.strip() else ln for ln in out]
        lines[i:f] = out
        count += 1
        i += len(out)
    return '\n'.join(lines), count


def _expr_ifs(toks):
    """Token indices of the `if` keywords that start a Luau if-expression (`x = if a then b else c`:
    no `end`)."""
    n = len(toks)

    def is_(i, kind, val):
        return 0 <= i < n and toks[i][0] == kind and toks[i][1] == val

    # Luau if-expressions (`x = if a then b else c`) have no `end`
    exprif = set()
    st = []             # open ifs: True for an expression
    for q in range(n):
        k, v = toks[q][0], toks[q][1]
        if k != 'kw':
            continue
        if v == 'if':
            pk, pv = toks[q - 1][0], toks[q - 1][1] if q else (None, None)
            if pk == 'op' and pv not in (';', ')', ']', '}', '...'):
                expr = True
            elif pk == 'kw' and pv in ('return', 'and', 'or', 'not'):
                expr = True
            elif pk == 'kw' and pv in ('then', 'else') and st and st[-1] in (True, 'chain'):
                if st[-1] == 'chain':
                    st.pop()
                expr = True
            else:
                expr = False
            if expr:
                exprif.add(q)
            st.append(expr)
        elif v == 'elseif' and st and st[-1] is True:
            pass
        elif v == 'else' and st and st[-1] is True:
            # the else arm ends the expression unless it goes on with another if
            if not is_(q + 1, 'kw', 'if'):
                st.pop()
            else:
                st.pop()        # `else if ...` : the nested if is pushed as an expression next
                st.append('chain')
        elif v in ('function', 'do', 'repeat'):
            st.append(None)
        elif v in ('end', 'until') and st:
            st.pop()
    # (`else if` inside an expression: the marker makes the following `if` an expression too)
    return exprif


def inline_protected_raw(src):
    """inline_protected_raw_info without the list of dissolved closures."""
    out, count, _removed = inline_protected_raw_info(src)
    return out, count


def inline_protected_raw_info(src):
    """The same rewrite as inline_protected for a script in Luraph's own minified layout:

        local ok, a, b, c = pcall(function() local ...; repeat local op = code[pc]; if ... until false; end);
        if ok then <act on a, b, c> else <re-raise> end

    The dispatch loop becomes `while true do ... end` in the interpreter function itself and every
    `return x, y, z` of the protected closure becomes the success branch with those values bound.
    Works on tokens, so the layout of the rest of the file is untouched.
    Returns (new source, number of interpreters rewritten, the dissolved closures as positions in
    the source-order list of the text's `function` keywords: a model of the rewritten text numbers
    its functions like a model of the original text with those left out)."""
    toks = [t for t in luparse.tokenize(src) if t[0] != 'eof']
    n = len(toks)
    removed = []

    def is_(i, kind, val):
        return 0 <= i < n and toks[i][0] == kind and toks[i][1] == val

    exprif = _expr_ifs(toks)
    # (`else if` inside an expression: the marker makes the following `if` an expression too)

    def match_block(i):
        """Index of the token closing the block opened at toks[i]."""
        depth = 0
        while i < n:
            k, v = toks[i][0], toks[i][1]
            if k == 'kw' and i in exprif:
                i += 1
                continue
            if k == 'kw':
                if v in ('function', 'if', 'do', 'repeat'):
                    depth += 1
                elif v in ('end', 'until'):
                    depth -= 1
                    if depth == 0:
                        return i
            i += 1
        raise ValueError('unbalanced block')

    # innermost function literal around every token
    encl = [None] * n
    stack = []
    for q in range(n):
        k, v = toks[q][0], toks[q][1]
        encl[q] = next((x for x in reversed(stack) if x is not None), None)
        if k == 'kw' and q not in exprif:
            if v == 'function':
                stack.append(q)
            elif v in ('if', 'do', 'repeat'):
                stack.append(None)
            elif v in ('end', 'until') and stack:
                stack.pop()

    def loop_head(i):
        """repeat local OP[, X] = CODE[PC]   |   while true do local OP[, X] = CODE[PC]"""
        if is_(i, 'kw', 'repeat'):
            q = i + 1
        elif is_(i, 'kw', 'while') and is_(i + 1, 'kw', 'true') and is_(i + 2, 'kw', 'do'):
            q = i + 3
        else:
            return False
        if not (is_(q, 'kw', 'local') and toks[q + 1][0] == 'name'):
            return False
        q += 2
        if is_(q, 'op', ',') and toks[q + 1][0] == 'name':
            q += 2
        return is_(q, 'op', '=') and toks[q + 1][0] == 'name' and is_(q + 2, 'op', '[') \
            and toks[q + 3][0] == 'name' and is_(q + 4, 'op', ']')

    edits = []          # (start pos, end pos, replacement)
    count = 0
    done = set()
    i = 0
    while i < n:
        if not loop_head(i):
            i += 1
            continue
        rep = i
        i += 1
        if is_(rep, 'kw', 'repeat'):
            unt = match_block(rep)
            if not is_(unt + 1, 'kw', 'false'):
                continue
        else:
            unt = None
        # enclosing closure: ... = F(function([...]) <locals>; <loop>
        f = encl[rep]
        if f is None or f < 1 or not is_(f - 1, 'op', '(') or not is_(f + 1, 'op', '('):
            continue
        body0 = f + 3 if is_(f + 2, 'op', ')') else f + 4 if (is_(f + 2, 'op', '...') and is_(f + 3, 'op', ')')) else None
        if body0 is None:
            continue
        fend = match_block(f)
        if is_(fend + 1, 'op', ')'):
            after = fend + 2
        elif is_(fend + 1, 'op', ',') and is_(fend + 2, 'op', '...') and is_(fend + 3, 'op', ')'):
            after = fend + 4
        else:
            continue
        if unt is not None:
            edits.append((toks[rep][3], toks[rep][3] + len('repeat'), 'while true do'))
            edits.append((toks[unt][3], toks[unt + 1][3] + len('false'), 'end'))
        if f in done:
            continue
        # statement start: local OK, A, B, C = <callee>(
        s = f - 1
        while s >= 0 and not is_(s, 'kw', 'local'):
            s -= 1
        names = []
        k = s + 1
        while toks[k][0] == 'name':
            names.append(toks[k][1])
            k += 1
            if is_(k, 'op', ','):
                k += 1
        if len(names) < 2 or not is_(k, 'op', '='):
            continue
        ok, vals = names[0], names[1:]
        # if OK then <success> else <failure> end
        j = after
        if is_(j, 'op', ';'):
            j += 1
        # (also `if not OK then <failure> else <success> end`, and OK in parentheses)
        if not is_(j, 'kw', 'if'):
            continue
        q = j + 1
        neg = is_(q, 'kw', 'not')
        if neg:
            q += 1
        par = 0
        while is_(q, 'op', '('):
            par += 1
            q += 1
        if not (q < n and toks[q][0] == 'name' and toks[q][1] == ok and all(is_(q + 1 + x, 'op', ')') for x in range(par))
                and is_(q + 1 + par, 'kw', 'then')):
            continue
        then = q + 1 + par
        done.add(f)
        ifend = match_block(j)
        depth = 0
        els = None
        for q in range(j, ifend):
            kk, vv = toks[q][0], toks[q][1]
            if kk == 'kw' and q in exprif:
                continue
            if kk == 'kw':
                if vv in ('function', 'if', 'do', 'repeat'):
                    depth += 1
                elif vv in ('end', 'until'):
                    depth -= 1
                elif vv in ('else', 'elseif') and depth == 1:
                    els = q
                    break
        if neg:
            if els is None:
                done.discard(f)
                continue
            if toks[els][1] == 'else':
                success = src[toks[els + 1][3]:toks[ifend][3]].rstrip().rstrip(';')
            else:
                success = 'if ' + src[toks[els + 1][3]:toks[ifend][3]] + ' end'
        else:
            succ_end = toks[els][3] if els is not None else toks[ifend][3]
            success = src[toks[then + 1][3]:succ_end].rstrip().rstrip(';')
        # rewrite the closure body
        edits.append((toks[s][3], toks[body0][3], 'local %s;' % ','.join(vals)))
        depth = 0
        q = body0
        while q < fend:
            kk, vv = toks[q][0], toks[q][1]
            if kk == 'kw' and vv == 'function':
                q = match_block(q) + 1      # nested closures keep their returns
                continue
            if kk == 'kw' and vv == 'return':
                e = q + 1
                br = 0
                while e < fend:
                    k2, v2 = toks[e][0], toks[e][1]
                    if k2 == 'kw' and v2 == 'function':
                        e = match_block(e) + 1
                        continue
                    if k2 == 'op' and v2 in '([{':
                        br += 1
                    elif k2 == 'op' and v2 in ')]}':
                        br -= 1
                    elif br == 0 and ((k2 == 'op' and v2 == ';') or (k2 == 'kw' and v2 in ('end', 'else', 'elseif', 'until'))):
                        break
                    e += 1
                exprs = src[toks[q + 1][3]:toks[e][3]].strip() if e > q + 1 else ''
                stop = toks[e][3] + (1 if is_(e, 'op', ';') else 0)
                if exprs:
                    edits.append((toks[q][3], stop, 'do %s=%s;%s;return;end;' % (','.join(vals), exprs, success)))
                else:
                    edits.append((toks[q][3], stop, 'do return;end;'))
                q = e
                continue
            q += 1
        # drop `end);if OK then ... end`
        tail = toks[ifend][3] + len('end')
        if is_(ifend + 1, 'op', ';'):
            tail = toks[ifend + 1][3] + 1
        edits.append((toks[fend][3], tail, ''))
        count += 1
        removed.append(f)
    if not count:
        return src, 0, []
    fkw = [q for q in range(n) if toks[q][0] == 'kw' and toks[q][1] == 'function']
    removed = sorted(fkw.index(f) for f in removed)
    edits.sort()
    out = []
    last = 0
    for a, b, text in edits:
        if a < last:
            raise ValueError('overlapping edits')
        out.append(src[last:a])
        out.append(text)
        last = b
    out.append(src[last:])
    return ''.join(out), count, removed


# ---------------------------------------------------------------------------------------------
# Dispatch-loop heads.
#
# Every Luraph file spells the head of its interpreter loops a little differently: the fetch in
# parentheses, `repeat ... until false` for `while true do ... end`, a truthy literal for `true`,
# junk constant locals declared with the opcode or right after it.  The spellings are accepted
# HERE and nowhere else: match_head() is the one predicate, normalise_heads() rewrites what it
# accepts to the two canonical forms the model (vmmodel.is_dispatch_loop / LOOP_HEAD_RE), the
# partial evaluator and inline_protected_raw know:
#
#     while true do local OP=CODE[PC];if ...                                   parallel code arrays
#     while true do local I=CODE[PC];local OP=I[n];PC=PC+1;if ...              one table per instruction
#
# Junk constants move in front of the loop (they are never assigned; opaque tests in the dispatch
# tree compare with them).

class Head:
    """One dispatch-loop head found in a token list."""
    __slots__ = ('start', 'if_tok', 'until', 'last', 'op', 'code', 'pc', 'insn', 'field', 'junk', 'plain', 'tail')


def _head_fetch(toks, q):
    """toks[q] is the first token inside the loop.  Returns a Head (start/until/last unset) when
    the loop begins with an instruction fetch followed by the dispatch `if`, else None."""
    n = len(toks)

    def tv(j):
        return toks[j][1] if j < n else None

    def tk(j):
        return toks[j][0] if j < n else None

    def number(j):
        """(text of an optionally parenthesised, optionally negative number at j, next index)"""
        par = 0
        while tv(j) == '(' and tk(j) == 'op':
            par += 1
            j += 1
        neg = ''
        if tk(j) == 'op' and tv(j) == '-':
            neg = '-'
            j += 1
        if tk(j) != 'num':
            return None, j
        text = neg + repr(toks[j][1])
        j += 1
        for _ in range(par):
            if not (tk(j) == 'op' and tv(j) == ')'):
                return None, j
            j += 1
        return text, j

    plain = True
    j = q
    if not (tk(j) == 'kw' and tv(j) == 'local') or tk(j + 1) != 'name':
        return None
    names = [tv(j + 1)]
    j += 2
    while tk(j) == 'op' and tv(j) == ',' and tk(j + 1) == 'name':
        names.append(tv(j + 1))
        j += 2
    if not (tk(j) == 'op' and tv(j) == '='):
        return None
    j += 1
    par = 0
    while tk(j) == 'op' and tv(j) == '(':
        par += 1
        j += 1
    if not (tk(j) == 'name' and tk(j + 1) == 'op' and tv(j + 1) == '[' and tk(j + 2) == 'name'
            and tk(j + 3) == 'op' and tv(j + 3) == ']'):
        return None
    code, pc = tv(j), tv(j + 2)
    j += 4
    for _ in range(par):
        if not (tk(j) == 'op' and tv(j) == ')'):
            return None
        j += 1
    if par:
        plain = False
    junk = []
    consts = []
    while tk(j) == 'op' and tv(j) == ',':
        text, k = number(j + 1)
        if text is None:
            return None
        consts.append(text)
        j = k
    if consts:
        if len(consts) != len(names) - 1:
            return None
        junk.append('local %s=%s;' % (','.join(names[1:]), ','.join(consts)))
        names = names[:1]
        plain = False
    if tk(j) == 'op' and tv(j) == ';':
        j += 1
    h = Head()
    h.op, h.code, h.pc, h.insn, h.field = names[0], code, pc, None, None
    h.start = h.until = h.last = None
    h.tail = False
    h.plain = plain
    # the names declared with the opcode and given no value (`local OP, X = CODE[PC]`) stay
    h.junk = ''
    extra = names[1:]
    # T form: local OP = I[n]; PC = PC + 1   (also `PC += 1`)
    if tk(j) == 'kw' and tv(j) == 'local' and tk(j + 1) == 'name' and tk(j + 2) == 'op' and tv(j + 2) == '=' and not extra:
        k = j + 3
        par = 0
        while tk(k) == 'op' and tv(k) == '(':
            par += 1
            k += 1
        if tk(k) == 'name' and tv(k) == names[0] and tk(k + 1) == 'op' and tv(k + 1) == '[' and tk(k + 2) == 'num' \
                and tk(k + 3) == 'op' and tv(k + 3) == ']':
            field = toks[k + 2][1]
            opname = tv(j + 1)
            k += 4
            ok = True
            for _ in range(par):
                if not (tk(k) == 'op' and tv(k) == ')'):
                    ok = False
                k += 1
            if ok and tk(k) == 'op' and tv(k) == ';':
                k += 1
            # the pc increment
            if ok and tk(k) == 'name' and tv(k) == pc:
                if tk(k + 1) == 'op' and tv(k + 1) == '+=' and tk(k + 2) == 'num' and toks[k + 2][1] == 1:
                    k += 3
                elif tk(k + 1) == 'op' and tv(k + 1) == '=':
                    k += 2
                    p2 = 0
                    while tk(k) == 'op' and tv(k) == '(':
                        p2 += 1
                        k += 1
                    if tk(k) == 'name' and tv(k) == pc and tk(k + 1) == 'op' and tv(k + 1) == '+' and tk(k + 2) == 'num' \
                            and toks[k + 2][1] == 1:
                        k += 3
                        for _ in range(p2):
                            if not (tk(k) == 'op' and tv(k) == ')'):
                                ok = False
                            k += 1
                    else:
                        ok = False
                else:
                    ok = False
                if ok:
                    if tk(k) == 'op' and tv(k) == ';':
                        k += 1
                    h.insn, h.op, h.field = names[0], opname, field
                    h.plain = h.plain and not par and src_is_plain_t(toks, j, k)
                    j = k
    # junk statements between the fetch and the `if`: local J = <number>
    while h.insn is None and tk(j) == 'kw' and tv(j) == 'local' and tk(j + 1) == 'name' and tk(j + 2) == 'op' and tv(j + 2) == '=':
        text, k = number(j + 3)
        if text is None:
            break
        junk.append('local %s=%s;' % (tv(j + 1), text))
        if tk(k) == 'op' and tv(k) == ';':
            k += 1
        j = k
        h.plain = False
    if not (tk(j) == 'kw' and tv(j) == 'if'):
        return None
    h.if_tok = j
    h.junk = ''.join(junk)
    if extra:
        h.op = ','.join(names)
    return h


def src_is_plain_t(toks, a, b):
    """True when tokens a..b-1 are exactly `local OP = I [ n ] ; PC = PC + 1 ;` (already canonical)."""
    kinds = [(t[0], t[1]) for t in toks[a:b]]
    return len(kinds) == 14 and kinds[7] == ('op', ';') and kinds[9] == ('op', '=') and kinds[11] == ('op', '+')


def find_heads(toks):
    """Every dispatch-loop head in a token list (luparse.tokenize without the eof token), in
    source order.  A loop qualifies when it never ends by its condition (`while <truthy literal>`
    / `repeat ... until false`) and starts with the instruction fetch."""
    n = len(toks)
    exprif = _expr_ifs(toks)

    def is_(i, kind, val):
        return 0 <= i < n and toks[i][0] == kind and toks[i][1] == val

    def truthy(a, b):
        """tokens a..b-1 are a literal that is always true, possibly in parentheses"""
        while b - a >= 3 and is_(a, 'op', '(') and is_(b - 1, 'op', ')'):
            a, b = a + 1, b - 1
        return b - a == 1 and (is_(a, 'kw', 'true') or toks[a][0] in ('str', 'num'))

    def after(j):
        """True when a statement follows in the block of the loop that ends in front of token j"""
        while is_(j, 'op', ';'):
            j += 1
        return j < n and not (toks[j][0] == 'kw' and toks[j][1] in ('end', 'until', 'else', 'elseif'))

    out = []
    stack = []      # open blocks: None or a Head waiting for its `end` / `until`
    wh = []         # indices of `while` / `for` keywords waiting for their `do`
    for i in range(n):
        k, v = toks[i][0], toks[i][1]
        if k != 'kw' or i in exprif:
            continue
        if v in ('while', 'for'):
            wh.append(i)
        elif v == 'do':
            w = wh.pop() if wh else None
            if w is not None and toks[w][1] == 'while' and truthy(w + 1, i):
                h = _head_fetch(toks, i + 1)
                if h is not None:
                    h.start, h.until, h.last = w, None, None
                    h.plain = h.plain and i == w + 2 and is_(w + 1, 'kw', 'true')
                    out.append(h)
                stack.append(h)
            else:
                stack.append(None)
        elif v in ('function', 'if'):
            stack.append(None)
        elif v == 'repeat':
            h = _head_fetch(toks, i + 1)
            if h is not None:
                h.start = i
            stack.append(h)
        elif v == 'end':
            h = stack.pop() if stack else None
            if h is not None and h.until is None:
                h.last = i
                h.tail = after(i + 1)
        elif v == 'until':
            h = stack.pop() if stack else None
            if h is not None and h.last is None:
                a = i + 1
                par = 0
                while is_(a, 'op', '('):
                    par += 1
                    a += 1
                if is_(a, 'kw', 'false') and all(is_(a + 1 + x, 'op', ')') for x in range(par)):
                    h.until, h.last = i, a + par
                    h.plain = False
                    h.tail = after(a + par + 1)
                    out.append(h)
    out.sort(key=lambda h: h.start)
    return out


def count_heads(src):
    """Number of Luraph dispatch-loop heads in a text, in any spelling (0: no interpreter in it)."""
    try:
        toks = [t for t in luparse.tokenize(src) if t[0] != 'eof']
    except SyntaxError:
        return 0
    return len(find_heads(toks))


def normalise_heads(src):
    """Rewrite every dispatch-loop head to its canonical form (see above).
    Returns (new source, number of heads found, number rewritten)."""
    toks = [t for t in luparse.tokenize(src) if t[0] != 'eof']
    heads = find_heads(toks)
    edits = []
    for h in heads:
        if h.plain:
            continue
        if h.insn is not None:
            fetch = 'local %s=%s[%s];local %s=%s[%s];%s=%s+1;' % (h.insn, h.code, h.pc, h.op, h.insn, luparse_num(h.field), h.pc, h.pc)
        else:
            fetch = 'local %s=%s[%s];' % (h.op, h.code, h.pc)
        # (a junk local hoisted in front of the loop must not capture a name the code after the
        # loop uses: such a loop gets a block of its own)
        scope = bool(h.junk) and h.tail
        edits.append((toks[h.start][3], toks[h.if_tok][3], '%s%swhile true do %s' % ('do ' if scope else '', h.junk, fetch)))
        if h.until is not None:
            edits.append((toks[h.until][3], toks[h.last][3] + (5 if toks[h.last][1] == 'false' else 1), 'end end' if scope else 'end'))
        elif scope:
            edits.append((toks[h.last][3], toks[h.last][3] + 3, 'end end'))
    edits.sort()
    out = []
    last = 0
    for a, b, text in edits:
        if a < last:
            raise ValueError('overlapping edits')
        out.append(src[last:a])
        out.append(text)
        last = b
    out.append(src[last:])
    return ''.join(out), len(heads), len(edits and [h for h in heads if not h.plain])


def luparse_num(v):
    return repr(int(v)) if float(v).is_integer() else repr(v)


def unshadow(src, makers):
    """Rename locals of closure makers that redeclare a name of the enclosing loader.

    makers = [(position of the maker in the source-order list of `function` keywords, names)].
    Luraph v14.4 closure makers cache loader state and then reuse the name
    (`local w={A[18],A,...};local A=(S[1]);`).  The harness dumps the interpreter's surroundings by
    name from inside the interpreter, so the loader's `A` would be lost, and the lifter, which
    executes the maker's body again, needs it.  Each such local gets the suffix `__s` from the end
    of its declaration to the end of the maker (nothing in that range can mean the outer variable).
    Only declarations at the top level of the maker's body are handled.  Returns (text, renames)."""
    toks = [t for t in luparse.tokenize(src) if t[0] != 'eof']
    n = len(toks)
    exprif = _expr_ifs(toks)
    fkw = [q for q in range(n) if toks[q][0] == 'kw' and toks[q][1] == 'function']
    edits = {}
    count = 0
    OPEN, CLOSE = ('function', 'if', 'do', 'repeat'), ('end', 'until')
    for ordinal, names in makers:
        if ordinal >= len(fkw) or not names:
            continue
        f = fkw[ordinal]
        # end of the parameter list
        q = f + 1
        while q < n and not (toks[q][0] == 'op' and toks[q][1] == ')'):
            q += 1
        body0 = q + 1
        # matching end
        depth = 0
        fend = None
        for q in range(f, n):
            k, v = toks[q][0], toks[q][1]
            if k != 'kw' or q in exprif:
                continue
            if v in OPEN:
                depth += 1
            elif v in CLOSE:
                depth -= 1
                if depth == 0:
                    fend = q
                    break
        if fend is None:
            continue
        pending = set(names)
        depth = 0           # blocks
        br = []             # open brackets
        q = body0
        while q < fend and pending:
            k, v = toks[q][0], toks[q][1]
            if k == 'kw' and q not in exprif:
                if v in OPEN:
                    depth += 1
                elif v in CLOSE:
                    depth -= 1
            elif k == 'op' and v in ('(', '[', '{'):
                br.append(v)
            elif k == 'op' and v in (')', ']', '}') and br:
                br.pop()
            if k == 'kw' and v == 'local' and depth == 0 and not br and q + 1 < fend and toks[q + 1][0] == 'name':
                decl = []
                j = q + 1
                while toks[j][0] == 'name':
                    decl.append(j)
                    if toks[j + 1][0] == 'op' and toks[j + 1][1] == ',':
                        j += 2
                    else:
                        j += 1
                        break
                clash = [d for d in decl if toks[d][1] in pending]
                if clash:
                    # end of the statement: `;` or the next statement keyword outside brackets and blocks
                    e = j
                    d2 = 0
                    b2 = 0
                    while e < fend:
                        k2, v2 = toks[e][0], toks[e][1]
                        if k2 == 'kw' and e not in exprif and v2 in OPEN and not (v2 == 'if' and d2 == 0 and b2 == 0):
                            d2 += 1
                        elif k2 == 'kw' and e not in exprif and v2 in CLOSE:
                            d2 -= 1
                        elif k2 == 'op' and v2 in ('(', '[', '{'):
                            b2 += 1
                        elif k2 == 'op' and v2 in (')', ']', '}'):
                            b2 -= 1
                        elif d2 == 0 and b2 == 0 and (k2 == 'op' and v2 == ';' or k2 == 'kw' and e not in exprif and v2 in (
                                'local', 'return', 'if', 'while', 'for', 'repeat', 'break', 'continue')):
                            break
                        e += 1
                    for d in clash:
                        x = toks[d][1]
                        pending.discard(x)
                        edits[d] = x + '__s'
                        inbr = []
                        for r in range(e, fend):
                            kr, vr = toks[r][0], toks[r][1]
                            if kr == 'op' and vr in ('(', '[', '{'):
                                inbr.append(vr)
                            elif kr == 'op' and vr in (')', ']', '}') and inbr:
                                inbr.pop()
                            elif kr == 'name' and vr == x:
                                prev = toks[r - 1]
                                if prev[0] == 'op' and prev[1] in ('.', ':'):
                                    continue
                                if inbr and inbr[-1] == '{' and prev[0] == 'op' and prev[1] in ('{', ',', ';') \
                                        and toks[r + 1][0] == 'op' and toks[r + 1][1] == '=':
                                    continue        # a field name in a table constructor
                                edits[r] = x + '__s'
                        count += 1
            q += 1
    if not edits:
        return src, 0
    out = []
    last = 0
    for q in sorted(edits):
        pos = toks[q][3]
        out.append(src[last:pos])
        out.append(edits[q])
        last = pos + len(toks[q][1])
    out.append(src[last:])
    return ''.join(out), count
