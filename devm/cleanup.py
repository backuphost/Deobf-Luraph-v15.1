"""Text-level tidying of decompiled output: drop unused declarations and give variables names
derived from what they are initialised with (services, required modules, instances, properties).

Bytecode carries no local names, so this is cosmetic: every generated name (v12, p3) is unique in
the file, which makes a token-level rename safe.
"""
import math
import re

from . import luparse, opaque, pe

KEYWORDS = {
    'and', 'break', 'do', 'else', 'elseif', 'end', 'false', 'for', 'function', 'if', 'in', 'local', 'nil', 'not',
    'or', 'repeat', 'return', 'then', 'true', 'until', 'while', 'continue', 'type', 'export',
}
RESERVED = KEYWORDS | {
    'game', 'workspace', 'script', 'Instance', 'Enum', 'math', 'string', 'table', 'task', 'coroutine', 'debug',
    'os', 'utf8', 'bit32', 'buffer', 'Vector3', 'Vector2', 'CFrame', 'Color3', 'UDim', 'UDim2', 'Random',
    'self', 'print', 'warn', 'error', 'assert', 'pcall', 'xpcall', 'require', 'select', 'next', 'pairs', 'ipairs',
    'type', 'typeof', 'tostring', 'tonumber', 'unpack', 'getfenv', 'setfenv', 'shared', '_G', 'tick', 'time',
    'wait', 'spawn', 'delay', 'rawget', 'rawset', 'rawequal', 'setmetatable', 'getmetatable', 'newproxy',
}

TOKEN = re.compile(r'''
    (?P<comment>--\[(=*)\[.*?\]\2\]|--[^\n]*)
  | (?P<string>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|\[(=*)\[.*?\]\4\]|`(?:\\.|[^`\\])*`)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<other>.)
''', re.S | re.X)

GEN = re.compile(r'^[vp]\d+$')


def tokens(text):
    out = []
    for m in TOKEN.finditer(text):
        kind = m.lastgroup
        out.append([kind, m.group(0)])
    return out


def camel(name, lower=True):
    parts = re.findall(r'[A-Za-z0-9]+', name)
    if not parts:
        return None
    s = parts[0] + ''.join(p[:1].upper() + p[1:] for p in parts[1:])
    if s[0].isdigit():
        s = '_' + s
    if lower:
        # RemoteEvent -> remoteEvent, GUID -> guid, HTTPService -> httpService
        m = re.match(r'[A-Z]+', s)
        if m:
            n = m.end()
            if n == len(s) or n == 1:
                s = s[:n].lower() + s[n:]
            else:
                s = s[:n - 1].lower() + s[n - 1:]
    return s


STR = r'"((?:\\.|[^"\\])*)"'


def suggest(rhs):
    """Name for a local from its initialiser, or None."""
    m = re.match(r'^game:GetService\(' + STR + r'\)$', rhs)
    if m:
        return camel(m.group(1), lower=False)
    m = re.match(r'^require\((.*)\)$', rhs)
    if m:
        inner = m.group(1)
        m2 = re.search(r'(?:WaitForChild|FindFirstChild)\(' + STR + r'\)$', inner)
        if m2:
            return camel(m2.group(1), lower=False)
        m2 = re.search(r'\.([A-Za-z_][A-Za-z0-9_]*)$', inner)
        if m2:
            return camel(m2.group(1), lower=False)
        return None
    m = re.match(r'^Instance\.new\(' + STR + r'(?:, .*)?\)$', rhs)
    if m:
        return camel(m.group(1))
    m = re.match(r'^.*:(?:WaitForChild|FindFirstChild|FindFirstChildOfClass|FindFirstChildWhichIsA|FindFirstAncestor)\('
                 + STR + r'[^()]*\)$', rhs)
    if m and rhs.count(':') >= 1:
        return camel(m.group(1))
    m = re.match(r'^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\.([A-Za-z_][A-Za-z0-9_]*)$', rhs)
    if m:
        return camel(m.group(1))
    m = re.match(r'^.*:(Get[A-Z][A-Za-z0-9]*|GenerateGUID)\([^()]*\)$', rhs)
    if m:
        name = m.group(1)
        if name.startswith('Get') and len(name) > 3:
            name = name[3:]
        elif name == 'GenerateGUID':
            name = 'guid'
        return camel(name)
    m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*):(Is[A-Z][A-Za-z0-9]*)\(\)$', rhs)
    if m:
        return camel(m.group(2))
    return None


def _double_not(text):
    """not not (a < b)  ->  a < b   (a comparison is a boolean already)"""
    out = []
    i = 0
    key = 'not not ('
    while True:
        j = text.find(key, i)
        if j < 0:
            out.append(text[i:])
            return ''.join(out)
        if j and (text[j - 1].isalnum() or text[j - 1] == '_'):
            out.append(text[i:j + 4])
            i = j + 4
            continue
        k = j + len(key)
        depth = 1
        quote = None
        while k < len(text) and depth:
            c = text[k]
            if quote:
                if c == '\\':
                    k += 1
                elif c == quote:
                    quote = None
            elif c in '"\'':
                quote = c
            elif c in '([{':
                depth += 1
            elif c in ')]}':
                depth -= 1
            elif c == '\n':
                break
            k += 1
        inner = text[j + len(key):k - 1]
        flat = re.sub(r'"(?:\\.|[^"\\])*"|\([^()]*\)', '', inner)
        flat = re.sub(r'\([^()]*\)', '', flat)
        if depth == 0 and re.search(r'[<>]=?|[=~]=', flat) and not re.search(r'\b(and|or|not)\b', flat):
            out.append(text[i:j])
            out.append(inner)
        else:
            out.append(text[i:k])
        i = k


def tidy(text):
    text = _double_not(text)
    # in a condition only truthiness counts
    text = re.sub(r'(?m)^(\t*(?:if|elseif|while) )not not ((?:[A-Za-z_]\w*)(?:\.[A-Za-z_]\w*)*)( (?:then|do))$', r'\1\2\3', text)
    # stores into a table nothing else can see (a VM scratch array that was never bound to a variable)
    text = '\n'.join(ln for ln in text.split('\n') if not re.match(r'^\s*\(\{\}\)\[\d+\] = [-\w.]+$', ln))
    text = _loop_artifacts(text)
    text = opaque.strip(text)
    text = drop_dead_constants(text)
    text = drop_unused(text)
    for _ in range(4):
        new = rename(text)
        if new == text:
            break
        text = new
    from . import renamer       # (imports this module)
    return renamer.humanize(text)


def drop_unused(text):
    while True:
        toks = tokens(text)
        count = {}
        for kind, s in toks:
            if kind == 'ident':
                count[s] = count.get(s, 0) + 1
        lines = text.split('\n')
        keep = []
        changed = False
        for ln in lines:
            m = re.match(r'^\s*local (v\d+)(?: = nil)?$', ln)
            if m and count.get(m.group(1)) == 1:
                changed = True
                continue
            keep.append(ln)
        text = '\n'.join(keep)
        if not changed:
            return text


class _NotConst(Exception):
    pass


# Library functions that have no effect and depend on nothing but their arguments (with their
# Luau behaviour on the arguments _const_value accepts)
_STRING_PURE = ('string.unpack', 'string.packsize', 'string.byte', 'string.len', 'string.sub', 'string.char', 'string.rep')


def _lua_number(v):
    return pe.is_num(v)


def _str_sub(s, i=1, j=-1):
    n = len(s)
    i = int(i)
    j = int(j)
    i = max(n + i + 1, 1) if i < 0 else max(i, 1)
    j = n + j + 1 if j < 0 else min(j, n)
    return s[i - 1:j] if i <= j else ''


def _call_pure(name, args):
    """The results of a pure library call on constants; _NotConst where it would raise in Luau or
    is not one this evaluator knows."""
    if name in pe.PURE:
        if not args or not all(_lua_number(a) and math.isfinite(a) for a in args):
            raise _NotConst()
        if name.startswith('math.') and name not in ('math.max', 'math.min') and len(args) != 1:
            raise _NotConst()
        if name in ('bit32.lshift', 'bit32.rshift', 'bit32.arshift', 'bit32.lrotate', 'bit32.rrotate') and len(args) != 2:
            raise _NotConst()
        if name in ('bit32.extract', 'bit32.replace'):
            # (Luau raises on a field outside 0..31)
            at = 1 if name == 'bit32.extract' else 2
            if not at < len(args) <= at + 2:
                raise _NotConst()
            f, w = args[at], args[at + 1] if len(args) > at + 1 else 1
            if not (0 <= f <= 31 and 1 <= w and f + w <= 32):
                raise _NotConst()
    elif name == 'string.sub':
        if not (1 <= len(args) <= 3 and isinstance(args[0], str) and all(_lua_number(a) and math.isfinite(a) for a in args[1:])):
            raise _NotConst()
        return [_str_sub(*args)]
    elif name == 'string.char':
        if not all(_lua_number(a) and a == int(a) and 0 <= a <= 255 for a in args):
            raise _NotConst()
        return [''.join(chr(int(a)) for a in args)]
    elif name == 'string.rep':
        if len(args) != 2 or not isinstance(args[0], str) or not _lua_number(args[1]) or not math.isfinite(args[1]) \
                or len(args[0]) * max(0, int(args[1])) > 1 << 16:
            raise _NotConst()
        return [args[0] * max(0, int(args[1]))]
    elif not _pure_name(name):
        raise _NotConst()
    try:
        r = pe.fold_pure(name, list(args))
    except Exception:
        raise _NotConst()
    if r is None:
        raise _NotConst()
    return r


def _truthy(v):
    return v is not None and v is not False


def _const_value(e, multi=False):
    """The value of an expression made of literals, arithmetic, comparisons, and/or/not and calls
    of pure library functions on such values; _NotConst for anything else or for what would raise
    at run time.  multi: the list of values (a call can give several)."""
    k = e[0]
    if k == 'call' and e[1][0] == 'index' and e[1][1][0] == 'name' and e[1][2][0] == 'str':
        name = e[1][1][1] + '.' + e[1][2][1]
        if not _pure_name(name):
            raise _NotConst()
        args = []
        for i, a in enumerate(e[2]):
            if i == len(e[2]) - 1:
                args += _const_value(a, multi=True)
            else:
                args.append(_const_value(a))
        r = _call_pure(name, args)
        return r if multi else (r[0] if r else None)
    if k == 'num':
        v = e[1]
    elif k == 'str':
        v = e[1]
    elif k == 'nil':
        v = None
    elif k in ('true', 'false'):
        v = k == 'true'
    elif k == 'paren':
        v = _const_value(e[1])
    elif k == 'index' and e[1] == ('name', 'math') and e[2] in (('str', 'pi'), ('str', 'huge')):
        v = math.pi if e[2][1] == 'pi' else math.inf
    elif k == 'un':
        x = _const_value(e[2])
        if e[1] == 'not':
            v = not _truthy(x)
        elif e[1] == '-' and _lua_number(x):
            v = -x
        elif e[1] == '#' and isinstance(x, str):
            v = len(x)
        else:
            raise _NotConst()
    elif k == 'bin':
        op = e[1]
        if op in ('and', 'or'):
            x = _const_value(e[2])
            if (op == 'and') != _truthy(x):
                return [x] if multi else x
            v = _const_value(e[3])
        else:
            x, y = _const_value(e[2]), _const_value(e[3])
            if op in ('==', '~='):
                v = (x == y and type(x) is type(y) or _lua_number(x) and _lua_number(y) and x == y) == (op == '==')
            elif op in ('<', '<=', '>', '>='):
                if not (_lua_number(x) and _lua_number(y) or isinstance(x, str) and isinstance(y, str)):
                    raise _NotConst()
                v = x < y if op == '<' else x <= y if op == '<=' else x > y if op == '>' else x >= y
            elif op in ('+', '-', '*', '/', '%', '//', '^'):
                if not (_lua_number(x) and _lua_number(y)):
                    raise _NotConst()
                try:
                    if op == '+':
                        v = x + y
                    elif op == '-':
                        v = x - y
                    elif op == '*':
                        v = x * y
                    elif op == '^':
                        v = float(x) ** float(y)
                    elif y == 0:
                        v = math.nan if op != '/' or x == 0 else math.copysign(math.inf, x)
                    elif op == '/':
                        v = x / y
                    elif op == '//':
                        v = math.floor(x / y)
                    else:
                        v = x - math.floor(x / y) * y
                except (OverflowError, ValueError, ZeroDivisionError):
                    raise _NotConst()
                if isinstance(v, complex):
                    raise _NotConst()
                if isinstance(v, int) and abs(v) >= 2 ** 53:
                    v = float(v)        # (Luau numbers are doubles)
            else:
                raise _NotConst()
    else:
        raise _NotConst()
    return [v] if multi else v


_DEAD_DEF = re.compile(r'^\t*local (v\d+) = (.+)$')


def dead_constant_lines(text):
    """Indexes of the lines `local vN = E` where vN is read nowhere and E is made of literals,
    arithmetic, comparisons, and/or/not and calls of pure library functions on constants: the
    values Luraph v14 programs compute for the self-patches of their code (the lifter folds those
    into the instructions they patch; the register assignment is left with no reader).
    Returns (removable, left): removable when E evaluates without an error (dropping the line then
    changes nothing a script can see), left when it has that shape but does not evaluate here."""
    count = {}
    for kind, s in tokens(text):
        if kind == 'ident':
            count[s] = count.get(s, 0) + 1
    removable, left = [], []
    for i, ln in enumerate(text.split('\n')):
        m = _DEAD_DEF.match(ln)
        if not m or count.get(m.group(1)) != 1 or not re.search(r'\d', m.group(2)):
            continue
        try:
            # (the parser reads source bytes as latin-1, which is how the escapes come out; a
            # character the printer wrote as itself is its UTF-8 bytes in the file)
            p = luparse.Parser(m.group(2).encode('utf-8', 'surrogatepass').decode('latin-1'))
            e = p.expr()
            if p.peek()[0] != 'eof':
                continue
        except Exception:
            continue
        if not _const_shape(e):
            continue
        try:
            _const_value(e)
        except (_NotConst, RecursionError):
            left.append(i)
            continue
        removable.append(i)
    return removable, left


def _const_shape(e):
    """E has the shape of a constant expression (literals, operators, library calls on them)."""
    k = e[0]
    if k in ('num', 'str', 'nil', 'true', 'false'):
        return True
    if k == 'paren':
        return _const_shape(e[1])
    if k == 'un':
        return _const_shape(e[2])
    if k == 'bin':
        return _const_shape(e[2]) and _const_shape(e[3])
    if k == 'index':
        return e[1] == ('name', 'math') and e[2] in (('str', 'pi'), ('str', 'huge'))
    if k == 'call':
        f = e[1]
        return f[0] == 'index' and f[1][0] == 'name' and f[2][0] == 'str' and _pure_name(f[1][1] + '.' + f[2][1]) \
            and all(_const_shape(a) for a in e[2])
    return False


def _pure_name(name):
    return name in pe.PURE or name in _STRING_PURE or name in ('math.modf', 'math.round')


def drop_dead_constants(text):
    """Drops the removable lines of dead_constant_lines (see there)."""
    drop, _left = dead_constant_lines(text)
    if not drop:
        return text
    gone = set(drop)
    return '\n'.join(ln for i, ln in enumerate(text.split('\n')) if i not in gone)


def rename(text):
    toks = tokens(text)
    free = set()
    prev = None
    for kind, s in toks:
        if kind == 'ident' and prev not in ('.', ':'):
            free.add(s)
        if kind != 'other' or not s.isspace():
            prev = s
    used = set(free) | RESERVED
    mapping = {}

    def claim(old, base):
        if not base or base in KEYWORDS:
            return
        name = base
        n = 2
        while name in used:
            name = '%s%d' % (base, n)
            n += 1
        used.add(name)
        mapping[old] = name

    for ln in text.split('\n'):
        m = re.match(r'^\s*local (v\d+) = (.+)$', ln)
        if m and m.group(1) not in mapping:
            base = suggest(m.group(2))
            if base:
                claim(m.group(1), base)
            continue
        m = re.match(r'^\s*local (v\d+), (v\d+) = pcall\(', ln)
        if m:
            claim(m.group(1), 'ok')
            claim(m.group(2), 'result')
            continue
        m = re.match(r'^\s*for (v\d+) = ', ln)
        if m:
            claim(m.group(1), 'i')
            continue
        m = re.match(r'^\s*for (v\d+), (v\d+) in ', ln)
        if m:
            claim(m.group(1), 'key')
            claim(m.group(2), 'value')
    if not mapping:
        return text
    out = []
    for kind, s in toks:
        if kind == 'ident' and s in mapping:
            out.append(mapping[s])
        else:
            out.append(s)
    return ''.join(out)


def _loop_artifacts(text):
    """Two leftovers of numeric / generic for loops whose step instruction was lifted as a loop of
    its own:

        for i = a, b, c do                      -- for-loop setup: a, b, c
            body                                <statements>
            -- for-loop without visible setup   -- for-loop without visible setup
            for i = nil --[[?]] do              for i = nil --[[?]] do
            end                                     body
            break                               end
        end

    left: the step of a loop that is already there (dropped); right: a loop whose bounds were set
    up a few statements earlier (reattached)."""
    import re
    lines = text.split('\n')
    out = []
    setup = {}          # indent -> (index in out, bounds)
    i = 0
    hdr = re.compile(r'^(\t*)for ([\w, ]+?) (=|in) nil --\[\[\?\]\] do$')
    while i < len(lines):
        ln = lines[i]
        ind = len(ln) - len(ln.lstrip('\t'))
        st = ln.strip()
        m = re.match(r'^-- for-loop setup: (.+)$', st)
        if m:
            setup[ind] = (len(out), m.group(1))
            out.append(ln)
            i += 1
            continue
        if st == '-- for-loop without visible setup':
            j = i + 1
            while j < len(lines) and re.match(r'^\t*local [\w, ]+$', lines[j]):
                j += 1
            h = hdr.match(lines[j]) if j < len(lines) else None
            if h and len(h.group(1)) == ind:
                # the step of an enclosing loop: `for x = nil do end break` right before its end
                if j == i + 1 and j + 3 < len(lines) and lines[j + 1].strip() == 'end' and lines[j + 2].strip() == 'break' \
                        and lines[j + 3].strip() == 'end' and len(lines[j + 3]) - len(lines[j + 3].lstrip('\t')) == ind - 1:
                    i = j + 3
                    continue
                if ind in setup and h.group(3) == '=':
                    at, bounds = setup.pop(ind)
                    out[at] = None
                    out.extend(lines[i + 1:j])
                    out.append('%sfor %s = %s do' % (h.group(1), h.group(2), bounds))
                    i = j + 1
                    continue
        if st in ('end', 'else') or st.startswith('elseif ') or st.startswith('until '):
            for k in [k for k in setup if k > ind]:
                del setup[k]
        out.append(ln)
        i += 1
    text = '\n'.join(l for l in out if l is not None)
    # what is left keeps a placeholder that at least parses
    return text.replace(' = nil --[[?]] do', ' = nil --[[bounds not recovered]], nil do')
