#!/usr/bin/env python3
"""Regression check: run the pipeline on the protected inputs you have and compare each result with
the recovered file committed under deobfuscated/.

    python tests/regress.py DIR            # DIR holds the protected scripts
    python tests/regress.py DIR message_1  # only this one

The protected inputs are not in the repository.  An input matches a committed result by name:
DIR/message_1.txt (or .lua) is compared with deobfuscated/message_1.lua.  Header comments of the
committed file are ignored.  Exit status 0 when every input that was found reproduces exactly.
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from devm import run        # noqa: E402

# results that are known to be reproducible from a single file with default options
CASES = ['message_1']


def body(text):
    lines = text.split('\n')
    i = 0
    while i < len(lines) and (lines[i].startswith('--') or not lines[i].strip()):
        i += 1
    return '\n'.join(lines[i:]).rstrip('\n')


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    indir = argv[1]
    names = argv[2:] or CASES
    bad = 0
    for name in names:
        src = next((os.path.join(indir, name + ext) for ext in ('.txt', '.lua', '.luau')
                    if os.path.exists(os.path.join(indir, name + ext))), None)
        want = os.path.join(ROOT, 'deobfuscated', name + '.lua')
        if src is None or not os.path.exists(want):
            print('%-12s skipped (%s)' % (name, 'input not in ' + indir if src is None else 'no committed result'))
            continue
        t = time.time()
        with tempfile.TemporaryDirectory() as work:
            got = run.deobfuscate(src, work, log=lambda *a: None)
        with open(want, encoding='utf-8') as f:
            ok = body(f.read()) == body(got)
        print('%-12s %s  (%.0fs)' % (name, 'ok' if ok else 'DIFFERENT', time.time() - t))
        bad += not ok
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
