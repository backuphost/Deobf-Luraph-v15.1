#!/usr/bin/env python3
"""mkfast.py <target.lua> <out_runner.lua> [minlen] [--stub] [--arg FILE]... [--no-call]
把目标脚本嵌进 runner，用纯 Lua buffer 模拟接管全部 buffer 操作，
可选叠加 Roblox 万能桩，跑完后 dump 所有 buffer（hex）——绕过符号执行直接拿解码结果。

What the runner does (it is a sandbox for Luraph's outer CONTAINER, not for the script):

  * buffer library = bufemu.lua, so every buffer the container fills can be dumped at the end
    ([BUF i] / [HEX i] lines, as before);
  * loadstring / load are hooked: the code the container wants to load is printed
    ([LOAD n] / [LOADHEX n]) and is NOT run; the function handed back records the arguments
    of its first call ([ARG n.k] / [ARGHEX n.k]: the data buffer the VM chunk is started
    with) and returns a function that does nothing.  The protected script never starts;
  * the target runs in a table of its own (luasandbox.py): Luau's libraries by an explicit
    list and nothing else of the interpreter.  bin/luau is the patched build with readfile /
    writefile / require ...; the target used to see all of it through a fall-back to `_G`;
  * what the target prints is written as "[OUT] ..." lines, so it cannot pass for a line of
    the runner ([LOADHEX n] and the like, which fastsolve.py turns into files);
  * the globals an executor adds and a key line in front of the protected code uses
    (getgenv, getrenv, shared, _G) exist.  `getgenv().SCRIPT_KEY = "..."` used to stop the run
    with "attempt to call a nil value";
  * text in front of the "-- This file was protected using Luraph Obfuscator" comment (such
    a key line) runs first, on its own; if it fails the container still runs.

  --arg FILE   pass FILE's bytes as a buffer argument to the target chunk (repeatable): an
               inner chunk that a container starts with a buffer
  --no-call    do not call the function the target returns
"""
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


# 多路径回退：脚本同级 → 上级 → 上上级
def _find(fname):
    for root in (HERE, os.path.dirname(HERE), os.path.dirname(os.path.dirname(HERE))):
        p = os.path.join(root, fname)
        if os.path.isfile(p):
            return p
    return None


def _read(path):
    # latin-1 both ways: every byte of the target reaches the interpreter unchanged
    with open(path, 'rb') as fh:
        return fh.read().decode('latin-1')


RUNNER = r'''
local MINLEN = @MINLEN@
local SRC = @SRC@
local BE = (function()
@EMU@
end)()
local ROB = @STUB@
local ARGHEX = {@ARGS@}
local CALL_RESULT = @CALL@
local SANDBOX = @SANDBOX@
local realprint = print
local real_loadstring = loadstring
local function hexof(s)
  local parts = {}
  for i = 1, #s, 4096 do
    parts[#parts + 1] = (string.gsub(string.sub(s, i, i + 4095), ".", function(c)
      return string.format("%02x", string.byte(c))
    end))
  end
  return table.concat(parts)
end
local function unhex(h)
  local parts = {}
  for i = 1, #h, 4096 do
    parts[#parts + 1] = (string.gsub(string.sub(h, i, i + 4095), "%x%x", function(c)
      return string.char(tonumber(c, 16))
    end))
  end
  return table.concat(parts)
end
-- Lines of the runner start with a [TAG].  Text that comes from the script (its prints, its
-- error messages) is written so that no line of it can start with one.
local function oneline(s) return (string.gsub(tostring(s), "[\r\n]", " ")) end
local function indent(s) return (string.gsub(tostring(s), "\r?\n", "\n    ")) end

-- what the container tries to load: record it, never run it
local LOADS = 0
local function capture(code, name)
  local probe, cerr
  if type(name) == "string" then probe, cerr = real_loadstring(code, name)
  else probe, cerr = real_loadstring(code, "=load") end
  if not probe then return nil, cerr end
  LOADS = LOADS + 1
  local n = LOADS
  realprint(string.format("[LOAD %d] len=%d namehex=%s", n, #code,
    type(name) == "string" and hexof(name) or "-"))     -- "-": no chunk name was given
  realprint("[LOADHEX " .. n .. "] " .. hexof(code))
  local called = false
  return function(...)
    if not called then
      called = true
      local cnt = select("#", ...)
      realprint(string.format("[CALL %d] args=%d", n, cnt))
      for i = 1, cnt do
        local v = select(i, ...)
        if BE.__isbuffer(v) then
          realprint(string.format("[ARG %d.%d] buffer len=%d", n, i, v.n))
          realprint("[ARGHEX " .. n .. "." .. i .. "] " .. hexof(BE.tostring(v)))
        elseif type(v) == "string" then
          realprint(string.format("[ARG %d.%d] string len=%d", n, i, #v))
          realprint("[ARGHEX " .. n .. "." .. i .. "] " .. hexof(v))
        else
          realprint(string.format("[ARG %d.%d] %s", n, i, type(v)))
        end
      end
    end
    return function() end
  end
end

-- The target runs in a table of its own (luasandbox.py): Luau's libraries by an explicit
-- list, nothing else of the interpreter.  No readfile / writefile / require, and getfenv
-- cannot reach the real globals.
local ENV = SANDBOX()
local function genv() return ENV end
local function say(...)
  local n = select("#", ...)
  local parts = table.create(n)
  for i = 1, n do parts[i] = tostring((select(i, ...))) end
  realprint("[OUT] " .. (string.gsub(table.concat(parts, "\t"), "\r?\n", "\n[OUT] ")))
end
ENV.buffer = BE
ENV.print = say
ENV.warn = say
ENV.getgenv = genv
ENV.getrenv = genv
ENV.shared = {}
ENV.loadstring = function(code, name)
  if type(code) ~= "string" then return nil, "loadstring: string expected" end
  return capture(code, name)
end
ENV.load = function(code, name)
  if type(code) == "function" then
    local parts = {}
    while true do
      local piece = code()
      if piece == nil or piece == "" then break end
      parts[#parts + 1] = piece
    end
    code = table.concat(parts)
  end
  if type(code) ~= "string" then return nil, "load: string expected" end
  return capture(code, name)
end
if ROB then
  -- --stub: names Luau does not have become do-nothing objects (robstub.lua)
  setmetatable(ENV, { __index = function(t, k)
    local v = ROB[k]
    if v ~= nil then return v end
    return ROB.ANY(tostring(k))
  end })
end

local function compile(text, name)
  local chunk, lerr = real_loadstring(text, name)
  if chunk then setfenv(chunk, ENV) end
  return chunk, lerr
end

-- an owner-added key line in front of the Luraph header runs on its own
local PRELUDE, MAIN = nil, SRC
local hpos = string.find(SRC, "%-%-[^\n]-protected using Luraph Obfuscator")
if hpos and hpos > 1 and string.find(string.sub(SRC, 1, hpos - 1), "%S") then
  local main = compile(string.sub(SRC, hpos), "=target")
  if main then PRELUDE, MAIN = string.sub(SRC, 1, hpos - 1), string.sub(SRC, hpos) end
end
if PRELUDE then
  local pre, perr = compile(PRELUDE, "=prelude")
  local ok, err = true, perr
  if pre then ok, err = pcall(pre) else ok = false end
  realprint(string.format("[PRELUDE] bytes=%d ok=%s%s", #PRELUDE, tostring(ok),
    ok and "" or (" err=" .. oneline(err))))
end

local ARGS = {}
for i = 1, #ARGHEX do ARGS[i] = BE.fromstring(unhex(ARGHEX[i])) end

local ok, err = xpcall(function()
  local chunk, lerr = compile(MAIN, "=target")
  if not chunk then error("load failed: " .. tostring(lerr)) end
  local r = chunk(table.unpack(ARGS, 1, #ARGS))
  if CALL_RESULT and type(r) == "function" then r() end
end, function(e) return tostring(e) .. string.char(10) .. debug.traceback("", 2) end)
if not ok then realprint("[RUNERR] " .. indent(err)) end
local bufs = BE.__dump(MINLEN)
realprint(string.format("[BUFS] %d", #bufs))
for i = 1, #bufs do
  local b = bufs[i]
  local s = BE.tostring(b)
  realprint(string.format("[BUF %d] len=%d", i, #s))
  realprint("[HEX " .. i .. "] " .. hexof(s))
end
realprint(string.format("[DONE] loads=%d", LOADS))
'''


def build(src, minlen="512", stub=False, arg_files=(), call_result=True):
    """Text of the runner for the target source `src` (a latin-1 decoded str)."""
    import luasandbox
    emu_path = _find('bufemu.lua')
    if not emu_path:
        raise FileNotFoundError('找不到 bufemu.lua（应在脚本目录或其上级）')
    emu = _read(emu_path)

    stub_src = 'nil'
    if stub:
        stub_path = _find('robstub.lua')
        if stub_path:
            stub_src = "(function()\n" + _read(stub_path) + "\nend)()"

    args = []
    for f in arg_files:
        with open(f, 'rb') as fh:
            args.append('"' + fh.read().hex() + '"')

    # SRC is the target byte for byte (luasandbox.lua_string_expr: a long bracket per piece,
    # cut at carriage returns, which Luau would otherwise drop from "\r\n"), except for a
    # UTF-8 byte order mark in front of it, which no Luau accepts
    literal = luasandbox.lua_string_expr(luasandbox.strip_bom(src))
    runner = (luasandbox.bytes_str(RUNNER).replace('@MINLEN@', str(int(minlen)))
              .replace('@EMU@', emu)
              .replace('@STUB@', stub_src)
              .replace('@ARGS@', ', '.join(args))
              .replace('@CALL@', 'true' if call_result else 'false')
              .replace('@SANDBOX@', luasandbox.SANDBOX_LUA)
              .replace('@SRC@', literal))
    return runner, literal.count('[\n')


def main():
    argv = sys.argv[1:]
    stub = '--stub' in argv
    call_result = '--no-call' not in argv
    arg_files = []
    pos = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--arg' and i + 1 < len(argv):
            arg_files.append(argv[i + 1])
            i += 2
            continue
        if not a.startswith('--'):
            pos.append(a)
        i += 1
    if len(pos) < 2:
        sys.stderr.write('用法: mkfast.py <target.lua> <out_runner.lua> [minlen] [--stub] '
                         '[--arg FILE]... [--no-call]\n')
        return 2
    src_path, out_path = pos[0], pos[1]
    minlen = pos[2] if len(pos) > 2 else "512"
    try:
        runner, n = build(_read(src_path), minlen, stub, arg_files, call_result)
    except FileNotFoundError as e:
        sys.stderr.write('[mkfast] %s\n' % e)
        return 3
    with open(out_path, 'wb') as fh:
        fh.write(runner.encode('latin-1'))
    print('wrote', out_path, len(runner), 'bytes; source in', n, 'piece(s); stub', bool(stub),
          'args', len(arg_files))
    return 0


if __name__ == '__main__':
    sys.exit(main())
