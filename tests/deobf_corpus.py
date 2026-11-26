#!/usr/bin/env python3
"""Checks of the second engine (deobf/): its contract with the front end and the lifter fixes
that the public Luraph corpus asked for.

    python3 tests/deobf_corpus.py [NAME ...]      only the checks whose name contains one of NAME

Without the corpus: the final `result:` / `deobf:` line, the time limit, no harness process left
behind when the engine is killed, and unit checks of the lifter changes on hand-made input.
With the corpus (environment variable LURAPH_CORPUS = the directory that holds one directory per
Luraph version): the engine on files picked by directory and property at run time. Nothing of a
corpus file is kept or printed here; the scripts are run only inside the engine's own sandbox.

Exit status 0 when every check passes (skipped checks do not fail).
"""
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEOBF = os.path.join(ROOT, "deobf")
DEOB = os.path.join(DEOBF, "deob.py")
PY = sys.executable
CORPUS = os.environ.get("LURAPH_CORPUS")
SLOW = 4 if os.environ.get("DEOBF_TEST_SLOW") else 1     # stretch the time limits on a loaded machine

sys.path.insert(0, DEOBF)


class Skip(Exception):
    pass


def engine(args, timeout=120):
    """(exit status, stdout lines, stderr lines) of deob.py."""
    r = subprocess.run([PY, "-B", DEOB] + args, capture_output=True, text=True, errors="replace",
                       timeout=timeout * SLOW, cwd=ROOT)
    return r.returncode, r.stdout.strip().split("\n"), r.stderr.strip().split("\n")


def parses(path):
    ast = os.path.join(ROOT, "bin", "luau-ast")
    r = subprocess.run([ast, path], capture_output=True, text=True, errors="replace", timeout=300)
    return r.returncode == 0 and "Parse errors" not in r.stderr[:4000]


def write(d, name, text):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


# --------------------------------------------------------------------------
# without the corpus

def check_no_syntax_warnings():
    """Every module compiles without a warning (an invalid escape sequence is one)."""
    bad = []
    for base, _, files in os.walk(DEOBF):
        for name in files:
            if not name.endswith(".py"):
                continue
            p = os.path.join(base, name)
            with open(p, encoding="utf-8") as f:
                src = f.read()
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                compile(src, p, "exec")
            bad += ["%s:%s %s" % (os.path.relpath(p, ROOT), x.lineno, x.message) for x in w]
    assert not bad, bad


def check_result_line_trace():
    """A behaviour trace is reported as PARTIAL, on the last line of standard output, status 0."""
    with tempfile.TemporaryDirectory() as d:
        src = write(d, "own.lua", 'print("own test script")\n')
        out = os.path.join(d, "out.lua")
        rc, so, se = engine([src, "-o", out])
        assert rc == 0, (rc, se[-3:])
        assert so[-1].startswith("result: PARTIAL: the output is the behaviour trace"), so[-1]
        assert "run status: finished" in so[-1], so[-1]
        assert os.path.getsize(out) > 0
        assert any(ln.startswith("[*] run status: finished") for ln in se), se


def check_failure_line():
    """Nothing produced: status 1 and one last line `deobf: <reason>`, no traceback."""
    with tempfile.TemporaryDirectory() as d:
        rc, so, se = engine([os.path.join(d, "missing.lua"), "-o", os.path.join(d, "out.lua")])
        assert rc == 1, rc
        assert se[-1].startswith("deobf: cannot read the input"), se[-1]
        assert not any("Traceback" in ln for ln in se), se
        assert not any(ln.startswith("result:") for ln in so), so
        # a script that fails to compile still gives a trace of that (the engine's own report)
        src = write(d, "broken.lua", "this is not Lua (\n")
        rc, so, se = engine([src, "-o", os.path.join(d, "out.lua")])
        assert (rc == 0 and so[-1].startswith("result: PARTIAL")) or (rc == 1 and se[-1].startswith("deobf: ")), \
            (rc, so[-1:], se[-1:])


def check_time_limit_run():
    """--time-limit ends a run that does not end by itself, with the reason in the last line."""
    with tempfile.TemporaryDirectory() as d:
        # (half a minute of the script's own computing: the run's other limits are set far above)
        src = write(d, "own_clock.lua", "local t = os.clock() while os.clock() - t < 30 do end print('done')\n")
        t = time.time()
        rc, so, se = engine([src, "-o", os.path.join(d, "out.lua"), "--time-limit", "3", "--budget", "600",
                             "--timeout", "600"])
        took = time.time() - t
        assert 2.5 < took < 25 * SLOW, took
        assert rc == 1 and se[-1] == "deobf: time limit of 3 s reached during a traced run", (rc, se[-1])
        assert not any(ln.startswith("result:") for ln in so), so


def check_time_limit_lift():
    """The lift thread is given up at the deadline (backend.run_big_stack raises limits.TimeLimit)."""
    code = ("import sys, time; sys.path.insert(0, %r)\n"
            "import limits, backend\n"
            "limits.set_limit(1)\n"
            "t = time.time()\n"
            "try:\n"
            "    backend.run_big_stack(time.sleep, 30)\n"
            "    print('no limit')\n"
            "except limits.TimeLimit as e:\n"
            "    print('limit %%.1f %%s' %% (time.time() - t, e))\n"
            "sys.stdout.flush(); import os; os._exit(0)\n" % DEOBF)
    r = subprocess.run([PY, "-B", "-c", code], capture_output=True, text=True, timeout=60 * SLOW)
    m = re.match(r"limit (\d+\.\d) time limit of 1 s reached while lifting", r.stdout.strip())
    assert m and float(m.group(1)) < 10 * SLOW, (r.stdout, r.stderr[-300:])
    # without a limit nothing changes
    code = ("import sys; sys.path.insert(0, %r)\nimport backend\nprint(backend.run_big_stack(lambda a, b: a + b, 2, 3))\n"
            % DEOBF)
    r = subprocess.run([PY, "-B", "-c", code], capture_output=True, text=True, timeout=60 * SLOW)
    assert r.stdout.strip() == "5", (r.stdout, r.stderr[-300:])


def _harness_pids(tag):
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                cmd = f.read()
        except OSError:
            continue
        if tag.encode() in cmd and b"harness" in cmd:
            out.append(int(pid))
    return out


def check_no_orphan():
    """Killing the engine (SIGKILL, SIGTERM) leaves no harness process behind."""
    if not sys.platform.startswith("linux"):
        raise Skip("needs /proc")
    for sig in (signal.SIGKILL, signal.SIGTERM):
        with tempfile.TemporaryDirectory() as d:
            tag = "own_spin_%d_%d" % (os.getpid(), int(sig))
            # (a busy loop: the harness process never ends by itself within the test)
            src = write(d, tag + ".lua", "while true do end\n")
            # (TMPDIR: the engine's work directory is made in here; a SIGKILL leaves it behind)
            work = os.path.join(d, "tmp")
            os.mkdir(work)
            proc = subprocess.Popen([PY, "-B", DEOB, src, "-o", os.path.join(d, "out.lua")], cwd=ROOT,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                    env=dict(os.environ, TMPDIR=work))
            try:
                deadline = time.time() + 15 * SLOW
                while not _harness_pids(tag) and time.time() < deadline and proc.poll() is None:
                    time.sleep(0.1)
                assert _harness_pids(tag), "the harness did not start"
                proc.send_signal(sig)
                err = proc.communicate(timeout=20 * SLOW)[1]
                deadline = time.time() + 5 * SLOW
                while _harness_pids(tag) and time.time() < deadline:
                    time.sleep(0.1)
                left = _harness_pids(tag)
                assert not left, "harness left running after signal %d: %s" % (sig, left)
                if sig == signal.SIGTERM:
                    assert proc.returncode == 128 + sig, proc.returncode
                    assert err.strip().split("\n")[-1] == "deobf: terminated by signal %d" % sig, err[-200:]
                    assert os.listdir(work) == [], "work directory left after SIGTERM: %s" % os.listdir(work)
            finally:
                if proc.poll() is None:
                    proc.kill()
                for pid in _harness_pids(tag):
                    os.kill(pid, signal.SIGKILL)


def check_pack_site():
    """A pack's identity for the walk does not depend on the visit that made it."""
    from obfuscators.luraph_v15 import devirt
    a = devirt.pack_site("pack(T2651_13556_1[1...])")
    assert a == devirt.pack_site("pack(T2651_17910_1[1...])") == "pack(T2651_1[1...])", a
    assert devirt.pack_site("pack(T2651_7_1[1...])") != devirt.pack_site("pack(T2652_7_1[1...])")
    assert devirt.pack_site("pack(vararg[1...])") == "pack(vararg[1...])"
    assert devirt.pack_site("pack(T-12_5_2[1], T3_9_1[2...])") == "pack(T-12_2[1], T3_1[2...])"


def check_chunk_params():
    """The main function's parameters at the top level never give `local ... = ...`."""
    from obfuscators.luraph_v15 import devirt
    assert devirt.chunk_lines([], ["x()"]) == ["x()"]
    assert devirt.chunk_lines(["..."], ["x(...)"]) == ["x(...)"]
    assert devirt.chunk_lines(["a", "b"], ["x(a)"]) == ["local a, b = ...", "x(a)"]
    assert devirt.chunk_lines(["a", "..."], ["x(a, ...)"]) == ["return (function(a, ...)", "\tx(a, ...)", "end)(...)"]
    with tempfile.TemporaryDirectory() as d:
        for params in ([], ["..."], ["a", "b"], ["a", "..."]):
            p = write(d, "c.lua", "\n".join(devirt.chunk_lines(params, ["print(%s)" % ", ".join(params or ["1"])])) + "\n")
            assert parses(p), params


def check_run_once_forin():
    """A generic `for` whose body always returns: the hidden iterator call becomes `f(s, ctl)`."""
    import ir as D
    import loops
    import structure
    from luasym import Const, Reg, Pseudo, Bin, TempVal, Multi

    def graph(extra=()):
        h, body, ex = structure.Block(1), structure.Block(2), structure.Block(3)
        it = Pseudo("a", 1)
        h.stmts = [D.Assign(Reg(9), Const(1)),
                   D.Assign(it, D.GenIter(Multi([Reg(0), Reg(4), Reg(5), Reg(6)]))),
                   D.Assign(Reg(7), Const(None))] + list(extra) + [D.CallStmt("T1", Pseudo("a", 1), Multi([]))]
        h.kind, h.cond, h.succ = "cond", TempVal("T1", 1), [2, 3]
        body.stmts = [D.Assign(Reg(7), TempVal("T1", 2)), D.Assign(Reg(8), TempVal("T1", 3))]
        body.kind, body.values = "ret", Multi([Reg(7), Bin("Add", TempVal("T1", 3), Const(1))])
        ex.kind, ex.values = "ret", Multi([])
        return {1: h, 2: body, 3: ex}

    blocks = graph()
    h, body = blocks[1], blocks[2]
    assert loops._run_once_forin(h, blocks, D) is False
    call = h.stmts[1]
    assert [type(s).__name__ for s in h.stmts] == ["Assign", "CallStmt", "Assign"], h.stmts
    assert isinstance(call.fn, Reg) and call.fn.n == 4 and [x.n for x in call.args.items] == [5, 6], call.__dict__
    assert call.t == "T1" and call.args.tail is None
    c = h.cond
    assert isinstance(c, Bin) and c.op == "CompareNe" and c.a == TempVal("T1", 1) and isinstance(c.b, Const) \
        and c.b.v is None, c.__dict__
    assert body.stmts[0].value == TempVal("T1", 1) and body.stmts[1].value == TempVal("T1", 2)
    assert body.values.items[1].a == TempVal("T1", 2)
    # not when something else than register moves stands between the prep and the call
    blocks = graph([D.CallStmt("T0", Reg(3), Multi([]))])
    before = list(blocks[1].stmts)
    loops._run_once_forin(blocks[1], blocks, D)
    assert blocks[1].stmts == before and isinstance(blocks[1].cond, TempVal)
    # the whole of recognize(): no call of the hidden slot is left
    blocks = graph()
    for b in blocks.values():
        b.preds = []
    blocks[2].preds = blocks[3].preds = [1]
    loops.recognize(1, blocks, D)
    assert not any(isinstance(s, D.CallStmt) and isinstance(s.fn, Pseudo) for s in blocks[1].stmts)


# --------------------------------------------------------------------------
# with the corpus (files picked by directory and property)

def _version_files(version):
    if not CORPUS:
        raise Skip("LURAPH_CORPUS is not set")
    d = os.path.join(CORPUS, version)
    if not os.path.isdir(d):
        raise Skip("no %s in the corpus" % version)
    files = [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.endswith(".lua")]
    return sorted(files, key=lambda p: (os.path.getsize(p), p))


def _is_container(path):
    with open(path, "rb") as f:
        head = f.read(4000)
    body = re.sub(rb"\A\s*--[^\n]*\n\s*", b"", head)
    return body.startswith(b"return(function()") and b"loadstring" in body


def check_corpus_small_script():
    """The smallest v15.0 file (a plain VM object): lifted whole, `result: complete`, the text parses."""
    path = next((p for p in _version_files("v15.0") if not _is_container(p)), None)
    if path is None:
        raise Skip("no plain v15.0 file")
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "out.lua")
        rc, so, se = engine([path, "-o", out, "--time-limit", str(600 * SLOW)], timeout=700)
        assert rc == 0 and so[-1] == "result: complete", (rc, so[-1:], se[-2:])
        assert any("0 unlifted blocks, 0 unstructured jumps" in ln for ln in se), se[-6:]
        with open(out, encoding="utf-8") as f:
            text = f.read()
        assert "dynamic trace" not in text[:300] and parses(out)


def check_corpus_stopped_loader():
    """The smallest v15.0 container: its run stops inside Luraph's start-up code. The engine must
    end (it used to walk for ever), say PARTIAL with the reason, and write the trace, not Luraph's
    start-up program as the script."""
    path = next((p for p in _version_files("v15.0") if _is_container(p)), None)
    if path is None:
        raise Skip("no v15.0 container")
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "out.lua")
        t = time.time()
        rc, so, se = engine([path, "-o", out, "--time-limit", str(600 * SLOW)], timeout=700)
        assert rc == 0 and so[-1].startswith("result: PARTIAL: "), (rc, so[-1:], se[-2:])
        assert time.time() - t < 400 * SLOW
        if "start-up code" in so[-1]:
            assert "behaviour trace" in so[-1], so[-1]
            with open(out, encoding="utf-8") as f:
                assert "dynamic trace" in f.read(300)
        assert any(ln.startswith("[*] run status: ") for ln in se), se[-6:]
        assert parses(out)


CHECKS = [
    ("syntax-warnings", check_no_syntax_warnings),
    ("pack-site", check_pack_site),
    ("chunk-params", check_chunk_params),
    ("run-once-forin", check_run_once_forin),
    ("result-line", check_result_line_trace),
    ("failure-line", check_failure_line),
    ("time-limit-lift", check_time_limit_lift),
    ("time-limit-run", check_time_limit_run),
    ("no-orphan", check_no_orphan),
    ("corpus-small-script", check_corpus_small_script),
    ("corpus-stopped-loader", check_corpus_stopped_loader),
]


def main(argv):
    only = [a for a in argv if not a.startswith("--")]
    bad = 0
    for name, fn in CHECKS:
        if only and not any(o in name for o in only):
            continue
        t = time.time()
        try:
            fn()
            res = "ok"
        except Skip as e:
            res = "skipped (%s)" % e
        except Exception as e:  # noqa: BLE001 - one line per check
            res = "FAILED: %s: %s" % (type(e).__name__, str(e)[:600])
            bad += 1
        print("%-24s %s  (%.0f s)" % (name, res, time.time() - t), flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
