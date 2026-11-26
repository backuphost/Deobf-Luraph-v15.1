#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trace_vm.py —— 对「VM 内联展开」的 Luraph 样本做执行轨迹追踪

适用场景（这是第三种形态）：
    - 业务代码编译进 VM 指令
    - 没有 luau_load / loadstring / vmcore 可 hook
    - 只有一个或多个 `while true do local D=F[b]; if D<61 then ...` 分派循环

做法：
    在分派点注入记录代码，把 (opcode, 操作数) 序列打出来。
    配合桩环境跑起来，得到指令流；再按 opcode 归类统计，
    能还原出「这个脚本到底调用了哪些 API、有哪些功能」。

局限（必须说清楚）：
    只有实际执行到的分支才有记录。没走到的代码看不到。

用法:
    python trace_vm.py <样本.lua> [轨迹.txt] [--max-ops 20000] [--arg buffer文件] [--timeout 60]

    --arg FILE   把 FILE 的内容作为 buffer 传给样本的顶层函数。Luraph 容器剥出来的 VM chunk
                 （deob.py 存成 *.chunk.lua）要用它的数据 buffer（*.buffer.bin）启动；
                 例：python trace_vm.py examples/layer3.lua --arg examples/blob4.bin

Fixed here: the stub environment used to answer EVERY global it did not list with a dummy
object, `setmetatable`, `select`, `type` and `error` included, so the VM object was never
built and nothing was ever traced; and the counter the injected code tests was a local of the
stub header, invisible to the sample.  Luau's own libraries are now in the environment by an
explicit list (luasandbox.py), only names Luau does not have become dummies.  The
interpreter's other globals (bin/luau is a patched build with readfile / writefile / require)
are not reachable from the sample.
"""
import re
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 分派点：local D=F[b];if D<61 —— 变量名和比较值都可能变
DISPATCH_RE = re.compile(
    r'local\s+(\w+)\s*=\s*(\w+)\s*\[\s*(\w+)\s*\]\s*;\s*if\s+\1\s*[<>]=?\s*\d+')


def find_dispatchers(src):
    return [(m.start(), m.group(1), m.group(2), m.group(3))
            for m in DISPATCH_RE.finditer(src)]


def build_hook(var, table, index, limit):
    """在分派点的 if 之前插入记录（__TRACE 是桩环境里的真函数，计数在它内部）"""
    return f'__TRACE({var},{index},{limit});'


def inject(src, limit=20000):
    """在所有分派点注入记录代码，返回 (新源码, 注入点数量)"""
    ds = find_dispatchers(src)
    if not ds:
        return None, 0
    # 从后往前插，避免偏移变化
    out = src
    for pos, var, table, index in reversed(ds):
        hook = build_hook(var, table, index, limit)
        # 匹配起点是 `local X=T[i];if X<..`：记录要插在 local 语句之后、if 之前
        semi = out.index(';', pos) + 1
        out = out[:semi] + hook + out[semi:]
    return out, len(ds)


STUB_HEADER = '''
-- 执行轨迹追踪用的桩头：把 Roblox 全局都变成「什么都能做」的对象。
-- 样本运行在 __ENV 里（luasandbox.py）：Luau 自带的库按白名单放进去，解释器的其它全局
-- （readfile / writefile / require ...）不在里面；Luau 没有的名字才变成万能桩。
local _TRACE_N = 0
local _real_print = print
local function __TRACE(op, pc, limit)
    if _TRACE_N < limit then
        _TRACE_N = _TRACE_N + 1
        _real_print("@@OP@@" .. tostring(op) .. "|" .. tostring(pc))
    end
end
local function mk(name)
    local t = {}
    return setmetatable(t, {
        __index = function(_, k)
            if type(k) == 'number' then
                return 0
            end
            return mk(name .. '.' .. tostring(k))
        end,
        __newindex = function() end,
        __call = function(...) return mk(name .. '()') end,
        __tostring = function() return name end,
        __concat = function(a, b) return tostring(a) .. tostring(b) end,
        __add = function() return 0 end, __sub = function() return 0 end,
        __mul = function() return 0 end, __div = function() return 0 end,
        __mod = function() return 0 end, __pow = function() return 0 end,
        __unm = function() return 0 end,
        __lt = function() return false end, __le = function() return false end,
        __eq = function() return false end,
        __len = function() return 0 end,
    })
end

local _env = __ENV
_env.__TRACE = __TRACE
-- 样本自己的输出加前缀：追踪记录（@@OP@@ 行）只能来自 __TRACE
_env.print = function(...)
    local n = select("#", ...)
    local parts = table.create(n)
    for i = 1, n do parts[i] = tostring((select(i, ...))) end
    _real_print("[OUT] " .. (string.gsub(table.concat(parts, "\\t"), "\\r?\\n", "\\n[OUT] ")))
end
_env.warn = function() end                       -- 噪音大
_env.loadstring = function() return function() end end
_env.load = _env.loadstring
_env.getgenv = function() return _env end
_env.getrenv = _env.getgenv
_env.getfenv = function() return _env end
_env.setfenv = function() end
_env.tick = os.clock
_env.time = os.clock
_env.task = { wait = function() end, spawn = function(f) if type(f)=='function' then pcall(f) end end,
    delay = function(_, f) if type(f)=='function' then pcall(f) end end,
    defer = function(f) if type(f)=='function' then pcall(f) end end }
setmetatable(_env, {
    -- 到这里的都是 Luau 没有、上面也没定义的名字（game / workspace / Vector3 / Enum ...）
    __index = function(_, k)
        return mk(tostring(k))
    end,
})
'''

RUN_BODY = '''
local _arg = nil
if __ARGHEX then
    _arg = buffer.fromstring((string.gsub(__ARGHEX, "%x%x", function(c) return string.char(tonumber(c, 16)) end)))
end
if not __CHUNK then print("@@ERR load failed: " .. (string.gsub(tostring(__LOADERR), "%c", " "))) return end
local ok, err = pcall(function()
  local r = __CHUNK(_arg)
  if type(r) == "function" then return r() end
end)
if not ok then print("@@ERR " .. (string.gsub(tostring(err), "%c", " "))) else print("@@FINISHED") end
'''


def run_trace(sample, limit=20000, arg_file=None, timeout=60):
    """注入并运行，返回轨迹行"""
    sys.path.insert(0, str(HERE))
    import luaubin
    luau = luaubin.find_luau()
    if not luau:
        print("[✗] 找不到 luau 运行时；" + luaubin.MISSING_HINT)
        return None

    import luasandbox
    src = luasandbox.strip_bom(Path(sample).read_bytes().decode('latin-1'))
    injected, n = inject(src, limit)
    if injected is None:
        print("[✗] 找不到 VM 分派点（local X=Y[Z];if X<64 这种模式）")
        return None
    print(f"[trace] 在 {n} 处 VM 分派点注入记录代码")

    td = Path(tempfile.mkdtemp(prefix='tracevm_'))
    f = td / 'traced.lua'
    # 样本里有顶层 `...`（vararg），所以不能包进 function()...end：用 loadstring 把它编译成
    # 一个 chunk，再 setfenv 到桩环境（luasandbox.launcher 做这两步）。样本逐字节不变
    # （按 latin-1 读入、写出）。
    import luasandbox
    setup = STUB_HEADER
    if arg_file:
        setup += '\nlocal __ARGHEX = "' + Path(arg_file).read_bytes().hex() + '"\n'
    else:
        setup += '\nlocal __ARGHEX = nil\n'
    text = luasandbox.launcher(RUN_BODY, injected, chunk_name='traced', setup_lua=setup)
    f.write_bytes(text.encode('latin-1'))

    timed_out = False
    log = td / 'out.txt'
    try:
        with open(log, 'wb') as fh:     # a file, so a run that is killed still leaves its trace
            try:
                subprocess.run([str(luau), str(f)], stdout=fh, stderr=subprocess.STDOUT,
                               timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
        out = log.read_bytes().decode('latin-1')
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # lines of the runner start at the beginning of a line; the sample's own prints are
    # prefixed with [OUT]
    ops = re.findall(r'^@@OP@@(-?\d+)\|(-?\d+)', out, re.M)
    m = re.search(r'^@@ERR (.*)', out, re.M)
    if timed_out:
        print(f"[trace] 运行 {timeout} 秒后被终止（脚本在循环或等事件）")
    elif m:
        print(f"[trace] 运行中断：{m.group(1)[:150]}")
    elif re.search(r'^@@FINISHED', out, re.M):
        print("[trace] 脚本运行结束")
    if not ops:
        rest = [ln for ln in out.strip().split('\n')
                if ln.strip() and not ln.startswith('@@') and not ln.startswith('[OUT]')]
        print(f"[✗] 没抓到指令：{rest[0][:150] if rest else '(没有执行到任何分派点)'}")
        return None
    print(f"[trace] 记录 {len(ops):,} 条指令执行" + (f"（达到上限 {limit:,}）" if len(ops) >= limit else ""))
    return ops


def main():
    limit = 20000
    timeout = 60
    arg_file = None
    args = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ('--max-ops', '--arg', '--out', '--timeout') and i + 1 < len(argv):
            if a == '--max-ops':
                limit = int(argv[i + 1])
            elif a == '--timeout':
                timeout = int(argv[i + 1])
            elif a == '--arg':
                arg_file = argv[i + 1]
            else:
                args.append(argv[i + 1])
            i += 2
            continue
        if not a.startswith('--'):
            args.append(a)
        i += 1

    if not args:
        print(__doc__)
        return 1
    for need in [args[0]] + ([arg_file] if arg_file else []):
        if not Path(need).is_file():
            print(f"[✗] 文件不存在：{need}")
            return 1

    ops = run_trace(args[0], limit, arg_file, timeout)
    if not ops:
        return 1

    from collections import Counter
    c = Counter(op for op, _ in ops)
    print(f"\n[trace] opcode 分布（共 {len(c)} 种）：")
    for op, cnt in c.most_common(20):
        print(f"    opcode {op:>4}: {cnt:>7,} 次")

    if len(args) > 1:
        Path(args[1]).write_text(
            '\n'.join(f"{o}\t{i}" for o, i in ops), encoding='utf-8')
        print(f"\n[trace] 轨迹已写出 → {args[1]}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
