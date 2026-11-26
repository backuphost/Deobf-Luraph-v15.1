#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deobf_plus.py (repository root): forwards to libs/deobf_plus.py

There used to be two different copies of this module: an older one here and the current one
in libs/.  deob.py puts the repository root first on sys.path, so `import deobf_plus` loaded
the OLD copy, whose formatter glued `;` to the next statement.  To make that impossible the
code now exists once, in libs/deobf_plus.py; this file loads exactly that file and re-exports
it, so `import deobf_plus` and `python3 deobf_plus.py ...` behave the same from anywhere.

用法: python3 deobf_plus.py <input.lua|.txt> -o <output.lua> [--no-rename] [--no-fold] [--map <map.md>]
"""
import importlib.util
import sys
from pathlib import Path

_IMPL = Path(__file__).resolve().parent / 'libs' / 'deobf_plus.py'
_spec = importlib.util.spec_from_file_location('_deobf_plus_impl', str(_IMPL))
_impl = importlib.util.module_from_spec(_spec)
sys.modules['_deobf_plus_impl'] = _impl
_spec.loader.exec_module(_impl)

globals().update({k: v for k, v in vars(_impl).items() if not k.startswith('__')})
IMPLEMENTATION = str(_IMPL)

if __name__ == '__main__':
    sys.exit(_impl.main())
