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
