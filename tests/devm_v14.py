"""Checks for devm's Luraph v14 support.

    python3 tests/devm_v14.py            (from the repository root; needs bin/luau)

Without a corpus (hand-written text only):
* `normalize.normalise_heads`: every spelling of a dispatch-loop head becomes one of the two
  canonical forms, loops that are not dispatch loops are left alone, and a junk local is only
  hoisted out of a loop that never assigns it;
* `normalize.inline_protected_raw_info`: the protected call around a dispatch loop is dissolved for
  both tail forms (`if ok then` / `if not ok then ... else`) and the dissolved closure is reported;
* `normalize.unshadow`: a closure maker's local that redeclares a loader name is renamed;
* the model of a small hand-written VM in Luraph's v14 shape: proto-table layout with a junk third
  parameter, a junk test around the closure maker's body and `return (name)`; the loader's
  first-program call is found; every interpreter gets the entry hook;
* the harness runs that VM, sees its first program return a proto and dumps when that proto is
  entered (`[handover]`, `[dumpnew]`); a first program that never returns ends by the instruction
  budget with a `[budget]` line and no dump; key functions the init installed (closures of the VM
  that return a fixed number) are in the dump with their values;
* `normalize.foreign_dialect` names builds for Lua 5.3/5.4 and LuaJIT and passes Luau text (type
  annotations, strings, truncated text); `run.main` ends with one line `devm: unsupported target:`
  or `devm: damaged input:` for them, without a traceback;
* `luparse`: interpolated strings and a backslash-u escape without a brace;
* `Model.regs_name` for an allocator called through the loader's state table;
* `bootstrap.patch`: a tamper routine in a local, in a table slot and as a table-constructor item;
* `container.may_be_container`: only container-shaped text is probed.

With a corpus (LURAPH_CORPUS=<directory with one sub-directory per Luraph version>; skipped
otherwise).  Files are picked at run time by directory and size, never by name:
* one plain file each of the one-table-per-instruction form (v14.1), the parallel-array form (v14.3)
  and the table shape (v14.5): the model is found, the VM's init hands over, the result is not the
  init, and the report has its `result:` line.
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.setrecursionlimit(100000)

from devm import bootstrap, container, luparse, luprint, normalize, vmmodel, run       # noqa: E402

CORPUS = os.environ.get('LURAPH_CORPUS')
HAVE_LUAU = os.path.exists(os.path.join(ROOT, 'bin', 'luau'))


def canon(text):
    return normalize.normalise_heads(text)[0]


class Heads(unittest.TestCase):
    def test_parallel_array_spellings(self):
        want = 'while true do local M=b[o];if M<1 then x() end;o=o+1;end'
        for text in ('while true do local M=(b[o]);if M<1 then x() end;o=o+1;end',
                     'while(true)do local M=b[o];if M<1 then x() end;o=o+1;end',
                     'while"R\\56+"do local M=((b[o]));if M<1 then x() end;o=o+1;end',
                     'while 1 do local M=b[o] if M<1 then x() end;o=o+1;end',
                     'repeat local M=b[o];if M<1 then x() end;o=o+1;until false',
                     'repeat local M=(b[o]);if M<1 then x() end;o=o+1;until(false)'):
            self.assertEqual(canon(text), want, text)

    def test_canonical_is_untouched(self):
        for text in ('while true do local M=b[o];if M<1 then x() end;o=o+1;end',
                     'while true do local I=C[p];local O=I[3];p=p+1;if O then end end'):
            out, found, rewritten = normalize.normalise_heads(text)
            self.assertEqual((out, found, rewritten), (text, 1, 0))

    def test_instruction_table_spellings(self):
        want = 'while true do local g=P[f];local Q=g[6];f=f+1;if Q>=57 then z() end end'
        for text in ('while"x"do local g=(P[f]);local Q=(g[0X6]);f=(f+1);if Q>=57 then z() end end',
                     'repeat local g=P[f];local Q=(g[0B110]);f+=1;if Q>=57 then z() end until false',
                     'while true do local g=P[f];local Q=g[6];f=f+0x1;if Q>=57 then z() end end'):
            self.assertEqual(canon(text), want, text)

    def test_junk_constants(self):
        self.assertEqual(canon('while true do local d,G=W[_],162;if d<G then a() end end'),
                         'local G=162;while true do local d=W[_];if d<G then a() end end')
        self.assertEqual(canon('repeat local f,v,t=M[X],0X7f,(0X96);if f<v then a(t) end until false'),
                         'local v,t=127,150;while true do local f=M[X];if f<v then a(t) end end')
        self.assertEqual(canon('while true do local S=(W[f]);local w=(0X2D);if S<w then a() end end'),
                         'local w=45;while true do local S=W[f];if S<w then a() end end')
        # code after the loop must not see the hoisted local
        self.assertEqual(canon('while true do local S=W[f];local w=45;if S<w then break end end;b(w)'),
                         'do local w=45;while true do local S=W[f];if S<w then break end end end;b(w)')

    def test_not_dispatch_loops(self):
        for text in ('repeat local a=b[c];if a then end until x',                    # ends by its condition
                     'while n do local a=b[c];if a then end end',
                     'repeat local t=(O[2]);for k,v in t do end until false',        # no dispatch `if`
                     'while true do local d,G=W[_],162;if d<G then G=G+1 end end',   # the "junk" is a variable
                     'while true do local d,G=W[_],162;if d<G then x,G=1,2 end end',
                     'while true do local d,G=W[_],f();if d<G then end end'):
            out, found, rewritten = normalize.normalise_heads(text)
            self.assertEqual((out, found, rewritten), (text, 0, 0), text)

    def test_model_accepts_canonical_only(self):
        self.assertTrue(vmmodel.LOOP_HEAD_RE.search('while true do local I=C[p];\nlocal O=I[3];\np=p+1;\nif O then end end'))
        self.assertFalse(vmmodel.LOOP_HEAD_RE.search('while true do local M=(b[o]);if M then end end'))
        self.assertEqual(normalize.count_heads('repeat local M=(b[o]);if M then end until false'), 1)
        self.assertEqual(normalize.count_heads('return 1'), 0)


# A VM in the shape of Luraph v14 (hand-written; three opcodes).  %(maker)s is the closure maker.
VM = '''return(function(E,S,U)local W;%(maker)s;local P={{1,2,3},{0,0,0},{{9,8,7},{0,0,0}}};'''\
     '''P=W(P,U)(E,S,U,1,2,W);return W(P,U);end)(getfenv,select,{})(...)'''
MAKER_PLAIN = ('W=function(p,u)local c,a=p[1],p[2];local f;f=function(...)local pc=1;local r=S("#",...);'
               '%(loop)s end;return f;end')
LOOP_PCALL = ('local ok,x=pcall(function()repeat local o=(c[pc]);if o==1 then pc=pc+1;elseif o==2 then pc=pc+1;'
              'else return p[3];end;until false;end);if not(ok)then error(x,0);else return x;end;')
LOOP_BARE = ('while true do local o=c[pc];if o==1 then pc=pc+1;elseif o==2 then pc=pc+1;else return p[3];end;end;')
# decoration: junk third parameter, junk constant among the unpacked fields, junk test, `return (f)`
MAKER_DECORATED = ('W=function(p,u,j)local c,a,k=p[1],p[2],230;if k~=230 then else local f;f=function(...)local pc=1;'
                   'local r=S("#",...);%(loop)s end;return(f);end;end')


def vm_text(maker=MAKER_PLAIN, loop=LOOP_BARE):
    return VM % {'maker': maker % {'loop': loop}}


def model_of(text):
    text = normalize.normalise_heads(text)[0]
    text, _n, removed = normalize.inline_protected_raw_info(text)
    return vmmodel.Model(normalize.reflow(text), oneline=True), removed


class Inline(unittest.TestCase):
    def test_both_tail_forms(self):
        neg = ('local W=function(p,u)local a,b=p[1],p[2];local f;f=function(...)local r=T(3);local ok,x,y=P(function()'
               'repeat local o=(a[b]);if o<3 then return 1,2;else b=b+1;end;until false;end);'
               'if not(ok)then E(x,0);else if x then return y;end;end;end;return f;end;')
        pos = neg.replace('if not(ok)then E(x,0);else if x then return y;end;end;', 'if ok then if x then return y;end;else E(x,0);end;')
        outs = []
        for text in (neg, pos):
            out, n, removed = normalize.inline_protected_raw_info(canon(text))
            self.assertEqual((n, removed), (1, [2]))
            self.assertNotIn('P(function()', out)
            self.assertIn('do x,y=1,2;if x then return y;end;return;end;', out)
            outs.append(out)
        self.assertEqual(outs[0], outs[1])

    def test_unshadow(self):
        text = ('return({A=function(S,A)S[1]=function(k,N,g)local w={A[18],A,{A=1}};local A,q=(k[1]),2;'
                'local f=function(...)return A.A+w[1]+q;end;return f;end;return A.x;end})')
        out, n = normalize.unshadow(text, [(1, ['A'])])
        self.assertEqual(n, 1)
        self.assertIn('local w={A[18],A,{A=1}};local A__s,q=(k[1]),2;', out)
        self.assertIn('return A__s.A+w[1]+q;', out)
        self.assertTrue(out.endswith('return A.x;end})'))


class Model(unittest.TestCase):
    def test_proto_table_with_decoration(self):
        for maker in (MAKER_PLAIN, MAKER_DECORATED):
            for loop in (LOOP_BARE, LOOP_PCALL):
                m, removed = model_of(vm_text(maker, loop))
                self.assertFalse(m.factory_style, (maker, loop))
                self.assertEqual(m.proto_param(), 'p')
                self.assertEqual(len(m.interps), 1)
                self.assertEqual(len(removed), 1 if loop is LOOP_PCALL else 0)
                self.assertFalse(m.insn_tables)

    def test_first_program_call_and_hooks(self):
        m, _ = model_of(vm_text())
        a, b = m.first_program_call()
        self.assertEqual(m.src[a:b].replace('\n', ''), 'W(P,U)(E,S,U,1,2,W)')
        patched = m.patched_source(newproto=True, count=True)
        self.assertIn('P=__LPH_MARK( W(P,U)(E,S,U,1,2,W)', patched.replace('\n', ''))
        self.assertEqual(patched.count('==__LPH_N.w then'), 1)
        self.assertEqual(patched.count('__LPH_N.s=__LPH_N.s+1'), 1)
        # a loader without such a call (single root program): nothing to mark
        single = vm_text().replace('P=W(P,U)(E,S,U,1,2,W);', '')
        self.assertIsNone(model_of(single)[0].first_program_call())

    def test_shadowed_maker_names(self):
        text = vm_text().replace('local c,a=p[1],p[2];', 'local c,a=p[1],p[2];local U=a;')
        m, _ = model_of(text)
        self.assertEqual([names for _fid, names in m.shadowed_maker_names()], [['U']])


@unittest.skipUnless(HAVE_LUAU, 'bin/luau is not built')
class Harness(unittest.TestCase):
    def dump(self, text, **kw):
        m = vmmodel.Model(normalize.reflow(normalize.normalise_heads(text)[0]), oneline=True)
        with tempfile.TemporaryDirectory() as work:
            return vmmodel.run_dump_script(m, work, 'vm', ['executor=1'], **kw)

    def test_handover(self):
        for loop in (LOOP_BARE, LOOP_PCALL):
            data, notes = self.dump(vm_text(loop=loop))
            self.assertIsNotNone(data, notes)
            self.assertTrue(any(n.startswith('[handover]') and 'returned a proto after 3 instructions' in n for n in notes), notes)
            self.assertTrue(any(n.startswith('[dumpnew]') for n in notes), notes)
            self.assertIn('"root"', data[:200])

    def test_budget(self):
        spin = vm_text().replace('{1,2,3}', '{1,2,4}').replace('elseif o==2 then pc=pc+1;', 'elseif o==2 then pc=pc+1;elseif o==4 then pc=1;')
        data, notes = self.dump(spin, budget=5000, timeout=60)
        self.assertIsNone(data)
        self.assertTrue(any(n.startswith('[budget]') and 'has not returned' in n for n in notes), notes)


# the same VM with two more opcodes: 5 = the init installs two key functions in the shared table,
# 6 = a key function returns its operand
KEYFN_VM = ('return(function(E,S,U)local W;W=function(p,u)local c,a=p[1],p[2];local f;f=function(...)local pc=1;'
            'local r=S("#",...);while true do local o=c[pc];if o==1 then pc=pc+1;elseif o==5 then '
            'u[7]={W(p[4],u),W(p[5],u)};pc=pc+1;elseif o==6 then return a[pc];else return p[3];end;end;end;return f;end;'
            'local P={{5,1,3},{0,0,0},{{9,8,7},{0,0,0}},{{6},{41}},{{6},{97}}};'
            'P=W(P,U)(E,S,U,1,2,W);return W(P,U);end)(getfenv,select,{})(...)')


@unittest.skipUnless(HAVE_LUAU, 'bin/luau is not built')
class KeyFunctions(unittest.TestCase):
    def test_values_at_the_handover(self):
        m = vmmodel.Model(normalize.reflow(normalize.normalise_heads(KEYFN_VM)[0]), oneline=True)
        with tempfile.TemporaryDirectory() as work:
            data, notes = vmmodel.run_dump_script(m, work, 'vm', ['executor=1'])
        self.assertIsNotNone(data, notes)
        self.assertIn('[keyfn] 1=41 2=97', notes)
        self.assertIn('{"cfn":41,"k":1}', data)
        self.assertIn('{"cfn":97,"k":2}', data)


class Dialect(unittest.TestCase):
    LUAU = ('local a = b ~= c; local s = "a ~ b << | &" -- x ~ y\nlocal t = [[ ~ ]]',
            'type T = A | B & C\nlocal x: Array<Array<number>> = {}\nlocal y = 1',
            'local goto = 1; print(goto)',
            'local x = 0xFFp',
            'return(function()local s="LPH~<<|&',                 # cut off inside a string
            'local s = `{a} ~ {b}`')

    def test_luau_passes(self):
        for text in self.LUAU:
            self.assertIsNone(normalize.foreign_dialect(text), text)

    def test_other_targets(self):
        one_line = 'function f() end function g() end function h() end function i() end function j() end '
        for text, target in (('local x = ~y', 'Lua 5.3/5.4'),
                             ('local x = 1 << 4', 'Lua 5.3/5.4'),
                             (one_line + 'local x = a & b | c >> 2', 'Lua 5.3/5.4'),
                             ('goto done; ::done::', 'Lua 5.2 or later'),
                             ('local x = 0x1p4', 'Lua 5.2 or later'),
                             ('local x = 12ULL', 'LuaJIT')):
            why = normalize.foreign_dialect(text)
            self.assertIsNotNone(why, text)
            self.assertIn(target, why)

    def run_main(self, text):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, 'in.lua')
            with open(path, 'w') as f:
                f.write(text)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                with self.assertRaises(SystemExit) as cm:
                    run.main([path, '-o', os.path.join(work, 'out.lua'), '--work', os.path.join(work, 'w')])
            self.assertFalse(os.path.exists(os.path.join(work, 'out.lua')))
        return cm.exception.code, out.getvalue()

    def test_one_line_failures(self):
        code, out = self.run_main('return(function(a)local x=a~1;return x<<2 end)(...)')
        self.assertEqual(code, 1)
        self.assertRegex(out.strip().split('\n')[-1], r'^devm: unsupported target: the file is built for Lua 5\.3/5\.4')
        self.assertNotIn('Traceback', out)
        code, out = self.run_main('return(function(a)local x={1,2')
        self.assertEqual(code, 1)
        self.assertRegex(out.strip().split('\n')[-1], r'^devm: damaged input: the text does not parse as Luau')
        self.assertNotIn('Traceback', out)
        code, out = self.run_main('return(function(a)return a+1 end)(...)')
        self.assertEqual(code, 1)
        self.assertRegex(out.strip().split('\n')[-1], r'^devm: no model: ')
        self.assertNotIn('Traceback', out)


class Parser(unittest.TestCase):
    def test_interpolated_strings(self):
        src = 'local a = `x{1+2}y`; local b = `{ {1,2}[1] } and {"}`"} \\` {f("a{b")}`; print(`hi {a}`) f`z`'
        toks = [t[1] for t in luparse.tokenize(src) if t[0] == 'istr']
        self.assertEqual(toks, ['`x{1+2}y`', '`{ {1,2}[1] } and {"}`"} \\` {f("a{b")}`', '`hi {a}`', '`z`'])
        ast = luparse.parse(src)
        body = ast[0]
        self.assertEqual(body[0], ('local', ['a'], [('istr', '`x{1+2}y`')]))
        self.assertEqual(luprint.SrcPrinter().expr(body[0][2][0]), '`x{1+2}y`')
        self.assertEqual(normalize.unflow(normalize.reflow(src)), src)

    def test_u_escape(self):
        toks = [t[1] for t in luparse.tokenize('x("\\u0041", "\\u{41}", \'\\u\')') if t[0] == 'str']
        self.assertEqual(toks, ['u0041', 'A', 'u'])


class Regs(unittest.TestCase):
    def test_allocator_forms(self):
        for alloc, want in (('local R=T(r);', 'R'), ('local R=U[2][15](r);', 'R'), ('local R=(U[4](r));', 'R'),
                            ('local R=U[2][15](r,1);', 'nil'), ('local R=U[k](r);', 'nil')):
            text = vm_text().replace('local r=S("#",...);', 'local r=S("#",...);' + alloc)
            m, _ = model_of(text)
            self.assertEqual(m.regs_name(m.interps[0]), want, alloc)


class Bootstrap(unittest.TestCase):
    TEXT = ('local t = {\n\t[5] = function()\n\tlocal v = string.rep(" ", 8)\n\twhile true do\n\tend\nend,\n'
            '\t[6] = function(a)\n\treturn a\nend,\n}\n'
            'p1[22] = function()\n\tlocal v = string.rep(" ", 8)\n\t;(function()\n\t\tx()\n\tend)()\n\twhile true do\n\tend\nend\n'
            'local crash = function()\n\tlocal v = string.rep(" ", 8)\n\twhile true do\n\tend\nend\n'
            'local other = function()\n\treturn 1\nend\n'
            'local open = function()\n\tlocal v = string.rep(" ", 8)\n')

    def test_tamper_routine_forms(self):
        out, info = bootstrap.patch(self.TEXT)
        self.assertEqual(info['crash'], 3)
        self.assertIn('\t[5] = function()\n\t\t__LPH_CRASH()\nend,', out)
        self.assertIn('p1[22] = function()\n\t__LPH_CRASH()\nend', out)
        self.assertIn('local crash = function()\n\t__LPH_CRASH()\nend', out)
        self.assertIn('local other = function()\n\treturn 1\nend', out)


class Containers(unittest.TestCase):
    def test_probe_only_for_container_shapes(self):
        loop = 'while true do local M=b[o];if M<1 then x() end;o=o+1;end'
        for text, want in (('return(function()local a=loadstring;return a("x") end)()(...)', True),
                           ('return setmetatable({A=function(z)return loadstring(z.o) end},{}):A()(...)', True),
                           ('return({A=function(z)return loadstring(z.o) end}):A()(...)', False),      # a plain table VM
                           ('return(function(a,b)return loadstring(a) end)(1,2)(...)', False),         # fn-args VM
                           ('return(function()local a=1;return a end)()(...)', False),                 # loads nothing
                           ('return(function()local a=loadstring;' + loop + ' end)()(...)', False)):   # holds its loop itself
            self.assertEqual(container.may_be_container(text), want, text)

    def test_stand_in_name(self):
        with tempfile.TemporaryDirectory() as work:
            c = container.stand_in(os.path.join(work, 'chunk.lua'), None, work, 't', 'Luraph   ')
            with open(c.carrier) as f:
                self.assertIn('pcall(loadstring,s,"Luraph   ",nil)', f.read())
            c = container.stand_in(os.path.join(work, 'chunk.lua'), None, work, 't', 'x")os.exit(')
            with open(c.carrier) as f:
                self.assertIn('pcall(loadstring,s,"Luraph",nil)', f.read())


def smallest(version, pred, limit=40):
    """The smallest file of a corpus directory that satisfies pred(text); None when there is none."""
    d = os.path.join(CORPUS, version)
    if not os.path.isdir(d):
        return None
    files = sorted((os.path.getsize(os.path.join(d, f)), f) for f in os.listdir(d) if f.endswith('.lua'))
    for _size, f in files[:limit]:
        with open(os.path.join(d, f), encoding='latin-1') as fh:
            text = fh.read()
        if pred(text):
            return os.path.join(d, f)
    return None


def plain_vm(text):
    """A plain (not compressed) Luau-target VM file: it holds its dispatch loop itself.
    (count_heads is 0 for a text the tokenizer cannot read, e.g. a Lua 5.3 build.)"""
    if len(text) < 20000:
        return False
    return normalize.count_heads(text) > 0


@unittest.skipUnless(CORPUS and os.path.isdir(CORPUS) and HAVE_LUAU, 'set LURAPH_CORPUS to the sample corpus')
class Corpus(unittest.TestCase):
    INIT_MARKS = ('LPH_NO_UPVALUES', 'Luraph Script:')

    def check(self, version, insn_tables):
        path = smallest(version, plain_vm)
        if path is None:
            self.skipTest('no plain VM file in %s' % version)
        lines = []
        with tempfile.TemporaryDirectory() as work:
            text = run.deobfuscate(path, work, log=lines.append, route='a', budget=4000000, run_timeout=120)
        log = '\n'.join(lines)
        self.assertIn('proto-table', log)
        self.assertEqual('one table per instruction' in log, insn_tables, log)
        self.assertRegex(log, r'\[handover\] the VM\'s first program returned a proto after \d+ instructions')
        self.assertTrue(any(n.startswith('[dumpnew]') for n in run.LAST_RUN['notes']))
        for mark in self.INIT_MARKS:
            self.assertNotIn(mark, text)
        rep = run.run_report(run.LAST_RUN['notes']) + run.report(text)
        self.assertTrue(rep[0].startswith('the VM ran its own init'), rep)
        self.assertTrue(any(ln.startswith('result: ') for ln in rep), rep)

    def test_instruction_tables(self):
        self.check('v14.1', True)

    def test_parallel_arrays(self):
        self.check('v14.3', False)

    def test_table_shape(self):
        self.check('v14.5', False)


if __name__ == '__main__':
    unittest.main()
