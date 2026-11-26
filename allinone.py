#!/usr/bin/env python3
"""Engine hand-over behind the all-in-one front end (deob.py).

Three engines live in this repository; deob.py looks at the input and picks one:

  static  deob.py itself.  Unwraps Luraph's compressed container and extracts the business
          functions a script kept as plain Lua inside the VM object ("form A").
  devm    devm/ (python3 -m devm.run).  Devirtualizer for scripts whose code runs on Luraph's
          VM (v15.1, v15.2): instrumented run in a mock Roblox/executor, partial evaluation of
          the interpreter per instruction, structuring, naming.
  deobf   deobf/ (python3 deobf/deob.py).  Dynamic engine for everything else: IronBrew1
          devirtualizer, generic behaviour trace for unknown obfuscators, and a second,
          independent Luraph v15 lifter that serves as the fallback when devm gives up.

Each engine runs as a child process (they keep global state, and a crash or a timeout of one
must not take the front end down).  Their own output is passed through unchanged.

Direct use, for a single engine:

    python3 allinone.py INPUT --engine devm|deobf|trace [-o OUT] [--work DIR] [--timeout S]
"""
import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# exit codes of the front end (deob.py documents the static ones: 2, 3, 4, 5, 7)
OK = 0              # a complete result was written
FAILED = 1          # no result
PARTIAL = 6         # a result was written, but it is partial (see the report that was printed)

ENGINES = ("auto", "static", "devm", "deobf", "trace")

TRACE_HEADER = "-- Deobfuscated by deobf (dynamic trace)"


def say(msg=""):
    print(msg, flush=True)


def default_output(inp):
    return os.path.splitext(inp)[0] + ".deobf.lua"


def _stream(cmd, timeout, cwd=HERE, env=None):
    """Run cmd, pass its output through line by line, return (exit status or None on timeout,
    the output as text)."""
    lines = []
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    timer = None
    timed_out = []
    if timeout:
        import threading

        def kill():
            timed_out.append(True)
            proc.kill()
        timer = threading.Timer(timeout, kill)
        timer.daemon = True
        timer.start()
    try:
        for line in proc.stdout:
            lines.append(line)
            sys.stdout.write("    " + line)
            sys.stdout.flush()
        proc.wait()
    finally:
        if timer:
            timer.cancel()
    return (None if timed_out else proc.returncode), "".join(lines)


def run_devm(inp, out, work=None, timeout=None, extra=()):
    """Devirtualize `inp` with devm.  Returns a dict:
      'ok'        a result file was written
      'complete'  the engine's own report says nothing is left unrecovered
      'report'    the report lines the engine printed (how the traced run ended, what is missing)
      'status'    exit status of the engine, None after a timeout
    Intermediate files go to `work` (kept) or to a temporary directory (removed)."""
    tmp = None
    if work is None:
        tmp = work = tempfile.mkdtemp(prefix="devm_")
    if os.path.exists(out):
        os.remove(out)
    cmd = [sys.executable, "-u", "-m", "devm.run", os.path.abspath(inp), "-o", os.path.abspath(out),
           "--work", os.path.abspath(work)] + list(extra)
    try:
        status, text = _stream(cmd, timeout)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    report = []
    seen = False
    for line in text.splitlines():
        if line.startswith("written "):
            seen = True
            continue
        if seen:
            report.append(line)
    ok = status == 0 and os.path.isfile(out) and os.path.getsize(out) > 0
    complete = ok and any(l.startswith("result: complete") for l in report)
    return {"ok": ok, "complete": complete, "report": report, "status": status}


def run_deobf(inp, out, timeout=None, trace_only=False, extra=()):
    """Run the deobf engine on `inp`.  Returns a dict:
      'ok'      a result file was written
      'kind'    'devirt' (lifted code), 'trace' (behaviour trace: only what ran) or None
      'status'  exit status of the engine, None after a timeout"""
    if os.path.exists(out):
        os.remove(out)
    cmd = [sys.executable, "-u", os.path.join(HERE, "deobf", "deob.py"), os.path.abspath(inp),
           "-o", os.path.abspath(out)]
    if trace_only:
        cmd.append("--no-devirt")
    cmd += list(extra)
    status, _text = _stream(cmd, timeout)
    ok = status == 0 and os.path.isfile(out) and os.path.getsize(out) > 0
    kind = None
    if ok:
        with open(out, encoding="utf-8", errors="replace") as f:
            head = f.read(400)
        kind = "trace" if TRACE_HEADER in head else "devirt"
    return {"ok": ok, "kind": kind, "status": status}


def detect_other(inp):
    """What the deobf engine's detector makes of the input: (plugin name, confidence, label).
    ('generic', 0.0, ...) for something it does not recognise."""
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "deobf", "deob.py"), os.path.abspath(inp), "--detect"],
                           capture_output=True, text=True, timeout=120)
        name, conf, label = r.stdout.strip().split("\t")
        return name, (0.0 if conf == "forced" else float(conf)), label
    except Exception:       # noqa: BLE001  detection is advisory
        return "generic", 0.0, "unknown"


def looks_like_lua(inp):
    """False for an input that is clearly not a script (binary data, empty file)."""
    try:
        with open(inp, "rb") as f:
            head = f.read(65536)
    except OSError:
        return False
    if not head.strip():
        return False
    return head.count(b"\0") == 0


def after_static(scan, rc, args):
    """Automatic mode: what to do once deob.py's static scan is through.

    `scan` is deob.Scan after deob.run() returned `rc`.  Returns None (the static result stands)
    or (engine, extra engine arguments).  Prints why.

    Nothing is handed over when the user asked for a look only (--list), for the static export
    anyway (--force), or for no execution at all (--no-run): the engines run the script in a mock
    environment."""
    if args.list or args.force or args.no_run:
        return None
    form = getattr(scan, "form", None)
    if rc == 4 and form == "vm":
        if scan.bare_chunk():
            buf = scan.sibling_buffer()
            if not buf:
                return None         # the advice printed above says how to supply the buffer
            say()
            say("[auto] VM chunk + 同目录的数据 buffer → 自动交给去虚拟化器 devm"
                " / handing the chunk and its buffer to the devirtualizer")
            return "devm", ["--chunk-arg", str(buf)]
        say()
        say("[auto] 脚本主体在 VM 里 → 自动交给去虚拟化器 devm（只做静态分析：--engine static）")
        say("       the script runs on Luraph's VM: handing over to the devirtualizer (static only: --engine static)")
        return "devm", []
    if rc == 2 and not scan.evidence():
        if not looks_like_lua(str(scan.inp)):
            return None
        name, conf, label = detect_other(str(scan.inp))
        if name not in ("generic", "luraph_v15") and conf >= 0.5:
            say()
            say("[auto] 不是 Luraph，识别为 %s（%.2f）→ 交给动态引擎 deobf / not Luraph: detected %s, "
                "handing over to the dynamic engine" % (label, conf, label))
            return "deobf", []
        say()
        say("[auto] 没有认出混淆器，什么都没有运行。想看脚本在模拟环境里做了什么：--engine trace")
        say("       no known obfuscator and nothing was run.  For a behaviour trace (the script is run in a"
            " mock environment): --engine trace")
    return None


def hand_over(inp, out, engine, work=None, timeout=None, extra=()):
    """Run one engine (or, for engine == 'devm', devm with the deobf engine as fallback) and
    print what came of it.  Returns the front end's exit code."""
    out = out or default_output(inp)
    if engine == "devm":
        say("[devm] devirtualizing with devm (python3 -m devm.run); big scripts take several minutes")
        res = run_devm(inp, out, work=work, timeout=timeout, extra=extra)
        if res["ok"]:
            if res["complete"]:
                say("[✓] devirtualized → %s" % out)
                return OK
            say("[!] devirtualized, PARTIAL → %s  (the report above says what is missing)" % out)
            return PARTIAL
        why = "timed out after %d s" % timeout if res["status"] is None else "ended with status %s and no result" % res["status"]
        say("[!] devm %s; trying the second engine (deobf)" % why)
        engine = "deobf"
        extra = ()
    if engine in ("deobf", "trace"):
        say("[deobf] running the dynamic engine (python3 deobf/deob.py%s)" % (" --no-devirt" if engine == "trace" else ""))
        res = run_deobf(inp, out, timeout=timeout, trace_only=engine == "trace", extra=extra)
        if not res["ok"]:
            say("[✗] the dynamic engine %s" % ("timed out" if res["status"] is None else "gave no result (status %s)" % res["status"]))
            return FAILED
        if res["kind"] == "devirt":
            say("[✓] devirtualized → %s" % out)
            return OK
        say("[!] behaviour trace → %s" % out)
        say("    A trace shows what the script did in the mock environment on this run: branches that")
        say("    were not taken are missing.  It is not the script's source.")
        return PARTIAL
    raise ValueError("unknown engine %r" % engine)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run one engine of the all-in-one deobfuscator directly "
                                             "(deob.py picks the engine automatically).")
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    ap.add_argument("--engine", choices=("devm", "deobf", "trace"), required=True)
    ap.add_argument("--work", help="directory for intermediate files (default: a temporary one)")
    ap.add_argument("--timeout", type=int, help="seconds before the engine is stopped (default: none)")
    ap.add_argument("--engine-args", default="", help="extra options for the engine, as one string")
    a = ap.parse_args(argv)
    if not os.path.isfile(a.input):
        say("[✗] no such file: %s" % a.input)
        return FAILED
    return hand_over(a.input, a.output, a.engine, work=a.work, timeout=a.timeout, extra=shlex.split(a.engine_args))


if __name__ == "__main__":
    sys.exit(main())
