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
import os
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
        self.orig = bytes(data)         # as dumped
        self.path = None

    def patches(self):
        """[(offset, bytes)]: where the buffer differs from the dump"""
        if self.data == self.orig:
            return []
        out = []
        d, o = self.data, self.orig
        n = min(len(d), len(o))
        i = 0
        while i < n:
            j = min(n, i + 4096)
            if d[i:j] != o[i:j]:
                k = i
                while k < j:
                    if d[k] != o[k]:
                        e = k + 1
                        while e < j and (d[e] != o[e] or (e + 1 < j and d[e + 1] != o[e + 1])):
                            e += 1
                        out.append((k, bytes(d[k:e])))
                        k = e
                    else:
                        k += 1
            i = j
        return out


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


class ConstFn:
    """A loader-installed function that always returns the same number."""
    def __init__(self, value, k=None):
        self.value = value
        self.k = k


class GlobalPath:
    """A well-known runtime object that is spelled as a global path (Enum.KeyCode.E, ...)."""
    def __init__(self, text):
        self.text = text


FREE_LIBS = {'string', 'table', 'math', 'bit32', 'buffer', 'coroutine', 'debug', 'os', 'utf8', 'task', 'vector'}
FREE_FUNCS = {'select', 'type', 'typeof', 'tostring', 'tonumber', 'pcall', 'xpcall', 'error', 'assert', 'next', 'pairs',
              'ipairs', 'rawget', 'rawset', 'rawequal', 'rawlen', 'setmetatable', 'getmetatable', 'unpack', 'getfenv',
              'setfenv', 'print', 'warn', 'require', 'newproxy', 'loadstring', 'gcinfo', 'tick', 'time', 'wait',
              'spawn', 'delay', 'elapsedTime', 'collectgarbage', 'islclosure', 'iscclosure', 'identifyexecutor'}
FREE_GLOBALS = {'game', 'workspace', 'script', 'shared', '_G', 'plugin'}


def _unparen(e):
    while e[0] == 'paren':
        e = e[1]
    return e


class KNum(float):
    """A number derived from the loader's key functions. Unlike ordinary values it is tracked
    across instructions, because self-modifying code uses it as patch address / patch value."""
    __slots__ = ()


class UpNum(float):
    """The number an upvalue held at the end of the traced run.  Luraph mixes opaque numbers kept in
    upvalues into decryption keys; whether a given upvalue is one of those is not known, so the
    value is only used when an instruction cannot be lifted without it (PE.use_upnum)."""
    __slots__ = ()


def keyed(r, *ops):
    if is_num(r) and not isinstance(r, KNum) and any(isinstance(o, KNum) for o in ops):
        return KNum(r)
    return r


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


class PackedList:
    """table.pack(a, b, ...) of known arity made by the interpreter itself (it packs call results
    and return values before moving them): the values, without materialising a table."""
    __slots__ = ('vals',)

    def __init__(self, vals):
        self.vals = list(vals)


class MultiTail:
    __slots__ = ('id',)

    def __init__(self, id):
        self.id = id


class Scope:
    __slots__ = ('vars', 'parent', 'boundary', 'owner')

    def __init__(self, parent=None, boundary=False):
        self.vars = {}
        self.parent = parent
        self.boundary = boundary
        self.owner = parent.owner if parent is not None else None     # id of the function it belongs to

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


def lua_unpack(fmt, data):
    """string.unpack for plain fixed-size numeric formats; None when the format is not supported."""
    raw = data.encode('latin-1')
    endian = '<'
    out = []
    pos = 0
    i = 0
    sizes = {'b': 1, 'B': 1, 'h': 2, 'H': 2, 'l': 8, 'L': 8, 'j': 8, 'J': 8, 'T': 8}
    while i < len(fmt):
        c = fmt[i]
        i += 1
        if c in '<=':
            endian = '<'
        elif c == '>':
            endian = '>'
        elif c == ' ':
            pass
        elif c in 'bBhHlLjJTiI':
            n = sizes.get(c, 4)
            j = i
            while j < len(fmt) and fmt[j].isdigit():
                j += 1
            if c in 'iI' and j > i:
                n = int(fmt[i:j])
                i = j
            if pos + n > len(raw):
                return None
            out.append(int.from_bytes(raw[pos:pos + n], 'little' if endian == '<' else 'big', signed=c in 'bhlji'))
            pos += n
        elif c in 'fdn':
            n = 4 if c == 'f' else 8
            if pos + n > len(raw):
                return None
            out.append(norm(struct.unpack(endian + ('f' if c == 'f' else 'd'), raw[pos:pos + n])[0]))
            pos += n
        else:
            return None
    out.append(pos + 1)
    return out


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
    'buffer.writestring': lambda b, off, st, n=None: _buf_writestring(b, off, st, n),
    'buffer.copy': lambda dst, doff, src, soff=0, n=None: _buf_copy(dst, doff, src, soff, n),
    'buffer.fill': lambda b, off, v, n=None: _buf_fill(b, off, v, n),
    'buffer.readbits': lambda b, bitoff, n: _buf_readbits(b, bitoff, n),
    'buffer.writebits': lambda b, bitoff, n, v: _buf_writebits(b, bitoff, n, v),
}


def _buf_writestring(b, off, st, n):
    raw = st.encode('latin-1')
    if n is not None:
        raw = raw[:int(n)]
    off = int(off)
    if off < 0 or off + len(raw) > len(b.data):
        raise PEError('buffer access out of bounds')
    b.data[off:off + len(raw)] = raw
    return None


def _buf_copy(dst, doff, src, soff, n):
    soff = int(soff or 0)
    n = len(src.data) - soff if n is None else int(n)
    doff = int(doff)
    if soff < 0 or doff < 0 or soff + n > len(src.data) or doff + n > len(dst.data):
        raise PEError('buffer access out of bounds')
    dst.data[doff:doff + n] = bytes(src.data[soff:soff + n])
    return None


def _buf_fill(b, off, v, n):
    off = int(off)
    n = len(b.data) - off if n is None else int(n)
    b.data[off:off + n] = bytes([int(v) & 0xFF]) * n
    return None


def _buf_readbits(b, bitoff, n):
    v = int.from_bytes(bytes(b.data), 'little')
    return (v >> int(bitoff)) & ((1 << int(n)) - 1)


def _buf_writebits(b, bitoff, n, val):
    bitoff, n = int(bitoff), int(n)
    v = int.from_bytes(bytes(b.data), 'little')
    mask = ((1 << n) - 1) << bitoff
    v = (v & ~mask) | ((int(val) << bitoff) & mask)
    b.data[:] = v.to_bytes(len(b.data), 'little')
    return None


# ------------------------------------------------------------------ dump loading
class Dump:
    def __init__(self, data, model):
        self.data = data
        self.model = model
        self.tables = {}
        self.by_path = {}
        self.raw = data['tables']
        self.server = None      # vmmodel.PoolServer: the finished run, kept alive for pool constants
        self.buffers = []       # Buffer objects with a path (their changes go along with pool requests)

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
        if 'lfn' in v:
            if 'fid' in v:
                fs = [self.model.funcs_by_id[v['fid']]]
                v = dict(v, lfn=fs[0].line)
            else:
                fs = self.model.func_by_line.get(v['lfn'], [])
            if 'cenv' in v:
                # a loader closure whose captured variables were recorded by the harness
                fs = [x for x in fs if x.id not in self.model.toplevel] or fs
            f = LFn(fs[0] if len(fs) == 1 else None, None, v['lfn'])
            f.path = path
            if 'cenv' in v and f.func is not None:
                sc = Scope()
                sc.vars = self.table(v['cenv']['t'], (path or '__LPH_CLOSENV') + '.upvalues').h
                f.scope = sc
            elif f.func is not None and f.func.id in self.model.toplevel:
                f.scope = Scope()      # top-level function: nothing captured
            return f
        if 'lib' in v:
            return Lib(v['lib'])
        if 'env' in v:
            return ENV
        if 'buf' in v:
            b = Buffer(bytes.fromhex(v['buf']))
            b.path = path
            if path:
                self.buffers.append(b)
            return b
        if 'cfn' in v:
            return ConstFn(self.conv(v['cfn']), v.get('k'))
        if 'g' in v:
            return GlobalPath(v['g'])
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
        if path:
            self.by_path[path] = t
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

    def proto_records(self):
        """[(interpreter index, {factory parameter: value})] for factory-style VMs, 1-based pid order."""
        out = []
        for i, p in enumerate(self.data['protos']):
            rec = self.conv(p, '__LPH_PROTOS[%d]' % (i + 1)).h
            out.append((rec.get('interp'), rec['vars'].h if isinstance(rec.get('vars'), LuaTable) else {}))
        return out

    def proto_upvals(self, pid):
        """The first table a proto's closure was bound to (harness: protos[pid].ups), or None."""
        rec = self.conv(self.data['protos'][pid - 1], '__LPH_PROTOS[%d]' % pid).h
        ups = rec.get('ups')
        if isinstance(ups, LuaTable):
            for i in sorted(k for k in ups.h if isinstance(k, int)):
                if isinstance(ups.h[i], LuaTable):
                    return ups.h[i].h
        return None


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
        self.state_sets = {it.index: self._classify_state_vars(it.loops) for it in model.interps}
        self.set_interp(model.interp_funcs[id(model.interp)])
        self.events = []
        self.wrap_func = None
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
        self.state_writes = set()
        self.regknown = {}
        self.regdirty = set()
        self.regdirty_all = False
        self.reg_reads = []       # registers read by the instruction being evaluated (known or not)
        self.nregs = None
        self.keyfn_override = {}
        self.known_reads = set()
        self.use_numknown = False
        self.write_log = None
        self.state_scopes = None
        self.model_writes = 0
        self.keyfn_used = set()
        self.lazy_depth = 0         # >0 while a lazy-constant __index handler of the loader runs
        # Lifting a dump taken at the end of a run: instructions that were executed have already
        # left their mark on the VM state (decrypted code, decoded constants), so their writes to
        # that state are dropped (frozen).  Writes of the other instructions are applied and
        # journalled, so that lifting an instruction again starts from the same state.
        self.frozen = False
        self.live_table_patches = None  # callable -> (('T', path, key, number), ...): shared-table entries this path changed
        self.use_upnum = False
        self.replay_tables = set()  # ids of code arrays whose changes are replayed even for instructions that ran
        self.journal = None
        self.protect = None         # (path prefix of the proto's code arrays, executed pcs): known-final code
        from . import luprint
        self.native_ids = {id(f) for f in model.funcs if luprint.native_shape(f)}
        self.volatile = set()       # paths of loader-state tables whose scalar fields change at run time
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

        for it in self.model.interps:
            walk_block(it.func.body, [])
        return res

    def set_interp(self, it):
        """Select which interpreter's variable classification applies (names clash between interpreters)."""
        self.cur_interp = it
        self.fvars, self.fstack, self.openups = self.state_sets[it.index]

    def _classify_state_vars(self, loops):
        """Find interpreter-level locals used as for-loop state / open-upvalue map."""
        fvars = set()
        fstack = set()
        openups = set()

        def visit(st):
            t = st[0]
            if t == 'assign' and len(st[1]) == len(st[2]):
                # `stack = {..., stack}`, also as one target of a multiple assignment
                # (`stack, a = {[9]=limit, [7]=stack, ...}, operand[pc]`)
                for tg, val in zip(st[1], st[2]):
                    while val[0] == 'paren':
                        val = val[1]
                    if tg[0] != 'name' or val[0] != 'table':
                        continue
                    nm = tg[1]
                    vals = [v for _, _, v in val[1]]
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
            if t == 'if':
                # `if open then local c = open[r]; if c then c[4] = c; c[5] = R[r]; c[6] = 5;
                # open[r] = nil end end`: handlers that end the scope of single registers
                for c, body in st[1]:
                    if c[0] == 'name' and body and body[0][0] == 'local':
                        txt = repr(body[:3])
                        if "('num', 6)" in txt and "('num', 5)" in txt and "('num', 4)" in txt and "[('nil',)]" in txt:
                            openups.add(c[1])
            for sub in substmts(st):
                visit(sub)

        for l in loops:
            for st in l[2]:
                visit(st)
        return fvars, fstack, openups

    # -- emission
    def newt(self):
        self.tcount += 1
        return ('t', '%s_%d' % (self.tprefix, self.tcount))

    def emit(self, st):
        # a register written by this instruction no longer holds the value known on entry
        if st[0] == 'set' and st[1][0] == 'r':
            self.regdirty.add(st[1][1])
        elif st[0] in ('setlist', 'params') or (st[0] == 'set' and st[1][0] not in ('t', 'r')):
            self.regdirty_all = True
        self.ir.append(st)

    def emit_expr(self, e, origin=None):
        t = self.newt()
        self.ir.append(('set', t, e))
        return Sym(t, origin)

    def atom(self, v):
        if isinstance(v, Sym):
            return v.e
        if isinstance(v, PackedList):
            # used as a value after all (the script's own table.pack): make the table
            return self.emit_call(('kfn', 'table.pack'), list(v.vals), 1)[0].e
        if isinstance(v, PackedTbl) and v.id not in ('args', 'va'):
            return self.emit_call(('kfn', 'table.pack'), [MultiTail(v.id)], 1)[0].e
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
        if isinstance(v, GlobalPath):
            return ('outer', v.text)
        if isinstance(v, ConstFn):
            return ('constfn', v.value, v.k)
        if isinstance(v, UpRef):
            # the upvalue *cell* {regs, _, slot}; generic table instructions may open it later
            return ('upcell', v.n)
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

    def check_reg(self, n):
        if isinstance(n, int) and (n < 0 or (self.nregs is not None and n > self.nregs + 4096)):
            raise PEError('register %d out of range (bad self-modifying patch?)' % n)

    def read_reg(self, n):
        self.check_reg(n)
        if isinstance(n, int):
            self.reg_reads.append(n)
        t = self.regknown.get(n)
        if n in self.regdirty or self.regdirty_all:
            t = None
        if isinstance(t, UpNum):
            t = KNum(t) if self.use_upnum else None
        if t is not None and n != '__captured__' and (self.use_numknown or isinstance(t, KNum) or
                                                      not (is_num(t) or isinstance(t, bool))):
            # the register is known to hold one of the program's own code/constant arrays
            # (self-modifying code): keep it concrete so the patch can be applied to the model.
            # Plain numbers are only used to resolve jumps through a register (use_numknown).
            self.known_reads.add(n)
            return t
        return self.emit_expr(('r', n), ('r', n))

    def regs_range(self, a, b):
        """values of R[a..b] where b may be symbolic top."""
        if isinstance(b, LinSym):
            vals = [self.read_reg(i) for i in range(int(a), int(b.k) + 1)]
            vals.append(MultiTail(b.id))
            return vals
        return [self.read_reg(i) for i in range(int(a), int(b) + 1)]

    # -- scopes
    def getvar(self, scope, name):
        s = scope.lookup(name)
        if s is None:
            # raw (not decompiled) scripts name globals directly in their handlers
            if name in FREE_LIBS:
                return Lib(name)
            if name in FREE_FUNCS:
                return Builtin(name)
            if self.model.oneline:
                # Roblox datatypes and executor functions (gethui, fireproximityprompt, ...)
                return GlobalPath(name)
            raise PEError('unbound name %s' % name)
        if s.owner == self.cur_interp.func.id and (self.state_scopes is None or id(s) in self.state_scopes):
            # interpreter-level state variables (names are only meaningful inside the interpreter)
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

    def is_state(self, scope, name):
        s = scope.lookup(name)
        return s is not None and s.owner == self.cur_interp.func.id and \
            (self.state_scopes is None or id(s) in self.state_scopes)

    def setvar(self, scope, name, v):
        s = scope.lookup(name)
        if s is None:
            raise PEError('assignment to unbound name %s' % name)
        if s.owner == self.cur_interp.func.id and (name in self.fvars or name in self.fstack) \
                and (self.state_scopes is None or id(s) in self.state_scopes):
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
                # decided like a branch: the instruction is lifted once per outcome
                return self.eval1(e[2], scope) if self.cond(c) else self.eval1(e[3], scope)
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
        if isinstance(key, KNum) and key.is_integer():
            key = int(key)
        if isinstance(obj, LuaTable):
            if isinstance(key, (Sym, LinSym)):
                if obj.path and not obj.fresh:
                    return self.emit_expr(('index', ('outer', obj.path), self.atom(key)))
                raise PEError('symbolic key into concrete table T%s' % obj.id)
            if isinstance(key, float):
                key = norm(key)
            v = obj.h.get(key)
            if v is None and isinstance(obj.mt, LuaTable) and not self.seeking:
                kt, et = obj.h.get(-1), obj.h.get(-2)
                if isinstance(kt, LuaTable) and isinstance(et, LuaTable) and is_num(et.h.get(key)):
                    # {[-1] = pool, [-2] = index array}: the metamethod (VM code) returns
                    # pool[index[pc]]; the harness has decoded the pool (fillPools)
                    idx = et.h.get(key)
                    if is_num(idx) and not isinstance(idx, bool) and float(idx).is_integer():
                        idx = int(idx)
                        v = kt.h.get(idx)
                        if os.environ.get('DEVM_POOLDBG') and isinstance(v, str) and any(ord(c) > 160 for c in v) and self.dump.server is not None:
                            patches = tuple((b.path, off, data) for b in self.dump.buffers for off, data in b.patches())
                            print('POOLDBG table', obj.id, 'pc', key, 'idx', idx, repr(v)[:40], 'server says', repr(self.dump.server.decode(obj.id, idx, patches))[:120],
                                  'neighbours', [repr(kt.h.get(idx + d))[:30] for d in (-2, -1, 1, 2)])
                        if v is None and self.dump.server is not None and not isinstance(idx, KNum):
                            # not decoded during the run: ask the run itself (it is still alive)
                            # (with the bytecode as this path has left it: decoding a nested
                            # function reads bytes that earlier instructions decrypt in place)
                            patches = tuple((b.path, off, data) for b in self.dump.buffers for off, data in b.patches())
                            if self.live_table_patches is not None:
                                patches += tuple(self.live_table_patches())
                            raw = self.dump.server.decode(obj.id, idx, patches, key if isinstance(key, int) and not isinstance(key, bool) else None)
                            if os.environ.get('DEVM_POOLDBG') and isinstance(raw, dict) and 's' in raw and any(c > 160 for c in bytes.fromhex(raw['s'])):
                                print('POOLDBG table', obj.id, 'pc', key, 'idx', idx, raw['s'][:40], 'patches', [(p_[0][-20:], p_[1], p_[2][:12]) for p_ in patches if p_[0] != 'T'][:6], 'T', len([p_ for p_ in patches if p_[0] == 'T']), [p_[1:] for p_ in patches if p_[0] == 'T'][:12])
                            # (a function built for the answer added entries to shared tables)
                            for tid, ents in self.dump.server.tdiffs:
                                live = self.dump.tables.get(tid)
                                if live is not None:
                                    for k, x in ents:
                                        live.h.setdefault(k, norm(x))
                            del self.dump.server.tdiffs[:]
                            if raw is not None and not (isinstance(raw, dict) and 'o' in raw):
                                v = self.dump.conv(raw)
                        return v
                h = obj.mt.h.get('__index')
                if isinstance(h, LFn) and h.scope is not None:
                    # lazily materialised entry (e.g. a constant decoded from the bytecode buffer)
                    self.lazy_depth += 1
                    try:
                        vals = self.call_lfn(h, [obj, key], 1)
                    finally:
                        self.lazy_depth -= 1
                    return vals[0] if vals else None
                if isinstance(h, LuaTable):
                    return self.index(h, key, keyast)
            if obj.path in self.volatile and (v is None or is_num(v) or isinstance(v, bool)) and not self.seeking \
                    and self.inject is not None:
                # loader state that changes while the program runs (e.g. the stream position)
                if self.trace is not None:
                    self.trace.append('   volatile read %s[%r]' % (obj.path, key))
                return self.emit_expr(('index', ('outer', obj.path), self.atom(key)))
            return v
        if obj is REGS:
            if isinstance(key, int):
                return self.read_reg(key)
            raise PEError('register index %r' % (key,))
        if isinstance(obj, Sym):
            return self.emit_expr(('index', obj.e, self.atom(key)))
        if obj is UPVALS:
            if not isinstance(key, int):
                raise PEError('upvalue index %r' % (key,))
            ups = getattr(self, 'upvals_list', None)
            if ups is not None:
                # plain-value upvalue lists (the payload interpreter of some builds): numbers and
                # library functions bound once by the bootstrap
                v = ups.get(key)
                if isinstance(v, (Builtin, Lib, GlobalPath)):
                    return v
                # A number, string or boolean here is what one instance of the function captured
                # by value: another instance has another (`makeToggle("A", ...)`, `makeToggle("B",
                # ...)`), so in the script's own layout it stays the variable it is.  (Opaque
                # numbers among them are still available as keys: see UpNum.)
                if (is_num(v) or isinstance(v, (str, bool))) and not self.model.oneline:
                    return v
            return UpRef(key - self.model.up_base)
        if isinstance(obj, UpRef):
            if keyast is not None and keyast[0] == 'num':
                if key == self.model.cells[0]:
                    return UpBox(obj.n)
                if key == self.model.cells[1]:
                    return UpKey(obj.n)
            return self.emit_expr(('index', ('upcell', obj.n), self.atom(key)))
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
            if key == 'n':
                return LinSym(obj.id, 0)
            raise PEError('packed index %r' % (key,))
        if isinstance(obj, PackedList):
            if key == 'n':
                return len(obj.vals)
            if isinstance(key, int):
                return obj.vals[key - 1] if 1 <= key <= len(obj.vals) else None
            raise PEError('packed index %r' % (key,))
        if obj is OPENUPS:
            return None
        if isinstance(obj, Lib):
            return Builtin(obj.name + '.' + key)
        if isinstance(obj, str):
            return Builtin('string.' + key)
        if obj is FSTATE:
            return FSTATE
        if isinstance(obj, UpKey) or obj is None or isinstance(obj, (int, float, bool)):
            if self.perm is not None:
                return FSTATE
            raise PEError('attempt to index %r' % (obj,))
        if isinstance(obj, (Builtin, LFn, Opaque, Buffer, GlobalPath)):
            return self.emit_expr(('index', self.atom(obj), self.atom(key)))
        raise PEError('index: unsupported object %r' % (obj,))

    def setindex(self, obj, key, val, keyast=None):
        if isinstance(key, KNum) and key.is_integer():
            key = int(key)
        if isinstance(val, UpNum) and isinstance(obj, LuaTable) and not obj.fresh:
            # a patch computed with an upvalue's number: in the code it is an ordinary operand
            val = norm(float(val))
        if isinstance(key, UpNum):
            key = norm(float(key))
        if isinstance(obj, LuaTable):
            if isinstance(key, (Sym, LinSym)):
                if obj.path and not obj.fresh:
                    self.emit(('setindex', ('outer', obj.path), self.atom(key), self.atom(val)))
                    return
                raise PEError('symbolic key store into concrete table')
            if obj.escaped:
                raise PEError('store into escaped table')
            if isinstance(key, float):
                key = norm(key)
            if not obj.fresh and self.frozen and obj.path not in self.volatile and id(obj) not in self.replay_tables:
                return
            if not obj.fresh and self.protect is not None and key in self.protect[1] and obj.path \
                    and obj.path.startswith(self.protect[0]):
                # an instruction that never ran must not rewrite one that did (and is in its final form)
                return
            if not obj.fresh and isinstance(val, (Sym, LinSym, MultiTail)) and obj.path not in self.volatile:
                raise PEError('self-modifying store of a run-time value into %s[%r]' % (obj.path or obj.id, key) + (' <%r was %r>' % (val, obj.h.get(key)) if os.environ.get('DEVM_PEDBG') else ''))
            if obj.path and not obj.fresh and not self.seeking and self.inject is not None \
                    and obj.path in self.volatile:
                # a VM instruction writing into loader state that outlives the instruction:
                # this must happen at run time, not inside the evaluator's model
                self.emit(('setindex', ('outer', obj.path), self.atom(key), self.atom(val)))
                self.state_writes.add((obj.path, key))
                return
            if not obj.fresh:
                self.model_writes += 1
                if self.write_log is not None and obj.path and obj.h.get(key) != val:
                    self.write_log.append((id(obj), key))
                if self.journal is not None:
                    self.journal.append((obj, key, obj.h.get(key)))
            obj.set(key, val)
            return
        if obj is REGS:
            if not isinstance(key, int):
                raise PEError('register store index %r' % (key,))
            self.check_reg(key)
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
            self.emit(('setindex', ('upcell', obj.n), self.atom(key), self.atom(val)))
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
                return keyed(norm(-v), v)
        if op == '#':
            if isinstance(v, str):
                return len(v)
            if isinstance(v, LuaTable):
                return v.length()
            if isinstance(v, PackedTbl):
                return LinSym(v.id, 0)
            if isinstance(v, PackedList):
                return len(v.vals)
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
                if isinstance(a, (Special, UpRef, Builtin, LuaTable, GlobalPath, Opaque)) or \
                        isinstance(b, (Special, UpRef, Builtin, LuaTable, GlobalPath, Opaque)):
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
            return keyed(norm(r), a, b)
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
        if isinstance(f, ConstFn):
            v = self.keyfn_override.get(f.k, f.value)
            self.keyfn_used.add(f.k)
            if v is None:
                raise PEError('key function %s could not be evaluated' % (f.k,))
            return [KNum(v)]
        if isinstance(f, Builtin):
            return self.call_builtin(f.name, args, want)
        if isinstance(f, LFn):
            return self.call_lfn(f, args, want)
        if isinstance(f, ProtoRef):
            return [self.make_closure([f] + list(args))]
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
            if self.seeking:
                if args and is_num(args[0]):
                    self.nregs = int(args[0])
                return [REGS]
            if self.lazy_depth:
                # the loader building a constant (e.g. a capture list): part of the model, not of the program
                return [LuaTable()]
            n = args[0] if args and is_num(args[0]) else 0
            return [self.emit_expr(('newtable', n))]
        if name == 'setmetatable' and self.lazy_depth and len(args) == 2 and isinstance(args[0], LuaTable) \
                and (args[1] is None or isinstance(args[1], LuaTable)):
            # the loader wiring up the lazy constant tables of a proto it has just deserialized
            args[0].mt = args[1]
            return [args[0]]
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
        if name == 'table.pack':
            if len(args) == 1 and isinstance(args[0], MultiTail):
                return [PackedTbl(args[0].id)]
            if not any(isinstance(a, MultiTail) for a in args):
                return [PackedList(args)]
        if name in ('unpack', 'table.unpack'):
            t = args[0]
            if t is REGS:
                return self.regs_range(args[1], args[2])
            if isinstance(t, PackedList) and len(args) == 3 and args[1] == 1 and args[2] == len(t.vals):
                return list(t.vals)
            if isinstance(t, PackedTbl) and len(args) == 3 and args[1] == 1 and isinstance(args[2], LinSym) \
                    and args[2].id == t.id and args[2].k == 0:
                return [MultiTail(t.id)]
            if isinstance(t, LuaTable) and not sym:
                a = args[1] if len(args) > 1 and args[1] is not None else 1
                b = args[2] if len(args) > 2 and args[2] is not None else t.length()
                return [t.h.get(i) for i in range(a, b + 1)]
        if name == 'table.move' and len(args) == 5 and args[4] is REGS and is_num(args[1]) and is_num(args[2]) \
                and is_num(args[3]) and (args[0] is VARARG_TBL or (isinstance(args[0], PackedTbl) and args[0].id == 'args')):
            # function prologue: the fixed parameters are copied into a block of registers
            if args[1] == 1:
                self.emit(('params', int(args[3]), int(args[2])))
            else:
                for i in range(int(args[1]), int(args[2]) + 1):
                    self.emit(('set', ('r', int(args[3]) + i - int(args[1])), ('arg', i)))
            return [REGS]
        if name == 'table.move' and len(args) == 5 and args[4] is REGS and args[1] == 1 and is_num(args[2]) and is_num(args[3]) \
                and isinstance(args[0], (PackedTbl, PackedList)):
            # the interpreter keeping the first n results of a call: R[A .. A+n-1] = results
            n, base = int(args[2]), int(args[3])
            if isinstance(args[0], PackedList):
                for i in range(n):
                    v = args[0].vals[i] if i < len(args[0].vals) else None
                    self.emit(('set', ('r', base + i), self.atom(v)))
            else:
                cid = args[0].id
                for j, st in enumerate(self.ir):
                    if st[0] == 'call' and st[1] == cid and st[5] == -1:
                        self.ir[j] = st[:5] + (n,)
                for i in range(n):
                    self.emit(('set', ('r', base + i), ('mv', cid, i + 1)))
            return [REGS]
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
        if not sym:
            if name == 'string.unpack' and len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], str):
                r = lua_unpack(args[0], args[1])
                if r is not None:
                    return r
            if name in PURE and all(is_num(a) for a in args):
                return [keyed(norm(PURE[name](*args)), *args)]
            if name == 'buffer.create' and args and is_num(args[0]) and 0 <= args[0] <= 1 << 26:
                return [Buffer(bytes(int(args[0])))]
            if name == 'buffer.fromstring' and args and isinstance(args[0], str):
                return [Buffer(args[0].encode('latin-1'))]
            if name in BUF and args and isinstance(args[0], Buffer):
                if 'write' in name or name in ('buffer.copy', 'buffer.fill'):
                    if args[0].path:
                        if self.frozen:
                            return [None]
                        self.model_writes += 1
                        if self.journal is not None and not any(j[0] is args[0] for j in self.journal if j[1] is None):
                            self.journal.append((args[0], None, bytes(args[0].data)))    # once per instruction
                return [BUF[name](*args)]
            if name == 'error':
                raise PEError('VM raised error: %r' % (args[:1],))
        if name in BUF and ('write' in name or name in ('buffer.copy', 'buffer.fill')) and args \
                and isinstance(args[0], Buffer) and args[0].path:
            # the VM patching its own bytecode with a value only known at run time: not part of
            # the script (and already done in the dump when the instruction ran)
            if getattr(self, 'sym_buf_error', False) and not self.use_upnum:
                raise PEError('self-modifying store of a run-time value into the bytecode buffer')
            return [None]
        if self.lazy_depth and not sym and all(is_num(a) or isinstance(a, (str, bool)) or a is None for a in args):
            # the loader materialising a constant of a Roblox datatype, e.g. Vector3.new(x, y, z)
            return [GlobalPath('%s(%s)' % (name, ', '.join(const_text(a) for a in args)))]
        return self.emit_call(('kfn', name), args, want)

    def call_lfn(self, f, args, want):
        # the vararg packer: f(...) -> count, {...}
        if args and isinstance(args[-1], MultiTail) and id(f.func) not in self.model.interp_funcs and (f.func is None or f.func.vararg):
            mid = args[-1].id
            if len(args) == 1:
                return [LinSym(mid, 0), PackedTbl(mid) if mid != 'args' else VARARG_TBL]
        if f.func is None:
            raise PEError('call of unresolved VM closure (line %s)' % f.line)
        if id(f.func) in self.native_ids and len(args) <= 3 and not self.lazy_depth \
                and all(a is None or a is False or (isinstance(a, LuaTable) and a.fresh) for a in args):
            # non-virtualised function instantiated like a proto: native(nil, upvalue_list, nil)
            ups = [a for a in args if isinstance(a, LuaTable)]
            return [self.make_closure([('native', f.func.line), ups[0] if ups else None])]
        if f.func is self.wrap_func and f.scope is None or (f.func is self.wrap_func and len(args) == 2 and isinstance(args[0], LuaTable)):
            return [self.make_closure(args)]
        if f.scope is None:
            # foreign helper: recognise the unpack-like helper (start, stop, regs)
            if any(a is REGS for a in args):
                nums = [a for a in args if a is not REGS]
                if len(nums) == 2:
                    a, b = self.unpack_order(f.func, args, nums)
                    return self.regs_range(a, b)
            if len(args) == 1 and (isinstance(args[0], LuaTable) and args[0].fresh or args[0] is False):
                # non-virtualised function: native(upvalue_table) -> closure
                return [self.make_closure([('native', f.func.line), args[0]])]
            ups = [a for a in args if a is not None]
            if len(args) == 3 and len(ups) <= 1 and all(isinstance(a, LuaTable) and a.fresh for a in ups):
                # same, called like a proto wrapper: native(nil, upvalue_table, nil)
                return [self.make_closure([('native', f.func.line), ups[0] if ups else None])]
            raise PEError('call of foreign VM closure (line %s) with %r' % (f.line, args))
        func = f.func
        is_interp = id(func) in self.model.interp_funcs
        sc = Scope(f.scope, boundary=is_interp)
        sc.owner = func.id
        if is_interp:
            self.interp_scope = sc
        for i, p in enumerate(func.params):
            if i < len(args) and not isinstance(args[i], MultiTail):
                sc.vars[p] = args[i]
            else:
                sc.vars[p] = None
        if func.vararg:
            sc.vars['...'] = args[len(func.params):]
        r = self.exec_block(func.body, sc)
        if r is not None and r[0] == 'return':
            return r[1]
        return []

    def unpack_order(self, func, args, nums):
        """(start, stop) for the VM's unpack-like helper, from `p = p or 1` in its body."""
        start_param = None
        for st in func.body:
            if st[0] == 'assign' and st[1][0][0] == 'name' and _unparen(st[2][0])[0] == 'bin' and _unparen(st[2][0])[1] == 'or' \
                    and _unparen(st[2][0])[3] == ('num', 1):
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
            if isinstance(ups, LuaTable):
                n = self.model.up_base
                while n in ups.h:
                    v = ups.h[n]
                    if isinstance(v, LuaTable) and v.h.get(self.model.cells[0]) is REGS:
                        caps.append(('ref', v.h.get(self.model.cells[1])))
                    elif isinstance(v, Sym) and v.origin:
                        caps.append(('val', v.origin[1]))
                    elif isinstance(v, UpRef):
                        caps.append(('up', v.n))
                    elif (is_num(v) or isinstance(v, (str, bool))) and not isinstance(v, Sym):
                        # captured by value from a register whose content is a known constant
                        caps.append(('const', v))
                    elif isinstance(v, (GlobalPath, Builtin)):
                        # ... or a library function / datatype (`local insert = table.insert`)
                        caps.append(('const', v))
                    else:
                        raise PEError('unknown upvalue capture %r' % (v,))
                    n += 1
            if isinstance(proto, tuple):
                key = proto
            else:
                key = self.on_closure(proto) if self.on_closure else proto
            st[2] = ('closure', key, tuple(caps))
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
            return True         # a count is a number, and every number is truthy
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

    trace = None

    def exec_core(self, st, scope):
        t = st[0]
        if self.trace is not None:
            self.trace.append(repr(st)[:300] if st[0] != 'assign' else repr(st)[:3000])
        if t == 'if':
            if len(st[1]) == 1 and not st[2] and len(st[1][0][1]) == 4 and st[1][0][0][0] == 'name':
                # `local c = open[r]; if c then c[4] = c; c[5] = R[r]; c[6] = 5; open[r] = nil end`:
                # the variable in register r goes out of scope (its upvalue cell is closed)
                last = st[1][0][1][3]
                if last[0] == 'assign' and len(last[1]) == 1 and last[1][0][0] == 'index' and last[2] == [('nil',)] \
                        and "('num', 6)" in repr(st[1][0][1][2]):
                    try:
                        holder = self.eval1(last[1][0][1], scope)
                    except PEError:
                        holder = None
                    if holder is OPENUPS:
                        r = self.eval1(last[1][0][2], scope)
                        if isinstance(r, float) and r.is_integer():
                            r = int(r)
                        if isinstance(r, int):
                            self.emit(('close', r, r))
                        return None
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
                    if nm in self.fstack and _unparen(st[2][0])[0] == 'table' and self.is_state(scope, nm):
                        if self.perm is None:
                            raise ForOp()
                        self.events.append(('push',))
                        return None
                    if nm in self.openups and self.is_state(scope, nm):
                        return None
                    self.setvar(scope, nm, self.eval1(st[2][0], scope))
                else:
                    obj = self.eval1(tg[1], scope)
                    key = self.eval1(tg[2], scope)
                    self.setindex(obj, key, self.eval1(st[2][0], scope), tg[2])
                return None
            if len(targets) == len(st[2]) and any(
                    tg[0] == 'name' and tg[1] in self.fstack and e[0] == 'table' and self.is_state(scope, tg[1])
                    for tg, e in zip(targets, st[2])):
                # for-state push as one target of a multiple assignment
                if self.perm is None:
                    raise ForOp()
                self.events.append(('push',))
                rest = [(tg, e) for tg, e in zip(targets, st[2])
                        if not (tg[0] == 'name' and tg[1] in self.fstack and e[0] == 'table')]
                vals = [self.eval1(e, scope) for _, e in rest]
                for (tg, _), v in zip(rest, vals):
                    self.assign_target(tg, v, scope)
                return None
            vals = self.adjust(self.evalargs(st[2], scope), len(targets))
            if self.trace is not None:
                self.trace.append('   -> %r' % (vals,))
                self.trace.append('   fn %r' % ([self.eval1(e[1], scope) for e in st[2] if e[0] == 'call'],))
            for tg, v in zip(targets, vals):
                self.assign_target(tg, v, scope)
            return None
        if t == 'local':
            names = st[1]
            if len(names) == 1 and len(st[2]) == 1:
                scope.vars[names[0]] = self.eval1(st[2][0], scope)
                if self.trace is not None:
                    self.trace.append('   -> %s = %r' % (names[0], scope.vars[names[0]]))
                return None
            vals = self.adjust(self.evalargs(st[2], scope), len(names)) if st[2] else [None] * len(names)
            for n, v in zip(names, vals):
                scope.vars[n] = v
            return None
        if t == 'opassign':
            tg = st[2]
            if tg[0] == 'name':
                if tg[1] in self.fvars and self.is_state(scope, tg[1]):
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
            if len(st[2]) == 3 and st[2][2] == ('nil',) and st[2][0][0] == 'name' and st[2][1][0] == 'name':
                # `for k[, v] in next, open_upvalues, nil do v[4] = ...; v[6] = ...; open_upvalues[k] = nil end`
                # (handlers that leave the function close its upvalues inline): no counterpart in
                # the lifted code
                txt = repr(st[3])
                if "('num', 4)" in txt and "('num', 6)" in txt and "[('nil',)]" in txt:
                    return None
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
            if it and isinstance(it[0], Builtin) and it[0].name == 'next' and len(it) >= 2:
                # `for k in next, open_upvalues do <close it> end` of an interpreter that keeps its
                # open-upvalue maps in plain locals: closing has no counterpart in the lifted code
                return None
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
            while c[0] == 'paren' or (c[0] == 'un' and c[1] == 'not'):
                c = c[-1]
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
        # interpreter-level scopes: a handler's own locals may reuse the names of state variables
        self.state_scopes = set()
        sc = scope
        while sc is not None:
            self.state_scopes.add(id(sc))
            sc = sc.parent
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


def const_text(v):
    """Lua literal for a constant argument; float32-valued numbers print in their short form."""
    if v is None:
        return 'nil'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, str):
        return '"%s"' % ''.join(c if 32 <= ord(c) < 127 and c not in '"\\' else '\\%d' % ord(c) for c in v)
    if isinstance(v, float) and not v.is_integer() and math.isfinite(v):
        f32 = struct.unpack('<f', struct.pack('<f', v))[0] if abs(v) < 3e38 else None
        if f32 == v:
            for prec in range(1, 10):
                t = '%.*g' % (prec, v)
                if struct.unpack('<f', struct.pack('<f', float(t)))[0] == v:
                    return t
        return repr(v)
    return lua_tostr(v)


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
