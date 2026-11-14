#!/usr/bin/env python3
"""Build the Luau CLI the devm harness runs samples with.

It is stock Luau plus devm/luau_cli.patch, which adds a handful of builtins to the REPL binary
(readfile / writefile / appendfile / ewrite, exitafter, cwrap).  Needs git, cmake and a C++
compiler.  The binary is written to bin/luau, which is git-ignored.

    python devm/build_luau.py [--build-dir DIR]
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO = 'https://github.com/luau-lang/luau.git'
COMMIT = 'c87565b8b0db5d5d5bfd2b8b00ad6573e5d06d9f'     # the revision the patch was made against


def run(cmd, cwd=None):
    print('+', ' '.join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--build-dir', default=os.path.join(ROOT, 'bin', 'luau-src'))
    args = ap.parse_args()
    for tool in ('git', 'cmake'):
        if not shutil.which(tool):
            sys.exit('%s is required' % tool)
    src = args.build_dir
    if not os.path.isdir(os.path.join(src, '.git')):
        os.makedirs(os.path.dirname(src), exist_ok=True)
        run(['git', 'init', '-q', src])
        run(['git', 'remote', 'add', 'origin', REPO], cwd=src)
    run(['git', 'fetch', '-q', '--depth', '1', 'origin', COMMIT], cwd=src)
    run(['git', 'checkout', '-q', '-f', 'FETCH_HEAD'], cwd=src)
    run(['git', 'apply', os.path.join(HERE, 'luau_cli.patch')], cwd=src)
    build = os.path.join(src, 'build')
    os.makedirs(build, exist_ok=True)
    run(['cmake', '..', '-DCMAKE_BUILD_TYPE=Release', '-DLUAU_BUILD_TESTS=OFF'], cwd=build)
    run(['cmake', '--build', '.', '--target', 'Luau.Repl.CLI', '-j', str(os.cpu_count() or 2)], cwd=build)
    out = os.path.join(ROOT, 'bin')
    os.makedirs(out, exist_ok=True)
    exe = 'luau.exe' if os.name == 'nt' else 'luau'
    shutil.copy2(os.path.join(build, exe), os.path.join(out, exe))
    print('built', os.path.join(out, exe))


if __name__ == '__main__':
    main()
