#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
luaubin.py: the one place that finds the Luau binaries (luau, luau-ast)

Every script of the static tool used to carry its own copy of this lookup, each with a
different list (some only looked for bin/luau.aarch64, one for a path on its author's
phone).  They all import this module now.

Lookup order for a binary NAME (luau or luau-ast):
  1. the environment variable LUAU_BIN (for luau) or LUAU_AST_BIN (for luau-ast)
  2. NAME.<arch> next to the scripts: ., bin/, libs/, deobf/bin/, ../bin/, ../libs/
     (<arch> is x86_64 or aarch64: a file built for this machine)
  3. NAME in the same directories, unless it is an ELF file for another CPU
  4. NAME on PATH

Nothing is downloaded here.  When a binary is missing, MISSING_HINT says how to build it.

    python3 luaubin.py          # print what was found
"""
import os
import platform
import shutil
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXE = '.exe' if os.name == 'nt' else ''
ENV_VARS = {'luau': 'LUAU_BIN', 'luau-ast': 'LUAU_AST_BIN'}
MISSING_HINT = ('build them once with  ./setup.sh  (or: python3 build_luau.py); '
                'this needs git, cmake and a C++ compiler and takes a few minutes')
_ELF_MACHINES = {0x3E: 'x86_64', 0xB7: 'aarch64', 0x03: 'i386', 0x28: 'arm'}


def arch_suffix():
    """Suffix of binaries built for this machine: x86_64 / aarch64 / ..."""
    m = platform.machine().lower()
    if m in ('x86_64', 'amd64'):
        return 'x86_64'
    if m in ('aarch64', 'arm64'):
        return 'aarch64'
    return m


def elf_arch(path):
    """Machine name of an ELF file, or None when `path` is not an ELF file."""
    try:
        with open(path, 'rb') as f:
            hdr = f.read(20)
    except OSError:
        return None
    if len(hdr) < 20 or hdr[:4] != b'\x7fELF':
        return None
    machine = struct.unpack('<H' if hdr[5] == 1 else '>H', hdr[18:20])[0]
    return _ELF_MACHINES.get(machine, '0x%x' % machine)


def check_elf_arch(path):
    """(ok, why not): can this machine run the file?  Non-ELF files are left to the OS."""
    arch = elf_arch(path)
    if arch is None or arch == arch_suffix():
        return True, ''
    return False, '这是 %s 二进制，当前架构 %s 无法执行' % (arch, platform.machine())


def ensure_executable(path):
    """chmod +x when the bit is missing (files unpacked from a zip).  True if it changed."""
    try:
        if not os.access(path, os.X_OK):
            os.chmod(path, 0o755)
            return True
    except OSError:
        pass
    return False


def search_dirs():
    return [HERE, HERE / 'bin', HERE / 'libs', HERE / 'deobf' / 'bin',
            HERE.parent / 'bin', HERE.parent / 'libs']


def find(name):
    """Path of the binary `name` (luau / luau-ast) as a string, or None."""
    env = os.environ.get(ENV_VARS.get(name, ''), '')
    if env and os.path.isfile(env):
        ensure_executable(env)
        return env
    suf = arch_suffix()
    dirs = search_dirs()
    for d in dirs:
        for cand in (d / ('%s.%s' % (name, suf)), d / ('%s.%s%s' % (name, suf, EXE))):
            if cand.is_file():
                ensure_executable(str(cand))
                return str(cand)
    for d in dirs:
        cand = d / (name + EXE)
        if cand.is_file() and check_elf_arch(str(cand))[0]:
            ensure_executable(str(cand))
            return str(cand)
    found = shutil.which(name)
    if found and check_elf_arch(found)[0]:
        return found
    return None


def find_luau():
    return find('luau')


def find_luau_ast():
    return find('luau-ast')


def require(name):
    """find(name), or exit with a message that says how to get the binary."""
    p = find(name)
    if not p:
        sys.stderr.write('[✗] 找不到 %s（%s not found）\n    %s\n' % (name, name, MISSING_HINT))
        sys.exit(3)
    return p


def main():
    rc = 0
    for name in ('luau', 'luau-ast'):
        p = find(name)
        if p:
            print('%-9s %s  (%s, %d bytes)' % (name, p, elf_arch(p) or 'not ELF',
                                               os.path.getsize(p)))
        else:
            print('%-9s NOT FOUND' % name)
            rc = 1
    if rc:
        print(MISSING_HINT)
    return rc


if __name__ == '__main__':
    sys.exit(main())
