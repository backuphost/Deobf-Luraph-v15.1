"""Text-level tidying of decompiled output: drop unused declarations and give variables names
derived from what they are initialised with (services, required modules, instances, properties).

Bytecode carries no local names, so this is cosmetic: every generated name (v12, p3) is unique in
the file, which makes a token-level rename safe.
"""
import re

from . import opaque

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
    # stores into a table nothing else can see (a VM scratch array that was never bound to a variable)
    text = '\n'.join(ln for ln in text.split('\n') if not re.match(r'^\s*\(\{\}\)\[\d+\] = [-\w.]+$', ln))
    text = _loop_artifacts(text)
    text = opaque.strip(text)
    text = drop_unused(text)
    for _ in range(4):
        new = rename(text)
        if new == text:
            break
        text = new
    return text


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
