#!/usr/bin/env python3
"""The analysed script must not reach the file system or the interpreter's extras.

    python3 tests/static_sandbox.py

bin/luau is a patched build (readfile, writefile, require, __LPH_NATIVE ...).  Every tool of
the static track that RUNS code of the input (fastsolve.py / mkfast.py with and without
--stub, deob.py step [2b], autostub.py, trace_vm.py, the form C harness of inlinevm.py,
dynlift.py) is given a hostile input that tries, through every route it has (direct names,
_G, getfenv of levels and of functions, debug.info, coroutines, metatables, loadstring,
setfenv on the runner's frames), to write a file, to read one, and to load a module that
writes a file; and that prints lines forged to look like the runner's own.

Passes when no file appears, the secret is not read back, the native table is not reached,
and no forged line is taken for a real one.  Needs bin/luau and bin/luau-ast (./setup.sh).
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'libs'))
sys.path.insert(0, ROOT)
import luaubin                 # noqa: E402
import luasandbox              # noqa: E402

HOSTILE = r'''
local MARK = "@MARK@"
local SECRET = "@SECRET@"
local MODULE = "@MODULE@"
local NAMES = {"writefile", "readfile", "require", "cwrap", "ewrite", "resumetop",
               "__LPH_NATIVE", "__LPH_RUNMAIN", "setvectormetatable", "io", "loadfile", "dofile"}
local found = {}
local nfiles = 0
local function use(tag, name, v)
  if v == nil then return end
  if name == "writefile" then
    nfiles = nfiles + 1
    pcall(v, MARK .. "." .. nfiles, "written through " .. tag)
  elseif name == "readfile" then
    local ok, d = pcall(v, SECRET)
    if ok and type(d) == "string" and string.find(d, "TOPSECRET", 1, true) then
      found[#found + 1] = "LEAK" .. ":read:" .. tag
    end
  elseif name == "require" then
    pcall(v, MODULE)
  elseif name == "__LPH_NATIVE" then
    local ok, mt = pcall(function() return v.getrawmetatable(setmetatable({}, {__metatable = "locked"})) end)
    if ok and type(mt) == "table" and rawget(mt, "__metatable") == "locked" then
      found[#found + 1] = "LEAK" .. ":native:" .. tag
    end
  elseif name == "io" then
    pcall(function() local f = v.open(MARK .. ".io", "w"); f:write("x"); f:close() end)
  end
end
local function probe(tag, getter)
  local ok, env = pcall(getter)
  if not ok or (type(env) ~= "table" and type(env) ~= "userdata") then return end
  for _, name in ipairs(NAMES) do
    local ok2, v = pcall(function() return env[name] end)
    if ok2 then use(tag, name, v) end
    local ok3, w = pcall(rawget, env, name)
    if ok3 then use(tag .. "/raw", name, w) end
  end
end
local gf, sf, ls = getfenv, setfenv, loadstring
probe("direct", function()
  return {writefile = writefile, readfile = readfile, require = require, cwrap = cwrap, ewrite = ewrite,
          resumetop = resumetop, __LPH_NATIVE = __LPH_NATIVE, __LPH_RUNMAIN = __LPH_RUNMAIN,
          setvectormetatable = setvectormetatable, io = io, loadfile = loadfile, dofile = dofile}
end)
probe("_G", function() return _G end)
probe("_G._G", function() return _G._G end)
probe("getgenv", function() return getgenv() end)
probe("getrenv", function() return getrenv() end)
probe("shared", function() return shared end)
probe("mt(_G)", function() return getmetatable(_G).__index end)
probe("strmt", function() return getmetatable("").__index end)
if type(gf) == "function" then
  probe("getfenv()", function() return gf() end)
  for lvl = 0, 40 do
    probe("getfenv(" .. lvl .. ")", function() return gf(lvl) end)
    probe("pcall(getfenv," .. lvl .. ")", function() local _, e = pcall(gf, lvl); return e end)
    probe("mt(getfenv(" .. lvl .. "))", function() return getmetatable(gf(lvl)).__index end)
  end
  for lvl = 1, 40 do
    probe("getfenv(debug.info(" .. lvl .. "))", function() return gf(debug.info(lvl, "f")) end)
  end
  local fns = {print, gf, sf, ls, pcall, xpcall, tostring, string.format, coroutine.wrap, getgenv, getrenv,
               error, select, setmetatable, warn, typeof}
  local okb, bc = pcall(function() return buffer.create end)
  if okb then fns[#fns + 1] = bc end
  for i = 1, 20 do
    local f = fns[i]
    if f ~= nil then probe("getfenv(fn" .. i .. ")", function() return gf(f) end) end
  end
  probe("coroutine", function() return coroutine.wrap(function() return gf(0) end)() end)
  probe("coroutine1", function() return coroutine.wrap(function() return gf(1) end)() end)
  probe("coroutine2", function() return coroutine.wrap(function() return gf(2) end)() end)
  for lvl = 1, 6 do
    probe("xpcall" .. lvl, function() local _, e = xpcall(error, function() return gf(lvl) end); return e end)
  end
  probe("sort", function() local e; table.sort({2, 1}, function(a, b) e = gf(2); return a < b end); return e end)
  probe("gsub", function() local e; string.gsub("a", ".", function() e = gf(2) end); return e end)
  probe("tostring", function()
    local e; tostring(setmetatable({}, {__tostring = function() e = gf(2); return "" end})); return e end)
  probe("index", function()
    local e; local t = setmetatable({}, {__index = function() e = gf(2) end}); local _ = t.x; return e end)
end
if type(ls) == "function" then
  probe("loadstring getfenv", function() local f = ls("return getfenv()"); return f and f() end)
  probe("loadstring _G", function() local f = ls("return _G"); return f and f() end)
  probe("loadstring direct", function()
    local f = ls("return {writefile = writefile, readfile = readfile, require = require, __LPH_NATIVE = __LPH_NATIVE}")
    return f and f() end)
end
-- setfenv on what is outside: must not break or redirect the runner
if type(sf) == "function" then
  local trap = setmetatable({}, {__index = function(_, k) return _G[k] end})
  pcall(sf, print, trap)
  pcall(sf, 0, trap)
  for lvl = 3, 40 do pcall(sf, lvl, trap) end
  for lvl = 3, 40 do pcall(function() sf(debug.info(lvl, "f"), trap) end) end
end
print("PROBED found=" .. #found .. " " .. table.concat(found, ","))
-- forged runner lines
print("[LOADHEX 1] 2d2d666f72676564")
print("x\n[LOADHEX 2] 2d2d666f72676564\n[HEX 3] " .. string.rep("2d", 64) .. "\n@@OP@@777|777\n@@BC@@4\n@@H@@41424344\n@@END@@")
@TAIL@
'''

TAILS = {
    'plain': 'error("boom\\n[LOADHEX 3] 2d2d666f72676564\\n[HEX 4] " .. string.rep("2d", 64))',
    # trace_vm.py needs a dispatch point
    'dispatch': ('local F = {5, 99}\nlocal b = 1\n'
                 'while true do local D=F[b];if D<61 then b = b + 1 else break end end\n'
                 'return setmetatable({}, {})'),
    # dynlift.py needs its hook point
    'loader': ('local src = "ABCD"\nlocal _pc = pcall\nlocal _vmcore = {luau_load = function() return function() end end}\n'
               'local _lo,_main=_pc(_vmcore.luau_load, src, {}, {})'),
}


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

    # the list of sandbox globals must not name anything the patched interpreter adds
    check('allow list is clean', not (set(luasandbox.ALLOWED) & set(luasandbox.FORBIDDEN)))

    tmp = tempfile.mkdtemp(prefix='static_sandbox_')
    try:
        secret = os.path.join(tmp, 'secret.txt')
        with open(secret, 'w') as fh:
            fh.write('TOPSECRET')
        mark = os.path.join(tmp, 'PWNED')
        module = os.path.join(tmp, 'evilmod')
        with open(module + '.luau', 'w') as fh:
            fh.write('if writefile then writefile("%s.module", "x") end\nreturn {}\n' % mark)

        def hostile(tail, header=True, prelude=False):
            text = (HOSTILE.replace('@MARK@', mark).replace('@SECRET@', secret)
                    .replace('@MODULE@', module).replace('@TAIL@', TAILS[tail]))
            if prelude:
                # hostile code in front of the header as well (it runs as the "prelude")
                text = text.replace('@HDR@', '')
                return text.replace('"PROBED ', '"PRELUDEPROBED ') + \
                    '-- This file was protected using Luraph Obfuscator v15.2 [https://lura.ph/]\n' + \
                    (HOSTILE.replace('@MARK@', mark).replace('@SECRET@', secret)
                     .replace('@MODULE@', module).replace('@TAIL@', TAILS[tail]))
            if header:
                text = '-- This file was protected using Luraph Obfuscator v15.2 [https://lura.ph/]\n' + text
            return text

        def leftovers():
            return sorted(f for f in os.listdir(tmp) if f.startswith('PWNED'))

        def run(name, cmd, cwd=tmp, timeout=300):
            r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                               encoding='utf-8', errors='replace')
            return r.returncode, r.stdout + r.stderr

        def verdict(name, log, want_probe=True):
            check(name + ': no file written', not leftovers(), str(leftovers()))
            for f in leftovers():
                os.unlink(os.path.join(tmp, f))
            # (the hostile source itself, which some logs quote, spells these markers in two halves)
            check(name + ': secret not read', 'LEAK:read:' not in log, log[-400:])
            check(name + ': native table not reached', 'LEAK:native:' not in log, log[-400:])
            if want_probe:
                check(name + ': the hostile code did run', 'PROBED found=0' in log, log[-600:])

        py = sys.executable

        # ---- fastsolve.py (the container sandbox), without and with a prelude
        for variant, kw in (('fastsolve', {}), ('fastsolve+prelude', {'prelude': True})):
            src = os.path.join(tmp, 'evil.lua')
            with open(src, 'w', encoding='utf-8') as fh:
                fh.write(hostile('plain', **kw))
            out = os.path.join(tmp, 'fs_out')
            shutil.rmtree(out, ignore_errors=True)
            rc, log = run(variant, [py, '-B', os.path.join(ROOT, 'fastsolve.py'), src, out])
            raw = open(os.path.join(out, '_out.txt'), encoding='latin-1').read() if os.path.isfile(
                os.path.join(out, '_out.txt')) else ''
            verdict(variant, log + raw)
            # the hostile code calls loadstring itself (those captures are real); the forged
            # lines carry the text "--forged"
            forged = [f for f in sorted(os.listdir(out)) if f.startswith(('chunk', 'blob', 'layer'))
                      and b'--forged' in open(os.path.join(out, f), 'rb').read()]
            check(variant + ': forged [LOADHEX]/[HEX] lines make no files', not forged, str(forged))
            check(variant + ': run reached its end', '[DONE]' in raw, raw[-300:])
            if kw:
                check(variant + ': prelude ran in the sandbox too', 'PRELUDEPROBED found=0' in raw, raw[-300:])

        # ---- mkfast.py --stub (robstub objects for unknown names)
        src = os.path.join(tmp, 'evil.lua')
        with open(src, 'w', encoding='utf-8') as fh:
            fh.write(hostile('plain'))
        runner = os.path.join(tmp, 'runner.lua')
        rc, log = run('mkfast', [py, '-B', os.path.join(ROOT, 'mkfast.py'), src, runner, '48', '--stub'])
        check('mkfast --stub builds', rc == 0, log[-300:])
        rc, log = run('mkfast run', [luau, runner])
        verdict('mkfast --stub', log)

        # ---- deob.py: step [2b] runs a file with a Luraph header in that sandbox
        rc, log = run('deob', [py, '-B', os.path.join(ROOT, 'deob.py'), src, '-o', os.path.join(tmp, 'o.lua')])
        verdict('deob.py', log, want_probe=False)
        chunk = os.path.join(tmp, 'evil.chunk.lua')
        check('deob.py: forged chunk not taken',
              not os.path.exists(chunk) or b'--forged' not in open(chunk, 'rb').read(), log[-300:])
        # ---- deob.py on a plain script (no header, no container): must not be run at all
        plain = os.path.join(tmp, 'plain.lua')
        with open(plain, 'w', encoding='utf-8') as fh:
            fh.write(hostile('plain', header=False).replace('PROBED', 'PLAINRAN'))
        rc, log = run('deob plain', [py, '-B', os.path.join(ROOT, 'deob.py'), plain, '-o', os.path.join(tmp, 'p.lua')])
        verdict('deob.py plain script', log, want_probe=False)
        check('deob.py: a plain script is not executed', '没有执行它' in log and rc == 2, 'rc=%d ' % rc + log[-300:])
        import deob
        info = deob.probe(plain, work_dir=os.path.join(tmp, 'probe'))
        verdict('deob.probe plain script', str(info), want_probe=False)
        check('deob.probe: a plain script is not executed',
              info.get('sandbox_run') is False and info.get('form') == 'unknown', str(info))
        info = deob.probe(src, work_dir=os.path.join(tmp, 'probe2'))
        verdict('deob.probe hostile file with header', str(info), want_probe=False)
        check('deob.probe: a file with a Luraph header is unwrapped in the sandbox',
              info.get('sandbox_run') is True, str(info))

        # ---- autostub.py
        rc, log = run('autostub', [py, '-B', os.path.join(ROOT, 'autostub.py'), src, '--timeout', '30',
                                   '--max-round', '6'])
        verdict('autostub.py', log, want_probe=False)

        # ---- trace_vm.py
        tsrc = os.path.join(tmp, 'evil_trace.lua')
        with open(tsrc, 'w', encoding='utf-8') as fh:
            fh.write(hostile('dispatch', header=False))
        tout = os.path.join(tmp, 'trace.txt')
        rc, log = run('trace_vm', [py, '-B', os.path.join(ROOT, 'trace_vm.py'), tsrc, tout, '--timeout', '30'])
        verdict('trace_vm.py', log, want_probe=False)
        trace = open(tout).read() if os.path.isfile(tout) else ''
        check('trace_vm.py: traced the real loop', trace.split() == ['5', '1', '99', '2'], repr(trace[:80]) + log[-300:])

        # ---- inlinevm.py: form C harness, run locally
        import inlinevm
        from pathlib import Path
        hpath = Path(tmp) / 'evil.harness.lua'
        inlinevm.make_harness(Path(src), {}, hpath)
        res = inlinevm.run_harness(hpath, timeout=60)
        verdict('inlinevm harness', str(res.get('raw', '')) + str(res.get('error', '')), want_probe=False)
        check('inlinevm harness: ran', res.get('ok') and 'PROBED found=0' in res.get('raw', ''),
              str(res.get('error')) + str(res.get('raw', ''))[-300:])

        # ---- dynlift.py
        dsrc = os.path.join(tmp, 'evil_dyn.lua')
        with open(dsrc, 'w', encoding='utf-8') as fh:
            fh.write(hostile('loader', header=False))
        import contextlib
        import io
        import dynlift
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bc = dynlift.extract_bc(dsrc)
        verdict('dynlift.py', buf.getvalue(), want_probe=False)
        check('dynlift.py: hook ran inside the sandbox and got the real value', bc == b'ABCD',
              repr(bc) + buf.getvalue()[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print('%d checks, %d failure(s)' % (checks, failures))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
