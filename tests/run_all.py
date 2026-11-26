#!/usr/bin/env python3
"""Run the checks of every part of the repository, one line per check.

    python3 tests/run_all.py            quick checks (a few minutes)
    python3 tests/run_all.py --full     also the slow ones (full devirtualization of the samples)
    python3 tests/run_all.py NAME ...   only the checks whose name contains one of NAME

  interpreter   bin/luau and bin/luau-ast run and carry both patches (build_luau.py --check)
  static        the static tool's own suite (tests/static_all.py)
  devm          devm's container, model and harness checks for Luraph v15.2 (tests/devm_v152.py)
  devm-v15      devm's traced-run snapshots and jump hubs for the v15.1 corpus builds (tests/devm_v15_corpus.py)
  structurer    deobf's structurer against random control-flow graphs (research/structure_fuzz.py)
  frontend      deob.py picks the right engine and reports truthfully (tests/allinone_test.py)

Everything needs the interpreter (./setup.sh).  Exit status 0 when every check passes.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable


def checks(full):
    """[(name, command, extra environment, timeout in seconds)]"""
    out = [
        ("interpreter", [PY, "build_luau.py", "--check"], {}, 120),
        ("static", [PY, "-B", "tests/static_all.py"] + ([] if full else ["--quick"]), {}, 1800),
        ("devm", [PY, "-B", "tests/devm_v152.py"], {}, 600),
        ("devm-v15", [PY, "-B", "tests/devm_v15_corpus.py"], {}, 600),
        ("structurer", [PY, "-B", "deobf/research/structure_fuzz.py", "0", "3000" if full else "500"], {}, 1800),
        ("frontend", [PY, "-B", "tests/allinone_test.py"], {"ALLINONE_FULL": "1"} if full else {}, 3600),
    ]
    if full:
        out.insert(3, ("devm-full", [PY, "-B", "tests/devm_v152.py", "V152Full", "Layer3Full"],
                       {"DEVM_V152_FULL": "1"}, 3600))
    return out


def main(argv):
    full = "--full" in argv
    only = [a for a in argv if not a.startswith("--")]
    bad = 0
    for name, cmd, env, limit in checks(full):
        if only and not any(o in name for o in only):
            continue
        script = next(a for a in cmd if a.endswith(".py"))
        if not os.path.exists(os.path.join(ROOT, script)):
            print("%-12s skipped (%s is not there)" % (name, script))
            continue
        t = time.time()
        try:
            r = subprocess.run(cmd, cwd=ROOT, env=dict(os.environ, **env), capture_output=True, text=True,
                               errors="replace", timeout=limit)
            ok, text = r.returncode == 0, r.stdout + r.stderr
        except subprocess.TimeoutExpired as e:
            got = e.stdout or ""
            ok, text = False, "timed out after %d s\n%s" % (limit, got.decode("utf-8", "replace") if isinstance(got, bytes) else got)
        print("%-12s %s  (%.0f s)" % (name, "ok" if ok else "FAILED", time.time() - t), flush=True)
        if not ok:
            bad += 1
            for line in text.strip().split("\n")[-25:]:
                print("    " + line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
