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
