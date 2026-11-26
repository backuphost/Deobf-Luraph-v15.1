#!/usr/bin/env python3
"""Unit checks of the static tool's lexer, formatter, renamer and slot hints.

    python3 tests/static_unit.py

Each small program below is formatted, renamed (scopes from luau-ast) and slot-annotated; then
  * the formatted text must lex back to the input tokens (comments included),
  * the renamed text must parse to the same tree up to local names (luascope.alpha_equal),
  * the original, the formatted and the renamed program are RUN with luau and must print
    the same thing.
Needs bin/luau and bin/luau-ast (./setup.sh).  Exit status 0 when everything passes.
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'libs'))
sys.path.insert(0, ROOT)
import deobf_plus as dp        # noqa: E402
import luascope                # noqa: E402
import luaubin                 # noqa: E402
import reslot                  # noqa: E402
import humanize                # noqa: E402
import luasandbox              # noqa: E402

PROGRAMS = {
    'glue': r'''
local R2,t=false,{5}
if not R2 then print("a")end
if t[1]then print("b")end
local x=1 ..2
print(x,- -t[1],#t- -1,t[1]-1,2^-1)
''',
    'semicolons': r'''
local a=1;local b=2;
local f=print;(f)(a,b);
do local c=a+b;f(c)end;
local t={1;2;3,n=4};f(#t,t.n)
''',
    'if_expression': r'''
local a,b=3,4
local m=if a>b then a elseif a==b then 0 else b
local function g(x)if x then return if x>1 then"big"else"small"end return"nil"end
print(m,g(2),g(1),g())
''',
    'scopes': r'''
local a=5
for a=1,2 do io=nil end
print(a)
local i,w=1,2
i,w=w,i
print(i,w)
local function fact(n)if n<=1 then return 1 end return n*fact(n-1)end
print(fact(5))
local t={}
function t.f(x)return x*2 end
function t:m(x)return self.f(x)+1 end
print(t.f(2),t:m(2))
local q=0
repeat local done=q>=2;q=q+1 until done
print(q)
local x=1
local x=x+1
do local x=x*10;print(x)end
print(x)
for k,v in pairs({10,20})do local k=k*2;print(k,v)end
local function dup(p,p,p)return p end
print(dup(1,2,3))
''',
    'strings': r'''
local s=[[
  two   lines ,  kept
  -- not a comment ( stays )
]]
local u="\65\66\x43\u{44}\z
         E"
local k={[ [[key]] ]=1}
print(s,u,k.key,#"a\"b",'it\'s')
''',
    'interpolated': r'''#!/usr/bin/env luau
local y,t="Y",{k="K"}
local function f(s)return"<"..s..">"end
print(`a {f(`in {y} ner`)} b {t.k}{"}"} \{lit\} {({1,2})[2]} {`x`}`)
print(`no braces`,`{y}`,`{ ({y})[1] }`,`multi\
line`,`{f("`")}{-- c `
y}`,`{[[}`]]}`)
do local y="inner" print(`{y}{f(y)}`,y)end
''',
    'comments': r'''
local a=1--[[inline]]+2 -- to end of line
--[==[ block
   comment ]==]
local b=a- --[[c]]1
print(a,b)
''',
    'numbers': r'''
print(0x10,0b101,1_000,1e3,.5,5.,1e999,-1e999,0xFF_FF,2^53,7//2,7%3)
''',
    'slots': r'''
local T={}
local svc="RunService";T[1]=svc
local n=0;T[2]=n
T[2]=T[2]+1
local function use()return T[1],T[2]end
if T[1]then print(use())end
''',
    'calls': r'''
local function id(...)return ... end
local r=(id)(1)
local s=id"x"
local t=id{1,2}
local u=("%d"):format(3)
print(r,s,t[2],u,(id(4)),select("#",id()))
print((function()return 7 end)())
''',
    'compound': r'''
local n,s=1,"a"
n+=2 n-=1 n*=5 n/=2 n//=1 n%=4 n^=2
s..="b"
print(n,s)
''',
}

# formatter must keep these token lists apart (first, second token)
MUST_SPACE = [('-', '-'), ('[', '[[x]]'), ('1', '..'), ('a', 'b'), ('1', 'or'), ('..', '.5'),
              ('<', '='), ('~', '='), ('=', '='), ('.', '.'), ('/', '/'), (':', ':'), ('-', '>')]


def run_luau(luau, text):
    fd, path = tempfile.mkstemp(suffix='.lua')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(text)
        r = subprocess.run([luau, path], capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr.replace(path, '<f>')
    finally:
        os.unlink(path)


def main():
    luau, ast = luaubin.find_luau(), luaubin.find_luau_ast()
    if not luau or not ast:
        print('SKIP: luau / luau-ast not found: ' + luaubin.MISSING_HINT)
        return 2
    bad = 0

    def check(name, cond, detail=''):
        nonlocal bad
        if not cond:
            bad += 1
        print('%-34s %s %s' % (name, 'ok' if cond else 'FAIL', detail if not cond else ''))

    # ---- lexer / spacing
    for a, b in MUST_SPACE:
        ta, tb = dp.tokenize(a), dp.tokenize(b)
        text = dp.format_tokens(ta + tb)
        check('spacing %r %r' % (a, b), dp.tokenize(text) == ta + tb, repr(text))

    # ---- numbers are lexed the way Luau's lexer reads them (one token each)
    for lit in ('0x10', '0XfF', '0b101', '1_000', '1e5', '1E+5', '1.5e-3', '.5', '5.', '0x10i', '5i',
                '9007199254740993', '1e999', '0xFF_FF'):
        check('number %s is one token' % lit, dp.tokenize(lit) == [('n', lit)], str(dp.tokenize(lit)))
    check('0xE+1 is three tokens', [v for _t, v in dp.tokenize('0xE+1')] == ['0xE', '+', '1'])
    check('a..5 is name, concat, number', dp.tokenize('a..5') == [('w', 'a'), ('o', '..'), ('n', '5')])
    check('x.y.z keeps its dots', [v for _t, v in dp.tokenize('x.y.z')] == ['x', '.', 'y', '.', 'z'])
    check('t[1].x', [v for _t, v in dp.tokenize('t[1].x')] == ['t', '[', '1', ']', '.', 'x'])
    for code in ('local a = 5i', 'local a = 0x10i * 2i', 'print(1 ..2, 3 .. .5)'):
        text, okf = dp.format_checked(dp.tokenize(code))
        check('formatted %r compiles like the input' % code,
              okf and luasandbox.compile_only(text, luau)[0] == luasandbox.compile_only(code, luau)[0], text)
    check('norm_num leaves an integer literal alone', dp.norm_num('5i') == '5i' and dp.norm_num('0x10i') == '0x10i')
    check('fold does not touch integer literals',
          ' '.join(v for _t, v in dp.fold_consts(dp.tokenize('x = 1 + 5i'))) == 'x = 1 + 5i')

    # ---- constant folding only folds whole operands
    def fold(t):
        return ' '.join(v for _, v in dp.fold_consts(dp.tokenize(t)))
    check('fold x - 1 + 2 untouched', fold('x = a - 1 + 2') == 'x = a - 1 + 2')
    check('fold a * 2 + 3 untouched', fold('x = a * 2 + 3') == 'x = a * 2 + 3')
    check('fold (0b1100+0b1)+(0B101111-0b100001)', fold('x = (0b1100+0b1)+(0B101111-0b100001)') == 'x = 27')
    check('fold f(1+2)', fold('f(1+2)') == 'f ( 3 )')
    # Luau's arithmetic, not Python's (the full list and a fuzz are in tests/static_fold.py)
    check('fold 0x10 // 0.1 is 160', fold('x = 0x10 // 0.1') == 'x = 160')
    check('fold keeps -0', fold('x = 1 / (0 * -1)') == 'x = 1 / ( 0 * - 1 )')
    check('fold leaves inexact % alone', fold('x = 2 ^ 100 % 3') == 'x = 2 ^ 100 % 3')
    check('norm_num keeps 1e999', dp.norm_num('1e999') == '1e999')
    check('norm_num 0x10', dp.norm_num('0x10') == '16')

    # ---- programs
    for name, src in PROGRAMS.items():
        rc0, out0 = run_luau(luau, src)
        check(name + ': original runs', rc0 == 0, out0[:200])
        ptoks = dp.tokenize_pos(src)
        toks = [(t, v) for t, v, _ in ptoks]

        text, ok = dp.format_checked(toks)
        check(name + ': layout keeps tokens', ok and dp.same_tokens(dp.tokenize(text), toks, comments=True))
        rc1, out1 = run_luau(luau, text)
        check(name + ': formatted prints the same', (rc1, out1) == (rc0, out0), out1[:200])

        hum = humanize.process(src)
        check(name + ': humanize keeps tokens', dp.same_tokens(dp.tokenize(hum), toks, comments=True))

        root, errors = luascope.parse(src, ast)
        check(name + ': parses', not errors, str(errors[:1]))
        an = luascope.Analysis(root, luascope.LineIndex(src), ptoks)
        ren = luascope.Renamer(an)
        rtoks = ren.apply(ptoks)
        hints = luascope.slot_hints(an)
        for inline in (False, True):
            tag = ' (inline slots)' if inline else ''
            stoks, stats, inlined = reslot.apply_hints(rtoks, hints, inline=inline)
            rtext, rok = dp.format_checked(stoks)
            check(name + ': renamed layout' + tag, rok)
            root2, errors2 = luascope.parse(rtext, ast)
            check(name + ': renamed parses' + tag, not errors2, str(errors2[:1]))
            eq, why = luascope.alpha_equal(root, root2, inlined)
            check(name + ': renamed binds the same' + tag, eq, why)
            rc2, out2 = run_luau(luau, rtext)
            check(name + ': renamed prints the same' + tag, (rc2, out2) == (rc0, out0), out2[:200])
        if name == 'slots':
            # T[1] is assigned once from a never-reassigned local; T[2] is assigned twice
            names = {k[1]: v for k, v in reslot.apply_hints(rtoks, hints)[1]['slots'].items()}
            check('slots: T[1] named, T[2] not', set(names) == {1}, str(names))
            out, st = reslot.process_text(src, ast)
            check('slots: reslot.py text mode keeps code', dp.same_tokens(dp.tokenize(out), toks), out)
            check('slots: reslot.py adds comments', st['comment'] == 2 and '--[[svc]]' in out, str(st))

    # ---- a name that stays (it is used inside an interpolated string) is not given to an
    #      outer local: the inner one would capture the outer one's uses
    pin = 'local a = string.rep("x", 2)\ndo local rep = "inner" print(`{rep}`, a) end\n'
    root, errors = luascope.parse(pin, ast)
    ptoks = dp.tokenize_pos(pin)
    ren = luascope.Renamer(luascope.Analysis(root, luascope.LineIndex(pin), ptoks))
    ptext, pok = dp.format_checked(ren.apply(ptoks))
    root2, errors2 = luascope.parse(ptext, ast)
    check('pinned name: renamed binds the same', pok and not errors2 and luascope.alpha_equal(root, root2)[0], ptext)
    check('pinned name: outer local was renamed', ren.renamed >= 1 and 'local rep =' in ptext, ptext)
    check('pinned name: prints the same', run_luau(luau, ptext) == run_luau(luau, pin), ptext)

    # ---- a slot name is not written where it is shadowed
    sh = 'local B = {}\nlocal s = "outer"\nB[1] = s\ndo local s = "inner" print(B[1], s) end\nprint(B[1])\n'
    out, st = reslot.process_text(sh, ast, inline=True)
    check('slots: shadowed read keeps tbl[N]', 'print(B[1], s)' in out and 'print(s)' in out and st['inline'] == 1, out)
    check('slots: --inline prints the same', run_luau(luau, out) == run_luau(luau, sh), out)
    out, st = reslot.process_text(sh, ast, inline=False)
    check('slots: no comment where the name is shadowed', 'print(B[1], s)' in out and st['comment'] == 1, out)

    # ---- format_checked says when it fell back to the plain layout
    toks = dp.tokenize('local a=1;if a then print(a)end')
    lay = {}
    text, ok = dp.format_checked(toks, info=lay)
    check('format_checked: normal layout reported', ok and lay == {'fallback': False, 'reason': None}, str(lay))
    # (the repository root is first on sys.path here, so `dp` is the forwarding module of the
    # root; the implementation it forwards to is the module to patch)
    impl = sys.modules.get('_deobf_plus_impl', dp)
    real_format = impl.format_tokens
    try:
        impl.format_tokens = lambda *a, **k: 'local a = 1 if a then print ( ) end'      # drops a token
        lay = {}
        text, ok = dp.format_checked(toks, info=lay)
        check('format_checked: wrong layout -> fallback, reported', ok and lay.get('fallback') is True
              and 'token' in str(lay.get('reason')) and dp.same_tokens(dp.tokenize(text), toks, comments=True), str(lay))
        impl.format_tokens = lambda *a, **k: 1 / 0
        lay = {}
        text, ok = dp.format_checked(toks, info=lay)
        check('format_checked: crash -> fallback, reported', ok and lay.get('fallback') is True
              and 'ZeroDivisionError' in str(lay.get('reason')), str(lay))
    finally:
        impl.format_tokens = real_format

    # ---- a syntax error is reported, not swallowed
    check('parse reports syntax errors', bool(luascope.parse('local x = 1 +\n', ast)[1]))

    # ---- alpha_equal must notice a wrong binding
    a_root, _ = luascope.parse('local a=1 local b=2 print(a)', ast)
    b_root, _ = luascope.parse('local x=1 local y=2 print(y)', ast)
    c_root, _ = luascope.parse('local x=1 local y=2 print(x)', ast)
    d_root, _ = luascope.parse('local x=1 local y=2 print(z)', ast)
    check('alpha_equal: same binding', luascope.alpha_equal(a_root, c_root)[0])
    check('alpha_equal: other local rejected', not luascope.alpha_equal(a_root, b_root)[0])
    check('alpha_equal: local->global rejected', not luascope.alpha_equal(a_root, d_root)[0])
    # a function cut out of its file: its outer locals read as globals of the same name
    whole, _ = luascope.parse('local up = 1\nlocal f = function(a) return a + up end', ast)
    fn = luascope.collect_functions(whole)[0]
    alone, _ = luascope.parse('return function(arg) return arg + up end', ast)
    other, _ = luascope.parse('return function(arg) return arg + down end', ast)
    inner, _ = luascope.parse('return function(arg) return up + up end', ast)
    check('alpha_equal: outer local of a cut-out function', luascope.alpha_equal(fn, luascope.collect_functions(alone)[0])[0])
    check('alpha_equal: other global name rejected', not luascope.alpha_equal(fn, luascope.collect_functions(other)[0])[0])
    check('alpha_equal: inner local -> global rejected', not luascope.alpha_equal(fn, luascope.collect_functions(inner)[0])[0])

    # ---- closure-factory shape and globals of a function (what deob.py goes by for the
    # small functions of a VM object)
    shapes = [
        ('function(W,W)return function(W,Z)local a=Z[1];local b=Z[2];return function()return a,b end end end', 2),
        ('function(W)return function()return function()return 1 end end end', 2),
        ('function(W)return function()return 1 end end', 1),
        ('function(W)local x=W[1];return function()return x end end', 0),      # code in front of the return
        ('function(W)return function()local n=0;n=n+1;return function()return n end end end', 1),  # not only locals
        ('function(W,a)if a then return 1 end;return W end', 0),
        ('function(W)return function()end,2 end', 0),                           # two values
        ('function(W)end', 0),
    ]
    for text, want in shapes:
        root_, errs_ = luascope.parse('return ' + text, ast)
        fns = sorted(luascope.collect_functions(root_), key=lambda f: luascope.LineIndex('return ' + text).span(f['location']))
        leaf, depth = luascope.factory_leaf(fns[0])
        check('factory_leaf depth %d: %s' % (want, text[:50]), not errs_ and depth == want
              and (leaf is fns[0]) == (want == 0), str(depth))
    root_, _e = luascope.parse('local up = 1\nreturn function(a) local t = game:GetService("X"); '
                               'loadstring(a)(); newglobal = up; return t, print end', ast)
    check('globals_in: reads and writes of globals, not locals',
          luascope.globals_in(root_) == {'game': 1, 'loadstring': 1, 'newglobal': 1, 'print': 1},
          str(luascope.globals_in(root_)))

    # ---- compile-only really is compile-only
    marker = os.path.join(tempfile.gettempdir(), 'static_unit_breakout_%d' % os.getpid())
    okc, msg = luasandbox.compile_only('end pcall(writefile, "%s", "x") if false then' % marker, luau)
    check('compile_only: breakout text is rejected, not run', not okc and not os.path.exists(marker), msg)
    check('compile_only: good text', luasandbox.compile_only('local a = 1\r\nreturn a', luau) == (True, ''))
    check('compile_only: line number of the error', 'output:3:' in luasandbox.compile_only('local a\nlocal b\nlocal = 1', luau)[1])
    # byte-exact string literal for any text (carriage returns, brackets, leading newline)
    for sample in ('a\r\nb', '\nlead', ']]x]==]', 'tail]', 'x\ry\r\rz', '', ']', '\r', 'a]=]b]]'):
        prog = 'local s = ' + luasandbox.lua_string_expr(sample) + '\nprint(#s) for i = 1, #s do print(string.byte(s, i)) end\n'
        rc_, out_ = run_luau(luau, prog)
        want = '\n'.join([str(len(sample))] + [str(ord(ch)) for ch in sample]) + '\n'
        check('lua_string_expr %r' % sample, rc_ == 0 and out_ == want, out_[:80])

    print('%d failure(s)' % bad)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
