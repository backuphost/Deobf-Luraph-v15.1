#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lift.py —— 把抓到的 Luau 字节码反编译回 Lua 源码

这是「可读性」的关键一环。字节码反汇编只是一堆寄存器操作，
真正的源码要靠反编译器还原。本脚本封装 Tovek（开源 Luau 反编译器），
自动处理 glibc 依赖、base64 包装、调用。

用法:
    python lift.py <字节码文件> [输出.luau]
    python lift.py --check            # 只看运行环境是否就绪
    python lift.py --fetch <字节码文件> [输出.luau]   # 缺反编译器时联网下载

前置:
    需要 tools/luau-lifter（Tovek，第三方二进制，只有 aarch64 版本）与 glibc loader。
    缺失时默认只报告并说明怎么补，不联网；加 --fetch 才会从 GitHub Release 下载
    （和 fetch_bin.py --lifter-only 同一处，超时即放弃，不会卡住）。
"""
import os
import sys
import base64
import shutil
import tempfile
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
LIFTER = TOOLS / "luau-lifter"
GLIBC = TOOLS / "glibc"

REPO = "sikon789/Deobf-Luraph-v15.1"
TAG = "v1.5.0"
MIRRORS = [
    "https://ghproxy.net/https://github.com/{repo}/releases/download/{tag}/{name}",
    "https://github.com/{repo}/releases/download/{tag}/{name}",
]


def say(stage, msg):
    print(f"[{stage}] {msg}", flush=True)


def glibc_loader():
    """找 glibc 的 loader。
    优先用系统自带的（Debian/Ubuntu/Fedora 直接就有），
    其次才用 tools/glibc 里下载的副本（Alpine 需要）。"""
    import platform as _pf
    mach = _pf.machine().lower()
    names = {
        "aarch64": ["/lib/ld-linux-aarch64.so.1",
                    "/usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1"],
        "x86_64": ["/lib64/ld-linux-x86-64.so.2",
                   "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"],
    }.get(mach, [])

    # 系统 loader 只有在不是 musl 假货时才用
    # （Alpine 的 /lib/ld-linux-aarch64.so.1 是 gcompat 的桩，跑不了 glibc 程序）
    for p in names:
        cand = Path(p)
        if cand.is_file() and "gcompat" not in str(cand.resolve()):
            # 验证它是真 glibc：能列出自己的版本
            try:
                r = subprocess.run([str(cand), "--version"],
                                   capture_output=True, text=True, timeout=10)
                if "GLIBC" in ((r.stdout or "") + (r.stderr or "")):
                    return cand
            except Exception:
                pass

    # 退回到随包下载的副本
    for cand in (GLIBC / "ld-linux-aarch64.so.1",
                 GLIBC / "ld-linux-x86-64.so.2",
                 GLIBC / "ld.so"):
        if cand.is_file():
            return cand
    return None


MANUAL_HINT = """
手动准备：
  1) 从 https://github.com/Kiet1308/Tovek/releases 取
     Tovek-V2.7-linux-aarch64.tar.gz，解出 luau-lifter 放到 tools/
  2) glibc（Alpine 跑 glibc 程序必需；Debian/Ubuntu 用系统自带的 loader）
     apt 环境：cp -r /usr/lib/aarch64-linux-gnu/ld-* /usr/lib/aarch64-linux-gnu/libc.so.6 tools/glibc/
  或者联网下载： python lift.py --fetch <字节码文件>
"""


def ensure_tools(fetch=False):
    """确保 luau-lifter + glibc 就位。缺失时只有 fetch=True 才下载。"""
    import platform as _pf
    need = []
    if not LIFTER.is_file():
        need.append("luau-lifter.aarch64")
    if not glibc_loader():
        need.append("glibc-aarch64.tar.gz")

    if not need:
        return True
    if LIFTER.is_file() is False and _pf.machine().lower() not in ("aarch64", "arm64"):
        say("失败", f"luau-lifter 只有 aarch64 版本，当前架构 {_pf.machine()} 用不了")
        return False
    if not fetch:
        say("失败", "缺少：" + "、".join(need))
        print(MANUAL_HINT, flush=True)
        return False

    TOOLS.mkdir(exist_ok=True)
    for name in need:
        ok = False
        for tmpl in MIRRORS:
            url = tmpl.format(repo=REPO, tag=TAG, name=name)
            dest = TOOLS / name
            say("下载", url[:70] + "…")
            try:
                r = subprocess.run(["curl", "-fsSL", "--connect-timeout", "15",
                                    "--max-time", "300", "-o", str(dest), url],
                                   capture_output=True, timeout=330)
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                say("失败", f"下载不了：{type(e).__name__}")
                continue
            if r.returncode == 0 and dest.exists() and dest.stat().st_size > 10000:
                ok = True
                break
        if not ok:
            say("失败", f"{name} 下载不到")
            print(MANUAL_HINT, flush=True)
            return False
        # 解包
        if name == "luau-lifter.aarch64":
            LIFTER.chmod(0o755)
        elif name.endswith(".tar.gz"):
            GLIBC.mkdir(exist_ok=True)
            subprocess.run(["tar", "-xzf", str(TOOLS / name), "-C", str(GLIBC)],
                           check=False)
            (TOOLS / name).unlink(missing_ok=True)
    return True


def run_lifter(bc_path, out_path):
    """真正调用 luau-lifter"""
    # luau-lifter 只吃 base64 文本（UniversalSynSaveInstance 格式）
    raw = Path(bc_path).read_bytes()
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src"
        dst = Path(td) / "dst"
        src.mkdir()
        b64 = base64.b64encode(raw).decode()
        (src / "sample.lua").write_text(
            "-- saved bytecode\n" + b64, encoding="utf-8")

        loader = glibc_loader()
        if loader:
            libdir = loader.parent
            cmd = [str(loader), "--library-path", str(libdir),
                   str(LIFTER), "decompile-folder", "--key", "1",
                   str(src), str(dst)]
        else:
            cmd = [str(LIFTER), "decompile-folder", "--key", "1",
                   str(src), str(dst)]

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            say("失败", "反编译超时（600 秒）")
            return None
        except OSError as e:
            say("失败", f"luau-lifter 无法执行：{e}")
            return None
        out = (r.stdout or "") + (r.stderr or "")
        print(out.strip()[-600:], flush=True)

        produced = list(dst.rglob("*.luau"))
        if not produced:
            say("失败", "反编译没有产出")
            return None
        text = produced[0].read_text(encoding="utf-8", errors="ignore")

    dest = Path(out_path)
    dest.write_text(text, encoding="utf-8")
    return dest


def main():
    if "--check" in sys.argv:
        say("luau-lifter", "已就位" if LIFTER.is_file() else "缺失")
        ld = glibc_loader()
        say("glibc loader", str(ld) if ld else "缺失")
        return 0 if (LIFTER.is_file() and ld) else 1

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print(__doc__)
        return 1

    bc = args[0]
    if not Path(bc).is_file():
        say("错误", f"找不到 {bc}")
        return 1
    out = args[1] if len(args) > 1 else str(Path(bc).with_suffix(".luau"))

    say("准备", "检查反编译器…")
    if not ensure_tools(fetch="--fetch" in sys.argv):
        return 1

    say("反编译", f"{bc}（{Path(bc).stat().st_size:,} 字节字节码）…")
    res = run_lifter(bc, out)
    if res:
        n = res.read_text(encoding='utf-8', errors='ignore').count("\n") + 1
        say("完成", f"{res}  {res.stat().st_size:,} 字节  {n:,} 行")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
