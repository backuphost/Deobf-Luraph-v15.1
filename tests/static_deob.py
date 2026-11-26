#!/usr/bin/env python3
"""deob.py on inputs built here: what it extracts, what it says, what it refuses to run.

    python3 tests/static_deob.py

No sample file is needed.  The inputs have Luraph's SHAPES (nothing is copied from a sample):

  vm object   `return setmetatable({[1]=<big plain-Lua function>, [2]=<dispatch loop>,
              [3]=<small function full of API calls>, [4]=<VM method>, [7]=Color3.new,
              [8]=<closure factory that reads globals>, [9]=<closure factory on captured
              variables only>, [10]=<small function that reads a global>},{}):rk(...)`
              -> every plain function is exported (big, API-dense, closure-factory shape,
                 reads a global), script code first; the dispatcher and the VM method are
                 not, and the log accounts for every API mention that is not exported;
  container   key line + header comment + code that hands a VM-only chunk to loadstring and
              calls it with a buffer -> chunk and buffer are saved byte for byte, the tool says
              "Luraph v15.2 virtual machine, use python3 -m devm.run" and exits 4; with --force
              the output's header says that the exported functions are VM dispatchers; the
              saved chunk given back as input gets the advice with `--chunk-arg <its buffer>`;
              without -o / --work nothing is left in the temporary directory;
  plain       an ordinary script -> not executed, exit 2; with --force its `local function`
              statements are written as statements and the output compiles.  One with a big
              function full of API calls is still not "form A" (exit 2 without --force).  One
              whose longest string is under half of the file is not executed; one that is
              mostly one string is run in the sandbox (and not with --no-run).

Every output is compiled by luau and every exported part is compared with the input function
(luascope.alpha_equal).  Needs bin/luau and bin/luau-ast (./setup.sh).
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'libs'))
sys.path.insert(0, ROOT)
import deobf_plus as dp        # noqa: E402
import luascope                # noqa: E402
import luaubin                 # noqa: E402

PART_HEAD = re.compile(r'^-- 第 (\d+) 段  原始偏移 (\d+) - (\d+)  \(([\d,]+) 字节\)  (.*)$', re.M)
API = re.compile(r'GetService|Instance\s*[.:]\s*new|UDim2|Color3|TweenService')


def business_function(lines):
    out = ['function(...)local Players=game:GetService("Players");local T={};'
           'local svc=game:GetService("RunService");T[1]=svc;local n=0;T[2]=n;T[2]=T[2]+1;']
    for i in range(lines):
        out.append('local f%d=Instance.new("Frame");f%d.Size=UDim2.new(0,%d,0x10,0b101);'
                   'f%d.BackgroundColor3=Color3.fromRGB(%d,2,3);f%d.Name="\\70\\114ame%d";'
                   'if not T[1]then f%d.Parent=Players end;' % (i, i, i, i, i % 255, i, i, i))
    out.append('return T[1],T[2]end')
    return ''.join(out)


def dispatcher(cases):
    out = ['function(F,b)local r,a={};while true do local D=F[b];if D<1 then return r end;']
    for i in range(cases):
        out.append('if D<%d then a=F[b];r[%d]=a;b=b+1;end;' % (i + 2, i + 1))
    out.append('b=b+1;end end')
    return ''.join(out)


SMALL_BUSINESS = ('function(parent)local g=Instance.new("ScreenGui");g.Name="Loading";'
                  'local fr=Instance.new("Frame");fr.Size=UDim2.new(1,0,1,0);'
                  'fr.BackgroundColor3=Color3.fromRGB(10,10,10);fr.Parent=g;'
                  'local t=Instance.new("TextLabel");t.TextColor3=Color3.new(1,1,1);t.Parent=fr;'
                  'g.Parent=parent;return g end')
# a method of the VM object: works on the object's slots, reads no global, is no closure
# factory; the API name in it is a string, not a call
HELPER = 'function(z,a,b)local s=z[5](a,1,b);return s,"Color3"end'
# how Luraph stores a function it did not virtualize: factory(vm object ...) -> maker(seeds,
# captured variables) -> the function.  One that is plainly script code (it reads globals,
# without any of the business-API names) and one that only works on captured variables
FACTORY_SCRIPT = ('function(W,W,W)return function(W,W)return function()'
                  'loadstring(game:HttpGet("https://example.invalid/list"))();end;end;end')
FACTORY_PURE = ('function(W,Z)return function(Z,Z,v)local p=v[1];local q=v[2];return function()'
                'local s=1;while true do if s<=0 then return;else p[6][p[5]]=q[6][q[5]];s=0;end;end;end;end;end')
READS_GLOBAL = 'function(a,b)local s=game:GetService("StarterGui");return s,a+b end'


def vm_object(entry='rk'):
    return ('return setmetatable({[1]=%s,[2]=%s,[3]=%s,[4]=%s,[5]=string.byte,[7]=Color3.new,'
            '[8]=%s,[9]=%s,[10]=%s,k=function(z)return z end},{}):%s(...);'
            % (business_function(40), dispatcher(160), SMALL_BUSINESS, HELPER,
               FACTORY_SCRIPT, FACTORY_PURE, READS_GLOBAL, entry))


def handed_object():
    """A VM object whose plain function hands its slot table to a closure the VM creates
    (`W:kk(1, 2, {[1] = T})`): the slot names stay comments even with --inline-slots."""
    fn = ('function(W)return function()local T={};local svc=game:GetService("RunService");T[1]=svc;'
          'local f=W:kk(1,2,{[1]=T});'
          + ''.join('local g%d=Instance.new("Frame");g%d.Size=UDim2.new(0,%d,0,1);'
                    'if T[1]then g%d.Parent=T[1]end;' % (i, i, i, i) for i in range(40))
          + 'return f,T[1]end end')
    return 'return setmetatable({[1]=%s,[2]=%s,kk=function(z)return z end},{}):rk(...);' % (fn, dispatcher(160))


def vm_only_chunk():
    """A chunk with nothing but VM code (two dispatch loops big enough to count)."""
    return ('return setmetatable({[1]=%s,[2]=%s,[9]=UDim2.new,[3]=%s},{}):yQ(...);'
            % (dispatcher(170), dispatcher(180), HELPER))


def container(chunk):
    """Key line + header + code that loads `chunk` and calls it with a buffer."""
    edge = '=='
    payload = '[%s[%s]%s]' % (edge, chunk, edge)
    return ('getgenv().SCRIPT_KEY = "k-123";\n'
            '-- This file was protected using Luraph Obfuscator v15.2 [https://lura.ph/]\n'
            'return (function(...)local s=%s;local b=buffer.create(6);buffer.writestring(b,0,"DATA\\0\\255");'
            'local ok,f=pcall(loadstring,s,"Luraph",nil);return f(b)end)(...)(...);\n' % payload)


# enough of Roblox for business_function() to run under plain luau
STUBS = '''
local log = {}
local function obj(kind) return setmetatable({}, {__newindex = function(t, k, v)
  log[#log + 1] = kind .. "." .. tostring(k) .. "=" .. tostring(v); rawset(t, k, v) end,
  __tostring = function() return kind end}) end
game = {GetService = function(_, n) return obj("svc:" .. n) end}
Instance = {new = function(c) return obj(c) end}
UDim2 = {new = function(...) return "UDim2(" .. table.concat({...}, ",") .. ")" end}
Color3 = {fromRGB = function(...) return "Color3(" .. table.concat({...}, ",") .. ")" end}
'''


PLAIN = '''local Players = game:GetService("Players")
local player = Players.LocalPlayer
local speed = 50
local function createGUI()
    local gui = Instance.new("ScreenGui")
    local frame = Instance.new("Frame", gui)
    frame.Size = UDim2.new(0, 200, 0, 40)
    frame.BackgroundColor3 = Color3.fromRGB(0, 0, 0)
    local label = Instance.new("TextLabel", frame)
    label.TextColor3 = Color3.new(1, 1, 1)
    label.Text = "Speed: " .. speed
    gui.Parent = player
    return gui
end
function setSpeed(v) speed = v end
local M = {}
function M:show() return createGUI(), self end
pcall(writefile, "PLAIN_RAN.txt", "x")
createGUI()
'''


def main():
    luau, ast = luaubin.find_luau(), luaubin.find_luau_ast()
    if not luau or not ast:
        print('needs luau and luau-ast: ' + luaubin.MISSING_HINT)
        return 2
    failures = checks = 0

    def check(name, cond, detail=''):
        nonlocal failures, checks
        checks += 1
        if not cond:
            failures += 1
            print('FAIL', name, detail)

    def deob(args, cwd):
        r = subprocess.run([sys.executable, '-B', os.path.join(ROOT, 'deob.py')] + args, cwd=cwd,
                           capture_output=True, text=True, timeout=600, encoding='utf-8', errors='replace')
        return r.returncode, r.stdout + r.stderr

    def compiles(text):
        import luasandbox
        return luasandbox.compile_only(text, luau, timeout=120)[0]

    def run_function(text):
        """Call a function expression with the stubs; what it logs and returns."""
        fd, path = tempfile.mkstemp(suffix='.lua')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write(STUBS + 'local f = ' + text + '\nlocal a, b = f()\n'
                         'print(tostring(a), tostring(b), #log)\nprint(table.concat(log, ";"))\n')
            r = subprocess.run([luau, path], capture_output=True, text=True, timeout=120)
            return r.returncode, r.stdout + r.stderr.replace(path, 'f.lua')
        finally:
            os.unlink(path)

    def parts(name, src, out_text, rename, inline=False):
        """Check every part of an output against the function at its offsets in `src`."""
        root_in, _ = luascope.parse(src, ast)
        idx = luascope.LineIndex(src)
        by_span = {idx.span(f['location']): f for f in luascope.collect_functions(root_in)}
        root_out, errs = luascope.parse(out_text, ast)
        check(name + ': output parses', not errs, str(errs[:1]))
        idx_out = luascope.LineIndex(out_text)
        heads = list(PART_HEAD.finditer(out_text))
        fns_out = sorted((idx_out.span(f['location']) + (f,) for f in luascope.collect_functions(root_out)),
                         key=lambda x: (x[0], -x[1]))
        found = []
        for k, m in enumerate(heads):
            s, e = int(m.group(2)), int(m.group(3))
            fin = by_span.get((s, e))
            # first function that starts after this part's heading
            fout = next((f for a, _b, f in fns_out if a >= m.end()), None)
            check('%s: part %s is a function of the input' % (name, m.group(1)), fin is not None)
            if fin is None or fout is None:
                continue
            if inline:
                found.append((s, e, m.group(5)))
                continue            # slot reads were replaced: compared by running, below
            eq, why = luascope.alpha_equal(fin, fout)
            check('%s: part %s is the input function up to local names' % (name, m.group(1)), eq, why)
            if not rename:
                seg_end = heads[k + 1].start() if k + 1 < len(heads) else out_text.rindex('\nreturn {')
                a = [dp.canon(t) for t in dp.tokenize(src[s:e]) if t[0] != 'c']
                b_all = [dp.canon(t) for t in dp.tokenize(out_text[m.end():seg_end]) if t[0] != 'c']
                # the part is the input tokens, possibly after `local __partN =`
                check('%s: part %s keeps every token' % (name, m.group(1)),
                      b_all[-len(a):] == a or b_all[:len(a)] == a, '')
            found.append((s, e, m.group(5)))
        return found

    with tempfile.TemporaryDirectory(prefix='static_deob_') as tmp:
        # ------------------------------------------------------------- VM object (form A)
        src = vm_object()
        inp = os.path.join(tmp, 'obj.lua')
        with open(inp, 'w', encoding='utf-8') as fh:
            fh.write(src)
        spans = {}
        for key, text in (('big', business_function(40)), ('disp', dispatcher(160)),
                          ('small', SMALL_BUSINESS), ('helper', HELPER), ('fscript', FACTORY_SCRIPT),
                          ('fpure', FACTORY_PURE), ('reads', READS_GLOBAL)):
            a = src.index(text)
            spans[key] = (a, a + len(text))
        # script code first (it names the environment), the bigger the earlier; then the
        # closure that only works on captured variables
        script_order = sorted(('big', 'small', 'fscript', 'reads'),
                              key=lambda k: -(spans[k][1] - spans[k][0]))
        want_parts = [spans[k] for k in script_order] + [spans['fpure']]
        want_labels = {spans['big']: '业务 API', spans['small']: '业务 API', spans['reads']: '业务 API',
                       spans['fscript']: '读全局 ', spans['fpure']: '闭包工厂形态，不读全局'}
        for flags in ([], ['--no-rename'], ['--inline-slots'], ['--no-humanize'], ['--no-slots']):
            tag = 'object ' + (' '.join(flags) or '(default)')
            out = os.path.join(tmp, 'obj.out.lua')
            rc, log = deob([inp, '-o', out, '--min-size', '3000'] + flags, tmp)
            check(tag + ': exit 0', rc == 0, log[-400:])
            if rc != 0:
                continue
            text = open(out, encoding='utf-8').read()
            check(tag + ': output compiles', compiles(text))
            found = parts(tag, src, text, rename='--no-rename' not in flags,
                          inline='--inline-slots' in flags)
            got = [(s, e) for s, e, _k in found]
            # the exported business function behaves like the input one
            seg = text[text.index('local __part1 = ') + len('local __part1 = '):text.index('-- 第 2 段')]
            seg = seg[:seg.rindex('-- ====')]
            want = run_function(business_function(40))
            have = run_function(seg)
            check(tag + ': exported business function runs like the input', want[0] == 0 and have == want,
                  'input %r\n output %r' % (want[1][:200], have[1][:200]))
            check(tag + ': every plain function exported, script code first; dispatcher and VM method not',
                  got == want_parts, str(got) + ' ' + str(spans))
            check(tag + ': each part is labelled by what it is',
                  all(k.startswith(want_labels.get((a, b), '?')) for a, b, k in found), str(found))
            check(tag + ': the factory that reads globals names them',
                  any('game' in k and 'loadstring' in k for a, b, k in found if (a, b) == spans['fscript']),
                  str(found))
            head = text.split('\n', 6)[:6]
            check(tag + ': header counts',
                  any('共导出 5 个函数：业务函数 3 个' in ln and '脚本函数 1 个' in ln
                      and '不读全局的函数 1 个' in ln and '另有 VM 分派器 1 个未导出' in ln for ln in head),
                  str(head))
            check(tag + ': header says the virtualized part is not in the output',
                  any('被虚拟化的部分' in ln and 'devm.run' in ln for ln in head), str(head))
            check(tag + ': HttpGet call of the small factory is in the output',
                  text.count('example.invalid/list') == 1, '')
            # every API mention is either in an exported part or accounted for in the log
            total = len(API.findall(src))
            inside = sum(len(API.findall(src[s:e])) for s, e in got)
            m = re.search(r'不导出、但出现业务 API 名的地方：(.*)', log)
            said = sum(int(x) for x in re.findall(r'共 (\d+) 处|函数之外 (\d+) 处', m.group(1))
                       for x in x if x) if m else 0
            check(tag + ': log accounts for the API mentions that are not exported',
                  said == total - inside, 'said %d, %d of %d outside the parts' % (said, total - inside, total))
            if '--inline-slots' in flags:
                n = re.search(r'直接替换 (\d+) 处', log)
                check(tag + ': slots were substituted and counted', bool(n) and int(n.group(1)) >= 40, log[-300:])
                check(tag + ': header gives the number of substitutions',
                      bool(n) and any(('%s 处 tbl[N] 已替换' % n.group(1)) in ln for ln in text.split('\n')[:8]),
                      str(text.split('\n')[:8]))
            elif '--no-rename' not in flags and '--no-slots' not in flags:
                check(tag + ': slot names are comments', '--[[' in seg and '直接替换' not in log, log[-300:])
        rc, log = deob([inp, '--list', '--min-size', '3000'], tmp)
        check('object --list: exit 0 and lists the small business function',
              rc == 0 and '一并导出' in log and '剔除 VM 分派器 1 个' in log, log[-400:])
        import deob as deob_mod
        info = deob_mod.probe(inp, work_dir=os.path.join(tmp, 'probe_obj'), min_size=3000)

        def triple(key):
            return spans[key] + (spans[key][1] - spans[key][0],)
        check('object probe: form A, static, nothing executed',
              info['form'] == 'A' and info['engine'] == 'static' and info['sandbox_run'] is False
              and info['big_functions'] == [triple('big')]
              and sorted(info['small_functions']) == sorted(triple(k) for k in ('small', 'fscript', 'fpure', 'reads'))
              and sorted(info['script_functions']) == sorted(triple(k) for k in ('big', 'small', 'fscript', 'reads'))
              and info['dispatchers'] == [triple('disp')]
              and info['luraph'] == ['setmetatable VM 对象'] and info['bare_chunk'] is True, str(info))

        # a formatter that returns a slightly different program and claims success must be
        # caught by the tree comparison, with and without renaming
        import bisect
        import deobf_plus as shim
        real_fc = shim.format_checked

        def lying(toks, **kw):
            text, _ok = real_fc(toks, **kw)
            return text.replace('UDim2.new(0', 'UDim2.new(1', 1), True
        sc = deob_mod.Scan(inp, min_size=3000, say=lambda *a, **k: None)
        try:
            sc.load(); sc.binaries(); sc.shells(); sc.functions(); sc.classify()
            all_ptoks = dp.tokenize_pos(sc.src)
            offsets = [o for _t, _v, o in all_ptoks]
            for rename in (True, False):
                shim.format_checked = lying
                try:
                    _text, binfo = deob_mod.build_part(sc, sc.sized[0], all_ptoks, offsets, rename=rename,
                                                       slots=rename, inline_slots=False, wrap=True, ast_bin=ast)
                finally:
                    shim.format_checked = real_fc
                check('a changed literal is caught by the tree comparison (rename=%s)' % rename,
                      binfo['tree'] is False and not binfo['binding'] and '语法树不一致' in str(binfo['note']),
                      str({k: binfo[k] for k in ('tree', 'binding', 'note', 'layout', 'literals')}))
                _text, binfo = deob_mod.build_part(sc, sc.sized[0], all_ptoks, offsets, rename=rename,
                                                   slots=rename, inline_slots=False, wrap=True, ast_bin=ast)
                check('the real formatter passes it (rename=%s)' % rename, binfo['tree'] is True, str(binfo['note']))
        finally:
            sc.cleanup()

        # the same object with the default threshold: its business function (7 KB) is below
        # 20,000 bytes, so the tool calls it a VM (exit 4) -- but it must SAY that plain
        # business functions exist, and --force must export them, not VM code
        rc, log = deob([inp, '-o', os.path.join(tmp, 'obj.def.lua')], tmp)
        check('object, default threshold: exit 4 and the plain functions are counted',
              rc == 4 and '有 5 个没被虚拟化的小函数' in log and '闭包工厂形态 2 个' in log
              and '脚本代码）4 个' in log and '--force' in log, log[-900:])
        check('object, default threshold: a bare chunk is told to take its buffer along',
              '--chunk-arg <数据 buffer 文件>' in log and '带容器的原始文件' in log, log[-900:])
        check('object, default threshold: no output without --force', not os.path.exists(os.path.join(tmp, 'obj.def.lua')))
        rc, log = deob([inp, '-o', os.path.join(tmp, 'obj.def.lua'), '--force'], tmp)
        check('object, default threshold, --force: exit 0', rc == 0, log[-500:])
        if rc == 0:
            text = open(os.path.join(tmp, 'obj.def.lua'), encoding='utf-8').read()
            found = parts('object --force', src, text, rename=True)
            check('object --force: the five plain functions, nothing else',
                  [(a, b) for a, b, _k in found] == want_parts, str(found))
            head = text.split('\n', 6)[:6]
            check('object --force: header says the script body is not in the output',
                  any('这里只有没被虚拟化的小函数' in ln for ln in head)
                  and any('业务函数 3 个' in ln for ln in head), str(head))
            check('object --force: output compiles', compiles(text))
        info = deob_mod.probe(inp, work_dir=os.path.join(tmp, 'probe_obj_def'))
        check('object probe, default threshold: vm, with the small functions listed',
              info['form'] == 'vm' and info['engine'] == 'devm' and info['big_functions'] == []
              and len(info['small_functions']) == 5 and len(info['script_functions']) == 4, str(info))

        # slot table handed to a closure the VM creates: --inline-slots substitutes nothing,
        # and the header must not claim that it did
        hin = os.path.join(tmp, 'handed.lua')
        with open(hin, 'w', encoding='utf-8') as fh:
            fh.write(handed_object())
        rc, log = deob([hin, '-o', os.path.join(tmp, 'handed.out.lua'), '--min-size', '3000', '--inline-slots'], tmp)
        check('handed slot table --inline-slots: exit 0', rc == 0, log[-500:])
        if rc == 0:
            text = open(os.path.join(tmp, 'handed.out.lua'), encoding='utf-8').read()
            head = text.split('\n', 8)[:8]
            n = re.search(r'注释标注 (\d+) 处，直接替换 0 处', log)
            check('handed slot table: comments only, and the log says why',
                  bool(n) and int(n.group(1)) >= 40 and '全部保留为注释' in log, log[-400:])
            check('handed slot table: header explains the comments and claims no substitution',
                  any('tbl[N --[[name]]]' in ln for ln in head) and not any('已替换' in ln for ln in head)
                  and any('没有一处槽位读取满足替换条件' in ln for ln in head), str(head))
            check('handed slot table: the reads carry the name as a comment', text.count('tbl[1 --[[') >= 40,
                  str(text.count('tbl[1 --[[')))
            check('handed slot table: output compiles', compiles(text))

        # a header comment in front of the object does not hide it
        with open(os.path.join(tmp, 'objh.lua'), 'w', encoding='utf-8') as fh:
            fh.write('-- This file was protected using Luraph Obfuscator v15.1 [https://lura.ph/]\n' + src)
        info = deob_mod.probe(os.path.join(tmp, 'objh.lua'), work_dir=os.path.join(tmp, 'probe_objh'),
                              min_size=3000)
        check('object with header: version read, still form A',
              info['luraph_version'] == '15.1' and info['form'] == 'A' and info['vm']['object'], str(info))

        # ------------------------------------------------------------- container (VM only)
        chunk = vm_only_chunk()
        cin = os.path.join(tmp, 'packed.luau')
        with open(cin, 'w', encoding='utf-8') as fh:
            fh.write(container(chunk))
        work = os.path.join(tmp, 'cwork')
        rc, log = deob([cin, '-o', os.path.join(work, 'o.lua')], tmp)
        check('container: exit 4 (virtualized)', rc == 4, log[-500:])
        check('container: says v15.2 VM and names the devirtualizer',
              'Luraph v15.2 虚拟机' in log and 'python3 -m devm.run' in log and '头注释前有 1 行附加代码' in log, log[-800:])
        cp, bp = os.path.join(work, 'packed.chunk.lua'), os.path.join(work, 'packed.buffer.bin')
        check('container: chunk saved byte for byte',
              os.path.isfile(cp) and open(cp, 'rb').read() == chunk.encode(), log[-300:])
        check('container: buffer saved byte for byte',
              os.path.isfile(bp) and open(bp, 'rb').read() == b'DATA\x00\xff', log[-300:])
        check('container: no output file without --force', not os.path.exists(os.path.join(work, 'o.lua')))
        info = deob_mod.probe(cin, work_dir=os.path.join(tmp, 'probe_c'), min_size=3000)
        check('container probe', info['form'] == 'vm' and info['engine'] == 'devm' and info['container']
              and info['luraph_version'] == '15.2' and info['chunk_name'] == 'Luraph'
              and info['vm']['entry'] == 'yQ' and info['vm']['dispatch_loops'] == 2
              and info['prelude'].startswith('getgenv()') and info['sandbox_run'] is True
              and info['bare_chunk'] is False and '头注释' in info['luraph'] and '容器' in info['luraph'],
              str(info))
        check('container: the advice is the plain devm command (devm unwraps the container itself)',
              ('python3 -m devm.run %s -o' % cin) in log and '--chunk-arg' not in log, log[-600:])
        # the saved chunk as input: it needs its buffer, and the tool finds the file it saved
        rc, log = deob([cp], tmp)
        check('saved chunk as input: exit 4 and the advice carries --chunk-arg with the saved buffer',
              rc == 4 and ('--chunk-arg %s' % bp) in log and '带容器的原始文件' in log, log[-700:])
        info = deob_mod.probe(cp, work_dir=os.path.join(tmp, 'probe_cp'))
        check('saved chunk probe: bare chunk with its sibling buffer, and it was not run',
              info['bare_chunk'] is True and info['sibling_buffer'] == bp and info['container'] is False
              and info['sandbox_run'] is False and info['error'] is None, str(info))
        check('saved chunk as input: the log says it is the VM itself and was not executed',
              'VM 对象本身' in log and '没有执行它' in log and '剥壳失败' not in log, log[:900])
        # no -o, no --work: what was unwrapped is not kept, and nothing stays in the temp dir
        tdir = os.path.join(tmp, 'tmpdir')
        os.mkdir(tdir)
        env = dict(os.environ, TMPDIR=tdir)
        r = subprocess.run([sys.executable, '-B', os.path.join(ROOT, 'deob.py'), cin], cwd=tmp, env=env,
                           capture_output=True, text=True, timeout=600, encoding='utf-8', errors='replace')
        check('container without -o: exit 4, says nothing was kept, temp dir is empty afterwards',
              r.returncode == 4 and '没有保存' in r.stdout and os.listdir(tdir) == [],
              r.stdout[-400:] + str(os.listdir(tdir)))
        # --no-run: the container is recognised and not executed
        rc, log = deob([cin, '-o', os.path.join(tmp, 'norun.lua'), '--no-run'], tmp)
        check('container --no-run: not executed, exit 2, says so',
              rc == 2 and '--no-run：没有执行它' in log and '剥壳完成' not in log, log[-500:])
        info = deob_mod.probe(cin, work_dir=os.path.join(tmp, 'probe_norun'), allow_run=False)
        check('container probe, allow_run=False: nothing run, no chunk, error says why',
              info['sandbox_run'] is False and info['container'] is False and info['chunk_path'] is None
              and '--no-run' in str(info['error']), str(info))

        # a container whose chunk holds a plain script function next to the VM: --force
        # exports that function, not the interpreter
        chunk2 = chunk.replace('[9]=UDim2.new,', '[9]=UDim2.new,[11]=%s,' % FACTORY_SCRIPT)
        cin2 = os.path.join(tmp, 'packed2.luau')
        with open(cin2, 'w', encoding='utf-8') as fh:
            fh.write(container(chunk2))
        rc, log = deob([cin2, '-o', os.path.join(tmp, 'cw2', 'o.lua'), '--min-size', '3000'], tmp)
        check('container with a plain function: exit 4 and the function is announced',
              rc == 4 and '有 1 个没被虚拟化的小函数' in log, log[-700:])
        rc, log = deob([cin2, '-o', os.path.join(tmp, 'cw2', 'f.lua'), '--force', '--min-size', '3000'], tmp)
        check('container with a plain function --force: exit 0', rc == 0, log[-500:])
        if rc == 0:
            text = open(os.path.join(tmp, 'cw2', 'f.lua'), encoding='utf-8').read()
            found = parts('container2 --force', chunk2, text, rename=True)
            a2 = chunk2.index(FACTORY_SCRIPT)
            check('container with a plain function --force: that function is the output',
                  [(a, b) for a, b, _k in found] == [(a2, a2 + len(FACTORY_SCRIPT))]
                  and found[0][2].startswith('读全局 '), str(found))
            head = text.split('\n', 6)[:6]
            check('container with a plain function --force: header says the script body is in the VM',
                  any('这里只有没被虚拟化的小函数' in ln for ln in head)
                  and any('另有 VM 分派器 2 个未导出' in ln for ln in head), str(head))
        rc, log = deob([cin, '-o', os.path.join(work, 'f.lua'), '--force', '--min-size', '3000'], tmp)
        check('container --force: exit 0', rc == 0, log[-500:])
        if rc == 0:
            text = open(os.path.join(work, 'f.lua'), encoding='utf-8').read()
            head = text.split('\n', 6)[:6]
            check('container --force: header says the parts are VM dispatchers, not business code',
                  any('VM 分派器 2 个' in ln and '业务函数' not in ln for ln in head)
                  and any('没有业务源码' in ln for ln in head), str(head))
            found = parts('container --force', chunk, text, rename=True)
            check('container --force: parts labelled as dispatchers',
                  len(found) == 2 and all(k.startswith('VM 分派器') for _s, _e, k in found), str(found))
            check('container --force: output compiles', compiles(text))

        # ------------------------------------------------------------- plain script
        pin = os.path.join(tmp, 'plain.lua')
        with open(pin, 'w', encoding='utf-8') as fh:
            fh.write(PLAIN)
        rc, log = deob([pin, '-o', os.path.join(tmp, 'plain.out.lua')], tmp)
        check('plain: exit 2, not executed', rc == 2 and '没有执行它' in log
              and not os.path.exists(os.path.join(tmp, 'PLAIN_RAN.txt')), log[-400:])
        info = deob_mod.probe(pin, work_dir=os.path.join(tmp, 'probe_p'))
        check('plain probe: unknown', info['form'] == 'unknown' and info['engine'] is None
              and info['sandbox_run'] is False and info['luraph_version'] is None, str(info))
        for flags in ([], ['--no-rename']):
            tag = 'plain --force ' + ' '.join(flags)
            rc, log = deob([pin, '-o', os.path.join(tmp, 'plain.f.lua'), '--force'] + flags, tmp)
            check(tag + ': exit 0', rc == 0, log[-500:])
            if rc != 0:
                continue
            text = open(os.path.join(tmp, 'plain.f.lua'), encoding='utf-8').read()
            check(tag + ': output compiles (function statements stay statements)', compiles(text))
            check(tag + ': header does not call it Luraph output', '没有被识别为 Luraph' in text.split('\n')[0],
                  text.split('\n')[0])
            found = parts(tag, PLAIN, text, rename=not flags)
            check(tag + ': three functions', len(found) == 3, str(found))
            if not flags:
                check(tag + ': every part renamed and compared', log.count('绑定一致 ✓') == 3, log[-600:])
            check(tag + ': header says that only the functions are in the output',
                  any('函数之外的代码' in ln for ln in text.split('\n')[:6]), str(text.split('\n')[:6]))

        # an ordinary script with one big function full of API calls: not Luraph, so not
        # "form A"; nothing is extracted unless --force says so
        bigfn = ('local function build()\n' + ''.join(
            '  do local q = Instance.new("Frame"); q.Size = UDim2.new(0, %d, 0, 1); '
            'q.BackgroundColor3 = Color3.new(); q.Name = "n%d" end\n' % (i, i) for i in range(220))
            + 'end\npcall(writefile, "BIGFN_RAN.txt", "x")\nbuild()\nprint("done")\n')
        bin_ = os.path.join(tmp, 'bigfn.lua')
        with open(bin_, 'w', encoding='utf-8') as fh:
            fh.write(bigfn)
        rc, log = deob([bin_, '-o', os.path.join(tmp, 'bigfn.out.lua')], tmp)
        check('plain script with a big function: exit 2, says it is not Luraph, writes nothing',
              rc == 2 and '不是 Luraph 的产物' in log and '--force' in log
              and not os.path.exists(os.path.join(tmp, 'bigfn.out.lua'))
              and not os.path.exists(os.path.join(tmp, 'BIGFN_RAN.txt')), log[-500:])
        info = deob_mod.probe(bin_, work_dir=os.path.join(tmp, 'probe_bigfn'))
        check('plain script with a big function, probe: unknown, no Luraph evidence, not run',
              info['form'] == 'unknown' and info['engine'] is None and info['luraph'] == []
              and info['sandbox_run'] is False and len(info['big_functions']) == 1, str(info))
        rc, log = deob([bin_, '-o', os.path.join(tmp, 'bigfn.out.lua'), '--force'], tmp)
        check('plain script with a big function --force: exit 0', rc == 0, log[-500:])
        if rc == 0:
            text = open(os.path.join(tmp, 'bigfn.out.lua'), encoding='utf-8').read()
            head = text.split('\n', 6)[:6]
            check('plain script with a big function --force: header is honest',
                  '没有被识别为 Luraph' in head[0] and any('函数之外的代码' in ln for ln in head), str(head))
            check('plain script with a big function --force: output compiles', compiles(text))

        # the payload rule: one string literal of at least 4,096 characters that is MORE THAN
        # HALF of the file.  Under half: not run.  Over half: run in the sandbox (the rule
        # does not know Luraph), where writefile does not exist; never with --no-run.
        def with_string(n_string, n_code):
            return ('local data = "%s"\npcall(writefile, "STR_RAN.txt", "x")\n' % ('Qk1G' * (n_string // 4))
                    + ''.join('local v%d = #data + %d\n' % (i, i) for i in range(n_code // 22)))
        for name, text_in, runs in (('string under half of the file', with_string(6000, 8000), False),
                                    ('string over half of the file', with_string(9000, 3000), True)):
            sp = os.path.join(tmp, 'str_%d.lua' % runs)
            with open(sp, 'w', encoding='utf-8') as fh:
                fh.write(text_in)
            share = 100.0 * (len(text_in.split('"')[1]) + 2) / len(text_in)
            check('%s: the test input is what it says (%.0f%%)' % (name, share), (share > 50) == runs, '')
            rc, log = deob([sp, '-o', os.path.join(tmp, 'str.out.lua')], tmp)
            info = deob_mod.probe(sp, work_dir=os.path.join(tmp, 'probe_str_%d' % runs))
            check('plain script, %s: %s' % (name, 'run in the sandbox' if runs else 'not executed'),
                  rc == 2 and info['sandbox_run'] is runs and ('没有执行它' in log) == (not runs)
                  and ('占了文件一半以上' in log) == runs
                  and not os.path.exists(os.path.join(tmp, 'STR_RAN.txt')), log[-500:] + str(info))
            if runs:
                info = deob_mod.probe(sp, work_dir=os.path.join(tmp, 'probe_str_norun'), allow_run=False)
                rc, log = deob([sp, '-o', os.path.join(tmp, 'str.out.lua'), '--no-run'], tmp)
                check('plain script, %s, --no-run: not executed' % name,
                      info['sandbox_run'] is False and rc == 2 and '--no-run：没有执行它' in log, log[-400:])

    print('%d checks, %d failure(s)' % (checks, failures))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
