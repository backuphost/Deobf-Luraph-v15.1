#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deob.py —— Luraph 反混淆一体化入口（v15.1 / v15.2）+ 静态提取引擎

    python3 deob.py 输入.lua -o 输出.lua

先做静态分析（剥容器、判断形态）。脚本的业务代码还是明文（形态 A）时，本文件自己把它切出来；
脚本主体跑在 Luraph 的 VM 里时，自动交给去虚拟化器 devm（python3 -m devm.run）；devm 没出
结果再试第二个引擎 deobf（python3 deobf/deob.py）；不是 Luraph 而是 IronBrew1 时交给 deobf。
引擎之间的交接在 allinone.py。用 --engine static 可以只做下面说的静态部分。

以下是静态提取引擎的说明。

思路
----
Luraph v15.1 的常见形态是：

    return setmetatable({ [n] = function(...) ... end, ... }, {}):rk(...)

真正有价值的业务代码，是表里那几个「巨型匿名函数」。它们本身就是**明文 Lua**，
只是被压成一行、塞进表里、外面套了加密壳。

所以正确做法不是去反编译 VM，而是：
  1. 剥外层壳（十进制转义 / base64 / HTML，以及 Luraph 自己的压缩容器）
  2. 用 luau-ast 解析出精确的字节区间
  3. 把没被虚拟化的明文函数按区间切出来：大函数；以及 VM 对象里更小的明文函数 ——
     闭包工厂形态（`function(..)return function(..) ... end end`，Luraph 存放未虚拟化函数
     的方式）、读全局变量的函数（VM 自己的代码从不直接读全局）、业务 API 密集（≥ 5 处）的
     函数。其余没导出的函数逐项计数并说明依据（不会悄悄漏掉）。
     哪个大函数是 VM 分派器，只看结构：解释器分派循环
     （`while true do OP = CODE[PC]; if OP < n ...`，对取出的操作码做数值分支）占了函数的
     大半，才算分派器；函数里出现什么名字（有没有 UI 代码）不影响判定。
     想自己看被剔除的分派器：--keep-dispatchers
  4. 断行 + 语义重命名（作用域取自 luau-ast）
  5. 验证：语法、token 无损、重命名前后绑定一致

脚本被整体虚拟化时（Luraph v15.2 的常见形态：容器里解出来的只有 VM 本体，没有明文业务
函数），静态提取到剥壳为止：默认（--engine auto）接着自动交给去虚拟化器 devm；
--engine static 时只说明情况并给出 `python3 -m devm.run` 的命令，退出码 4。

关于「执行」：静态提取不运行脚本。唯一会运行输入的一步是 [2b] 剥 Luraph 容器。它只在
下面三种情况之一成立时才做（unwrap_reason）：
  · 文件带 Luraph 头注释；
  · 文件是 `return setmetatable({...},{}):X()(...)` 这种对象形态（Luraph 容器的写法），或者是
    `...):X(...)` 形态而且里面没有解释器分派循环（有分派循环的是 VM 本体，不是容器，不执行）；
  · 一个字符串字面量占了文件一半以上（且 ≥ 4,096 字符）：打包的密文。
其它文件不会被执行。注意第三条不认 Luraph：任何「一个大字符串占一半以上」的脚本都会被当作
容器在沙箱里跑一遍。沙箱是 luasandbox.py 的环境表：只有 Luau 自带的库，没有文件读写、
没有 require、没有网络；容器要 loadstring 的代码只被截下来，不运行。
加 --no-run 可以完全禁止这一步（容器就剥不开了）。

用法
----
    python deob.py 输入.lua [-o 输出.lua] [--engine auto|static|devm|deobf|trace]
                            [--min-size 20000] [--no-rename] [--no-run]
                            [--force] [--keep-dispatchers]

退出码：0 完整结果；6 有结果但不完整（引擎的报告说明缺什么，或只有行为轨迹）；
4 VM 形态而没有引擎给出结果（或 --engine static）；3 静态结果没通过语法校验；
2 没认出来；1 出错；7 形态 C 自跑脚本。

    python deob.py 输入.lua --list       # 只列出发现的大函数，不输出

给其它工具用的入口：probe(path, work_dir=None) 返回版本 / 形态 / 剥出的 chunk 与 buffer 路径。
"""
import sys
import os
import re
import bisect
import shutil
import tempfile
import argparse
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (str(HERE / "libs"), str(HERE)):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

import luaubin          # noqa: E402

# ---------------------------------------------------------------- 读写

def read_source(path):
    """File -> str without losing a byte (invalid UTF-8 survives as surrogate escapes)."""
    return Path(path).read_bytes().decode("utf-8", errors="surrogateescape")


def write_source(path, text):
    Path(path).write_bytes(text.encode("utf-8", errors="surrogateescape"))


def nbytes(text):
    """Size in bytes of a text read with read_source (not its length in characters)."""
    return len(text.encode("utf-8", errors="surrogateescape"))


def strip_bom(text):
    """(text without a leading UTF-8 byte order mark, whether there was one).  Editors on
    Windows put one in front of a saved file; Luau's parser takes it for a syntax error."""
    import luasandbox
    bare = luasandbox.strip_bom(text)
    return bare, len(bare) != len(text)


# ---------------------------------------------------------------- Luraph 头注释

# "generated using" is the wording of v14.0.2 - v14.2 (and of some v14.3 files), "protected
# using" that of everything later.  The ` [https://lura.ph/]` part can be missing, and owners
# glue text behind the comment: only the words and the version are required.
LURAPH_HEADER = re.compile(r'--[^\n]*?(?:generated|protected) using Luraph Obfuscator v(\d+(?:\.\d+)*)')


def luraph_header(text):
    """The "-- This file was generated|protected using Luraph Obfuscator vX.Y" comment.

    Returns {'version': '15.2' or None, 'offset': where the comment starts or None,
    'prelude': the text in front of it}.  Script owners put lines such as
    `getgenv().SCRIPT_KEY = "..."` in front of the header: that is the prelude.
    The comment can stand anywhere (owners put licence comments, guards and key lines in
    front of it; one file of the public corpus has it at the very end)."""
    import lphfront
    hit = lphfront.find_header(text)
    if not hit:
        return {"version": None, "offset": None, "prelude": ""}
    start, m = hit
    prelude = text[:start]
    return {"version": m.group(2), "offset": start,
            "prelude": prelude if prelude.strip() else ""}


def version_label(version):
    return f"Luraph v{version}" if version else "Luraph v15.x"


# ---------------------------------------------------------------- 外层壳剥离

DEC_ESC = re.compile(r'"(\\\d{1,3}(?:\\\d{1,3}){100,})"')


def strip_decimal_shell(src):
    """处理 loadstring("\\100\\101\\102...") 这种十进制转义壳"""
    m = DEC_ESC.search(src)
    if not m:
        return None
    try:
        body = bytes(int(x) for x in m.group(1).split("\\")[1:])
    except ValueError:
        return None
    return body.decode("utf-8", errors="surrogateescape")


def strip_base64_shell(src):
    """处理 loadstring(base64_decode("...")) 这种"""
    m = re.search(r'(?:base64_?decode|atob)\s*\(\s*"([A-Za-z0-9+/=]{200,})"', src, re.I)
    if not m:
        return None
    import base64
    try:
        return base64.b64decode(m.group(1)).decode("utf-8", errors="surrogateescape")
    except Exception:
        return None


# 已知的 Luraph 产物语法瑕疵：少一个 end。
# 修补是纯文本层面的，不改变语义 —— 缺的 end 本来就该在那里。
KNOWN_SYNTAX_FIXES = [
    ('else y=0;end;end return function() end end,Ty=function',
     'else y=0;end;end end return function() end end,Ty=function'),
]


def repair_syntax(src):
    """返回 (修补后的源码, 是否改动过)"""
    changed = False
    for bad, good in KNOWN_SYNTAX_FIXES:
        if bad in src:
            src = src.replace(bad, good, 1)
            changed = True
    return src, changed


def strip_html_wrapper(src):
    """从 HTML 里抽出真正的 Lua 代码。
    这类样本是把 Luraph 产物整段塞进 <body>，行数少但某一行极长。"""
    head = src[:600].lower()
    if "<html" not in head and "<!doctype" not in head and "<body" not in head:
        return None
    lines = src.split("\n")
    if not lines:
        return None
    # 最长的那一行就是脚本
    code = max(lines, key=len)
    if len(code) < 1000:
        return None
    return code


def unwrap_shell(src, depth=0, say=print):
    """递归剥壳，最多 3 层"""
    if depth > 3:
        return src, depth
    # 先剥 HTML 外壳（用户常把脚本包在 .html 里发过来）
    inner_html = strip_html_wrapper(src)
    if inner_html and len(inner_html) > 1000:
        fixed, ch = repair_syntax(inner_html)
        if ch:
            say("     ↳ 修补已知语法瑕疵（缺 1 个 end）")
        return unwrap_shell(fixed, depth + 1, say)
    for fn in (strip_decimal_shell, strip_base64_shell):
        inner = fn(src)
        if inner and len(inner) > 1000:
            return unwrap_shell(inner, depth + 1, say)
    return src, depth


# ---------------------------------------------------------------- Luraph 压缩容器

PAYLOAD_MIN = 4096
PAYLOAD_WHY = "一个字符串字面量占了文件一半以上（打包的密文）"


def looks_like_container(text):
    """A container carries its payload: ONE string literal is more than half of the file
    (and at least PAYLOAD_MIN characters).  This test does not know Luraph: any script that
    is mostly one string passes it."""
    from deobf_plus import tokenize
    longest = max((len(v) for t, v in tokenize(text) if t == "s"), default=0)
    return longest >= PAYLOAD_MIN and longest * 2 > len(text)


def unwrap_reason(text, header, feats=None):
    """Why this text is worth running in the unwrap sandbox, or None.
    `feats`: vm_features(text, tree) when the caller has parsed the text (Scan does), so
    that "has dispatch loops" is the syntax tree's answer; else the text-level guess is used.

    Unwrapping means RUNNING the text (in the sandbox of mkfast.py, with loadstring hooked).
    That is done for exactly three kinds of text: one with the Luraph header comment; one
    shaped `return setmetatable({...},{}):X()(...)` (the container Luraph writes), or
    `...):X(...)` without any interpreter dispatch loop in it (an object that only loads
    something; one WITH dispatch loops is the VM itself, there is nothing to unwrap, and run
    without the buffer its container passes it would only fail); one in which a single
    string literal is more than half of the file (looks_like_container).  Any other file is
    not executed."""
    if header.get("version"):
        return "Luraph 头注释"
    if feats is None:
        feats = vm_features(text)
    if feats["object"] and feats["loader"]:
        return "`return setmetatable({...},{}):%s()(...)` 形态" % feats["loader"]
    if feats["object"] and feats["entry"] and not feats["dispatch_loops"]:
        return "`return setmetatable({...},{}):%s(...)` 形态，没有解释器分派循环" % feats["entry"]
    if looks_like_container(text):
        return PAYLOAD_WHY
    return None


class UnwrapNote(str):
    """The note unwrap_container returns: a text, plus `kind`:
      'ok'       a chunk came out
      'damaged'  the container's payload does not decode (corrupt or cut data)
      'needs'    the container needs something this machine's tools do not have (Roblox's
                 EncodingService for a Zstd container when the static decoder is missing)
      'timeout'  the sandbox run was stopped
      'failed'   nothing usable came out for another reason"""
    kind = "failed"

    def __new__(cls, text, kind="failed"):
        self = super().__new__(cls, text)
        self.kind = kind
        return self


def static_container():
    """The static container decoder (devm/container_static.py: pure Python, runs nothing of
    the input, opens the Zstd containers too), or None when this tree does not have it."""
    try:
        from devm import container_static
        return container_static
    except Exception:           # noqa: BLE001  optional module
        return None


def unwrap_container(src, workdir, arg_files=(), call_result=True, timeout=300, say=print):
    """Open Luraph's outer container and collect what it decodes.  The static decoder
    (static_container) is used when this tree has it; else, and when it does not know the
    container, the container is run in the bufemu sandbox (fastsolve.py / mkfast.py), where
    the code it wants to load is captured, not run.

    Returns (result, note).  note is an UnwrapNote (a str with .kind).  result is None when
    nothing usable came out, else a dict:
      'text'         source of the chunk the container loads
      'chunk_path'   file holding exactly those bytes
      'buffer_path'  file holding the buffer the container starts the chunk with, or None
      'data_path'    file holding the data argument whatever its kind (buffer or string), or None
      'data_kind'    'buffer' | 'string' | None
      'chunk_name'   chunk name the container gives to loadstring ("Luraph" + 0-4 spaces), or None
      'compiles'     False when Luau refused the chunk in the sandbox (a build for another Lua)
      'via'          'static' (decoded without running) | 'loadstring' (hooked call) |
                     'buffer' (looked like Lua source in a dump)
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    cs = static_container() if (not arg_files and call_result) else None
    if cs is not None:
        try:
            r = cs.decode(src)
        except cs.ContainerError as e:
            return None, UnwrapNote("容器损坏 / damaged container: %s" % e, "damaged")
        except Exception as e:          # noqa: BLE001  fall back to the sandbox
            r = None
            say("     （静态解码器出错，改用沙箱：%s: %s）" % (type(e).__name__, e))
        if r is not None:
            chunk = workdir / "static.chunk.lua"
            chunk.write_bytes(r.chunk)
            data = None
            if r.data is not None:
                data = workdir / "static.data.bin"
                data.write_bytes(r.data)
            return {"text": read_source(chunk), "chunk_path": str(chunk),
                    "buffer_path": str(data) if data and r.data_kind == "buffer" else None,
                    "data_path": str(data) if data else None, "data_kind": r.data_kind,
                    "chunk_name": r.chunk_name, "compiles": None, "via": "static",
                    "prelude": None}, UnwrapNote("; ".join(r.notes) or "静态解码（%s）" % r.codec, "ok")
    import fastsolve
    target = workdir / "target.lua"
    write_source(target, src)
    lines = []
    try:
        res = fastsolve.solve(str(target), str(workdir / "out"), arg_files=arg_files,
                              call_result=call_result, timeout=timeout, say=lines.append)
    except Exception as e:
        return None, UnwrapNote(f"剥壳执行失败: {e}")
    note = "; ".join(x.strip() for x in lines if x.strip().startswith("[!]")) or "无运行时错误"

    chunks = sorted(res["chunks"], key=lambda c: -c["size"])
    if chunks:
        c = chunks[0]
        buf = next((a for a in c["args"] if a["kind"] == "buffer" and a["path"]), None)
        dat = next((a for a in c["args"] if a["kind"] in ("buffer", "string") and a["path"]), None)
        return {"text": read_source(c["path"]), "chunk_path": c["path"],
                "buffer_path": buf["path"] if buf else None,
                "data_path": dat["path"] if dat else None, "data_kind": dat["kind"] if dat else None,
                "chunk_name": c["name"], "compiles": c.get("compiles", True), "via": "loadstring",
                "prelude": res.get("prelude")}, UnwrapNote(note, "ok")

    layers = sorted((b for b in res["blobs"] if b["layer"]), key=lambda b: -b["size"])
    if layers:
        b = layers[0]
        return {"text": read_source(b["layer"]), "chunk_path": b["layer"], "buffer_path": None,
                "data_path": None, "data_kind": None, "compiles": None,
                "chunk_name": None, "via": "buffer", "prelude": res.get("prelude")}, UnwrapNote(note, "ok")

    # 退而求其次：把所有 blob 当 lua 试
    best = None
    for b in res["blobs"]:
        txt = read_source(b["path"])
        if "setmetatable" in txt and (best is None or len(txt) > len(best[1])):
            best = (b, txt)
    if best:
        return {"text": best[1], "chunk_path": best[0]["path"], "buffer_path": None,
                "data_path": None, "data_kind": None, "compiles": None,
                "chunk_name": None, "via": "buffer", "prelude": res.get("prelude")}, UnwrapNote(note, "ok")
    if res.get("timeout"):
        return None, UnwrapNote(f"沙箱运行超时（{timeout} 秒），没有截到任何代码", "timeout")
    err = res["error"] or ""
    first = err.strip().splitlines()[0] if err.strip() else ""
    if re.search(r"CompressionAlgorithm|GetService|EncodingService|DecompressBuffer", err) \
            or ("DecompressBuffer" in src and re.search(r"attempt to index nil", err)):
        return None, UnwrapNote(
            "Zstd 容器：解压要用 Roblox 的 EncodingService，沙箱里没有；静态解码器 devm/container_static.py "
            "不在这棵树里 / a Zstd container: it inflates through Roblox's EncodingService, which the "
            "sandbox does not have, and the static decoder (devm/container_static.py) is not in this tree"
            "（" + first[:160] + "）", "needs")
    if re.search(r"Luraph decompression error|unsigned overflow|invalid argument #\d+ to 'pack'", err):
        return None, UnwrapNote(
            "容器的密文解不开（Luraph 自己的解码器在沙箱里失败）/ the container's payload does not decode "
            "(Luraph's own decoder fails in the sandbox): " + first[:200], "damaged")
    return None, UnwrapNote("未找到可用的明文层；" + (err or "容器没有调用 loadstring，也没有解出 Lua 源码")[:300])


def strip_luraph_vm(src):
    """Kept for callers of the old name: (inner source or None, note)."""
    tmp = tempfile.mkdtemp(prefix="lv151_")
    try:
        res, note = unwrap_container(src, tmp)
        return (res["text"] if res else None), note
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 二进制

def _arch_suffix():
    """当前平台对应的二进制后缀，如 aarch64 / x86_64"""
    return luaubin.arch_suffix()


def _pick(name):
    """按优先级找二进制（luaubin.find）"""
    return luaubin.find(name)


def find_luau_ast():
    """定位 luau-ast：环境变量 → 架构专用 → 通用 → PATH"""
    return luaubin.find_luau_ast()


def find_luau_runtime():
    """定位 luau 运行时：环境变量 → 架构专用 → 通用 → PATH"""
    return luaubin.find_luau()


def check_elf_arch(path):
    """检查 ELF 架构是否可执行，返回 (ok, 说明)"""
    return luaubin.check_elf_arch(path)


def ensure_executable(path):
    """确保有可执行权限（zip 解压后常见 644）"""
    return luaubin.ensure_executable(path)


def find_luau_ast_or_fetch(quiet=False):
    """Nothing is downloaded any more (the release the old fetch pointed at does not exist
    for this repository): a missing binary is reported with how to build it."""
    p = find_luau_ast()
    if not p and not quiet:
        print("     ✗ 本地没有可用的 luau-ast；" + luaubin.MISSING_HINT, flush=True)
    return p


def find_luau_runtime_or_fetch(quiet=False):
    p = find_luau_runtime()
    if not p and not quiet:
        print("     ✗ 本地没有可用的 luau；" + luaubin.MISSING_HINT, flush=True)
    return p


# ---------------------------------------------------------------- AST 定位

def run_ast(src_text, ast_bin):
    """跑 luau-ast，返回 JSON（{'root': ...}）。临时文件用完即删，文件名每次不同。"""
    import luascope
    root, errors = luascope.parse(src_text, ast_bin)
    return {"root": root, "errors": errors}


def parse_loc(loc):
    """'0,55 - 0,110749' -> (start_line, start_col, end_line, end_col)"""
    a, b = loc.split(" - ")
    l1, c1 = a.split(",")
    l2, c2 = b.split(",")
    return int(l1), int(c1), int(l2), int(c2)


def collect_functions(node, out, depth=0):
    import luascope
    luascope.collect_functions(node, out)


def byte_offset_of(src, line, col):
    """把 (line,col) 转成字符偏移（luau-ast 的行号从 0 开始，列按字节计）"""
    import luascope
    return luascope.LineIndex(src).offset(line, col)


# ---------------------------------------------------------------- 排版

def tidy(text):
    """Kept for old callers.  The formatter's output needs no clean-up; stripping lines of a
    text, as this used to do, would also edit multi-line string literals, so it is a no-op."""
    return text


def luau_compiles(text, luau_bin=None):
    """(True, '') or (False, the compiler's message).  Compile only: the text is handed to
    loadstring as a string and the result is never called (luasandbox.compile_only)."""
    import luasandbox
    luau = luau_bin or find_luau_runtime()
    if not luau:
        return True, ""
    return luasandbox.compile_only(text, luau, timeout=300, chunk_name="input")


def verify(text):
    """用 luau 做语法验证。

    只编译、不执行：把代码当作字符串交给 loadstring，编译出来的函数不调用
    （luasandbox.compile_only）。行号就是输出文件里的行号。
    """
    import luasandbox
    luau = find_luau_runtime()
    if not luau:
        return True, "跳过（未找到 luau 二进制）"
    ok, msg = luasandbox.compile_only(text, luau, timeout=300, chunk_name="output")
    if ok:
        return True, "OK"
    return False, ("验证超时" if msg == "timeout" else msg.replace("output:", "<output>:", 1))


# ---------------------------------------------------------------- 扫描（步骤 1 ~ 5b）

# Text form of an interpreter dispatch loop head: `while true do [local] OP[, X] = CODE[PC];
# if OP < 73 ...` (opcode fetched from an array by a program counter, then compared with a
# number).  It is only a guess made BEFORE the text is parsed (may the text be run to unwrap
# it?); every number the tool reports is counted on the syntax tree (luascope.dispatch_loops).
VM_LOOP = re.compile(r'\bwhile\s+true\s+do\s+(?:local\s+)?([A-Za-z_]\w*)(?:\s*,\s*[A-Za-z_]\w*)*\s*='
                     r'\s*[A-Za-z_]\w*\s*\[\s*[A-Za-z_]\w*\s*\]\s*;?\s*if\s+\1\s*(?:[<>]=?|==|~=)\s*\d')
VM_TAIL = re.compile(r'\}\s*,\s*\{[^{}]*\}\s*\)\s*:\s*(\w+)\s*\(\s*\.\.\.\s*\)\s*;?\s*$')
# the container Luraph wraps the VM chunk in ends `},{}):A()(...)`: A() decodes and loads the
# chunk, and what it returns is called with the script's arguments
LOADER_TAIL = re.compile(r'\}\s*,\s*\{[^{}]*\}\s*\)\s*:\s*(\w+)\s*\(\s*\)\s*\(\s*\.\.\.\s*\)\s*;?\s*$')
SIGNALS = (
    ("GetService", r'GetService'),
    ("Instance.new", r'Instance\s*[.:]\s*new'),
    ("UDim2", r'UDim2'),
    ("Color3", r'Color3'),
    ("TweenService", r'TweenService'),
)


BUSINESS_MIN = 5        # API mentions that make a result (or one small function) business code
V14_LAYOUTS = ("fn-args", "fn-args-vararg", "table")    # VM layouts of Luraph v14.0.2 - v14.9


def api_mentions(text):
    """How often the business-API names of SIGNALS occur in `text`."""
    return sum(len(re.findall(pat, text)) for _k, pat in SIGNALS)


def function_kind(fact):
    """Kind of a function, from what Scan.fact() found (every item is a fact about the
    function's syntax tree or text, none about a particular script):
      'dispatcher'  VM dispatch loop: interpreter dispatch loops make up most of it
      'business'    reads globals and mentions business APIs (or mentions them BUSINESS_MIN
                    times without reading a global: the environment then comes in through
                    captured variables)
      'script'      reads globals, no business-API name: code of the script all the same
                    (HttpGet, loadstring, task, ...).  The VM's own code never reads a global.
      'plain'       neither: it only works on its arguments and captured variables (Luraph's
                    own helper closures, VM methods, or logic that does not touch the
                    environment)"""
    if fact["dispatcher"]:
        return "dispatcher"
    if fact["globals"]:
        return "business" if fact["api"] else "script"
    if fact["api"] >= BUSINESS_MIN:
        return "business"
    return "plain"


_LEAD_COMMENT = re.compile(r'\s*(?:--\[(=*)\[[\s\S]*?\]\1\]|--[^\n]*(?:\n|\Z))')


def code_start(src):
    """Offset of the first code of a Luraph file: after the header comment (and whatever the
    script's owner put in front of it), after any other leading comments and blank space."""
    pos = 0
    m = LURAPH_HEADER.search(src[:8192])
    if m:
        nl = src.find("\n", m.end())
        if nl >= 0 and src[nl + 1:].strip():   # (a header at the very end has no code behind it)
            pos = nl + 1
    while True:
        m = _LEAD_COMMENT.match(src, pos)
        if not m:
            break
        pos = m.end()
    while pos < len(src) and src[pos].isspace():
        pos += 1
    return pos


def vm_features(src, root=None, start=None):
    """What marks a chunk as Luraph's VM object: interpreter dispatch loops and the
    `return setmetatable({...},{}):NAME(...)` shape.  `start`: offset of the main `return`
    when the caller knows it (owner-added statements can stand in front of it).

    'dispatch_loops' is counted on the syntax tree when `root` (the luau-ast tree of `src`) is
    given: luascope.dispatch_loops, the exact shape.  Without a tree it is the number of
    places where the TEXT has that shape (VM_LOOP), which is good enough to tell a VM chunk
    from a container before anything is parsed, and is not reported anywhere."""
    tail = VM_TAIL.search(src[-400:])
    loader = LOADER_TAIL.search(src[-400:])
    if start is None:
        start = code_start(src)
    if root is not None:
        import luascope
        loops = len(luascope.dispatch_loops(root))
    else:
        loops = sum(1 for _m in VM_LOOP.finditer(src))
    return {"dispatch_loops": loops,
            "object": bool(re.match(r'return\s+setmetatable\s*\(', src[start:start + 64])),
            "entry": tail.group(1) if tail else None,
            "loader": loader.group(1) if loader else None}


def is_statement_function(toks):
    """True when an extracted function is a statement (`local function f()`, `function a.b()`,
    with or without attributes) and not a function expression (`function(...) ... end`).
    luau-ast gives the whole statement as the function's location."""
    code = [t for t in toks if t[0] != "c"]
    return not (len(code) >= 2 and code[0][1] == "function" and code[1][1] in ("(", "<"))


class Scan:
    """Steps 1 to 5b: read, find the binaries, peel shells and the container, parse, size up
    the functions, tell business code from VM code.  main() prints as it goes; probe() runs
    the same steps silently."""

    def __init__(self, inp, min_size=20000, work_dir=None, say=print, allow_run=True,
                 keep_dispatchers=False):
        self.inp = Path(inp)
        self.min_size = min_size
        self.say = say
        self.allow_run = allow_run  # False: never run the input (step 2b is skipped)
        self.keep_dispatchers = keep_dispatchers    # export the VM dispatchers too (labelled)
        self.bom = False            # the file started with a UTF-8 byte order mark (removed)
        self._own_work = work_dir is None
        self.work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="lv151_"))
        self.work.mkdir(parents=True, exist_ok=True)
        self._tmp = Path(tempfile.mkdtemp(prefix="lv151_run_"))
        self.raw = ""
        self.header = {"version": None, "offset": None, "prelude": ""}
        self.src = ""
        self.layers = 0
        self.container = None       # dict from unwrap_container + saved paths
        self.unwrap_note = None
        self.sandbox_run = False    # the input's code was run in the unwrap sandbox
        self.ast_bin = None
        self.luau_bin = None
        self.root = None
        self.parse_errors = []
        self.index = None
        self.funcs = []
        self.loops = []             # (start, end, cases) of every interpreter dispatch loop
        self.all_sized = []         # outermost functions of at least min_size
        self.outer_all = []         # outermost functions of any size
        self.sized = []             # (start, end, size, node): what gets extracted
        self.dropped = []           # VM dispatchers among the big functions
        self.kept = []              # the big functions that are no dispatchers
        self.small_plain = []       # functions below min_size that are exported all the same:
                                    # plain (not virtualized) functions of a VM object
        self.small_other = []       # (item, API mentions): below min_size, not exported
        self.api_outside = 0        # API mentions outside every function (table entries)
        self.vm_object = False      # the layer is `return setmetatable({...},{}):X(...)`
        self.signals = {}
        self.score = 0
        self.chunk_score = 0        # the same signals counted over the whole chunk
        self.vm = {"dispatch_loops": 0, "object": False, "entry": None, "loader": None}
        self.form = "unknown"
        self._ast_cache = {}
        self._facts = {}
        # the front check (libs/lphfront.py; see front_check)
        self.recovered = None       # 'json:<key>' / 'oneline': how the Lua text was got out of the file
        self.front = None           # verdict dict for the text after the simple shells
        self.fx = None              # lphfront.Facts of that text
        self.repairs = []           # what was changed in the text so that Luau tools can read it
        self.inner = None           # verdict dict of the chunk a container holds
        self.inner_fx = None
        self.container_info = None  # {'opened': bool, 'via': ..., 'why': ...} for a container
        self._pre_unwrap = ""       # the text as it was before the container was opened

    def cleanup(self, keep_work=False):
        shutil.rmtree(self._tmp, ignore_errors=True)
        if self._own_work and not keep_work:
            shutil.rmtree(self.work, ignore_errors=True)

    # ---- 1
    def load(self):
        import lphfront
        self.raw, self.bom = strip_bom(read_source(self.inp))
        # a JSON wrapper around the script / a file whose line breaks were lost
        self.raw, self.recovered = lphfront.recover(self.raw)
        self.header = luraph_header(self.raw)
        self.src = self.raw

    # ---- 2a  what is this text, as far as it says so itself
    def front_decisive(self):
        """The front check's verdict is final when the text shows something of Luraph: the
        header comment, an "LPH" payload literal, or the container's loader.  For any other
        text (a script without header: an HTML page, another obfuscator, a hand-made VM
        object) the older, looser tests of this file decide."""
        f, fx = self.front, self.fx
        return bool(f and (f["header"] or (fx and (fx.n_lph or "env_msg" in fx.markers))))

    def layout(self):
        """Layout of the VM text the scan ended on: 'fn-args' / 'fn-args-vararg' / 'table' /
        'setmt' when the front check knows it, else None."""
        f = self.inner if self.inner else self.front
        if f and f["verdict"] == "luraph" and f["layout"] in ("fn-args", "fn-args-vararg", "table", "setmt"):
            return f["layout"]
        return None

    def main_offset(self, text):
        """Offset of the main `return` of `text` when the front check located it."""
        if text is self.src:
            fx = self.inner_fx if self.inner_fx is not None else self.fx
            if fx is not None and fx.main_return is not None and fx.size == len(text):
                return fx.main_return
        return None

    def front_check(self):
        """Classify the text (libs/lphfront.py: tokens only), then let Luau's compiler say
        whether a text that looks whole really is.  Two repairs are tried before a text is
        called corrupt, both announced: escapes only Lua 5.1 reads are rewritten with their
        Lua 5.1 meaning; text behind the closed main statement is cut.  A Luarmor loader guard
        (`if not is_from_loader then ... return end`) in front of the script is taken out of
        what the tools run: it is not part of the protected program and would end every run
        at once.  Nothing is executed here."""
        import lphfront
        self.fx = lphfront.analyze(self.src)
        f = self.front = lphfront.classify(self.src, self.fx, self.recovered)
        f["validity"] = {"luraph": "ok", "damaged": "damaged", "unsupported-target": "not-luau"}.get(
            f["verdict"], "n/a")
        f["repairs"] = self.repairs
        if f["verdict"] != "luraph" or not self.luau_bin:
            return f
        text = self.src
        if "luarmor-guard" in f["prefix"] and self.fx.main_return is not None:
            head = text[:self.fx.main_return]
            cut = lphfront.LUARMOR_GUARD.sub("--[[ Luarmor loader guard (left out) ]]", head, count=1)
            if cut != head:
                text = cut + text[self.fx.main_return:]
                self.repairs.append("luarmor-guard-left-out")
        ok, msg = luau_compiles(text, self.luau_bin)
        if not ok and self.fx.feats.get("esc_lua51_only"):
            fixed, n = lphfront.fix_lua51_escapes(text)
            ok2, msg2 = luau_compiles(fixed, self.luau_bin)
            if ok2 or msg2 != msg:
                text, ok, msg = fixed, ok2, msg2
                self.repairs.append("lua5.1-escapes(%d)" % n)
        if not ok and f["suffix"] != ["none"] and self.fx.stmt_end:
            # (offsets of fx are those of self.src; the repairs above keep the main statement's
            # end where it was only when they did not change the length, so find it again)
            fx2 = lphfront.analyze(text) if text is not self.src else self.fx
            if fx2.stmt_end:
                cut = text[:fx2.stmt_end] + "\n"
                ok2, msg2 = luau_compiles(cut, self.luau_bin)
                if ok2:
                    self.repairs.append("cut-trailing(%d bytes)" % (len(text) - fx2.stmt_end))
                    text, ok, msg = cut, ok2, msg2
        if not ok:
            f["verdict"] = "damaged"
            f["validity"] = "corrupt"
            f["reason"] = ("corrupt text: Luau's compiler rejects it (%s) although the main statement is "
                           "closed; the text was edited or damaged" % msg.strip()[:200])
            return f
        if self.repairs:
            f["validity"] = "ok-after-repair"
        if text is not self.src:
            self.src = text
            self.fx = lphfront.analyze(text)
            f["stmt_end"] = self.fx.stmt_end
        return f

    def engine_text(self):
        """The text an engine must be given when it is not the file as it stands (JSON wrapper,
        lost line breaks, repairs of front_check), else None: the engines read the file."""
        if self.recovered or self.repairs or self.bom:
            return self.src if not self.container else self._pre_unwrap
        return None

    # ---- 1b
    def binaries(self):
        self.ast_bin = find_luau_ast()
        self.luau_bin = find_luau_runtime()

    # ---- AST with a one-entry cache (the same text is parsed for sizing and for scanning)
    def _parse(self, text):
        import luascope
        key = id(text)
        hit = self._ast_cache.get(key)
        if hit is not None and hit[0] is text:
            return hit[1], hit[2], hit[3]
        root, errors = luascope.parse(text, self.ast_bin)
        index = luascope.LineIndex(text)
        self._ast_cache = {key: (text, root, errors, index)}
        return root, errors, index

    def _functions(self, text):
        """[(start, end, size, node)] of every function, document order."""
        import luascope
        root, _errors, index = self._parse(text)
        out = []
        for f in luascope.collect_functions(root):
            loc = f.get("location")
            if not loc:
                continue
            s, e = index.span(loc)
            out.append((s, e, e - s, f))
        out.sort(key=lambda x: (x[0], -x[2]))
        return out

    def features(self, text):
        """vm_features(text) with the dispatch loops counted on the syntax tree (the
        text-level guess only when luau-ast gives no tree at all)."""
        try:
            root, _errors, _index = self._parse(text)
        except Exception:           # noqa: BLE001
            root = None
        return vm_features(text, root, self.main_offset(text))

    def has_big_func(self, text):
        try:
            return any(n >= self.min_size for _s, _e, n, _f in self._functions(text))
        except Exception:
            return False

    # ---- 2
    def shells(self):
        self.src, self.layers = unwrap_shell(self.raw, say=self.say)

    # ---- 2b
    def unwrap(self):
        """Luraph's own container.

        When the front check knows what the text is (front_decisive), a container is what
        its main statement says: `return(function() ... end)()(...)` with the loader, or the
        setmetatable object with payload literals and the loader's message.  A VM text is no
        container, whatever the size of its payload string: nothing is run for it and
        nothing is said.

        For any other text the older tests decide.  Unwrapping then RUNS the text
        (sandboxed), so it is tried only when the text carries a payload (one string literal
        is more than half of the file) or ends like Luraph's container (`},{}):X()(...)`),
        or when it has no big function and unwrap_reason() names another reason (the header
        comment, an object without dispatch loops); and never with allow_run=False."""
        say = self.say
        self._pre_unwrap = self.src
        decisive = self.front_decisive()
        if decisive:
            f = self.front
            if f["verdict"] != "luraph" or f["wrapper"] == "none":
                return False
            why = "主语句是 Luraph 的压缩容器：%s" % f["wrapper"]
        else:
            feats = self.features(self.src)
            container_shape = looks_like_container(self.src) or bool(feats["object"] and feats["loader"])
            if not container_shape and self.has_big_func(self.src):
                return False
            why = unwrap_reason(self.src, luraph_header(self.src), feats)
            if why is None:
                if feats["object"] and feats["dispatch_loops"]:
                    self.unwrap_note = ("这一层是 VM 对象本身（setmetatable 对象，里面有解释器分派循环），"
                                        "不是容器：没有可剥的壳，没有执行它")
                else:
                    self.unwrap_note = ("这一层没有大函数，也不像 Luraph 的容器（没有头注释、不是 setmetatable "
                                        "容器形态、没有占文件一半以上的字符串），没有执行它")
                say("[2b] " + self.unwrap_note)
                return False
        static = static_container() is not None
        if not self.allow_run and not static:
            self.unwrap_note = f"这一层像 Luraph 的容器（{why}），但指定了 --no-run：没有执行它，容器没有剥开"
            self.container_info = {"opened": False, "kind": "no-run", "why": "--no-run"}
            say("[2b] " + self.unwrap_note)
            return False
        if static:
            say(f"[2b] 这一层是 Luraph 的容器（{why}），静态解码（不执行；解码器不认识时才进沙箱）…")
        else:
            say(f"[2b] 这一层像 Luraph 的容器（{why}），尝试剥壳（沙箱内执行，拦截 loadstring，不运行被加载的代码）…")
            self.sandbox_run = True
        res, note = unwrap_container(self.src, self._tmp / "c0")
        self.unwrap_note = note
        if res and decisive and res["via"] == "buffer":
            # a buffer that merely looks like Lua source is no answer for a container whose
            # loader we know: it must hand its chunk to loadstring
            res, note = None, UnwrapNote("容器没有把代码交给 loadstring / the container never handed a chunk to "
                                         "loadstring (" + str(note)[:160] + ")")
            self.unwrap_note = note
        if not res:
            self.container_info = {"opened": False, "kind": getattr(note, "kind", "failed"), "why": str(note)}
            say(f"[2b] 剥壳失败：{note}")
            return False
        if res["via"] != "static":
            self.sandbox_run = True
        buf = f" + 数据 buffer {os.path.getsize(res['buffer_path']):,} 字节" if res["buffer_path"] else ""
        if not buf and res.get("data_path"):
            buf = f" + 数据（{res['data_kind']}）{os.path.getsize(res['data_path']):,} 字节"
        name = f"，loadstring 名 \"{res['chunk_name']}\"" if res["chunk_name"] else ""
        say(f"[2b] ✓ 剥壳完成 → {os.path.getsize(res['chunk_path']):,} 字节{buf}（经由 {res['via']}{name}）")
        self.container = res
        self.src = res["text"]
        # 剥出来的层可能还是壳：只有它自己带着大段密文时才继续下探
        for depth in range(3):
            if decisive:
                import lphfront
                self.inner_fx = lphfront.analyze(self.src)
                self.inner = lphfront.classify(self.src, self.inner_fx)
                if self.inner["verdict"] != "luraph" or self.inner["wrapper"] == "none":
                    break
            elif self.has_big_func(self.src) or not looks_like_container(self.src):
                break
            args = [self.container["buffer_path"]] if self.container["buffer_path"] else []
            deeper, _n2 = unwrap_container(self.src, self._tmp / f"c{depth + 1}", arg_files=args,
                                           call_result=False, timeout=120)
            if not deeper or len(deeper["text"]) >= len(self.src):
                break
            self.container = deeper
            self.src = deeper["text"]
            say(f"[2b] 继续下探第 {depth + 2} 层 → {nbytes(self.src):,} 字节")
        c = self.container
        self.container_info = {
            "opened": True, "via": c["via"], "chunk_bytes": os.path.getsize(c["chunk_path"]),
            "chunk_name": c["chunk_name"], "data_kind": c.get("data_kind"),
            "data_bytes": os.path.getsize(c["data_path"]) if c.get("data_path") else None}
        if decisive and self.inner and self.inner["verdict"] == "luraph" and self.luau_bin:
            # the chunk the container loads must itself be a text Luau reads
            ok, msg = luau_compiles(self.src, self.luau_bin)
            if not ok:
                self.inner["verdict"] = "damaged"
                self.inner["reason"] = "Luau's compiler rejects it (%s)" % msg.strip()[:200]
        if decisive and self.inner and self.inner["verdict"] == "damaged" and c.get("compiles") is False:
            # what Luraph's own decoder made of the payload is not a whole chunk
            self.inner["payload_corrupt"] = True
            self.inner["reason"] = ("the container's payload is corrupt: what Luraph's decoder makes of it is "
                                    "not a whole chunk (" + self.inner["reason"] + ")")
        self._save_artifacts()
        return True

    def _save_artifacts(self):
        stem = self.inp.stem
        c = self.container
        chunk = self.work / f"{stem}.chunk.lua"
        shutil.copyfile(c["chunk_path"], chunk)
        c["chunk_path"] = str(chunk)
        if c["buffer_path"]:
            buf = self.work / f"{stem}.buffer.bin"
            shutil.copyfile(c["buffer_path"], buf)
            if c.get("data_path") == c["buffer_path"]:
                c["data_path"] = str(buf)
            c["buffer_path"] = str(buf)
        elif c.get("data_path"):
            dat = self.work / f"{stem}.data.bin"
            shutil.copyfile(c["data_path"], dat)
            c["data_path"] = str(dat)

    # ---- 3 / 4 / 5 / 5a
    def functions(self):
        import luascope
        root, errors, index = self._parse(self.src)
        self.root, self.parse_errors, self.index = root, errors, index
        allf = self._functions(self.src)
        self.funcs = allf
        self.loops = sorted(index.span(d["node"]["location"]) + (d["cases"],)
                            for d in luascope.dispatch_loops(root))
        self._facts = {}
        outer = []
        for item in allf:           # sorted by (start, -size): an outer function comes first
            if outer and item[0] >= outer[-1][0] and item[1] <= outer[-1][1]:
                continue
            outer.append(item)
        self.outer_all = outer
        big = []
        for item in (x for x in allf if x[2] >= self.min_size):
            if big and item[0] >= big[-1][0] and item[1] <= big[-1][1]:
                continue            # 被上一个包含
            big.append(item)
        big.sort(key=lambda x: -x[2])
        self.all_sized = big
        self.dropped = [x for x in big if self.fact(x)["dispatcher"]]
        kept = self.kept = [x for x in big if x not in self.dropped]
        # A VM dispatcher is told by its structure alone (fact(): interpreter dispatch loops
        # make up most of the function), never by which names the function mentions.
        # Functions below the size threshold are not all VM plumbing.  In Luraph's VM object
        # (`return setmetatable({...},{}):X(...)`) a function of the script that was left as
        # plain Lua can be small; three things mark one, and each is checked on the syntax
        # tree or the text of the function itself:
        #   factory   closure-factory shape `function(..)return function(..) ... end end`
        #             (luascope.factory_leaf): how Luraph stores a function it did not
        #             virtualize.  Its own small helper closures have this shape too.
        #   globals   the function reads a global (game, loadstring, Instance ...).  The VM's
        #             own code never does: it keeps every library function in a table slot.
        #   API       at least BUSINESS_MIN business-API mentions (the threshold the tool
        #             requires of a whole result).
        # Such a function is exported with the big ones; every other function is counted, so
        # that nothing is left out silently.  In a text that is not a VM object a small
        # function is just somebody's script, and none of this applies.
        self.small_plain, self.small_other = [], []
        in_functions = 0
        vm_object = self.vm_object = vm_features(self.src, None, self.main_offset(self.src))["object"]
        for item in outer:
            f = self.fact(item)
            in_functions += f["api"]
            if item[2] >= self.min_size:
                continue
            if (vm_object and not f["dispatcher"]
                    and (f["factory"] or f["globals"] or f["api"] >= BUSINESS_MIN)):
                self.small_plain.append(item)
            elif f["api"]:
                self.small_other.append((item, f["api"]))
        self.api_outside = api_mentions(self.src) - in_functions
        if kept or self.small_plain:
            # script code first (it names the environment), then closures that only work on
            # captured variables; the bigger the earlier
            self.sized = sorted(kept + self.small_plain,
                                key=lambda x: (0 if self.is_script(x) else 1, -x[2], x[0]))
            if self.keep_dispatchers:
                self.sized += self.dropped      # asked for: last, and labelled as what they are
        else:
            self.sized = list(big)      # nothing but dispatchers: classify() will say so

    def fact(self, item):
        """What the tool goes by for one function (start, end, size, node):
          'api'         business-API mentions in its text (information; nothing is decided
                        by these names alone except BUSINESS_MIN of them in one function)
          'globals'     {name: uses} of the globals it reads or writes (syntax tree)
          'factory'     depth of the closure-factory shape (0 = not one)
          'loops'       interpreter dispatch loops inside it (luascope.dispatch_loops)
          'loop_share'  share of the function's text that lies inside those loops (0..1)
          'dispatcher'  True when the dispatch loops are more than half of the function:
                        the function IS an interpreter.  A function that merely contains
                        such a loop next to other code is not one."""
        import luascope
        key = (item[0], item[1])
        f = self._facts.get(key)
        if f is None:
            text = self.src[item[0]:item[1]]
            _leaf, depth = luascope.factory_leaf(item[3])
            inside = [(s, e) for s, e, _c in self.loops if s >= item[0] and e <= item[1]]
            covered, end = 0, item[0]
            for s, e in inside:                 # sorted by start; nested loops count once
                if e > end:
                    covered += e - max(s, end)
                    end = e
            size = max(item[1] - item[0], 1)
            f = {"api": api_mentions(text), "globals": luascope.globals_in(item[3]),
                 "factory": depth, "loops": len(inside), "loop_share": covered / size,
                 "dispatcher": covered * 2 > size}
            self._facts[key] = f
        return f

    def is_script(self, item):
        """The function names the environment (function_kind 'business' or 'script': it
        reads a global, or mentions the business API BUSINESS_MIN times) and is no dispatch
        loop: code of the script, not of the VM."""
        return function_kind(self.fact(item)) in ("business", "script")

    def evidence(self):
        """What marks the input as Luraph's output (empty list: nothing does)."""
        ev = []
        if self.header["version"]:
            ev.append("头注释")
        if self.container:
            ev.append("容器")
        if self.layout() and self.front_decisive():
            ev.append("主语句形态（%s）" % self.layout())
        if self.vm["object"]:
            ev.append("setmetatable VM 对象")
        elif self.vm["entry"]:
            ev.append("结尾的 :%s(...) 调用" % self.vm["entry"])
        return ev

    def bare_chunk(self):
        """The input itself is a VM chunk that was cut out of its container before: Luraph's
        VM object with dispatch loops, without the header comment a protected file starts
        with and without a container around it.  The container starts such a chunk with a
        data buffer (the bytecode); run alone it fails at the first read of that buffer."""
        if self.front and self.front["bare_chunk"] and self.front["verdict"] == "luraph" \
                and not self.container and not self.layers:
            return True     # by its entry call: `:M(...)` taking the buffer, `local B=...;return({...}):M()`
        return bool(self.vm["object"] and self.vm["dispatch_loops"] and not self.container
                    and not self.header["version"] and not self.layers)

    def sibling_buffer(self):
        """The buffer file this tool saves next to a chunk it unwrapped
        (`NAME.chunk.lua` -> `NAME.buffer.bin`), if the input is such a chunk and the file
        is there; else None."""
        name = self.inp.name
        for suffix in (".chunk.lua", ".chunk.luau"):
            if name.endswith(suffix):
                cand = self.inp.with_name(name[:-len(suffix)] + ".buffer.bin")
                if cand.is_file():
                    return cand
        return None

    # ---- 5b
    def classify(self):
        region = "".join(self.src[s:e] for s, e, _n, _f in self.sized if (s, e, _n, _f) not in self.dropped)
        self.signals = {k: len(re.findall(pat, region)) for k, pat in SIGNALS}
        self.score = sum(self.signals.values())
        self.chunk_score = api_mentions(self.src)
        # the loop count is taken from the syntax tree once there is one
        self.vm = vm_features(self.src, self.root, self.main_offset(self.src))
        if self.layout() in V14_LAYOUTS:
            # A v14 VM (function-with-arguments or table layout) holds no plain-Lua script
            # functions: every function in it is Luraph's own.  Never form A.
            self.form = "vm"
            return self.form
        # Every Luraph script has a virtualized part; what differs is how much of it the author
        # left as plain Lua.  Form A = at least one plain script function of min_size or more:
        # a function that is no dispatcher and names the environment (is_script: it reads a
        # global, which the VM's own code never does).  The static result is then a
        # substantial part of the script.  Which names it reads plays no part: a script
        # without any UI code is form A all the same.  A VM object whose only plain script
        # code is small functions is form vm (the devirtualizer's case); those functions are
        # listed, and --force / a lower --min-size exports them.
        # Form A is a statement about Luraph's output: a text that shows nothing of Luraph (no
        # header comment, no container, not the VM object) is not classified, however big its
        # functions are (an ordinary script is not "form A").
        big_script = [x for x in self.kept if self.is_script(x)]
        luraph = bool(self.evidence())
        if big_script and luraph:
            self.form = "A"
        elif self.vm["dispatch_loops"] >= 1 and (self.vm["object"] or self.container):
            self.form = "vm"
        elif any(self.is_script(x) for x in self.sized) and luraph:
            self.form = "A"             # plain script functions and no VM loop in sight
        else:
            self.form = "unknown"
        return self.form

    def script_globals(self):
        """{global name: uses} over the functions that would be extracted (dispatchers
        aside): what the form-A decision rests on, for the log."""
        out = {}
        for x in self.sized:
            if x in self.dropped:
                continue
            for name, n in self.fact(x)["globals"].items():
                out[name] = out.get(name, 0) + n
        return out


def probe(path, work_dir=None, min_size=20000, allow_run=True):
    """Static look at a protected script, for a caller that has to pick the engine.

    Nothing is printed.  The outer container, if there is one, is unwrapped (its code is
    captured from loadstring, never run) and the result is saved in `work_dir` (a new temporary
    directory when None; the caller removes it).  allow_run=False: the input is never run,
    so a container stays closed ('error' then says so).  A UTF-8 byte order mark in front of
    the file is ignored.

    Returns a dict:
      'input'           the path given
      'luraph_version'  '15.2', '15.1', ... from the header comment, or None (no header)
      'prelude'         text in front of the header comment ('' if none): owner-added lines
                        such as `getgenv().SCRIPT_KEY = "..."` that must run before the script
      'container'       True when a Luraph container was unwrapped
      'chunk_path'      file with the chunk the container loads (None without a container)
      'buffer_path'     file with the buffer the container starts that chunk with, or None
      'chunk_name'      chunk name given to loadstring ("Luraph"), or None
      'luraph'          what marks the input as Luraph's output: a list out of "头注释",
                        "容器", "setmetatable VM 对象", "结尾的 :X(...) 调用"; [] for a text
                        that shows nothing of Luraph (form is then 'unknown')
      'form'            'A'   plain-Lua script functions inside the VM object, at least one
                              of them of min_size or more: this tool extracts them.  Such a
                              function is no dispatcher and reads a global (the VM's own
                              code reads none); which names it mentions plays no part
                        'vm'  the chunk is Luraph's VM (dispatch loops) and holds no plain-Lua
                              business function of min_size: needs the devirtualizer.  Small
                              plain functions may exist all the same: see 'small_functions'
                              (deob.py --force exports them)
                        'unknown'  neither (also: not Luraph's output, binaries missing, parse
                              error; see 'luraph' and 'error')
      'engine'          'static' for form A, 'devm' for form vm, None otherwise
      'score'           business-API hits in the functions that would be extracted
                        (information only: no decision rests on these names)
      'chunk_score'     the same count over the whole chunk (library references in the VM
                        table such as `[n]=Color3.new` and small helper functions that were
                        left as plain Lua show up here)
      'big_functions'   [(start, end, size)] in the chunk: functions of min_size and more
                        that are not VM dispatchers
      'small_functions' [(start, end, size)] plain-Lua functions of a VM object below
                        min_size: closure-factory shape, or reads a global, or at least 5
                        business-API mentions.  Form A extracts big_functions +
                        small_functions
      'script_functions' the subset of big_functions + small_functions that names the
                        environment (reads a global or mentions a business API): code of the
                        script for certain.  The others only work on captured variables
                        (Luraph's own helper closures, or pure logic)
      'dispatchers'     [(start, end, size)] big functions that are VM dispatchers: their
                        interpreter dispatch loops are more than half of the function
      'sandbox_run'     True when the input's own code was run in the unwrap sandbox (only
                        done for a text that looks like Luraph's output; a plain script is
                        never executed)
      'vm'              {'dispatch_loops': n, 'object': bool, 'entry': 'yQ' or None,
                         'loader': 'A' or None (the text ends `:A()(...)`: a container)}
                        dispatch_loops is counted on the syntax tree
                        (luascope.dispatch_loops: `while true do OP = CODE[PC]; if ...` with
                        OP compared with at least 4 number constants), so loops of the
                        script's own functions that merely begin with a table read are not
                        in it; it is the number devm reports for the same chunk
      'bare_chunk'      True when the input itself looks like a VM chunk that was cut out of
                        its container before (VM object with dispatch loops, no header
                        comment, no container around it).  A container starts its chunk with
                        a data buffer, so the devirtualizer then needs
                        `--chunk-arg <buffer file>`; 'sibling_buffer' is that file when the
                        input is named NAME.chunk.lua and NAME.buffer.bin lies next to it
                        (the names this tool saves them under), else None
      'work_dir'        where chunk_path / buffer_path live
      'error'           None, or why the scan stopped
    """
    sc = Scan(path, min_size=min_size, work_dir=work_dir, say=lambda *a, **k: None,
              allow_run=allow_run)
    out = {"input": str(path), "luraph_version": None, "prelude": "", "container": False,
           "chunk_path": None, "buffer_path": None, "chunk_name": None, "luraph": [],
           "form": "unknown", "engine": None, "score": 0, "chunk_score": 0, "big_functions": [],
           "small_functions": [], "script_functions": [], "dispatchers": [],
           "sandbox_run": False, "vm": dict(sc.vm), "bare_chunk": False,
           "sibling_buffer": None, "work_dir": str(sc.work), "error": None}
    try:
        sc.load()
        out["luraph_version"] = sc.header["version"]
        out["prelude"] = sc.header["prelude"]
        sc.binaries()
        if not sc.ast_bin or not sc.luau_bin:
            out["error"] = "luau / luau-ast not found: " + luaubin.MISSING_HINT
            return out
        sc.shells()
        sc.unwrap()
        out["sandbox_run"] = sc.sandbox_run
        if sc.container:
            out["container"] = True
            out["chunk_path"] = sc.container["chunk_path"]
            out["buffer_path"] = sc.container["buffer_path"]
            out["chunk_name"] = sc.container["chunk_name"]
        elif sc.unwrap_note and (sc.sandbox_run or "--no-run" in sc.unwrap_note):
            out["error"] = sc.unwrap_note
        sc.functions()
        if sc.parse_errors:
            out["error"] = "parse error: " + sc.parse_errors[0]
        sc.classify()
        out["form"] = sc.form
        out["engine"] = {"A": "static", "vm": "devm"}.get(sc.form)
        out["score"] = sc.score
        out["chunk_score"] = sc.chunk_score
        out["big_functions"] = [(s, e, n) for s, e, n, _f in sc.all_sized if (s, e, n, _f) not in sc.dropped]
        out["small_functions"] = [(s, e, n) for s, e, n, _f in sc.small_plain]
        exported = [x for x in sc.all_sized if x not in sc.dropped] + sc.small_plain
        out["script_functions"] = [(x[0], x[1], x[2]) for x in exported if sc.is_script(x)]
        out["dispatchers"] = [(s, e, n) for s, e, n, _f in sc.dropped]
        out["vm"] = dict(sc.vm)
        out["luraph"] = sc.evidence()
        out["bare_chunk"] = sc.bare_chunk()
        sib = sc.sibling_buffer() if out["bare_chunk"] else None
        out["sibling_buffer"] = str(sib) if sib else None
    except Exception as e:      # noqa: BLE001  a probe reports, it does not raise
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        sc.cleanup(keep_work=True)
    return out


# ---------------------------------------------------------------- 形态 B / C（旧路径）

def try_dynamic_lift(inp_path, args, out_dir):
    """形态 B：走动态路径，截获字节码 → 反编译 → 返回输出路径"""
    try:
        import dynlift
    except Exception as e:
        print(f"     ✗ 动态模块不可用：{e}")
        return None

    bc = dynlift.extract_bc(str(inp_path))
    if not bc:
        return None

    tmpd = Path(tempfile.mkdtemp(prefix="lv151_bc_"))
    try:
        tmp = tmpd / "payload.bin"
        tmp.write_bytes(bc)
        out = Path(out_dir) / f"{Path(inp_path).stem}_源码.luau"
        print("     交给反编译器（Tovek）…")
        try:
            r = subprocess.run([sys.executable, str(HERE / "lift.py"), str(tmp), str(out)],
                               capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            print("     ✗ 反编译超时")
            return None
        tail = ((r.stdout or "") + (r.stderr or "")).strip()
        if r.returncode != 0:
            print("     ✗ " + tail.split("\n")[-1][:150])
            return None
        if not out.is_file():
            return None
        n = out.read_text(encoding="utf-8", errors="ignore").count("\n") + 1
        print(f"     ✓ {out.stat().st_size:,} 字节  {n:,} 行")
        return out
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def form_c(inp, raw, args, out_dir):
    """形态 C：内联展开 VM / 没能在 [2b] 剥开的容器。
    静态解剖 + 生成自跑捕获脚本并在本地实跑。产物写到 out_dir（不写到样本旁边）。
    返回退出码，或 None 表示不是这种形态。"""
    try:
        import inlinevm
        is_inl, info = inlinevm.detect(raw)
    except Exception as e:
        is_inl, info = False, {'reasons': [f'inlinevm 不可用：{e}']}
    if not is_inl:
        return None

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print()
    print("═" * 64)
    print("  ◆ 识别为「内联展开 VM」形态（形态 C）")
    print("═" * 64)
    for r in info.get('reasons', []):
        print(f"    · {r}")

    print()
    print("[6c] 静态解剖 VM 语义层…")
    try:
        vm = inlinevm.dissect(inp, verbose=True)
    except Exception as e:
        print(f"      ⚠ 解剖失败：{e}")
        vm = None
    if not vm:
        return None

    print()
    print("[6d] 生成本地可执行的自跑捕获脚本…")
    harness = out_dir / (inp.stem + '.harness.lua')
    try:
        p = inlinevm.make_harness(inp, vm, harness)
        print(f"      ✓ {p.name}  {p.stat().st_size:,} 字节")
    except Exception as e:
        print(f"      ⚠ 生成失败：{e}")
        return None

    # ── 实跑一遍，所有统计数字都从这次执行里取 ──
    print()
    print("[6e] 在本地运行自跑脚本，采集实测数据…")
    run = inlinevm.run_harness(p)

    # ── 拦到明文 = 直接交付源码，不再走自跑脚本那条路 ──
    if run.get('captured_plain') and run.get('captured_src'):
        cp = run['captured_plain']
        out_src = out_dir / (inp.stem + '.unpacked.lua')
        out_src.write_text(run['captured_src'], encoding='utf-8')
        print()
        print("═" * 64)
        print("  ✓ 已解出下一层源码")
        print("═" * 64)
        print(f"  通过 {cp['via']} 截获 {cp['bytes']:,} 字节明文")
        print(f"  （buffer.readu8 调用 {run.get('reads', 0):,} 次，"
              f"分配 {run.get('buffers', 0)} 个 buffer）")
        print(f"  → {out_src}")
        # 判断解出来的东西还能不能再剥一层
        inner = out_src.read_text(encoding='utf-8', errors='ignore')
        feats = vm_features(inner)
        if feats["dispatch_loops"]:
            print()
            print("  注意：解出来的是 Luraph 的 VM 本体，不是业务源码。")
            print(f"        里面有明文业务函数时：python3 deob.py {out_src}")
            print(f"        整体虚拟化时用去虚拟化器：python3 -m devm.run {inp}")
        print("═" * 64)
        return 0

    print()
    print("═" * 64)
    print("  为什么交付的是自跑脚本，而不是源码")
    print("═" * 64)
    print("  这个样本把业务代码编译进了 VM 自己管理的 buffer，")
    print("  明文里只剩 handler 表和密文串。反混淆器已经做到：")
    print(f"    · 命名了 {len(vm['prims'])} 个标准库原语（m[N] → buffer.readu8 这类）")
    print(f"    · 还原了 {len(vm['handlers'])} 个 handler 的状态转移")
    if vm.get('top_dispatch'):
        print(f"    · 定位到顶层调度器 m[{vm['top_dispatch']['slot']}]"
              f"（转发 {vm['top_dispatch']['dispatch_calls']} 个别 handler）")
    if vm.get('main_loop'):
        print(f"    · 定位到最大解释器 m[{vm['main_loop']['slot']}]"
              f"（{vm['main_loop']['comparisons']} 个状态比较）")
    if vm.get('decoys'):
        print(f"    · 识别出 {len(vm['decoys'])} 个诱饵槽位（定义了但从不被引用）")
    print()
    if run.get('ok'):
        print("  在本地实际跑了一遍（数字全部来自这次运行）：")
        print(f"    · buffer.readu8 调用 {run.get('reads', 0):,} 次")
        print(f"    · 分配 {run.get('buffers', 0)} 个 buffer："
              + ", ".join(f"{n:,}" for n in run.get('buffer_sizes', [])))
        print(f"    · writeu8 {run.get('writes', 0)} 次 / "
              f"writestring {run.get('strings', 0)} 次")
        if run.get('captured_plain'):
            cp = run['captured_plain']
            print(f"    · ★ 通过 {cp['via']} 拦到 {cp['bytes']:,} 字节明文")
        else:
            print(f"    · 拦到 {run.get('loads', 0)} 次加载调用")
            print("      （没拦到明文，说明 VM 是解释执行而非解出字节码再加载）")
        print()
        if run.get('exec_ok'):
            print("  VM 完整跑完了。")
        else:
            err = (run.get('exec_err') or '')
            print(f"  执行未跑完：{err[:110]}")
            # 分情况说明，不要一律套「解密成功但缺 Roblox」的模板
            low = err.lower()
            if 'buffer expected' in low or 'got nil' in low:
                print()
                print("  这是「缺少输入」而不是「缺 Roblox 运行时」：")
                print("  VM 的入口要一段字节码 buffer，而这个样本的字节码")
                print("  由外层供给（双层混淆）。单独跑内层必然拿到 nil。")
                print("  正确做法是连外层一起跑。")
            elif 'cancel' in low or "with 'new'" in low or 'index nil' in low:
                print()
                print("  这是「缺 Roblox 运行时」：解密后的业务代码要调 tween /")
                print("  instance 这类真实对象。这个错误词在整份源码里搜不到，")
                print("  只可能来自解密后的字节码 —— 说明解密本身是成功的。")
            else:
                print()
                print("  错误原因见上。")
    else:
        print(f"  本地实跑失败：{run.get('error', '未知原因')}")
    print()
    print("  → 把自跑脚本放到有 Roblox 的机器上运行，它会自己：")
    print("      1. dump VM 分配的每一个 buffer")
    print("      2. 记录每一次 buffer.readu8（可重放）")
    print("      3. 挂 loadstring / setfenv / luau_load，谁拿到明文就拦谁")
    print("    输出用 markdown 包起来，直接粘回来就行。")
    print("  → 或者直接用去虚拟化器（带 Roblox 模拟环境）：")
    print(f"      python3 -m devm.run {inp} -o 输出.lua")
    print("═" * 64)
    print()
    print(f"  产物：{p}")
    if run.get('log'):
        print(f"  实跑日志：{run['log']}")
    if args.vm_report:
        rp = out_dir / (inp.stem + '.vm_anatomy.md')
        inlinevm.write_report(vm, rp)
        print(f"  解剖报告：{rp}")
    else:
        print("  解剖报告：加 --vm-report 可导出 vm_anatomy.md")
    return 7


# ---------------------------------------------------------------- 提取（形态 A）

def normalize_literals(toks):
    """Decimal numbers; "\\229\\141\\141"-style escapes back to readable UTF-8 text.
    Every literal keeps its value (checked by the caller with same_values).
    Returns (tokens, number of strings decoded)."""
    from deobf_plus import norm_num, decode_string
    fixed = []
    decoded = 0
    for t, v in toks:
        if t == "n":
            fixed.append((t, norm_num(v)))
        elif t == "s" and "\\" in v and v[0] in "\"'":
            # 十进制转义 "\229\141\141" = 中文，还原成真字符，
            # 否则产物里全是 \xxx\xxx，读不了也搜不到
            new = v
            try:
                raw = decode_string(v)
                u = raw.decode("utf-8") if raw is not None else None
            except UnicodeDecodeError:
                u = None
            # 含任何控制字符（换行/制表/回车）、反斜杠或引号就保留原样
            if (u is not None and not any(ord(ch) < 32 or ord(ch) == 127 for ch in u)
                    and "\\" not in u and '"' not in u and "'" not in u):
                new = f'"{u}"'
                decoded += 1
            fixed.append((t, new))
        else:
            fixed.append((t, v))
    return fixed, decoded


def build_part(scan, item, all_ptoks, offsets, rename, slots, inline_slots, wrap, ast_bin):
    """One extracted function -> (text, info).  Every step is checked:
      layout   the formatted text lexes back to the tokens it was made from
      literals normalised numbers / strings keep their values
      tree     the text is parsed again by luau-ast and compared with the tree of the input
               function: with renaming it must be the same up to a consistent renaming of
               locals (and the recorded slot substitutions) = `binding`; without renaming it
               must be the same tree outright
    If the renamed version fails `binding`, the part is rebuilt with the original names (and
    that text goes through the tree comparison too)."""
    import luascope
    import reslot
    from deobf_plus import format_checked, same_values
    s, e, _n, node = item
    a = bisect.bisect_left(offsets, s)
    b = bisect.bisect_left(offsets, e)
    ptoks = all_ptoks[a:b]
    orig = [(t, v) for t, v, _o in ptoks]
    fact = scan.fact(item)
    kind, api = function_kind(fact), fact["api"]
    stmt = is_statement_function(orig)
    info = {"renamed": 0, "slots": {}, "slot_comment": 0, "slot_inline": 0, "decoded": 0,
            "layout": False, "literals": False, "binding": None, "tree": None, "note": None,
            "fallback": None, "kind": kind, "api": api, "statement": stmt,
            "label": describe_function(scan, item)}
    # text that parses on its own: an expression needs something in front of it
    lead = "" if stmt else "return "

    def finish(toks):
        fixed, decoded = normalize_literals(toks)
        lay = {}
        text, ok = format_checked(fixed, wrap=wrap, info=lay)
        info["decoded"] = decoded
        info["layout"] = ok
        # the indented layout failed its own check and the one-statement-per-line layout was
        # used instead: still the same tokens, but the caller has to say so
        info["fallback"] = lay.get("reason") if lay.get("fallback") else None
        info["literals"] = same_values(fixed, toks)
        return text

    def tree_same(text, inlined=None):
        """The text, parsed by luau-ast, against the input function's tree."""
        whole = lead + text
        root2, errors2 = luascope.parse(whole, ast_bin)
        if errors2:
            return False, "输出解析失败: " + errors2[0]
        fns = luascope.collect_functions(root2)
        if not fns:
            return False, "输出里没有函数"
        idx2 = luascope.LineIndex(whole)
        fns.sort(key=lambda f: (idx2.span(f["location"])[0], -idx2.span(f["location"])[1]))
        return luascope.alpha_equal(node, fns[0], inlined)

    if rename:
        try:
            analysis = luascope.Analysis(node, scan.index, ptoks)
            ren = luascope.Renamer(analysis)
            toks = ren.apply(ptoks)
            inlined = {}
            stats = {"slots": {}, "comment": 0, "inline": 0}
            if slots:
                hints = luascope.slot_hints(analysis)
                toks, stats, inlined = reslot.apply_hints(toks, hints, inline=inline_slots)
            text = finish(toks)
            # binding check: parse the result and compare it with the original function
            ok, why = tree_same(text, inlined)
            if ok:
                info.update(renamed=ren.renamed, slots=stats["slots"], binding=True, tree=True,
                            slot_comment=stats["comment"], slot_inline=stats["inline"])
                return text, info
            info["note"] = "重命名结果未通过绑定校验，已改用原变量名：" + why[:160]
        except Exception as ex:     # noqa: BLE001  fall back to the unrenamed text
            info["note"] = f"重命名跳过（{type(ex).__name__}: {str(ex)[:140]}），保留原变量名"
        info["binding"] = False
    text = finish(orig)
    try:
        ok, why = tree_same(text)
    except Exception as ex:         # noqa: BLE001
        ok, why = False, f"{type(ex).__name__}: {str(ex)[:140]}"
    info["tree"] = ok
    if not ok:
        note = "输出与原函数的语法树不一致：" + why[:160]
        info["note"] = (info["note"] + "；" + note) if info["note"] else note
    return text, info


def handler_table(src_toks):
    """`[n] = value` entries of the top-level setmetatable({...}) table, as comment lines."""
    from deobf_plus import extract_handlers, format_tokens
    start = None
    for i, (t, v) in enumerate(src_toks):
        if t == "w" and v == "setmetatable":
            start = i
            break
    if start is None:
        return []
    brace = None
    for i in range(start, min(start + 6, len(src_toks))):
        if src_toks[i] == ("o", "{"):
            brace = i
            break
    if brace is None:
        return []
    depth = 0
    endb = None
    for i in range(brace, len(src_toks)):
        t, v = src_toks[i]
        if t == "o" and v in ("(", "[", "{"):
            depth += 1
        elif t == "o" and v in (")", "]", "}"):
            depth -= 1
            if depth == 0:
                endb = i
                break
    if endb is None:
        return []
    lines = []
    for key, _raw, valtoks, _ in extract_handlers(src_toks, brace + 1, endb):
        desc = " ".join(format_tokens(valtoks[:40], wrap=False).split())[:70]
        lines.append(f"-- [{key}] = {desc}")
    return lines


# ---------------------------------------------------------------- 主流程

def describe_function(scan, item, limit=4):
    """One short label for a function, from what Scan.fact() found."""
    f = scan.fact(item)
    share = f"占函数 {100.0 * f['loop_share']:.0f}%"
    if f["dispatcher"]:
        return f"VM 分派器（{f['loops']} 个分派循环，{share}），不是业务源码"
    # a loop of the interpreter's shape inside a function that is mostly something else
    note = f"；另含 {f['loops']} 个分派循环形态的循环（{share}）" if f["loops"] else ""
    if f["globals"]:
        names = sorted(f["globals"], key=lambda k: (-f["globals"][k], k))
        if f["api"]:
            return f"业务 API {f['api']} 处，读全局 {len(names)} 个" + note
        more = f" 等 {len(names)} 个" if len(names) > limit else ""
        return "读全局 " + ", ".join(names[:limit]) + more + "（未见业务 API 名）" + note
    if f["api"]:
        return (f"业务 API 名 {f['api']} 处，不读全局（名字在字符串 / 字段名里，"
                "或环境经捕获的变量传入）") + note
    if f["factory"]:
        return "闭包工厂形态，不读全局（只操作捕获的变量）" + note
    return "未见业务 API，不读全局" + note


def small_plain_summary(scan):
    """'闭包工厂形态 a 个 … 其中读全局或调用业务 API 的 b 个' for scan.small_plain."""
    sp = scan.small_plain
    fac = sum(1 for x in sp if scan.fact(x)["factory"])
    script = sum(1 for x in sp if scan.is_script(x))
    bits = []
    if fac:
        bits.append(f"闭包工厂形态 {fac} 个")
    if len(sp) - fac:
        bits.append(f"其它形态 {len(sp) - fac} 个")
    return "，".join(bits) + f"；其中读全局或调用业务 API 的（脚本代码）{script} 个"


def devm_advice(scan, inp):
    """Lines that tell how to hand this input to the devirtualizer."""
    out = []
    if scan.bare_chunk():
        buf = scan.sibling_buffer()
        out.append("  这个文件看起来是已经从容器里剥出来的 VM chunk（VM 对象，没有头注释，外面没有容器）。")
        out.append("  容器启动 chunk 时会传入一块数据 buffer（字节码在里面）；不带 buffer 单独运行，")
        out.append("  chunk 会在第一次读 buffer 时出错。两种做法：")
        out.append("")
        out.append("      python3 -m devm.run <带容器的原始文件> -o 输出.lua")
        out.append("          （推荐：devm 自己剥容器，buffer 一并带上）")
        if buf:
            out.append(f"      python3 -m devm.run {inp} --chunk-arg {buf} -o 输出.lua")
            out.append("          （同目录下找到了本工具剥壳时存的 buffer 文件）")
        else:
            out.append(f"      python3 -m devm.run {inp} --chunk-arg <数据 buffer 文件> -o 输出.lua")
            out.append("          （buffer 文件：deob.py 剥容器时存成 <名字>.buffer.bin，见 --work）")
    else:
        out.append(f"      python3 -m devm.run {inp} -o 输出.lua")
    return out


def print_vm_block(scan, inp, kept_work):
    label = version_label(scan.header["version"])
    c = scan.container
    print()
    print("═" * 64)
    print(f"  ◆ {label} 虚拟机：脚本主体已被虚拟化（virtualized）")
    print("═" * 64)
    if c:
        size = os.path.getsize(c["chunk_path"])
        buf = (f" + 数据 buffer {os.path.getsize(c['buffer_path']):,} 字节"
               if c["buffer_path"] else "")
        print(f"  · 外层压缩容器已剥除：VM chunk {size:,} 字节{buf}")
    entry = f"，入口 :{scan.vm['entry']}(...)" if scan.vm["entry"] else ""
    print(f"  · 这一层是 Luraph 的 VM 对象：{scan.vm['dispatch_loops']} 个解释器分派循环{entry}")
    in_big = sum(api_mentions(scan.src[s:e]) for s, e, _n, _f in scan.all_sized)
    nb, nd = len(scan.all_sized), len(scan.dropped)
    if not nb:
        print(f"  · 没有 ≥ {scan.min_size:,} 字节的函数")
    elif nd == nb:
        print(f"  · {nb} 个大函数全是 VM 分派器（函数的大半是分派循环），没有 ≥ {scan.min_size:,} 字节的"
              f"明文脚本函数（大函数里的业务 API 名：{in_big} 处）")
    else:
        # by classify(): none of the others names the environment (else this were form A)
        print(f"  · {nb} 个大函数里 {nd} 个是 VM 分派器（函数的大半是分派循环）；其余 {nb - nd} 个不是分派器，"
              f"但不读全局、业务 API 名不足 {BUSINESS_MIN} 处（VM 自己的方法，或不碰环境的逻辑）：")
        for x in scan.kept:
            print(f"      {x[0]:>8} - {x[1]:<8}  {x[2]:>8,} 字节  {describe_function(scan, x)}")
        print(f"    没有 ≥ {scan.min_size:,} 字节的明文脚本函数（大函数里的业务 API 名：{in_big} 处）")
    if scan.small_plain:
        sp = scan.small_plain
        print(f"  · 有 {len(sp)} 个没被虚拟化的小函数（明文 Lua，共 {sum(x[2] for x in sp):,} 字节，业务 API "
              f"{sum(scan.fact(x)['api'] for x in sp)} 处）：{small_plain_summary(scan)}")
        print(f"    加 --force 可以导出它们；最大的 {max(x[2] for x in sp):,} 字节，"
              f"小于阈值 {scan.min_size:,}，所以不算作脚本主体")
    if scan.dropped:
        print("  · 被判为分派器的函数想自己看：加 --keep-dispatchers（连同 --force）一并导出，逐段标注")
    small = sum(a for _it, a in scan.small_other)
    if small or scan.api_outside:
        where = []
        if small:
            where.append(f"{small} 处在 {len(scan.small_other)} 个不导出的小函数里")
        if scan.api_outside:
            where.append(f"{scan.api_outside} 处是 VM 表里的库函数引用（`[n]=Color3.new` 这类）")
        print(f"  · chunk 里其它出现业务 API 名的地方：" + "，".join(where))
    print("  · 脚本主体以字节码形式由 VM 解释执行（数据在 buffer 里），不在这些明文里")
    print()
    print("  静态提取到此为止。还原这类脚本要用去虚拟化器：")
    print()
    for line in devm_advice(scan, inp):
        print(line)
    if c:
        print()
        if kept_work:
            print("  已剥出的文件：")
            print(f"      VM chunk     {c['chunk_path']}")
            if c["buffer_path"]:
                print(f"      数据 buffer  {c['buffer_path']}")
        else:
            print("  剥出的 VM chunk / 数据 buffer 没有保存（临时目录已清理）；"
                  "要保存：加 --work 目录（--engine static 时也可以用 -o，存到输出文件旁边）")
    print("═" * 64)


OTHER_LOADER_REASON = ("not Luraph: the main statement is a loader function that is not Luraph's container (no "
                       "payload literal, no loader message), and what it loads is not a Luraph VM: ")
LAYOUT_TEXT = {
    "fn-args": "`return(function(P1..Pn) ... end)(A1..An)(...)`（v14.0.2 - v14.3 的 VM / fn-args VM）",
    "fn-args-vararg": "`return(function(P1..Pn, ...) ... end)(A1..An)(...)`（旧式 fn-args VM / legacy fn-args VM）",
    "table": "`return({...}):M()(...)`（v14.4 - v14.9 的 VM / table VM）",
    "setmt": "`return setmetatable({...},{}):M()(...)`（v15.x 的 VM / setmetatable VM）",
}
VERDICT_TEXT = {
    "not-luraph": "不是 Luraph 的产物 / not Luraph",
    "damaged": "输入已损坏，无法按现状处理 / damaged input, cannot be processed as given",
    "unsupported-target": "目标平台不支持 / unsupported target",
}


def allinone_code(name):
    import allinone
    return getattr(allinone, name)


def print_front(scan):
    """One or two lines: what the front check made of the text."""
    f = scan.front
    if scan.recovered:
        how = {"oneline": "换行丢失，整个程序在头注释的 `--` 后面：已把代码放回单独一行 / line breaks were lost, "
                          "the program sat behind the header comment"}
        bits = [how.get(x, "JSON 包装（键 %s）里取出 Lua 源码 / Lua source taken out of a JSON wrapper" % x[5:]
                        if x.startswith("json:") else x) for x in scan.recovered.split("+")]
        print("[1c] " + "；".join(bits))
    if not (scan.front_decisive() or f["verdict"] in ("damaged", "unsupported-target")):
        return
    what = []
    if f["wrapper"] != "none":
        what.append("压缩容器 / compressed container (%s)" % f["wrapper"])
    elif f["layout"] in LAYOUT_TEXT:
        what.append("主语句 " + LAYOUT_TEXT[f["layout"]])
        if f["bare_chunk"]:
            what.append("从容器里取出的 chunk，数据由参数传入 / a chunk taken out of its container")
    if f["platform"] not in ("none", "luau"):
        what.append("目标 / target: %s" % f["platform"])
    for kinds, label in ((f["prefix"], "头注释周围 / around the header"), (f["suffix"], "主语句之后 / after the statement")):
        if kinds and kinds != ["none"]:
            what.append("%s: %s" % (label, ", ".join(kinds)))
    if what:
        print("[2a] 识别 / front check：" + "；".join(what))
    for r in scan.repairs:
        print("     ↳ 修整 / adjusted: " + r)


def front_exit(scan):
    """Exit code when the front check ends the run (not Luraph: 2; damaged or a build for
    another Lua: 8), else 0.  Prints the verdict and its reason."""
    f = scan.front
    v = f["verdict"]
    if v in ("damaged", "unsupported-target"):
        print(f"[✗] {VERDICT_TEXT[v]}")
        print("    " + f["reason"])
        return allinone_code("UNPROCESSABLE")
    if v == "not-luraph" and scan.front_decisive() and not scan.layers:
        print(f"[✗] {VERDICT_TEXT[v]}")
        print("    " + f["reason"])
        if f["header"]:
            print("    （文件带着 Luraph 的头注释，但头注释后面不是 Luraph 的产物 / the file carries a Luraph "
                  "header comment, but what follows is not Luraph's output）")
        return 2
    return 0


def container_exit(scan):
    """The same for what a container turned out to hold (or for a container that does not
    open because its payload is damaged)."""
    if not scan.front_decisive():
        return 0
    ci = scan.container_info
    other = scan.front["wrapper"] == "container:other"
    if other and ci and ci.get("kind") != "no-run" and (not ci["opened"] or (
            scan.inner and scan.inner["verdict"] == "not-luraph")):
        print(f"[✗] {VERDICT_TEXT['not-luraph']}")
        print("    " + OTHER_LOADER_REASON + (ci.get("why") or scan.inner["reason"])[:300])
        return 2
    if ci and not ci["opened"] and ci["kind"] == "damaged":
        print(f"[✗] {VERDICT_TEXT['damaged']}")
        print("    容器打不开 / the container does not open: " + ci["why"])
        return allinone_code("UNPROCESSABLE")
    inner = scan.inner
    if inner and inner["verdict"] != "luraph":
        v = "damaged" if inner["verdict"] == "not-luraph" else inner["verdict"]
        print(f"[✗] {VERDICT_TEXT[v]}")
        print("    容器里的 chunk / the chunk inside the container: " + inner["reason"])
        return allinone_code("UNPROCESSABLE")
    return 0


def print_v14_block(scan, inp, kept_work):
    label = version_label(scan.header["version"])
    c = scan.container
    lay = scan.layout()
    print()
    print("═" * 64)
    print(f"  ◆ {label} 虚拟机：脚本整体被虚拟化（virtualized）")
    print("═" * 64)
    if c:
        size = os.path.getsize(c["chunk_path"])
        data = (f" + 数据（{c.get('data_kind')}）{os.path.getsize(c['data_path']):,} 字节"
                if c.get("data_path") else "")
        print(f"  · 外层压缩容器已剥除：VM chunk {size:,} 字节{data}")
    print(f"  · 这一层是 Luraph 的 VM，主语句 {LAYOUT_TEXT[lay]}")
    print("  · 这种布局里没有明文的脚本函数：里面的函数全是 Luraph 自己的（解码器、解释器），"
          "静态提取没有可导出的东西")
    print("    this layout holds no plain-Lua script functions (every function in it is Luraph's own):")
    print("    nothing for the static export; the devirtualizer takes it")
    if scan.bare_chunk():
        for line in devm_advice(scan, inp):
            print(line)
    if c and kept_work:
        print("  已剥出的文件：")
        print(f"      VM chunk     {c['chunk_path']}")
        if c.get("data_path"):
            print(f"      数据         {c['data_path']}")
    print("═" * 64)


def classify_file(path, work_dir=None, allow_run=True):
    """What `deob.py FILE --classify` prints, as a dict.  No engine is started and nothing of
    the input is run, except that a container is opened: by the static decoder when this
    tree has it, else in the unwrap sandbox (where the code it loads is captured, never run;
    allow_run=False forbids that too).

      file        the path given
      verdict     'luraph' | 'not-luraph' | 'damaged' | 'unsupported-target'
      reason      one line for the last three
      version     header version ('14.3'), None without a header
      header      None | {'wording': 'generated'|'protected', 'url': ... or None}
      wrapper     'none' | 'container:<head>-<codec>-<payloads>' (fn0-lzma-1, fn0-lzma-2,
                  fn0-zstd-2, setmt-setmt-2)
      layout      layout of the VM text (the file, or the chunk inside its container):
                  'fn-args' | 'fn-args-vararg' | 'table' | 'setmt' | 'unknown' (container not
                  opened) | 'none'
      entry       call chain of the VM's main statement, e.g. ':M()(...)'
      platform    'luau' | 'lua5.1-syntax' | 'lua5.1' | 'lua5.3+' | 'luajit' | 'lua5.2+/luajit'
                  of the VM text ('?' appended when the text is damaged)
      validity    'ok' | 'ok-after-repair' | 'corrupt' | 'damaged' | 'not-luau' | 'n/a'
      repairs     what was changed so that Luau tools can read the text
      prefix, suffix   kinds of what stands around the header / behind the main statement
      recovered   None | 'json:<key>' | 'oneline'
      bare_chunk  the input is a chunk taken out of a container (needs its data argument)
      container   None | {'opened': bool, 'via', 'chunk_bytes', 'chunk_name', 'data_kind',
                  'data_bytes'} or {'opened': False, 'kind', 'why'}
      route       engines the front end would try, in order ('' = none)
    """
    sc = Scan(path, work_dir=work_dir, say=lambda *a, **k: None, allow_run=allow_run)
    out = {"file": str(path), "verdict": "not-luraph", "reason": "", "version": None, "header": None,
           "wrapper": "none", "layout": "none", "entry": None, "platform": "none", "validity": "n/a",
           "repairs": [], "prefix": [], "suffix": [], "recovered": None, "bare_chunk": False,
           "container": None, "route": ""}
    try:
        sc.load()
        sc.binaries()
        sc.shells()
        f = sc.front_check()
        out.update({k: f[k] for k in ("verdict", "reason", "version", "wrapper", "layout", "entry", "platform",
                                      "validity", "prefix", "suffix", "bare_chunk")})
        out["repairs"] = list(sc.repairs)
        out["recovered"] = sc.recovered
        if f["header"]:
            out["header"] = {"wording": f["header"]["wording"], "url": f["header"]["url"]}
        if sc.layers:
            out["shells"] = sc.layers
        if f["verdict"] == "luraph" and f["wrapper"] != "none":
            sc.unwrap()
            ci = sc.container_info or {"opened": False, "kind": "failed", "why": str(sc.unwrap_note or "")}
            out["container"] = ci
            inner = sc.inner
            if f["wrapper"] == "container:other" and ci.get("kind") != "no-run" and (
                    not ci["opened"] or (inner and inner["verdict"] == "not-luraph")):
                out["verdict"], out["validity"], out["wrapper"], out["layout"] = "not-luraph", "n/a", "none", "none"
                out["reason"] = OTHER_LOADER_REASON + (ci.get("why") or inner["reason"])[:300]
            elif not ci["opened"]:
                if ci["kind"] == "damaged":
                    out["verdict"], out["validity"] = "damaged", "payload-corrupt"
                    out["reason"] = "the container does not open: " + ci["why"]
            elif inner:
                out["layout"], out["entry"], out["platform"] = inner["layout"], inner["entry"], inner["platform"]
                if inner["verdict"] != "luraph":
                    out["verdict"] = "damaged" if inner["verdict"] == "not-luraph" else inner["verdict"]
                    out["reason"] = "the chunk inside the container: " + inner["reason"]
                    if inner.get("payload_corrupt"):
                        out["validity"] = "payload-corrupt"
        if out["verdict"] == "luraph":
            if out["layout"] in V14_LAYOUTS:
                out["route"] = "devm"
            elif (out["version"] or "").startswith("15.0"):
                out["route"] = "deobf,devm"
            else:
                out["route"] = "devm,deobf"
    except Exception as e:      # noqa: BLE001  a classification reports, it does not raise
        out["verdict"], out["reason"] = "error", f"{type(e).__name__}: {e}"
    finally:
        sc.cleanup(keep_work=work_dir is not None)
    return out


CLASSIFY_EXIT = {"luraph": 0, "not-luraph": 2, "damaged": 8, "unsupported-target": 8}


def main():
    ap = argparse.ArgumentParser(
        description="Luraph 反混淆一体化入口：自动判断形态并选引擎（静态提取 / devm 去虚拟化 / deobf 动态引擎）。"
                    "All-in-one front end: static extraction, devm devirtualizer, deobf dynamic engine.")
    ap.add_argument("input", help="输入的 .lua / .txt 文件")
    ap.add_argument("-o", "--output", help="输出文件")
    ap.add_argument("--min-size", type=int, default=20000,
                    help="「大函数」的最小长度（字节），默认 20000；VM 对象里更小的函数只导出明文的："
                         "闭包工厂形态、读全局、或业务 API ≥ 5 处")
    ap.add_argument("--list", action="store_true", help="只列出大函数，不输出")
    ap.add_argument("--classify", action="store_true",
                    help="只识别，不还原 / classify only: 输出一行 JSON（版本、容器、VM 布局、结论 luraph / "
                         "not-luraph / damaged / unsupported-target、头注释前后的附加内容），不启动任何引擎；"
                         "退出码 0 / 2 / 8")
    ap.add_argument("--no-rename", action="store_true", help="不做语义重命名（输出与输入逐 token 相同）")
    ap.add_argument("--no-humanize", action="store_true",
                    help="不折行（保留每条语句一行）")
    ap.add_argument("--no-slots", action="store_true", help="不标注 VM 槽位名（tbl[60 --[[RunService]]]）")
    ap.add_argument("--inline-slots", action="store_true",
                    help="能证明只赋值一次的槽位直接替换成变量名（默认只加注释，不改代码）")
    ap.add_argument("--no-lift", action="store_true", help="不尝试动态反编译（形态 B 时跳过）")
    ap.add_argument("--force", action="store_true",
                    help="判定为 VM 时也导出：有没被虚拟化的小函数就导出它们，否则导出 VM 函数骨架；"
                         "输入不像 Luraph 产物时，仍把其中的函数切出来")
    ap.add_argument("--keep-dispatchers", action="store_true",
                    help="被判为 VM 分派器的大函数也导出（排在最后，逐段标注为分派器）；"
                         "判定依据是结构：函数的大半是解释器分派循环")
    ap.add_argument("--no-run", action="store_true",
                    help="完全不执行输入：跳过 [2b] 剥容器（那一步在沙箱里运行容器代码）")
    ap.add_argument("--vm-report", action="store_true",
                    help="形态 C（内联 VM）时导出 vm_anatomy.md 解剖报告")
    ap.add_argument("--work", help="保存剥壳得到的 VM chunk / 数据 buffer 的目录"
                                   "（--engine static 时默认存到 -o 所在目录；其它情况不给就不保存）；"
                                   "交给引擎时，引擎的中间文件放在它下面的 engine/ 里")
    import allinone
    ap.add_argument("--engine", choices=allinone.ENGINES, default="auto",
                    help="用哪个引擎 / which engine: auto（默认：先静态分析；脚本主体在 VM 里就自动交给 devm 去虚拟化，"
                         "devm 没出结果再试 deobf）、static（只做静态分析，不运行 VM）、devm（去虚拟化器）、"
                         "deobf（动态引擎：Luraph v15 提升器 / IronBrew1）、trace（只要行为轨迹）")
    ap.add_argument("--timeout", type=int, default=None,
                    help="交给 devm / deobf 引擎时每个引擎的超时秒数（默认 %d；大脚本要跑几分钟）；"
                         "超时后引擎连同它启动的进程一起结束" % allinone.DEFAULT_TIMEOUT)
    ap.add_argument("--engine-args", default="",
                    help="原样传给 devm / deobf 引擎的额外参数，写成一个字符串，例如 \"--http-map replies.txt\"")
    args = ap.parse_args()

    inp = Path(args.input)
    if not inp.is_file():
        print(f"[✗] 文件不存在：{inp}")
        return 1

    if args.classify:
        import json
        res = classify_file(inp, work_dir=args.work, allow_run=not args.no_run)
        print(json.dumps(res, ensure_ascii=False))
        return CLASSIFY_EXIT.get(res["verdict"], 1)

    import shlex
    out_path = Path(args.output) if args.output else inp.with_name(inp.stem + "_反混淆.lua")
    engine_work = str(Path(args.work) / "engine") if args.work else None
    engine_extra = shlex.split(args.engine_args)
    if args.engine in ("devm", "deobf", "trace"):
        # the engine was named: no static scan, the engine unwraps the container itself.  Only
        # the front check is made (tokens and a compile, nothing is run): a damaged file or a
        # build for another Lua is said to be that instead of being fed to the engine.
        scan = Scan(inp, say=lambda *a, **k: None, allow_run=False)
        tmp_in = None
        try:
            scan.load()
            scan.binaries()
            if scan.luau_bin:
                scan.front_check()
                if scan.front["verdict"] in ("damaged", "unsupported-target"):
                    print_front(scan)
                    return front_exit(scan)
            src_path, tmp_in = engine_input(scan, inp)
        finally:
            scan.cleanup(keep_work=False)
        try:
            return allinone.hand_over(src_path, str(out_path), args.engine, work=engine_work,
                                      timeout=args.timeout, extra=engine_extra)
        finally:
            if tmp_in:
                shutil.rmtree(tmp_in, ignore_errors=True)

    # the unwrapped chunk and buffer are kept where the user asked for them: --work, or, when
    # only the static engine runs, next to -o.  In automatic mode the engines unwrap on their own.
    work_dir = args.work or (str(out_path.parent) if args.output and args.engine == "static" else None)
    scan = Scan(inp, min_size=args.min_size, work_dir=work_dir, allow_run=not args.no_run,
                keep_dispatchers=args.keep_dispatchers)
    plan = None
    src_path, tmp_in = str(inp), None
    try:
        rc, _keep = run(scan, args, inp, out_path, kept_work=work_dir is not None)
        if args.engine == "auto":
            # what the static scan cannot recover goes to the engine that can
            plan = allinone.after_static(scan, rc, args)
        if plan is not None:
            src_path, tmp_in = engine_input(scan, inp)
        elif rc == 4 and scan.form == "vm" and scan.front_decisive() and scan.bare_chunk() \
                and not scan.sibling_buffer():
            print("[✗] 这是从容器里取出的 VM chunk，它的数据不在文件里：按现状无法处理（退出码 8）/ a VM chunk taken "
                  "out of its container, without its data: it cannot be processed as given (exit 8)")
            rc = allinone.UNPROCESSABLE
    finally:
        # a work directory the tool made itself is temporary and always removed: what was
        # unwrapped is only kept where the user asked for it (--work, or next to -o)
        scan.cleanup(keep_work=False)
    if plan is None:
        return rc
    engine, extra = plan
    try:
        return allinone.hand_over(src_path, str(out_path), engine, work=engine_work,
                                  timeout=args.timeout, extra=extra + engine_extra)
    finally:
        if tmp_in:
            shutil.rmtree(tmp_in, ignore_errors=True)


def engine_input(scan, inp):
    """(path the engines read, temporary directory to remove afterwards or None).  The engines
    read the input file itself unless the front check had to change the text (Scan.engine_text):
    then they get that text, in a file of the same name in a temporary directory."""
    text = scan.engine_text()
    if text is None:
        return str(inp), None
    tmp = tempfile.mkdtemp(prefix="deob_in_")
    path = Path(tmp) / inp.name
    write_source(path, text)
    what = [x for x in ([scan.recovered] if scan.recovered else []) + list(scan.repairs)
            + (["BOM"] if scan.bom else [])]
    print("[i] 引擎读取的是整理过的文本（%s），不是原文件 / the engines are given the adjusted text, "
          "not the file as it stands" % ", ".join(what))
    return str(path), tmp


def run(scan, args, inp, out_path, kept_work):
    """Returns (exit code, False).  The second value used to say "keep the temporary work
    directory"; nothing is kept there any more."""
    scan.load()
    raw = scan.raw
    print(f"[1] 读取：{inp.name}  {inp.stat().st_size:,} 字节"
          + ("（开头的 UTF-8 BOM 已去掉）" if scan.bom else ""))
    if scan.header["version"]:
        pre = scan.header["prelude"]
        extra = f"（头注释前有 {len(pre.strip().splitlines())} 行附加代码）" if pre else ""
        print(f"     头注释：Luraph Obfuscator v{scan.header['version']}{extra}")

    # 前置检查：确认解析器与运行时可用（不联网）
    print("[1b] 检查解析器（luau-ast / luau）…")
    scan.binaries()
    for name, p in (("luau-ast", scan.ast_bin), ("luau", scan.luau_bin)):
        if p:
            print(f"     ✓ {name:<9} {Path(p).name}  {Path(p).stat().st_size:,} 字节")
        else:
            print(f"     ✗ 未找到 {name}")
    if not scan.ast_bin or not scan.luau_bin:
        print("[✗] 缺少 Luau 二进制，无法继续。")
        print("    " + luaubin.MISSING_HINT)
        print("    也可以用环境变量 LUAU_BIN / LUAU_AST_BIN 指向已有的二进制。")
        return 1, False
    ok_arch, arch_msg = check_elf_arch(scan.ast_bin)
    if not ok_arch:
        print(f"[✗] {Path(scan.ast_bin).name}: {arch_msg}")
        print("    " + luaubin.MISSING_HINT)
        return 5, False

    # 剥壳
    scan.shells()
    if scan.layers:
        print(f"[2] 剥除外层壳 {scan.layers} 层 → {nbytes(scan.src):,} 字节")
    else:
        print("[2] 未检测到简单外层壳")
    front = scan.front_check()
    print_front(scan)
    rc = front_exit(scan)
    if rc:
        return rc, False
    scan.unwrap()
    keep = False
    keep_vm = False
    rc = container_exit(scan)
    if rc:
        return rc, False
    if scan.layout() in V14_LAYOUTS and scan.front_decisive() and not (args.force or args.list):
        # a v14 VM: nothing in it is plain script code, so there is nothing to parse and cut
        # out here; the devirtualizer takes it (allinone.after_static)
        scan.form = "vm"
        print_v14_block(scan, inp, kept_work)
        if scan.bare_chunk() and not scan.sibling_buffer():
            return allinone_code("UNPROCESSABLE"), False
        return 4, False

    # 解析 AST
    try:
        scan.functions()
    except Exception as e:
        print(f"[✗] AST 解析失败：{e}")
        return 1, keep
    if scan.parse_errors:
        print(f"[✗] AST 解析失败：{scan.parse_errors[0]}")
        return 1, keep
    scan.classify()
    print("[3] AST 解析成功")
    print(f"[4] 发现 {len(scan.funcs)} 个函数")
    print(f"[5] 长度 ≥ {args.min_size:,} 的函数：{len(scan.all_sized)} 个")
    for x in scan.all_sized:
        print(f"      {x[0]:>8} - {x[1]:<8}  {x[2]:>8,} 字节  {describe_function(scan, x)}")
    if scan.small_plain:
        sp = scan.small_plain
        how = "一并导出" if scan.form == "A" else \
            "没有更大的明文业务函数，整体按 VM 形态处理：加 --force 才导出，见下"
        print(f"    + VM 对象里小于 {args.min_size:,} 字节、没被虚拟化的明文函数：{len(sp)} 个"
              f"（共 {sum(x[2] for x in sp):,} 字节；{how}）")
        print(f"      {small_plain_summary(scan)}")
        shown = sorted(sp, key=lambda x: (0 if scan.is_script(x) else 1, -x[2], x[0]))
        for x in shown[:8]:
            print(f"      {x[0]:>8} - {x[1]:<8}  {x[2]:>8,} 字节  {describe_function(scan, x)}")
        if len(shown) > 8:
            print(f"      … 其余 {len(shown) - 8} 个略（都不大于 {shown[8][2]:,} 字节）"
                  if all(not scan.is_script(x) for x in shown[8:]) else
                  f"      … 其余 {len(shown) - 8} 个略")
    left = sum(a for _it, a in scan.small_other)
    if left or scan.api_outside:
        bits = []
        if left:
            bits.append(f"{len(scan.small_other)} 个小于阈值的函数里共 {left} 处"
                        + ("（不读全局、不是闭包工厂形态，每个不足 "
                           f"{BUSINESS_MIN} 处）" if scan.vm_object else ""))
        if scan.api_outside:
            bits.append(f"函数之外 {scan.api_outside} 处"
                        + ("（VM 表里的库函数引用，如 `[n]=Color3.new`）" if scan.vm_object else ""))
        print("    · 不导出、但出现业务 API 名的地方：" + "；".join(bits)
              + ("。小函数可用 --min-size 调低阈值导出" if left and scan.vm_object else ""))
    if scan.vm_object:
        # the rest of the VM object's functions, so that the list above is the whole picture
        named = set(id(x) for x in scan.all_sized) | set(id(x) for x in scan.small_plain) \
            | set(id(it) for it, _a in scan.small_other)
        rest = [x for x in scan.outer_all if id(x) not in named]
        loops = [x for x in rest if scan.fact(x)["dispatcher"]]
        rest = [x for x in rest if not scan.fact(x)["dispatcher"]]
        if rest:
            # what is left after the tests of Scan.functions(): none of these reads a global,
            # has the closure-factory shape or mentions a business API.  The basis is printed
            # so that the reader can check it.
            print(f"    · 其余 {len(rest)} 个小于阈值的函数不导出"
                  f"（共 {sum(x[2] for x in rest):,} 字节，最大 {max(x[2] for x in rest):,} 字节）："
                  "它们不读任何全局、不是闭包工厂形态、没有业务 API 名 —— VM 对象自己的方法"
                  "（解码器 / 状态机片段）；要看可用 --min-size 调低阈值导出")
        if loops:
            print(f"    · 另有 {len(loops)} 个小于阈值的函数是查表跳转循环（VM 分派器的形态），不导出"
                  f"（共 {sum(x[2] for x in loops):,} 字节）")

    keeping = "（--keep-dispatchers：仍然导出，排在最后）" if scan.keep_dispatchers else ""
    if scan.dropped and len(scan.dropped) < len(scan.all_sized):
        print(f"[5a] 剔除 VM 分派器 {len(scan.dropped)} 个{keeping}：")
        for s, e, n, _f in scan.dropped:
            print(f"      {s:>8} - {e:<8}  {n:>8,} 字节  （查表跳转，非业务代码）")
    elif scan.dropped:
        print(f"[5a] {len(scan.dropped)} 个大函数全部是 VM 分派器（查表跳转），没有业务函数{keeping}")
    if scan.dropped:
        print("     依据是结构，不看函数里出现什么名字：解释器分派循环"
              "（while true do OP = CODE[PC]; if OP < n …，对 OP 做数值分支）占了函数的大半")
    if args.list:
        return 0, keep

    label = version_label(scan.header["version"])
    out_dir = Path(args.work) if args.work else (out_path.parent if args.output else Path("."))

    forced_all = False          # --force on a layer without any candidate: export every function
    if not scan.sized:
        if scan.form == "vm":
            print_vm_block(scan, inp, kept_work)
            if not args.force:
                return 4, keep_vm
            keep = keep_vm
        elif scan.evidence():
            print(f"[!] 没有找到 ≥ {args.min_size:,} 字节的函数。这是 {label} 的产物"
                  f"（依据：{'、'.join(scan.evidence())}），但这一层没有明文业务函数。")
        else:
            print("[!] 没有找到大函数，输入也没有 Luraph 的特征（没有头注释、没有容器、不是 "
                  "setmetatable VM 对象）：它可能不是 Luraph 的产物。")
        if not scan.allow_run and scan.unwrap_note and "--no-run" in scan.unwrap_note:
            print("    容器没有剥开（--no-run）：里面的 VM chunk 没有看到。去掉 --no-run 再试。")
        if not args.force:
            print("    加 --force 可导出最佳猜测的骨架（跳过大小阈值）。")
            return 2, keep
        forced_all = True
        # --force：阈值降到 0，把所有最外层函数都当候选
        print("    --force 已指定，跳过大小阈值，重新收集候选…")
        scan.sized = [x for x in scan.outer_all if x[2] > 0]
        if not scan.sized:
            print("[✗] 连非空函数都没有，这个文件不像是 Luraph 产物。")
            return 2, keep
        print(f"    收集到 {len(scan.sized)} 个函数（不再限制大小）")
        form_before = scan.form
        scan.classify()
        scan.form = form_before if form_before == "vm" else scan.form

    # ---- 形态识别：判断是真明文业务代码，还是纯 VM 字节码 ----
    # 只看结构化信号（真实 API 调用），不看字符串。
    # 原因：随机标识符形如 "YmhpVeGpVQ" 会被误判成 UI 文案；明文 Lua 片段（反篡改自检）
    # 又会让「含空格」的规则得分暴涨。两侧都不安全 → 只用 VM 字节码里不可能出现的 API 结构名。
    print("[5b] 业务特征扫描：")
    for k, v in scan.signals.items():
        print(f"      {k:<22} {v}")
    print(f"      综合得分 = {scan.score}")
    # what the decision rests on is structure, not these names: which of the candidate
    # functions read globals at all (the VM's own code keeps every library function in a
    # table slot and reads none)
    cand = [x for x in scan.sized if x not in scan.dropped]
    n_script = sum(1 for x in cand if scan.is_script(x))
    g = scan.script_globals()
    names = sorted(g, key=lambda k: (-g[k], k))
    print(f"      脚本函数（读全局，或业务 API ≥ {BUSINESS_MIN} 处）：{len(cand)} 个候选里 {n_script} 个；"
          f"读到的全局名 {len(names)} 种" + ("：" + ", ".join(names[:8]) + (" …" if len(names) > 8 else "")
                                       if names else ""))
    print(f"      （形态 A = 候选里有 ≥ {scan.min_size:,} 字节的脚本函数；判定只看这一条和函数结构，"
          "不看上面的 API 名：没有 UI 代码的脚本一样算数）")

    if forced_all:
        pass                    # the user asked for everything; said so above
    elif scan.form != "A":
        if scan.form == "vm":
            print_vm_block(scan, inp, kept_work)
            keep = keep_vm
            if not args.force:
                return 4, keep
            plain = [x for x in scan.sized if x not in scan.dropped]
            if plain:
                bits = []
                small = [x for x in plain if x[2] < scan.min_size]
                if small:
                    bits.append(f"{len(small)} 个没被虚拟化的小函数")
                if len(plain) - len(small):
                    bits.append(f"{len(plain) - len(small)} 个 ≥ {scan.min_size:,} 字节、不是分派器的函数"
                                "（不读全局）")
                if len(scan.sized) - len(plain):
                    bits.append(f"{len(scan.sized) - len(plain)} 个 VM 分派器（--keep-dispatchers）")
                print(f"  --force 已指定：导出 {len(scan.sized)} 个函数（" + "，".join(bits)
                      + "）；脚本主体仍在 VM 里，不在输出中 …")
            else:
                print("  --force 已指定，继续导出 VM 函数骨架（不是业务源码）…")
            print()
        elif not scan.evidence():
            # big functions, API calls, and nothing of Luraph: an ordinary script.  It is not
            # "form A" (that is a statement about Luraph's VM object), and cutting single
            # functions out of it would drop the rest of the script without a word.
            print()
            print("[!] 输入没有 Luraph 的特征（没有头注释、没有容器、不是 setmetatable VM 对象）："
                  "看起来是一份普通脚本，不是 Luraph 的产物。没有做提取。")
            print("    只想把整份文件排版 / 重命名： python3 libs/deobf_plus.py 输入.lua -o 输出.lua")
            if not args.force:
                print(f"    加 --force 仍可把其中 ≥ {args.min_size:,} 字节的函数单独切出来"
                      "（输出只有这些函数，不是整份脚本）。")
                return 2, keep
            print(f"    --force 已指定：只导出 ≥ {args.min_size:,} 字节的 {len(scan.sized)} 个函数；"
                  "函数之外的代码（顶层语句、其它函数）不在输出里。")
            print()
        else:
            print()
            print("═" * 64)
            print("  ⚠ 业务代码已编译进 VM 字节码（静态提取拿不到源码）")
            print("═" * 64)
            print("  候选函数都不读全局，也没有可读的业务 API 调用（只有 VM 寄存器操作）。")
            print("  静态路径到此为止 —— 但字节码本身是可解的。")
            print("═" * 64)
            print()

            if args.no_lift:
                print("  --no-lift 已指定，跳过动态反编译。")
                return 4, keep

            # 形态 B 才需要 luau_load 截获 + 反编译器；先诊断，不是就别去找反编译器
            is_b, why = False, ""
            try:
                import dynlift
                is_b, why = dynlift.probe_loader(raw)
            except Exception as e:
                why = f"动态模块不可用：{e}"
            if not is_b:
                print("  ⚠ 动态路径（截获 luau_load）不适用于这个样本")
                print("        " + why)
                rc = form_c(inp, raw, args, out_dir)
                if rc is not None:
                    return rc, keep
                print()
                print("  可以改用去虚拟化器（带 Roblox 模拟环境）：")
                print(f"      python3 -m devm.run {inp} -o 输出.lua")
                if not args.force:
                    return 4, keep
            else:
                lifter = HERE / "tools" / "luau-lifter"
                if not lifter.is_file():
                    print("[6b] 形态 B 需要反编译器 tools/luau-lifter（Tovek，第三方，仅 aarch64），本地没有。")
                    print("     需要时手动获取： python fetch_bin.py --lifter-only   （会联网）")
                    return 4, keep
                print("[6b] 尝试动态路径：在桩环境里截获字节码并反编译…")
                print("     注意：会在隔离的本地桩环境中执行样本的 VM 加载器，")
                print("           不联网、不接触真实 Roblox 环境。")
                lifted = try_dynamic_lift(inp, args, out_dir)
                if lifted:
                    print(f"[✓] 已还原源码 → {lifted}")
                    return 0, keep
                print()
                print("  动态路径未成功。可用的替代产出：")
                print("    · handler 映射表（VM 索引 → math.random 这类）")
                print("    · 函数结构骨架（不含业务逻辑），加 --force 导出")
                if not args.force:
                    print()
                    print("  已停止输出。")
                    return 4, keep
            print("  --force 已指定，继续导出骨架…")
            print("")

    # ---- 切出来 → 重命名 → 断行，逐段校验 ----
    from deobf_plus import tokenize_pos
    all_ptoks = tokenize_pos(scan.src)
    offsets = [o for _t, _v, o in all_ptoks]
    rename = not args.no_rename
    bodies, infos = [], []
    for idx, item in enumerate(scan.sized, 1):
        text, info = build_part(scan, item, all_ptoks, offsets, rename=rename,
                                slots=rename and not args.no_slots,
                                inline_slots=args.inline_slots, wrap=not args.no_humanize,
                                ast_bin=scan.ast_bin)
        bodies.append(text)
        infos.append(info)
        if info["decoded"]:
            print(f"     ↳ 第 {idx} 段还原 {info['decoded']} 个转义字符串")
        if info["note"]:
            print(f"[!] 第 {idx} 段：{info['note']}")
        if info["fallback"]:
            print(f"[!] 第 {idx} 段：缩进排版没有通过校验（{str(info['fallback'])[:120]}），"
                  "这一段改用保守排版（每条语句一行、token 之间一个空格）")
        checks = "排版无损 " + ("✓" if info["layout"] else "✗") + \
                 ("（保守排版）" if info["fallback"] else "") + "，字面量取值 " + \
                 ("✓" if info["literals"] else "✗")
        if info["binding"]:
            checks += f"，重命名 {info['renamed']:,} 个局部变量、绑定一致 ✓"
        else:
            checks += "，语法树与原函数一致 " + ("✓" if info["tree"] else "✗")
        print(f"[6.{idx}] 断行完成  {nbytes(text):,} 字节  {text.count(chr(10)) + 1:,} 行  （{checks}）")

    if rename:
        done = sum(1 for i in infos if i["binding"])
        print(f"[7] 语义重命名：{done}/{len(infos)} 段（作用域取自 luau-ast，逐段与原函数比对）")
    else:
        print("[7] 按参数跳过重命名：输出与输入逐 token 相同（数字 / 字符串写法除外），"
              "每段重新解析后语法树与原函数一致")

    slot_map = {}
    for i in infos:
        slot_map.update(i["slots"])
    if slot_map:
        nc = sum(i["slot_comment"] for i in infos)
        ni = sum(i["slot_inline"] for i in infos)
        how = f"注释标注 {nc:,} 处" + (f"，直接替换 {ni:,} 处" if args.inline_slots else "")
        if args.inline_slots and not ni:
            how += "（没有一处读取能证明只看得到那一个值：槽位表被交给了 VM 创建的闭包，或读取不在赋值之后的同一块里；全部保留为注释）"
        print(f"[7c] VM 槽位：{len(slot_map)} 个只赋值一次的槽位，{how}")
        for (tab, n), name in sorted(slot_map.items(), key=lambda x: (x[0][0], -x[0][1]))[:5]:
            print(f"        {tab}[{n}] → {name}")

    # 提取 handler 表（[n] = math.random 这类）
    handler_lines = []
    try:
        handler_lines = handler_table([(t, v) for t, v, _o in all_ptoks])
        print(f"[7b] handler 表：{len(handler_lines)} 条")
    except Exception as ex:
        print(f"[!] handler 提取跳过：{ex}")

    # 组装
    all_ok = all(i["layout"] and i["literals"] and i["tree"] for i in infos)
    out_lines = []
    luraph = scan.evidence()
    if luraph:
        out_lines.append(f"-- {label} 静态反混淆输出（deob.py）")
    else:
        out_lines.append("-- 静态提取输出（deob.py --force）：输入没有被识别为 Luraph 的产物")
    out_lines.append("-- 源文件：" + " ".join(inp.name.split()))    # one line, whatever the name
    kinds = [i["kind"] for i in infos]
    n_bus, n_script = kinds.count("business"), kinds.count("script")
    n_plain, n_disp = kinds.count("plain"), kinds.count("dispatcher")
    what = []
    if n_bus:
        what.append(f"业务函数 {n_bus} 个（含业务 API 调用）")
    if n_script:
        what.append(f"读全局、未见业务 API 名的脚本函数 {n_script} 个")
    if n_plain:
        what.append(f"不读全局的函数 {n_plain} 个（只操作参数和捕获的变量："
                    "Luraph 运行时的辅助闭包、VM 方法，或不碰环境的纯逻辑）")
    if n_disp:
        what.append(f"VM 分派器 {n_disp} 个（解释器骨架，不是业务源码）")
    not_exported = [x for x in scan.dropped if x not in scan.sized]
    # a dispatcher is only ever in the output because the user asked (--keep-dispatchers) or
    # because nothing else was there (--force on a VM-only chunk)
    out_lines.append(f"-- 共导出 {len(bodies)} 个函数：" + "，".join(what)
                     + (f"；另有 VM 分派器 {len(not_exported)} 个未导出" if not_exported else ""))
    if not luraph:
        out_lines.append("-- 注意：这里只有从文件里切出来的函数；函数之外的代码（顶层语句等）不在这份输出里")
    elif scan.form == "vm" and (n_bus or n_script):
        size_word = "小函数" if all(x[2] < scan.min_size for x in scan.sized if x not in scan.dropped) \
            else "函数"
        out_lines.append(f"-- 注意：这里只有没被虚拟化的{size_word}。脚本主体由 VM 以字节码形式解释执行，不在这份输出里；"
                         "要用去虚拟化器（python3 -m devm.run）")
    elif n_bus + n_script == 0:
        out_lines.append("-- 注意：这份输出里没有业务源码。"
                         + ("脚本主体由 VM 以字节码形式解释执行，要用去虚拟化器（python3 -m devm.run）"
                            if scan.form == "vm" else "导出的函数里没有业务 API 调用，也不读全局"))
    elif scan.vm["dispatch_loops"]:
        out_lines.append("-- 注意：这里是脚本里没被虚拟化的明文函数。被虚拟化的部分（主流程，以及这些函数通过 "
                         "VM 对象创建的闭包）不在这份输出里；要还原它们用去虚拟化器（python3 -m devm.run）")
    if any(i["fallback"] for i in infos):
        out_lines.append("-- 部分段落使用了保守排版（每条语句一行）：缩进排版没有通过 token 校验")
    if rename:
        out_lines.append("-- 局部变量已重命名（作用域取自 luau-ast；每段都与原函数做过语法树比对）。名字说的是变量"
                         "初始化时的值；之后又被赋了别的值的变量保留原名")
    n_comment = sum(i["slot_comment"] for i in infos)
    n_inline = sum(i["slot_inline"] for i in infos)
    if n_comment:
        out_lines.append("-- tbl[N --[[name]]]：该槽位在可见代码里只被赋值一次，值是局部变量 name（注释，仅供阅读；"
                         "槽位表交给 VM 创建的闭包后，那些闭包里的改写这里看不到）")
    if n_inline:
        out_lines.append(f"-- {n_inline:,} 处 tbl[N] 已替换为赋给它的局部变量名（--inline-slots；"
                         "只在可见代码能证明读到的就是那个值的地方）")
    elif args.inline_slots and slot_map:
        out_lines.append("-- --inline-slots：没有一处槽位读取满足替换条件，代码未改动（槽位名只以注释给出）")
    out_lines.append("")
    if handler_lines:
        out_lines.append("-- ============================================================")
        out_lines.append("-- handler 表（VM 索引 → 实际值）")
        out_lines.append("-- ============================================================")
        out_lines.extend(handler_lines)
        out_lines.append("")
    for i, (text, (s, e, n, _f)) in enumerate(zip(bodies, scan.sized), 1):
        out_lines.append("-- ============================================================")
        k = infos[i - 1]
        out_lines.append(f"-- 第 {i} 段  原始偏移 {s} - {e}  ({n:,} 字节)  {k['label']}")
        out_lines.append("-- ============================================================")
        if k["statement"]:
            # `local function f()` / `function a.b()`: already a statement, written as it is
            out_lines.append(text)
        else:
            out_lines.append(f"local __part{i} = {text}")
        out_lines.append("")
    rets = ", ".join(f"__part{i}" for i in range(1, len(bodies) + 1) if not infos[i - 1]["statement"])
    out_lines.append(f"return {{ {rets} }}")
    out_text = "\n".join(out_lines) + "\n"

    # 验证
    ok, msg = verify(out_text)
    print(f"[8] 语法验证：{'✓ 通过' if ok else '✗ 失败'}  {msg if not ok else ''}".rstrip())
    if not all_ok:
        print("[8] ✗ 内部校验未通过：有段落的排版、字面量或语法树与输入不一致（见上）")

    # 输出
    write_source(out_path, out_text)
    print(f"[9] 输出：{out_path}  {len(out_text.encode('utf-8', 'surrogateescape')):,} 字节  "
          f"{out_text.count(chr(10)):,} 行")
    if scan.container and kept_work:
        print(f"     剥壳得到的 chunk：{scan.container['chunk_path']}")
    if luraph and scan.form == "A" and scan.vm["dispatch_loops"]:
        # form A is never the whole script: say what is missing and which tool recovers it
        print("[i] 输出是脚本里没被虚拟化的明文函数。被虚拟化的部分（主流程，以及这些函数通过 VM 对象"
              "创建的闭包）不在里面；要还原它们用去虚拟化器：")
        for line in devm_advice(scan, inp):
            print(line)
    return (0 if (ok and all_ok) else 3), keep


if __name__ == "__main__":
    sys.exit(main())
