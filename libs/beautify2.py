#!/usr/bin/env python3
"""Line-break + indent minified Lua/Luau. Lossless (checked).

This module used to carry its own lexer and its own formatter.  The lexer did not know
0b / 0x_ / digit separators (`0B110001_1` came out as `0 B110001_1`) and the formatter could
glue `[` to a following long string.  Both now come from deobf_plus.py, the one lexer and
formatter of the static tool; the names below are kept for existing callers.

    python libs/beautify2.py in.lua out.lua
"""
import sys
from pathlib import Path

_LIBS = Path(__file__).resolve().parent
if str(_LIBS) not in sys.path:
    sys.path.insert(0, str(_LIBS))

from deobf_plus import tokenize, format_checked, same_tokens      # noqa: E402,F401

OPEN = {'function', 'then', 'do', 'repeat'}
MID = {'else', 'elseif'}
CLOSE = {'end', 'until'}
MAXD = 16


def build_tokens(toks):
    """Formatted text of a token list (whitespace only; falls back to a plain layout if the
    pretty one would not lex back to the same tokens)."""
    return format_checked(toks)[0]


def build(s):
    return build_tokens(tokenize(s))


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    from deobf_plus import read_lua
    src = read_lua(sys.argv[1])
    toks = tokenize(src)
    lay = {}
    out, ok = format_checked(toks, info=lay)
    Path(sys.argv[2]).write_bytes((out + '\n').encode('utf-8', errors='surrogateescape'))
    ls = [len(l) for l in out.split('\n')]
    print('lines', len(ls), 'bytes', len(out), 'maxline', max(ls), 'median', sorted(ls)[len(ls) // 2],
          'lossless', ok and same_tokens(tokenize(out), toks, comments=True),
          'layout', 'plain fallback (%s)' % lay.get('reason') if lay.get('fallback') else 'indented')
    sys.exit(0 if (ok and not lay.get('fallback')) else 3)
