r"""Open Luraph's compressed container without running it.

    from devm import container_static
    res = container_static.decode(text)       # None: `text` is not such a container
    res.chunk        bytes   the Lua source the container hands to loadstring
    res.chunk_name   str     the chunk name it passes: "Luraph" plus 0-4 spaces
    res.data         bytes | None   the second payload (the VM's data), None for one-payload containers
    res.data_kind    "buffer" | "string" | None   how the container hands the data to the chunk
    res.codec        "lzma" | "zstd"
    res.notes        list of str    anything unusual that was seen

    python3 -m devm.container_static FILE [OUT_PREFIX]      (writes OUT_PREFIX.chunk.lua / .data.bin)

What it reads (Luraph v14.0.3 ... v14.9, one v15.0 file; 1,706 files of the public corpus):

    -- This file was protected using Luraph Obfuscator vX [https://lura.ph/]
    return(function()
      local sub,byte,gsub,pack,loadstring,... = string.sub,...          -- order shuffled per file
      local OFF = 5  ...  three loadstring probes; a hooked loadstring changes OFF and so garbles the output
      DEC = function(s) s = sub(s,OFF); s = gsub(s,"z","!!!!!")
                        return gsub(s,".....",<five base85 digits -> pack(">I4" or "<I4", value)>) end
      SRC  = INFLATE(DEC([=[LPH%...]=]))
      DATA = INFLATE(DEC([==[LPH/...]==]))                              -- only in two-payload containers
      ok, f = pcall(loadstring, SRC, "Luraph" .. rep(" ", K))
      return f                                 -- one payload: the caller applies (...)
      return f(DATA) | f(buffer.fromstring(DATA))                       -- two payloads
    end)()(...)

INFLATE is either Luraph's own decoder, written out in the container: a plain LZMA1 stream (lc=3, lp=0,
pb=0, end marker), which Python's lzma module reads as a raw stream; or, in part of v14.7, Roblox:
`EncodingService:DecompressBuffer(buffer.fromstring(string.sub(x,1,LEN)), Enum.CompressionAlgorithm.Zstd)`,
one Zstandard frame per payload (devm/zstd.py).

Nothing of the input is executed; the text is only lexed.  A container that cannot be opened because it
is damaged (text cut off, payload that does not decode) raises ContainerError; when the first payload
could still be read, the exception carries it as `.partial` (a Result whose `data` is None).

Not handled here (decode() returns None, the sandbox route opens them): the v15 containers, which are
`return setmetatable({...},{}):M()(...)` objects with a state-machine decoder.
"""
import lzma
import re
import struct
import sys

__all__ = ['decode', 'Result', 'ContainerError']

MAX_OUTPUT = 1 << 27            # per payload; also the LZMA dictionary size (Luraph's decoder has no window limit)
_HEADER = re.compile(r'--[^\n]*?Luraph Obfuscator v[0-9][0-9.]*(?: *\[[^\]\n]*\])?')


class ContainerError(Exception):
    """The text is a compressed container, but a damaged one.  `.partial`: a Result with what could be read, or None."""

    def __init__(self, message, partial=None):
        Exception.__init__(self, message)
        self.partial = partial


class Result:
    __slots__ = ('chunk', 'chunk_name', 'data', 'data_kind', 'codec', 'notes', 'byte_order', 'payloads')

    def __init__(self, chunk, chunk_name, data, data_kind, codec, notes, byte_order, payloads):
        self.chunk = chunk
        self.chunk_name = chunk_name
        self.data = data
        self.data_kind = data_kind
        self.codec = codec
        self.notes = notes
        self.byte_order = byte_order            # '<' or '>': how the container packs each base85 group
        self.payloads = payloads                # number of payload strings in the text

    def __repr__(self):
        return '<container %s: chunk %d bytes, name %r, data %s%s>' % (
            self.codec, len(self.chunk), self.chunk_name,
            'none' if self.data is None else '%s of %d bytes' % (self.data_kind, len(self.data)),
            ', %d note(s)' % len(self.notes) if self.notes else '')


# ---------------------------------------------------------------------------------------------- lexer

_TOKEN = re.compile(r'''
    (?P<ws>\s+)
  | (?P<lcom>--\[(?P<ceq>=*)\[)
  | (?P<com>--[^\n]*)
  | (?P<lstr>\[(?P<seq>=*)\[)
  | (?P<quote>["'`])
  | (?P<num>0[xX][0-9a-fA-F_]+(?:\.[0-9a-fA-F_]*)?(?:[pP][+-]?[0-9_]+)?
          | 0[bB][01_]+
          | (?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9][0-9_]*)(?:[eE][+-]?[0-9_]+)?)
  | (?P<id>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>\.\.\.|\.\.|==|~=|<=|>=|<<|>>|//|::|[-+*/%^\#&|~<>=(){}\[\];:,.])
  | (?P<bad>.)
''', re.X | re.S)
_STR_STOP = {'"': re.compile(r'["\\\n]'), "'": re.compile(r"['\\\n]"), '`': re.compile(r'[`\\]')}
_SPACE = re.compile(r'\s*')
_ESCAPE = re.compile(r'\\(x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]+\}|z\s*|[0-9]{1,3}|\r\n|[\s\S])')
_SIMPLE = {'a': '\a', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t', 'v': '\v', '\r\n': '\n'}


def _unescape(body):
    """Value of a quoted Lua string body, one str character per byte."""
    if '\\' not in body:
        return body

    def one(m):
        e = m.group(1)
        if e in _SIMPLE:
            return _SIMPLE[e]
        c = e[0]
        if c == 'x' and len(e) == 3:
            return chr(int(e[1:], 16))
        if c == 'u' and len(e) > 3:
            v = int(e[2:-1], 16)
            return chr(v).encode('utf-8', 'surrogatepass').decode('latin-1') if v < 0x110000 else ''
        if c == 'z':
            return ''
        if c.isdigit():
            return chr(int(e) & 255)
        return e[0]                             # \\ \" \' \<newline>; Luau and Lua 5.1: backslash + any other character
    return _ESCAPE.sub(one, body)


def _lex(text, start=0):
    """Tokens (kind, value, offset) of `text`: kind is 'id', 'num', 'op', 'str' (value = the string's value),
    'cut' (a string or comment the text ends in; value = what is there of it) or 'bad'."""
    out = []
    pos = start
    n = len(text)
    match = _TOKEN.match
    while pos < n:
        m = match(text, pos)
        kind = m.lastgroup
        if kind == 'ws' or kind == 'com':
            pos = m.end()
        elif kind == 'lstr' or kind == 'lcom':
            close = ']' + m.group('seq' if kind == 'lstr' else 'ceq') + ']'
            end = text.find(close, m.end())
            if end < 0:
                if kind == 'lstr':
                    out.append(('cut', text[m.end():], pos))
                break
            if kind == 'lstr':
                first = m.end()
                if text.startswith('\r\n', first):
                    first += 2
                elif text.startswith('\n', first):
                    first += 1
                out.append(('str', text[first:end], pos))
            pos = end + len(close)
        elif kind == 'quote':
            q = m.group()
            stop = _STR_STOP[q]
            i = m.end()
            while True:
                s = stop.search(text, i)
                if s is None:
                    out.append(('cut', text[m.end():], pos))
                    return out
                c = s.group()
                if c == q:
                    out.append(('str', _unescape(text[m.end():s.start()]), pos))
                    pos = s.end()
                    break
                if c == '\n':                   # a quoted string cannot hold a raw line break: the text is broken here
                    out.append(('cut', text[m.end():s.start()], pos))
                    pos = s.end()
                    break
                i = s.end() + 1                 # backslash: skip the character it escapes
                if text.startswith('z', s.end()):
                    i = _SPACE.match(text, i).end()
                elif text.startswith('\r\n', s.end()):
                    i += 1
        else:
            out.append((kind, m.group(), pos))
            pos = m.end()
    return out


def _number(tok):
    """Value of a Lua / Luau number literal, or None."""
    t = tok.replace('_', '')
    try:
        if t[:2] in ('0x', '0X'):
            return float.fromhex(t) if ('.' in t or 'p' in t or 'P' in t) else int(t, 16)
        if t[:2] in ('0b', '0B'):
            return int(t[2:], 2)
        v = float(t)
        return int(v) if v == int(v) else v
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------- the pieces

_BLOCK_OPEN = frozenset(('function', 'do', 'if', 'repeat'))
_BLOCK_CLOSE = frozenset(('end', 'until'))
_HEAD = (('op', '('), ('id', 'function'), ('op', '('), ('op', ')'))
_TAIL = (('id', 'end'), ('op', ')'), ('op', '('), ('op', ')'), ('op', '('), ('op', '...'), ('op', ')'))


def _is(tokens, i, pattern):
    if i < 0 or i + len(pattern) > len(tokens):
        return False
    for j, (kind, value) in enumerate(pattern):
        t = tokens[i + j]
        if t[0] != kind or (value is not None and t[1] != value):
            return False
    return True


def _main_statement(tokens):
    """Index of the token after `return(function()` when that is the first top-level `return`; else None."""
    depth = block = 0
    for i, (kind, value, _pos) in enumerate(tokens):
        if kind == 'op':
            if value in '([{':
                depth += 1
            elif value in ')]}':
                depth -= 1
        elif kind == 'id':
            if value in _BLOCK_OPEN:
                block += 1
            elif value in _BLOCK_CLOSE:
                block -= 1
            elif value == 'return' and depth <= 0 and block <= 0:
                return i + 1 + len(_HEAD) if _is(tokens, i + 1, _HEAD) else None
    return None


_POW = (85 ** 4, 85 ** 3, 85 ** 2, 85, 1)
_DIGIT = [[(c - 33) * p for c in range(256)] for p in _POW]


def _base85(body, order, wrap):
    """Luraph's text decoder on `body` (the payload without its first four characters).

    -> (bytes, note or None).  Every 5 characters are one 32-bit value written in `order`; what is left
    after the last group of five stays as it is, as in the container (gsub leaves it alone)."""
    body = body.replace('z', '!!!!!')
    try:
        raw = body.encode('latin-1')
    except UnicodeEncodeError:
        raise ContainerError('the payload holds characters outside the byte range') from None
    n = len(raw) // 5
    d0, d1, d2, d3, d4 = _DIGIT
    end = 5 * n
    vals = [d0[a] + d1[b] + d2[c] + d3[d] + d4[e] for a, b, c, d, e in
            zip(raw[0:end:5], raw[1:end:5], raw[2:end:5], raw[3:end:5], raw[4:end:5])]
    try:
        out = struct.pack('%s%dI' % (order, n), *vals)
    except struct.error:
        if not wrap:
            bad = next(i for i, v in enumerate(vals) if not 0 <= v <= 0xFFFFFFFF)
            raise ContainerError('base85 group %d of %d (%r) is not a 32-bit value: the payload text is corrupt'
                                 % (bad + 1, n, body[5 * bad:5 * bad + 5])) from None
        out = struct.pack('%s%dI' % (order, n), *[v & 0xFFFFFFFF for v in vals])
    rest = raw[end:]
    return out + rest, ('%d character(s) after the last base85 group' % len(rest)) if rest else None


def _lzma(raw):
    """Luraph's own decoder = raw LZMA1, lc=3 lp=0 pb=0, ended by the end marker."""
    if len(raw) < 5:
        raise ContainerError('the payload is %d bytes: too short for an LZMA stream' % len(raw))
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[
        {'id': lzma.FILTER_LZMA1, 'lc': 3, 'lp': 0, 'pb': 0, 'dict_size': MAX_OUTPUT}])
    try:
        out = dec.decompress(raw, MAX_OUTPUT + 1)
    except lzma.LZMAError as e:
        raise ContainerError('the payload is not an LZMA stream of the kind Luraph writes (%s)' % (e or 'corrupt data')) from None
    if len(out) > MAX_OUTPUT:
        raise ContainerError('the payload expands to more than %d bytes' % MAX_OUTPUT)
    if not dec.eof:
        raise ContainerError('the LZMA stream ends after %d bytes of output without its end marker '
                             '(payload cut off or corrupt)' % len(out))
    return out, len(dec.unused_data)


def _zstd(raw):
    from . import zstd
    try:
        return zstd.decompress(raw, MAX_OUTPUT)
    except zstd.ZstdError as e:
        raise ContainerError('the payload is not a Zstandard frame (%s)' % e) from None


def _find_all(tokens, start, pattern):
    return [i for i in range(start, len(tokens) - len(pattern) + 1) if _is(tokens, i, pattern)]


def _chunk_name(tokens, start, notes):
    for i in range(start, len(tokens)):
        if tokens[i][0] == 'str' and tokens[i][1] == 'Luraph':
            # "Luraph" .. rep(" ", K)
            if _is(tokens, i + 1, (('op', '..'), ('id', None), ('op', '('), ('str', ' '), ('op', ','), ('num', None), ('op', ')'))):
                k = _number(tokens[i + 6][1])
                if isinstance(k, int) and 0 <= k <= 64:
                    return 'Luraph' + ' ' * k
                notes.append('chunk name: cannot read the repeat count %r' % tokens[i + 6][1])
            elif _is(tokens, i + 1, (('op', '..'),)):
                notes.append('chunk name: "Luraph" is followed by an expression this decoder does not know')
            return 'Luraph'
    notes.append('no "Luraph" chunk name in the text; "Luraph" assumed')
    return 'Luraph'


def decode(text):
    """Open the compressed container in `text` (str; bytes are read as Latin-1).

    None when the text is not a `return(function()...end)()(...)` container with `LPH` payload strings;
    a Result when it is; ContainerError when it is one but damaged."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode('latin-1')
    notes = []
    tokens = _lex(text)
    body = _main_statement(tokens)
    if body is None and not any(t[0] != 'cut' for t in tokens):
        # the line break after the header comment was lost: the whole program sits in the comment
        m = _HEADER.match(text.lstrip('\ufeff \t\r\n'))
        if m and 'return' in text[m.end():m.end() + 64]:
            off = len(text) - len(text.lstrip('\ufeff \t\r\n')) + m.end()
            tokens = _lex(text, off)
            body = _main_statement(tokens)
            if body is not None:
                notes.append('the program follows the header comment on the same line (line break lost); read from there')
    if body is None:
        return None
    # the statement ends where the parenthesis opened after `return` closes: return ( function() ... end ) () (...)
    close = None
    depth = 0
    for i in range(body - len(_HEAD), len(tokens)):
        if tokens[i][0] == 'op':
            if tokens[i][1] == '(':
                depth += 1
            elif tokens[i][1] == ')':
                depth -= 1
                if depth == 0:
                    close = i
                    break
    stop = close if close is not None else len(tokens)
    inner = tokens[body:stop]
    # payload strings: literals "LPH" + one character + base85 text, in source order
    pay = [i for i in range(body, stop) if tokens[i][0] in ('str', 'cut') and tokens[i][1].startswith('LPH')
           and (len(tokens[i][1]) >= 64 or tokens[i][0] == 'cut')]
    if not pay:
        return None
    names = set(t[1] for t in inner if t[0] == 'id')
    short = set(t[1] for t in inner if t[0] == 'str' and len(t[1]) < 64)
    codec = 'zstd' if ('DecompressBuffer' in names or 'DecompressBuffer' in short) else 'lzma'
    if '<I4' in short and '>I4' not in short:
        order = '<'
    elif '>I4' in short and '<I4' not in short:
        order = '>'
    else:
        order = None                            # the four bytes are computed without string.pack: found by trial
    wrap = order is None
    sniffed = False
    complete = close is not None and _is(tokens, close - 1, _TAIL)
    name = _chunk_name(tokens[:stop], pay[-1] + 1, notes)
    if complete:
        extra = tokens[close - 1 + len(_TAIL):]
        if extra and extra[0][1] == ';':
            extra = extra[1:]
        if extra:
            notes.append('text after the end of the container (from offset %d) ignored' % extra[0][2])
    elif close is not None:
        notes.append('the container does not end in `end)()(...)`')
    if len(pay) > 2:
        notes.append('%d payload strings; the first two are used' % len(pay))

    # lengths the Zstd variant cuts the decoded text to: X = string.sub(X, 1, LEN)
    cuts = []
    if codec == 'zstd':
        for i in _find_all(inner, 0, (('op', '('), ('id', None), ('op', ','), ('num', '1'), ('op', ','), ('num', None), ('op', ')'))):
            v = _number(inner[i + 5][1])
            if isinstance(v, int) and v > 0:
                cuts.append(v)

    def inflate(index):
        tok = tokens[pay[index]]
        text85 = tok[1][4:]
        ordinal = ('first', 'second')[index]
        nonlocal order, codec, sniffed
        if order is None:
            found = None
            for cand in ('>', '<'):
                try:
                    raw, _note = _base85(text85, cand, True)
                    if codec == 'zstd':
                        if raw[:4] != b'\x28\xb5\x2f\xfd':
                            continue
                    else:
                        _lzma(raw)
                except ContainerError:
                    continue
                found = cand
                break
            if found is None:
                raise ContainerError('the %s payload decodes in neither byte order (the text names no string.pack '
                                     'format; this is not Luraph\'s own compression, or the payload is corrupt)' % ordinal)
            order = found
            notes.append('no string.pack format in the text: byte order %r found by trial' % order)
        try:
            raw, note = _base85(text85, order, wrap)
            if note:
                notes.append('%s payload: %s' % (ordinal, note))
            if not complete and codec == 'lzma' and raw[:4] == b'\x28\xb5\x2f\xfd':
                # the text stops before the code that inflates the payloads; an LZMA stream starts with a zero byte
                codec = 'zstd'
                sniffed = True
                notes.append('the payload starts with the Zstandard magic number: read as a Zstd container')
            if codec == 'lzma':
                out, unused = _lzma(raw)
                if unused > 3:
                    notes.append('%s payload: %d bytes after the LZMA end marker' % (ordinal, unused))
                return out
            fit = [c for c in cuts if 0 <= len(raw) - c < 4]
            if fit:
                return _zstd(raw[:fit[0]])
            if not sniffed:
                notes.append('%s payload: no string.sub(x, 1, LEN) cut fits its %d bytes; frame end found by trial' % (ordinal, len(raw)))
            err = None
            for drop in range(4):
                try:
                    return _zstd(raw[:len(raw) - drop])
                except ContainerError as e:
                    err = err or e
            raise err
        except ContainerError as e:
            raise ContainerError('%s payload: %s' % (ordinal, e), e.partial) from None

    def broken(index):
        i = pay[index]
        where = 'the text ends inside' if i == len(tokens) - 1 else 'a line break cuts'
        return '%s the %s payload string (%d characters of it are there): the file is %s' % (
            where, ('first', 'second')[index], len(tokens[i][1]), 'cut off' if i == len(tokens) - 1 else 'damaged')

    if tokens[pay[0]][0] != 'str':
        raise ContainerError(broken(0))
    chunk = inflate(0)
    two = len(pay) >= 2
    data = kind = None
    partial = None
    if two:
        if codec == 'zstd':
            kind = 'string' if len(_find_all(inner, 0, (('id', 'buffer'), ('op', '.'), ('id', 'tostring')))) >= 2 else 'buffer'
        else:
            kind = 'buffer' if _find_all(inner, 0, (('id', 'buffer'), ('op', '.'), ('id', 'fromstring'))) else 'string'
        if tokens[pay[1]][0] != 'str':
            partial = Result(chunk, name, None, None, codec, notes + ['the data payload is incomplete'], order, len(pay))
            raise ContainerError(broken(1) + '; the chunk was read, its data is missing', partial)
        try:
            data = inflate(1)
        except ContainerError as e:
            partial = Result(chunk, name, None, None, codec, notes + ['the data payload does not decode'], order, len(pay))
            raise ContainerError(str(e), partial) from None
    res = Result(chunk, name, data, kind, codec, notes, order, len(pay))
    if not complete:
        res.notes = notes + ['the text of the container is incomplete']
        raise ContainerError('the text ends before the end of the container (no `end)()(...)` after the payload%s): '
                             'the file is cut off; %s' % ('s' if two else '', 'both payloads were read' if two else
                                                           'the payload was read, a second one may be missing'), res)
    return res


def main(argv):
    if not 1 <= len(argv) <= 2:
        sys.stderr.write('usage: python3 -m devm.container_static FILE [OUT_PREFIX]\n')
        return 2
    with open(argv[0], 'rb') as f:
        text = f.read().decode('latin-1')
    try:
        res = decode(text)
    except ContainerError as e:
        print('damaged container: %s' % e)
        res = e.partial
        status = 8
    else:
        status = 0
        if res is None:
            print('not a compressed Luraph container')
            return 2
    if res is not None:
        print(repr(res))
        for note in res.notes:
            print('  note: ' + note)
        if len(argv) == 2:
            with open(argv[1] + '.chunk.lua', 'wb') as f:
                f.write(res.chunk)
            if res.data is not None:
                with open(argv[1] + '.data.bin', 'wb') as f:
                    f.write(res.data)
    return status


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
