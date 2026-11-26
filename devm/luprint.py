"""luparse AST -> Lua source (used for non-virtualised functions kept by Luraph)."""
from .structure import lua_string, lua_number, IDENT, KEYWORDS, PREC, RIGHT


class SrcPrinter:
    def __init__(self, rewrite=None):
        self.rewrite = rewrite or (lambda e: None)
        self.lines = []
        self.level = 0

    def emit(self, s):
        self.lines.append('\t' * self.level + s)

    def expr(self, e, prec=0):
        r = self.rewrite(e)
        if r is not None:
            return r
        t = e[0]
        if t == 'nil':
            return 'nil'
        if t == 'true':
            return 'true'
        if t == 'false':
            return 'false'
        if t == 'vararg':
            return '...'
        if t == 'num':
            s = lua_number(e[1])
            return '(' + s + ')' if s.startswith('-') and prec > PREC['unary'] else s
        if t == 'str':
            return lua_string(e[1])
        if t == 'name':
            return e[1]
        if t == 'paren':
            return '(' + self.expr(e[1]) + ')'
        if t == 'index':
            k = e[2]
            if k[0] == 'str' and IDENT.match(k[1]) and k[1] not in KEYWORDS:
                return self.prefix(e[1]) + '.' + k[1]
            return self.prefix(e[1]) + '[' + self.expr(k) + ']'
        if t == 'call':
            return self.prefix(e[1]) + '(' + ', '.join(self.expr(a) for a in e[2]) + ')'
        if t == 'mcall':
            return self.prefix(e[1]) + ':' + e[2] + '(' + ', '.join(self.expr(a) for a in e[3]) + ')'
        if t == 'bin':
            op = e[1]
            p = PREC[op]
            if op in RIGHT:
                s = self.expr(e[2], p + 1) + ' ' + op + ' ' + self.expr(e[3], p)
            else:
                s = self.expr(e[2], p) + ' ' + op + ' ' + self.expr(e[3], p + 1)
            return '(' + s + ')' if p < prec else s
        if t == 'un':
            p = PREC['unary']
            inner = self.expr(e[2], p)
            s = ('not ' + inner) if e[1] == 'not' else (e[1] + inner)
            return '(' + s + ')' if p < prec else s
        if t == 'table':
            items = []
            for kind, k, v in e[1]:
                if kind == 'pos':
                    items.append(self.expr(v))
                else:
                    items.append('[%s] = %s' % (self.expr(k), self.expr(v)))
            return '{ ' + ', '.join(items) + ' }' if items else '{}'
        if t == 'ifexpr':
            return '(if %s then %s else %s)' % (self.expr(e[1]), self.expr(e[2]), self.expr(e[3]))
        if t == 'func':
            return self.func(e[1])
        raise ValueError('print expr ' + t)

    def prefix(self, e):
        s = self.expr(e, 9)
        if e[0] in ('name', 'index', 'call', 'mcall', 'paren') or s.startswith('('):
            return s
        return '(' + s + ')'

    def func(self, f, name=None):
        sub = SrcPrinter(self.rewrite)
        sub.level = self.level
        sub.block(f.body)
        params = list(f.params) + (['...'] if f.vararg else [])
        return 'function%s(%s)\n%s\n%send' % (' ' + name if name else '', ', '.join(params),
                                              '\n'.join(sub.lines), '\t' * self.level)

    def block(self, stmts):
        self.level += 1
        for st in stmts:
            self.stmt(st)
        self.level -= 1

    def stmt(self, st):
        def semi(text):
            # a statement that starts with a parenthesis would continue the line before it
            # (`f()` newline `(g or h)()` is one call chain to the parser)
            return ';' + text if text.startswith('(') else text
        t = st[0]
        E = self.expr
        if t == 'local':
            self.emit('local ' + ', '.join(st[1]) + (' = ' + ', '.join(E(e) for e in st[2]) if st[2] else ''))
        elif t == 'assign':
            self.emit(semi(', '.join(E(e) for e in st[1]) + ' = ' + ', '.join(E(e) for e in st[2])))
        elif t == 'opassign':
            self.emit(semi('%s %s= %s' % (E(st[2]), st[1], E(st[3]))))
        elif t == 'callstmt':
            self.emit(semi(E(st[1])))
        elif t == 'do':
            self.emit('do'); self.block(st[1]); self.emit('end')
        elif t == 'while':
            self.emit('while %s do' % E(st[1])); self.block(st[2]); self.emit('end')
        elif t == 'repeat':
            self.emit('repeat'); self.block(st[1]); self.emit('until ' + E(st[2]))
        elif t == 'if':
            for i, (c, b) in enumerate(st[1]):
                self.emit(('if %s then' if i == 0 else 'elseif %s then') % E(c))
                self.block(b)
            if st[2]:
                self.emit('else'); self.block(st[2])
            self.emit('end')
        elif t == 'fornum':
            self.emit('for %s = %s do' % (st[1], ', '.join(E(e) for e in st[2:5] if e)))
            self.block(st[5]); self.emit('end')
        elif t == 'forin':
            self.emit('for %s in %s do' % (', '.join(st[1]), ', '.join(E(e) for e in st[2])))
            self.block(st[3]); self.emit('end')
        elif t == 'localfunc':
            self.emit('local ' + self.func(st[2], st[1]))
        elif t == 'funcstmt':
            self.emit(E(st[1]) + ' = ' + self.func(st[2]))
        elif t == 'return':
            self.emit('return' + (' ' + ', '.join(E(e) for e in st[1]) if st[1] else ''))
        elif t == 'break':
            self.emit('break')
        elif t == 'continue':
            self.emit('continue')
        else:
            raise ValueError('print stmt ' + t)


def native_shape(func, underscore_is_name=False):
    """(upvalue-list parameter, {alias: index}, inner Func) for a non-virtualised function kept by
    Luraph: `function(up) local a = up[i] ... return function(...) ... end end`; None otherwise.
    Minified builds repeat parameter names (the last one wins) and shadow the unused ones."""
    if not func.params:
        return None
    alias = {}
    inner = None
    ups = set()
    for st in func.body:
        if st[0] == 'local' and len(st[1]) == 1 and len(st[2]) == 1 and st[2][0][0] == 'index' \
                and st[2][0][1][0] == 'name' and st[2][0][2][0] == 'num':
            alias[st[1][0]] = st[2][0][2][1]
            ups.add(st[2][0][1][1])
        elif st[0] == 'return' and len(st[1]) == 1 and st[1][0][0] == 'func':
            inner = st[1][0][1]
        else:
            return None
    if inner is None or len(ups) > 1:
        return None
    up = next(iter(ups)) if ups else func.params[0]
    if up not in func.params:
        return None
    return up, alias, inner


def native_closure_text(func, upnames, level, cells=(1, 3), base=0):
    """Source of the inner function with upvalue-cell accesses replaced by the captured names."""
    shape = native_shape(func)
    if shape is None:
        return None
    up, alias, inner = shape
    alias = {k: v - base for k, v in alias.items()}
    box, slot = ('num', cells[0]), ('num', cells[1])

    def upname(i):
        return upnames[i] if 0 <= i < len(upnames) else 'UPVAL_%d' % i

    def rewrite(e):
        # box access: a[box][a[slot]]
        if e[0] == 'index' and e[1][0] == 'index' and e[1][2] == box and e[1][1][0] == 'name' \
                and e[1][1][1] in alias and e[2] == ('index', e[1][1], slot):
            return upname(alias[e[1][1][1]])
        if e[0] == 'name' and e[1] in alias:
            return upname(alias[e[1]])
        if e[0] == 'index' and e[1] == ('name', up) and e[2][0] == 'num':
            return upname(e[2][1] - base)
        return None

    p = SrcPrinter(rewrite)
    p.level = level
    return p.func(inner)
