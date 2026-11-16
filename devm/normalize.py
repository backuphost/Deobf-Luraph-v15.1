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


def inline_protected_raw(src):
    """The same rewrite as inline_protected for a script in Luraph's own minified layout:

        local ok, a, b, c = pcall(function() local ...; repeat local op = code[pc]; if ... until false; end);
        if ok then <act on a, b, c> else <re-raise> end

    The dispatch loop becomes `while true do ... end` in the interpreter function itself and every
    `return x, y, z` of the protected closure becomes the success branch with those values bound.
    Works on tokens, so the layout of the rest of the file is untouched.
    Returns (new source, number of interpreters rewritten)."""
    toks = [t for t in luparse.tokenize(src) if t[0] != 'eof']
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
        if not (is_(j, 'kw', 'if') and toks[j + 1] [1] == ok and is_(j + 2, 'kw', 'then')):
            continue
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
        succ_end = toks[els][3] if els is not None else toks[ifend][3]
        success = src[toks[j + 3][3]:succ_end].rstrip().rstrip(';')
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
    if not count:
        return src, 0
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
    return ''.join(out), count
