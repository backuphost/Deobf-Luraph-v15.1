"""Partial evaluator: runs the Luraph interpreter source on one VM instruction at a time,
with concrete operands / VM state and symbolic registers, emitting register-level IR.

IR atoms / expressions (tuples):
  ('k', value)            constant (None/bool/number/str)
  ('r', n)                VM register
  ('t', id)               single-assignment temporary
  ('up', n)               upvalue n of the current closure
  ('global', name)        environment lookup
  ('arg', i)              i-th actual argument of the closure (1-based, params first)
  ('nargs',)              number of actual arguments
  ('mv', id, i)           i-th value of multi-value producer id
  ('env',) ('regs',) ('kfn', name) ('opaque', text)
  ('bin', op, a, b) ('un', op, a) ('index', a, b) ('and', a, b) ('or', a, b)
  ('newtable', n) ('closure', proto_key, caps)
IR statements:
  ('set', dst, expr)
  ('setindex', obj, key, val)
  ('call', id, f, args, tail, nres)      nres: -1 open, else count; results via ('mv', id, i)
  ('vararg', id)                          multi-value producer holding the varargs
  ('setmulti', base, id)                  R[base...] = all values of id
  ('setlist', tbl, start, vals, tail)
  ('close', n)
  ('numforprep', A) ('numforloop', A) ('genforprep', A) ('genforloop', regs) ('forpop',)
Terminators:
  ('goto', pc) ('ret', vals, tail) ('br', cond, T, F) where T/F = (stmts, term)
"""
import math
import struct
import sys

from .luparse import Func

sys.setrecursionlimit(20000)


# ------------------------------------------------------------------ values
class LuaTable:
    __slots__ = ('h', 'id', 'mt', 'escaped', 'fresh', 'path')

    def __init__(self, id=None):
        self.h = {}
        self.id = id
        self.path = None
        self.mt = None
        self.escaped = False
        self.fresh = id is None

    def get(self, k):
        return self.h.get(k)

    def set(self, k, v):
        if v is None:
            self.h.pop(k, None)
        else:
            self.h[k] = v

    def length(self):
        n = 0
        h = self.h
        while (n + 1) in h:
            n += 1
        return n

    def __repr__(self):
        return '<T%s #%d>' % (self.id, len(self.h))


class Special:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return '<%s>' % self.name


REGS = Special('REGS')
ENV = Special('ENV')
UPVALS = Special('UPVALS')
VARARG_TBL = Special('VARARG_TBL')
OPENUPS = Special('OPENUPS')
FORITER = Special('FORITER')
FSTATE = Special('FSTATE')


class Builtin:
    __slots__ = ('name',)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return '<builtin %s>' % self.name

    def __eq__(self, o):
        return isinstance(o, Builtin) and o.name == self.name

    def __hash__(self):
        return hash(self.name)


class Lib:
    def __init__(self, name):
        self.name = name


class Buffer:
    def __init__(self, data):
        self.data = bytearray(data)
        self.path = None


def _qs(s):
    return '"' + ''.join(c if 32 <= ord(c) < 127 and c not in '"\\' else '\\%03d' % ord(c) for c in s) + '"'


class Opaque:
    def __init__(self, desc):
        self.desc = desc
        self.path = None

    def __repr__(self):
        return '<opaque %s>' % self.desc


class LFn:
    __slots__ = ('func', 'scope', 'line', 'path')

    def __init__(self, func, scope, line=None):
        self.func = func
        self.scope = scope
        self.line = line
        self.path = None

    def __repr__(self):
        return '<lfn L%s>' % (self.func.line if self.func else self.line)


class ProtoRef:
    def __init__(self, pid):
        self.pid = pid


class Sym:
    __slots__ = ('e', 'origin')

    def __init__(self, e, origin=None):
        self.e = e
        self.origin = origin

    def __repr__(self):
        return 'Sym%r' % (self.e,)


class LinSym:
    """count(multi id) + k"""
    __slots__ = ('id', 'k')

    def __init__(self, id, k):
        self.id = id
        self.k = k

    def __repr__(self):
        return 'LinSym(%r,%d)' % (self.id, self.k)

    def __eq__(self, o):
        return isinstance(o, LinSym) and o.id == self.id and o.k == self.k

    def __hash__(self):
        return hash((self.id, self.k))


class UpRef:
    __slots__ = ('n',)

    def __init__(self, n):
        self.n = n


class UpBox:
    __slots__ = ('n',)

    def __init__(self, n):
        self.n = n


class UpKey:
    __slots__ = ('n',)

    def __init__(self, n):
        self.n = n


class PackedTbl:
    __slots__ = ('id',)

    def __init__(self, id):
        self.id = id


class MultiTail:
    __slots__ = ('id',)

    def __init__(self, id):
        self.id = id


class Scope:
    __slots__ = ('vars', 'parent', 'boundary')

    def __init__(self, parent=None, boundary=False):
        self.vars = {}
        self.parent = parent
        self.boundary = boundary

    def lookup(self, name):
        s = self
        while s is not None:
            if name in s.vars:
                return s
            s = s.parent
        return None


class PEError(Exception):
    pass


class NeedFork(Exception):
    pass


class ForOp(Exception):
    pass


class LoopHead(Exception):
    def __init__(self, loop, snapshot):
        self.loop = loop
        self.snapshot = snapshot


class ReturnSig(Exception):
    def __init__(self, vals):
        self.vals = vals


def norm(v):
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 2 ** 53:
            if v == 0 and math.copysign(1, v) < 0:
                return v
            return int(v)
    return v


def tou32(v):
    if isinstance(v, float):
        if v != v or v in (math.inf, -math.inf):
            return 0
        v = int(v)
    return v & 0xFFFFFFFF


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def truthy(v):
    return v is not None and v is not False


# ------------------------------------------------------------------ builtins
def _bit_shift_l(a, n):
    a = tou32(a)
    n = int(n)
    if n <= -32 or n >= 32:
        return 0
    if n < 0:
        return a >> (-n)
    return (a << n) & 0xFFFFFFFF


def _lrot(a, n):
    a = tou32(a)
    n = int(n) % 32
    return ((a << n) | (a >> (32 - n))) & 0xFFFFFFFF if n else a


def _countlz(a):
    a = tou32(a)
    return 32 - a.bit_length()


def _countrz(a):
    a = tou32(a)
    if a == 0:
        return 32
    return (a & -a).bit_length() - 1


def _band(*a):
    r = 0xFFFFFFFF
    for x in a:
        r &= tou32(x)
    return r


def _bor(*a):
    r = 0
    for x in a:
        r |= tou32(x)
    return r


def _bxor(*a):
    r = 0
    for x in a:
        r ^= tou32(x)
    return r


def _arshift(a, n):
    a = tou32(a)
    n = int(n)
    if n < 0:
        return _bit_shift_l(a, -n)
    if n >= 32:
        return 0xFFFFFFFF if a & 0x80000000 else 0
    if a & 0x80000000:
        return ((a >> n) | (0xFFFFFFFF << (32 - n))) & 0xFFFFFFFF
    return a >> n


def _extract(a, f, w=1):
    return (tou32(a) >> int(f)) & ((1 << int(w)) - 1)


def _replace(a, v, f, w=1):
    m = ((1 << int(w)) - 1) << int(f)
    return (tou32(a) & ~m) | ((tou32(v) << int(f)) & m)


PURE = {
    'bit32.band': _band, 'bit32.bor': _bor, 'bit32.bxor': _bxor,
    'bit32.bnot': lambda a: (~tou32(a)) & 0xFFFFFFFF,
    'bit32.lshift': _bit_shift_l, 'bit32.rshift': lambda a, n: _bit_shift_l(a, -int(n)),
    'bit32.lrotate': _lrot, 'bit32.rrotate': lambda a, n: _lrot(a, -int(n)),
    'bit32.countlz': _countlz, 'bit32.countrz': _countrz,
    'bit32.arshift': _arshift, 'bit32.extract': _extract, 'bit32.replace': _replace,
    'bit32.btest': lambda *a: _band(*a) != 0,
    'bit32.byteswap': lambda a: struct.unpack('<I', struct.pack('>I', tou32(a)))[0],
    'math.floor': lambda a: math.floor(a), 'math.ceil': lambda a: math.ceil(a),
    'math.abs': lambda a: abs(a), 'math.max': lambda *a: max(a), 'math.min': lambda *a: min(a),
}


NOTK = Special('NOTK')


ESCAPED = '__escaped'


def k_eval(e, regs, tv, used=None):
    """Value of an IR expression when it is a compile-time constant on this path, else NOTK."""
    t = e[0]
    if t == 'k':
        return e[1]
    if t == 'r':
        if e[1] in regs:
            if used is not None:
                used[e[1]] = regs[e[1]]
            return regs[e[1]]
        return NOTK
    if t == 't' or t == 'mv':
        return tv.get(e, NOTK)
    if t == 'bin':
        a = k_eval(e[2], regs, tv, used)
        if a is NOTK:
            return NOTK
        b = k_eval(e[3], regs, tv, used)
        if b is NOTK:
            return NOTK
        if e[1] in ('==', '~='):
            if isinstance(a, (LuaTable, Special)) or isinstance(b, (LuaTable, Special)):
                return NOTK
            eq = (a == b) and (isinstance(a, bool) == isinstance(b, bool)) and (isinstance(a, str) == isinstance(b, str))
            return eq if e[1] == '==' else not eq
        if not is_num(a) or not is_num(b):
            return NOTK
        try:
            op = e[1]
            if op == '+':
                r = a + b
            elif op == '-':
                r = a - b
            elif op == '*':
                r = a * b
            elif op == '%':
                r = a - math.floor(a / b) * b
            elif op == '//':
                r = math.floor(a / b)
            elif op == '/':
                r = a / b
            elif op in ('<', '<=', '>', '>='):
                return {'<': a < b, '<=': a <= b, '>': a > b, '>=': a >= b}[op]
            else:
                return NOTK
        except (ZeroDivisionError, OverflowError):
            return NOTK
        return norm(r)
    if t == 'un':
        a = k_eval(e[2], regs, tv, used)
        if a is NOTK:
            return NOTK
        if e[1] == 'not':
            return not truthy(a)
        if e[1] == '-' and is_num(a):
            return norm(-a)
        return NOTK
    if t in ('and', 'or'):
        a = k_eval(e[1], regs, tv, used)
        if a is NOTK:
            return NOTK
        if truthy(a) == (t == 'or'):
            return a
        return k_eval(e[2], regs, tv, used)
    return NOTK


def k_deps(e, td, out):
    """Registers a constant expression was computed from (td: temporary -> registers)."""
    t = e[0]
    if t == 'r':
        out.add(e[1])
    elif t == 't' or t == 'mv':
        out |= td.get(e, ())
    elif t == 'bin':
        k_deps(e[2], td, out)
        k_deps(e[3], td, out)
    elif t == 'un':
        k_deps(e[2], td, out)
    elif t in ('and', 'or'):
        k_deps(e[1], td, out)
        k_deps(e[2], td, out)
    return out


def k_transfer(stmts, regs, tv, used=None, td=None):
    """Push known-constant register / temporary values through a straight line of IR."""
    for st in stmts:
        t = st[0]
        if t == 'set':
            dst = st[1]
            if dst[0] == 't':
                v = k_eval(st[2], regs, tv, used) if isinstance(st[2], tuple) else NOTK
                if v is NOTK:
                    tv.pop(dst, None)
                else:
                    tv[dst] = v
                    if td is not None:
                        td[dst] = frozenset(k_deps(st[2], td, set()))
            elif dst[0] == 'r':
                v = k_eval(st[2], regs, tv, used)
                if v is NOTK or dst[1] in regs.get(ESCAPED, ()):
                    regs.pop(dst[1], None)
                else:
                    regs[dst[1]] = v
            ex = st[2]
            if isinstance(ex, (tuple, list)) and ex[0] == 'closure' and isinstance(ex[2], tuple):
                # a register captured by reference can be changed behind our back by the closure
                refs = [c[1] for c in ex[2] if c[0] == 'ref']
                if refs:
                    regs[ESCAPED] = regs.get(ESCAPED, frozenset()) | frozenset(refs)
                    for r in refs:
                        regs.pop(r, None)
        elif t == 'close':
            esc = regs.get(ESCAPED)
            if esc and isinstance(st[1], int):
                regs[ESCAPED] = frozenset(r for r in esc if not (isinstance(r, int) and r >= st[1]))
        elif t == 'call':
            key = ('mv', st[1], 1)
            f = st[2]
            v = NOTK
            if f[0] == 'kfn' and f[1] in PURE and st[4] is None:
                args = [k_eval(a, regs, tv, used) for a in st[3]]
                if all(a is not NOTK and is_num(a) for a in args):
                    try:
                        v = norm(PURE[f[1]](*args))
                    except Exception:
                        v = NOTK
            if v is NOTK:
                tv.pop(key, None)
            else:
                tv[key] = v
                if td is not None:
                    deps = set()
                    for a in st[3]:
                        k_deps(a, td, deps)
                    td[key] = frozenset(deps)
        elif t == 'setmulti' or (t == 'setlist' and st[1] == ('regs',)):
            base = st[1] if t == 'setmulti' else st[2]
            if isinstance(base, int):
                for r in [r for r in regs if isinstance(r, int) and r >= base]:
                    del regs[r]
            else:
                for r in [r for r in regs if isinstance(r, int)]:
                    del regs[r]
        elif t in ('numforprep', 'genforprep'):
            if isinstance(st[1], int):
                for r in range(st[1], st[1] + 6):
                    regs.pop(r, None)
        elif t == 'clobber':
            for r in st[1]:
                regs.pop(r, None)


def lua_type(v):
    if v is None:
        return 'nil'
    if isinstance(v, bool):
        return 'boolean'
    if is_num(v):
        return 'number'
    if isinstance(v, str):
        return 'string'
    if isinstance(v, (LuaTable, Special, PackedTbl, UpRef, UpBox)):
        return 'table'
    if isinstance(v, (Builtin, LFn)):
        return 'function'
    if isinstance(v, Buffer):
        return 'buffer'
    return None


def _buf_read(fmt, size):
    def f(b, off):
        off = int(off)
        return norm(struct.unpack_from(fmt, b.data, off)[0])
    return f


def _buf_write(fmt):
    def f(b, off, v):
        if fmt in ('<B', '<H', '<I'):
            v = int(v) & {'<B': 0xFF, '<H': 0xFFFF, '<I': 0xFFFFFFFF}[fmt]
        elif fmt in ('<b', '<h', '<i'):
            bits = {'<b': 8, '<h': 16, '<i': 32}[fmt]
            v = int(v) & ((1 << bits) - 1)
            if v >= 1 << (bits - 1):
                v -= 1 << bits
        struct.pack_into(fmt, b.data, int(off), v)
        return None
    return f


BUF = {
    'buffer.readu8': _buf_read('<B', 1), 'buffer.readi8': _buf_read('<b', 1),
    'buffer.readu16': _buf_read('<H', 2), 'buffer.readi16': _buf_read('<h', 2),
    'buffer.readu32': _buf_read('<I', 4), 'buffer.readi32': _buf_read('<i', 4),
    'buffer.readf32': _buf_read('<f', 4), 'buffer.readf64': _buf_read('<d', 8),
    'buffer.writeu8': _buf_write('<B'), 'buffer.writei8': _buf_write('<b'),
    'buffer.writeu16': _buf_write('<H'), 'buffer.writei16': _buf_write('<h'),
    'buffer.writeu32': _buf_write('<I'), 'buffer.writei32': _buf_write('<i'),
    'buffer.writef32': _buf_write('<f'), 'buffer.writef64': _buf_write('<d'),
    'buffer.len': lambda b: len(b.data),
    'buffer.readstring': lambda b, off, n: bytes(b.data[int(off):int(off) + int(n)]).decode('latin-1'),
    'buffer.tostring': lambda b: bytes(b.data).decode('latin-1'),
}


def _lua_sub_range(n, i, j):
    i = int(i)
    j = int(j)
    if i < 0:
        i = max(n + i + 1, 1)
    elif i == 0:
        i = 1
    if j < 0:
        j = n + j + 1
    elif j > n:
        j = n
    return i, j


def _buf_copy(dst, doff, src, soff=0, count=None):
    soff = int(soff or 0)
    if count is None:
        count = len(src.data) - soff
    dst.data[int(doff):int(doff) + int(count)] = src.data[soff:soff + int(count)]


def _buf_fill(b, off, val, count=None):
    off = int(off)
    if count is None:
        count = len(b.data) - off
    b.data[off:off + int(count)] = bytes([int(val) & 0xFF]) * int(count)


def _buf_writestring(b, off, st, count=None):
    raw = st.encode('latin-1')
    if count is not None:
        raw = raw[:int(count)]
    b.data[int(off):int(off) + len(raw)] = raw


def _str_byte(st, i=1, j=None):
    if j is None:
        j = i
    i, j = _lua_sub_range(len(st), i, j)
    return [ord(c) for c in st[i - 1:j]]


def _str_sub(st, i=1, j=-1):
    i, j = _lua_sub_range(len(st), i, j)
    return st[i - 1:j] if i <= j else ''


# builtins the loader uses on concrete data (lazy constant decoding builds strings in buffers);
# each returns a list of results
CONCRETE = {
    'buffer.create': lambda n: [Buffer(bytes(int(n)))],
    'buffer.fromstring': lambda st: [Buffer(st.encode('latin-1'))],
    'buffer.copy': lambda *a: [_buf_copy(*a)][:0],
    'buffer.fill': lambda *a: [_buf_fill(*a)][:0],
    'buffer.writestring': lambda *a: [_buf_writestring(*a)][:0],
    'string.char': lambda *a: [''.join(chr(int(x) & 0xFF) for x in a)],
    'string.byte': _str_byte,
    'string.sub': lambda *a: [_str_sub(*a)],
    'string.rep': lambda st, n: [st * max(int(n), 0)],
    'string.len': lambda st: [len(st)],
    'string.reverse': lambda st: [st[::-1]],
}
CONCRETE_TYPES = (int, float, str, Buffer)


# ------------------------------------------------------------------ dump loading
class Dump:
    def __init__(self, data, model):
        self.data = data
        self.model = model
        self.tables = {}
        self.raw = data['tables']

    def conv(self, v, path=None):
        if v is None or isinstance(v, (bool, int)):
            return v
        if isinstance(v, float):
            return norm(v)
        if 's' in v:
            return bytes.fromhex(v['s']).decode('latin-1')
        if 't' in v:
            return self.table(v['t'], path)
        if 'f' in v:
            return {'nan': math.nan, 'inf': math.inf, '-inf': -math.inf, '-0': -0.0}[v['f']]
        if 'fn' in v:
            return Builtin(v['fn'])
        if 'lfn' in v and 'cap' in v:
            func = self.model.func_exprs.get(v['fid'])
            sc = Scope()
            for n in self.model.captures.get(v['fid'], ()):
                sc.vars[n] = None
            f = LFn(func, sc, v['lfn'])
            f.path = path
            cap = self.table(v['cap']['t'])
            for k, x in cap.h.items():
                sc.vars[k] = x
            return f
        if 'lfn' in v:
            fs = self.model.func_by_line.get(v['lfn'], [])
            f = LFn(fs[0] if len(fs) == 1 else None, None, v['lfn'])
            f.path = path
            return f
        if 'lib' in v:
            return Lib(v['lib'])
        if 'env' in v:
            return ENV
        if 'buf' in v:
            b = Buffer(bytes.fromhex(v['buf']))
            b.path = path
            return b
        if 'proto' in v:
            return ProtoRef(v['proto'])
        o = Opaque(v.get('o', '?'))
        o.path = path
        return o

    def table(self, tid, path=None):
        t = self.tables.get(tid)
        if t is not None:
            return t
        t = LuaTable(tid)
        t.path = path
        self.tables[tid] = t
        raw = self.raw[str(tid)]
        for i, x in enumerate(raw['arr']):
            if x is not None:
                t.h[i + 1] = self.conv(x, path + '[%d]' % (i + 1) if path else None)
        for k, x in raw['hash']:
            kk = self.conv(k)
            sub = None
            if path and isinstance(kk, str):
                sub = path + '[%s]' % _qs(kk)
            elif path and isinstance(kk, int):
                sub = path + '[%d]' % kk
            t.h[kk] = self.conv(x, sub)
        if raw.get('mt'):
            t.mt = self.conv(raw['mt'])
        return t

    def root_vars(self):
        raw = self.raw[str(self.data['root']['t'])]
        vt = [x for k, x in raw['hash'] if bytes.fromhex(k['s']) == b'vars'][0]
        return self.table(vt['t'], '__LPH_ENV').h

    def proto_envs(self):
        return [self.conv(p).h for p in self.data['protos']]


# ------------------------------------------------------------------ evaluator
ARITH = {'+', '-', '*', '/', '//', '%', '^'}
CMP = {'<', '<=', '>', '>=', '==', '~='}


class PE:
    def __init__(self, model, dump):
        self.model = model
        self.dump = dump
        self.loop_ids = {id(l): i for i, l in enumerate(model.loops)}
        self.loop_anc = self._ancestors()
        self.ir = None
        self.tcount = 0
        self.tprefix = ''
        self.decisions = []
        self.dpos = 0
        self.perm = None            # None | True | False : permissive (for-op) mode
        self.inject = None          # (loop_idx, snapshot)
        self.seeking = False
        self.interp_scope = None
        self.fvars, self.fstack, self.openups = self._classify_state_vars()
        self.events = []
        self.wrap_func = None
        self.factory = None         # (outer Func, mid Func) for factory-style VMs
        self.foreign = 0            # >0 while running loader/helper code rather than a handler
        self.instantiating = False
        self.global_scope = Scope()
        self.box_fields = (1, 3)    # (field holding the register table, field holding the index)
        # constants known along the path that reached the instruction being lifted (see concretize)
        self.kregs = {}
        self.tv = {}
        self.kused = {}
        self.kcur = None
        self.kpos = 0
        self.up_base = 0
        self.on_closure = None
        self.cur_pc = None
        self.track = False
        self.injected = set()
        self.exposed = set()
        self.written = set()
        self.pending_closures = []
        self.path = []
        self.va_base = None
        self.pcvars = {}
        for l in model.loops:
            self.pcvars[id(l)] = l[2][0][2][0][2][1]

    # -- static helpers
    def _ancestors(self):
        """ids of statements/funcs that (transitively) contain each dispatch loop."""
        res = {}
        targets = {id(l) for l in self.model.loops}

        def walk_block(block, path):
            for st in block:
                walk_stmt(st, path)

        def walk_expr(e, path):
            if not isinstance(e, tuple):
                return
            t = e[0]
            if t == 'func':
                walk_block(e[1].body, path + [id(e[1])])
            elif t in ('index', 'bin'):
                walk_expr(e[-2], path); walk_expr(e[-1], path)
            elif t in ('un', 'paren'):
                walk_expr(e[-1], path)
            elif t == 'call':
                walk_expr(e[1], path)
                for a in e[2]:
                    walk_expr(a, path)
            elif t == 'mcall':
                walk_expr(e[1], path)
                for a in e[3]:
                    walk_expr(a, path)

        def walk_stmt(st, path):
            t = st[0]
            p2 = path + [id(st)]
            if id(st) in targets:
                res[id(st)] = set(p2)
            if t in ('while',):
                walk_block(st[2], p2)
            elif t == 'local' or t == 'assign':
                for e in st[2]:
                    walk_expr(e, p2)
            elif t == 'callstmt':
                walk_expr(st[1], p2)
            elif t == 'do':
                walk_block(st[1], p2)
            elif t == 'repeat':
                walk_block(st[1], p2)
            elif t == 'if':
                for c, b in st[1]:
                    walk_block(b, p2)
                if st[2]:
                    walk_block(st[2], p2)
            elif t in ('fornum',):
                walk_block(st[5], p2)
            elif t == 'forin':
                walk_block(st[3], p2)
            elif t == 'return':
                for e in st[1]:
                    walk_expr(e, p2)

        walk_block(self.model.interp.body, [])
        return res

    def _classify_state_vars(self):
        """Find interpreter-level locals used as for-loop state / open-upvalue map."""
        fvars = set()
        fstack = set()
        openups = set()

        def visit(st):
            t = st[0]
            if t == 'assign' and len(st[1]) == 1 and st[1][0][0] == 'name' and len(st[2]) == 1 and st[2][0][0] == 'table':
                nm = st[1][0][1]
                vals = [v for _, _, v in st[2][0][1]]
                if any(v == ('name', nm) for v in vals):
                    fstack.add(nm)
                    for v in vals:
                        if v[0] == 'name':
                            fvars.add(v[1])
            if t == 'forin' and len(st[2]) == 1 and st[2][0][0] == 'name' and len(st[1]) == 2:
                body = st[3]
                txt = repr(body)
                if "('num', 3)" in txt and "('num', 2)" in txt:
                    openups.add(st[2][0][1])
            for sub in substmts(st):
                visit(sub)

        for l in self.model.loops:
            for st in l[2]:
                visit(st)
        return fvars, fstack, openups

    # -- emission
    def newt(self):
        self.tcount += 1
        return ('t', '%s_%d' % (self.tprefix, self.tcount))

    def emit(self, st):
        self.ir.append(st)

    def emit_expr(self, e, origin=None):
        t = self.newt()
        self.ir.append(('set', t, e))
        return Sym(t, origin)

    def atom(self, v):
        if isinstance(v, Sym):
            return v.e
        if v is None or isinstance(v, (bool, int, float, str)):
            return ('k', v)
        if v is REGS:
            return ('regs',)
        if v is ENV:
            return ('env',)
        if isinstance(v, Builtin):
            return ('kfn', v.name)
        if isinstance(v, Lib):
            return ('global', v.name)
        if isinstance(v, UpRef):
            return ('up', v.n)
        if isinstance(v, LuaTable):
            if v.fresh and not v.h:
                v.escaped = True
                t = self.newt()
                self.ir.append(('set', t, ('newtable', 0)))
                return t
            if v.path:
                return ('outer', v.path)
            return ('opaque', 'T%s' % v.id)
        if isinstance(v, LinSym):
            return ('opaque', repr(v))
        if v is FSTATE or v is FORITER:
            return ('opaque', 'forstate')
        if isinstance(v, (LFn, Buffer, Opaque)) and getattr(v, 'path', None):
            return ('outer', v.path)
        if isinstance(v, LFn):
            return ('opaque', 'vmfn_L%s' % (v.func.line if v.func else v.line))
        if isinstance(v, Buffer):
            return ('opaque', 'buffer')
        if isinstance(v, Special):
            return ('opaque', v.name)
        if isinstance(v, (Opaque, PackedTbl, UpBox, UpKey, ProtoRef)):
            return ('opaque', type(v).__name__)
        raise PEError('cannot atomize %r' % (v,))

    def concretize(self, v):
        """Concrete value of a symbolic value when the path fixes it (the VM decrypts its own
        bytecode with keys that pass through registers), else the value unchanged."""
        if not isinstance(v, Sym):
            return v
        if self.kcur is None:
            self.kcur = dict(self.kregs)
            self.kpos = 0
        if self.kpos < len(self.ir):
            k_transfer(self.ir[self.kpos:], self.kcur, self.tv, self.kused)
            self.kpos = len(self.ir)
        c = k_eval(v.e, self.kcur, self.tv, self.kused)
        return v if c is NOTK else c

    def read_reg(self, n):
        return self.emit_expr(('r', n), ('r', n))

    def regs_range(self, a, b):
        """values of R[a..b] where b may be symbolic top."""
        if isinstance(b, LinSym):
            vals = [self.read_reg(i) for i in range(a, b.k + 1)]
            vals.append(MultiTail(b.id))
            return vals
        return [self.read_reg(i) for i in range(int(a), int(b) + 1)]

    # -- scopes
    def getvar(self, scope, name):
        s = scope.lookup(name)
        if s is None:
            raise PEError('unbound name %s' % name)
        if name in self.openups:
            return OPENUPS
        if self.perm is not None and (name in self.fvars or name in self.fstack):
            return FSTATE
        if name in self.fvars or name in self.fstack:
            if not self.seeking:
                raise ForOp()
        if self.track and name not in self.written and (id(s), name) in self.injected:
            self.exposed.add(name)
        return s.vars[name]

    def setvar(self, scope, name, v):
        s = scope.lookup(name)
        if s is None:
            raise PEError('assignment to unbound name %s' % name)
        if name in self.fvars or name in self.fstack:
            if self.perm is None and not self.seeking:
                raise ForOp()
        if self.track and (id(s), name) in self.injected:
            self.written.add(name)
        s.vars[name] = v

    def snapshot(self, scope):
        snap = {}
        s = scope
        while s is not None:
            for k, v in s.vars.items():
                if k not in snap:
                    snap[k] = v
            if s.boundary:
                break
            s = s.parent
        return snap

    # -- expression evaluation
    def eval1(self, e, scope):
        t = e[0]
        if t == 'name':
            return self.getvar(scope, e[1])
        if t == 'num' or t == 'str':
            return e[1]
        if t == 'index':
            obj = self.eval1(e[1], scope)
            if obj is VARARG_TBL and e[2][0] == 'bin' and e[2][1] == '+':
                a = self.eval1(e[2][2], scope)
                b = self.eval1(e[2][3], scope)
                if self.va_base is None:
                    self.va_base = max(a, b)
                return Sym(('varargn', a + b - self.va_base + 1))
            return self.index(obj, self.eval1(e[2], scope), e[2])
        if t == 'bin':
            return self.binop(e, scope)
        if t == 'call' or t == 'mcall' or t == 'vararg':
            vals = self.evalmulti(e, scope, want=1)
            if not vals:
                return None
            v = vals[0]
            if isinstance(v, MultiTail):
                return self.mv(v.id, 1)
            return v
        if t == 'nil':
            return None
        if t == 'true':
            return True
        if t == 'false':
            return False
        if t == 'paren':
            return self.eval1(e[1], scope)
        if t == 'un':
            return self.unop(e[1], self.eval1(e[2], scope))
        if t == 'func':
            return LFn(e[1], scope)
        if t == 'table':
            tb = LuaTable()
            n = 0
            items = e[1]
            for i, (kind, k, v) in enumerate(items):
                if kind == 'kv':
                    tb.set(self.eval1(k, scope), self.eval1(v, scope))
                else:
                    if i == len(items) - 1 and v[0] in ('call', 'mcall', 'vararg'):
                        vals = self.evalmulti(v, scope)
                        if vals and isinstance(vals[-1], MultiTail):
                            if n == 0 and len(vals) == 1:
                                return PackedTbl(vals[0].id)
                            raise PEError('table constructor with open tail')
                        for x in vals:
                            n += 1
                            tb.set(n, x)
                    else:
                        n += 1
                        tb.set(n, self.eval1(v, scope))
            return tb
        if t == 'ifexpr':
            c = self.eval1(e[1], scope)
            if isinstance(c, Sym):
                return self.eval1(e[2] if self.cond(c) else e[3], scope)
            if isinstance(c, tuple) and c[0] == 'linlt0':
                a = self.eval1(e[2], scope)
                b = self.eval1(e[3], scope)
                if a == -1 and isinstance(b, LinSym):
                    return LinSym('va', -1)
                raise PEError('unsupported clamp')
            return self.eval1(e[2], scope) if truthy(c) else self.eval1(e[3], scope)
        raise PEError('eval: unknown expr %r' % (t,))

    def mv(self, id, i):
        if id == 'va':
            return Sym(('varargn', i))
        return Sym(('mv', id, i))

    def evalmulti(self, e, scope, want=-1):
        """Evaluate an expression that may yield several values. Returns a list, possibly ending in MultiTail."""
        t = e[0]
        if t == 'call':
            f = self.eval1(e[1], scope)
            args = self.evalargs(e[2], scope)
            return self.call(f, args, want, e)
        if t == 'mcall':
            obj = self.eval1(e[1], scope)
            f = self.index(obj, e[2], None)
            args = [obj] + self.evalargs(e[3], scope)
            return self.call(f, args, want, e)
        if t == 'vararg':
            v = scope.lookup('...')
            if v is None:
                raise PEError('vararg outside vararg function')
            return list(v.vars['...'])
        return [self.eval1(e, scope)]

    def evalargs(self, es, scope):
        out = []
        n = len(es)
        for i, a in enumerate(es):
            if i == n - 1 and a[0] in ('call', 'mcall', 'vararg'):
                out.extend(self.evalmulti(a, scope))
            else:
                out.append(self.eval1(a, scope))
        return out

    def index(self, obj, key, keyast):
        if isinstance(obj, LuaTable):
            if isinstance(key, Sym):
                key = self.concretize(key)
            if isinstance(key, (Sym, LinSym)):
                raise PEError('symbolic key into concrete table T%s' % obj.id)
            if isinstance(key, float):
                key = norm(key)
            r = obj.h.get(key)
            if r is None and obj.mt is not None:
                h = obj.mt.h.get('__index')
                if isinstance(h, LFn):
                    # lazily materialised VM data (constants / protos decoded on first use)
                    vals = self.call_lfn(h, [obj, key], 1)
                    return vals[0] if vals else None
                if isinstance(h, LuaTable):
                    return self.index(h, key, keyast)
            return r
        if obj is REGS:
            if isinstance(key, int):
                return self.read_reg(key)
            raise PEError('register index %r' % (key,))
        if isinstance(obj, Sym):
            return self.emit_expr(('index', obj.e, self.atom(key)))
        if obj is UPVALS:
            if not isinstance(key, int):
                raise PEError('upvalue index %r' % (key,))
            return UpRef(key)
        if isinstance(obj, UpRef):
            kind = self.upkind(obj.n)
            if kind == 'box' or (kind is None and keyast is not None and keyast[0] == 'num'):
                # boxed upvalue {regs, value, index}; the 1 / 3 may come from operands when the
                # instruction was split into micro-ops
                if key == self.box_fields[0]:
                    return UpBox(obj.n)
                if key == self.box_fields[1]:
                    return UpKey(obj.n)
            return self.emit_expr(('index', ('up', obj.n), self.atom(key)))
        if isinstance(obj, UpBox):
            if isinstance(key, UpKey) and key.n == obj.n:
                return self.emit_expr(('up', obj.n))
            raise PEError('bad upvalue box access')
        if obj is ENV:
            if isinstance(key, str):
                return self.emit_expr(('global', key))
            return self.emit_expr(('index', ('env',), self.atom(key)))
        if obj is VARARG_TBL:
            if isinstance(key, int):
                return Sym(('arg', key))
            raise PEError('vararg table index %r' % (key,))
        if isinstance(obj, PackedTbl):
            if isinstance(key, int):
                return self.mv(obj.id, key)
            raise PEError('packed index %r' % (key,))
        if obj is OPENUPS:
            return None
        if isinstance(obj, Lib):
            if not isinstance(key, str):
                raise PEError('library %s indexed with %r' % (obj.name, key))
            return Builtin(obj.name + '.' + key)
        if isinstance(obj, str):
            if not isinstance(key, str):
                raise PEError('string indexed with %r' % (key,))
            return Builtin('string.' + key)
        if obj is FSTATE:
            return FSTATE
        if isinstance(obj, UpKey) or obj is None or isinstance(obj, (int, float, bool)):
            if self.perm is not None:
                return FSTATE
            raise PEError('attempt to index %r' % (obj,))
        if isinstance(obj, Builtin) or isinstance(obj, LFn) or isinstance(obj, Opaque) or isinstance(obj, Buffer):
            return self.emit_expr(('index', self.atom(obj), self.atom(key)))
        raise PEError('index: unsupported object %r' % (obj,))

    def setindex(self, obj, key, val, keyast=None):
        if isinstance(obj, LuaTable):
            if isinstance(key, Sym):
                key = self.concretize(key)
            if isinstance(key, (Sym, LinSym)):
                raise PEError('symbolic key store into concrete table')
            if not obj.fresh and isinstance(val, Sym):
                val = self.concretize(val)
            if obj.escaped:
                raise PEError('store into escaped table')
            if isinstance(key, float):
                key = norm(key)
            obj.set(key, val)
            return
        if obj is REGS:
            if not isinstance(key, int):
                raise PEError('register store index %r' % (key,))
            self.emit(('set', ('r', key), self.atom(val)))
            return
        if isinstance(obj, Sym):
            self.emit(('setindex', obj.e, self.atom(key), self.atom(val)))
            return
        if isinstance(obj, UpBox):
            if isinstance(key, UpKey) and key.n == obj.n:
                self.emit(('set', ('up', obj.n), self.atom(val)))
                return
            raise PEError('bad upvalue box store')
        if isinstance(obj, UpRef):
            self.emit(('setindex', ('up', obj.n), self.atom(key), self.atom(val)))
            return
        if obj is ENV:
            if isinstance(key, str):
                self.emit(('set', ('global', key), self.atom(val)))
            else:
                self.emit(('setindex', ('env',), self.atom(key), self.atom(val)))
            return
        if obj is OPENUPS:
            return
        if obj is FSTATE and self.perm is not None:
            return
        raise PEError('setindex: unsupported object %r' % (obj,))

    def unop(self, op, v):
        if isinstance(v, Sym):
            return self.emit_expr(('un', op, v.e))
        if v is FSTATE:
            return FSTATE
        if op == 'not':
            return not truthy(v)
        if op == '-':
            if is_num(v):
                return norm(-v)
        if op == '#':
            if isinstance(v, str):
                return len(v)
            if isinstance(v, LuaTable):
                return v.length()
            if isinstance(v, PackedTbl):
                return LinSym(v.id, 0)
        if isinstance(v, (Special, UpRef, LinSym)):
            return self.emit_expr(('un', op, self.atom(v)))
        raise PEError('unop %s on %r' % (op, v))

    def binop(self, e, scope):
        op = e[1]
        if op == 'and':
            a = self.eval1(e[2], scope)
            if isinstance(a, Sym):
                b = self.eval1(e[3], scope)
                return self.emit_expr(('and', a.e, self.atom(b)))
            if a is FSTATE:
                self.eval1(e[3], scope)
                return FSTATE
            return self.eval1(e[3], scope) if truthy(a) else a
        if op == 'or':
            a = self.eval1(e[2], scope)
            if isinstance(a, Sym):
                b = self.eval1(e[3], scope)
                return self.emit_expr(('or', a.e, self.atom(b)))
            if a is FSTATE:
                self.eval1(e[3], scope)
                return FSTATE
            if a is OPENUPS:
                return a
            return a if truthy(a) else self.eval1(e[3], scope)
        a = self.eval1(e[2], scope)
        b = self.eval1(e[3], scope)
        return self.arith(op, a, b)

    def arith(self, op, a, b):
        if a is FSTATE or b is FSTATE:
            return FSTATE
        if isinstance(a, Sym) or isinstance(b, Sym):
            return self.emit_expr(('bin', op, self.atom(a), self.atom(b)))
        if isinstance(a, LinSym) or isinstance(b, LinSym):
            if op == '+':
                if isinstance(a, LinSym) and is_num(b):
                    return LinSym(a.id, a.k + b)
                if isinstance(b, LinSym) and is_num(a):
                    return LinSym(b.id, b.k + a)
            if op == '-' and isinstance(a, LinSym) and is_num(b):
                return LinSym(a.id, a.k - b)
            if op == '<' and isinstance(a, LinSym) and b == 0:
                return ('linlt0', a)
            if op in ('==', '~='):
                eq = isinstance(a, LinSym) and isinstance(b, LinSym) and a == b
                return eq if op == '==' else not eq
            raise PEError('unsupported symbolic-count arithmetic %r %s %r' % (a, op, b))
        if op in ARITH:
            if isinstance(a, str) and op != '..':
                a = float(a) if a.strip() else a
            if not (is_num(a) and is_num(b)):
                if self.perm is not None:
                    return FSTATE
                if isinstance(a, (Special, UpRef, Builtin, LuaTable)) or isinstance(b, (Special, UpRef, Builtin, LuaTable)):
                    return self.emit_expr(('bin', op, self.atom(a), self.atom(b)))
                raise PEError('arith %r %s %r' % (a, op, b))
            try:
                if op == '+':
                    r = a + b
                elif op == '-':
                    r = a - b
                elif op == '*':
                    r = a * b
                elif op == '/':
                    r = a / b if b != 0 else (math.nan if a == 0 else math.copysign(math.inf, a) * (math.copysign(1, b)))
                elif op == '//':
                    r = math.floor(a / b) if b != 0 else (math.nan if a == 0 else math.copysign(math.inf, a))
                elif op == '%':
                    if b == 0:
                        r = math.nan
                    elif isinstance(a, int) and isinstance(b, int):
                        r = a % b
                    else:
                        r = math.fmod(a, b)
                        if r != 0 and (r < 0) != (b < 0):
                            r += b
                else:
                    r = float(a) ** b
            except OverflowError:
                r = math.inf
            if isinstance(r, int) and abs(r) >= 2 ** 53:
                r = float(r)
            return norm(r)
        if op == '..':
            if isinstance(a, (str, int, float)) and isinstance(b, (str, int, float)):
                return lua_tostr(a) + lua_tostr(b)
            return self.emit_expr(('bin', op, self.atom(a), self.atom(b)))
        if op == '==' or op == '~=':
            if is_num(a) and is_num(b):
                r = a == b
            elif type(a) != type(b):
                r = False
            elif isinstance(a, (str, bool)) or a is None:
                r = a == b
            else:
                r = a is b
            return r if op == '==' else not r
        if op in CMP:
            if (is_num(a) and is_num(b)) or (isinstance(a, str) and isinstance(b, str)):
                if op == '<':
                    return a < b
                if op == '<=':
                    return a <= b
                if op == '>':
                    return a > b
                return a >= b
            if self.perm is not None:
                return FSTATE
            raise PEError('compare %r %s %r' % (a, op, b))
        raise PEError('binop %s' % op)

    # -- calls
    def call(self, f, args, want, ast=None):
        if isinstance(f, Builtin):
            return self.call_builtin(f.name, args, want)
        if isinstance(f, LFn):
            return self.call_lfn(f, args, want)
        if isinstance(f, Sym):
            return self.emit_call(f.e, args, want)
        if f is FORITER or f is FSTATE:
            self.events.append(('fcall', args))
            return [Sym(('forval', 1)), Sym(('forval', 2)), Sym(('forval', 3)), Sym(('forval', 4)), Sym(('forval', 5))]
        if isinstance(f, (UpRef, Special)):
            return self.emit_call(self.atom(f), args, want)
        if self.perm is not None:
            self.events.append(('fcall', args))
            return [FSTATE] * 5
        raise PEError('call of %r' % (f,))

    def emit_call(self, fe, args, want):
        tail = None
        if args and isinstance(args[-1], MultiTail):
            tail = args[-1].id
            args = args[:-1]
        self.tcount += 1
        cid = '%s_c%d' % (self.tprefix, self.tcount)
        self.emit(('call', cid, fe, [self.atom(a) for a in args], tail, want))
        if want == 1:
            return [Sym(('mv', cid, 1))]
        return [MultiTail(cid)]

    def call_builtin(self, name, args, want):
        sym = any(isinstance(a, (Sym, MultiTail)) for a in args)
        if name == 'table.create':
            if self.foreign:
                return [LuaTable()]
            if self.seeking:
                return [REGS]
            n = args[0] if args and is_num(args[0]) else 0
            return [self.emit_expr(('newtable', n))]
        if name == 'setmetatable' and len(args) == 2 and isinstance(args[0], LuaTable) \
                and (args[1] is None or isinstance(args[1], LuaTable)) and not args[0].escaped:
            args[0].mt = args[1]
            return [args[0]]
        if name == 'getmetatable' and len(args) == 1 and isinstance(args[0], LuaTable) and not args[0].escaped:
            return [args[0].mt]
        if name == 'getfenv':
            return [ENV]
        if name == 'setfenv':
            if len(args) == 2 and args[1] is ENV:
                return [args[0]]
        if name == 'pcall':
            f = args[0]
            if isinstance(f, LFn):
                try:
                    vals = self.call_lfn(f, args[1:], -1)
                except ReturnSig as r:
                    vals = r.vals
                return [True] + list(vals)
        if name == 'coroutine.wrap':
            if isinstance(args[0], LFn):
                self.events.append(('wrap',))
                return [FORITER]
        if name == 'select' and args and args[0] == '#':
            rest = args[1:]
            if rest and isinstance(rest[-1], MultiTail):
                return [LinSym(rest[-1].id, len(rest) - 1)]
            return [len(rest)]
        if name in ('unpack', 'table.unpack'):
            t = args[0]
            if t is REGS:
                return self.regs_range(args[1], args[2])
            if isinstance(t, LuaTable) and not sym:
                a = args[1] if len(args) > 1 and args[1] is not None else 1
                b = args[2] if len(args) > 2 and args[2] is not None else t.length()
                return [t.h.get(i) for i in range(a, b + 1)]
        if name == 'table.move' and len(args) == 5 and args[0] is REGS:
            vals = self.regs_range(args[1], args[2])
            tail = None
            if vals and isinstance(vals[-1], MultiTail):
                tail = vals[-1].id
                vals = vals[:-1]
            self.emit(('setlist', self.atom(args[4]), args[3], [self.atom(v) for v in vals], tail))
            return [args[4]]
        if name == 'type' or name == 'typeof':
            if len(args) == 1 and not sym:
                ty = lua_type(args[0])
                if ty:
                    return [ty]
        if name in CONCRETE and args and all(isinstance(a, CONCRETE_TYPES) and not isinstance(a, bool) or a is None
                                             for a in args):
            try:
                return CONCRETE[name](*args)
            except (struct.error, IndexError, ValueError, TypeError, AttributeError) as ex:
                raise PEError('%s failed on concrete data: %s' % (name, ex))
        if name in BUF and args and isinstance(args[0], Buffer):
            if sym:
                args = [self.concretize(a) for a in args]
                if any(isinstance(a, (Sym, MultiTail)) for a in args):
                    raise PEError('symbolic access to VM buffer in %s' % name)
            try:
                return [BUF[name](*args)]
            except (struct.error, IndexError, ValueError, TypeError) as ex:
                raise PEError('buffer access failed in %s: %s' % (name, ex))
        if not sym:
            if name in PURE and all(is_num(a) for a in args):
                return [norm(PURE[name](*args))]
            if name == 'error':
                raise PEError('VM raised error: %r' % (args[:1],))
        return self.emit_call(('kfn', name), args, want)

    def call_lfn(self, f, args, want):
        # the vararg packer: f(...) -> count, {...}
        if args and isinstance(args[-1], MultiTail) and f.func is not self.model.interp and (f.func is None or f.func.vararg):
            mid = args[-1].id
            if len(args) == 1:
                return [LinSym(mid, 0), PackedTbl(mid) if mid != 'args' else VARARG_TBL]
        if f.func is None:
            raise PEError('call of unresolved VM closure (line %s)' % f.line)
        if self.factory is not None:
            outer, mid = self.factory
            if f.func is mid and not self.instantiating:
                # proto(?, upvalues, ...) -> closure
                ups = [a for a in args if isinstance(a, LuaTable)]
                return [self.make_closure([f, ups[0] if ups else None])]
            if f.scope is None and (f.func is outer or f.func.id in self.model.toplevel):
                # functions of the top-level VM table close over nothing: run them directly
                f = LFn(f.func, self.global_scope, f.line)
        if f.func is self.wrap_func and f.scope is None or (f.func is self.wrap_func and len(args) == 2 and isinstance(args[0], LuaTable)):
            return [self.make_closure(args)]
        if f.scope is None or f.func.id in self.model.captures:
            # foreign helper: recognise the unpack-like helper (start, stop, regs)
            if any(a is REGS for a in args):
                nums = [a for a in args if a is not REGS]
                if len(nums) == 2:
                    a, b = self.unpack_order(f.func, args, nums)
                    return self.regs_range(a, b)
            if len(args) == 1 and (isinstance(args[0], LuaTable) and args[0].fresh or args[0] is False) \
                    and not self.foreign:
                # non-virtualised function: native(upvalue_table) -> closure
                return [self.make_closure([('native', f.func.line), args[0]])]
            if f.scope is None:
                raise PEError('call of foreign VM closure (line %s) with %r' % (f.line, args))
        func = f.func
        sc = Scope(f.scope, boundary=(func is self.model.interp))
        if func is self.model.interp:
            self.interp_scope = sc
        for i, p in enumerate(func.params):
            if i < len(args) and not isinstance(args[i], MultiTail):
                sc.vars[p] = args[i]
            else:
                sc.vars[p] = None
        if func.vararg:
            sc.vars['...'] = args[len(func.params):]
        if func.id in self.model.inside_interp:
            r = self.exec_block(func.body, sc)
        else:
            # loader / helper code: its locals merely share names with interpreter state
            saved = (self.fvars, self.fstack, self.openups)
            self.fvars, self.fstack, self.openups = set(), set(), set()
            self.foreign += 1
            try:
                r = self.exec_block(func.body, sc)
            finally:
                self.foreign -= 1
                self.fvars, self.fstack, self.openups = saved
        if r is not None and r[0] == 'return':
            return r[1]
        return []

    def unpack_order(self, func, args, nums):
        """(start, stop) for the VM's unpack-like helper, from `p = p or 1` in its body."""
        start_param = None
        for st in func.body:
            if st[0] == 'assign' and st[1][0][0] == 'name' and st[2][0][0] == 'bin' and st[2][0][1] == 'or' \
                    and st[2][0][3] == ('num', 1):
                start_param = st[1][0][1]
        if start_param in func.params:
            i = func.params.index(start_param)
            if i < len(args) and args[i] is not REGS:
                a = args[i]
                b = [x for j, x in enumerate(args) if j != i and x is not REGS][0]
                return a, b
        if isinstance(nums[0], LinSym):
            return nums[1], nums[0]
        return nums[0], nums[1]

    def box_layout(self, v):
        """(regs field, index field) when v is a freshly made upvalue box over the register table."""
        if not isinstance(v, LuaTable):
            return None
        rf = [k for k, x in v.h.items() if x is REGS]
        if len(rf) != 1:
            return None
        nf = [k for k, x in v.h.items() if isinstance(x, int) and not isinstance(x, bool)]
        if self.box_fields[0] == rf[0] and self.box_fields[1] in nf:
            return self.box_fields
        if len(nf) != 1:
            return None
        return (rf[0], nf[0])

    def upkind(self, n):
        """'box' / 'val' for upvalue n of the proto being lifted, None when unknown."""
        kinds = self.capkinds.get(self.cur_proto)
        if kinds is not None and n < len(kinds):
            return kinds[n]
        return None

    def make_closure(self, args):
        proto = args[0]
        ups = args[1] if len(args) > 1 else None
        self.tcount += 1
        t = self.newt()
        st = ['set', t, ['closure', proto, ups]]
        self.ir.append(st)
        self.pending_closures.append(st)
        return Sym(t)

    def finish_closures(self):
        for st in self.pending_closures:
            proto, ups = st[2][1], st[2][2]
            caps = []
            kinds = []
            if isinstance(ups, LuaTable):
                n = 0
                if 0 not in ups.h and 1 in ups.h:
                    n = 1
                    self.up_base = 1
                    caps.append(('pad', 0))
                    kinds.append(None)
                while n in ups.h:
                    v = ups.h[n]
                    box = self.box_layout(v)
                    if box is not None:
                        self.box_fields = box
                        caps.append(('ref', v.h.get(box[1])))
                        kinds.append('box')
                    elif isinstance(v, Sym) and v.origin:
                        caps.append(('val', v.origin[1]))
                        kinds.append('val')
                    elif isinstance(v, UpRef):
                        caps.append(('up', v.n))
                        kinds.append(self.upkind(v.n))
                    else:
                        raise PEError('unknown upvalue capture %r' % (v,))
                    n += 1
            if isinstance(proto, tuple):
                key = proto
            else:
                key = self.on_closure(proto) if self.on_closure else proto
            st[2] = ('closure', key, tuple(caps))
            try:
                self.capkinds.setdefault(key, kinds)
            except TypeError:
                pass
        res = [tuple(s) if isinstance(s, list) else s for s in self.ir]
        self.ir[:] = res
        self.pending_closures = []

    # -- statements
    def cond(self, v):
        """Resolve a branch condition; forks on symbolic values."""
        if isinstance(v, Sym):
            if self.perm is not None:
                return self.perm
            if self.dpos < len(self.decisions):
                d = self.decisions[self.dpos]
                self.dpos += 1
                self.path.append((len(self.ir), v.e, d))
                return d
            raise NeedFork()
        if v is FSTATE:
            return bool(self.perm)
        if isinstance(v, tuple):
            raise PEError('unsupported condition %r' % (v,))
        if isinstance(v, LinSym):
            raise PEError('condition on symbolic count')
        return truthy(v)

    def exec_block(self, block, scope):
        sc = Scope(scope)
        for st in block:
            r = self.exec_stmt(st, sc)
            if r is not None:
                return r
        return None

    def assign_target(self, target, v, scope):
        t = target[0]
        if t == 'name':
            self.setvar(scope, target[1], v)
        elif t == 'index':
            obj = self.eval1(target[1], scope)
            key = self.eval1(target[2], scope)
            self.setindex(obj, key, v, target[2])
        else:
            raise PEError('bad assignment target')

    def adjust(self, vals, n):
        out = []
        for i in range(n):
            if i < len(vals):
                v = vals[i]
                if isinstance(v, MultiTail):
                    for j in range(i, n):
                        out.append(self.mv(v.id, j - i + 1))
                    return out
                out.append(v)
            else:
                out.append(None)
        return out

    def exec_stmt(self, st, scope):
        if self.seeking:
            return self.exec_seek(st, scope)
        return self.exec_core(st, scope)

    def exec_core(self, st, scope):
        t = st[0]
        if t == 'if':
            for c, body in st[1]:
                if self.cond(self.eval1(c, scope)):
                    return self.exec_block(body, scope)
            if st[2]:
                return self.exec_block(st[2], scope)
            return None
        if t == 'assign':
            targets = st[1]
            if len(targets) == 1 and len(st[2]) == 1:
                tg = targets[0]
                if tg[0] == 'name':
                    nm = tg[1]
                    if nm in self.fstack and st[2][0][0] == 'table':
                        if self.perm is None:
                            raise ForOp()
                        self.events.append(('push',))
                        return None
                    if nm in self.openups:
                        return None
                    self.setvar(scope, nm, self.eval1(st[2][0], scope))
                else:
                    obj = self.eval1(tg[1], scope)
                    key = self.eval1(tg[2], scope)
                    self.setindex(obj, key, self.eval1(st[2][0], scope), tg[2])
                return None
            vals = self.adjust(self.evalargs(st[2], scope), len(targets))
            for tg, v in zip(targets, vals):
                self.assign_target(tg, v, scope)
            return None
        if t == 'local':
            names = st[1]
            if len(names) == 1 and len(st[2]) == 1:
                scope.vars[names[0]] = self.eval1(st[2][0], scope)
                return None
            vals = self.adjust(self.evalargs(st[2], scope), len(names)) if st[2] else [None] * len(names)
            for n, v in zip(names, vals):
                scope.vars[n] = v
            return None
        if t == 'opassign':
            tg = st[2]
            if tg[0] == 'name':
                if tg[1] in self.fvars:
                    if self.perm is None:
                        raise ForOp()
                    self.events.append(('fop',))
                cur = self.getvar(scope, tg[1])
                self.setvar(scope, tg[1], self.arith(st[1], cur, self.eval1(st[3], scope)))
            else:
                obj = self.eval1(tg[1], scope)
                key = self.eval1(tg[2], scope)
                cur = self.index(obj, key, tg[2])
                self.setindex(obj, key, self.arith(st[1], cur, self.eval1(st[3], scope)), tg[2])
            return None
        if t == 'callstmt':
            self.evalmulti(st[1], scope, want=0)
            return None
        if t == 'while':
            if id(st) in self.loop_ids:
                return self.exec_dispatch(st, scope)
            n = 0
            while True:
                if not self.cond(self.eval1(st[1], scope)):
                    return None
                r = self.exec_block(st[2], scope)
                if r is not None:
                    if r[0] == 'break':
                        return None
                    if r[0] == 'return':
                        return r
                n += 1
                if n > 200000:
                    raise PEError('runaway loop')
        if t == 'repeat':
            n = 0
            while True:
                sc = Scope(scope)
                r = None
                for s in st[1]:
                    r = self.exec_stmt(s, sc)
                    if r is not None:
                        break
                if r is not None:
                    if r[0] == 'break':
                        return None
                    if r[0] == 'return':
                        return r
                if self.cond(self.eval1(st[2], sc)):
                    return None
                n += 1
                if n > 200000:
                    raise PEError('runaway loop')
        if t == 'fornum':
            a = self.eval1(st[2], scope)
            b = self.eval1(st[3], scope)
            c = self.eval1(st[4], scope) if st[4] else 1
            if isinstance(b, LinSym) and is_num(a) and c == 1:
                # R[a .. a+count-1] = all values of a multi-value producer
                if b.k - a + 1 != 0:
                    raise PEError('open copy loop with offset %r %r' % (a, b))
                self.emit(('setmulti', a, b.id))
                return None
            if not (is_num(a) and is_num(b) and is_num(c)):
                if self.perm is not None:
                    return None
                raise PEError('symbolic numeric for %r %r %r' % (a, b, c))
            i = a
            n = 0
            while (c > 0 and i <= b) or (c < 0 and i >= b):
                sc = Scope(scope)
                sc.vars[st[1]] = i
                r = None
                for s in st[5]:
                    r = self.exec_stmt(s, sc)
                    if r is not None:
                        break
                if r is not None:
                    if r[0] == 'break':
                        break
                    if r[0] == 'return':
                        return r
                i = norm(i + c)
                n += 1
                if n > 2000000:
                    raise PEError('runaway for')
            return None
        if t == 'forin':
            it = self.evalargs(st[2], scope)
            if it and it[0] is OPENUPS:
                self.emit(('close', self.close_base(st, scope)))
                return None
            if it and isinstance(it[0], LuaTable) and len(it) == 1:
                for k, v in list(it[0].h.items()):
                    sc = Scope(scope)
                    sc.vars[st[1][0]] = k
                    if len(st[1]) > 1:
                        sc.vars[st[1][1]] = v
                    r = None
                    for s in st[3]:
                        r = self.exec_stmt(s, sc)
                        if r is not None:
                            break
                    if r is not None:
                        if r[0] == 'break':
                            break
                        if r[0] == 'return':
                            return r
                return None
            if it and it[0] is None:
                raise PEError('for-in over nil')
            raise PEError('unsupported for-in over %r' % (it[:1],))
        if t == 'return':
            vals = self.evalargs(st[1], scope)
            return ('return', vals)
        if t == 'break':
            return ('break',)
        if t == 'continue':
            return ('continue',)
        if t == 'do':
            return self.exec_block(st[1], scope)
        if t == 'localfunc':
            scope.vars[st[1]] = LFn(st[2], scope)
            return None
        raise PEError('exec: unknown stmt %s' % t)

    def close_base(self, st, scope):
        """for k, v in openups do if k >= X then ... -> X"""
        body = st[3]
        kname = st[1][0]
        if body and body[0][0] == 'if':
            c = body[0][1][0][0]
            if c[0] == 'bin':
                if c[2] == ('name', kname):
                    return self.eval1(c[3], scope)
                if c[3] == ('name', kname):
                    return self.eval1(c[2], scope)
        raise PEError('unrecognised close-upvalues loop')

    def exec_seek(self, st, scope):
        """Prologue / fast-forward execution towards the dispatch loop to resume."""
        t = st[0]
        target = self.inject[0] if self.inject else None
        if t == 'while' and id(st) in self.loop_ids:
            if target is None or self.model.loops[target] is st:
                return self.exec_dispatch(st, scope)
            return None
        if target is not None and t in ('if', 'while', 'repeat', 'fornum', 'forin', 'do'):
            anc = self.loop_anc[id(self.model.loops[target])]
            if id(st) not in anc:
                return None
            bodies = []
            if t == 'if':
                bodies = [b for _, b in st[1]] + ([st[2]] if st[2] else [])
            elif t == 'while':
                bodies = [st[2]]
            elif t == 'do':
                bodies = [st[1]]
            for b in bodies:
                if any(id(s) in anc for s in b):
                    return self.exec_block(b, scope)
            raise PEError('seek: lost target loop')
        return self.exec_core(st, scope)

    def exec_dispatch(self, st, scope):
        """One iteration of a dispatch loop."""
        lid = self.loop_ids[id(st)]
        self.injected = set()
        if not self.seeking:
            # arrived at a dispatch loop after leaving another one
            raise LoopHead(lid, self.snapshot(scope))
        self.seeking = False
        if self.inject is None:
            raise LoopHead(lid, self.snapshot(scope))
        for name, v in self.inject[1].items():
            s = scope.lookup(name)
            if s is not None:
                s.vars[name] = v
                self.injected.add((id(s), name))
        self.exposed = set()
        self.written = set()
        self.track = True
        r = self.exec_block(st[2], scope)
        if r is None or r[0] == 'continue':
            raise LoopHead(lid, self.snapshot(scope))
        if r[0] == 'break':
            return None
        return r


def substmts(st):
    t = st[0]
    if t in ('while',):
        return st[2]
    if t == 'do' or t == 'repeat':
        return st[1]
    if t == 'if':
        out = []
        for _, b in st[1]:
            out.extend(b)
        if st[2]:
            out.extend(st[2])
        return out
    if t == 'fornum':
        return st[5]
    if t == 'forin':
        return st[3]
    return []


def lua_tostr(v):
    if isinstance(v, str):
        return v
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 1e15:
            return str(int(v))
        return repr(v)
    return str(v)
