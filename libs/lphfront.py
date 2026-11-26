#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lphfront.py: what a text says about itself as Luraph output, WITHOUT running or parsing it
with an interpreter.  Everything here works on a token stream (one regular expression).

The front end (deob.py) uses it to decide, before any engine is started:

  * where the Luraph header comment is (`-- This file was generated|protected using Luraph
    Obfuscator vX.Y[.Z]`, with or without ` [https://lura.ph/]`, with text glued after it,
    anywhere in the file), and what the script's owner put around it (prefix / suffix kinds);
  * the MAIN STATEMENT: the first `return` at bracket depth 0 and block depth 0 (a `return`
    inside `if not is_from_loader then ... return end` is not it), and its shape:

        return(function() ... end)()(...)                 fn0      compressed container
        return(function(P1..Pn) ... end)(A1..An)(...)     fn-args  VM of v14.0.2 - v14.3
        return({...}):M()(...)                            table    VM of v14.4 - v14.9
        return setmetatable({...},{}):M()(...)            setmt    VM of v15.x, or a v15 container
        local B=...;return({...}):M()                     a chunk taken out of a container
        return setmetatable({...},{}):M(...)              (it gets its data as argument)

  * whether the text is whole (main statement closed, no unterminated literal), which Lua it
    was built for (Luau, Lua 5.1, Lua 5.3+, LuaJIT), and whether it is Luraph at all.

classify(text) gives the verdict as far as the text alone can: 'luraph', 'not-luraph',
'damaged', 'unsupported-target'.  What needs a compiler (tampered text) or the container's
decoder (a corrupt payload, the layout of what the container holds) is added by the caller.
"""
import json
import re

# ---------------------------------------------------------------- tokens

# White space is Luau's: these six characters and nothing else (Python's \s also takes
# U+0085, U+00A0 ... which are not white space to any Lua).
TOKEN = re.compile(r'''
    (?P<ws>[ \t\r\n\f\v]+)
  | (?P<lcom>--\[(?P<lc_eq>=*)\[[\s\S]*?\](?P=lc_eq)\])
  | (?P<lcbad>--\[=*\[)
  | (?P<com>--[^\n]*)
  | (?P<lstr>\[(?P<ls_eq>=*)\[[\s\S]*?\](?P=ls_eq)\])
  | (?P<lsbad>\[=*\[)
  | (?P<dstr>"[^"\\\n]*(?:\\(?:z[ \t\r\n\f\v]*|\r\n|[\s\S])[^"\\\n]*)*")
  | (?P<sstr>'[^'\\\n]*(?:\\(?:z[ \t\r\n\f\v]*|\r\n|[\s\S])[^'\\\n]*)*')
  | (?P<bstr>`[^`\\]*(?:\\[\s\S][^`\\]*)*`)
  | (?P<qbad>["'`])
  | (?P<num>(?:0[xX][0-9a-fA-F_]*(?:\.[0-9a-fA-F_]*)?(?:[pP][+-]?[0-9_]+)?
              |0[bB][01_]+
              |(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?[\d_]+)?)
            (?:[uU]?[lL][lL]|i\b)?)
  | (?P<id>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>\.\.\.|\.\.=|\.\.|==|~=|<=|>=|<<|>>|//=|//|::|->|\+=|-=|\*=|/=|%=|\^=
           |[-+*/%^\#&|~<>=(){}\[\];:,.?@!])
  | (?P<bad>[\s\S])
''', re.X)

STRING_KINDS = ('dstr', 'sstr', 'lstr')


def scan(text):
    """(code tokens [(kind, text, offset)], comments [(offset, text)], damage).

    damage: list of (offset, what) for places where the text stops being Lua tokens: an
    unfinished long string / long comment (everything behind it is inside the literal, the
    scan stops there), an unfinished quoted string (the scan goes on at the next line, as a
    compiler's error recovery would), a character that starts no token."""
    toks, comments, damage = [], [], []
    pos, n = 0, len(text)
    while pos < n:
        restart = None
        for m in TOKEN.finditer(text, pos):
            k = m.lastgroup
            if k == 'ws':
                continue
            if k == 'com' or k == 'lcom':
                comments.append((m.start(), m.group(k)))
            elif k == 'lsbad' or k == 'lcbad':
                damage.append((m.start(), 'unfinished long string' if k == 'lsbad'
                               else 'unfinished long comment'))
                return toks, comments, damage
            elif k == 'qbad':
                damage.append((m.start(), 'unfinished string'))
                nl = text.find('\n', m.end())
                restart = n if nl < 0 else nl + 1
                break
            elif k == 'bad':
                if len(damage) < 64:
                    damage.append((m.start(), 'stray character %r' % m.group(k)))
            else:
                toks.append((k, m.group(k), m.start()))
        if restart is None:
            break
        pos = restart
    return toks, comments, damage


# ---------------------------------------------------------------- header, recovery

HEADER_RE = re.compile(
    r'This file was (generated|protected) using Luraph Obfuscator v(\d+(?:\.\d+)*)(?:\s*\[([^\]\n]*)\])?')
GENUINE_URL = 'https://lura.ph/'
# line breaks lost: the program sits behind the `--` of the header comment
_ONELINE_TAIL = re.compile(r'Luraph Obfuscator v[\d.]+(?:[ \t]*\[[^\]\n]*\])?([ \t]+)(?:return|local)\b')


def find_header(text):
    """(offset of the `--` that starts the header comment, re.Match of HEADER_RE) for the first
    header comment in `text`, or None.  Cheap (no token scan): the words are searched, then the
    `--` in front of them on the same line; a hit inside a string literal is possible in
    theory, analyze() is the exact answer."""
    at = 0
    while True:
        idx = text.find('Luraph Obfuscator v', at)
        if idx < 0:
            return None
        line = text.rfind('\n', 0, idx) + 1
        if idx - line < 400:
            dd = text.find('--', line, idx)
            m = HEADER_RE.search(text, max(line, idx - 40), idx + 80)
            if dd >= 0 and m:
                return dd, m
        at = idx + 1


def json_unwrap(text):
    """(key path, Lua text) when `text` is a JSON value that carries Lua source in a string
    (`{"luaCode": "-- This file was protected ..."}`), else None.  The longest string wins."""
    s = text.lstrip(' \t\r\n')
    if not s or s[0] not in '{["':
        return None
    try:
        data = json.loads(s)
    except (ValueError, RecursionError):
        return None
    best = None

    def walk(v, key, depth=0):
        nonlocal best
        if isinstance(v, str):
            if best is None or len(v) > len(best[1]):
                best = (key, v)
        elif depth < 16 and isinstance(v, dict):
            for k2, v2 in v.items():
                walk(v2, (key + '.' if key else '') + str(k2), depth + 1)
        elif depth < 16 and isinstance(v, list):
            for i, v2 in enumerate(v):
                walk(v2, '%s[%d]' % (key, i), depth + 1)
    walk(data, '')
    if best is None or len(best[1]) < 16:
        return None
    if not (HEADER_RE.search(best[1]) or re.search(r'\b(?:return|local|function)\b', best[1])):
        return None
    return best


def recover(text):
    """(text, how).  Two kinds of file do not hold their Lua text as it was written:
    'json:<key>'  a JSON value with the source in a string;
    'oneline'     the line breaks were lost, so the whole program sits behind the `--` of the
                  header comment (the file as it stands is an empty chunk): the code is given
                  its own line again.
    how is None for an ordinary file, else the steps joined with '+'."""
    how = []
    j = json_unwrap(text)
    if j is not None:
        how.append('json:' + j[0])
        text = j[1]
    hit = find_header(text)
    if hit:
        m = _ONELINE_TAIL.match(text, hit[1].start() + len('This file was protected using '))
        if m:
            eol = text.find('\n', m.end())
            if (len(text) if eol < 0 else eol) - m.end() > 100 and _is_comment_start(text, hit[0]):
                how.append('oneline')
                text = text[:m.start(1)] + '\n\n' + text[m.end(1):]
    return text, ('+'.join(how) if how else None)


def _is_comment_start(text, pos):
    """True when the `--` at `pos` starts a line comment that is not inside a literal: only
    comments, blank space or short statements may stand in front of it (a cheap test: no
    token damage and no string that swallows `pos` in the text before it)."""
    toks, comments, damage = scan(text[:pos])
    return not damage and all(o + len(t) <= pos for _k, t, o in toks[-1:])


# ---------------------------------------------------------------- structure

OPEN_BR = {'(': ')', '[': ']', '{': '}'}
CLOSE_BR = {')', ']', '}'}
COMPOUND = {'+=', '-=', '*=', '/=', '%=', '^=', '..=', '//='}
STMT_END = {';', 'end', 'else', 'elseif', 'until'}
STMT_START = {'local', 'return', 'for', 'while', 'break'}
# a token after which an `if` can only be an if-EXPRESSION (Luau), which has no `end`
EXPR_PREV = {'=', '(', ',', '{', '[', '+', '-', '*', '/', '%', '^', '#', '..', '==', '~=', '<', '>', '<=',
             '>=', '//', '&', '|', '~', '<<', '>>', '+=', '-=', '*=', '/=', '%=', '^=', '..=', '//=',
             'return', 'and', 'or', 'not', 'while', 'until', 'if', 'elseif', 'in'}
MARKERS = {
    'env_msg': 'does your environment support load/loadstring?',
    'decomp_msg': 'Luraph decompression error',
    'zstd_call': 'DecompressBuffer',
}
LIBS_ANY = {'string', 'table', 'math', 'bit32', 'buffer', 'coroutine', 'debug', 'utf8', 'vector', 'task'}
BIT_FNS = {'band', 'bor', 'bxor', 'bnot', 'lshift', 'rshift', 'arshift', 'rol', 'ror', 'bswap', 'tobit', 'tohex'}
LUAU_LIB_FNS = {'table.create', 'table.find', 'table.clear', 'table.clone', 'table.freeze', 'table.isfrozen',
                'bit32.countlz', 'bit32.countrz', 'bit32.byteswap', 'string.split', 'math.clamp', 'math.round',
                'math.sign', 'math.noise', 'coroutine.close', 'debug.info', 'debug.profilebegin'}
LUAU_LIBS = {'buffer', 'vector', 'task'}
LUAU_GLOBALS = {'typeof', 'game', 'workspace', 'Instance', 'Enum', 'Vector3', 'CFrame', 'Color3', 'UDim2'}
WATCHED_GLOBALS = LUAU_GLOBALS | {'getfenv', 'setfenv', '_ENV', 'loadstring', 'load', 'unpack', 'bit32',
                                  'buffer', 'bit'}
ESC_BADX_RE = re.compile(r'\\(?:x(?![0-9a-fA-F]{2})|u(?!\{[0-9a-fA-F]+\}))')


def _clip(s, n=160):
    s = ' '.join(s.split())
    return s if len(s) <= n else s[:n] + '...'


def _shorten(text, keep=24):
    """`text` with every long literal cut down (for messages)."""
    out, pos = [], 0
    for m in TOKEN.finditer(text):
        if m.lastgroup in STRING_KINDS and m.end() - m.start() > 4 * keep:
            s = m.group(0)
            out.append(text[pos:m.start()] + '%s<%d chars>%s' % (s[:keep], len(s), s[-4:]))
            pos = m.end()
    out.append(text[pos:])
    return ''.join(out)


class Facts:
    """Token-level facts of one text (see analyze)."""

    def __init__(self):
        self.size = 0
        self.empty = False
        self.tokens = 0
        self.damage = []            # (offset, what)
        self.headers = []           # [{offset, wording, version, url, rest, exact}]
        self.comments = []          # (offset, text)
        self.markers = []
        self.open_brackets = 0      # brackets still open at the end of the text
        self.open_blocks = 0
        self.mismatches = 0
        self.if_expressions = 0
        self.main_return = None     # offset of the main `return`
        self.head = None            # fn0 | fn-args | table | setmt | other description | None
        self.head_info = {}
        self.tail = None            # signature of the call chain, e.g. ':M()(...)'; None = not closed
        self.methods = []
        self.closed = False
        self.stmt_end = None        # offset just behind the main statement (and its `;`)
        self.pre_code = ''          # code between the start (or the header) and the main return
        self.pre_header_code = ''
        self.post_header_code = ''
        self.pre_header_comments = []
        self.post_header_comments = []
        self.pre_header_blank = False
        self.header_after_main = False
        self.trailing_code = ''
        self.trailing_comments = []
        self.n_lph = 0
        self.lph = []               # [(kind, length)]
        self.longest_string = 0
        self.feats = {}
        self.libs = set()
        self.globals = set()
        self.table_keys = None      # name keys of the VM object's table (table / setmt heads)


def analyze(text):
    """Facts about `text` (a str; a UTF-8 byte order mark must be gone)."""
    fx = Facts()
    fx.size = len(text)
    if not text.strip(' \t\r\n\f\v'):
        fx.empty = True
        return fx
    toks, comments, damage = scan(text)
    n = len(toks)
    fx.tokens, fx.comments, fx.damage = n, comments, damage
    fx.markers = sorted(k for k, v in MARKERS.items() if v in text)

    for pos, c in comments:
        m = HEADER_RE.search(c)
        if m:
            fx.headers.append({
                'offset': pos, 'wording': m.group(1), 'version': m.group(2), 'url': m.group(3),
                'rest': c[m.end():].strip(),
                'exact': bool(re.fullmatch(r'-- This file was (generated|protected) using Luraph Obfuscator '
                                           r'v[\d.]+( \[https://lura\.ph/\])?\s*', c))})

    # ---- brackets, blocks, the main `return`
    # Luau has `if c then a else b` EXPRESSIONS (no `end`): an `if` is taken for one when the
    # token in front of it cannot end a statement (EXPR_PREV), or when it directly follows the
    # then/else of another if-expression.  bstack holds [token index, 'B'] for block openers
    # and [token index, state, bracket depth] for if-expressions (state: cond / then / else).
    match, bmatch = {}, {}
    bstack, stack = [], []
    pdepth = bdepth = 0
    main_ret = None
    prev = ''
    for i in range(n):
        k, t, _p = toks[i]
        if k == 'op':
            if t in OPEN_BR:
                stack.append(i)
                pdepth += 1
            elif t in CLOSE_BR:
                while bstack and len(bstack[-1]) == 3 and bstack[-1][2] >= pdepth:
                    bstack.pop()
                if stack and OPEN_BR[toks[stack[-1]][1]] == t:
                    match[stack.pop()] = i
                    pdepth -= 1
                else:
                    fx.mismatches += 1
            elif t == ';':
                while bstack and len(bstack[-1]) == 3 and bstack[-1][2] >= pdepth:
                    bstack.pop()
            elif t == ',':
                while bstack and len(bstack[-1]) == 3 and bstack[-1][2] == pdepth and bstack[-1][1] == 'else':
                    bstack.pop()
        elif k == 'id':
            if t == 'if':
                top = bstack[-1] if bstack else None
                if prev in EXPR_PREV or (prev in ('then', 'else') and top is not None and len(top) == 3):
                    bstack.append([i, 'cond', pdepth])
                    fx.if_expressions += 1
                else:
                    while bstack and len(bstack[-1]) == 3 and bstack[-1][2] >= pdepth:
                        bstack.pop()
                    bstack.append([i, 'B'])
                    bdepth += 1
            elif t == 'then' or t == 'else' or t == 'elseif':
                while bstack and len(bstack[-1]) == 3:
                    x = bstack[-1]
                    if x[1] == 'else':
                        bstack.pop()
                        continue
                    if t == 'then':
                        x[1] = 'then' if x[1] == 'cond' else x[1]
                    elif t == 'else':
                        x[1] = 'else' if x[1] == 'then' else x[1]
                    else:
                        x[1] = 'cond' if x[1] == 'then' else x[1]
                    break
            elif t == 'function' or t == 'repeat' or t == 'do':
                if t == 'do':
                    while bstack and len(bstack[-1]) == 3 and bstack[-1][1] == 'else':
                        bstack.pop()
                bstack.append([i, 'B'])
                bdepth += 1
            elif t == 'end' or t == 'until':
                while bstack and len(bstack[-1]) == 3:
                    bstack.pop()
                bdepth -= 1
                if bstack:
                    bmatch[bstack.pop()[0]] = i
            elif t in STMT_START:
                while bstack and len(bstack[-1]) == 3 and bstack[-1][2] >= pdepth and bstack[-1][1] == 'else':
                    bstack.pop()
                if t == 'return' and main_ret is None and pdepth == 0 and bdepth == 0:
                    main_ret = i
        prev = t
    fx.open_brackets = len(stack)
    fx.open_blocks = bdepth

    # ---- text around the header and the main statement
    hpos = fx.headers[0]['offset'] if fx.headers else None
    ret_pos = toks[main_ret][2] if main_ret is not None else None
    fx.main_return = ret_pos
    lim = ret_pos if ret_pos is not None else len(text)

    def code_between(a, b):
        sel = [t for t in toks if a <= t[2] < b]
        if not sel:
            return ''
        return text[sel[0][2]:sel[-1][2] + len(sel[-1][1])]

    if hpos is not None:
        fx.pre_header_code = code_between(0, min(hpos, lim))
        fx.pre_header_comments = [c for p, c in comments if p < hpos]
        fx.pre_header_blank = bool(text[:hpos]) and not text[:hpos].strip(' \t\r\n\f\v')
        fx.post_header_code = code_between(hpos, lim) if hpos < lim else ''
        fx.post_header_comments = [c for p, c in comments if hpos < p < lim]
        fx.header_after_main = ret_pos is not None and hpos > ret_pos
    else:
        fx.pre_code = code_between(0, lim)

    if main_ret is not None:
        _main_statement(fx, text, toks, match, bmatch, main_ret, comments)

    # ---- payload strings
    # (literals behind the closed main statement are not the program's: one corpus file has a
    # dump of its own inner chunk appended)
    for k, t, _p in toks:
        if k in STRING_KINDS:
            if fx.stmt_end is not None and _p >= fx.stmt_end:
                break
            ln = len(t)
            if ln > fx.longest_string:
                fx.longest_string = ln
            if ln > 64:
                body = t[1:4] if k != 'lstr' else t[t.index('[', 1) + 1:t.index('[', 1) + 4]
                if body == 'LPH':
                    fx.lph.append((k, ln))
    fx.n_lph = len(fx.lph)

    # ---- syntax features (which Lua the text was written for)
    feats = {}

    def bump(key, by=1):
        feats[key] = feats.get(key, 0) + by
    for i in range(n):
        k, t, _p = toks[i]
        if k == 'num':
            c2 = t[:2]
            if '_' in t:
                bump('num_underscore')
            if c2 in ('0b', '0B'):
                bump('num_binary')
            elif c2 in ('0x', '0X') and ('p' in t or 'P' in t or '.' in t):
                bump('num_hexfloat')
        elif k == 'op':
            if t in COMPOUND:
                bump('compound_assign')
            elif t in ('&', '|', '<<', '>>'):
                bump('op_bit_native')
            elif t == '~':
                bump('op_tilde')
            elif t == '::':
                bump('op_dcolon')
        elif k == 'id':
            before = toks[i - 1][1] if i else ''
            nxt = toks[i + 1][1] if i + 1 < n else ''
            if before == '.' or before == ':':
                continue
            if t == 'continue':
                if nxt in STMT_END or nxt == '':
                    bump('continue_stmt')
            elif t == 'goto':
                if i + 1 < n and toks[i + 1][0] == 'id':
                    bump('goto_stmt')
            if nxt == '.' and i + 2 < n and toks[i + 2][0] == 'id':
                f = toks[i + 2][1]
                if t in LIBS_ANY or (t == 'bit' and f in BIT_FNS):
                    fx.libs.add(t + '.' + f)
            if t in WATCHED_GLOBALS:
                fx.globals.add(t)
        elif k == 'bstr':
            bump('backtick_string')
        elif (k == 'dstr' or k == 'sstr') and '\\' in t and len(t) < 4096:
            nb = len(ESC_BADX_RE.findall(t.replace('\\\\', '')))
            if nb:
                bump('esc_lua51_only', nb)
    for i in range(n - 2):
        if toks[i][1] == '<' and toks[i + 1][1] in ('const', 'close') and toks[i + 2][1] == '>':
            bump('attrib_const_close')
    fx.feats = feats
    return fx


def _args_summary(toks, bmatch, a, b):
    if b == a + 1:
        return '()'
    if b == a + 2 and toks[a + 1][1] == '...':
        return '(...)'
    return '(N)'


def _table_keys(toks, match, bmatch, o):
    """Name keys (`K=`) at the top level of the table constructor that opens at token o, or
    None when the constructor is not closed."""
    c = match.get(o)
    if c is None:
        return None
    keys = set()
    start = True
    i = o + 1
    while i < c:
        k, t, _p = toks[i]
        if start:
            start = False
            if k == 'id' and i + 1 < c and toks[i + 1][1] == '=':
                keys.add(t)
            elif t == '[':
                e = match.get(i)
                if e is not None and e + 1 < c and toks[e + 1][1] == '=':
                    if e == i + 2 and toks[i + 1][0] in ('dstr', 'sstr'):
                        keys.add(toks[i + 1][1][1:-1])
                    i = e
        k, t, _p = toks[i]
        if k == 'op':
            if t in OPEN_BR:
                m = match.get(i)
                if m is None:
                    break
                i = m
            elif t == ',' or t == ';':
                start = True
        elif t == 'function' and k == 'id':
            m = bmatch.get(i)
            if m is None or m > c:
                break
            i = m
        i += 1
    return keys


def _main_statement(fx, text, toks, match, bmatch, r, comments):
    n = len(toks)
    i = r + 1
    if i >= n:
        fx.head = 'return-nothing'
        return
    k, t, _p = toks[i]
    prim_end = None
    head = 'other'
    info = {}
    if t == '(':
        j = match.get(i)
        inner = toks[i + 1][1] if i + 1 < n else ''
        if inner == 'function':
            po = i + 2
            pc = match.get(po) if po < n and toks[po][1] == '(' else None
            if pc is not None:
                params = [toks[x][1] for x in range(po + 1, pc) if toks[x][0] != 'op' or toks[x][1] == '...']
                info['n_params'] = len(params)
                info['vararg_param'] = '...' in params
                head = 'fn0' if not params else 'fn-args'
            else:
                head = 'fn-unclosed-params'
        elif inner == '{':
            head = 'table'
            fx.table_keys = _table_keys(toks, match, bmatch, i + 1)
        else:
            head = 'paren-other'
        prim_end = j
    elif t == 'setmetatable' and i + 1 < n and toks[i + 1][1] == '(':
        j = match.get(i + 1)
        if i + 2 < n and toks[i + 2][1] == '{':
            head = 'setmt'
            fx.table_keys = _table_keys(toks, match, bmatch, i + 2)
        else:
            head = 'setmt-other'
        prim_end = j
    elif k == 'id':
        head = 'id:' + t
        prim_end = i
    elif t == '{':
        head = 'bare-table'
        prim_end = match.get(i)
    else:
        head = 'expr:' + _clip(t, 20)
    fx.head, fx.head_info = head, info
    if prim_end is None:
        fx.closed, fx.tail = False, None
        return
    sig = []
    j = prim_end + 1
    last = prim_end
    while j < n:
        t = toks[j][1]
        if t == '(':
            c = match.get(j)
            if c is None:
                sig.append('(<unclosed>')
                last = None
                break
            sig.append(_args_summary(toks, bmatch, j, c))
            last = c
            j = c + 1
        elif t == ':' and j + 1 < n and toks[j + 1][0] == 'id':
            sig.append(':M')
            fx.methods.append(toks[j + 1][1])
            last = j + 1
            j += 2
        elif t == '.' and j + 1 < n and toks[j + 1][0] == 'id':
            sig.append('.F')
            last = j + 1
            j += 2
        elif t == '[':
            c = match.get(j)
            if c is None:
                sig.append('[<unclosed>')
                last = None
                break
            sig.append('[i]')
            last = c
            j = c + 1
        elif toks[j][0] in STRING_KINDS or t == '{':
            if t == '{':
                c = match.get(j)
                if c is None:
                    last = None
                    break
                j = c + 1
                last = c
            else:
                last = j
                j += 1
            sig.append('"s"')
        else:
            break
    fx.tail = ''.join(sig)
    fx.closed = last is not None
    if last is None:
        return
    if j < n and toks[j][1] == ';':
        j += 1
    fx.stmt_end = toks[j - 1][2] + len(toks[j - 1][1])
    rest = toks[j:]
    fx.trailing_code = text[rest[0][2]:] if rest else ''
    fx.trailing_comments = [c for p, c in comments if p >= fx.stmt_end]


# ---------------------------------------------------------------- Lua 5.1 escapes

_STR_RE = re.compile(r'"(?:[^"\\\n]|\\[\s\S])*"|\'(?:[^\'\\\n]|\\[\s\S])*\'')


def fix_lua51_escapes(text):
    """Rewrite, inside quoted strings, the escapes that only Lua 5.1 reads as "backslash +
    character = that character" and Luau rejects: `\\x` without two hex digits and `\\u`
    without `{...}`.  The string VALUES stay what Lua 5.1 makes of them; every other escape is
    left as written (Luau reads backslash + any other character the Lua 5.1 way).
    Returns (text, number of escapes rewritten)."""
    count = 0

    def in_string(m):
        nonlocal count
        s = m.group(0)
        if '\\x' not in s and '\\u' not in s:
            return s
        out = []
        i, n = 0, len(s)
        while i < n:
            c = s[i]
            if c != '\\' or i + 1 >= n:
                out.append(c)
                i += 1
                continue
            d = s[i + 1]
            if d == 'x' and not re.match(r'[0-9a-fA-F]{2}', s[i + 2:i + 4]):
                out.append('x')
                count += 1
            elif d == 'u' and not re.match(r'\{[0-9a-fA-F]+\}', s[i + 2:i + 12]):
                out.append('u')
                count += 1
            else:
                out.append(c + d)
            i += 2
        return ''.join(out)
    return _STR_RE.sub(in_string, text), count


# ---------------------------------------------------------------- classification

LURAPH_HEADS = ('fn0', 'fn-args', 'table', 'setmt')
_LOCAL_VARARG = re.compile(r'local\s+[A-Za-z_]\w*\s*=\s*\.\.\.\s*;?$')
LUARMOR_GUARD = re.compile(r'if\s+not\s+is_from_loader\s+then\b[\s\S]*?\breturn\s+end\s*;?')


def platform_of(fx):
    """(label, [evidence]) for the Lua the text was written for.  First match wins:
    native bit operators / attributes -> 'lua5.3+'; Luau-only syntax -> 'luau'; the `bit.*`
    library -> 'luajit'; `goto` -> 'lua5.2+/luajit'; escapes only Lua 5.1 reads -> 'lua5.1';
    Luau-only library functions -> 'luau'; nothing of all that -> 'lua5.1-syntax'."""
    f = fx.feats
    reliable = not fx.damage
    luau_syn = [k for k in ('num_underscore', 'compound_assign', 'continue_stmt', 'backtick_string') if f.get(k)]
    if fx.if_expressions:
        luau_syn.append('if_expression')
    lua53 = [k for k in ('op_bit_native', 'op_tilde', 'attrib_const_close') if f.get(k)]
    bit_fns = sorted(k for k in fx.libs if k.startswith('bit.'))
    luau_libs = sorted({k for k in fx.libs if k in LUAU_LIB_FNS}
                       | {k for k in fx.libs if k.split('.')[0] in LUAU_LIBS}
                       | {k for k in fx.globals if k in LUAU_GLOBALS or k == 'buffer'})
    if lua53 and luau_syn and not reliable:
        return 'luau', luau_syn         # stray operators inside a damaged string
    if lua53:
        ev = lua53 + [k for k in ('goto_stmt', 'num_hexfloat') if f.get(k)] + (['_ENV'] if '_ENV' in fx.globals else [])
        return 'lua5.3+', ev
    if luau_syn:
        return 'luau', luau_syn
    if bit_fns:
        return 'luajit', bit_fns[:4] + (['goto_stmt'] if f.get('goto_stmt') else [])
    if f.get('goto_stmt'):
        return 'lua5.2+/luajit', ['goto_stmt']
    if f.get('esc_lua51_only'):
        return 'lua5.1', ['esc_lua51_only']
    if luau_libs or f.get('num_binary'):
        return 'luau', luau_libs[:4] or ['num_binary']
    return 'lua5.1-syntax', []


def not_luau_syntax(fx):
    """Names of the things in the text that no Luau parser accepts and that mark a build for
    another Lua (not damage): [] when there is none."""
    f = fx.feats
    out = []
    if f.get('op_bit_native') or f.get('op_tilde'):
        out.append('native bit operators (& | ~ << >>)')
    if f.get('goto_stmt'):
        out.append('goto')
    if f.get('num_hexfloat'):
        out.append('hexadecimal floats')
    if f.get('attrib_const_close'):
        out.append('<const>/<close> attributes')
    return out


def prefix_kinds(fx, recovered, luraph_head):
    """Classes of what stands around the header (the census's names)."""
    out = []
    h = fx.headers[0] if fx.headers else None
    if h is None:
        out.append('no-header')
    else:
        if h['url'] is None:
            out.append('header-no-url')
        elif h['url'] != GENUINE_URL:
            out.append('header-fake-url')
        if h['rest'] and 'oneline' not in (recovered or ''):
            out.append('header-trailer')
        if fx.header_after_main:
            out.append('header-at-end')
    if (recovered or '').startswith('json'):
        out.append('json-wrapper')
    if 'oneline' in (recovered or ''):
        out.append('oneline')
    if fx.pre_header_blank:
        out.append('blank-before-header')
    coms = fx.pre_header_comments
    if any('licensed with Luarmor' in c for c in coms):
        out.append('luarmor-comment')
    if any('licensed with Luarmor' not in c for c in coms):
        out.append('comment-before-header')
    if fx.pre_header_code:
        out.append('luarmor-guard' if 'is_from_loader' in fx.pre_header_code else 'code-before-header')
    if fx.post_header_comments and not fx.header_after_main:
        out.append('comment-after-header')
    if fx.post_header_code and luraph_head and not _LOCAL_VARARG.match(fx.post_header_code):
        out.append('luarmor-guard' if LUARMOR_GUARD.fullmatch(fx.post_header_code.strip())
                   else 'code-after-header')
    if h is None and fx.pre_code and luraph_head and not _LOCAL_VARARG.match(fx.pre_code):
        out.append('code-before-vm')
    return out or ['none']


def suffix_kinds(fx):
    out = []
    if fx.trailing_code:
        out.append('trailing-code')
    if fx.trailing_comments and not fx.header_after_main:
        out.append('trailing-comment')
    return out or ['none']


def _where(text, offset):
    """'line L, column C (byte offset N)' of a character offset."""
    line = text.count('\n', 0, offset) + 1
    col = offset - (text.rfind('\n', 0, offset) + 1) + 1
    return 'line %d, column %d (offset %d of %d)' % (line, col, offset, len(text))


def classify(text, fx=None, recovered=None):
    """Static verdict for one text (BOM gone, recover() applied by the caller).

    Returns a dict:
      verdict   'luraph' | 'not-luraph' | 'damaged' | 'unsupported-target'
      reason    one line for the three verdicts that are not 'luraph'
      version   header version ('14.3') or None
      header    None | {wording, version, url, offset, rest}
      wrapper   'none' | 'container:fn0-lzma-1' | 'container:fn0-lzma-2' | 'container:fn0-zstd-2' |
                'container:setmt-setmt-2'   (head, codec, number of payload literals)
      layout    'fn-args' | 'fn-args-vararg' | 'table' | 'setmt' (of THIS text when it is a VM) |
                'unknown' (a container: the layout is that of the chunk inside) | 'none'
      entry     the main statement's call chain: ':M()(...)', '(N)(...)', ...
      method    name of the entry method (table / setmt) or None
      bare_chunk  True for a chunk taken out of a container (it gets its data as an argument)
      platform  see platform_of
      prefix, suffix   kinds (lists)
      stmt_end  offset behind the main statement, or None
      notes     list of remarks"""
    if fx is None:
        fx = analyze(text)
    out = {'verdict': 'not-luraph', 'reason': '', 'version': None, 'header': None, 'wrapper': 'none',
           'layout': 'none', 'entry': None, 'method': None, 'bare_chunk': False, 'platform': 'none',
           'platform_evidence': [], 'prefix': [], 'suffix': [], 'stmt_end': fx.stmt_end, 'notes': []}
    if fx.empty:
        out['reason'] = 'the file is empty'
        return out
    h = fx.headers[0] if fx.headers else None
    if h:
        out['header'] = {k: h[k] for k in ('wording', 'version', 'url', 'offset', 'rest')}
        out['version'] = h['version']
    head = fx.head
    luraph_head = head in LURAPH_HEADS
    out['prefix'] = prefix_kinds(fx, recovered, luraph_head)
    out['suffix'] = suffix_kinds(fx)
    out['platform'], out['platform_evidence'] = platform_of(fx)
    if fx.damage:
        out['platform'] += '?'
    out['entry'] = fx.tail
    out['method'] = fx.methods[0] if fx.methods else None

    # ---- what stands in the place of Luraph's main statement
    if not luraph_head:
        if fx.damage or fx.open_brackets:
            # a fragment: nothing that can be read as a statement of Luraph's
            off = fx.damage[0][0] if fx.damage else len(text)
            what = fx.damage[0][1] if fx.damage else '%d bracket(s) still open' % fx.open_brackets
            if h:
                out['verdict'] = 'damaged'
                out['reason'] = ('truncated or corrupt text: %s at %s, and no complete Luraph main statement'
                                 % (what, _where(text, off)))
                return out
        out['reason'] = _not_luraph_reason(fx, text, h)
        return out

    pre = fx.post_header_code if h and not fx.header_after_main else (fx.pre_code if not h else fx.pre_header_code)
    genuine = bool(fx.n_lph or 'env_msg' in fx.markers)
    # a chunk taken out of a container carries no payload: the container hands it the data
    # (`return setmetatable({...},{}):M(...)`, `local B=...;return({...}):M()`)
    chunk_obj = head in ('setmt', 'table') and fx.tail == ':M(...)'
    chunk_table = head == 'table' and fx.tail == ':M()' and bool(_LOCAL_VARARG.search(pre or ''))
    if (chunk_obj or chunk_table) and not genuine:
        genuine = True
        out['bare_chunk'] = True

    # ---- container or VM
    is_container = 'env_msg' in fx.markers or (
        head == 'fn0' and fx.n_lph > 0 and bool(fx.globals & {'loadstring', 'load'}))
    # a function that loads something and has neither Luraph's payload literal nor its loader
    # message: some other loader (hand-made, repacked).  Not one of Luraph's containers, but
    # what it loads can be looked at.
    other_loader = (not genuine and head in ('fn0', 'fn-args')
                    and bool(fx.globals & {'loadstring', 'load'}))
    if is_container:
        codec = 'setmt' if head == 'setmt' else ('zstd' if 'zstd_call' in fx.markers else 'lzma')
        out['wrapper'] = 'container:%s-%s-%d' % (head, codec, fx.n_lph)
        out['layout'] = 'unknown'
    elif other_loader:
        genuine = True
        is_container = True
        out['wrapper'] = 'container:other'
        out['layout'] = 'unknown'
        out['notes'].append('a loader function without Luraph\'s payload literal or loader message')
    elif head == 'fn-args':
        out['layout'] = 'fn-args-vararg' if fx.head_info.get('vararg_param') else 'fn-args'
    elif head in ('table', 'setmt'):
        out['layout'] = head
    else:
        out['layout'] = 'none'          # fn0 without the loader: a stub

    # ---- whole?
    if not fx.closed:
        off = fx.damage[0][0] if fx.damage else len(text)
        what = fx.damage[0][1] if fx.damage else 'the text ends'
        out['verdict'] = 'damaged'
        out['reason'] = ('truncated or broken: the main statement is not closed (%s at %s; %d bracket(s) still open)'
                         % (what, _where(text, off), fx.open_brackets))
        return out

    # ---- Luraph, or something in its shape
    if not genuine and not fx.damage and not fx.open_brackets:
        if head == 'fn0':
            out['reason'] = ('not Luraph: a function in the container shape `return(function() ... end)` without '
                             'payload and without the loader (a stub, or another obfuscator)')
        else:
            out['reason'] = ('not Luraph: an imitation of the %s shape without any payload (no "LPH" literal, '
                             'no data argument)' % head)
        if h and h['url'] not in (None, GENUINE_URL):
            out['reason'] += '; the header names [%s], which is not Luraph\'s URL' % h['url']
        out['layout'] = 'none'
        out['wrapper'] = 'none'
        return out
    if head in ('table', 'setmt') and fx.methods and fx.table_keys is not None \
            and not out['bare_chunk'] and fx.methods[0] not in fx.table_keys:
        out['reason'] = ('not a working Luraph file: the entry method :%s() is not a field of the object '
                         '(an imitation, or a VM object that was edited)' % fx.methods[0])
        out['layout'] = 'none'
        out['wrapper'] = 'none'
        return out
    if not is_container and not out['bare_chunk'] and head in ('table', 'setmt', 'fn-args') and not fx.tail \
            and not fx.trailing_code:
        out['verdict'] = 'damaged'
        out['reason'] = ('incomplete: the VM object is whole but the call that starts it is missing '
                         '(the main statement ends right behind the object)')
        return out

    # ---- a build for another Lua
    odd = not_luau_syntax(fx)
    if odd and not fx.damage:
        target = {'lua5.3+': 'Lua 5.3/5.4', 'luajit': 'LuaJIT', 'lua5.2+/luajit': 'Lua 5.2+/LuaJIT'}.get(
            out['platform'], 'another Lua')
        out['verdict'] = 'unsupported-target'
        out['reason'] = ('built for %s, not for Luau/Roblox: the text uses %s, which Luau cannot read'
                         % (target, ', '.join(odd)))
        return out

    out['verdict'] = 'luraph'
    if fx.damage:
        out['notes'].append('token damage: %s at %s' % (fx.damage[0][1], _where(text, fx.damage[0][0])))
    return out


def _not_luraph_reason(fx, text, h):
    head = fx.head
    behind = ' behind a Luraph header comment' if h else ''
    if head is None:
        if fx.tokens == 0:
            return 'no code' + (': only the header comment' if h else '')
        a = fx.comments[0][0] + len(fx.comments[0][1]) if (h and fx.comments) else 0
        start = _clip(_shorten(text[a:a + 4000]), 60)
        return ('not Luraph: no main `return` statement%s (a plain script or another obfuscator; '
                'the code starts `%s`)' % (behind, start))
    return ('not Luraph: the main statement is `return %s...`, none of Luraph\'s shapes%s'
            % ({'paren-other': '(', 'setmt-other': 'setmetatable(', 'bare-table': '{',
                'fn-unclosed-params': '(function('}.get(head, head.split(':', 1)[-1] + ' '), behind))
