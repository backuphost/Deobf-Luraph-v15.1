#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_bin.py —— 检查 luau / luau-ast 二进制；需要时从你指定的地址下载

二进制不进 Git 仓库。本仓库的做法是**本地构建**：

    ./setup.sh              （或 python3 build_luau.py）

构建出来的是打过补丁的 Luau 0.739（devm / deobf 两个引擎都依赖这些补丁），
普通的官方 luau 不能代替它。

This script does NOT touch the network unless you ask it to:

    python fetch_bin.py                 # 检查 bin/ 里有什么；齐了就结束，缺了就告诉你怎么构建
    python fetch_bin.py --check         # 同上，只是不给构建提示以外的任何输出
    python fetch_bin.py --url URL       # 从 URL/<名字>.<架构> 下载缺失的二进制（你自己的镜像）
    python fetch_bin.py --download      # 从默认的 GitHub Release 下载（见下）
    python fetch_bin.py --force ...     # 配合 --url / --download：已有也重新下载
    python fetch_bin.py --lifter-only   # 只下载形态 B 用的反编译器（Tovek luau-lifter，仅 aarch64）

环境变量:
    LUAU_BIN_URL     等同 --url
    LUAU_BIN_MIRROR  给默认 Release 地址加镜像前缀（如 https://ghproxy.net/）

About --download: the default address is the release v1.5.0 of sikon789/Deobf-Luraph-v15.1.
That release does not exist for every copy of this repository (a fork has no releases), and
what it held was a stock Luau without the patches, so this is no longer done automatically.
Downloads use short timeouts and never hang: a failure prints how to build instead.
"""
import os
import sys
import shutil
import platform
import subprocess
import struct
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE / "bin"

# Release 地址。换版本时改这两行即可
REPO = "sikon789/Deobf-Luraph-v15.1"
TAG = "v1.5.0"

# 文件清单：仓库内名字 → (Release 附件名)
ARTIFACTS = ["luau", "luau-ast"]

# 反编译器（形态 B 用）：luau-lifter + glibc loader
# 单独处理，因为它不是 ELF 架构校验能覆盖的（luau-lifter 要 glibc 跑）
LIFTER_ASSET = "luau-lifter.aarch64"
GLIBC_ASSET = "glibc-aarch64.tar.gz"

# 默认镜像（内地直连 GitHub 常超时）
DEFAULT_MIRRORS = [
    "https://ghproxy.net/https://github.com/{repo}/releases/download/{tag}/{name}",
    "https://github.com/{repo}/releases/download/{tag}/{name}",
]


def say(stage, msg):
    print(f"[{stage}] {msg}", flush=True)


def arch_suffix():
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    return m


def elf_arch(path):
    """返回 (ok_for_this_machine, 架构名)。非 ELF 视为 python 脚本，放行。"""
    try:
        with open(path, "rb") as f:
            hdr = f.read(20)
    except OSError:
        return False, "读不到"
    if hdr[:4] != b"\x7fELF":
        return True, "非ELF"
    e_machine = struct.unpack("<H", hdr[18:20])[0]
    names = {0x3E: "x86_64", 0xB7: "aarch64", 0x03: "i386", 0x28: "arm"}
    got = names.get(e_machine, f"0x{e_machine:x}")
    return got == arch_suffix(), got


def check(path):
    """检查某个现有文件是否可用"""
    p = Path(path)
    if not p.is_file():
        return False, "不存在"
    if p.stat().st_size < 1000:
        return False, f"只有 {p.stat().st_size} 字节，像是占位文件"
    ok, got = elf_arch(str(p))
    if not ok:
        return False, f"架构是 {got}，不是 {arch_suffix()}"
    if not os.access(str(p), os.X_OK):
        try:
            p.chmod(0o755)
        except OSError:
            return False, "不可执行且无法改权限"
    return True, got


def resolve(name):
    """找已存在的可用二进制：先带后缀，再通用名"""
    suf = arch_suffix()
    for cand in (BIN / f"{name}.{suf}", BIN / name, HERE / f"{name}.{suf}", HERE / name):
        ok, why = check(cand)
        if ok:
            return cand
    return None


def try_system(name):
    """退路：PATH 里的同名程序（如 pip/apt 装的 luau）"""
    found = shutil.which(name)
    if not found:
        return None
    ok, _ = elf_arch(found)
    return Path(found) if ok else None


BUILD_HINT = ("本地构建：  ./setup.sh   （或 python3 build_luau.py；需要 git、cmake 和 C++ 编译器，几分钟）")


def download(url, dest):
    cmd = ["curl", "-fsSL", "--connect-timeout", "15", "--max-time", "120",
           "-o", str(dest), url]
    if not shutil.which("curl"):
        if not shutil.which("wget"):
            say("失败", "没有 curl 也没有 wget，无法下载")
            return False
        cmd = ["wget", "-q", "-T", "30", "-t", "1", "-O", str(dest), url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=150)
    except subprocess.TimeoutExpired:
        say("失败", f"{url[:60]}…  超时")
        return False
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size < 1000:
        err = (r.stderr or r.stdout or "").strip().split("\n")[-1]
        say("失败", f"{url[:60]}…  {err[:80]}")
        return False
    say("完成", f"{url[:60]}…  {dest.stat().st_size:,} 字节")
    return True


def fetch(name, force=False, custom_url=None):
    suf = arch_suffix()
    if not force:
        got = resolve(name)
        if got:
            say("缓存", f"{name} 已就绪 → {got.name}（{got.stat().st_size:,} 字节）")
            return str(got)

    BIN.mkdir(exist_ok=True)
    tmp = BIN / f".{name}.{suf}.part"
    asset = f"{name}.{suf}"

    if custom_url:
        candidates = [custom_url.rstrip("/") + "/" + asset]
    else:
        env = os.environ.get("LUAU_BIN_URL")
        if env:
            candidates = [env.rstrip("/") + "/" + asset]
        else:
            tmpl = os.environ.get("LUAU_BIN_MIRROR")
            if tmpl:
                candidates = [tmpl.rstrip("/") + "/"
                              f"https://github.com/{REPO}/releases/download/{TAG}/{asset}"]
            else:
                candidates = [t.format(repo=REPO, tag=TAG, name=asset)
                              for t in DEFAULT_MIRRORS]

    for url in candidates:
        if download(url, tmp):
            dest = BIN / f"{name}.{suf}"
            shutil.move(str(tmp), str(dest))
            dest.chmod(0o755)
            ok, why = check(dest)
            if ok:
                say("完成", f"{dest.name}  {dest.stat().st_size:,} 字节  ✓")
                return str(dest)
            say("警告", f"下载到的文件不可用（{why}）")
            dest.unlink(missing_ok=True)

    # 下载失败 → 退路
    tmp.unlink(missing_ok=True)
    sysb = try_system(name)
    if sysb:
        say("退路", f"用系统 PATH 里的 {sysb}")
        return str(sysb)

    say("失败", f"{name} 下载不到，本地也没有可用的")
    print("  " + BUILD_HINT, flush=True)
    return None


def ensure_lifter(force=False, custom_url=None):
    """确保反编译器（luau-lifter + glibc）就位。
    这是形态 B 出可读源码的必要条件，缺了就只能出骨架。"""
    tools = HERE / "tools"
    lifter = tools / "luau-lifter"
    glibc = tools / "glibc"

    def loader_ok():
        return any((glibc / n).is_file() for n in
                   ("ld-linux-aarch64.so.1", "ld.so"))

    if not force and lifter.is_file() and loader_ok():
        say("缓存", f"反编译器已就绪 → tools/luau-lifter（{lifter.stat().st_size:,} 字节）")
        return True

    if arch_suffix() != "aarch64":
        say("跳过", f"反编译器 luau-lifter 只有 aarch64 版本，当前是 {arch_suffix()}，不下载")
        return False
    if not shutil.which("curl"):
        say("警告", "没有 curl，跳过反编译器获取")
        return False

    tools.mkdir(exist_ok=True)
    ok_all = True
    for asset, target in ((LIFTER_ASSET, lifter),
                          (GLIBC_ASSET, tools / GLIBC_ASSET)):
        if not force and target.exists() and target.stat().st_size > 50000:
            continue
        got = False
        if custom_url:
            cands = [custom_url.rstrip("/") + "/" + asset]
        else:
            env = os.environ.get("LUAU_BIN_URL")
            cands = ([env.rstrip("/") + "/" + asset] if env else
                     [t.format(repo=REPO, tag=TAG, name=asset) for t in DEFAULT_MIRRORS])
        for url in cands:
            say("下载", f"{asset}…")
            try:
                r = subprocess.run(["curl", "-fsSL", "--connect-timeout", "15",
                                    "--max-time", "300", "-o", str(target), url],
                                   capture_output=True, text=True, timeout=330)
            except subprocess.TimeoutExpired:
                say("失败", "超时")
                continue
            if r.returncode == 0 and target.exists() and target.stat().st_size > 50000:
                got = True
                break
            say("失败", (r.stderr or "").strip().split("\n")[-1][:70])
        if not got:
            ok_all = False
            continue

    if (tools / GLIBC_ASSET).exists():
        glibc.mkdir(exist_ok=True)
        subprocess.run(["tar", "-xzf", str(tools / GLIBC_ASSET),
                        "-C", str(glibc)], check=False)
        (tools / GLIBC_ASSET).unlink(missing_ok=True)

    if lifter.is_file():
        lifter.chmod(0o755)
    if lifter.is_file() and loader_ok():
        say("完成", f"反编译器就绪（{lifter.stat().st_size:,} 字节）")
        return True

    say("警告", "反编译器未就绪 —— 形态 B 将只能输出 handler 表和骨架")
    print("""
  手动补法（任一）：
    1) 从 https://github.com/Kiet1308/Tovek/releases 取
       Tovek-V2.7-linux-aarch64.tar.gz，解出 luau-lifter 放到 tools/
    2) glibc（Alpine 跑 glibc 程序必需，Debian/Ubuntu 系统上不用，
       脚本会自动用系统 loader）
""", flush=True)
    return ok_all


def main():
    name_arg = [a for a in sys.argv[1:] if not a.startswith("-")]
    force = "--force" in sys.argv
    custom = None
    skip = set()
    for i, a in enumerate(sys.argv):
        if a == "--url" and i + 1 < len(sys.argv):
            custom = sys.argv[i + 1]
            skip.add(custom)
    name_arg = [a for a in name_arg if a not in skip]

    say("平台", f"{platform.machine()} → {arch_suffix()}")
    if "--lifter-only" in sys.argv:
        return 0 if ensure_lifter(force=force, custom_url=custom) else 1

    targets = [name_arg[0]] if name_arg and name_arg[0] in ARTIFACTS else ARTIFACTS

    # 先看本地：齐了就不碰网络
    missing = []
    for n in targets:
        got = resolve(n) or try_system(n)
        if got and not force:
            ok, arch = check(got)
            say("已有", f"{n}: {got}  {Path(got).stat().st_size:,} 字节  {arch}")
        else:
            missing.append(n)
            if not got:
                say("缺失", f"{n}: bin/{n} 或 bin/{n}.{arch_suffix()} 不存在")
    if not missing:
        return 0

    want_download = bool(custom or os.environ.get("LUAU_BIN_URL") or "--download" in sys.argv)
    if "--check" in sys.argv or not want_download:
        print("  " + BUILD_HINT, flush=True)
        if "--check" not in sys.argv:
            print("  （要从自己的镜像下载：python fetch_bin.py --url URL；"
                  "默认 Release 地址见本文件开头的说明：--download）", flush=True)
        return 1

    ok_all = True
    for n in missing:
        if not fetch(n, force=force, custom_url=custom):
            ok_all = False
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
