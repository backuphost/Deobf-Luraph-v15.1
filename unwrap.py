#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
unwrap.py —— 去掉 Luraph 的纯包装函数层，减少无谓缩进（只为阅读）

Luraph 把业务代码包成：

    local __part1 = function(arg, ..., arg7)
        return function()
            return function()
                -- 真正的业务代码（缩进 12+）
            end
        end
    end

中间那几层只是闭包包装：函数体只有一句 `return function() ... end`。剥掉后：

    local __part1 = function(arg, ..., arg7)
        -- 真正的业务代码（缩进 4）
    end

What this does to the code, stated plainly: the result is easier to read but it is NOT the
same function.  `__part1(a)()()` ran the body; after unwrapping `__part1(a)` runs it.  Use it
on a copy you read, not on something you run.

To keep every name meaning what it meant, a layer is only removed when

  * the body of the outer function is exactly one statement, `return function() ... end`, and
  * the inner function takes no parameters and no `...`

so no variable of the body loses its declaration (a layer like `return function(v, p)` whose
body uses `v` is left alone).

The first version matched `function`/`end` pairs by counting keywords and counted both the
`for`/`while` and its `do`, so any body with a loop was cut at the wrong `end`; it also
shifted text lines, multi-line strings included.  This one takes the function boundaries from
luau-ast, splices at those offsets and re-indents with the token formatter (humanize.py).

用法:
    python unwrap.py 输入.luau [输出.luau]
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (str(HERE / 'libs'), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _wrapped_function(fn):
    """The inner function when `fn` is a pure wrapper around a parameterless function."""
    body = (fn.get('body') or {}).get('body', [])
    if len(body) != 1 or body[0].get('type') != 'AstStatReturn':
        return None
    lst = body[0].get('list', [])
    if len(lst) != 1 or lst[0].get('type') != 'AstExprFunction':
        return None
    inner = lst[0]
    if inner.get('args') or inner.get('vararg') or inner.get('self'):
        return None
    return inner


def unwrap_one(text, ast_bin=None):
    """处理文件里所有 `local __partN = function(...) return function() ...`
    返回 (新文本, 剥掉的层数)"""
    import luascope
    import luaubin
    import humanize
    ast_bin = ast_bin or luaubin.find_luau_ast()
    if not ast_bin:
        raise RuntimeError('找不到 luau-ast；' + luaubin.MISSING_HINT)
    root, errors = luascope.parse(text, ast_bin)
    if errors:
        raise RuntimeError('输入有语法错误，不处理: ' + errors[0])
    index = luascope.LineIndex(text)
    edits = []          # (start, end, replacement text), disjoint
    total = 0
    for st in root.get('body', []):
        if st.get('type') != 'AstStatLocal' or len(st.get('vars', [])) != 1:
            continue
        if not st['vars'][0]['name'].startswith('__part') or len(st.get('values', [])) != 1:
            continue
        outer = st['values'][0]
        if outer.get('type') != 'AstExprFunction':
            continue
        inner, layers = outer, 0
        while True:
            nxt = _wrapped_function(inner)
            if nxt is None:
                break
            inner, layers = nxt, layers + 1
        if not layers:
            continue
        o_s, o_e = index.span(outer['body']['location'])
        i_s, i_e = index.span(inner['body']['location'])
        edits.append((o_s, o_e, '\n' + text[i_s:i_e].strip('\n') + '\n'))
        total += layers
    if not edits:
        return text, 0
    out = text
    for s, e, new in sorted(edits, reverse=True):
        out = out[:s] + new + out[e:]
    try:
        out = humanize.process(out)
    except ValueError as e:
        raise RuntimeError('内部校验失败：重新排版改变了 token 序列（%s），未输出' % e)
    _root2, errors2 = luascope.parse(out, ast_bin)
    if errors2:
        raise RuntimeError('内部校验失败：剥层后的文本不能解析（' + errors2[0] + '），未输出')
    return out, total


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    if not args:
        print(__doc__)
        return 1
    src = Path(args[0])
    if not src.is_file():
        print(f"[✗] 文件不存在：{src}")
        return 1
    dst = Path(args[1]) if len(args) > 1 else src.with_suffix('.unwrap.luau')
    from deobf_plus import read_lua
    text = read_lua(src)
    try:
        out, n = unwrap_one(text)
    except RuntimeError as e:
        print(f"[unwrap] {e}")
        return 2
    dst.write_bytes(out.encode('utf-8', errors='surrogateescape'))
    indents = [len(l) - len(l.lstrip()) for l in out.split('\n') if l.strip()]
    print(f"[unwrap] 剥掉 {n} 层包装，最大缩进 {max(indents, default=0)} 空格 → {dst}")
    if n:
        print("[unwrap] 注意：剥层后的函数调用方式变了（少了几层 `()`），只用于阅读。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
