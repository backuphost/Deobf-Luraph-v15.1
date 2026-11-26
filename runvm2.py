#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""runvm2.py —— 用模板文件渲染，彻底避开 f-string 转义地狱

    python runvm2.py <样本.lua> [模板.lua]

A research script for ONE kind of sample: a VM object whose table has the slots `L6` (the
interpreter) and `F6` (the encrypted string table).  It pastes the table, the patched `L6`
and `F6` into a template (placeholders --$STUB, --$TABLE, --$L6, __F6__) and runs it.

The template run_template.lua was never added to the repository, so the script cannot run
as shipped: it now says so instead of dying at import, and takes the template as the second
argument.  For other samples use deob.py (static) or python3 -m devm.run (devirtualizer).
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import luaubin      # noqa: E402

STUB = """
-- Roblox 桩：只补类型，不干扰 VM 数值
local function RT(name)
    local t = {}
    return setmetatable(t, {
        __index = function(_, k)
            return function() return RT(name) end
        end,
        __call = function() return RT(name) end,
        __tostring = function() return name end,
    })
end
Color3, Vector3, Vector2, UDim, UDim2, Rect, CFrame = RT('Color3'), RT('Vector3'), RT('Vector2'), RT('UDim'), RT('UDim2'), RT('Rect'), RT('CFrame')
NumberRange, NumberSequence, ColorSequence, TweenInfo = RT('NumberRange'), RT('NumberSequence'), RT('ColorSequence'), RT('TweenInfo')
"""


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    sample = Path(sys.argv[1])
    template_path = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / 'run_template.lua'
    if not sample.is_file():
        print(f'[✗] 文件不存在：{sample}')
        return 1
    if not template_path.is_file():
        print(f'[✗] 找不到模板 {template_path}')
        print('    run_template.lua 不在仓库里（它只配合当初那一个样本）；把模板作为第二个参数传入，')
        print('    或者改用 deob.py / python3 -m devm.run。')
        return 2
    luau, luau_ast = luaubin.find_luau(), luaubin.find_luau_ast()
    if not luau or not luau_ast:
        print('[✗] 找不到 luau / luau-ast；' + luaubin.MISSING_HINT)
        return 3
    TEMPLATE = template_path.read_text(encoding='utf-8')
    r = subprocess.run([luau_ast, str(sample)], capture_output=True, text=True, timeout=180)
    start = r.stdout.find('{')
    if start < 0:
        print('[✗] luau-ast 没有输出 AST：' + (r.stderr or r.stdout)[:200])
        return 1
    ast = json.loads(r.stdout[start:])
    src = sample.read_text(encoding='utf-8', errors='ignore')
    lines = src.split('\n')

    def off(l, c):
        return sum(len(x) + 1 for x in lines[:l]) + c

    def text(node):
        m = re.match(r'(\d+),(\d+) - (\d+),(\d+)', node.get('location', ''))
        sl, sc, el, ec = map(int, m.groups())
        return src[off(sl, sc):off(el, ec)]

    try:
        expr = ast['root']['body'][0]['list'][0]['func']['func']['expr']
        items = expr['args'][0]['items']
    except (KeyError, IndexError, TypeError):
        print('[✗] 样本不是 `return setmetatable({...},{}):X()(...)` 这种形状，本脚本不适用')
        return 2
    slots = {}
    for it in items:
        k = it.get('key', {})
        key = k.get('value') if k.get('type') == 'AstExprConstantString' else (
            str(k.get('value')) if k.get('type') == 'AstExprConstantNumber' else None)
        if key is not None:
            slots[key] = it['value']

    if 'L6' not in slots or 'F6' not in slots or 'value' not in slots['F6']:
        print('[✗] 这个样本的表里没有 L6 / F6 槽位：本脚本只适用于当初那一个样本')
        print('    （槽位名每个样本都不同）。请改用 deob.py 或 python3 -m devm.run。')
        return 2
    table_text = text(expr['args'][0])
    l6_text = text(slots['L6'])
    # patch: nil -> universal object at the single return point
    patched = l6_text.replace('local d,X=33;', 'local d,X=33; __L6LOG(d,X);')
    if patched != l6_text:
        print('[+] L6 patched: nil guard injected')
        l6_text = patched
    else:
        print('[!] L6 patch NOT applied')
    f6 = slots['F6']['value']

    out = TEMPLATE
    out = out.replace('--$STUB', STUB)
    out = out.replace('--$TABLE', table_text)
    out = out.replace('--$L6', l6_text)
    out = out.replace('__F6__', '[==[' + f6 + ']==]')

    # the rendered program (template + pieces of the sample) runs inside the sandbox table of
    # luasandbox.py: Luau's libraries only, none of the patched interpreter's file functions
    import luasandbox
    body = ('if not __CHUNK then print("load failed: " .. tostring(__LOADERR)) else\n'
            '  local ok, err = pcall(__CHUNK)\n'
            '  if not ok then print("run failed: " .. tostring(err)) end\nend\n')
    program = luasandbox.launcher(body, out.encode('utf-8').decode('latin-1'), chunk_name='=run')
    tmpd = Path(tempfile.mkdtemp(prefix='runvm2_'))
    try:
        tmp = tmpd / 'run.lua'
        tmp.write_bytes(program.encode('latin-1'))
        outf = tmpd / 'luau_out.txt'
        with open(outf, 'wb') as fh:
            try:
                r = subprocess.run([luau, str(tmp)], stdout=fh, stderr=subprocess.PIPE, timeout=600)
            except subprocess.TimeoutExpired:
                print('[✗] 超时（600 秒）')
                return 1
        out = outf.read_text(errors='ignore')
        print(f'[+] stdout {len(out)} bytes')
        print(out[:3000])
        if r.stderr:
            print('--- stderr ---')
            print(r.stderr.decode('utf-8', 'replace')[:1500])
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
