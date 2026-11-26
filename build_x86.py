#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_x86.py —— 在 Alpine（aarch64）上为 x86_64 **交叉编译** luau / luau-ast

For a normal build on the machine you are sitting at, use  ./setup.sh  (python3
build_luau.py): it builds the patched Luau 0.739 that every engine of this repository needs.
This script is the old cross-compile route (clang --target=x86_64-alpine-linux-musl on an
Alpine/aarch64 host, runtime packages fetched from an Alpine mirror with `apk` and `curl`);
it builds whatever Luau checkout it finds, WITHOUT the repository's patches, so its output is
enough for deob.py but not for devm / deobf.  It therefore only runs when asked with --cross.

运行时会打印当前阶段，缺什么依赖就自动下载什么，不静默卡住。

用法:
    python build_x86.py --check                # 只检查现有二进制架构
    python build_x86.py --cross                # 交叉编译并放到 bin/（需要 Alpine + 网络）
    python build_x86.py --cross --work DIR     # 指定工作目录（sysroot 缓存、构建目录）
                                               # 默认每次新建临时目录，不与别的运行共用

原理:
    用 clang 的 --target=x86_64-alpine-linux-musl 交叉编译，
    缺的 C/C++ 运行时从 Alpine 官方源下 x86_64 包解压成 sysroot。
"""
import os
import sys
import platform
import subprocess
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
# set in main(): a fresh temporary directory per run, or --work DIR.  (These used to be fixed
# names under /tmp, shared by every run and every user of the machine.)
WORK = None
SYSROOT = None
MIRROR = "https://mirrors.aliyun.com/alpine/v3.21"
ARCH = "x86_64"

# 构建所需的 x86_64 运行库与头文件包
PACKAGES = [
    "musl", "musl-dev", "libgcc", "gcc",
    "libstdc++", "libstdc++-dev", "linux-headers",
]

BUILD_TARGETS = ["Luau.Ast.CLI", "Luau.Repl.CLI"]
OUTPUT_NAMES = {"Luau.Ast.CLI": "luau-ast", "Luau.Repl.CLI": "luau"}


def say(stage, msg):
    print(f"[{stage}] {msg}", flush=True)


def check_arch(path):
    """返回 (ok, 实际架构)"""
    import struct
    try:
        with open(path, "rb") as f:
            hdr = f.read(20)
        if hdr[:4] != b"\x7fELF":
            return True, "非ELF"
        m = struct.unpack("<H", hdr[18:20])[0]
        names = {0x3E: "x86_64", 0xB7: "aarch64", 0x03: "i386", 0x28: "arm"}
        return True, names.get(m, f"0x{m:x}")
    except Exception:
        return False, "未知"


def find_package(name):
    """从 APKINDEX 里查最新版本号"""
    idx = SYSROOT / "APKINDEX"
    if not idx.exists():
        say("索引", "下载 APKINDEX…")
        os.makedirs(SYSROOT, exist_ok=True)
        url = f"{MIRROR}/main/{ARCH}/APKINDEX.tar.gz"
        subprocess.run(["curl", "-sL", "--max-time", "90", "-o",
                        str(SYSROOT / "APKINDEX.tar.gz"), url], check=False)
        subprocess.run(["tar", "-xzf", str(SYSROOT / "APKINDEX.tar.gz"),
                        "-C", str(SYSROOT)], check=False)
    if not idx.exists():
        return None
    cur = None
    for line in idx.read_text(errors="ignore").split("\n"):
        if line.startswith("P:"):
            cur = line[2:].strip()
        elif line.startswith("V:") and cur == name:
            return line[2:].strip()
    return None


def ensure_packages():
    """下载并解压所有依赖包到 sysroot"""
    os.makedirs(SYSROOT, exist_ok=True)
    for pkg in PACKAGES:
        marker = SYSROOT / ".installed" / pkg
        if marker.exists():
            say("依赖", f"{pkg} 已就绪")
            continue
        ver = find_package(pkg)
        if not ver:
            say("依赖", f"⚠ 源里找不到 {pkg}，跳过")
            continue
        say("依赖", f"下载 {pkg}-{ver} …")
        apk = SYSROOT / f"{pkg}-{ver}.apk"
        url = f"{MIRROR}/main/{ARCH}/{pkg}-{ver}.apk"
        r = subprocess.run(["curl", "-sL", "--max-time", "180", "-o", str(apk), url])
        if r.returncode != 0 or not apk.exists() or apk.stat().st_size < 1000:
            say("依赖", f"⚠ {pkg} 下载失败")
            continue
        subprocess.run(["tar", "-xzf", str(apk), "-C", str(SYSROOT)], check=False)
        marker.parent.mkdir(exist_ok=True)
        marker.write_text(ver)
        say("依赖", f"✓ {pkg} 就绪（{apk.stat().st_size:,} 字节）")


def ensure_toolchain():
    """确保 clang / lld / cmake / make 可用，缺则 apk add"""
    need = []
    for tool, pkg in (("clang", "clang"), ("ld.lld", "lld"),
                      ("cmake", "cmake"), ("make", "make"), ("curl", "curl")):
        if not shutil.which(tool):
            need.append(pkg)
    if need:
        say("工具链", f"安装缺失组件：{' '.join(need)}")
        subprocess.run(["apk", "add", "--no-cache"] + need, check=False)
    else:
        say("工具链", "clang / lld / cmake / make 均已就绪")


def write_wrappers():
    """写编译器包装脚本，把 target/sysroot/lld 固化进去"""
    cc = WORK / "x86cc"
    cxx = WORK / "x86cxx"
    base = (f'--target={ARCH}-alpine-linux-musl --sysroot={SYSROOT} '
            f'-fuse-ld=lld')
    cc.write_text(f'#!/bin/sh\nexec clang {base} "$@"\n')
    cxx.write_text(f'#!/bin/sh\nexec clang++ {base} "$@"\n')
    for p in (cc, cxx):
        p.chmod(0o755)
    say("工具链", f"编译器包装脚本就绪 → {cc.name} / {cxx.name}")
    return cc, cxx


def build(luau_src: Path, cc: Path, cxx: Path):
    build_dir = WORK / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    tc = WORK / "toolchain.cmake"
    tc.write_text(f'''set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR x86_64)
set(CMAKE_C_COMPILER {cc})
set(CMAKE_CXX_COMPILER {cxx})
set(CMAKE_EXE_LINKER_FLAGS_INIT "-static")
set(CMAKE_FIND_ROOT_PATH {SYSROOT})
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
''')

    say("配置", "运行 cmake…")
    r = subprocess.run([
        "cmake", str(luau_src),
        f"-DCMAKE_TOOLCHAIN_FILE={tc}",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DLUAU_BUILD_TESTS=OFF",
    ], cwd=build_dir, capture_output=True, text=True)
    if r.returncode != 0:
        say("配置", "✗ 失败")
        print((r.stdout + r.stderr)[-1500:])
        return False
    say("配置", "✓ 完成")

    for tgt in BUILD_TARGETS:
        say("编译", f"{tgt}（首次约 10-20 分钟，ARM 上要用 qemu 模拟 x86 编译器）…")
        r = subprocess.run(["make", "-j8", tgt], cwd=build_dir,
                           capture_output=True, text=True)
        if r.returncode != 0:
            tail = (r.stdout + r.stderr)[-2000:]
            say("编译", f"✗ {tgt} 失败")
            if "kd.h" in tail:
                say("提示", "缺 linux/kd.h —— 需要 linux-headers 包")
            print(tail)
            return False
        say("编译", f"✓ {tgt} 完成")

    # 拷贝产物
    bindir = HERE / "bin"
    bindir.mkdir(exist_ok=True)
    for tgt, name in OUTPUT_NAMES.items():
        src = build_dir / name
        if src.exists():
            dst = bindir / f"{name}.x86_64"
            shutil.copy2(src, dst)
            dst.chmod(0o755)
            say("产物", f"{dst.name}  {dst.stat().st_size:,} 字节")
    return True


def main():
    global WORK, SYSROOT
    if "--check" in sys.argv:
        found = 0
        for name in ("luau", "luau-ast"):
            for d in (HERE / "bin", HERE / "libs"):
                for cand in sorted(d.glob(name + "*")) if d.is_dir() else []:
                    if cand.name == name or cand.name.startswith(name + "."):
                        ok, arch = check_arch(cand)
                        print(f"{cand}  →  {arch}")
                        found += 1
        if not found:
            print("bin/ 里没有 luau / luau-ast；本机构建： ./setup.sh")
        return 0

    if "--cross" not in sys.argv:
        print(__doc__)
        print("本机构建请用  ./setup.sh ；确实要在 Alpine 上交叉编译 x86_64，请加 --cross。")
        return 1

    if not shutil.which("apk"):
        say("错误", "这条路线依赖 Alpine 的 apk；当前系统没有。本机构建请用 ./setup.sh")
        return 1
    import tempfile
    work = None
    for i, a in enumerate(sys.argv):
        if a == "--work" and i + 1 < len(sys.argv):
            work = sys.argv[i + 1]
    WORK = Path(work).resolve() if work else Path(tempfile.mkdtemp(prefix="lv151_x86_"))
    WORK.mkdir(parents=True, exist_ok=True)
    SYSROOT = WORK / "sysroot"
    say("平台", f"当前架构 {platform.machine()}，工作目录 {WORK}")

    luau_src = HERE / "luau"
    if not luau_src.exists():
        for cand in (HERE.parent / "luau",):
            if (cand / "CMakeLists.txt").exists():
                luau_src = cand
                break
    if not (luau_src / "CMakeLists.txt").exists():
        say("错误", "找不到 luau 源码目录（需要 CMakeLists.txt）")
        say("提示", "git clone https://github.com/luau-lang/luau 后放到脚本同级")
        return 1
    say("源码", f"luau 源码 → {luau_src}")

    ensure_toolchain()
    ensure_packages()
    cc, cxx = write_wrappers()

    ok = build(luau_src, cc, cxx)
    if ok:
        say("完成", "✓ x86_64 二进制已生成到 bin/")
        say("说明", "文件名带 .x86_64 后缀，需要时重命名替换 bin/luau 与 bin/luau-ast")
        return 0
    say("失败", "构建未完成，请查看上方日志")
    return 1


if __name__ == "__main__":
    sys.exit(main())
