#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
humanize.py —— 把反混淆产物整理成「像人写的」样子

只改空白，不改任何 token。做的事：

  1. 长行折行：超过 MAX_LEN 的行在逗号 / 二元运算符之后断开，续行多缩进一级
  2. 表字面量：长表、含函数的表每个字段一行
  3. 顶层函数之间空一行
  4. 运算符两侧空格、逗号后空格
  5. 缩进按块结构重排

The first version worked on text lines with regular expressions.  It could not see multi-line
long strings or `--[[ ]]` comments (their content was re-spaced and re-wrapped like code), and
a rule of its own could glue or split tokens.  It now lexes the file and hands the tokens to
the formatter of libs/deobf_plus.py, which only chooses the whitespace between them; the
result is lexed again and compared with the input (strings and comments byte for byte) before
it is returned.

用法:
    python humanize.py 输入.luau [输出.luau]
    python humanize.py 输入.luau --max-len 100
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (str(HERE / 'libs'), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

MAX_LEN = 100


def process(text, max_len=MAX_LEN, info=None):
    """Re-indent and re-wrap Luau source.  Raises ValueError if the result would not be the
    same token sequence as the input (it never returns changed code).

    `info`: a dict that is told whether the indented layout was used (info['fallback'] False)
    or the plain one-statement-per-line layout, because the indented one failed its check
    (info['fallback'] True, info['reason'])."""
    import deobf_plus as dp
    toks = dp.tokenize(text)
    lay = {}
    out, ok = dp.format_checked(toks, max_len=max_len, wrap=True, blank_lines=True, info=lay)
    if info is not None:
        info.update(lay)
    if not ok or not dp.same_tokens(dp.tokenize(out), toks, comments=True):
        raise ValueError('formatting would change the token sequence')
    return out + '\n'


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    max_len = MAX_LEN
    skip = set()
    for i, a in enumerate(sys.argv):
        if a == '--max-len' and i + 1 < len(sys.argv):
            max_len = int(sys.argv[i + 1])
            skip.add(sys.argv[i + 1])
    args = [a for a in args if a not in skip]

    if not args:
        print(__doc__)
        return 1

    src = Path(args[0])
    if not src.is_file():
        print(f"[✗] 文件不存在：{src}")
        return 1
    dst = Path(args[1]) if len(args) > 1 else src.with_suffix('.human.luau')
    from deobf_plus import read_lua
    text = read_lua(src)

    before = text.count('\n')
    lay = {}
    try:
        out = process(text, max_len, info=lay)
    except ValueError as e:
        print(f"[humanize] 未输出：{e}")
        return 2
    after = out.count('\n')
    dst.write_bytes(out.encode('utf-8', errors='surrogateescape'))
    print(f"[humanize] {src.name} {before:,} 行 → {dst.name} {after:,} 行")
    if lay.get('fallback'):
        print(f"[humanize] 注意：缩进排版没有通过 token 校验（{str(lay.get('reason'))[:120]}），"
              "输出用的是保守排版（每条语句一行）")
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
