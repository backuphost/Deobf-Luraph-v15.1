"""Remove Luraph's opaque predicates from decompiled text.

The obfuscator adds number variables that are only ever set to large constants, and guards every
function with comparisons between them.  One arm of such a guard is the real code; the other one
scrambles those numbers and then runs garbage that never executes.  The junk arm is recognised by
the scrambling (arithmetic updates of the opaque numbers), which real paths never contain.
"""
import os
import re

BIG = 100000
ID = r'[A-Za-z_]\w*'
NUM = r'-?\d+(?:\.\d+)?'


def _indent(ln):
    return len(ln) - len(ln.lstrip('\t'))


def _candidates(lines):
    occ = {}
    for i, ln in enumerate(lines):
        for name in set(re.findall(r'\bv\d+\b', ln)):
            occ.setdefault(name, []).append(i)
    cand = set()
    for name, idx in occ.items():
        big = False
        for i in idx:
            m = re.match(r'^\s*(?:local )?%s = (\d+)$' % name, lines[i])
            if m and int(m.group(1)) >= BIG:
                big = True
        if big:
            cand.add(name)

    for name, idx in occ.items():
        # every literal the variable is set to must be such a constant
        for i in idx:
            m = re.match(r'^\s*(?:local )?%s = (%s)$' % (name, NUM), lines[i])
            if m and not (re.fullmatch(r'\d+', m.group(1)) and int(m.group(1)) >= BIG):
                cand.discard(name)
    for _ in range(3):      # copies: local x = opaque +- n
        for name, idx in occ.items():
            if name not in cand:
                m = re.fullmatch(r'\s*local %s = (%s) [-+] \d+' % (name, ID), lines[idx[0]])
                if m and m.group(1) in cand:
                    cand.add(name)

    for _ in range(3):      # aliases: x = opaque
        for name, idx in occ.items():
            if name in cand:
                continue
            rhs = [m.group(1) for m in (re.fullmatch(r'\s*(?:local )?%s = (.*)' % name, lines[i]) for i in idx) if m]
            if rhs and all(r in cand or r == 'nil' for r in rhs) and any(r in cand for r in rhs):
                cand.add(name)

    def opaque_cond(c):
        c = re.sub(r'\b(?:not|and|or)\b|[()<>=~\s]', ' ', c)
        return all(re.fullmatch(NUM, t) or t in cand for t in c.split())

    def fits(name, ln):
        s = ln.strip()
        if re.fullmatch(r'(?:local )?%s = (?:\d+|nil)' % name, s) or re.fullmatch(r'local %s' % name, s):
            return True
        if re.fullmatch(r'%s [-+]= \d+' % name, s):
            return True
        m = re.fullmatch(r'(?:local )?(%s) = (%s) [-+] \d+' % (ID, ID), s)
        if m and m.group(1) in cand and m.group(2) in cand:
            return True
        m = re.fullmatch(r'(?:local )?(%s) = (%s)' % (ID, ID), s)
        if m and m.group(1) in cand and m.group(2) in cand:
            return True
        m = re.fullmatch(r'(?:if|elseif) (.*) then', s)
        return bool(m and opaque_cond(m.group(1)))

    strict = {n for n in cand if all(fits(n, lines[i]) for i in occ[n])}
    return cand, opaque_cond, strict


def strip(text):
    for _ in range(20000):
        new = _strip_once(text)
        if new == text:
            break
        text = new
    for _ in range(40):
        new = _fold_consts(text)
        for _ in range(2000):
            nxt = _fold_const_ifs(new)
            if nxt == new:
                break
            new = nxt
        for fn in (_drop_dead_copies, _inline_fn_alias, _iife_params, _fold_fn_truth, _drop_empty_ifs, _drop_dead_opaque_stores, _drop_callback_opaque):
            for _ in range(500):
                nxt = fn(new)
                if nxt == new:
                    break
                new = nxt
        for _ in range(2000):
            nxt = _fold_const_ifs(new)
            if nxt == new:
                break
            new = nxt
        for _ in range(500):
            nxt = _drop_opaque_params(new)
            if nxt == new:
                break
            new = nxt
        new = _drop_opaque_flow(_drop_orphans(new))
        for _ in range(200):
            nxt = _fold_param_guards(new)
            if nxt == new:
                break
            new = nxt
        if new == text:
            break
        text = new
    return text


# ---------------------------------------------------------------------------------------------
# Opaque numbers by value.  Whatever route such a number takes (a chain of bit32.bxor, a sum, the
# mixed boolean-arithmetic form `mul(x, C) + mul(bnot(x), C)` that is the same for every x), it is
# a constant, and the script only ever compares it with another constant.  Expressions made of
# numbers, arithmetic and bit32 are evaluated; what does not depend on any real value is folded.

_TOK = re.compile(r"\s*(?:(?P<num>0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][-+]?\d+)?)|(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
                  r"|(?P<str>\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*')|(?P<op>==|~=|<=|>=|//|\.\.\.|\.\.|[-+*/%^(),<>={}\[\]#;:.]))")
_BIT = {'bit32.band', 'bit32.bor', 'bit32.bxor', 'bit32.bnot', 'bit32.lshift', 'bit32.rshift'}
_KW = {'and', 'or', 'not', 'if', 'then', 'else', 'elseif', 'end', 'local', 'return', 'function', 'while', 'do', 'for', 'in',
       'repeat', 'until', 'break', 'continue', 'nil', 'true', 'false'}


def _tokens(line):
    out = []
    pos = 0
    n = len(line)
    while pos < n:
        m = _TOK.match(line, pos)
        if not m or m.end() == pos:
            if line[pos:].strip() == '':
                break
            # (anything else ends every expression around it)
            out.append(('other', line[pos], pos, pos + 1))
            pos += 1
            continue
        kind = m.lastgroup
        out.append((kind, m.group(kind), m.start(kind), m.end(kind)))
        pos = m.end()
    return out


class _NoConst(Exception):
    pass


def _u32(v):
    if isinstance(v, bool) or v != v or v in (float('inf'), float('-inf')):
        raise _NoConst()
    return int(v // 1) & 0xFFFFFFFF


def _parse(toks, i, value_of, atom_only=False):
    """Evaluate the longest arithmetic expression that starts at token i: (value, next index, names used)."""
    used = []

    def atom(i):
        if i >= len(toks):
            raise _NoConst()
        k, v = toks[i][0], toks[i][1]
        if k == 'num':
            return (float(int(v, 16)) if v[:2].lower() == '0x' else float(v)), i + 1
        if k == 'name' and v in _BIT:
            if i + 1 >= len(toks) or toks[i + 1][1] != '(':
                raise _NoConst()
            args = []
            j = i + 2
            while True:
                a, j = cmp(j)
                args.append(a)
                if j < len(toks) and toks[j][1] == ',':
                    j += 1
                    continue
                break
            if j >= len(toks) or toks[j][1] != ')':
                raise _NoConst()
            a = [_u32(x) for x in args]
            f = v[6:]
            if f == 'bnot' and len(a) == 1:
                r = ~a[0] & 0xFFFFFFFF
            elif f in ('band', 'bor', 'bxor') and a:
                r = a[0]
                for x in a[1:]:
                    r = r & x if f == 'band' else r | x if f == 'bor' else r ^ x
            elif f in ('lshift', 'rshift') and len(a) == 2:
                sh = int(args[1])
                if not 0 <= sh < 32:
                    raise _NoConst()
                r = (a[0] << sh) & 0xFFFFFFFF if f == 'lshift' else a[0] >> sh
            else:
                raise _NoConst()
            return float(r), j + 1
        if k == 'name' and v not in _KW and '.' not in v:
            used.append(v)
            return value_of(v), i + 1
        if k == 'op' and v == '(':
            a, j = cmp(i + 1)
            if j >= len(toks) or toks[j][1] != ')':
                raise _NoConst()
            return a, j + 1
        raise _NoConst()

    def num(v):
        if isinstance(v, bool):
            raise _NoConst()
        return v

    def pow_(i):
        a, j = atom(i)
        if j < len(toks) and toks[j][1] == '^':
            b, j = unary(j + 1)
            try:
                a = float(num(a) ** num(b))
            except (OverflowError, ZeroDivisionError, TypeError):
                raise _NoConst()
        return a, j

    def unary(i):
        if i < len(toks) and toks[i][0] == 'op' and toks[i][1] == '-':
            a, j = unary(i + 1)
            return -num(a), j
        return pow_(i)

    def mul(i):
        a, j = unary(i)
        while j < len(toks) and toks[j][0] == 'op' and toks[j][1] in ('*', '/', '%', '//'):
            op = toks[j][1]
            b, j = unary(j + 1)
            a, b = num(a), num(b)
            try:
                if op == '*':
                    a = a * b
                elif op == '/':
                    a = a / b
                elif op == '%':
                    a = a - (a // b) * b if b not in (float('inf'), float('-inf')) else a
                else:
                    a = a // b
            except (ZeroDivisionError, OverflowError):
                raise _NoConst()
        return a, j

    def add(i):
        a, j = mul(i)
        while j < len(toks) and toks[j][0] == 'op' and toks[j][1] in ('+', '-'):
            op = toks[j][1]
            b, j = mul(j + 1)
            a = num(a) + num(b) if op == '+' else num(a) - num(b)
        return a, j

    def cmp(i):
        a, j = add(i)
        if j < len(toks) and toks[j][0] == 'op' and toks[j][1] in ('==', '~='):
            op = toks[j][1]
            b, j = add(j + 1)
            a = (a == b) == (op == '==')
        return a, j

    v, j = (atom if atom_only else cmp)(i)
    if isinstance(v, float) and (v != v or v in (float('inf'), float('-inf'))):
        raise _NoConst()
    return v, j, used


def _lit(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if float(v).is_integer() and abs(v) < 2 ** 63:
        return str(int(v))
    return repr(float(v))


_ARITH_OPS = {'+', '-', '*', '/', '%', '^', '//'}


def _fold_consts(text):
    import random
    lines = text.split('\n')
    toks = [None] * len(lines)

    def T(i):
        if toks[i] is None:
            toks[i] = _tokens(lines[i])
        return toks[i]

    VAR = re.compile(r'\b[vp]\d+\b')
    def_re = re.compile(r'^(\t*)(local )?([vp]\d+) = (.+)$')
    ndefs = {}
    defline = {}
    uses = {}
    for i, ln in enumerate(lines):
        found = VAR.findall(ln)
        if not found:
            continue
        for nm in found:
            uses.setdefault(nm, []).append(i)
        m = def_re.match(ln)
        if m:
            ndefs[m.group(3)] = ndefs.get(m.group(3), 0) + 1
            defline[m.group(3)] = i
        # every other way a variable gets a value
        m = None if m else re.match(r'^\t*(?:local )?([\w, ]+?) (?:[-+*/%^]|\.\.)?= ', ln)
        if m:
            for nm in VAR.findall(m.group(1)):
                ndefs[nm] = ndefs.get(nm, 0) + 2
        for m in re.finditer(r'function\b[^(\n]*\(([^)\n]*)\)', ln):
            for nm in VAR.findall(m.group(1)):
                ndefs[nm] = ndefs.get(nm, 0) + 2
        m = re.match(r'^\t*for ([\w, ]+?) (?:=|in) ', ln)
        if m:
            for nm in VAR.findall(m.group(1)):
                ndefs[nm] = ndefs.get(nm, 0) + 2
        m = re.match(r'^\t*local function ([vp]\d+)', ln)
        if m:
            ndefs[m.group(1)] = ndefs.get(m.group(1), 0) + 2

    # parameters that every caller gives the same number
    pconst = {}
    for name, d in _functions(text).items():
        for k in range(max(len(ps) for ps, _ in d['defs'])):
            vals = {text[c[k][0]:c[k][1]].strip() if k < len(c) else None for c in d['calls']}
            if len(vals) != 1:
                continue
            v = next(iter(vals))
            if v is None or not re.fullmatch(r'\d{7,}', v) or int(v) < 2 ** 24:
                continue
            for ps, _ in d['defs']:
                if k < len(ps) and re.fullmatch(r'p\d+', ps[k]) and ndefs.get(ps[k]) == 2:
                    pconst[ps[k]] = float(v)

    TRIALS = 4
    rnd = random.Random(12345)
    free = {}
    memo = {}
    deps = {}
    tainted = set()

    def value(nm, t, stack=()):
        key = (nm, t)
        if key in memo:
            return memo[key]
        if nm in pconst:
            memo[key] = pconst[nm]
            return memo[key]
        v = None
        if ndefs.get(nm) == 1 and nm not in stack:
            m = def_re.match(lines[defline[nm]])
            tk = _tokens(m.group(4))
            try:
                v, j, used = _parse(tk, 0, lambda x: value(x, t, stack + (nm,)))
                if j != len(tk):
                    v = None
                else:
                    deps[nm] = set(used)
            except _NoConst:
                v = None
        if v is None:
            if nm not in free:
                free[nm] = [float(rnd.choice([rnd.randrange(0, 2 ** 32), rnd.randrange(-2 ** 20, 2 ** 20),
                                              rnd.uniform(-1e6, 1e6), rnd.uniform(0, 2 ** 33)])) for _ in range(TRIALS)]
            v = free[nm][t]
        memo[key] = v
        return v

    const = {}
    for nm in list(defline):
        if ndefs.get(nm) != 1:
            continue
        vals = [value(nm, t) for t in range(TRIALS)]
        if nm in free:
            continue
        if all(type(x) is type(vals[0]) and x == vals[0] for x in vals):
            const[nm] = vals[0]
    for nm, v in pconst.items():
        const[nm] = v

    def opaque_def(nm, seen=()):
        """does the value come out of large numbers / bit operations (and not out of the script's own small constants)"""
        if nm in pconst:
            return True
        if nm in seen or nm not in defline or ndefs.get(nm) != 1:
            return False
        rhs = def_re.match(lines[defline[nm]]).group(4)
        if 'bit32.' in rhs or any(float(x) >= 2 ** 24 for x in re.findall(r'(?<![\w.])\d{8,}(?![\w.])', rhs)):
            return True
        return any(opaque_def(d, seen + (nm,)) for d in deps.get(nm, ()))

    const = {nm: v for nm, v in const.items() if opaque_def(nm)}

    def fail(x):
        raise _NoConst()

    def spans(i, names):
        """[(start col, end col, value, tokens, names, token before, token after)]: constant expressions of line i"""
        tk = T(i)
        out = []
        j = 0
        m = def_re.match(lines[i])
        first = 0
        if m:
            # (the variable a line defines is not a use of it)
            first = next((n for n, t in enumerate(tk) if t[1] == '='), -1) + 1
        j = first
        plist = set()           # (parameter lists)
        for n, t in enumerate(tk):
            if t[0] == 'name' and t[1] == 'function':
                e = next((x for x in range(n, len(tk)) if tk[x][1] == ')'), len(tk))
                plist.update(range(n, e + 1))
        while j < len(tk):
            k, v = tk[j][0], tk[j][1]
            start_ok = (k == 'name' and (v in names or v in _BIT)) or (k == 'op' and v in ('(', '-')) or k == 'num'
            prev = tk[j - 1] if j else None
            if not start_ok or j in plist or (prev is not None and prev[0] == 'op' and prev[1] in ('.', ':')):
                j += 1
                continue
            got = None
            for atom_only in (False, True):
                try:
                    val, e, used = _parse(tk, j, lambda x: names[x] if x in names else fail(x), atom_only)
                except _NoConst:
                    continue
                if not used and not (not atom_only and e - j >= 3 and any(
                        t[0] == 'num' and float(int(t[1], 16) if t[1][:2].lower() == '0x' else t[1]) >= 2 ** 24 for t in tk[j:e])):
                    break
                nxt = tk[e] if e < len(tk) else None
                after_value = prev is not None and (prev[0] in ('num', 'str') or prev[0] == 'name' and prev[1] not in _KW
                                                    or prev[0] == 'op' and prev[1] in (')', ']', '}'))
                if after_value and tk[j][1] in ('(', '-') and not atom_only:
                    continue
                if after_value and tk[j][1] == '(':
                    break           # the argument list of a call, not a parenthesised value
                before_suffix = nxt is not None and (nxt[0] == 'op' and nxt[1] in ('(', '[', '.', ':', '{') or nxt[0] == 'str')
                if before_suffix:
                    if atom_only:
                        break
                    continue
                if not atom_only:
                    if prev is not None and (prev[0] == 'op' and prev[1] in _ARITH_OPS | {'#'} or prev[0] == 'name' and prev[1] == 'not'):
                        continue
                    if nxt is not None and nxt[0] == 'op' and nxt[1] in _ARITH_OPS:
                        continue
                elif isinstance(val, bool):
                    break
                got = (tk[j][2], tk[e - 1][3], val, e - j, used, prev, nxt)
                break
            if got:
                out.append(got)
                j += got[3]
            else:
                j += 1
        return out

    # a number the script itself uses as a value stays a variable
    names = dict(const)
    changed = True
    while changed:
        changed = False
        covered = {}
        for nm in names:
            for i in set(uses.get(nm, ())):
                if i not in covered:
                    covered[i] = spans(i, names)
        for nm in list(names):
            dl = defline.get(nm) if nm not in pconst else None
            seed = dl is not None and re.fullmatch(r'\d+', def_re.match(lines[dl]).group(4)) is not None
            for i in set(uses.get(nm, ())):
                n_here = len(re.findall(r'\b%s\b' % nm, lines[i]))
                if i == dl:
                    n_here -= 1
                elif nm in pconst and re.search(r'function\b[^(\n]*\([^)\n]*\b%s\b' % nm, lines[i]):
                    n_here -= 1
                sp = [x for x in covered.get(i, ()) if nm in x[4]]
                if sum(x[4].count(nm) for x in sp) != n_here:
                    del names[nm]       # an occurrence that is not a plain value
                    changed = True
                    break
                if seed and any(x[3] == 1 and not (x[5] is not None and x[5][1] in ('(', ',') and x[6] is not None and x[6][1] in (')', ','))
                                for x in sp):
                    del names[nm]
                    changed = True
                    break
    drop = set()
    for nm in names:
        if nm in defline and nm not in pconst:
            drop.add(defline[nm])
    touched = set()
    for nm in names:
        touched.update(uses.get(nm, ()))
    # (and expressions of large numbers alone)
    lit = re.compile(r'(?<![\w.])\d{8,}(?![\w.])')
    for i, ln in enumerate(lines):
        if lit.search(ln) and ('bit32.' in ln or re.search(r'\d{8,}\)* [-+*%] \(*\d', ln) or re.search(r'\d\)* [-+*%] \(*\d{8,}', ln)):
            touched.add(i)
    for i in sorted(touched):
        if i in drop:
            continue
        ln = lines[i]
        if re.fullmatch(r'\t*local [\w, ]+', ln):
            rest = [x.strip() for x in ln.strip()[6:].split(',') if x.strip() not in names or x.strip() in pconst]
            if rest:
                lines[i] = ln[:len(ln) - len(ln.lstrip('\t'))] + 'local ' + ', '.join(rest)
            else:
                drop.add(i)
            continue
        for a, b, val, n, used, prev, nxt in sorted(spans(i, names), reverse=True):
            rep = _lit(val)
            if rep.startswith('-') and prev is not None and prev[0] == 'op' and prev[1] in _ARITH_OPS:
                rep = '(' + rep + ')'
            ln = ln[:a] + rep + ln[b:]
        lines[i] = ln
        toks[i] = None
    # what only fed them: arithmetic on a real value whose result did not depend on it
    feed = set()
    stack = [d for nm in names for d in deps.get(nm, ())]
    while stack:
        d = stack.pop()
        if d in feed or d in names:
            continue
        feed.add(d)
        stack.extend(deps.get(d, ()))
    again = True
    while again:
        again = False
        for d in list(feed):
            if d in defline and defline[d] not in drop and ndefs.get(d) == 1 and d in deps:
                cnt = sum(len(re.findall(r'\b%s\b' % d, lines[i])) for i in set(uses.get(d, ())) if i not in drop)
                if cnt == 1:
                    drop.add(defline[d])
                    feed.discard(d)
                    again = True
    return '\n'.join(ln for i, ln in enumerate(lines) if i not in drop)


def _fold_const_ifs(text):
    """`if true then A else B end` is A (and so on), after the guards were evaluated."""
    lines = text.split('\n')
    for i, ln in enumerate(lines):
        g = re.fullmatch(r'(\t*)(if|elseif|while) (not )?\(?(true|false)\)? (then|do)', ln)
        if not g:
            continue
        ind = g.group(1)
        truth = (g.group(4) == 'true') != bool(g.group(3))
        # the statement's parts at this indentation
        marks = []
        end = None
        for j in range(i + 1, len(lines)):
            if lines[j] == ind + 'end':
                end = j
                break
            if lines[j] == ind + 'else' or lines[j].startswith(ind + 'elseif '):
                marks.append(j)
        if end is None:
            continue

        def ded(blk):
            return [x[1:] if x.startswith('\t') else x for x in blk]
        nxt = marks[0] if marks else end
        if g.group(2) == 'while':
            if truth:
                continue
            lines[i:end + 1] = []
        elif g.group(2) == 'if':
            if truth:
                lines[i:end + 1] = ded(lines[i + 1:nxt])
            elif not marks:
                lines[i:end + 1] = []
            elif lines[nxt] == ind + 'else':
                lines[i:end + 1] = ded(lines[nxt + 1:end])
            else:
                lines[i:nxt + 1] = [ind + 'if ' + lines[nxt][len(ind) + 7:]]
        else:
            if truth:
                lines[i:end] = [ind + 'else'] + lines[i + 1:nxt]
            else:
                lines[i:nxt] = []
        return '\n'.join(lines)
    return text


_OPQ_FN = re.compile(r'bit32\.\w+')
_WORD = re.compile(r'[A-Za-z_]\w*')


def _drop_opaque_flow(text):
    """Opaque state numbers that travel: computed from one another, passed to functions as
    arguments, received in parameters that nothing reads except to pass them on.  Found as the
    largest set of variables every occurrence of which is one of those roles; their assignments,
    the parameters and the arguments are removed together."""
    names = {}
    for m in re.finditer(r'\b[vp]\d+\b', text):
        names.setdefault(m.group(0), []).append(m.start())
    if not names:
        return text
    line_start = [0]
    for m in re.finditer('\n', text):
        line_start.append(m.end())
    import bisect

    def line_of(pos):
        i = bisect.bisect_right(line_start, pos) - 1
        a = line_start[i]
        b = text.find('\n', a)
        return a, (len(text) if b < 0 else b)

    # function definitions with a plain name, and their call sites
    defs = {}
    for m in re.finditer(r'(?m)^[ \t]*(?:local (v\d+) = function|local function (v\d+)|(v\d+) = function)\(([^)\n]*)\)', text):
        name = m.group(1) or m.group(2) or m.group(3)
        if name in defs:
            defs[name] = None            # assigned twice: not one function
            continue
        params = [x.strip() for x in m.group(4).split(',') if x.strip()]
        defs[name] = {'params': params, 'span': (m.start(4), m.end(4)), 'namepos': m.start(1 if m.group(1) else 2 if m.group(2) else 3),
                      'calls': [], 'ok': True}
    for name, d in defs.items():
        if not d:
            continue
        for pos in names.get(name, ()):
            if pos == d['namepos']:
                continue
            end = pos + len(name)
            before = text[pos - 1:pos]
            # (`local v5` forward declarations are fine)
            a, b = line_of(pos)
            if re.fullmatch(r'\s*local [\w, ]+', text[a:b]):
                continue
            if text[end:end + 1] != '(' or before in ('.', ':'):
                d['ok'] = False
                break
            args, close = _call_args(text, end)
            if args is None:
                d['ok'] = False
                break
            d['calls'].append(args)
    defs = {k: d for k, d in defs.items() if d and d['ok']}
    # parameter -> (function, index); argument spans per (function, index)
    param_of = {}
    for name, d in defs.items():
        for k, prm in enumerate(d['params']):
            if re.fullmatch(r'p\d+', prm):
                param_of[prm] = (name, k)

    cand = set(names)
    sinks = {(f, k) for prm, (f, k) in param_of.items()}

    def opaque_expr(t):
        t = _OPQ_FN.sub(' ', t)
        for nm in re.findall(r'\b[A-Za-z_]\w*\b', t):
            if nm not in cand:
                return False
        return re.fullmatch(r'[\w\s()+\-*/%,.]*(?:[=~]= ?[\w\s()+\-*/%,.]*)?', t) is not None and t.strip() != ''

    def_line = re.compile(r'\s*(?:local )?([vp]\d+) = (.+)')

    def role(name, pos):
        """how this occurrence is accounted for, or None"""
        a, b = line_of(pos)
        ln = text[a:b]
        if re.fullmatch(r'\s*local [\w, ]+', ln):
            return ('decl', a, b)
        if name in param_of:
            f, k = param_of[name]
            sa, sb = defs[f]['span']
            if sa <= pos < sb:
                return ('param', f, k) if (f, k) in sinks else None
        m = def_line.fullmatch(ln)
        if m and m.group(1) in cand and opaque_expr(m.group(2)):
            return ('def', a, b)
        # an argument of a sink call
        for f, k in sinks:
            for args in defs[f]['calls']:
                if k < len(args) and args[k][0] <= pos < args[k][1]:
                    return ('arg', f, k) if opaque_expr(text[args[k][0]:args[k][1]]) else None
        return None

    # (argument lookup by position, instead of scanning every sink for every occurrence)
    arg_at = []
    for f, d in defs.items():
        for args in d['calls']:
            for k, (x, y) in enumerate(args):
                arg_at.append((x, y, f, k))
    arg_at.sort()
    arg_starts = [x[0] for x in arg_at]

    def role(name, pos):          # noqa: F811
        a, b = line_of(pos)
        ln = text[a:b]
        if re.fullmatch(r'\s*local [\w, ]+', ln):
            return ('decl', a, b)
        if name in param_of:
            f, k = param_of[name]
            sa, sb = defs[f]['span']
            if sa <= pos < sb:
                return ('param', f, k) if (f, k) in sinks else None
        m = def_line.fullmatch(ln)
        if m and m.group(1) in cand and opaque_expr(m.group(2)):
            return ('def', a, b)
        i = bisect.bisect_right(arg_starts, pos) - 1
        while i >= 0 and i > bisect.bisect_right(arg_starts, pos) - 40:
            x, y, f, k = arg_at[i]
            if x <= pos < y and (f, k) in sinks:
                return ('arg', f, k) if opaque_expr(text[x:y]) else None
            i -= 1
        return None

    changed = True
    rounds = 0
    while changed and rounds < 60:
        changed = False
        rounds += 1
        for name in list(cand):
            bad = next((pos for pos in names[name] if role(name, pos) is None), None)
            if bad is not None:
                cand.discard(name)
                changed = True
                if os.environ.get('DEVM_TRACE_OPQFLOW') and any(
                        re.search(r'= (\d{7,}$|bit32\.)', text[slice(*line_of(p2))]) and def_line.fullmatch(text[slice(*line_of(p2))])
                        and def_line.fullmatch(text[slice(*line_of(p2))]).group(1) == name for p2 in names[name]):
                    print('opqflow r%d: %s kept because of: %s' % (rounds, name, text[slice(*line_of(bad))].strip()[:150]))
        for f, k in list(sinks):
            d = defs[f]
            prm = d['params'][k]
            if prm not in cand or not d['calls'] or any(
                    k >= len(args) or not opaque_expr(text[args[k][0]:args[k][1]]) for args in d['calls']):
                sinks.discard((f, k))
                changed = True
                if os.environ.get('DEVM_TRACE_OPQFLOW') and any(
                        k < len(args) and re.search(r'bit32\.|\d{7,}', text[args[k][0]:args[k][1]]) for args in d['calls']):
                    why = 'parameter %s is used' % prm if prm not in cand else 'an argument is not opaque: ' + next(
                        (text[args[k][0]:args[k][1]].strip()[:60] if k < len(args) else '(missing)') for args in d['calls']
                        if k >= len(args) or not opaque_expr(text[args[k][0]:args[k][1]]))
                    print('opqflow r%d: sink %s #%d dropped: %s' % (rounds, f, k, why))
    # something has to be an opaque number to begin with: a variable set to a large constant
    seeds = {n for n in cand if any(re.fullmatch(r'\s*(?:local )?%s = \d{7,}' % n, text[slice(*line_of(p))]) for p in names[n])}
    live = set(seeds)
    grew = True
    while grew:
        grew = False
        for n in cand - live:
            for p in names[n]:
                r = role(n, p)
                a, b = line_of(p)
                if r and r[0] == 'def' and any(x in live for x in re.findall(r'\b[vp]\d+\b', text[a:b]) if x != n):
                    live.add(n)
                    grew = True
                    break
                if r and r[0] == 'param':
                    f, k = r[1], r[2]
                    if any(set(re.findall(r'\b[vp]\d+\b', text[args[k][0]:args[k][1]])) & live
                           or re.fullmatch(r'\s*\d{7,}\s*', text[args[k][0]:args[k][1]]) for args in defs[f]['calls']):
                        live.add(n)
                        grew = True
                        break
    if not live:
        return text
    cuts = []
    drop_sinks = {param_of[n] for n in live if n in param_of and param_of[n] in sinks}
    for n in live:
        for p in names[n]:
            r = role(n, p)
            if r and r[0] == 'def':
                a, b = r[1], r[2]
                cuts.append((a, min(len(text), b + 1), ''))
            elif r and r[0] == 'decl':
                a, b = r[1], r[2]
                ln = text[a:b]
                rest = [x.strip() for x in ln.strip()[len('local '):].split(',') if x.strip() and x.strip() not in live]
                ind = ln[:len(ln) - len(ln.lstrip())]
                cuts.append((a, min(len(text), b + 1), (ind + 'local ' + ', '.join(rest) + '\n') if rest else ''))
    for f, k in drop_sinks:
        d = defs[f]
        params = [x for i, x in enumerate(d['params']) if (f, i) not in drop_sinks]
        cuts.append((d['span'][0], d['span'][1], ', '.join(params)))
        for args in d['calls']:
            keep = [text[x:y].strip() for i, (x, y) in enumerate(args) if (f, i) not in drop_sinks]
            if args:
                cuts.append((args[0][0], args[-1][1], ', '.join(keep)))
    # apply, last first; overlapping edits (nested calls) are left for the next round
    cuts = sorted(set(cuts))
    out, pos = [], 0
    for a, b, rep in cuts:
        if a < pos:
            continue
        out.append(text[pos:a])
        out.append(rep)
        pos = b
    out.append(text[pos:])
    return ''.join(out)


def _call_args(text, i):
    """text[i] is the `(` of a call: [(start, end)] of its top-level arguments, and the index of the `)`."""
    depth = 0
    args = []
    start = i + 1
    j = i
    n = len(text)
    while j < n:
        c = text[j]
        if c in '"\'':
            j += 1
            while j < n and text[j] != c:
                j += 2 if text[j] == '\\' else 1
        elif c == '[' and text[j + 1:j + 2] in ('[', '='):
            m = re.match(r'\[(=*)\[', text[j:])
            if m:
                end = text.find(']' + m.group(1) + ']', j)
                if end < 0:
                    return None, None
                j = end + len(m.group(1)) + 1
        elif c == '-' and text[j + 1:j + 2] == '-':
            end = text.find('\n', j)
            j = (end if end >= 0 else n) - 1
        elif c.isalpha() or c == '_':
            # a word: block keywords nest like brackets (a function literal among the arguments)
            m = _WORD.match(text, j)
            w = m.group(0)
            if w in ('function', 'if', 'do', 'repeat'):
                depth += 1
            elif w in ('end', 'until'):
                depth -= 1
            j = m.end() - 1
        elif c in '({[':
            depth += 1
        elif c in ')}]':
            depth -= 1
            if depth == 0:
                if text[start:j].strip():
                    args.append((start, j))
                return args, j
        elif c == ',' and depth == 1:
            args.append((start, j))
            start = j + 1
        j += 1
    return None, None


def _fold_param_guards(text):
    """A guard on a parameter for which every caller passes the same magic number:
    `if p == 139446416 then BODY end` is BODY, `if p ~= 139446416 then JUNK else BODY end` is BODY."""
    lines = text.split('\n')
    joined = text
    done = False
    for m in re.finditer(r'(?m)^[ \t]*(?:local (v\d+) = function|local function (v\d+)|(v\d+) = function)\(([^)\n]*)\)', joined):
        name = m.group(1) or m.group(2) or m.group(3)
        params = [x.strip() for x in m.group(4).split(',') if x.strip()]
        calls = []
        ok = True
        for u in re.finditer(r'\b%s\b' % name, joined):
            if m.start() <= u.start() < m.end():
                continue
            a = joined.rfind('\n', 0, u.start()) + 1
            b = joined.find('\n', u.start())
            if re.fullmatch(r'\s*local [\w, ]+', joined[a:b if b >= 0 else len(joined)]):
                continue
            if joined[u.end():u.end() + 1] != '(' or joined[u.start() - 1:u.start()] in ('.', ':'):
                ok = False
                break
            args, close = _call_args(joined, u.end())
            if args is None:
                ok = False
                break
            calls.append([joined[x:y].strip() for x, y in args])
        if not ok or not calls:
            continue
        for k, prm in enumerate(params):
            if not re.fullmatch(r'p\d+', prm):
                continue
            vals = {c[k] if k < len(c) else None for c in calls}
            if len(vals) != 1:
                continue
            v = next(iter(vals))
            if v is None or not re.fullmatch(r'\d{7,}', v):
                continue
            v = int(v)
            pat = re.compile(r'(\t*)if (?:%s|%s \+ (\d+)) ([=~]=) (\d+) then' % (prm, prm))
            for i, ln in enumerate(lines):
                g = pat.fullmatch(ln)
                if not g:
                    continue
                ind = g.group(1)
                truth = ((v + int(g.group(2) or 0)) % 4294967296 == int(g.group(4))) == (g.group(3) == '==')
                # the statement's extent: `else` / `end` at the same indentation
                els, end = None, None
                for j in range(i + 1, len(lines)):
                    if lines[j] == ind + 'else' and els is None:
                        els = j
                    elif lines[j].startswith(ind + 'elseif ') and els is None:
                        break
                    elif lines[j] == ind + 'end':
                        end = j
                        break
                if end is None:
                    continue
                body = lines[i + 1:(els if els is not None else end)] if truth else (lines[els + 1:end] if els is not None else [])
                lines[i:end + 1] = [x[1:] if x.startswith('\t') else x for x in body]
                done = True
                break
            if done:
                break
        if done:
            break
    return '\n'.join(lines) if done else text


_FN_DEF = re.compile(r'(?m)^[ \t]*(?:local (v\d+) = function|local function (v\d+)|(v\d+) = function)\(([^)\n]*)\)')


def _functions(text):
    """Functions that are only ever called by name: name -> {'defs': [(params, span of the list)],
    'calls': [argument spans]}.  A name may be given a function in several places (one per branch)."""
    out = {}
    for m in _FN_DEF.finditer(text):
        gi = 1 if m.group(1) else 2 if m.group(2) else 3
        name = m.group(gi)
        d = out.setdefault(name, {'defs': [], 'calls': [], 'at': set(), 'ok': True})
        d['defs'].append(([x.strip() for x in m.group(4).split(',') if x.strip()], (m.start(4), m.end(4))))
        d['at'].add(m.start(gi))
    if not out:
        return out
    for u in re.finditer(r'\bv\d+\b', text):
        d = out.get(u.group(0))
        if d is None or not d['ok'] or u.start() in d['at']:
            continue
        a = text.rfind('\n', 0, u.start()) + 1
        b = text.find('\n', u.start())
        if re.fullmatch(r'\s*local [\w, ]+', text[a:b if b >= 0 else len(text)]):
            continue
        if text[u.end():u.end() + 1] != '(' or text[u.start() - 1:u.start()] in ('.', ':'):
            d['ok'] = False
            continue
        args, close = _call_args(text, u.end())
        if args is None:
            d['ok'] = False
            continue
        d['calls'].append(args)
    return {k: d for k, d in out.items() if d['ok'] and d['calls']}


def _drop_opaque_params(text):
    """A parameter the function never looks at, for which every caller passes one of the opaque
    numbers: it only fed the guards that are gone.  Parameter and arguments are removed.  The same
    for an opaque number passed to a function that has no parameter in that place."""
    counts = {}
    for m in re.finditer(r'\b[vp]\d+\b', text):
        counts[m.group(0)] = counts.get(m.group(0), 0) + 1
    for name, d in _functions(text).items():
        width = max(len(c) for c in d['calls'])
        for k in range(width):
            if any(len(c) > k and not re.fullmatch(r'\s*\d{7,}\s*', text[c[k][0]:c[k][1]]) for c in d['calls']):
                continue
            if not any(len(c) > k for c in d['calls']):
                continue
            if any(len(c) <= k < max(len(ps) for ps, _ in d['defs']) for c in d['calls']):
                continue            # (a call that leaves the place empty: the later arguments would shift)
            good = True
            for ps, span in d['defs']:
                if '...' in ps and k >= ps.index('...'):
                    good = False
                elif k < len(ps) and (not re.fullmatch(r'p\d+', ps[k]) or counts.get(ps[k]) != 1):
                    good = False
            if not good:
                continue
            cuts = []
            for args in d['calls']:
                if len(args) <= k:
                    continue
                a, b = args[k]
                while a < b and text[a] in ' \t\n':
                    a += 1
                if k + 1 < len(args):
                    nx = args[k + 1][0]
                    while text[nx:nx + 1] == ' ':
                        nx += 1
                    cuts.append((a, nx, ''))            # the argument and the comma after it
                elif k > 0:
                    cuts.append((args[k - 1][1], b, ''))    # the comma before it and the argument
                else:
                    cuts.append((a, b, ''))
            for ps, span in d['defs']:
                if k < len(ps):
                    cuts.append((span[0], span[1], ', '.join(ps[:k] + ps[k + 1:])))
            for c in sorted(cuts, reverse=True):
                text = text[:c[0]] + c[2] + text[c[1]:]
            return text
    return text


def _iife_params(text):
    """`(function(p, ...) ... end)(123456789, ...)`: the same for a function that is called on the spot."""
    lines = text.split('\n')
    for i, ln in enumerate(lines):
        m = re.match(r'^(\t*)end\)\((\d{7,})(, |\))', ln)
        if not m or int(m.group(2)) < 2 ** 24:
            continue
        ind = m.group(1)
        j = i - 1
        while j >= 0 and (lines[j].startswith(ind + '\t') or not lines[j].strip()):
            j -= 1
        h = re.search(r'\(function\((p\d+)((?:, [\w.]+)*)\)$', lines[j]) if j >= 0 and lines[j].startswith(ind) else None
        if not h and j + 1 < i:
            # (the header can sit one level deeper, inside a table constructor that opened above)
            j += 1
            h = re.search(r'\(function\((p\d+)((?:, [\w.]+)*)\)$', lines[j])
        if not h:
            continue
        prm = h.group(1)
        body = '\n'.join(lines[j + 1:i])
        n = len(re.findall(r'\b%s\b' % prm, body))
        if n == 0:
            rest = h.group(2)[2:] if h.group(2) else ''
            lines[j] = lines[j][:h.start()] + '(function(' + rest + ')'
            lines[i] = ind + 'end)(' + (ln[m.end():] if m.group(3) == ', ' else ')' + ln[m.end():])
            return '\n'.join(lines)
        # used: by guards on its value
        val = float(m.group(2))
        changed = False
        for x in range(j + 1, i):
            if not re.search(r'\b%s\b' % prm, lines[x]):
                continue
            tk = _tokens(lines[x])
            new = lines[x]
            for n0 in range(len(tk) - 1, -1, -1):
                if tk[n0][1] != prm:
                    continue
                prev = tk[n0 - 1] if n0 else None
                for start in ([n0 - 1] if prev is not None and prev[1] == '(' else []) + [n0]:
                    try:
                        v, e, used = _parse(tk, start, lambda nm: val if nm == prm else (_ for _ in ()).throw(_NoConst()))
                    except _NoConst:
                        continue
                    before = tk[start - 1] if start else None
                    after = tk[e] if e < len(tk) else None
                    if (before is not None and (before[0] == 'op' and before[1] in _ARITH_OPS | {'.', ':'} or before[0] == 'name' and before[1] not in _KW)) \
                            or (after is not None and after[0] == 'op' and after[1] in _ARITH_OPS | {'(', '[', '.', ':'}):
                        continue
                    if e - start < 2:
                        continue
                    new = new[:tk[start][2]] + _lit(v) + new[tk[e - 1][3]:]
                    break
                else:
                    continue
                break
            if new != lines[x]:
                lines[x] = new
                changed = True
        if changed:
            return '\n'.join(lines)
    return text


def _drop_dead_copies(text):
    """What is left of `not not x` guards and of register shuffling: `v = not not x` / `local v = x`
    where v is never read."""
    lines = text.split('\n')
    occ = {}
    for i, ln in enumerate(lines):
        for nm in set(re.findall(r'\bv\d+\b', ln)):
            occ.setdefault(nm, []).append(i)
    drop = set()
    fnames = {m.group(1) or m.group(2) or m.group(3) for m in _FN_DEF.finditer(text)}
    for nm, idx in occ.items():
        ok = True
        mine = []
        for i in idx:
            ln = lines[i]
            if re.fullmatch(r'\t*local [\w, ]+', ln):
                continue
            g = re.fullmatch(r'\t*(?:local )?%s = ((?:not )*)([A-Za-z_]\w*)' % nm, ln)
            # (a plain copy only of a function of the script: a variable the script itself sets
            # and never reads is its own business)
            if g and g.group(2) != nm and (g.group(1) or g.group(2) in fnames):
                mine.append(i)
                continue
            ok = False
            break
        if ok and mine:
            drop.update(mine)
            for i in idx:
                ln = lines[i]
                if i not in mine and re.fullmatch(r'\t*local [\w, ]+', ln):
                    rest = [x.strip() for x in ln.strip()[6:].split(',') if x.strip() != nm]
                    if rest:
                        lines[i] = ln[:len(ln) - len(ln.lstrip('\t'))] + 'local ' + ', '.join(rest)
                    else:
                        drop.add(i)
    if not drop:
        return text
    return '\n'.join(ln for i, ln in enumerate(lines) if i not in drop)


def _fold_fn_truth(text):
    """`local a = f` where f is a function of the script and a is only ever tested: the test is true."""
    lines = text.split('\n')
    fnames = {m.group(1) or m.group(2) or m.group(3) for m in _FN_DEF.finditer(text)}
    occ = {}
    for i, ln in enumerate(lines):
        for nm in set(re.findall(r'\bv\d+\b', ln)):
            occ.setdefault(nm, []).append(i)
    for nm, idx in occ.items():
        d = [i for i in idx if re.fullmatch(r'\t*local %s = (v\d+)' % nm, lines[i])]
        if len(d) != 1 or re.fullmatch(r'\t*local %s = (v\d+)' % nm, lines[d[0]]).group(1) not in fnames:
            continue
        rest = [i for i in idx if i != d[0]]
        if not rest or not all(re.fullmatch(r'\t*(?:if|elseif) (?:not )?%s then' % nm, lines[i]) for i in rest):
            continue
        for i in rest:
            lines[i] = re.sub(r'not %s\b' % nm, 'false', lines[i])
            lines[i] = re.sub(r'\b%s\b' % nm, 'true', lines[i])
        del lines[d[0]]
        return '\n'.join(lines)
    return text


def _drop_empty_ifs(text):
    """`if x then end` with a test that does nothing: what is left when a junk arm was removed."""
    lines = text.split('\n')
    out = []
    i = 0
    changed = False
    while i < len(lines):
        ln = lines[i]
        m = re.fullmatch(r'(\t*)if (.*) then', ln)
        if m and i + 1 < len(lines) and lines[i + 1] == m.group(1) + 'end':
            c = re.sub(r'bit32\.\w+\(', '', m.group(2))
            if not re.search(r'[\w\]\)]\s*[\(:{"\']', c) and '..' not in c and '#' not in c:
                i += 2
                changed = True
                continue
        out.append(ln)
        i += 1
    return '\n'.join(out) if changed else text


def _drop_dead_opaque_stores(text):
    """`v = 180628196` into a variable that otherwise holds real values and is never part of any
    arithmetic: the register was shared with an opaque number, and that store is never read."""
    lines = text.split('\n')
    stores = {}
    for i, ln in enumerate(lines):
        m = re.fullmatch(r'\t*(v\d+) = (\d{8,})', ln)
        if m and int(m.group(2)) >= 2 ** 24:
            stores.setdefault(m.group(1), []).append(i)
    if not stores:
        return text
    drop = set()
    for nm, idx in stores.items():
        real = False
        ok = True
        for i, ln in enumerate(lines):
            if i in idx or not re.search(r'\b%s\b' % nm, ln):
                continue
            if re.fullmatch(r'\t*local [\w, ]+', ln):
                continue
            if re.fullmatch(r'\t*(?:local )?%s = .*' % nm, ln) and len(re.findall(r'\b%s\b' % nm, ln)) == 1 \
                    and not re.fullmatch(r'\t*(?:local )?%s = [\d.]+' % nm, ln):
                real = True
                continue
            if 'bit32.' in ln or re.search(r'\b%s [-+*/%%^] |[-+*/%%^] %s\b|[=~<>]= %s\b|\b%s [=~<>]=' % (nm, nm, nm, nm), ln):
                ok = False
                break
        if ok and real:
            drop.update(idx)
    if not drop:
        return text
    return '\n'.join(ln for i, ln in enumerate(lines) if i not in drop)


def _drop_callback_opaque(text):
    """An opaque number handed to a callback: `local function g(cb) ... cb(123456789) end` where
    every call of g passes a function literal that never looks at that parameter."""
    counts = {}
    for m in re.finditer(r'\b[vp]\d+\b', text):
        counts[m.group(0)] = counts.get(m.group(0), 0) + 1
    for name, d in _functions(text).items():
        if len(d['defs']) != 1:
            continue
        ps, _ = d['defs'][0]
        for k, F in enumerate(ps):
            if not re.fullmatch(r'p\d+', F):
                continue
            sites = []
            ok = True
            for u in re.finditer(r'\b%s\b' % F, text):
                if text[u.end():u.end() + 1] != '(':
                    if text[u.end():u.end() + 1] in (',', ')') and re.search(r'function\([^)\n]*$', text[max(0, u.start() - 200):u.start()]):
                        continue        # (its place in the parameter list)
                    ok = False
                    break
                args, close = _call_args(text, u.end())
                if args is None:
                    ok = False
                    break
                sites.append(args)
            if not ok or not sites:
                continue
            for mi in range(max(len(a) for a in sites)):
                if not all(len(a) > mi and re.fullmatch(r'\s*\d{7,}\s*', text[a[mi][0]:a[mi][1]]) for a in sites):
                    continue
                cuts = []
                good = True
                for c in d['calls']:
                    if k >= len(c):
                        good = False
                        break
                    x, y = c[k]
                    lit = re.match(r'\s*function\(([^)\n]*)\)', text[x:y])
                    if not lit:
                        good = False
                        break
                    lps = [q.strip() for q in lit.group(1).split(',') if q.strip()]
                    if mi < len(lps):
                        if not re.fullmatch(r'p\d+', lps[mi]) or counts.get(lps[mi]) != 1:
                            good = False
                            break
                        cuts.append((x + lit.start(1), x + lit.end(1), ', '.join(lps[:mi] + lps[mi + 1:])))
                    elif '...' in lps:
                        good = False
                        break
                if not good:
                    continue
                for a in sites:
                    s0, e0 = a[mi]
                    while s0 < e0 and text[s0] in ' \t\n':
                        s0 += 1
                    if mi + 1 < len(a):
                        nx = a[mi + 1][0]
                        while text[nx:nx + 1] == ' ':
                            nx += 1
                        cuts.append((s0, nx, ''))
                    elif mi > 0:
                        cuts.append((a[mi - 1][1], e0, ''))
                    else:
                        cuts.append((s0, e0, ''))
                for c in sorted(cuts, reverse=True):
                    text = text[:c[0]] + c[2] + text[c[1]:]
                return text
    return text


def _inline_fn_alias(text):
    """`v9 = v3` right before `v9(...)`, where v9 is a variable that was free at that point (the
    register a call's function is loaded into): the call is `v3(...)`."""
    lines = text.split('\n')
    fnames = {m.group(1) or m.group(2) or m.group(3) for m in _FN_DEF.finditer(text)}
    for i in range(len(lines) - 1):
        m = re.fullmatch(r'(\t*)(v\d+) = (v\d+)', lines[i])
        if not m or m.group(3) not in fnames or m.group(2) in fnames:
            continue
        ind, alias, fn = m.groups()
        nxt = lines[i + 1]
        if not nxt.startswith(ind) or nxt.startswith(ind + '\t'):
            continue
        hits = [u for u in re.finditer(r'\b%s\b' % alias, nxt)]
        if len(hits) != 1 or nxt[hits[0].end():hits[0].end() + 1] != '(' or nxt[hits[0].start() - 1:hits[0].start()] in ('.', ':'):
            continue
        # the next thing that happens to the alias must be a new value
        later = None
        for j in range(i + 2, len(lines)):
            if re.search(r'\b%s\b' % alias, lines[j]):
                later = lines[j]
                break
        if later is not None and not (re.fullmatch(r'\t*(?:local )?%s = .*' % alias, later) and len(re.findall(r'\b%s\b' % alias, later)) == 1):
            continue
        lines[i + 1] = nxt[:hits[0].start()] + fn + nxt[hits[0].end():]
        del lines[i]
        return '\n'.join(lines)
    return text



def _drop_orphans(text):
    """Opaque numbers whose guards are gone: variables that are only ever set to a constant and
    updated from themselves, and never read by anything else."""
    lines = text.split('\n')
    occ = {}
    for i, ln in enumerate(lines):
        for name in set(re.findall(r'\b[vp]\d+\b', ln)):
            occ.setdefault(name, []).append(i)
    drop = set()
    for name, idx in occ.items():
        own = re.compile(r'\s*(?:local %s = \d+|%s = \d+|%s = bit32\.\w+\(%s, \d+\)|%s = %s [-+*] \d+)' % ((name,) * 6))
        if name[0] == 'v' and all(own.fullmatch(lines[i]) for i in idx) and any(
                re.fullmatch(r'\s*local %s = \d{6,}' % name, lines[i]) for i in idx):
            drop.update(idx)
    # what is left of a removed test: `local v = not not f`, never read
    for name, idx in occ.items():
        if len(idx) == 1 and name[0] == 'v':
            m = re.fullmatch(r'\s*local %s = ((?:not )+)([A-Za-z_]\w*)' % name, lines[idx[0]])
            if m and len(re.findall(r'\b%s\b' % name, lines[idx[0]])) == 1:
                drop.add(idx[0])
    if not drop:
        return text
    return '\n'.join(ln for i, ln in enumerate(lines) if i not in drop)


def _strip_once(text):
    lines = text.split('\n')
    cand, opaque_cond, strict = _candidates(lines)
    if not cand:
        return text

    def scramble(ln):
        s = ln.strip()
        m = re.fullmatch(r'(%s) [-+]= \d+' % ID, s)
        if m and m.group(1) in cand:
            return True
        m = re.fullmatch(r'(?:local )?(%s) = (%s) [-+] \d+' % (ID, ID), s)
        return bool(m and m.group(1) in cand and m.group(2) in cand)

    def junk(lo, hi, ind):
        """lines[lo:hi] is a statement list at indentation ind"""
        return any(_indent(lines[i]) == ind and (scramble(lines[i]) or 'LIFT ERROR' in lines[i])
                   for i in range(lo, hi))

    def block_end(i, ind):
        """first line after i with indentation <= ind"""
        j = i
        while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) > ind):
            j += 1
        return j

    def exits(lo, hi, ind):
        for i in range(hi - 1, lo - 1, -1):
            s = lines[i].strip()
            if s and _indent(lines[i]) == ind and not re.match(r'(end\b|[})\]])', s):
                return bool(re.match(r'(return|break|continue)\b', s))
        return False

    def dedent(lo, hi):
        return [ln[1:] if ln.startswith('\t') else ln for ln in lines[lo:hi]]

    for i, ln in enumerate(lines):
        m = re.fullmatch(r'\t*if (.*) then', ln)
        if not m or not opaque_cond(m.group(1)) or not re.search(r'\bv\d+\b', m.group(1)):
            continue
        ind = _indent(ln)
        a_end = block_end(i + 1, ind)
        if a_end >= len(lines):
            continue
        closer = lines[a_end].strip()
        if _indent(lines[a_end]) != ind:
            continue
        if closer == 'else':
            b_end = block_end(a_end + 1, ind)
            if b_end >= len(lines) or lines[b_end].strip() != 'end' or _indent(lines[b_end]) != ind:
                continue
            ja, jb = junk(i + 1, a_end, ind + 1), junk(a_end + 1, b_end, ind + 1)
            if ja == jb:
                continue
            keep = dedent(a_end + 1, b_end) if ja else dedent(i + 1, a_end)
            return '\n'.join(lines[:i] + keep + lines[b_end + 1:])
        m2 = re.fullmatch(r'elseif (.*) then', closer)
        if m2 and junk(i + 1, a_end, ind + 1):
            return '\n'.join(lines[:i] + ['\t' * ind + 'if ' + m2.group(1) + ' then'] + lines[a_end + 1:])
        if closer != 'end':
            continue
        rest_end = block_end(a_end + 1, ind - 1) if ind > 0 else len(lines)
        ja = junk(i + 1, a_end, ind + 1)
        jr = junk(a_end + 1, rest_end, ind)
        if ja and not jr:
            return '\n'.join(lines[:i] + lines[a_end + 1:])
        if jr and not ja and exits(i + 1, a_end, ind + 1):
            return '\n'.join(lines[:i] + dedent(i + 1, a_end) + lines[rest_end:])
    # no guard left to resolve: drop the opaque numbers that are no longer tested
    used = set()
    for ln in lines:
        m = re.fullmatch(r'\t*(?:if|elseif) (.*) then', ln)
        if m:
            used.update(re.findall(r'\bv\d+\b', m.group(1)))
    changed = True
    while changed:        # numbers feeding a tested number stay
        changed = False
        for ln in lines:
            m = re.fullmatch(r'\s*(?:local )?(%s) = (%s) [-+] \d+' % (ID, ID), ln)
            if m and m.group(1) in used and m.group(2) not in used:
                used.add(m.group(2))
                changed = True
    dead = strict - used
    out = []
    for ln in lines:
        s = ln.strip()
        m = re.fullmatch(r'(?:local )?(%s)(?: (?:[-+]?= .*))?' % ID, s)
        if m and m.group(1) in dead and (s.startswith('local ') or ' = ' in s or '+= ' in s or '-= ' in s):
            continue
        out.append(ln)
    return '\n'.join(out)


# ---------------------------------------------------------------------------------------------
# The same on the lifted instructions, before control flow is structured: junk arms jump back
# into real code in ways that make the structurer give up, so they are cut off here first.

def _is_big(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and float(v).is_integer() and abs(v) >= BIG


def _walk_stmts(tree):
    stmts, term = tree
    yield from stmts
    if term[0] == 'br':
        yield from _walk_stmts(term[2])
        yield from _walk_stmts(term[3])
    elif term[0] == 'forbr':
        yield from _walk_stmts(term[3])
        yield from _walk_stmts(term[4])


def _closures(e):
    if isinstance(e, tuple):
        if e and e[0] == 'closure':
            yield e
        for x in e:
            yield from _closures(x)
    elif isinstance(e, list):
        for x in e:
            yield from _closures(x)


def _ends(tree):
    t = tree[1]
    if t[0] == 'br':
        yield from _ends(t[2])
        yield from _ends(t[3])
    elif t[0] == 'forbr':
        yield from _ends(t[3])
        yield from _ends(t[4])
    elif t[0] == 'goto':
        yield t[1]


def cut_arms(tree, blocked):
    """Copy of an instruction tree without the branch arms that only lead to blocked targets."""
    stmts, term = tree
    if term[0] == 'br':
        a, b = cut_arms(term[2], blocked), cut_arms(term[3], blocked)
        ea, eb = set(_ends(a)), set(_ends(b))
        ja, jb = bool(ea) and ea <= blocked, bool(eb) and eb <= blocked
        if ja != jb:
            keep = b if ja else a
            return (list(stmts) + list(keep[0]), keep[1])
        return (stmts, ('br', term[1], a, b))
    if term[0] == 'forbr':
        return (stmts, term[:3] + (cut_arms(term[3], blocked), cut_arms(term[4], blocked)))
    return tree


def prune_ir(protos, cuts=None):
    """Cut branch arms that lead straight into code scrambling the opaque numbers.
    Returns the number of branches resolved; cuts[proto][instruction] collects the jump targets
    that were cut off, so that a second lift can leave those paths alone."""
    parent = {}
    for key, pir in protos.items():
        for node in pir.nodes.values():
            for st in _walk_stmts(node.tree):
                for c in _closures(st):
                    parent.setdefault(c[1], (key, c[2]))

    def resolve(key, n, depth=0):
        """the variable behind upvalue n of proto key: (owner proto, register)"""
        p = parent.get(key)
        if p is None or depth > 50 or not isinstance(n, int) or n >= len(p[1]):
            return ('?', key, n)
        cap = p[1][n]
        if cap[0] == 'ref' and isinstance(cap[1], int):
            return (p[0], cap[1])
        if cap[0] == 'up':
            return resolve(p[0], cap[1], depth + 1)
        return ('?', key, n)

    big, small = set(), set()
    for key, pir in protos.items():
        for node in pir.nodes.values():
            for st in _walk_stmts(node.tree):
                if st[0] != 'set' or st[1][0] not in ('r', 'up') or not isinstance(st[2], tuple) or st[2][0] != 'k':
                    continue
                var = (key, st[1][1]) if st[1][0] == 'r' else resolve(key, st[1][1])
                v = st[2][1]
                if _is_big(v):
                    big.add(var)
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    # (strings and booleans are the register's earlier life as a temporary)
                    small.add(var)
    opq = big - small
    if not opq:
        return 0

    def scrambles(key, stmts):
        temps = {}
        for st in stmts:
            if st[0] != 'set' or not isinstance(st[2], tuple):
                continue
            dst, e = st[1], st[2]
            if dst[0] == 't':
                if e[0] == 'r':
                    temps[dst[1]] = ('src', e[1])
                elif e[0] == 'bin' and e[1] in ('+', '-') and e[2][0] == 't' and e[3][0] == 'k' \
                        and temps.get(e[2][1], ('', None))[0] == 'src':
                    temps[dst[1]] = ('arith', temps[e[2][1]][1])
            elif dst[0] == 'r' and e[0] == 't' and temps.get(e[1]) == ('arith', dst[1]) and (key, dst[1]) in opq:
                return True
            elif dst[0] == 'up' and e[0] != 'k' and resolve(key, dst[1]) in opq:
                return True
        return False

    def touches(key, stmts):
        """writes one of the opaque numbers, or could not be lifted"""
        for st in stmts:
            if st[0] == 'error':
                return True
            if st[0] == 'set' and st[1][0] == 'up' and resolve(key, st[1][1]) in opq:
                return True
            if st[0] == 'set' and st[1][0] == 'r' and (key, st[1][1]) in opq and isinstance(st[2], tuple) \
                    and st[2][0] == 'k' and st[2][1] is not None:
                return True
        return False

    total = 0
    for key, pir in protos.items():
        nodes = pir.nodes
        scr = {k for k, n in nodes.items() if scrambles(key, n.tree[0])}
        tch = {k for k, n in nodes.items() if touches(key, n.tree[0])}
        # registers that hold nothing but opaque numbers in this function
        strict = {r for (kk, r) in opq if kk == key}
        for n in nodes.values():
            for st in _walk_stmts(n.tree):
                if st[0] == 'setmulti':
                    strict = {r for r in strict if r < st[1]} if isinstance(st[1], int) else set()
                if st[0] != 'set' or st[1][0] != 'r' or st[1][1] not in strict or not isinstance(st[2], tuple):
                    continue
                e = st[2]
                if e[0] == 'k' and (e[1] is None or _is_big(e[1])):
                    continue
                if e[0] == 't' and scrambles(key, n.tree[0]):
                    continue
                strict.discard(st[1][1])
        preds = {}
        for k, n in nodes.items():
            for t in _ends(n.tree):
                preds.setdefault(t, []).append(k)

        def reg_opaque(k, r):
            """register r holds an opaque number on entry to node k (looking back along the only path)"""
            for _ in range(12):
                ps = preds.get(k, ())
                if len(ps) != 1:
                    return False
                k = ps[0]
                temps = {}
                found = None
                for st in nodes[k].tree[0]:
                    if st[0] == 'set' and isinstance(st[2], tuple):
                        if st[1][0] == 't':
                            temps[st[1][1]] = st[2]
                        elif st[1] == ('r', r):
                            e = st[2]
                            found = temps.get(e[1], e) if e[0] == 't' else e
                    elif st[0] in ('setmulti', 'call'):
                        found = found or ('?',)
                if found is not None:
                    return (found[0] == 'up' and resolve(key, found[1]) in opq) or (found[0] == 'k' and _is_big(found[1]))
            return False

        def opaque_branch(k, stmts, cond):
            temps = {st[1][1]: st[2] for st in stmts if st[0] == 'set' and st[1][0] == 't' and isinstance(st[2], tuple)}

            def op(e, depth=0):
                if e[0] == 't' and e[1] in temps and depth < 8:
                    return op(temps[e[1]], depth + 1)
                if e[0] == 'un' and e[1] == 'not':
                    return op(e[2], depth + 1)
                if e[0] == 'bin' and e[1] in ('==', '~=', '<', '<=', '>', '>='):
                    x, y = op(e[2], depth + 1), op(e[3], depth + 1)
                    return x and y and 'var' in (x, y) and 'var'
                if e[0] == 'k':
                    return _is_big(e[1]) and 'k'
                if e[0] == 'up':
                    return resolve(key, e[1]) in opq and 'var'
                if e[0] == 'r':
                    return (e[1] in strict or reg_opaque(k, e[1])) and 'var'
                return False
            e = cond
            for _ in range(16):         # only comparisons are guards, not truth tests of a value
                if e[0] == 't' and e[1] in temps:
                    e = temps[e[1]]
                elif e[0] == 'un' and e[1] == 'not':
                    e = e[2]
                else:
                    break
            return e[0] == 'bin' and op(cond) == 'var'

        def linear_hits(tree, marks):
            stmts, term = tree
            if touches(key, stmts):
                return True
            seen = set()
            while term[0] == 'goto':
                k2 = term[1]
                if k2 in seen or k2 not in nodes or len(seen) > 400:
                    return False
                seen.add(k2)
                if k2 in marks:
                    return True
                term = nodes[k2].tree[1]
            return False

        def junk_tree(tree):
            stmts, term = tree
            if scrambles(key, stmts):
                return True
            seen = set()
            while term[0] == 'goto':
                k = term[1]
                if k in seen or k not in nodes or len(seen) > 400:
                    return False
                seen.add(k)
                if k in scr:
                    return True
                term = nodes[k].tree[1]
            return False

        def fix(tree, k=None):
            nonlocal total
            stmts, term = tree
            if term[0] == 'br':
                a, b = fix(term[2], k), fix(term[3], k)
                ja, jb = junk_tree(a), junk_tree(b)
                rule = 'scramble'
                if ja == jb and k is not None and opaque_branch(k, stmts, term[1]):
                    # a guard between opaque numbers: the arm that goes on to rewrite them is the junk
                    ja, jb = linear_hits(a, tch), linear_hits(b, tch)
                    rule = 'guard'
                if ja != jb:
                    total += 1
                    if os.environ.get('DEVM_TRACE_OPAQUE'):
                        print('OPAQUE', key, k, rule, [st for st in stmts][-3:], term[1])
                    keep = b if ja else a
                    if cuts is not None and k is not None:
                        gone = set(_ends(a if ja else b))
                        if gone:
                            cuts.setdefault(key, {}).setdefault(k[:2], set()).update(gone)
                    return (list(stmts) + list(keep[0]), keep[1])
                return (stmts, ('br', term[1], a, b))
            if term[0] == 'forbr':
                return (stmts, term[:3] + (fix(term[3], k), fix(term[4], k)))
            return tree

        for _ in range(8):
            before = total
            for k, n in nodes.items():
                if n.tree[1][0] in ('br', 'forbr'):
                    n.tree = fix(n.tree, k)
            if total == before:
                break
        # drop what is no longer reachable
        seen, stack = set(), [pir.entry]
        while stack:
            k = stack.pop()
            if k in seen or k not in nodes:
                continue
            seen.add(k)

            def ends(tree):
                t = tree[1]
                if t[0] == 'br':
                    yield from ends(t[2])
                    yield from ends(t[3])
                elif t[0] == 'forbr':
                    yield from ends(t[3])
                    yield from ends(t[4])
                elif t[0] == 'goto':
                    yield t[1]
            stack.extend(ends(nodes[k].tree))
        for k in [k for k in nodes if k not in seen]:
            del nodes[k]
        pir.errors = [e for e in pir.errors if e[0] in seen]
    return total


def param_literals(text):
    """{proto id: {parameter index: number}} for the functions (tagged `-- proto N`) every call
    of which passes the same big literal number in that position: opaque state numbers.  A
    function that never ran takes decryption keys of its own code from them."""
    out = {}
    for m in re.finditer(r'(?m)^[ \t]*(?:local (v\d+) = function|local function (v\d+)|(v\d+) = function)'
                         r'\(([^)\n]*)\) -- proto (\d+)$', text):
        name = m.group(1) or m.group(2) or m.group(3)
        nparams = len([x for x in m.group(4).split(',') if x.strip()])
        calls = []
        for c in re.finditer(r'(?<![\w.:])%s\(' % name, text):
            args, close = _call_args(text, c.end() - 1)
            if args is None:
                calls = None
                break
            calls.append(args)
        if not calls:
            continue
        vals = {}
        for k in range(nparams):
            seen = set()
            for args in calls:
                a = args[k] if k < len(args) else None
                a = text[a[0]:a[1]].strip() if isinstance(a, tuple) else (a.strip() if isinstance(a, str) else None)
                seen.add(a)
            if len(seen) == 1:
                a = next(iter(seen))
                if a and re.fullmatch(r'\d+', a) and int(a) >= 2 ** 24:
                    vals[k] = int(a)
        if vals:
            out[int(m.group(5))] = vals
    return out


def relift_params(lift, protos, root, native_text=None, rounds=6, log=None):
    """Functions that never ran and whose code is decrypted with numbers their callers pass:
    find those numbers in the decompiled callers and lift the functions again."""
    from . import structure
    done = {}
    for _ in range(rounds):
        saved = os.environ.get('DEVM_PROTO_IDS')
        os.environ['DEVM_PROTO_IDS'] = '1'
        try:
            text = structure.Decompiler(protos, root, native_text).run()
        finally:
            if saved is None:
                del os.environ['DEVM_PROTO_IDS']
            else:
                os.environ['DEVM_PROTO_IDS'] = saved
        def below(k, seen=None):
            seen = set() if seen is None else seen
            for c in getattr(protos.get(k), 'children', ()) or ():
                if c not in seen and c in protos:
                    seen.add(c)
                    below(c, seen)
            return seen

        # (the number may also be handed on to a nested function that needs it)
        # (also where nothing failed: the function's own guards on the number fold away)
        vals = {k: v for k, v in param_literals(text).items()
                if k in protos and done.get(k) != v}
        if not vals:
            break
        for k, v in vals.items():
            if k not in protos:
                continue
            done[k] = v
            if log:
                log('lifting proto %s again with parameters %s' % (k, v))
            sub = below(k)
            keep = {x: q for x, q in protos.items() if x not in sub}
            before = len(protos[k].errors) + sum(len(protos[c].errors) for c in sub)
            new = lift.lift_all(k, redo_into=keep, param_vals={k: v})
            after = len(new[k].errors) + sum(len(new[c].errors) for c in new if c not in keep)
            if after <= before:
                protos = new
            elif log:
                log('  (kept as it was: %d errors instead of %d)' % (after, before))
    return protos
