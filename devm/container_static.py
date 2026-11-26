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

What it reads.  Luraph v14.0.3 ... v14.9 and one v15.0 file (1,706 files of the public corpus):

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

The v15 container (7 corpus files) is an object instead of a function:

    return setmetatable({ P=[==[LPH...]==], Q=[==[LPH...]==], W={["!"]="PveHq",...}, N="Luraph",
                          ...methods: a state machine and the LZMA decoder... },{}):M()(...)

Same idea, three per-file twists in the text decoder (ten spare characters stand for whole groups, the 85
digits start at a per-file character, the five digits of a group are stored in a per-file order); the LZMA
streams are the same, their lengths are literals in the text; see _decode_setmt.  Always two payloads, the
data handed over as a buffer.
"""
import lzma
import re
import struct
import sys

__all__ = ['decode', 'Result', 'ContainerError']

MAX_OUTPUT = 1 << 27            # per payload; also the LZMA dictionary size (Luraph's decoder has no window limit)
_HEADER = re.compile(r'^[ \t\ufeff]*--[^\n]*?Luraph Obfuscator v[0-9][0-9.]*(?: *\[[^\]\n]*\])?', re.M)


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


def _first_return(tokens):
    """Index of the first `return` outside every bracket and block (the main statement of a Luraph file), or None."""
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
                return i
    return None


def _main_statement(tokens):
    """Index of the token after `return(function()` when that is the first top-level `return`; else None."""
    i = _first_return(tokens)
    if i is None or not _is(tokens, i + 1, _HEAD):
        return None
    return i + 1 + len(_HEAD)


def _closing(tokens, start):
    """Index of the `)` that closes the `(` at tokens[start], or None when the text ends first."""
    depth = 0
    for i in range(start, len(tokens)):
        if tokens[i][0] == 'op':
            if tokens[i][1] == '(':
                depth += 1
            elif tokens[i][1] == ')':
                depth -= 1
                if depth == 0:
                    return i
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


# ---------------------------------------------------------------------------------------------- v15: setmetatable object

_SETMT_HEAD = (('id', 'setmetatable'), ('op', '('), ('op', '{'))
_PERMUTATIONS = None


def _substitution_table(tokens, start, stop):
    """{character: five characters}: the table constructor whose entries all are  c = "xxxxx"  /  ["c"] = "xxxxx"."""
    for i in range(start, stop):
        if not (tokens[i][0] == 'op' and tokens[i][1] == '{'):
            continue
        table = {}
        j = i + 1
        while j < stop:
            if _is(tokens, j, (('op', '['), ('str', None), ('op', ']'), ('op', '='), ('str', None))):
                key, value, j = tokens[j + 1][1], tokens[j + 4][1], j + 5
            elif _is(tokens, j, (('id', None), ('op', '='), ('str', None))):
                key, value, j = tokens[j][1], tokens[j + 2][1], j + 3
            else:
                break
            if len(key) != 1 or len(value) != 5 or key in table:
                table = None
                break
            table[key] = value
            if j < stop and tokens[j][0] == 'op' and tokens[j][1] in ',;':
                j += 1
        if table and j < stop and tokens[j][0] == 'op' and tokens[j][1] == '}':
            return table
    return None


def _groups_to_bytes(raw, offset, perm, count=None):
    """bytes of the first `count` five-character groups (all when None): character k of a group is the base-85
    digit of weight 85^perm[k], digits count from `offset`, the value is written little-endian.
    None when a group is not a 32-bit value."""
    n = len(raw) // 5 if count is None else min(count, len(raw) // 5)
    end = 5 * n
    tabs = [[(c - offset) * 85 ** e for c in range(256)] for e in perm]
    d0, d1, d2, d3, d4 = tabs
    vals = [d0[a] + d1[b] + d2[c] + d3[d] + d4[e] for a, b, c, d, e in
            zip(raw[0:end:5], raw[1:end:5], raw[2:end:5], raw[3:end:5], raw[4:end:5])]
    try:
        return struct.pack('<%dI' % n, *vals)
    except struct.error:
        return None


def _looks_like_lua(chunk):
    head = chunk[:256]
    return bool(head) and all(32 <= b < 127 or b in (9, 10, 13) for b in head) and (b'return' in head or b'local' in head)


def _decode_setmt(tokens, ret, notes):
    """The v15 container:  return setmetatable({ ...methods..., P=[==[LPH...]==], Q=[==[LPH...]==],
    W={["!"]="PveHq",...}, N="Luraph", ... },{}):M()(...)

    Its payload text is base 85 as in v14, with three per-file twists the object's state machine implements:
    ten characters outside the 85-character alphabet stand for whole groups (the table W), the alphabet starts
    at a per-file character, and the five digits of a group are stored in a per-file order.  The LZMA streams
    are inflated to lengths that are literals in the call that does it (chunk first, then data).
    The digit order is found by trial: only the right one makes every group a 32-bit value and the bytes an
    LZMA stream.  Which payload is the chunk is decided by the two lengths."""
    global _PERMUTATIONS
    open_paren = ret + 2
    close = _closing(tokens, open_paren)
    stop = close if close is not None else len(tokens)
    pay = [i for i in range(open_paren, stop) if tokens[i][0] in ('str', 'cut') and tokens[i][1].startswith('LPH')
           and (len(tokens[i][1]) >= 64 or tokens[i][0] == 'cut')]
    table = _substitution_table(tokens, open_paren, stop)
    if table is None or not pay:
        return None                             # a plain v15 VM: one payload, no decompressor, no such table
    for i in pay[:2]:
        if tokens[i][0] != 'str':
            raise ContainerError('the text ends inside a payload string (%d characters of it are there): the file is cut off'
                                 % len(tokens[i][1]))
    if len(pay) < 2:
        if close is None:
            raise ContainerError('the text ends before the second payload string: the file is cut off')
        return None
    if len(pay) > 2:
        notes.append('%d payload strings; the first two are used' % len(pay))
    complete = close is not None and _is(tokens, close + 1, (('op', ':'), ('id', None), ('op', '('), ('op', ')'),
                                                               ('op', '('), ('op', '...'), ('op', ')')))
    alphabet = [c for c in range(32, 127) if chr(c) not in table]
    if len(alphabet) != 85 or alphabet[-1] - alphabet[0] != 84:
        raise ContainerError('the substitution table of the container leaves no run of 85 characters for the digits '
                             '(an unknown variant of the v15 container)')
    offset = alphabet[0]
    trans = {ord(k): v for k, v in table.items()}
    raws = []
    for n, i in enumerate(pay[:2]):
        try:
            raw = tokens[i][1][4:].translate(trans).encode('latin-1')
        except UnicodeEncodeError:
            raise ContainerError('a payload holds characters outside the byte range') from None
        if len(raw) % 5:
            notes.append('payload %d: %d character(s) after the last group of five' % (n + 1, len(raw) % 5))
        raws.append(raw)
    # the two lengths: ID(ID, ID, N1), ID(ID, ID, N2)
    sizes = None
    call = (('id', None), ('op', '('), ('id', None), ('op', ','), ('id', None), ('op', ','), ('num', None), ('op', ')'))
    for i in _find_all(tokens[:stop], open_paren, call + (('op', ','),) + call):
        a, b = _number(tokens[i + 6][1]), _number(tokens[i + 15][1])
        if isinstance(a, int) and isinstance(b, int) and 0 < a <= MAX_OUTPUT and 0 < b <= MAX_OUTPUT:
            sizes = (a, b)
            break
    if sizes is None and not complete:
        notes.append('the text is cut off before the code that names the lengths of the payloads')
    elif sizes is None:
        notes.append('the lengths of the two payloads are not where this decoder looks for them; the chunk is '
                     'told from its data by its content')
    # digit order: every group must be a 32-bit value, and the bytes must start an LZMA stream
    if _PERMUTATIONS is None:
        import itertools
        _PERMUTATIONS = list(itertools.permutations(range(5)))
    fits = []
    for raw in raws:
        good = set()
        for perm in _PERMUTATIONS:
            head = _groups_to_bytes(raw, offset, perm, 3000)
            if head is None or head[:1] != b'\0':
                continue
            try:
                dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[
                    {'id': lzma.FILTER_LZMA1, 'lc': 3, 'lp': 0, 'pb': 0, 'dict_size': 1 << 16}])
                if dec.decompress(head, 4096):
                    good.add(perm)
            except lzma.LZMAError:
                pass
        fits.append(good)
    both = fits[0] & fits[1]
    if len(both) == 1:
        found = next(iter(both))
    elif len(both) > 1:
        raise ContainerError('the order of the base-85 digits cannot be told (%d orders fit)' % len(both))
    elif len(fits[0]) + len(fits[1]) == 1:
        found = next(iter(fits[0] | fits[1]))   # one payload is damaged from its start on; the other still tells the order
    else:
        raise ContainerError('the payloads decode in no order of the base-85 digits: corrupt payload text, or an '
                             'unknown variant of the v15 container')
    data85 = []
    for n, raw in enumerate(raws):
        data85.append(_groups_to_bytes(raw, offset, found))
    names = [t[1] for t in tokens[open_paren:stop] if t[0] == 'str' and t[1].startswith('Luraph') and not t[1][6:].strip(' ')]
    if names:
        name = names[0]
    else:
        name = 'Luraph'
        notes.append('no "Luraph" chunk name in the text; "Luraph" assumed')
    kind = 'buffer' if len(_find_all(tokens[:stop], open_paren, (('id', 'buffer'), ('op', '.'), ('id', 'tostring')))) < 2 else 'string'
    notes.append('v15 container (setmetatable object): digits start at %r, digit order %s found by trial'
                 % (chr(offset), ''.join(str(e) for e in found)))
    # inflate (the streams end in an end marker although the container's decoder stops at the known length), then
    # tell the chunk from its data: by the two lengths, or by its content when the lengths are not known
    outs, errors = [], []
    for n in (0, 1):
        try:
            if data85[n] is None:
                raise ContainerError('a base85 group is not a 32-bit value: the payload text is corrupt')
            out, unused = _lzma(data85[n])
            if unused > 3:
                notes.append('payload %d: %d bytes after the LZMA end marker' % (n + 1, unused))
            outs.append(out)
        except ContainerError as e:
            outs.append(None)
            errors.append('payload %d: %s' % (n + 1, e))
    which = None
    if sizes is not None:
        for n in (0, 1):
            if outs[n] is not None and len(outs[n]) == sizes[0] and (outs[1 - n] is None or len(outs[1 - n]) == sizes[1]):
                which = n
                break
        if which is None and not errors:
            raise ContainerError('the payloads inflate to %d and %d bytes, the container expects %d (chunk) and %d (data)'
                                 % (len(outs[0]), len(outs[1]), sizes[0], sizes[1]))
    else:
        texts = [n for n in (0, 1) if outs[n] is not None and _looks_like_lua(outs[n])]
        if len(texts) == 1:
            which = texts[0]
        elif not errors:
            raise ContainerError('cannot tell the chunk from its data (%d of the two payloads inflate to text)' % len(texts))
    if which is None:
        raise ContainerError('; '.join(errors))
    chunk, data = outs[which], outs[1 - which]
    if data is None:
        raise ContainerError('data payload: %s' % errors[0].split(': ', 1)[1], Result(chunk, name, None, None, 'lzma', notes + [
            'the data payload does not decode'], '<', len(pay)))
    res = Result(chunk, name, data, kind, 'lzma', notes, '<', len(pay))
    if not complete:
        res.notes = notes + ['the text of the container is incomplete']
        raise ContainerError('the text ends before the end of the container (no `):M()(...)` after the object): '
                             'the file is cut off; both payloads were read', res)
    if close is not None:
        extra = tokens[close + 8:]
        if extra and extra[0][1] == ';':
            extra = extra[1:]
        if extra:
            notes.append('text after the end of the container (from offset %d) ignored' % extra[0][2])
    return res


def decode(text):
    """Open the compressed container in `text` (str; bytes are read as Latin-1).

    None when the text is not a compressed container (v14: `return(function()...end)()(...)` with `LPH` payload
    strings; v15: `return setmetatable({...},{}):M()(...)` with two of them and a decompressor);
    a Result when it is; ContainerError when it is one but damaged."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode('latin-1')
    notes = []
    tokens = _lex(text)
    body = _main_statement(tokens)
    if body is None and not any(t[0] != 'cut' for t in tokens):
        # the line break after the header comment was lost: the whole program sits in the comment
        m = _HEADER.search(text)
        if m and 'return' in text[m.end():m.end() + 64]:
            tokens = _lex(text, m.end())
            body = _main_statement(tokens)
            if body is not None:
                notes.append('the program follows the header comment on the same line (line break lost); read from there')
    if body is None:
        ret = _first_return(tokens)
        if ret is not None and _is(tokens, ret + 1, _SETMT_HEAD):
            return _decode_setmt(tokens, ret, notes)
        return None
    # the statement ends where the parenthesis opened after `return` closes: return ( function() ... end ) () (...)
    close = _closing(tokens, body - len(_HEAD))
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
