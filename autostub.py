#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
autostub.py —— 自动补齐 Luau CLI 缺失的 Roblox 全局，让样本能一路跑下去

思路：
    Luau CLI 没有 Roblox 类型（Color3 / Vector2 / Rect / UDim2 / game / workspace …）。
    每缺一个，样本就崩一次。这里反复运行：出错是因为某个值是 nil 时，把出错前样本最后
    读到的那个「不存在的全局名」补成万能桩，再跑，直到跑通、超时或达到迭代上限。
    （Luau 的报错不带全局名；旧版把报错里的类型名 nil 当成全局名去补，生成的桩不合语法。）

    桩只是「什么都不做的占位」：补出来的环境不忠实，跑通不代表脚本行为被还原。
    桩能调用、能取任意深度的字段、也能用数字下标（每个桩最多 256 个不同的下标，之后读到
    nil，自己数下标的 while 循环因此会结束）；不能和数字比大小。

    每轮补的东西会累积到最终 stub 文件里，可以直接拿去做执行轨迹。

    样本不在解释器自己的全局表里运行，而是在一张单独的环境表里（luasandbox.py）：
    只有 Luau 自带的库和这里补的桩，没有 readfile / writefile / require ——
    仓库用的 bin/luau 是打过补丁的版本，那些函数在它的全局表里是真的。

用法:
    python autostub.py <样本.lua> [--max-round 40] [--save stub.lua] [--timeout 60]
"""
import re
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

BASE_STUB = '''-- 自动生成的 Roblox 环境补齐
-- 返回一个「看起来像数字」的对象：能算术、能比较、能转数字
local __num_mt
__num_mt = {
    __index = function() return 0 end,
    __tostring = function() return "0" end,
    __add = function() return 0 end, __sub = function() return 0 end,
    __mul = function() return 0 end, __div = function() return 0 end,
    __mod = function() return 0 end, __pow = function() return 0 end,
    __idiv = function() return 0 end, __unm = function() return 0 end,
    __concat = function() return "" end,
    __len = function() return 0 end,
    __eq = function() return false end,
    __lt = function() return false end,
    __le = function() return false end,
}
local function __value()
    return setmetatable({}, __num_mt)
end

-- 数字下标：桩的 t[1]、t[3] 也是桩（同一个下标每次读到同一个对象），所以
-- `Foo:Bar(1).list[3]["k"]` 这样的链能一路读下去。每个桩最多给 __NUM_MAX 个不同的数字下标，
-- 再往后读到 nil：`while t[i] do i += 1 end` 这种自己数下标的循环因此会结束
-- （ipairs 和 # 不走元方法：对桩来说长度是 0，ipairs 一轮都不跑）。
local __NUM_MAX = 256
local __num_cache = setmetatable({}, { __mode = "k" })
local function __nth(t, k, make)
    if k ~= k then return nil end   -- NaN
    local c = __num_cache[t]
    if not c then
        c = { count = 0, items = {} }
        __num_cache[t] = c
    end
    local v = c.items[k]
    if v == nil then
        if c.count >= __NUM_MAX then return nil end
        c.count += 1
        v = make()
        c.items[k] = v
    end
    return v
end

-- 万能桩：能调用、能取任意深度的字段和下标（Foo.Bar.Baz、Foo.Bar().x.y()、Foo.list[3].k）、
-- 能参与算术和拼接；什么都不做。缺失的全局名补成它。
-- 做不到的：和数字比大小（Luau 不为 table < number 调元方法，直接报错）；那种脚本会停在
-- 「非缺失类错误」上，如实报告。数字下标见上：每个桩最多 __NUM_MAX 个。
local __any_name = setmetatable({}, { __mode = "k" })
local __any_mt = {}
local function __any(name)
    local t = {}
    __any_name[t] = name
    return setmetatable(t, __any_mt)
end
__any_mt.__index = function(t, k)
    if type(k) == 'number' then
        return __nth(t, k, function() return __any(tostring(__any_name[t]) .. '[' .. tostring(k) .. ']') end)
    end
    local v = __any(tostring(__any_name[t]) .. '.' .. tostring(k))
    rawset(t, k, v)                 -- 同一个字段每次读到同一个对象
    return v
end
__any_mt.__call = function(t) return __any(tostring(__any_name[t]) .. '()') end
__any_mt.__tostring = function(t) return tostring(__any_name[t]) end
__any_mt.__concat = function(a, b) return tostring(a) .. tostring(b) end
__any_mt.__len = function() return 0 end
__any_mt.__unm = function() return 0 end
__any_mt.__lt = function() return false end
__any_mt.__le = function() return false end
__any_mt.__iter = function() return function() return nil end end
for _, ev in ipairs({ '__add', '__sub', '__mul', '__div', '__mod', '__pow', '__idiv' }) do
    __any_mt[ev] = function() return 0 end
end

-- 类型表：Color3.new(...) / Vector2.new(...) 这类必须能调用并返回可用对象；
-- 成员本身还能继续取字段（Enum.KeyCode.E）
local function __typetbl(name)
    local t = {}
    return setmetatable(t, {
        __index = function(_, k)
            if type(k) == 'number' then return 0 end
            local member
            member = setmetatable({}, {
                __call = function() return __value() end,
                __index = function(_, k2)
                    local sub = name .. '.' .. tostring(k)
                    if type(k2) == 'number' then
                        return __nth(member, k2, function() return __any(sub .. '[' .. tostring(k2) .. ']') end)
                    end
                    return __any(sub .. '.' .. tostring(k2))
                end,
                __tostring = function() return name .. '.' .. tostring(k) end,
            })
            rawset(t, k, member)
            return member
        end,
        __call = function() return __value() end,
        __tostring = function() return name end,
    })
end

-- 服务/实例桩：任何方法都能调，返回可继续索引的对象
local __inst_cache = {}
local function __inst(name)
    if __inst_cache[name] then return __inst_cache[name] end
    local t = {}
    __inst_cache[name] = t
    return setmetatable(t, {
        __index = function(_, k)
            if type(k) == 'number' then
                return __nth(t, k, function() return __inst(name .. '[' .. tostring(k) .. ']') end)
            end
            local member
            member = setmetatable({}, {
                __index = function(_, k2)
                    local sub = name .. '.' .. tostring(k)
                    if type(k2) == 'number' then
                        return __nth(member, k2, function() return __inst(sub .. '[' .. tostring(k2) .. ']') end)
                    end
                    return __inst(sub .. '.' .. tostring(k2))
                end,
                __call = function() return __inst(name .. '.' .. tostring(k) .. '()') end,
                __tostring = function() return name .. '.' .. tostring(k) end,
            })
            return member
        end,
        __newindex = function() end,
        __call = function() return __inst(name .. '()') end,
        __tostring = function() return name end,
        __eq = function() return false end,
    })
end

local __FUNC_STUBS = {
    Wait = function() end, wait = function() end,
    tick = function() return os.clock() end,
    time = function() return os.clock() end,
    spawn = function(f) if type(f) == 'function' then pcall(f) end end,
    task = {
        wait = function() end,
        spawn = function(f) if type(f) == 'function' then pcall(f) end end,
        delay = function(_, f) if type(f) == 'function' then pcall(f) end end,
        defer = function(f) if type(f) == 'function' then pcall(f) end end,
        cancel = function() end,
    },
    warn = function() end,
    identifyexecutor = function() return "stub", "1.0" end,
    gethui = function() return __inst("gethui") end,
    getscriptclosure = function() return function() end end,
    hookfunction = function(a, b) return a end,
    checkcaller = function() return false end,
    setreadonly = function() end,
    isreadonly = function() return false end,
    cloneref = function(x) return x end,
    firetouchinterest = function() end,
    fireclickdetector = function() end,
    request = function() return { StatusCode = 200, Body = "" } end,
    http_request = function() return { StatusCode = 200, Body = "" } end,
}

-- 类型表
local __TYPES = {
    'Color3', 'Vector2', 'Vector3', 'Vector3int16', 'Vector2int16',
    'Rect', 'UDim', 'UDim2', 'CFrame', 'BrickColor', 'Font',
    'NumberSequence', 'ColorSequence', 'NumberRange', 'PhysicalProperties',
    'TweenInfo', 'Ray', 'Region3', 'Region3int16', 'Random', 'DateTime',
    'Instance', 'Enum', 'EnumItem', 'OverlapParams', 'RaycastParams',
    'NumberSequenceKeypoint', 'ColorSequenceKeypoint', 'Axes', 'Faces',
    'Content', 'Path2DControlPoint', 'SharedTable', 'buffer',
}
'''

APPLY_STUB = '''
-- 把这些补进样本的全局表。样本不在解释器自己的全局里跑，而是在一张单独的表里
-- （luasandbox.py：只有 Luau 自带的库，没有 readfile / writefile / require），
-- 这张表就是 __ENV，可以直接写。
local _ENV_T = __ENV
-- 只补「真的没有」的名字：Luau 自带的 buffer / typeof 等不会被桩顶掉。
for _, n in ipairs(__TYPES) do
    if _ENV_T[n] == nil then _ENV_T[n] = __typetbl(n) end
end
for k, v in pairs(__FUNC_STUBS) do
    if _ENV_T[k] == nil then _ENV_T[k] = v end
end
if _ENV_T['game'] == nil then _ENV_T.game = __inst('game') end
if _ENV_T['workspace'] == nil then _ENV_T.workspace = __inst('workspace') end
if _ENV_T['script'] == nil then _ENV_T.script = __inst('script') end
if _ENV_T['Enum'] == nil then _ENV_T.Enum = __typetbl('Enum') end
_ENV_T.__inst = __inst
_ENV_T.__value = __value
_ENV_T.__typetbl = __typetbl
_ENV_T.__any = __any
-- getgenv() 必须返回一个可写的表：`getgenv().SCRIPT_KEY = "..."` 这种前导行很常见。
_ENV_T.getgenv = function() return _ENV_T end
_ENV_T.getrenv = _ENV_T.getgenv
-- 记下样本读过、但这里没有的全局名（按先后顺序）。Luau 的报错只说
-- "attempt to index nil with 'X'"，不说是哪个全局；出错前最后读到的那个缺失名就是线索。
local __MISSING = {}
setmetatable(_ENV_T, { __index = function(_, k)
    if type(k) == "string" then __MISSING[#__MISSING + 1] = k end
    return nil
end })
'''

# 样本在沙箱表里运行；出错时把错误信息打印出来（下面按它决定补哪个名字）
RUN_BODY = '''
if not __CHUNK then
    print("@@ERR@@ run.lua: load failed: " .. (string.gsub(tostring(__LOADERR), "%c", " ")))
else
    local ok, err = xpcall(__CHUNK, function(e) return tostring(e) end)
    if not ok then
        print("@@ERR@@ " .. (string.gsub(tostring(err), "%c", " ")))
        print("@@MISSING@@ " .. tostring(__MISSING[#__MISSING]))
    end
end
'''


def find_luau():
    sys.path.insert(0, str(HERE))
    import luaubin
    return luaubin.find_luau()


# errors that mean "something the sample expected was nil"
NIL_ERR = re.compile(r"attempt to (?:index nil|call a nil value|perform arithmetic .*nil|"
                     r"compare nil|concatenate nil|get length of a nil value)")
IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')


def build(sample_text, stub_extra):
    """Text of the program that runs the sample inside the sandbox with the stubs added.
    `sample_text`: the sample, one character per byte (read as latin-1)."""
    sys.path.insert(0, str(HERE))
    import luasandbox
    return luasandbox.launcher(RUN_BODY, sample_text, chunk_name='=run.lua',
                               setup_lua=BASE_STUB + APPLY_STUB + stub_extra)


def run(sample_text, stub_extra, luau, timeout=60):
    """跑一次，返回 (状态, 错误信息, 出错前最后读到的缺失全局名或 None)。
    状态：True 跑通 / False 出错 / 'timeout'。`sample_text`：按 latin-1 读入的样本。"""
    td = Path(tempfile.mkdtemp(prefix='autostub_'))
    f = td / 'run.lua'
    f.write_bytes(build(sample_text, stub_extra).encode('latin-1'))
    try:
        r = subprocess.run([str(luau), str(f)], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 'timeout', '执行超时（可能进了死循环或等事件）', None
    finally:
        shutil.rmtree(td, ignore_errors=True)
    out = ((r.stdout or b'') + (r.stderr or b'')).decode('utf-8', errors='replace')
    out = out.replace(str(f), 'run.lua')
    m = re.search(r'^@@ERR@@ (.*)$', out, re.M)
    if m:
        mm = re.search(r'^@@MISSING@@ (.*)$', out, re.M)
        missing = mm.group(1).strip() if mm else None
        return False, m.group(1), (missing if missing and missing != 'nil' else None)
    if r.returncode != 0:
        # the runner itself failed (a stub line that does not compile, the interpreter died)
        return False, (out.strip().split('\n')[0] or f'luau 返回码 {r.returncode}'), None
    return True, '', None


def main():
    max_round = 40
    save = None
    timeout = 60
    args = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ('--max-round', '--save', '--timeout') and i + 1 < len(argv):
            if a == '--max-round':
                max_round = int(argv[i + 1])
            elif a == '--save':
                save = argv[i + 1]
            else:
                timeout = int(argv[i + 1])
            i += 2
            continue
        if not a.startswith('--'):
            args.append(a)
        i += 1

    if not args:
        print(__doc__)
        return 1
    if not Path(args[0]).is_file():
        print(f"[✗] 文件不存在：{args[0]}")
        return 1

    import luasandbox
    sample = luasandbox.strip_bom(Path(args[0]).read_bytes().decode('latin-1'))
    luau = find_luau()
    if not luau:
        print("[✗] 找不到 luau（先跑 ./setup.sh）")
        return 1

    extra = []
    seen = set()
    prev_err = None
    last = None
    for rd in range(1, max_round + 1):
        ok, err, missing = run(sample, '\n'.join(extra), luau, timeout)
        if ok is True:
            print(f"\n[✓] 第 {rd} 轮跑通，无报错")
            break
        if ok == 'timeout':
            print(f"\n[⏱] 第 {rd} 轮：{err}")
            break
        if err == prev_err and last is not None:
            # the stub added last round changed nothing: the nil does not come from it
            extra.pop()
            seen.discard(last)
            print(f"[轮 {rd}] 补了 {last} 之后错误没有变化（已撤回这个桩）；"
                  f"这个 nil 不是缺失的全局造成的：{err[:140]}")
            break
        prev_err = err
        if not NIL_ERR.search(err):
            print(f"[轮 {rd}] 非缺失类错误：{err[:160]}")
            break
        if not missing or not IDENT.match(missing):
            print(f"[轮 {rd}] 出错的 nil 不是来自缺失的全局名，补不了：{err[:160]}")
            break
        if missing in seen:
            print(f"[轮 {rd}] 补了 {missing} 之后仍在同一处失败，放弃：{err[:160]}")
            break
        seen.add(missing)
        last = missing
        print(f"[轮 {rd:>2}] {err[:90]}")
        print(f"        出错前最后读到的缺失全局是 {missing} → 补成万能桩（能调用、能取任意深度的字段和下标、"
              "能做算术和拼接，什么都不做）")
        extra.append(f'_ENV_T["{missing}"] = __any("{missing}")')
    else:
        print(f"\n[!] 到达轮数上限 {max_round}，仍未跑通")
    if seen:
        print(f"[=] 共补了 {len(seen)} 个全局：{', '.join(sorted(seen))}")

    if save:
        # the saved file is the sandbox + the stubs: it defines __ENV, the table a sample has
        # to be started in (setfenv(chunk, __ENV)); see build() above
        import luasandbox
        hdr = ('local __SANDBOX = ' + luasandbox.SANDBOX_LUA + '\nlocal __ENV = __SANDBOX()\n'
               + BASE_STUB + APPLY_STUB + '\n'.join(extra) + '\nreturn __ENV\n')
        Path(save).write_text(hdr, encoding='utf-8')
        print(f"[+] stub 已保存 → {save}（返回样本应当运行其中的环境表）")
    return 0


if __name__ == '__main__':
    sys.exit(main())
