#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
luasandbox.py: the environment every script under analysis runs in

Several tools of this repository RUN (parts of) the protected script with bin/luau: the
container unwrap (mkfast.py / fastsolve.py), autostub.py, trace_vm.py, the form C harness of
inlinevm.py and dynlift.py.  bin/luau is not a plain Luau: the build this repository uses
(devm/luau_cli.patch) adds `readfile`, `writefile`, `ewrite`, `cwrap`, `resumetop`,
`setvectormetatable`, `__LPH_RUNMAIN` and `__LPH_NATIVE`, and every Luau CLI has `require`
and a real `loadstring`.  The devirtualizer needs those; a script that is only being looked
at must not get them.  (The runners used to let every name they did not define fall through
to the interpreter's globals, so `writefile("...", "...")` in an input file wrote that file.)

SANDBOX_LUA is a Lua expression.  Its value is a function `new()` that returns a fresh global
table for the analysed code:

  * it holds Luau's own libraries and base functions, by an explicit list (ALLOWED).  A name
    that is not on the list does not exist in the sandbox, whatever the interpreter has;
  * `_G` is the sandbox table itself;
  * `getfenv` never hands out the interpreter's global table or a runner's environment: for
    any function or stack level outside the sandbox it answers with the sandbox table;
  * `setfenv` only works on functions that already run inside the sandbox;
  * `loadstring` compiles into the sandbox (the runner may replace it by a hook).

The runner then adds what it wants the script to see (stub Roblox globals, a hooked
loadstring, an emulated buffer library ...) and starts the script with
`setfenv(chunk, env)`.  Nothing here makes the stubs faithful; the point is only that the
analysed code cannot reach the file system, the module loader or the interpreter patches.

Other limits stay with the caller: a timeout on the interpreter process (CPU), and the
interpreter's own memory handling.
"""

# Luau's standard globals (and nothing the CLI or the interpreter patches add).
ALLOWED = (
    '_VERSION', 'assert', 'bit32', 'buffer', 'collectgarbage', 'coroutine', 'debug', 'error',
    'gcinfo', 'getmetatable', 'integer', 'ipairs', 'math', 'newproxy', 'next', 'os', 'pairs',
    'pcall', 'print', 'rawequal', 'rawget', 'rawlen', 'rawset', 'select', 'setmetatable',
    'string', 'table', 'tonumber', 'tostring', 'type', 'typeof', 'unpack', 'utf8', 'vector',
    'xpcall',
)

# What the patched interpreter and the CLI add: must never be reachable from the sandbox.
# (Only used by the tests, to check that the list above stays clean.)
FORBIDDEN = (
    'readfile', 'writefile', 'ewrite', 'cwrap', 'resumetop', 'setvectormetatable',
    '__LPH_RUNMAIN', '__LPH_NATIVE', 'require', 'io', 'dofile', 'loadfile',
)

SANDBOX_LUA = r'''(function()
  local REAL = _G                     -- the interpreter's global table
  local rget, rset, rload = getfenv, setfenv, loadstring
  local dinfo = debug.info
  local type, pcall, error, setmetatable, floor = type, pcall, error, setmetatable, math.floor
  local ALLOWED = { @ALLOWED@ }
  return function()
    local env = {}
    for i = 1, #ALLOWED do env[ALLOWED[i]] = REAL[ALLOWED[i]] end
    env._G = env
    -- environments the analysed code may see: the sandbox table and tables it passed to
    -- setfenv itself
    local inside = setmetatable({}, { __mode = "k" })
    inside[env] = true
    local function target(f, who)
      if type(f) == "function" then return f end
      if f == nil then f = 1 end
      if type(f) ~= "number" then
        error("invalid argument #1 to '" .. who .. "' (number expected, got " .. type(f) .. ")", 3)
      end
      f = floor(f)
      if f < 0 then error("invalid argument #1 to '" .. who .. "' (level must be non-negative)", 3) end
      if f == 0 then return nil end   -- the thread's environment
      if f > 100000 then error("invalid argument #1 to '" .. who .. "' (invalid level)", 3) end
      local fn = dinfo(f + 2, "f")    -- + this helper and the wrapper that called it
      if fn == nil then error("invalid argument #1 to '" .. who .. "' (invalid level)", 3) end
      return fn
    end
    env.getfenv = function(f)
      local fn = target(f, "getfenv")
      if fn == nil then return env end
      local ok, e = pcall(rget, fn)
      if ok and inside[e] then return e end
      return env                      -- C functions, the runner's own functions
    end
    env.setfenv = function(f, t)
      if type(t) ~= "table" then
        error("invalid argument #2 to 'setfenv' (table expected, got " .. type(t) .. ")", 2)
      end
      local fn = target(f, "setfenv")
      if fn == nil then return end    -- the thread's environment is not the script's to change
      local ok, e = pcall(rget, fn)
      if not (ok and inside[e]) then
        error("'setfenv' cannot change environment of given object", 2)
      end
      inside[t] = true
      rset(fn, t)
      if type(f) == "function" then return f end
    end
    env.loadstring = function(code, name)
      if type(code) ~= "string" then
        error("invalid argument #1 to 'loadstring' (string expected, got " .. type(code) .. ")", 2)
      end
      local fn, err
      if type(name) == "string" then fn, err = rload(code, name) else fn, err = rload(code) end
      if fn then rset(fn, env) end
      return fn, err
    end
    return env
  end
end)()'''.replace('@ALLOWED@', ', '.join('"%s"' % n for n in ALLOWED))


def strip_bom(text):
    """`text` without a leading UTF-8 byte order mark (EF BB BF).

    Editors on Windows put one in front of a saved file.  It is not part of the script: Luau
    does not skip it (`Expected identifier ..., got Unicode character U+feff`), so a file
    that has one is not a program until it is gone.  Works on bytes, on a str decoded as
    UTF-8 ('\ufeff') and on a str with one character per byte (latin-1: 'ï»¿')."""
    if isinstance(text, bytes):
        return text[3:] if text.startswith(b'\xef\xbb\xbf') else text
    if text.startswith('\ufeff'):
        return text[1:]
    if text.startswith('\xef\xbb\xbf'):
        return text[3:]
    return text


def lua_string_expr(text):
    """A Lua expression whose value is exactly `text`.

    `text` is a str with one character per byte (the file read as latin-1).  A long bracket
    keeps every byte except that Luau drops a newline right after the opening bracket and
    turns "\\r\\n" into "\\n": so one newline is put after every opening bracket, and the
    text is cut at each carriage return and joined again with "\\r"."""
    def one(piece):
        edge = ''
        while (']' + edge + ']') in piece or piece.endswith(']' + edge):
            edge += '='
        return '[' + edge + '[\n' + piece + ']' + edge + ']'

    parts = text.split('\r')
    if len(parts) == 1:
        return one(parts[0])
    if len(parts) > 20000:
        # too many pieces for a table constructor to be worth it: hexadecimal
        return ('(string.gsub("' + text.encode('latin-1').hex()
                + '", "%x%x", function(c) return string.char(tonumber(c, 16)) end))')
    return 'table.concat({' + ', '.join(one(p) for p in parts) + '}, "\\r")'


def bytes_str(text):
    """Text of a runner (may hold non-ASCII comments) as a str with one character per UTF-8
    byte, so that it can be joined with a sample read as latin-1 and written out in one go."""
    return text.encode('utf-8').decode('latin-1')


def launcher(body_lua, source_text, chunk_name='=sample', setup_lua=''):
    """Text of a Luau program that runs `source_text` inside a fresh sandbox.

    The program defines, in this order:
        local __SANDBOX = <new()>           the factory
        local __ENV = __SANDBOX()           the sandbox table
        <setup_lua>                         the runner's additions to __ENV (stubs, hooks)
        local __SRC = <source_text>
        local __CHUNK, __LOADERR = loadstring(__SRC, chunk_name), with __ENV as environment
        <body_lua>                          what to do with __CHUNK (call it, report errors)
    `source_text`: str, one character per byte (file bytes decoded as latin-1); a byte order
    mark in front of it is dropped (strip_bom).  setup_lua and body_lua are ordinary text.
    The result is a str with one character per byte: write it with .encode('latin-1')."""
    source_text = strip_bom(source_text)
    return '\n'.join([
        'local __SANDBOX = ' + SANDBOX_LUA,
        'local __ENV = __SANDBOX()',
        bytes_str(setup_lua),
        'local __SRC = ' + lua_string_expr(source_text),
        'local __CHUNK, __LOADERR = loadstring(__SRC, "' + chunk_name + '")',
        'if __CHUNK then setfenv(__CHUNK, __ENV) end',
        bytes_str(body_lua),
        '',
    ])


def compile_only(text, luau_bin, timeout=300, chunk_name='output'):
    """Does `text` compile?  Nothing of it is executed: the interpreter is given a program
    that holds the text as a string and calls loadstring on it, and never calls the result.

    (Wrapping a text in `if false then ... end` and running that is NOT compile-only: a text
    that contains `end ... if false then` gets its middle part executed.)

    `text`: str as read with errors='surrogateescape', or bytes.
    Returns (True, '') or (False, first line of the compiler's message; line numbers are
    those of `text`)."""
    import os
    import subprocess
    import tempfile
    raw = text if isinstance(text, bytes) else text.encode('utf-8', errors='surrogateescape')
    prog = ('local f, e = loadstring(' + lua_string_expr(raw.decode('latin-1')) + ', "=' + chunk_name + '")\n'
            'if f then print("@@COMPILE_OK@@") else print("@@COMPILE_ERR@@ " .. '
            '(string.gsub(tostring(e), "%c", " "))) end\n')
    fd, path = tempfile.mkstemp(prefix='lv151_compile_', suffix='.lua')
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(prog.encode('latin-1'))
        try:
            r = subprocess.run([str(luau_bin), path], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, 'timeout'
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    out = (r.stdout + r.stderr).decode('utf-8', errors='replace')
    if '@@COMPILE_OK@@' in out:
        return True, ''
    for line in out.splitlines():
        if line.startswith('@@COMPILE_ERR@@ '):
            return False, line[len('@@COMPILE_ERR@@ '):].strip()
    first = out.strip().splitlines()[0] if out.strip() else 'luau exit status %d' % r.returncode
    return False, first.replace(path, '<check>')


if __name__ == '__main__':
    print(__doc__)
