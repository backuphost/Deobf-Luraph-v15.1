#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reslot.py —— 还原 VM 的槽位访问

Luraph 把被闭包捕获的局部变量存在一个表里，用数字下标访问：
    local RunService = game:GetService("RunService"); tbl[60] = RunService
    ...
    tbl[60].Heartbeat:Connect(...)        ← 读的是 RunService

只要 `tbl[N] = 名字` 是这个槽位在可见代码里**唯一**的一次赋值，后面读 `tbl[N]` 的地方
就可以标上那个名字。

What changed, and why
---------------------
The first version replaced text with a regular expression: it took the FIRST `tbl[N] = name`
it met and rewrote every later `tbl[N]`.  Two things were wrong with that:

  * it glued tokens: `if not tbl[5]then` became `if not R2then`, which does not parse;
  * it changed what the code means: a slot that is assigned again later (`tbl[4] = R; ...;
    tbl[4] = nil`) still had all its reads replaced by the first name, and
    `tbl[5] = R2 + 1` was read as "slot 5 is R2".  The slot table is also handed to
    virtualized closures (`W:kk(1, 2, {[1] = tbl, ...})`), which can write any slot unseen.

Now the slots come from the syntax tree (libs/luascope.py: slot_hints): a slot gets a name only
when the visible code assigns it exactly once, from a plain local that is itself never
assigned again.  By default the name is added as a COMMENT, so the code is untouched:

    tbl[60 --[[RunService]]].Heartbeat:Connect(...)

A comment must be true as well: it says "the only assignment to this slot that the visible
code makes is `tbl[60] = RunService`".  So a table gets slot names only when the visible code
has no second way to write it (luascope.slot_table_problem): it is declared `local tbl = {}`
and never assigned again, there is no write with a computed index (`tbl[i] = ...`), and the
table itself is not copied or passed on (`local alias = tbl`, `x.t = tbl`, `f(tbl)`,
`table.insert(tbl, v)`, `return tbl` all take the names away).  The one hand-over that is let
through is Luraph's own: `obj:method(1, 2, {[1] = tbl, ...})` with `obj` a parameter that is
never assigned (the VM object creating a closure and giving it the captured variables).
What such a closure writes is outside the visible code; the output's header says so.

`--inline` substitutes the name instead (the old look) where the visible code proves the read
can only see that value: the table is a `local tbl = {}` used only as `tbl[number]`, the
assignment runs once, the read comes after it in the same block, and the name is not shadowed
at the read (`do local RunService = 1; print(tbl[60]) end` keeps `tbl[60]`).  Where that proof
is not available the comment form is used.  The substituted text is parsed again and compared
with the input tree (every substituted name must be bound to the local that was assigned to
the slot); if that comparison fails, everything falls back to comments.

用法:
    python reslot.py 输入.luau [输出.luau] [--inline]
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (str(HERE / 'libs'), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _name_of(local):
    return local.new or local.name


def _usable(h, toks):
    """The hint is for the plain shape `T [ N ]`, with a name that can be written down and
    that is not shadowed at the read.  Returns the name or None."""
    a, b = h['range']
    if b - a != 4 or toks[a][0] != 'w' or (toks[a + 1][0], toks[a + 1][1]) != ('o', '[') \
            or toks[a + 2][0] != 'n' or (toks[a + 3][0], toks[a + 3][1]) != ('o', ']'):
        return None
    name = _name_of(h['local'])
    if not name.isidentifier() or not name.isascii() or h.get('shadowed'):
        return None
    return name


def apply_hints(toks, hints, inline=False):
    """Put slot names into a token list.

    `toks`: [(type, text), ...] of the text the hints were computed on (same indices);
    `hints`: luascope.slot_hints(analysis).
    Returns (new token list, stats, inlined) where stats = {'comment': n, 'inline': n,
    'slots': {(table name, N): slot name}} and inlined maps the AST location of every
    substituted read to the key of the local that replaced it (for luascope.alpha_equal)."""
    toks = [(t[0], t[1]) for t in toks]
    edits = {}           # first token index -> ('c' | 'i', end token index, name)
    stats = {'comment': 0, 'inline': 0, 'slots': {}}
    inlined = {}
    for h in hints:
        a, b = h['range']
        name = _usable(h, toks)
        if name is None:
            continue
        table = _name_of(h['table_local']) if h.get('table_local') is not None else h['table']
        stats['slots'][(table, h['slot'])] = name
        if inline and h['inline']:
            edits[a] = ('i', b, name)
            inlined[h['node']['location']] = h['local'].key
            stats['inline'] += 1
        else:
            edits[a] = ('c', b, name)
            stats['comment'] += 1
    out = []
    i, n = 0, len(toks)
    while i < n:
        e = edits.get(i)
        if e is None:
            out.append(toks[i])
            i += 1
        elif e[0] == 'i':
            out.append(('w', e[2]))
            i = e[1]
        else:
            out.extend(toks[i:i + 3])
            out.append(('c', '--[[' + e[2] + ']]'))
            out.append(toks[i + 3])
            i = e[1]
    return out, stats, inlined


def process_text(text, ast_bin=None, inline=False):
    """Annotate (or, with inline, substitute) the slot reads of a Luau source text.
    Returns (new text, stats).  The layout of the text is kept: names are spliced in at the
    exact offsets the parser reported, and the result is checked token by token."""
    import deobf_plus as dp
    import luascope
    if ast_bin is None:
        import luaubin
        ast_bin = luaubin.find_luau_ast()
        if not ast_bin:
            raise RuntimeError('找不到 luau-ast；' + luaubin.MISSING_HINT)
    root, errors = luascope.parse(text, ast_bin)
    if errors:
        raise RuntimeError('输入有语法错误，不处理: ' + errors[0])
    ptoks = dp.tokenize_pos(text)
    analysis = luascope.Analysis(root, luascope.LineIndex(text), ptoks)
    hints = luascope.slot_hints(analysis)
    want, stats, _inl = apply_hints(ptoks, hints, inline=inline)

    # splice into the text from the back so earlier offsets stay valid
    edits = []
    for h in hints:
        a, b = h['range']
        name = _usable(h, ptoks)
        if name is None:
            continue
        start = ptoks[a][2]
        end = ptoks[b - 1][2] + 1                       # one past `]`
        num_end = ptoks[a + 2][2] + len(ptoks[a + 2][1])
        if inline and h['inline']:
            left = ' ' if start > 0 and (text[start - 1].isalnum() or text[start - 1] == '_') else ''
            right = ' ' if end < len(text) and (text[end].isalnum() or text[end] == '_') else ''
            edits.append((start, end, left + name + right))
        else:
            edits.append((num_end, num_end, ' --[[' + name + ']]'))
    out = text
    for start, end, new in sorted(edits, reverse=True):
        out = out[:start] + new + out[end:]
    if not dp.same_tokens(dp.tokenize(out), want, comments=True):
        raise RuntimeError('内部校验失败：改写后的 token 序列与预期不符，未输出')
    if inline and stats['inline']:
        # a substituted name must be bound to the very local that was assigned to the slot:
        # parse the result and compare the two trees (the comment form needs no such check,
        # it leaves the code as it was)
        root2, errors2 = luascope.parse(out, ast_bin)
        ok, why = (False, '改写后有语法错误: ' + errors2[0]) if errors2 else \
            luascope.alpha_equal(root, root2, _inl)
        if not ok:
            out, stats = process_text(text, ast_bin=ast_bin, inline=False)
            stats['inline_rejected'] = why
            return out, stats
    return out, stats


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    inline = '--inline' in sys.argv
    if not args:
        print(__doc__)
        return 1
    src = Path(args[0])
    if not src.is_file():
        print(f"[✗] 文件不存在：{src}")
        return 1
    dst = Path(args[1]) if len(args) > 1 else src.with_suffix('.reslot.luau')
    from deobf_plus import read_lua
    text = read_lua(src)
    try:
        out, stats = process_text(text, inline=inline)
    except RuntimeError as e:
        print(f"[reslot] {e}")
        return 2

    slots = stats['slots']
    print(f"[reslot] 发现 {len(slots)} 个只赋值一次的槽位：")
    for (tab, idx), name in sorted(slots.items(), key=lambda x: (x[0][0], x[0][1]))[:20]:
        print(f"    {tab}[{idx}] = {name}")
    if len(slots) > 20:
        print(f"    … 其余 {len(slots) - 20} 个略")
    dst.write_bytes(out.encode('utf-8', errors='surrogateescape'))
    if stats.get('inline_rejected'):
        print("[reslot] --inline 的结果没有通过绑定校验（" + stats['inline_rejected'][:140]
              + "），已全部改用注释")
    print(f"[reslot] 注释标注 {stats['comment']:,} 处，直接替换 {stats['inline']:,} 处  →  {dst}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
