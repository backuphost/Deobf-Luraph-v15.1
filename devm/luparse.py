"""Minimal Luau parser (enough for decompiler output of Luraph VMs).

AST is made of plain tuples; the first element is the node tag.

Expressions
  ('nil',) ('true',) ('false',) ('vararg',)
  ('num', float|int) ('str', str[latin-1 bytes]) ('name', id)
  ('index', obj, key) ('call', f, [args]) ('mcall', obj, name, [args])
  ('func', Func) ('bin', op, a, b) ('un', op, a)
  ('table', [(kind, key, value)])   kind: 'pos' | 'kv'
  ('ifexpr', cond, a, b) ('paren', e)
Statements
  ('local', [names], [exprs]) ('assign', [targets], [exprs])
  ('opassign', op, target, expr) ('callstmt', expr) ('do', body)
  ('while', cond, body) ('repeat', body, cond)
  ('if', [(cond, body)], else_body|None)
  ('fornum', var, a, b, c|None, body) ('forin', [names], [exprs], body)
  ('localfunc', name, Func) ('funcstmt', target_expr, Func)
  ('return', [exprs]) ('break',) ('continue',)
"""
import re


class Func:
    __slots__ = ('params', 'vararg', 'body', 'line', 'body_pos', 'end_line', 'id', 'kw_pos', 'end_pos', 'name_stmt', 'fid',
                 'stmt_name')
    _n = 0

    def __init__(self, params, vararg, body, line, body_pos, end_line):
        self.params = params
        self.vararg = vararg
        self.body = body
        self.line = line
        self.body_pos = body_pos
        self.end_line = end_line
        self.name_stmt = None     # NAME when declared as `local function NAME`
        self.stmt_name = None     # `a.b.c` when declared by the statement `function a.b.c()` / `function a.b:c()`
        Func._n += 1
        self.id = Func._n

    def __repr__(self):
        return '<Func L%d %s>' % (self.line, self.params)


KEYWORDS = {
    'and', 'break', 'do', 'else', 'elseif', 'end', 'false', 'for', 'function',
    'if', 'in', 'local', 'nil', 'not', 'or', 'repeat', 'return', 'then',
    'true', 'until', 'while',
}

TOKEN_RE = re.compile(r'''
    (?P<ws>\s+)
  | (?P<comment>--\[(?P<ceq>=*)\[.*?\](?P=ceq)\]|--[^\n]*)
  | (?P<num>0[xX][0-9a-fA-F_]+|0[bB][01_]+|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<lstr>\[(?P<seq>=*)\[.*?\](?P=seq)\])
  | (?P<str>"(?:[^"\\\n]|\\.|\\\n)*"|'(?:[^'\\\n]|\\.|\\\n)*')
  | (?P<op>\.\.\.|\.\.=|\.\.|==|~=|<=|>=|//=|//|::|->|\+=|-=|\*=|/=|%=|\^=|[-+*/%^\#<>=(){}\[\];:,.?|&])
''', re.S | re.X)


def unescape(s):
    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c != '\\':
            out.append(c)
            i += 1
            continue
        i += 1
        c = s[i]
        if c == 'n': out.append('\n'); i += 1
        elif c == 't': out.append('\t'); i += 1
        elif c == 'r': out.append('\r'); i += 1
        elif c == 'a': out.append('\a'); i += 1
        elif c == 'b': out.append('\b'); i += 1
        elif c == 'f': out.append('\f'); i += 1
        elif c == 'v': out.append('\v'); i += 1
        elif c == 'x':
            out.append(chr(int(s[i + 1:i + 3], 16))); i += 3
        elif c == 'z':
            i += 1
            while i < n and s[i] in ' \t\r\n':
                i += 1
        elif c == 'u':
            j = s.index('}', i)
            cp = int(s[i + 2:j], 16)
            out.append(chr(cp).encode('utf-8').decode('latin-1')); i = j + 1
        elif c.isdigit():
            j = i
            while j < n and j < i + 3 and s[j].isdigit():
                j += 1
            out.append(chr(int(s[i:j]))); i = j
        elif c == '\n':
            out.append('\n'); i += 1
        else:
            out.append(c); i += 1
    return ''.join(out)


def tokenize(src):
    toks = []
    pos = 0
    line = 1
    n = len(src)
    m = TOKEN_RE.match
    while pos < n:
        mo = m(src, pos)
        if not mo:
            raise SyntaxError('bad token at line %d: %r' % (line, src[pos:pos + 30]))
        kind = mo.lastgroup
        text = mo.group(kind)
        if kind in ('ws', 'comment'):
            pass
        elif kind == 'num':
            t = text.replace('_', '')
            if t[:2] in ('0x', '0X'):
                v = int(t, 16)
            elif t[:2] in ('0b', '0B'):
                v = int(t[2:], 2)
            else:
                v = float(t)
                if v.is_integer() and abs(v) < 2 ** 53:
                    v = int(v)
            toks.append(('num', v, line, pos))
        elif kind == 'name':
            toks.append(('kw' if text in KEYWORDS else 'name', text, line, pos))
        elif kind == 'lstr':
            eq = len(mo.group('seq'))
            body = text[eq + 2:-(eq + 2)]
            if body.startswith('\n'):
                body = body[1:]
            toks.append(('str', body, line, pos))
        elif kind == 'str':
            toks.append(('str', unescape(text[1:-1]), line, pos))
        else:
            toks.append(('op', text, line, pos))
        line += text.count('\n')
        pos = mo.end()
    toks.append(('eof', None, line, pos))
    return toks


BINPRI = {
    'or': (1, 1), 'and': (2, 2),
    '<': (3, 3), '>': (3, 3), '<=': (3, 3), '>=': (3, 3), '~=': (3, 3), '==': (3, 3),
    '..': (9, 8),
    '+': (10, 10), '-': (10, 10),
    '*': (11, 11), '/': (11, 11), '//': (11, 11), '%': (11, 11),
    '^': (14, 13),
}
UNARY_PRI = 12
COMPOUND = {'+=': '+', '-=': '-', '*=': '*', '/=': '/', '//=': '//', '%=': '%', '^=': '^', '..=': '..'}


class Parser:
    def __init__(self, src):
        self.src = src
        self.toks = tokenize(src)
        self.i = 0
        self.funcs = []

    # -- helpers
    def peek(self):
        return self.toks[self.i]

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def check(self, kind, val=None):
        t = self.toks[self.i]
        return t[0] == kind and (val is None or t[1] == val)

    def accept(self, kind, val=None):
        if self.check(kind, val):
            return self.next()
        return None

    def expect(self, kind, val=None):
        t = self.next()
        if t[0] != kind or (val is not None and t[1] != val):
            raise SyntaxError('line %d: expected %s %s, got %r' % (t[2], kind, val, t[:2]))
        return t

    # -- types (parsed and discarded)
    def skip_type(self):
        self.skip_simple_type()
        while self.check('op', '|') or self.check('op', '&'):
            self.next()
            self.skip_simple_type()

    def skip_simple_type(self):
        self.accept('op', '|')
        self.accept('op', '&')
        t = self.peek()
        if t[0] == 'op' and t[1] == '(':
            self.skip_balanced('(', ')')
            if self.accept('op', '->'):
                self.skip_type()
        elif t[0] == 'op' and t[1] == '{':
            self.skip_balanced('{', '}')
        elif t[0] == 'op' and t[1] == '...':
            self.next()
            self.skip_type()
        elif t[0] in ('name', 'str') or (t[0] == 'kw' and t[1] in ('nil', 'true', 'false', 'function')):
            self.next()
            if t[1] == 'typeof' and self.check('op', '('):
                self.skip_balanced('(', ')')
            while self.accept('op', '.'):
                self.next()
            if self.check('op', '<'):
                self.skip_balanced('<', '>')
        else:
            raise SyntaxError('line %d: bad type %r' % (t[2], t[:2]))
        while self.accept('op', '?'):
            pass

    def skip_balanced(self, o, c):
        depth = 0
        while True:
            t = self.next()
            if t[0] == 'op':
                if t[1] == o:
                    depth += 1
                elif t[1] == c:
                    depth -= 1
                    if depth == 0:
                        return
                elif t[1] == '->' and o == '<':
                    pass
            elif t[0] == 'eof':
                raise SyntaxError('unbalanced type')

    # -- statements
    def block(self):
        stmts = []
        while True:
            t = self.peek()
            if t[0] == 'eof' or (t[0] == 'kw' and t[1] in ('end', 'else', 'elseif', 'until')):
                break
            if t[0] == 'kw' and t[1] == 'return':
                self.next()
                exprs = []
                t2 = self.peek()
                if not (t2[0] == 'eof' or (t2[0] == 'kw' and t2[1] in ('end', 'else', 'elseif', 'until')) or (t2[0] == 'op' and t2[1] == ';')):
                    exprs = self.exprlist()
                self.accept('op', ';')
                stmts.append(('return', exprs))
                break
            s = self.statement()
            if s is not None:
                stmts.append(s)
        return stmts

    def statement(self):
        t = self.peek()
        if t[0] == 'op' and t[1] == ';':
            self.next()
            return None
        if t[0] == 'kw':
            k = t[1]
            if k == 'local':
                self.next()
                if self.accept('kw', 'function'):
                    name = self.expect('name')[1]
                    f = self.funcbody(t[2])
                    f.name_stmt = name
                    return ('localfunc', name, f)
                names = []
                while True:
                    names.append(self.expect('name')[1])
                    if self.accept('op', ':'):
                        self.skip_type()
                    if not self.accept('op', ','):
                        break
                exprs = []
                if self.accept('op', '='):
                    exprs = self.exprlist()
                return ('local', names, exprs)
            if k == 'if':
                self.next()
                clauses = []
                cond = self.expr()
                self.expect('kw', 'then')
                clauses.append((cond, self.block()))
                els = None
                while True:
                    if self.accept('kw', 'elseif'):
                        cond = self.expr()
                        self.expect('kw', 'then')
                        clauses.append((cond, self.block()))
                    elif self.accept('kw', 'else'):
                        els = self.block()
                        self.expect('kw', 'end')
                        break
                    else:
                        self.expect('kw', 'end')
                        break
                return ('if', clauses, els)
            if k == 'while':
                self.next()
                cond = self.expr()
                self.expect('kw', 'do')
                body = self.block()
                self.expect('kw', 'end')
                return ('while', cond, body)
            if k == 'do':
                self.next()
                body = self.block()
                self.expect('kw', 'end')
                return ('do', body)
            if k == 'for':
                self.next()
                names = [self.expect('name')[1]]
                if self.accept('op', ':'):
                    self.skip_type()
                if self.accept('op', '='):
                    a = self.expr()
                    self.expect('op', ',')
                    b = self.expr()
                    c = None
                    if self.accept('op', ','):
                        c = self.expr()
                    self.expect('kw', 'do')
                    body = self.block()
                    self.expect('kw', 'end')
                    return ('fornum', names[0], a, b, c, body)
                while self.accept('op', ','):
                    names.append(self.expect('name')[1])
                    if self.accept('op', ':'):
                        self.skip_type()
                self.expect('kw', 'in')
                exprs = self.exprlist()
                self.expect('kw', 'do')
                body = self.block()
                self.expect('kw', 'end')
                return ('forin', names, exprs, body)
            if k == 'repeat':
                self.next()
                body = self.block()
                self.expect('kw', 'until')
                cond = self.expr()
                return ('repeat', body, cond)
            if k == 'function':
                self.next()
                target = ('name', self.expect('name')[1])
                path = [target[1]]
                while True:
                    if self.accept('op', '.'):
                        target = ('index', target, ('str', self.expect('name')[1]))
                        path.append(target[2][1])
                    elif self.accept('op', ':'):
                        target = ('index', target, ('str', self.expect('name')[1]))
                        path.append(target[2][1])
                        f = self.funcbody(t[2])
                        f.params.insert(0, 'self')
                        f.stmt_name = '.'.join(path)
                        return ('funcstmt', target, f)
                    else:
                        break
                f = self.funcbody(t[2])
                f.stmt_name = '.'.join(path)
                return ('funcstmt', target, f)
            if k == 'break':
                self.next()
                return ('break',)
        if t[0] == 'name' and t[1] == 'continue':
            nt = self.toks[self.i + 1]
            if not (nt[0] == 'op' and nt[1] in ('=', '.', '[', '(', ':', ',', '+=', '-=', '*=', '/=', '..=', '{')) and nt[0] != 'str':
                self.next()
                return ('continue',)
        # expression statement
        e = self.suffixedexp()
        t = self.peek()
        if t[0] == 'op' and (t[1] == '=' or t[1] == ','):
            targets = [e]
            while self.accept('op', ','):
                targets.append(self.suffixedexp())
            self.expect('op', '=')
            return ('assign', targets, self.exprlist())
        if t[0] == 'op' and t[1] in COMPOUND:
            self.next()
            return ('opassign', COMPOUND[t[1]], e, self.expr())
        if e[0] not in ('call', 'mcall'):
            raise SyntaxError('line %d: syntax error near %r' % (t[2], t[:2]))
        return ('callstmt', e)

    def funcbody(self, line):
        kw_pos = self.toks[self.i - 1][3]
        if self.check('op', '<'):
            self.skip_balanced('<', '>')
        self.expect('op', '(')
        params = []
        vararg = False
        if not self.check('op', ')'):
            while True:
                if self.accept('op', '...'):
                    vararg = True
                    if self.accept('op', ':'):
                        self.skip_type()
                    break
                params.append(self.expect('name')[1])
                if self.accept('op', ':'):
                    self.skip_type()
                if not self.accept('op', ','):
                    break
        self.expect('op', ')')
        if self.accept('op', ':'):
            self.skip_type()
        body_pos = self.peek()[3]
        body = self.block()
        endt = self.expect('kw', 'end')
        f = Func(params, vararg, body, line, body_pos, endt[2])
        f.kw_pos = kw_pos
        f.end_pos = endt[3] + 3
        self.funcs.append(f)
        return f

    # -- expressions
    def exprlist(self):
        es = [self.expr()]
        while self.accept('op', ','):
            es.append(self.expr())
        return es

    def primaryexp(self):
        t = self.next()
        if t[0] == 'name':
            return ('name', t[1])
        if t[0] == 'op' and t[1] == '(':
            e = self.expr()
            self.expect('op', ')')
            return ('paren', e)
        raise SyntaxError('line %d: unexpected %r' % (t[2], t[:2]))

    def suffixedexp(self):
        e = self.primaryexp()
        while True:
            t = self.peek()
            if t[0] == 'op':
                if t[1] == '.':
                    self.next()
                    nm = self.next()
                    e = ('index', e, ('str', nm[1]))
                    continue
                if t[1] == '[':
                    self.next()
                    k = self.expr()
                    self.expect('op', ']')
                    e = ('index', e, k)
                    continue
                if t[1] == ':':
                    self.next()
                    nm = self.next()[1]
                    e = ('mcall', e, nm, self.callargs())
                    continue
                if t[1] == '(' or t[1] == '{':
                    e = ('call', e, self.callargs())
                    continue
            elif t[0] == 'str':
                e = ('call', e, self.callargs())
                continue
            return e

    def callargs(self):
        t = self.peek()
        if t[0] == 'str':
            self.next()
            return [('str', t[1])]
        if t[0] == 'op' and t[1] == '{':
            return [self.tablecons()]
        self.expect('op', '(')
        if self.accept('op', ')'):
            return []
        args = self.exprlist()
        self.expect('op', ')')
        return args

    def tablecons(self):
        self.expect('op', '{')
        items = []
        while not self.check('op', '}'):
            t = self.peek()
            if t[0] == 'op' and t[1] == '[':
                self.next()
                k = self.expr()
                self.expect('op', ']')
                self.expect('op', '=')
                items.append(('kv', k, self.expr()))
            elif t[0] == 'name' and self.toks[self.i + 1][:2] == ('op', '='):
                self.next()
                self.next()
                items.append(('kv', ('str', t[1]), self.expr()))
            else:
                items.append(('pos', None, self.expr()))
            if not (self.accept('op', ',') or self.accept('op', ';')):
                break
        self.expect('op', '}')
        return ('table', items)

    def simpleexp(self):
        t = self.peek()
        k = t[0]
        if k == 'num':
            self.next()
            return ('num', t[1])
        if k == 'str':
            self.next()
            return ('str', t[1])
        if k == 'kw':
            v = t[1]
            if v == 'nil':
                self.next(); return ('nil',)
            if v == 'true':
                self.next(); return ('true',)
            if v == 'false':
                self.next(); return ('false',)
            if v == 'function':
                self.next()
                return ('func', self.funcbody(t[2]))
            if v == 'if':
                self.next()
                c = self.expr()
                self.expect('kw', 'then')
                a = self.expr()
                while self.check('kw', 'elseif'):
                    # nested via recursion
                    self.toks[self.i] = ('kw', 'if', self.toks[self.i][2], self.toks[self.i][3])
                    b = self.simpleexp()
                    return ('ifexpr', c, a, b)
                self.expect('kw', 'else')
                b = self.expr()
                return ('ifexpr', c, a, b)
        if k == 'op':
            if t[1] == '...':
                self.next()
                return ('vararg',)
            if t[1] == '{':
                return self.tablecons()
        return self.suffixedexp()

    def expr(self, limit=0):
        t = self.peek()
        if (t[0] == 'kw' and t[1] == 'not') or (t[0] == 'op' and t[1] in ('-', '#')):
            self.next()
            operand = self.expr(UNARY_PRI)
            if t[1] == '-' and operand[0] == 'num':
                left = ('num', -operand[1])
            else:
                left = ('un', t[1], operand)
        else:
            left = self.simpleexp()
            while self.accept('op', '::'):
                self.skip_type()
        while True:
            t = self.peek()
            op = t[1]
            if t[0] not in ('op', 'kw') or op not in BINPRI:
                break
            lp, rp = BINPRI[op]
            if lp <= limit:
                break
            self.next()
            right = self.expr(rp)
            left = ('bin', op, left, right)
        return left


def parse(src):
    p = Parser(src)
    body = p.block()
    if p.peek()[0] != 'eof':
        t = p.peek()
        raise SyntaxError('line %d: unexpected %r' % (t[2], t[:2]))
    return body, p.funcs
