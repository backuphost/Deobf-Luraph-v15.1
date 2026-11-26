#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
luascope.py: scope-exact renaming and slot hints from the luau-ast JSON

The readable-name pass used to track scopes itself on the token stream (libs/readable3.py:
rename).  That is a guess: it leaked `for` scopes, missed `a, b = ...` targets and
`function name()` statements, and so could bind a renamed use to the wrong variable.  Here the
binding of every identifier comes from Luau's own parser (luau-ast prints, for every use of a
local, the position of its declaration), so a rename can only ever replace a declaration and
exactly the uses the parser bound to it.

What this module provides

  LineIndex            (line, column) of an AST location  ->  offset in the text
  Renamer              new names for the locals of one AST subtree; the naming heuristics are
                       the ones of readable3.derive (unchanged), the scoping is the parser's
  slot_hints           `tbl[N]` reads whose slot is assigned exactly once, from a plain local,
                       in the visible code: the name of that local is a reading aid
  alpha_equal          proof obligation for the two above: two ASTs are the same program up to
                       a consistent renaming of locals

Nothing here runs the analysed script.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

try:
    from readable3 import derive, sanitize, BAD_BASE, RESERVED
except ImportError:                                   # imported as libs.luascope
    from .readable3 import derive, sanitize, BAD_BASE, RESERVED

_LOC_KEYS = frozenset(('location', 'argLocation', 'varargLocation', 'indexLocation',
                       'nameLocation', 'prefixLocation', 'keyLocation', 'debugname',
                       'functionDepth', 'commentLocations'))
_GENERIC_BASE = frozenset(('v', 'val', 'Res', 'num', 'str', 'cond', 'flag'))
_LOOPS = frozenset(('AstStatWhile', 'AstStatRepeat', 'AstStatFor', 'AstStatForIn'))

_FN_HINTS = (
    ('RenderStepped', 'onFrame'), ('Heartbeat', 'onTick'), ('Stepped', 'onStep'),
    ('InputBegan', 'onInput'), ('InputChanged', 'onInputChanged'),
    ('InputEnded', 'onInputEnded'), ('MouseButton1Click', 'onClick'),
    ('pcall', 'safeCall'), ('Drawing', 'draw'), ('Raycast', 'raycast'),
    ('getgenv', 'setupEnv'), ('writefile', 'saveFile'), ('readfile', 'loadFile'),
    ('Tween', 'tween'), ('Create', 'create'), ('Destroy', 'destroy'),
    ('Disconnect', 'disconnect'), ('Humanoid', 'onHumanoid'), ('Character', 'onCharacter'),
    ('Camera', 'onCamera'), ('Health', 'onHealth'), ('Update', 'update'),
    ('Render', 'render'), ('Refresh', 'refresh'), ('Draw', 'draw'),
)


class ScopeError(Exception):
    """The analysis disagrees with the parser (or the AST has a shape it does not know)."""


def run_deep(fn, *args, **kw):
    """Run `fn` on a thread with a large stack: the AST of a one-line obfuscated script nests
    deeper than Python's default recursion limit allows.  Where such a thread cannot be
    created (small devices), smaller stacks are tried and finally the current thread."""
    box = {}

    def target():
        try:
            box['value'] = fn(*args, **kw)
        except BaseException as e:              # noqa: BLE001  (re-raised below)
            box['error'] = e

    old_limit = sys.getrecursionlimit()
    try:
        old_size = threading.stack_size()
    except Exception:                           # noqa: BLE001
        old_size = None
    started = False
    try:
        for megabytes, limit in ((512, 400000), (128, 100000), (32, 25000)):
            try:
                threading.stack_size(megabytes * 1024 * 1024)
                sys.setrecursionlimit(max(old_limit, limit))
                th = threading.Thread(target=target)
                th.start()
            except (ValueError, RuntimeError, MemoryError):
                continue
            started = True
            th.join()
            break
        if not started:
            sys.setrecursionlimit(max(old_limit, 5000))
            target()
    finally:
        if old_size is not None:
            try:
                threading.stack_size(old_size)
            except (ValueError, RuntimeError):
                pass
        sys.setrecursionlimit(old_limit)
    if 'error' in box:
        raise box['error']
    return box.get('value')


def parse(text, ast_bin, timeout=300):
    """Parse `text` with luau-ast.  Returns (root node, [parse error lines])."""
    fd, path = tempfile.mkstemp(prefix='lv151_ast_', suffix='.lua')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', errors='surrogateescape', newline='') as fh:
            fh.write(text)
        r = subprocess.run([ast_bin, path], capture_output=True, timeout=timeout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    out = r.stdout.decode('utf-8', errors='replace')
    err = r.stderr.decode('utf-8', errors='replace')
    start = out.find('{"root"')
    if start < 0:
        start = out.find('{')
    if start < 0:
        raise RuntimeError('luau-ast printed no AST: ' + (out + err).strip()[:300])
    # luau-ast still prints a (partial) tree when the source has syntax errors; the errors
    # go to stderr as "  (line, col) - (line, col) - message" and the exit status is 1
    errors = [ln.strip() for ln in (out[:start] + '\n' + err).splitlines()
              if ln.strip() and not ln.startswith('Parse errors were encountered')]
    if r.returncode != 0 and not errors:
        errors = ['luau-ast exit status %d' % r.returncode]
    data = run_deep(json.loads, out[start:])
    return data.get('root', data), errors


class LineIndex:
    """Offsets of AST locations.  luau-ast counts lines from 0 and columns in BYTES."""

    def __init__(self, text):
        self.text = text
        self.starts = [0] + [m.end() for m in re.finditer('\n', text)]
        self.ascii = text.isascii()
        self._maps = {}

    def _line_map(self, line):
        m = self._maps.get(line)
        if m is None:
            st = self.starts[line]
            en = self.starts[line + 1] if line + 1 < len(self.starts) else len(self.text)
            m = []
            for k, ch in enumerate(self.text[st:en]):
                m.extend([k] * len(ch.encode('utf-8', 'surrogateescape')))
            m.append(en - st)
            self._maps[line] = m
        return m

    def offset(self, line, col):
        if line >= len(self.starts):
            return len(self.text)
        if self.ascii:
            return self.starts[line] + col
        m = self._line_map(line)
        return self.starts[line] + m[min(col, len(m) - 1)]

    def span(self, loc):
        """'l,c - l,c'  ->  (start offset, end offset)"""
        a, b = loc.split(' - ')
        l1, c1 = a.split(',')
        l2, c2 = b.split(',')
        return self.offset(int(l1), int(c1)), self.offset(int(l2), int(c2))


def collect_functions(node, out=None):
    """Every AstExprFunction node below `node` (iterative: no recursion limit)."""
    out = [] if out is None else out
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            if n.get('type') == 'AstExprFunction':
                out.append(n)
            stack.extend(v for v in n.values() if isinstance(v, (dict, list)))
        elif isinstance(n, list):
            stack.extend(v for v in n if isinstance(v, (dict, list)))
    return out


def globals_in(node):
    """{global name: number of uses} below `node` (reads and writes; iterative)."""
    out = {}
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            if n.get('type') == 'AstExprGlobal':
                name = n.get('global')
                out[name] = out.get(name, 0) + 1
            stack.extend(v for v in n.values() if isinstance(v, (dict, list)))
        elif isinstance(n, list):
            stack.extend(v for v in n if isinstance(v, (dict, list)))
    return out


def _statements(fn):
    body = fn.get('body') or {}
    return body.get('body', []) if body.get('type') == 'AstStatBlock' else []


def factory_leaf(fn):
    """Closure-factory shape of an AstExprFunction: (innermost function, depth).

    Luraph keeps a function that is not virtualized as a factory in its VM object,

        [n] = function(<vm object, ...>) return function(<seeds, captured variables>)
                  local a = captured[1]; local b = captured[2]
                  return function(...) <the function itself, plain Lua> end end end

    and the VM instantiates it like one of its own closures.  The shape, exactly: the body
    of `fn` is nothing but `return function ... end`; each further level may first unpack
    captured variables with `local` statements and then again ends in `return function`.
    depth 0 = `fn` does not have this shape (the innermost function is then `fn` itself)."""
    cur, depth = fn, 0
    while True:
        stats = _statements(cur)
        if not stats:
            break
        last = stats[-1]
        values = last.get('list', []) if last.get('type') == 'AstStatReturn' else []
        if len(values) != 1 or values[0].get('type') != 'AstExprFunction':
            break
        lead = stats[:-1]
        if (depth == 0 and lead) or any(st.get('type') != 'AstStatLocal' for st in lead):
            break
        cur = values[0]
        depth += 1
    return cur, depth


# ------------------------------------------------------------------------------------------
# scope walk
# ------------------------------------------------------------------------------------------
class _Local:
    __slots__ = ('key', 'name', 'kind', 'decl_tok', 'refs', 'pinned', 'assigned', 'new',
                 'scope_id', 'block_id', 'decl_off', 'init', 'index', 'fn_tok')

    def __init__(self, key, name, kind):
        self.key = key
        self.name = name
        self.kind = kind            # local | localfn | param | self | for | forin | external
        self.decl_tok = None        # token index of the declaring identifier
        self.refs = []              # token indices of uses
        self.pinned = kind in ('self', 'external')
        self.assigned = 0           # assignments after the declaration
        self.new = None
        self.scope_id = None
        self.block_id = None        # id of the AstStatBlock node that declares it
        self.decl_off = -1
        self.init = None            # (first token, end token) of the initialising expression
        self.index = 0              # position among the variables of its statement
        self.fn_tok = None          # token index of `function` for a local function


class Analysis:
    """One walk over an AST subtree that reproduces Luau's scoping, checks every use against
    the declaration the parser recorded for it, and collects what the passes below need."""

    def __init__(self, node, index, toks):
        """`node`: AST subtree; `index`: LineIndex of the text the AST was parsed from;
        `toks`: [(type, text, offset)] covering the subtree (offsets into that same text)."""
        self.node = node
        self.index = index
        self.toks = toks
        self.tok_at = {off: i for i, (_t, _v, off) in enumerate(toks)}
        self.locals = {}            # key -> _Local
        self.order = []             # _Local in declaration order
        self.globals = set()
        self.slot_writes = {}       # (table key, N) -> [write record]
        self.slot_reads = {}        # (table key, N) -> [read record]
        self.table_uses = {}        # table key -> number of uses that are not `T[number]`
        self.table_escapes = {}     # table key -> uses that hand the table itself to visible code
                                    # (copied to a variable or field, passed to a function,
                                    # returned, compared ...): another name may then write it
        self.table_handed = {}      # table key -> [receiver local key or None] for every use as
                                    # an item of a table constructor that is an argument of a
                                    # method call: `obj:m(1, 2, {[1] = T, ...})`, the shape in
                                    # which Luraph hands captured variables to a VM closure
        self.table_dynw = {}        # table key -> writes `T[<not a constant>] = ...`
        self.empty_tables = set()   # keys of locals declared as `local T = {}`
        self._as_base = set()       # ids of AstExprLocal nodes that are the base of an index
        self._as_handed = {}        # id of an AstExprLocal node -> receiver key (see above)
        self._scopes = []           # [{name: _Local}]
        self._scope_ids = []
        self._blocks = []           # ids of the enclosing AstStatBlock nodes
        self._anc = []              # (type, id) of the enclosing nodes
        self._next_scope = 0
        self._span = index.span
        run_deep(self._walk, node)

    # ---- helpers
    def _key(self, loc_node):
        return (loc_node['location'], loc_node['name'])

    def _push(self):
        self._scopes.append({})
        self._next_scope += 1
        self._scope_ids.append(self._next_scope)

    def _pop(self):
        self._scopes.pop()
        self._scope_ids.pop()

    def _declare(self, loc_node, kind, init=None, index=0, fn_tok=None):
        key = self._key(loc_node)
        if key in self.locals:
            raise ScopeError('local declared twice: %r' % (key,))
        lc = _Local(key, loc_node['name'], kind)
        start, _end = self._span(loc_node['location'])
        lc.decl_off = start
        ti = self.tok_at.get(start)
        if ti is not None and self.toks[ti][0] == 'w' and self.toks[ti][1] == lc.name:
            lc.decl_tok = ti
        else:
            lc.pinned = True        # `self`, or a declaration that is not a plain name token
        lc.scope_id = self._scope_ids[-1] if self._scope_ids else 0
        lc.block_id = self._blocks[-1] if self._blocks else None
        lc.init = init
        lc.index = index
        lc.fn_tok = fn_tok
        self.locals[key] = lc
        self.order.append(lc)
        if not self._scopes:
            self._push()
        self._scopes[-1][lc.name] = lc
        return lc

    def _lookup(self, name):
        for sc in reversed(self._scopes):
            lc = sc.get(name)
            if lc is not None:
                return lc
        return None

    def _tok_range(self, node):
        s, e = self._span(node['location'])
        a = self.tok_at.get(s)
        if a is None:
            return None
        b = a
        n = len(self.toks)
        while b < n and self.toks[b][2] < e:
            b += 1
        return a, b

    def _use(self, node):
        ref = node['local']
        key = self._key(ref)
        lc = self.locals.get(key)
        found = self._lookup(ref['name'])
        if lc is None:
            if found is not None:
                raise ScopeError('use of %r at %s binds outside the subtree for the parser, '
                                 'inside it here' % (ref['name'], node['location']))
            lc = _Local(key, ref['name'], 'external')
            self.locals[key] = lc
        elif found is not lc and lc.kind != 'external':
            raise ScopeError('use of %r at %s: the parser binds it to %s, the scope walk to %s'
                             % (ref['name'], node['location'], key[0],
                                found.key[0] if found else None))
        start, _end = self._span(node['location'])
        ti = self.tok_at.get(start)
        if ti is not None and self.toks[ti][0] == 'w' and self.toks[ti][1] == lc.name:
            lc.refs.append(ti)
        else:
            lc.pinned = True        # e.g. a use inside an interpolated string token
        return lc

    def _slot_of(self, node):
        """(table local key, N) when `node` is `T[<number literal>]` with T a local."""
        if node.get('type') != 'AstExprIndexExpr':
            return None
        ex, ix = node['expr'], node['index']
        if ex.get('type') != 'AstExprLocal' or ix.get('type') != 'AstExprConstantNumber':
            return None
        val = ix.get('value')
        if not isinstance(val, (int, float)) or val != int(val):
            return None
        return self._key(ex['local']), int(val)

    def _snapshot(self):
        return tuple(self._anc), tuple(self._blocks), tuple(self._scope_ids)

    def _slot_write(self, var, stmt, value, single):
        slot = self._slot_of(var)
        self._use(var['expr'])
        rec = {'stmt': stmt, 'value': value, 'single': single, 'ctx': self._snapshot(),
               'block': self._blocks[-1] if self._blocks else 0}
        self.slot_writes.setdefault(slot, []).append(rec)

    def _dynamic_write(self, var):
        """`T[<not a constant>] = ...`: which slot is written is not known."""
        if var.get('type') != 'AstExprIndexExpr':
            return
        ex, ix = var.get('expr') or {}, var.get('index') or {}
        if ex.get('type') != 'AstExprLocal':
            return
        if ix.get('type') in ('AstExprConstantString', 'AstExprConstantNumber'):
            return                  # a string key, or a number that is no slot (1.5)
        key = self._key(ex['local'])
        self.table_dynw[key] = self.table_dynw.get(key, 0) + 1

    # ---- the walk
    def _walk(self, n, parent_key=None):
        if isinstance(n, list):
            for x in n:
                self._walk(x, parent_key)
            return
        if not isinstance(n, dict):
            return
        ty = n.get('type')
        if ty is None:
            for k, v in n.items():
                if isinstance(v, (dict, list)):
                    self._walk(v, k)
            return
        self._anc.append((ty, id(n)))
        try:
            self._node(n, ty)
        finally:
            self._anc.pop()

    def _children(self, n, skip=()):
        for k, v in n.items():
            if k in skip or k in _LOC_KEYS:
                continue
            if isinstance(v, (dict, list)):
                self._walk(v, k)

    def _node(self, n, ty):
        if ty == 'AstStatBlock':
            self._push()
            self._blocks.append(id(n))
            try:
                self._walk(n.get('body', []))
            finally:
                self._blocks.pop()
                self._pop()
        elif ty == 'AstStatLocal':
            values = n.get('values', [])
            self._walk(values)
            for v in n.get('vars', []):
                self._walk(v.get('luauType'))
            for i, v in enumerate(n.get('vars', [])):
                init = self._tok_range(values[i]) if i < len(values) else None
                lc = self._declare(v, 'local', init, i)
                if (i < len(values) and values[i].get('type') == 'AstExprTable'
                        and not values[i].get('items')):
                    self.empty_tables.add(lc.key)
        elif ty == 'AstStatLocalFunction':
            fn = n['func']
            s, _e = self._span(fn['location'])
            # the function's location starts at `local` or at `function`, depending on the
            # Luau version: find the `function` keyword itself
            ft = self.tok_at.get(s)
            while ft is not None and ft < len(self.toks) and self.toks[ft][1] != 'function':
                ft += 1
            self._declare(n['name'], 'localfn', fn_tok=ft)
            self._walk(fn)
        elif ty == 'AstExprFunction':
            self._push()
            try:
                if n.get('self'):
                    self._declare(n['self'], 'self')
                for a in n.get('args', []):
                    self._walk(a.get('luauType'))
                for i, a in enumerate(n.get('args', [])):
                    self._declare(a, 'param', index=i)
                self._children(n, skip=('self', 'args'))
            finally:
                self._pop()
        elif ty == 'AstStatFor':
            self._walk(n.get('from'))
            self._walk(n.get('to'))
            self._walk(n.get('step'))
            self._push()
            try:
                self._declare(n['var'], 'for')
                self._walk(n.get('body'))
            finally:
                self._pop()
        elif ty == 'AstStatForIn':
            self._walk(n.get('values', []))
            self._push()
            try:
                vs = n.get('vars', [])
                for i, v in enumerate(vs):
                    lc = self._declare(v, 'forin', index=i)
                    lc.init = (len(vs), i)
                self._walk(n.get('body'))
            finally:
                self._pop()
        elif ty == 'AstStatRepeat':
            # the condition sees the locals of the body
            body = n.get('body') or {}
            self._push()
            self._blocks.append(id(body))
            self._anc.append(('AstStatBlock', id(body)))
            try:
                self._walk(body.get('body', []))
                self._walk(n.get('condition'))
            finally:
                self._anc.pop()
                self._blocks.pop()
                self._pop()
        elif ty == 'AstStatAssign':
            vars_, values = n.get('vars', []), n.get('values', [])
            self._walk(values)
            single = len(vars_) == 1 and len(values) == 1
            for i, v in enumerate(vars_):
                if self._slot_of(v) is not None:
                    self._slot_write(v, n, values[i] if i < len(values) else None, single)
                elif v.get('type') == 'AstExprLocal':
                    self._use(v).assigned += 1
                else:
                    self._dynamic_write(v)
                    self._walk(v)
        elif ty == 'AstStatCompoundAssign':
            self._walk(n.get('value'))
            v = n['var']
            if self._slot_of(v) is not None:
                self._slot_write(v, n, None, False)
            elif v.get('type') == 'AstExprLocal':
                self._use(v).assigned += 1
            else:
                self._dynamic_write(v)
                self._walk(v)
        elif ty == 'AstStatFunction':
            name = n['name']
            if name.get('type') == 'AstExprLocal':
                self._use(name).assigned += 1
            else:
                self._walk(name)
            self._walk(n['func'])
        elif ty == 'AstExprLocal':
            lc = self._use(n)
            self.table_uses[lc.key] = self.table_uses.get(lc.key, 0) + 1
            if id(n) in self._as_base:
                pass                # `T[expr]` / `T.name`: the table itself goes nowhere
            elif id(n) in self._as_handed:
                self.table_handed.setdefault(lc.key, []).append(self._as_handed[id(n)])
            else:
                self.table_escapes[lc.key] = self.table_escapes.get(lc.key, 0) + 1
        elif ty == 'AstExprIndexName':
            if (n.get('expr') or {}).get('type') == 'AstExprLocal':
                self._as_base.add(id(n['expr']))
            self._children(n)
        elif ty == 'AstExprCall':
            fn = n.get('func') or {}
            if n.get('self') and fn.get('type') == 'AstExprIndexName':
                recv = fn.get('expr') or {}
                rkey = self._key(recv['local']) if recv.get('type') == 'AstExprLocal' else None
                for arg in n.get('args', []):
                    if arg.get('type') != 'AstExprTable':
                        continue
                    for item in arg.get('items', []):
                        val = item.get('value') or {}
                        if val.get('type') == 'AstExprLocal':
                            self._as_handed[id(val)] = rkey
            self._children(n)
        elif ty == 'AstExprGlobal':
            self.globals.add(n.get('global'))
        elif ty == 'AstExprIndexExpr':
            slot = self._slot_of(n)
            if slot is not None:
                self._use(n['expr'])
                rng = self._tok_range(n)
                # which of the locals assigned to this slot so far does their own NAME still
                # reach here?  (`local s = 1; T[1] = s; do local s = 2; print(T[1]) end`:
                # writing `s` for that read would name the inner variable)
                visible = set()
                for w in self.slot_writes.get(slot, ()):
                    val = w['value']
                    if val is not None and val.get('type') == 'AstExprLocal':
                        ref = val['local']
                        lc = self.locals.get(self._key(ref))
                        if lc is not None and self._lookup(ref['name']) is lc:
                            visible.add(lc.key)
                self.slot_reads.setdefault(slot, []).append(
                    {'node': n, 'range': rng, 'ctx': self._snapshot(),
                     'off': self._span(n['location'])[0], 'visible': visible})
            else:
                if (n.get('expr') or {}).get('type') == 'AstExprLocal':
                    self._as_base.add(id(n['expr']))
                self._children(n)
        elif ty == 'AstLocal':
            raise ScopeError('declaration in a place this walk does not know: %s'
                             % n.get('location'))
        else:
            self._children(n)


# ------------------------------------------------------------------------------------------
# renaming
# ------------------------------------------------------------------------------------------
class Renamer:
    """Choose readable names for the locals of an Analysis.

    A new name is accepted only if it is not a keyword, not a global the code uses, and not
    the name of any local that is visible where the new one is declared; so a renamed
    declaration can neither capture a use of an outer variable or of a global nor be captured
    by an inner declaration (every inner declaration is checked against this one in turn)."""

    def __init__(self, analysis):
        self.a = analysis
        self.names = {}             # token index -> new text
        self.renamed = 0
        self._visible = {}          # name -> number of visible locals called so
        self._scope_names = []
        self._fixed = set(BAD_BASE) | set(RESERVED) | set(analysis.globals)
        for lc in analysis.locals.values():
            # names that stay as they are (outer locals, `self`, locals used inside an
            # interpolated string) are never given to another variable: an inner one of them
            # would capture the uses of an outer local that was renamed to its name
            if lc.kind == 'external' or lc.pinned:
                self._fixed.add(lc.name)
        run_deep(self._walk, analysis.node)

    def _taken(self, nm):
        return nm in self._fixed or self._visible.get(nm, 0) > 0

    def _push(self):
        self._scope_names.append([])

    def _pop(self):
        for nm in self._scope_names.pop():
            self._visible[nm] -= 1

    def _bind(self, lc, base):
        orig = lc.name
        if lc.pinned or lc.decl_tok is None:
            new = orig
        else:
            base = sanitize(base) or 'val'
            new = None
            if (len(orig) <= 2 and orig.isalpha() and orig.isascii()
                    and base in _GENERIC_BASE and not self._taken(orig)):
                new = orig
            if new is None:
                if self._taken(base):
                    m = 2
                    while self._taken(base + str(m)):
                        m += 1
                    base = base + str(m)
                new = base
        lc.new = new
        if not self._scope_names:
            self._push()
        self._scope_names[-1].append(new)
        self._visible[new] = self._visible.get(new, 0) + 1
        if new != orig:
            self.renamed += 1
            self.names[lc.decl_tok] = new

    def _base_for(self, lc):
        a = self.a
        toks = a.toks
        if lc.kind == 'param':
            return 'arg'
        if lc.kind == 'for':
            return 'i'
        if lc.kind == 'forin':
            count, q = lc.init
            if count == 2:
                return 'i' if q == 0 else 'item'
            return 'k' if q % 2 == 0 else 'item'
        if lc.kind == 'localfn':
            base = 'fn'
            if lc.fn_tok is not None:
                body = ' '.join(t[1] for t in toks[lc.fn_tok:lc.fn_tok + 400])
                for kw, nm in _FN_HINTS:
                    if kw in body:
                        base = nm
                        break
            return base
        if lc.kind == 'local':
            seg = None
            if lc.init is not None:
                s, e = lc.init
                seg = [(t[0], t[1]) for t in toks[s:e]]
                first = s
                if seg and first in self._ref_new:
                    # name things after what the variable is called NOW (tbl_60, not B_60)
                    seg[0] = ('w', self._ref_new[first])

                def resolve(name, _first=first):
                    # derive() only ever asks about the first word of the expression
                    return self._ref_new.get(_first, name)
            else:
                def resolve(name):
                    return name
            if seg and len(seg) == 1 and seg[0][0] == 'w' and seg[0][1] in RESERVED:
                # `local x = nil` / `= true`: nothing to name it after (not `nil2`, `true2`)
                base = 'flag' if seg[0][1] in ('true', 'false') else None
            else:
                base = derive(seg, resolve) if seg else None
            if base is None:
                nm = lc.name
                base = nm if (nm.isidentifier() and nm.isascii()) else 'v'
            return base
        return lc.name

    def _walk(self, node):
        """Replays the declarations in scope order.  Uses the Analysis' own walk order by
        re-walking the AST with the same scoping rules, but only to keep the set of visible
        NEW names; the bindings themselves were fixed (and checked) by the Analysis."""
        a = self.a
        self._ref_new = {}          # token index of a use -> new name of the local it is

        def declare(loc_node):
            lc = a.locals[(loc_node['location'], loc_node['name'])]
            self._bind(lc, self._base_for(lc))
            if lc.new != lc.name:
                for ti in lc.refs:
                    self.names[ti] = lc.new
            for ti in lc.refs:
                self._ref_new[ti] = lc.new

        def walk(n):
            if isinstance(n, list):
                for x in n:
                    walk(x)
                return
            if not isinstance(n, dict):
                return
            ty = n.get('type')
            if ty == 'AstStatBlock':
                self._push()
                walk(n.get('body', []))
                self._pop()
            elif ty == 'AstStatLocal':
                walk(n.get('values', []))
                for v in n.get('vars', []):
                    declare(v)
            elif ty == 'AstStatLocalFunction':
                declare(n['name'])
                walk(n['func'])
            elif ty == 'AstExprFunction':
                self._push()
                if n.get('self'):
                    declare(n['self'])
                for arg in n.get('args', []):
                    declare(arg)
                walk(n.get('body'))
                self._pop()
            elif ty == 'AstStatFor':
                walk(n.get('from'))
                walk(n.get('to'))
                walk(n.get('step'))
                self._push()
                declare(n['var'])
                walk(n.get('body'))
                self._pop()
            elif ty == 'AstStatForIn':
                walk(n.get('values', []))
                self._push()
                for v in n.get('vars', []):
                    declare(v)
                walk(n.get('body'))
                self._pop()
            elif ty == 'AstStatRepeat':
                body = n.get('body') or {}
                self._push()
                walk(body.get('body', []))
                walk(n.get('condition'))
                self._pop()
            elif ty in ('AstExprLocal', 'AstLocal'):
                return
            else:
                for k, v in n.items():
                    if k not in _LOC_KEYS and isinstance(v, (dict, list)):
                        walk(v)

        walk(node)

    def apply(self, toks):
        """Token list [(type, text)] with the new names put in."""
        out = [(t[0], t[1]) for t in toks]
        for ti, new in self.names.items():
            out[ti] = ('w', new)
        return out


# ------------------------------------------------------------------------------------------
# slot hints
# ------------------------------------------------------------------------------------------
def slot_table_problem(analysis, tkey):
    """Why reads of the slots of table local `tkey` cannot be named, or None.

    A slot name says "the only assignment to this slot that the visible code makes is
    `T[N] = name`".  That can only be claimed when the visible code has no second way to
    write the table:

      * T is declared as `local T = {}` and never assigned again (a parameter, or a local
        that was initialised from something else, may be one table under two names);
      * there is no write `T[<expression>] = ...` whose slot is not known;
      * T itself is not copied anywhere the visible code could write it through:
        `local alias = T`, `x.t = T`, `f(T)`, `table.insert(T, v)`, `return T` all count.
        The one use that is let through is Luraph's own way of handing captured variables to
        a closure it creates, `obj:method(..., {[k] = T, ...}, ...)` with `obj` a parameter
        (the VM object) that is never assigned: what such a closure writes is outside the
        visible code in any case (the output's header says so)."""
    a = analysis
    tab = a.locals.get(tkey)
    if tab is None:
        return 'unknown table'
    if tab.kind != 'local' or tkey not in a.empty_tables:
        return 'not declared as `local T = {}`'
    if tab.assigned:
        return 'assigned again'
    if a.table_dynw.get(tkey):
        return 'written with a computed index'
    if a.table_escapes.get(tkey):
        return 'copied or passed on'
    for rkey in a.table_handed.get(tkey, ()):
        recv = a.locals.get(rkey) if rkey is not None else None
        if recv is None or recv.kind not in ('param', 'external') or recv.assigned:
            return 'handed to a method of an object built in the visible code'
    return None


def slot_hints(analysis):
    """Reads of `T[N]` whose slot has, in the visible code, exactly one assignment, and that
    assignment is `T[N] = <a local that is never assigned again>`; T must be a table the
    visible code cannot write under another name (slot_table_problem).

    Returns a list of dicts: {'range': (first token, end token), 'node': the AstExprIndexExpr,
    'local': the _Local assigned to the slot, 'table': original name of T, 'table_local': the
    _Local of T, 'slot': N, 'inline': bool, 'shadowed': bool}.

    `shadowed`: at the read, the local's name means another variable (an inner declaration of
    the same name).  Such a read gets no name at all, not even as a comment: the comment
    would point the reader at the wrong variable.

    `inline` is True when, as far as the visible code goes, the read can only see that value:
    T is declared as `local T = {}`, T is only ever used as `T[number]`, the assignment runs at
    most once per T (no loop or function between T's declaration and it), the read sits
    after the assignment inside the assignment's own block, where the local is still in scope,
    and the name that would be written there still means that local: either the local was
    given a new name by a Renamer on this Analysis (those are unique among everything in
    scope), or its own name is not shadowed at the read.
    Code the tool cannot see (a virtualized function that got the table as an upvalue) may
    still write the slot, so even `inline` is evidence, not proof; the caller decides whether
    to print the name as a comment (always safe) or to substitute it, and must compare the
    substituted text with the original (alpha_equal(..., inlined))."""
    a = analysis
    out = []
    span = a.index.span
    problems = {}
    for slot, writes in a.slot_writes.items():
        tkey, num = slot
        if len(writes) != 1:
            continue
        w = writes[0]
        val = w['value']
        if not w['single'] or val is None or val.get('type') != 'AstExprLocal':
            continue
        lc = a.locals.get((val['local']['location'], val['local']['name']))
        tab = a.locals.get(tkey)
        if lc is None or tab is None or lc.assigned or lc.kind == 'external':
            continue
        if tkey not in problems:
            problems[tkey] = slot_table_problem(a, tkey)
        if problems[tkey] is not None:
            continue
        reads = a.slot_reads.get(slot, [])
        if not reads:
            continue
        # T is only ever used as T[number] (table_uses counts every other use of the local)
        w_anc, _w_blocks, _w_scopes = w['ctx']
        once = False
        if (tkey in a.empty_tables and tab.kind == 'local' and not tab.assigned
                and a.table_uses.get(tkey, 0) == 0 and tab.block_id is not None):
            # walk the ancestors of the assignment from the outside in: below the block that
            # declares T there must be no loop and no function
            inside = False
            once = True
            for (ty, nid) in w_anc:
                if inside and (ty in _LOOPS or ty == 'AstExprFunction'):
                    once = False
                    break
                if ty == 'AstStatBlock' and nid == tab.block_id:
                    inside = True
            once = once and inside
        s_end = span(w['stmt']['location'])[1]
        for r in reads:
            if r['range'] is None:
                continue
            _r_anc, r_blocks, r_scopes = r['ctx']
            unshadowed = ((lc.new is not None and not lc.pinned)
                          or lc.key in r.get('visible', ()))
            inline = (once and r['off'] >= s_end and w['block'] in r_blocks
                      and lc.scope_id in r_scopes and lc.decl_off < r['off'] and unshadowed)
            out.append({'range': r['range'], 'node': r['node'], 'local': lc,
                        'table': tab.name, 'table_local': tab, 'slot': num,
                        'inline': inline, 'shadowed': not unshadowed})
    return out


# ------------------------------------------------------------------------------------------
# alpha equivalence
# ------------------------------------------------------------------------------------------
def _decl_ordinals(node):
    """location+name of every declaration  ->  its position in document order"""
    ords = {}
    stack = [(node, None)]
    seq = []
    while stack:
        n, pk = stack.pop()
        if isinstance(n, dict):
            if n.get('type') == 'AstLocal':
                if pk != 'local':
                    seq.append((n['location'], n['name']))
                continue
            for k in reversed(list(n.keys())):
                v = n[k]
                if isinstance(v, (dict, list)) and k not in _LOC_KEYS:
                    stack.append((v, k))
        elif isinstance(n, list):
            for v in reversed(n):
                stack.append((v, pk))
    for i, key in enumerate(seq):
        ords.setdefault(key, i)
    return ords


_FOLDABLE = frozenset(('AstExprBinary', 'AstExprUnary', 'AstExprGroup'))


def alpha_equal(a, b, inlined=None, folds=False):
    """Are the ASTs `a` and `b` the same program up to a consistent renaming of locals?

    `inlined`: {location string of an AstExprIndexExpr in `a`: (location, name) of the local
    that was substituted for it}; such a node may face an AstExprLocal in `b`.
    `folds`: a constant arithmetic expression in `a` may face a number in `b`, if that number
    is the value Luau computes for the expression (libs/luaunum.py; the sign of zero counts).
    Returns (True, '') or (False, where and why)."""
    oa, ob = _decl_ordinals(a), _decl_ordinals(b)
    inlined = inlined or {}
    if folds:
        try:
            import luaunum
        except ImportError:                           # imported as libs.luascope
            from . import luaunum
    stack = [(a, b, 'root')]
    while stack:
        x, y, path = stack.pop()
        if isinstance(x, dict) and isinstance(y, dict):
            tx, ty = x.get('type'), y.get('type')
            if tx == 'AstExprIndexExpr' and ty == 'AstExprLocal' and x.get('location') in inlined:
                want = oa.get(inlined[x['location']])
                got = ob.get((y['local']['location'], y['local']['name']))
                if want is None or want != got:
                    return False, '%s: inlined slot at %s names another local' % (
                        path, x.get('location'))
                continue
            if tx == 'AstExprLocal' and ty == 'AstExprGlobal':
                # A function cut out of its file: a local of the surrounding code (declared
                # outside `a`) reads as a global of the same name once the function stands
                # alone.  Such names are never changed by the Renamer.
                ref = x.get('local') or {}
                if (oa.get((ref.get('location'), ref.get('name'))) is None
                        and ref.get('name') == y.get('global')):
                    continue
            if folds and tx in _FOLDABLE and ty == 'AstExprConstantNumber':
                try:
                    val = luaunum.eval_ast(x)
                    got = float(y.get('value'))
                except (luaunum.Unfoldable, TypeError, ValueError):
                    return False, '%s: %s at %s was replaced by a number, but it is not a ' \
                        'constant every Luau computes alike' % (path, tx, x.get('location'))
                if not luaunum.same_number(val, got):
                    return False, '%s: constant at %s is %r, the output has %r' % (
                        path, x.get('location'), val, got)
                continue
            if tx != ty:
                return False, '%s: %s at %s became %s at %s' % (
                    path, tx, x.get('location'), ty, y.get('location'))
            if tx == 'AstLocal':
                ka, kb = (x['location'], x['name']), (y['location'], y['name'])
                if path.endswith('.local'):
                    ia, ib = oa.get(ka), ob.get(kb)
                    if ia is None and ib is None:
                        if x['name'] != y['name']:
                            return False, '%s: outer local %s became %s' % (
                                path, x['name'], y['name'])
                    elif ia != ib:
                        return False, ('%s: use of %s (declared at %s) became a use of %s '
                                       '(declared at %s)' % (path, x['name'], x['location'],
                                                             y['name'], y['location']))
                else:
                    if oa.get(ka) != ob.get(kb):
                        return False, '%s: declarations out of step at %s / %s' % (
                            path, x['location'], y['location'])
                    stack.append((x.get('luauType'), y.get('luauType'), path + '.luauType'))
                continue
            ka = [k for k in x if k not in _LOC_KEYS]
            kb = [k for k in y if k not in _LOC_KEYS]
            if ka != kb:
                return False, '%s: fields differ at %s / %s' % (
                    path, x.get('location'), y.get('location'))
            for k in ka:
                stack.append((x[k], y[k], path + '.' + k))
        elif isinstance(x, list) and isinstance(y, list):
            if len(x) != len(y):
                return False, '%s: %d items became %d' % (path, len(x), len(y))
            for i in range(len(x)):
                stack.append((x[i], y[i], path))
        else:
            if x != y:
                if isinstance(x, float) and isinstance(y, float) and x != x and y != y:
                    continue        # NaN
                return False, '%s: %r became %r' % (path, x, y)
    return True, ''
