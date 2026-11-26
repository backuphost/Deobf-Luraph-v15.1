#!/usr/bin/env python3
"""Run every test of the static tool, one after the other, and print one line per test file.

    python3 tests/static_all.py [--quick]

  static_unit.py        lexer, formatter, renamer, slot hints, compile-only helper
  static_fold.py        constant folding against the interpreter (directed cases + fuzz)
  static_reslot.py      slot comments / --inline never change what a program prints (fuzz)
  static_sandbox.py     hostile inputs cannot write, read or load files through any runner
  static_deob.py        deob.py on synthetic VM objects, containers and plain scripts
  static_roundtrip.py   deob.py on examples/layer3.lua and examples/v15.2/sample.luau, plus
                        the formatter and renamer over every Lua file of the repository
                        (skipped with --quick; about a minute)

Needs bin/luau and bin/luau-ast (./setup.sh).  Exit status 0 when every file passes.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = [
    ('static_unit.py', []),
    ('static_fold.py', []),
    ('static_reslot.py', []),
    ('static_sandbox.py', []),
    ('static_deob.py', []),
    ('static_roundtrip.py', ['--corpus']),
]


def main():
    quick = '--quick' in sys.argv
    failed = 0
    for name, args in TESTS:
        if quick and name == 'static_roundtrip.py':
            print('%-22s skipped (--quick)' % name)
            continue
        t = time.time()
        r = subprocess.run([sys.executable, '-B', os.path.join(HERE, name)] + args,
                           capture_output=True, text=True, encoding='utf-8', errors='replace')
        lines = [ln for ln in (r.stdout + r.stderr).strip().splitlines() if ln.strip()]
        last = lines[-1] if lines else '(no output)'
        ok = r.returncode == 0
        failed += not ok
        print('%-22s %s  %5.1fs  %s' % (name, 'ok  ' if ok else 'FAIL', time.time() - t, last[:110]), flush=True)
        if not ok:
            for ln in lines[-12:]:
                print('    | ' + ln[:200])
    print('%d test file(s) failed' % failed)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
