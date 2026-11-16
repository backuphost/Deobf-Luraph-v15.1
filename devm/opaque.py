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
    return text


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
