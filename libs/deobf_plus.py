#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Luraph v15.1 静态反混淆 · 增强版 (deobf_plus)

This is the ONE implementation of the static tool's lexer and formatter.  The file
deobf_plus.py in the repository root only forwards to this module (it used to be an older
copy that shadowed this one on sys.path).

  1. Luau 词法：0b/0x 前缀、下划线分隔符、长括号字符串/注释、\\z、插值字符串。
  2. 字符串转义解码：\\xNN / \\NNN / \\u{XXXX} / \\z 还原成明文，
     例如 math["\\x72\\x61\\110\\100\\x6f\\x6d"] -> math["random"]（语义等价、无损）。
  3. 常量算术折叠：((0b1100+0b1)+(0B101111-0b100001)) -> 27。只折叠 Luau 在任何机器上
     都算出同一个值的表达式（libs/luaunum.py），有 luau 时逐条交给解释器复核。
  4. handler 表语义标注：抽取 [key]=value 映射，写入代码注释并导出 handler_map.md。
  5. 变量语义重命名：作用域取自 luau-ast（libs/luascope.py），名字按初始化表达式的语法树取（libs/luanames.py：
     叫值是什么，而不是表达式里第一个方法的名字）。
  6. 排版只改空白：format_checked() 把结果重新词法化并与输入逐 token 比较。

只做静态等价变换，不执行任何被分析脚本。业务层源码需要动态执行 VM 解密桩才能拿到，
本工具不负责这一步。

用法: python3 deobf_plus.py <input.lua|.txt> -o <output.lua> [--no-rename] [--no-fold] [--map <map.md>]
"""
import sys
import re
import math
import argparse
from pathlib import Path

_LIBS = Path(__file__).resolve().parent
for _p in (str(_LIBS), str(_LIBS.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------- 词法分析
# Token types: w = identifier/keyword, n = number, s = string, o = operator/punctuation,
# c = comment.  The lexer follows Luau's rules where they decide what is ONE token
# (long brackets, `--` comments, greedy numbers, compound operators), because every later
# stage is checked by lexing its output again and comparing the two token lists.
#
# Numbers are Luau's Lexer::readNumber to the letter: a digit (or a dot before a digit), then
# digits / dots / underscores, an optional `e` with an optional sign, then every letter,
# digit and underscore that follows.  So `0x10`, `1e+5`, `1_000` and the integer literals of
# newer Luau (`5i`, `0x10i`) are one token each, exactly as Luau reads them; whether the
# token is a VALID number is Luau's business, not the formatter's.
# One exception, for text that is not Luau at all: a hexadecimal float of Lua 5.2+ / LuaJIT
# (`0x1.c8P5`, `0x.8p-1`, `0x1p+4`) is ONE token.  Luau would cut it into `0x1` `.` `c8P5`,
# and the formatter then printed `1 .c8P5`: another program for the Lua the file was written
# for (and a syntax error in Luau either way, so no Luau text reads differently for it: a
# number literal cannot be indexed, and `0x1..s` keeps its `..`).
_TOKEN_RE = re.compile(r'''
  (?P<ws>[ \t\r\n\f\v]+)
 |(?P<c>--(?:\[(?P<ceq>=*)\[[\s\S]*?(?:\](?P=ceq)\]|\Z)|[^\n]*))
 |(?P<s>\[(?P<seq>=*)\[[\s\S]*?(?:\](?P=seq)\]|\Z)
       |"(?:[^"\\\n]|\\z[ \t\r\n\f\v]*|\\\r\n|\\[\s\S])*(?:"|(?=\n)|\Z)
       |'(?:[^'\\\n]|\\z[ \t\r\n\f\v]*|\\\r\n|\\[\s\S])*(?:'|(?=\n)|\Z)
       |`(?:[^`\\]|\\[\s\S])*(?:`|\Z))
 |(?P<n>0[xX][0-9a-fA-F_]*\.(?!\.)[0-9a-fA-F_]*(?:[pP][+-]?[0-9_]+)?[A-Za-z0-9_]*
       |0[xX][0-9a-fA-F_]+[pP][+-][0-9_]+[A-Za-z0-9_]*
       |(?:[0-9]|\.(?=[0-9]))[0-9._]*(?:[eE][+-]?)?[A-Za-z0-9_]*)
 |(?P<w>[A-Za-z_][A-Za-z0-9_]*)
 |(?P<o>\.\.\.|\.\.=|//=|==|~=|<=|>=|\.\.|::|->|//|\+=|-=|\*=|/=|%=|\^=|[\s\S])
''', re.X)

OPS = ('...', '..=', '//=', '==', '~=', '<=', '>=', '..', '::', '->', '//',
       '+=', '-=', '*=', '/=', '%=', '^=')


def _interp_end(s, i):
    """s[i] is the backtick that opens an interpolated string; returns the offset just after
    the backtick that closes it.  The `{expression}` parts are skipped token by token, so a
    brace or a backtick inside a nested string or comment, and nested interpolated strings,
    do not end it early.  (Luau's lexer cuts such a string into begin / mid / end pieces
    around the expressions; here the whole string is ONE token that no later stage looks
    into, which is the safe reading for a tool that only moves whitespace.)"""
    n = len(s)
    i += 1
    while i < n:
        ch = s[i]
        if ch == '\\':
            i += 2
        elif ch == '`':
            return i + 1
        elif ch == '{':
            i += 1
            depth = 0
            while i < n:
                if s[i] == '`':
                    i = _interp_end(s, i)
                    continue
                m = _TOKEN_RE.match(s, i)
                if m is None:
                    return n
                if m.lastgroup == 'o':
                    v = m.group()
                    if v == '{':
                        depth += 1
                    elif v == '}':
                        if depth == 0:
                            i = m.end()
                            break
                        depth -= 1
                i = m.end()
        else:
            i += 1
    return n


def tokenize_pos(s):
    """[(type, text, start offset)] for every token of `s` (whitespace dropped)."""
    out = []
    add = out.append
    pos = 0
    if s.startswith('#!'):                   # a shebang line: Luau skips it like a comment
        end = s.find('\n')
        end = len(s) if end < 0 else end
        add(('c', s[:end], 0))
        pos = end
    if '`' not in s:
        for m in _TOKEN_RE.finditer(s, pos):
            k = m.lastgroup
            if k == 'ws':
                continue
            if k in ('ceq', 'seq'):          # the long-bracket level groups close last
                k = 'c' if m.group('c') is not None else 's'
            add((k, m.group(k), m.start()))
        return out
    # the text has backticks: interpolated strings need the scanner above
    match = _TOKEN_RE.match
    n = len(s)
    while pos < n:
        if s[pos] == '`':
            end = _interp_end(s, pos)
            add(('s', s[pos:end], pos))
            pos = end
            continue
        m = match(s, pos)
        if m is None:
            break
        k = m.lastgroup
        if k in ('ceq', 'seq'):
            k = 'c' if m.group('c') is not None else 's'
        if k != 'ws':
            add((k, m.group(k), pos))
        pos = m.end()
    return out


def tokenize(s):
    """返回 [(type, text)]，type ∈ w(标识符) n(数字) s(字符串) o(运算符/标点) c(注释)"""
    return [(t, v) for t, v, _ in tokenize_pos(s)]


def same_tokens(a, b, comments=False):
    """True when two token lists are the same program text apart from whitespace
    (and, unless `comments`, apart from comments)."""
    if not comments:
        a = [(x[0], x[1]) for x in a if x[0] != 'c']
        b = [(x[0], x[1]) for x in b if x[0] != 'c']
    else:
        a = [(x[0], x[1]) for x in a]
        b = [(x[0], x[1]) for x in b]
    return a == b


def first_token_difference(a, b):
    """(index, token of a, token of b) of the first difference between two token lists
    (comments ignored), or None."""
    a = [(x[0], x[1]) for x in a if x[0] != 'c']
    b = [(x[0], x[1]) for x in b if x[0] != 'c']
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else None
        y = b[i] if i < len(b) else None
        if x != y:
            return i, x, y
    return None


# ---------------------------------------------------------------- 数字归一化
def parse_num(lit):
    t = lit.replace('_', '')
    try:
        if t[:2].lower() == '0x':
            return int(t, 16)
        if t[:2].lower() == '0b':
            return int(t, 2)
        if re.fullmatch(r'[+-]?\d+', t):
            return int(t)
        return float(t)
    except ValueError:
        return None


def norm_num(lit):
    """Decimal spelling of a number literal with the same value, or the literal itself when
    there is none (1e999 is infinity: repr() would print `inf`, an identifier)."""
    v = parse_num(lit)
    if v is None:
        return lit
    if isinstance(v, int):
        if v >= 2 ** 64:
            return lit                # Luau wraps or rejects these; keep what was written
        return str(v)
    if not math.isfinite(v):
        return lit
    return repr(v)


def num_value(lit):
    """The double a number literal stands for (None when it cannot be parsed)."""
    v = parse_num(lit)
    if v is None:
        return None
    try:
        return float(v)
    except OverflowError:
        return float('inf')


# ---------------------------------------------------------------- 字符串解码
ESC_SIMPLE = {
    'a': 7, 'b': 8, 'f': 12, 'n': 10, 'r': 13, 't': 9, 'v': 11,
    '\\': 92, '"': 34, "'": 39, '[': 91, ']': 93,
}


def decode_string(lit):
    """把 Lua 短字符串解码成 bytes；失败返回 None。"""
    if len(lit) < 2 or lit[0] not in '"\'' or lit[-1] != lit[0]:
        return None
    body = lit[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != '\\':
            out.extend(ch.encode('utf-8', 'surrogateescape'))
            i += 1
            continue
        i += 1
        if i >= len(body):
            return None
        e = body[i]
        if e == 'x':
            if i + 2 >= len(body) or not re.fullmatch(r'[0-9a-fA-F]{2}', body[i + 1:i + 3]):
                return None
            out.append(int(body[i + 1:i + 3], 16))
            i += 3
        elif e == 'u' and i + 1 < len(body) and body[i + 1] == '{':
            k = body.find('}', i + 2)
            if k < 0 or not re.fullmatch(r'[0-9a-fA-F]+', body[i + 2:k]):
                return None
            try:
                out.extend(chr(int(body[i + 2:k], 16)).encode('utf-8'))
            except ValueError:
                return None
            i = k + 1
        elif e == 'z':
            i += 1
            while i < len(body) and body[i] in ' \t\r\n':
                i += 1
        elif e.isdigit():
            m = re.match(r'\d{1,3}', body[i:])
            if not m:
                return None
            out.append(int(m.group()) & 0xFF)
            i += len(m.group())
        elif e in ESC_SIMPLE:
            out.append(ESC_SIMPLE[e])
            i += 1
        else:
            return None
    return bytes(out)


def reencode_string(lit):
    """解码后若全部为可打印 ASCII，则改写成明文短字符串（等价变换）。"""
    raw = decode_string(lit)
    if raw is None or not raw:
        return lit
    if any(b < 0x20 or b > 0x7E for b in raw):
        return lit  # 含二进制 payload，保留原转义以免破坏数据
    txt = raw.decode('ascii')
    if '"' not in txt and '\\' not in txt:
        return '"' + txt + '"'
    if "'" not in txt and '\\' not in txt:
        return "'" + txt + "'"
    # 含引号或反斜杠：重新转义
    esc = txt.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
    return '"' + esc + '"'


# ---------------------------------------------------------------- 常量折叠
# tokens after which a run of numbers and arithmetic operators is a complete operand:
# folding `1 + 2` in `x - 1 + 2` or in `x * 1 + 2` would change the result
_FOLD_AFTER_OPS = frozenset(('(', '[', '{', ',', ';', '=', '==', '~=', '<', '<=', '>', '>=', '..',
                             '+=', '-=', '*=', '/=', '//=', '%=', '^=', '..='))
_FOLD_AFTER_WORDS = frozenset(('and', 'or', 'return', 'if', 'elseif', 'while', 'until', 'then',
                               'else', 'in', 'do'))
# tokens that must not follow a folded run: a higher-priority operator, or something that
# applies to the parenthesised value (`(1 + 2):f()`, `(1 + 2)"s"`)
_FOLD_BEFORE_BAD = frozenset(('^', '*', '/', '//', '%', '.', ':', '[', '(', '{'))
_FOLD_OPS = frozenset(('+', '-', '*', '/', '//', '%', '^', '(', ')'))
_FOLD_MAX_RUN = 512     # tokens; a longer run of numbers and operators is left alone


def _fold_literal(lit):
    """Double of a number literal as Luau reads it, or None when Luau rejects or truncates it
    (hexadecimal / binary literals of 2^64 and more)."""
    v = parse_num(lit)
    if v is None:
        return None
    if isinstance(v, int) and v >= 2 ** 64 and lit[:2].lower() in ('0x', '0b'):
        return None
    try:
        return float(v)
    except OverflowError:
        return None


def fold_consts(toks, report=None):
    """把连续「纯数字+算术运算符」（可含括号/一元负号）段折叠为一个数字 token。

    A run is folded only when
      (1) it is one complete operand: what stands in front of it is not an operator that binds
          tighter, and what follows does not continue it;
      (2) every operation in it has the same result in every Luau (libs/luaunum.py: IEEE
          `+ - * /`, `//` = floor(a / b), `%` = a - floor(a / b) * b and `^` only when exact;
          nothing that leaves the finite numbers);
      (3) the result is an integer in 0 .. 2^53-1 and not negative zero: such a value has one
          decimal spelling that is a single token.  (-1 would lex as two tokens, and `0` for
          -0 changes `1 / x` and `x .. ""`.)
    Everything else is left exactly as written.

    `report`: a list that receives (first token index in `toks`, end index, folded literal)
    for every fold, so a caller can check the folds against the interpreter."""
    import luaunum

    def can_start(i):
        if i == 0:
            return True
        pt, pv = toks[i - 1]
        if pt == 'o':
            return pv in _FOLD_AFTER_OPS
        if pt == 'w':
            return pv in _FOLD_AFTER_WORDS
        return False

    out = []
    i, n = 0, len(toks)
    while i < n:
        t, v = toks[i]
        is_start = (t == 'n' or (t == 'o' and v in ('(', '-'))) and can_start(i)
        if not is_start:
            out.append(toks[i])
            i += 1
            continue
        j = i
        depth = 0
        limit = i + _FOLD_MAX_RUN       # keeps the scan linear on a very long chain
        while j < n and j < limit and (toks[j][0] == 'n' or
                                       (toks[j][0] == 'o' and toks[j][1] in _FOLD_OPS)):
            if toks[j][1] == '(':
                depth += 1
            elif toks[j][1] == ')':
                if depth == 0:
                    break             # closes a bracket opened before the run
                depth -= 1
            j += 1
        seg = toks[i:j]
        folded = None
        nxt = toks[j] if j < n else (None, None)
        if (len(seg) >= 3 and depth == 0 and j < limit
                and nxt[1] not in _FOLD_BEFORE_BAD and nxt[0] != 's'
                and any(tt == 'o' and vv not in ('(', ')') for tt, vv in seg)):
            try:
                val = luaunum.eval_tokens(seg, _fold_literal)
            except luaunum.Unfoldable:
                val = None
            if (val is not None and math.isfinite(val) and val == int(val)
                    and 0 <= val < 2 ** 53 and not luaunum.is_negative_zero(val)):
                folded = str(int(val))
        if folded is None:
            out.append(toks[i])
            i += 1
            continue
        if report is not None:
            report.append((i, j, folded))
        out.append(('n', folded))
        i = j
    return out


def check_folds(toks, report, luau_bin, timeout=120):
    """Ask the interpreter: is every folded run equal to the literal that replaced it (the sign
    of zero included)?  Only number literals, arithmetic operators and parentheses are run.

    Returns (number of distinct folds checked, [(expression text, folded literal), ...] that
    the interpreter computes differently)."""
    import os
    import subprocess
    import tempfile
    cases = []
    seen = set()
    for i, j, lit in report:
        seg = toks[i:j]
        if not all(t == 'n' or (t == 'o' and v in _FOLD_OPS) for t, v in seg):
            raise ValueError('fold report does not match the token list')
        expr = ' '.join(v for _t, v in seg)
        if (expr, lit) not in seen:
            seen.add((expr, lit))
            cases.append((expr, lit))
    if not cases:
        return 0, []
    lines = ['local function same(a, b) return a == b and 1 / a == 1 / b end']
    for start in range(0, len(cases), 100):          # small functions: no register limits
        lines.append('do')
        for k in range(start, min(start + 100, len(cases))):
            expr, lit = cases[k]
            lines.append('  if not same(%s, %s) then print("FOLDBAD %d") end' % (expr, lit, k))
        lines.append('end')
    lines.append('print("FOLDDONE %d")' % len(cases))
    fd, path = tempfile.mkstemp(prefix='lv151_fold_', suffix='.lua')
    try:
        with os.fdopen(fd, 'w', encoding='ascii') as fh:
            fh.write('\n'.join(lines) + '\n')
        r = subprocess.run([str(luau_bin), path], capture_output=True, timeout=timeout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    text = r.stdout.decode('utf-8', 'replace')
    if ('FOLDDONE %d' % len(cases)) not in text:
        raise RuntimeError('luau did not run the fold check: '
                           + (text + r.stderr.decode('utf-8', 'replace')).strip()[:200])
    bad = [cases[int(m)] for m in re.findall(r'FOLDBAD (\d+)', text)]
    return len(cases), bad


# ---------------------------------------------------------------- handler 表解析
def find_table_body(toks, start):
    """从 start 处的 '{' 找到配对 '}'，返回 (body_start, body_end)。"""
    depth = 0
    for k in range(start, len(toks)):
        t, v = toks[k]
        if t == 'o' and v in ('(', '[', '{'):
            depth += 1
        elif t == 'o' and v in (')', ']', '}'):
            depth -= 1
            if depth == 0:
                return start + 1, k
    return start + 1, len(toks)


NUM_KEY_RE = re.compile(r'^(?:0[xXbB][0-9a-fA-F_]+|[0-9][0-9_]*)$')


def extract_handlers(toks, body_start, body_end):
    """抽取顶层 [数字键] = value 元素，返回 [(key_int, key_raw, value_tokens, elem_start_idx)]

    The table is split at EVERY top-level `,` / `;`, so the value of `[1] = string.byte`
    ends there.  (It used to end only at the next `[number] =` entry, and a named field that
    followed, `c = function...`, was glued onto the value before it.)"""
    bounds = [body_start]
    depth = 0
    for k in range(body_start, body_end):
        t, v = toks[k]
        if t != 'o':
            continue
        if v in ('(', '[', '{'):
            depth += 1
        elif v in (')', ']', '}'):
            depth -= 1
        elif v in (',', ';') and depth == 0:
            bounds.append(k + 1)
    elems = []
    for bi, st in enumerate(bounds):
        en = bounds[bi + 1] - 1 if bi + 1 < len(bounds) else body_end  # 减去分隔符
        while st < en and toks[st][0] == 'c':
            st += 1
        if st + 3 > en:
            continue
        if not (toks[st] == ('o', '[') and toks[st + 1][0] == 'n' and
                NUM_KEY_RE.fullmatch(toks[st + 1][1]) and toks[st + 2] == ('o', ']')):
            continue
        key_raw = toks[st + 1][1]
        key_int = parse_num(key_raw)
        if key_int is None:
            continue
        # 跳过 '='
        p = st + 3
        if p < en and toks[p] == ('o', '='):
            p += 1
        elems.append((key_int, key_raw, toks[p:en], st))
    return elems


def describe_value(vt):
    """把 handler 值概括成可读描述。"""
    v = [x for x in vt if x[0] != 'c']
    if not v:
        return '(空)'
    # 库["成员"] 形式
    if len(v) >= 4 and v[0][0] == 'w' and v[1] == ('o', '[') and v[2][0] == 's' and v[3] == ('o', ']'):
        raw = decode_string(v[2][1])
        member = raw.decode('ascii', 'replace') if raw and all(0x20 <= b <= 0x7E for b in raw) else v[2][1]
        return f'{v[0][1]}.{member}'
    if len(v) == 1 and v[0][0] == 'w':
        return v[0][1]
    if v[0][0] == 'w' and v[0][1] == 'function':
        # 参数个数
        depth, cnt = 0, 0
        for t, x in v[1:]:
            if t == 'o' and x == '(':
                depth += 1
                continue
            if t == 'o' and x == ')':
                break
            if depth == 1 and t == 'w':
                cnt += 1
        return f'function({cnt} 参数) …'
    if all(t == 'n' for t, _ in v):
        return '常量 ' + ' '.join(norm_num(x) for _, x in v)
    # 折叠后的常量表达式
    expr = ''.join(norm_num(x) if t == 'n' else x for t, x in v[:12])
    if len(expr) > 90:
        expr = expr[:90] + '…'
    return expr or '(复合表达式)'


# ---------------------------------------------------------------- 排版
# The formatter only decides WHITESPACE between tokens: it never drops, adds, reorders or
# rewrites a token (semicolons included).  Two things in Luau depend on whitespace and are
# respected here:
#   * tokens that would lex differently when glued (`R2` `then`, `-` `-`, `[` `[[x]]`,
#     `1` `..`): see _must_space;
#   * a `(` that starts a line after an expression is an "ambiguous syntax" error, so a line
#     is only ever broken after `;`, `,`, a binary operator or a block keyword, never
#     before a call's `(`.
# format_checked() lexes the result again and compares it with the input tokens.
KEYWORDS = frozenset((
    'and', 'break', 'do', 'else', 'elseif', 'end', 'false', 'for', 'function', 'if', 'in',
    'local', 'nil', 'not', 'or', 'repeat', 'return', 'then', 'true', 'until', 'while',
))
BLOCK_OPEN = {'function', 'then', 'do', 'repeat'}
BLOCK_CLOSE = {'end', 'until'}
STMT_START = {'local', 'return', 'if', 'for', 'while', 'break', 'continue'}
_VALUE_KW = frozenset(('end', 'nil', 'true', 'false'))      # keywords that end an expression
_BINOPS = frozenset((
    '+', '-', '*', '/', '//', '%', '^', '..', '==', '~=', '<', '<=', '>', '>=', '=',
    '+=', '-=', '*=', '/=', '//=', '%=', '^=', '..=', '->', '::', '|', '&',
))
_OPENERS = {'(': 'p', '[': 'b', '{': 't'}
_INDENTING = frozenset(('func', 'if', 'block', 'repeat'))
_LONG_OPEN = re.compile(r'--\[(=*)\[')
MAX_INDENT = 16
_pair_cache = {}


def _is_word_char(ch):
    return ch == '_' or ('0' <= ch <= '9') or ('a' <= ch <= 'z') or ('A' <= ch <= 'Z')


def _must_space(ta, va, tb, vb):
    """True when the two tokens cannot be written next to each other without changing how
    Luau lexes them."""
    if ta == 'c' or tb == 'c':
        return True
    x, y = va[-1], vb[0]
    if _is_word_char(x) and _is_word_char(y):
        return True
    if ta == 'n' and y == '.':
        return True                       # Luau numbers swallow every following dot
    if x == '.' and (y == '.' or '0' <= y <= '9'):
        return True
    if ta == 'o' and tb == 'o':
        key = (va, vb)
        ok = _pair_cache.get(key)
        if ok is None:
            ok = tokenize(va + vb) == [('o', va), ('o', vb)]
            _pair_cache[key] = ok
        return not ok
    if x == '[' and y == '[':
        return True                       # `[` + `[[long string]]`
    if x == '-' and y == '-':
        return True
    return False


def _block_comment(v):
    """True for a closed --[[ ]] comment (one that does not swallow the rest of its line)."""
    m = _LONG_OPEN.match(v)
    if not m:
        return False
    close = ']' + m.group(1) + ']'
    return v.endswith(close) and len(v) >= len(m.group(0)) + len(close)


def format_tokens(toks, max_len=100, wrap=True, blank_lines=False):
    """Lay out a token list as indented, line-broken Luau.  Whitespace only.

    wrap         break lines longer than max_len after `,` / binary operators
    blank_lines  put an empty line between top-level functions"""
    toks = [(t[0], t[1]) for t in toks]
    n = len(toks)

    # ---- which table constructors get one field per line
    multiline = {}
    plen = [0] * (n + 1)
    pfn = [0] * (n + 1)
    for i, (t, v) in enumerate(toks):
        plen[i + 1] = plen[i] + len(v) + 1
        pfn[i + 1] = pfn[i] + (1 if (t == 'w' and v == 'function') or
                               (t == 'c' and not _block_comment(v)) else 0)
    opens = []
    for i, (t, v) in enumerate(toks):
        if t != 'o':
            continue
        if v in _OPENERS:
            opens.append(i)
        elif v in (')', ']', '}') and opens:
            j = opens.pop()
            if toks[j][1] == '{' and v == '}' and i > j + 1:
                multiline[j] = (plen[i] - plen[j] > 60) or (pfn[i] - pfn[j] > 0)
    nextcode = [None] * n               # index of the next non-comment token
    nx = None
    for i in range(n - 1, -1, -1):
        nextcode[i] = nx
        if toks[i][0] != 'c':
            nx = i

    lines = []              # (indent, [[text, space_before, break_after, depth], ...])
    cur = []
    cur_indent = [0]
    stack = []              # [kind, flag]
    level = [0]

    def newline():
        nonlocal cur
        if cur:
            lines.append((cur_indent[0], cur))
            cur = []

    def emit(text, space, brk=False, indent=None):
        if not cur:
            cur_indent[0] = level[0] if indent is None else indent
            space = False
        cur.append([text, space, brk, len(stack)])

    def push(kind, flag=False):
        stack.append([kind, flag])
        if kind in _INDENTING or (kind == 't' and flag):
            level[0] += 1

    def pop_to(kinds):
        """Close everything down to the innermost entry of one of `kinds`."""
        for k in range(len(stack) - 1, -1, -1):
            if stack[k][0] in kinds:
                found = stack[k]
                for kind, flag in stack[k:]:
                    if kind in _INDENTING or (kind == 't' and flag):
                        level[0] -= 1
                del stack[k:]
                return found
        return None

    def innermost(kinds):
        for k in range(len(stack) - 1, -1, -1):
            if stack[k][0] in kinds:
                return stack[k]
        return None

    prev = None             # previous code token
    prev_unary = False
    prev_ifx = False        # previous token was `then`/`else`/`elseif` of an if-EXPRESSION
    last_typ = None         # type of the last piece put on the current line
    need_break = False
    soft_break = False      # after `end`: break unless the next token continues the expression
    fn_pending = False

    for i, (typ, v) in enumerate(toks):
        # ------------------------------------------------------------ comments
        if typ == 'c':
            if need_break or soft_break:
                newline()
                need_break = soft_break = False
            emit(v, True)
            last_typ = 'c'
            if not _block_comment(v):
                newline()                # a line comment ends its line
            continue

        kw = v if (typ == 'w' and v in KEYWORDS) else None
        top = stack[-1][0] if stack else None

        # ------------------------------------------------------------ pending breaks
        if need_break:
            newline()
            need_break = soft_break = False
        elif soft_break:
            glue = (typ == 'o') or kw in ('and', 'or', 'then', 'do')
            if not glue:
                newline()
            soft_break = False

        # ------------------------------------------------------------ unary minus
        unary = False
        if typ == 'o' and v in ('-', '#'):
            if v == '#':
                unary = True
            elif prev is None:
                unary = True
            elif prev[0] == 'o':
                unary = prev[1] not in (')', ']', '}', '...')
            elif prev[0] == 'w':
                unary = prev[1] in KEYWORDS and prev[1] not in _VALUE_KW

        # ------------------------------------------------------------ structure
        line_indent = None
        after_break = False
        is_ifx_kw = False
        if kw == 'end':
            pop_to(_INDENTING | {'head'})
            newline()
        elif kw == 'until':
            pop_to(('repeat',))
            newline()
        elif kw in ('else', 'elseif', 'then'):
            ent = innermost(('if', 'ifx'))
            if ent is not None and ent[0] == 'if':
                if kw != 'then':
                    # close anything left open inside the previous branch
                    while stack and stack[-1] is not ent:
                        pop_to((stack[-1][0],))
                    newline()
                    line_indent = level[0] - 1
                if kw != 'elseif':
                    after_break = True
            elif ent is not None:
                is_ifx_kw = True
                if kw == 'else':
                    pop_to(('ifx',))
        elif kw == 'if':
            if prev is None:
                stmt = True
            elif prev[0] == 'o':
                stmt = prev[1] in (';', ')', ']', '}')
            elif prev[0] == 'w' and prev[1] in KEYWORDS:
                stmt = (prev[1] in ('then', 'else', 'do', 'repeat', 'end', 'nil', 'true',
                                    'false', 'break') and not prev_ifx)
            else:
                stmt = True
            if stmt:
                newline()
        elif kw in ('while', 'for', 'repeat', 'local', 'return', 'break'):
            newline()
        elif kw == 'do':
            if top != 'head':
                newline()            # a `do ... end` block of its own
        elif kw == 'function':
            nx = nextcode[i]
            named = nx is not None and toks[nx][0] == 'w' and toks[nx][1] not in KEYWORDS
            if named and not (prev is not None and prev == ('w', 'local')):
                newline()
        elif typ == 'w' and kw is None:
            # A name right after something that ends an expression can only be the start of
            # the next statement (`f() g()`, `x = 1 y = 2`): Lua has no other place where two
            # such tokens meet, except Luau's `export type Name`.
            if cur and prev is not None and top not in ('p', 'b', 't'):
                pt, pv = prev
                ends = (pt in ('n', 's')
                        or (pt == 'w' and (pv not in KEYWORDS or pv in _VALUE_KW)
                            and pv not in ('export', 'type'))
                        or (pt == 'o' and pv in (')', ']', '}', '...')))
                if ends:
                    newline()
        elif typ == 'o' and v == '}':
            ent = pop_to(('t',))
            if ent is not None and ent[1]:
                newline()
        elif typ == 'o' and v in (')', ']'):
            ent = pop_to(('p',) if v == ')' else ('b',))
            if ent is not None and v == ')' and ent[1]:
                after_break = True       # end of a function's parameter list

        # ------------------------------------------------------------ spacing
        if not cur:
            space = False
        elif last_typ == 'c':
            # after an inline --[[ ]] comment: closers and separators stay attached
            space = not (typ == 'o' and v in (',', ';', ')', ']', '}'))
        else:
            ta, va = prev if prev is not None else ('c', '--')
            if typ == 'o' and not unary and v != '...':
                if v in (',', ';', ')', ']', '}', '.', ':'):
                    space = False
                elif v in ('(', '['):
                    if ta == 'w':
                        space = va in KEYWORDS and va != 'function'
                    elif ta in ('s', 'n'):
                        space = False
                    elif va in (')', ']', '}', '(', '[', '{', '.', ':', '...'):
                        space = False
                    elif prev_unary:
                        space = False
                    else:
                        space = va in _BINOPS or va in (',', ';')
                elif v == '{':
                    space = not (va in ('(', '[', '{') or prev_unary)
                elif v in _BINOPS:
                    space = True
                else:
                    space = False        # other punctuation (`@`, `?`, ...) stays attached
            else:                        # a name, keyword, number, string, `...` or unary op
                if ta in ('w', 'n', 's'):
                    space = True
                elif va in ('(', '[', '{', '.', ':'):
                    space = False
                elif prev_unary:
                    space = False
                elif va in (')', ']', '}', '...', ',', ';') or va in _BINOPS:
                    space = True
                else:
                    space = False
            if not space and _must_space(ta, va, typ, v):
                space = True

        brk = False
        if typ == 'o' and not unary:
            brk = v == ',' or (v in _BINOPS and v not in ('=', '::', '->'))
        elif kw in ('and', 'or'):
            brk = True
        emit(v, space, brk, line_indent)
        last_typ = typ

        # ------------------------------------------------------------ after the token
        if kw == 'end':
            soft_break = True
        elif kw == 'if':
            if stmt:
                push('if')
            else:
                push('ifx')
        elif kw in ('while', 'for'):
            push('head')
        elif kw == 'repeat':
            push('repeat')
            need_break = True
        elif kw == 'do':
            if top == 'head':
                pop_to(('head',))
            push('block')
            need_break = True
        elif kw == 'function':
            fn_pending = True
        elif kw in ('then', 'else'):
            if after_break:
                need_break = True
        elif typ == 'o':
            if v in _OPENERS:
                if v == '(':
                    push('p', fn_pending)
                    fn_pending = False
                elif v == '{':
                    ml = multiline.get(i, False)
                    push('t', ml)
                    if ml:
                        need_break = True
                else:
                    push('b')
            elif v == ')' and after_break:
                push('func')
                need_break = True
            elif v in (',', ';'):
                if top == 't':
                    if stack[-1][1]:
                        need_break = True
                elif v == ';' and top not in ('p', 'b'):
                    need_break = True

        prev = (typ, v)
        prev_unary = unary
        prev_ifx = is_ifx_kw

    newline()

    # ---- render, wrapping long lines after `,` / binary operators
    def text_of(seg):
        return ''.join((' ' if (sp and q > 0) else '') + tx
                       for q, (tx, sp, _b, _d) in enumerate(seg))

    out = []
    prev_end = False
    for indent, pieces in lines:
        ind = min(max(indent, 0), MAX_INDENT)
        if blank_lines:
            words = [p[0] for p in pieces[:3]]
            starts_fn = bool(words) and (words[0] == 'function' or
                                         (words[0] == 'local' and len(words) > 1))
            if prev_end and starts_fn and indent <= 1 and out:
                out.append('')
            prev_end = indent <= 1 and [p[0] for p in pieces] in (['end'], ['end', ';'])
        if not wrap:
            out.append('    ' * ind + text_of(pieces))
            continue
        start = 0
        while True:
            pad = '    ' * (ind if start == 0 else ind + 1)
            width = len(pad)
            cands = []                   # (piece index, width after it, bracket depth)
            overflow = False
            for k in range(start, len(pieces)):
                text, space, brk, depth = pieces[k]
                w = len(text) + (1 if (space and k > start) else 0)
                if width + w > max_len and cands:
                    overflow = True
                    break
                width += w
                if brk and k + 1 < len(pieces) and width >= max_len * 0.4:
                    cands.append((k, width, depth))
            if not overflow:
                out.append(pad + text_of(pieces[start:]))
                break
            shallow = min(c[2] for c in cands)
            cut = max(c[0] for c in cands if c[2] == shallow)
            out.append(pad + text_of(pieces[start:cut + 1]))
            start = cut + 1
    return '\n'.join(out)


def safe_join(toks):
    """Fallback layout that cannot glue anything: one space between tokens, a new line
    after every `;` and every comment."""
    out, cur = [], []
    for t in toks:
        typ, v = t[0], t[1]
        cur.append(v)
        if typ == 'c' or (typ == 'o' and v == ';'):
            out.append(' '.join(cur))
            cur = []
    if cur:
        out.append(' '.join(cur))
    return '\n'.join(out)


def format_checked(toks, max_len=100, wrap=True, blank_lines=False, info=None):
    """format_tokens + proof: the result lexes back to exactly `toks` (comments included).
    Returns (text, ok).

    When the indented layout raises or fails that check, the fallback layout (safe_join: one
    statement per line, single spaces) is returned instead; ok is False only if even that one
    does not round-trip.  `info`, a dict, is then told so: info['fallback'] = True and
    info['reason'] = why the indented layout was rejected.  A caller that reports "layout
    checked" should also report the fallback, the two outputs look very different."""
    toks = [(t[0], t[1]) for t in toks]
    reason = None
    try:
        text = format_tokens(toks, max_len=max_len, wrap=wrap, blank_lines=blank_lines)
        back = tokenize(text)
        if same_tokens(back, toks, comments=True):
            if info is not None:
                info['fallback'] = False
                info['reason'] = None
            return text, True
        diff = None
        for k in range(max(len(back), len(toks))):
            x = back[k] if k < len(back) else None
            y = toks[k] if k < len(toks) else None
            if x != y:
                diff = (k, y, x)
                break
        reason = 'token %d: %r came back as %r' % diff if diff else 'token lists differ'
    except Exception as e:                      # noqa: BLE001  reported through `info`
        reason = '%s: %s' % (type(e).__name__, e)
    text = safe_join(toks)
    if info is not None:
        info['fallback'] = True
        info['reason'] = reason
    return text, same_tokens(tokenize(text), toks, comments=True)


# ---------------------------------------------------------------- 等价性
def canon(tok):
    """Value of a token: numbers by the double they denote, short strings by their bytes.
    Two token lists with equal canon() values are the same program text up to the spelling
    of literals (0x10 / 16, "\65" / "A")."""
    t, v = tok[0], tok[1]
    if t == 'n':
        x = num_value(v)
        return ('n', v) if x is None else ('n', x)
    if t == 's':
        raw = decode_string(v)
        return ('s', v) if raw is None else ('s', raw)
    return (t, v)


def same_values(a, b):
    """same_tokens, but literals are compared by value."""
    a = [canon(x) for x in a if x[0] != 'c']
    b = [canon(x) for x in b if x[0] != 'c']
    return a == b


def read_lua(path):
    """A Lua file as text for the tools that lex, format or parse it: every byte survives
    (invalid UTF-8 as surrogate escapes), except a UTF-8 byte order mark in front of the
    file, which is dropped (Luau's parser takes it for a syntax error; it is an artefact of
    the editor that saved the file, not part of the script)."""
    text = Path(path).read_bytes().decode('utf-8', errors='surrogateescape')
    return text[1:] if text.startswith('\ufeff') else text


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description='Luraph v15.1 静态反混淆增强版')
    ap.add_argument('input')
    ap.add_argument('-o', '--output', required=True)
    ap.add_argument('--map', default=None, help='导出 handler 映射表 (markdown)')
    ap.add_argument('--no-rename', action='store_true', help='不做变量语义重命名')
    ap.add_argument('--no-fold', action='store_true', help='不做常量折叠')
    args = ap.parse_args()

    src = read_lua(args.input)
    ptoks = tokenize_pos(src)
    toks = [(t, v) for t, v, _ in ptoks]
    print(f'[1] 词法: {len(toks):,} tokens')

    import luaubin
    import luascope
    ast_bin = luaubin.find_luau_ast()
    luau_bin = luaubin.find_luau()
    root = None
    if ast_bin:
        try:
            root, errors = luascope.parse(src, ast_bin)
            if errors:
                print(f'[!] 输入有语法错误（{errors[0][:100]}）：不重命名，不做语法树复核')
                root = None
        except Exception as e:                  # noqa: BLE001
            print(f'[!] luau-ast 解析失败（{e}）：不重命名，不做语法树复核')
            root = None
    else:
        print('[!] 找不到 luau-ast：不重命名，不做语法树复核。' + luaubin.MISSING_HINT)

    # 变量重命名：作用域来自 luau-ast，名字取自初始化表达式的语法树（libs/luanames.py）
    renamed = False
    if args.no_rename:
        print('[2] 按参数跳过重命名')
    elif root is None:
        print('[2] 重命名跳过（没有语法树）')
    else:
        try:
            analysis = luascope.Analysis(root, luascope.LineIndex(src), ptoks)
            ren = luascope.Renamer(analysis)
            cand = ren.apply(toks)
            # 校验：重命名后的文本重新解析，语法树必须与原文一致（只差局部变量名）
            root2, errors2 = luascope.parse(format_tokens(cand, wrap=False), ast_bin)
            same, why = (False, errors2[0]) if errors2 else luascope.alpha_equal(root, root2)
            if not same:
                raise RuntimeError('重命名结果未通过绑定校验（' + why[:120] + '），保留原名')
            toks = cand
            renamed = True
            print(f'[2] 语义重命名完成: {ren.renamed:,} 个局部变量 (作用域取自 luau-ast，绑定校验 ✓)')
        except Exception as e:                  # noqa: BLE001
            print(f'[2] 重命名跳过: {e}')
    named = list(toks)

    # 数字归一化 + 字符串解码
    toks = [(t, norm_num(v) if t == 'n' else (reencode_string(v) if t == 's' else v))
            for t, v in toks]
    lossless = same_values(toks, named)
    print('[3] 数字归一化 + 字符串转义解码完成')

    # 常量折叠：每一处都是 Luau 自己会算出的值（libs/luaunum.py），有 luau 时再交给解释器复核
    folds_checked = None
    n_folds = 0
    if args.no_fold:
        print('[4] 按参数跳过常量折叠')
    else:
        report = []
        folded = fold_consts(toks, report)
        n_folds = len(report)
        if report and luau_bin:
            try:
                n_cases, bad = check_folds(toks, report, luau_bin)
            except Exception as e:              # noqa: BLE001
                n_cases, bad = 0, None
                print(f'[!] 折叠复核没有跑成（{e}）')
            if bad:
                print(f'[!] luau 对 {len(bad)} 处折叠算出了不同的值（例如 {bad[0][0]} ≠ {bad[0][1]}），'
                      '已放弃全部折叠')
                folded, n_folds = toks, 0
            elif bad is not None:
                folds_checked = n_cases
        toks = folded
        how = ''
        if n_folds:
            how = (f'，{folds_checked} 种表达式已由 luau 复核' if folds_checked is not None
                   else '（没有 luau，未经解释器复核）')
        print(f'[4] 常量折叠: {n_folds:,} 处{how} → {len(toks):,} tokens')

    # 定位顶层 setmetatable({...}, {}) 的 handler 表
    tbl_start = None
    for i, (t, v) in enumerate(toks):
        if t == 'w' and v == 'setmetatable':
            for k in range(i + 1, min(i + 5, len(toks))):
                if toks[k] == ('o', '('):
                    if k + 1 < len(toks) and toks[k + 1] == ('o', '{'):
                        tbl_start = k + 1
                    break
            if tbl_start is not None:
                break

    # 统计各数字索引在正文（形如 V[key]）中被引用的次数
    import collections
    ref = collections.Counter()
    for i in range(len(toks) - 2):
        if toks[i][0] == 'w' and toks[i + 1] == ('o', '[') and toks[i + 2][0] == 'n':
            v = parse_num(toks[i + 2][1])
            if v is not None:
                ref[v] += 1

    handlers = []
    if tbl_start is not None:
        bs, be = find_table_body(toks, tbl_start)
        handlers = extract_handlers(toks, bs, be)
        print(f'[5] handler 表: 定位到 {bs}..{be}，抽取 {len(handlers)} 个顶层条目')

        # 由后往前插入注释，避免索引偏移
        inserts = []
        for key_int, key_raw, vt, st in handlers:
            # one line: a string token in the value may contain a line break
            desc = ' '.join(describe_value(vt).split())
            inserts.append((st, ('c', f'-- [{key_int}] = {desc}')))
        for st, ct in sorted(inserts, key=lambda x: -x[0]):
            toks.insert(st, ct)
    else:
        print('[5] 未定位到顶层 handler 表，跳过映射标注')

    layout = {}
    out_text, ok = format_checked(toks, info=layout)
    out_bytes = (out_text + '\n').encode('utf-8', errors='surrogateescape')
    Path(args.output).write_bytes(out_bytes)
    print(f'[6] 输出: {args.output}  ({len(out_bytes):,} 字节, {len(out_text.splitlines()):,} 行)')
    if layout.get('fallback'):
        print(f'[!] 缩进排版没有通过校验（{str(layout.get("reason"))[:120]}），'
              '输出改用保守排版：每条语句一行、token 之间一个空格')

    # 校验
    #   排版    输出重新词法化后与排版前逐 token 相同（注释也算）
    #   字面量  数字 / 字符串改写前后按值相同
    #   语法树  输出重新解析，与输入的语法树只差：局部变量名、被折叠的常量子表达式（其值
    #           按 Luau 的算术重新算一遍，必须等于写进去的数字）
    tree = None
    if root is not None:
        try:
            root3, errors3 = luascope.parse(out_text, ast_bin)
            tree, why = (False, '输出有语法错误: ' + errors3[0]) if errors3 else \
                luascope.alpha_equal(root, root3, folds=True)
        except Exception as e:                  # noqa: BLE001
            tree, why = False, f'{type(e).__name__}: {e}'
    good = ok and lossless and tree is not False
    if good:
        parts = ['排版只改了空白', '字面量取值不变']
        if tree:
            parts.append('语法树与输入一致' + ('（局部变量名、已折叠的常量除外）' if (renamed or n_folds)
                                         else ''))
        else:
            parts.append('语法树未复核（没有 luau-ast 或输入有语法错误）')
        print('[7] 校验通过：' + '，'.join(parts))
    else:
        print('[7] 警告: 输出与输入不等价，请人工检查：'
              + ('' if ok else ' 排版后的 token 序列变了；')
              + ('' if lossless else ' 字面量取值变了；')
              + ('' if tree is not False else f' 语法树不一致（{why[:160]}）'))
    if not args.no_rename and not renamed:
        print('    （未重命名：输出仍是原变量名）')

    if args.map and handlers:
        rows = ['# Luraph v15.1 handler 映射表', '',
                f'共 {len(handlers)} 个顶层条目。key 已归一化为十进制，便于在代码中检索 `V[<key>]`。', '',
                '说明：`V[大数字]` 是查 handler 表；`V[0]`~`V[200]` 一类小索引是 VM 寄存器 / 栈槽，不属于本表。', '',
                '| key (十进制) | 原始键字面量 | 含义 / 值 | 正文引用次数 |',
                '| ---: | --- | --- | ---: |']
        for key_int, key_raw, vt, _ in sorted(handlers):
            desc = describe_value(vt).replace('|', '\\|')
            rows.append(f'| {key_int} | `{key_raw}` | {desc} | {ref.get(key_int, 0)} |')
        Path(args.map).write_text('\n'.join(rows) + '\n', encoding='utf-8')
        print(f'[8] 映射表: {args.map}')
    return 0 if good else 3


if __name__ == '__main__':
    sys.exit(main())
