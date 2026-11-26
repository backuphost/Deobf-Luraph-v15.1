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
must not take the front end down).  Their own output is passed through (a Python traceback is
cut down to its last line).  A result is judged before it is reported: the engine's own
`result:` line, unlifted blocks, a parse check, whether there is a line of code at all.

Which engines, in which order (engine_order): a v14 VM goes to devm only; v15.0 to deobf first,
then devm; everything else to devm, then deobf.  When no engine produced a result the exit code
is 4 and the last lines are the engines' own reasons (`devm: <stage>: <reason>`).

Direct use, for a single engine:

    python3 allinone.py INPUT --engine devm|deobf|trace [-o OUT] [--work DIR] [--timeout S]
"""
import argparse
import os
import re
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
NO_RESULT = 4       # a VM script, and no engine produced a result
UNPROCESSABLE = 8   # Luraph, but the input cannot be processed as given: truncated or corrupt text,
                    # a corrupt payload, a build for Lua 5.3/5.4 or LuaJIT that Luau cannot read
NOT_RECOGNISED = 2  # not Luraph (also: an imitation, another obfuscator behind a Luraph header)

ENGINES = ("auto", "static", "devm", "deobf", "trace")

TRACE_HEADER = "-- Deobfuscated by deobf (dynamic trace)"


def say(msg=""):
    print(msg, flush=True)


def default_output(inp):
    return os.path.splitext(inp)[0] + ".deobf.lua"


def _descendants(pid):
    """Process ids of every descendant of `pid` (Linux: read from /proc; elsewhere [])."""
    kids = {}
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % name, "rb") as f:
                    stat = f.read().decode("latin-1")
                ppid = int(stat[stat.rindex(")") + 2:].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            kids.setdefault(ppid, []).append(int(name))
    except OSError:
        return []
    out, todo = [], [pid]
    while todo:
        for k in kids.get(todo.pop(), ()):
            out.append(k)
            todo.append(k)
    return out


def _kill_tree(proc):
    """Stop an engine and everything it started.  (An engine that is merely killed leaves its
    interpreter child, `bin/luau devm/harness.lua`, running at full speed.)"""
    import signal
    victims = _descendants(proc.pid)
    try:
        proc.kill()
    except OSError:
        pass
    for pid in victims + _descendants(proc.pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _on_term(signum, _frame):
    raise SystemExit(128 + signum)


def _stream(cmd, timeout, cwd=HERE, env=None):
    """Run cmd, pass its output through line by line, return (exit status or None on timeout,
    the output as text).  A Python traceback of the engine is not passed through line by
    line: its last line (the exception) is printed, marked as such; the full text is in the
    returned output."""
    lines = []
    # SIGTERM to the front end must not leave the engine (and the interpreter it started) behind:
    # turn it into an exception, so that the clean-up below runs.  (SIGKILL cannot be caught.)
    import signal
    old_term = None
    try:
        old_term = signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError, AttributeError):      # not the main thread / no such signal
        old_term = None
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    timer = None
    timed_out = []
    if timeout:
        import threading

        def kill():
            timed_out.append(True)
            _kill_tree(proc)
        timer = threading.Timer(timeout, kill)
        timer.daemon = True
        timer.start()
    in_tb = False
    try:
        for line in proc.stdout:
            lines.append(line)
            if line.startswith("Traceback (most recent call last):"):
                in_tb = True
                continue
            if in_tb:
                if line[:1] in (" ", "\t") or not line.strip():
                    continue
                in_tb = False
                sys.stdout.write("    [engine error] " + line)
                sys.stdout.flush()
                continue
            sys.stdout.write("    " + line)
            sys.stdout.flush()
        proc.wait()
    except BaseException:
        _kill_tree(proc)        # interrupted (Ctrl-C, SIGTERM as KeyboardInterrupt/SystemExit): take the engine down too
        raise
    finally:
        if timer:
            timer.cancel()
        if old_term is not None:
            try:
                signal.signal(signal.SIGTERM, old_term)
            except (ValueError, OSError):
                pass
    return (None if timed_out else proc.returncode), "".join(lines)


_REASON_LINE = re.compile(r"^(?:devm|deobf): \S.*$")


def last_reason(text, engine):
    """The engine's own last word on why there is no result: its final `devm: <stage>:
    <reason>` / `deobf: <reason>` line when it prints one, else the exception line of a
    Python traceback, else its last non-empty line."""
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    for l in reversed(lines):
        if _REASON_LINE.match(l):
            return l
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("Traceback (most recent call last):"):
            for l in lines[i + 1:]:
                if l[:1] not in (" ", "\t"):
                    return "%s: %s" % (engine, l)
    for l in reversed(lines):
        if l.startswith("[!]") or l.startswith("[✗]"):
            return "%s: %s" % (engine, l.lstrip("[!✗] ").strip())
    return "%s: %s" % (engine, lines[-1] if lines else "no output")


# a line that is only the frame of a function or block: no statement of the script
_SHELL_LINE = re.compile(r"^(?:(?:local\s+)?(?:function\b[^()]*|[\w.]+\s*=\s*function\s*)\([^()]*\)|end[);,]*|do|else|"
                         r"repeat|return|then)$")


def code_lines(path, substance=False):
    """Number of lines of a result file that are code (not blank, not a `--` comment).
    substance=True: lines that are only the frame of a function or block (`local v1 =
    function(p1, p2)`, `end`, `return`) do not count either: a result made of empty function
    shells around "could not be lifted" comments has no line of the script in it."""
    n = 0
    in_long = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                t = line.strip()
                if in_long is not None:
                    if in_long in t:
                        in_long = None
                    continue
                if not t:
                    continue
                if t.startswith("--"):
                    m = re.match(r"--\[(=*)\[", t)
                    if m and ("]" + m.group(1) + "]") not in t:
                        in_long = "]" + m.group(1) + "]"
                    continue
                if substance and _SHELL_LINE.match(t.split("--", 1)[0].strip() or t):
                    continue
                n += 1
    except OSError:
        return 0
    return n


def parses(path):
    """True / False: does the result file parse (bin/luau-ast; it reports on stderr and by its
    exit status)?  None when the parser is not there."""
    try:
        import luaubin
        ast = luaubin.find_luau_ast()
    except Exception:       # noqa: BLE001
        ast = None
    if not ast:
        return None
    try:
        r = subprocess.run([ast, path], capture_output=True, text=True, errors="replace", timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.returncode == 0 and "Parse errors" not in (r.stderr[:4000] + r.stdout[:4000])


def run_devm(inp, out, work=None, timeout=None, extra=()):
    """Devirtualize `inp` with devm.  Returns a dict:
      'ok'        a result file was written
      'complete'  the engine's own report says nothing is left unrecovered
      'report'    the report lines the engine printed (how the traced run ended, what is missing)
      'status'    exit status of the engine, None after a timeout
      'reason'    without a result: the engine's last reason line (`devm: <stage>: <reason>`)
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
    result = [l for l in text.splitlines() if l.startswith("result: ")]
    complete = ok and bool(result) and result[-1].startswith("result: complete")
    why = []
    if ok and code_lines(out, substance=True) == 0:
        ok, complete = False, False
        why.append("devm: the result file holds no code of the script (comments and empty function shells only)")
    elif ok and complete and parses(out) is False:
        complete = False
        why.append("the result does not parse (bin/luau-ast)")
    reason = None
    if not ok:
        reason = why[0] if why else ("devm: timed out after %d s" % timeout if status is None
                                     else last_reason(text, "devm"))
    return {"ok": ok, "complete": complete, "report": report, "status": status, "reason": reason,
            "why": why, "engine": "devm"}


_UNLIFTED = re.compile(r"(\d+) functions.*?, (\d+) unlifted blocks(?:, (\d+) unstructured jumps)?")
_RUN_STATUS = re.compile(r"run status: *(.*)")


def judge_deobf(out, text):
    """What the second engine's result is worth: ('complete' | 'partial' | 'nothing', [why]).

    Its own final line decides when it prints one (`result: complete` / `result: PARTIAL: <what
    is missing>`).  Otherwise: a behaviour trace is partial by nature; a lift with unlifted
    blocks or unstructured jumps, or after a run that did not end by itself, is partial; a
    result that does not parse is partial; a file without a line of code is nothing."""
    if not (os.path.isfile(out) and os.path.getsize(out) > 0):
        return "nothing", []
    n = code_lines(out, substance=True)
    if n == 0:
        return "nothing", ["the result file holds no code of the script (comments and empty function shells only)"]
    why = []
    with open(out, encoding="utf-8", errors="replace") as f:
        head = f.read(400)
    if TRACE_HEADER in head:
        why.append("a behaviour trace, not lifted code")
    lines = text.splitlines()
    result = [l for l in lines if l.startswith("result: ")]
    if result:
        last = result[-1]
        if last.startswith("result: complete") and not why:
            verdict = "complete"
        else:
            verdict = "partial"
            if last.startswith("result: PARTIAL"):
                why.append(last[len("result: PARTIAL"):].lstrip(": ").strip() or "the engine reports a partial result")
    else:
        counts = [m for m in (_UNLIFTED.search(l) for l in lines) if m]
        if counts:
            m = counts[-1]
            if int(m.group(2)):
                why.append("%s unlifted blocks" % m.group(2))
            if m.group(3) and int(m.group(3)):
                why.append("%s unstructured jumps" % m.group(3))
        status = [m.group(1).strip() for m in (_RUN_STATUS.search(l) for l in lines) if m]
        if status and not status[-1].startswith("finished"):
            why.append("the traced run did not end by itself: " + status[-1][:160])
        if any("devirtualiz" in l and "failed" in l for l in lines):
            why.append("the lift failed and the engine fell back")
        verdict = "partial" if why else "complete"
    if parses(out) is False:
        why.append("the result does not parse (bin/luau-ast)")
        verdict = "partial"
    return verdict, why


def run_deobf(inp, out, timeout=None, trace_only=False, extra=()):
    """Run the deobf engine on `inp`.  Returns a dict:
      'ok'        a result file with code in it was written
      'complete'  judge_deobf says nothing is known to be missing
      'kind'      'devirt' (lifted code), 'trace' (behaviour trace: only what ran) or None
      'why'       what is missing from a partial result
      'status'    exit status of the engine, None after a timeout
      'reason'    without a result: the engine's last reason line"""
    if os.path.exists(out):
        os.remove(out)
    cmd = [sys.executable, "-u", os.path.join(HERE, "deobf", "deob.py"), os.path.abspath(inp),
           "-o", os.path.abspath(out)]
    if trace_only:
        cmd.append("--no-devirt")
    cmd += list(extra)
    status, text = _stream(cmd, timeout)
    verdict, why = ("nothing", []) if status != 0 else judge_deobf(out, text)
    ok = verdict != "nothing"
    kind = None
    if ok:
        with open(out, encoding="utf-8", errors="replace") as f:
            head = f.read(400)
        kind = "trace" if TRACE_HEADER in head else "devirt"
    reason = None
    if not ok:
        if os.path.exists(out) and status == 0:
            os.remove(out)          # three comment lines are not a result
        reason = ("deobf: timed out after %d s" % timeout if status is None
                  else ("deobf: " + why[0]) if why else last_reason(text, "deobf"))
    return {"ok": ok, "complete": verdict == "complete", "kind": kind, "why": why, "status": status,
            "reason": reason, "engine": "deobf"}


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
        order = engine_order(scan)
        say()
        if order == ("deobf", "devm"):
            say("[auto] Luraph v15.0 的 VM 布局 → 先交给第二个引擎 deobf（它能提升这种布局），没有完整结果再试 devm")
            say("       the v15.0 VM layout: the second engine (deobf) lifts it, so it goes first; devm after it"
                " (static only: --engine static)")
        elif order == ("devm",):
            say("[auto] Luraph v14 的 VM（脚本整体在 VM 里）→ 交给去虚拟化器 devm（只做静态分析：--engine static）")
            say("       a v14 VM: the whole script runs on it; handing over to the devirtualizer (static only: --engine static)")
        else:
            say("[auto] 脚本主体在 VM 里 → 自动交给去虚拟化器 devm（只做静态分析：--engine static）")
            say("       the script runs on Luraph's VM: handing over to the devirtualizer (static only: --engine static)")
        return ("devm" if order == ("devm", "deobf") else order), []
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


V14_LAYOUTS = ("fn-args", "fn-args-vararg", "table")


def engine_order(scan):
    """Engines to try for a VM script, in order.

    v14 layouts (function with arguments, table): devm only; the second engine's Luraph
    lifter is a v15 lifter and would hand back a trace of Luraph's own start-up code.
    v15.0 (by the header comment): the second engine first, since it lifts that layout and
    devm does not model it; devm after it.  Everything else: devm, then the second engine."""
    layout = scan.layout() if hasattr(scan, "layout") else None
    version = (getattr(scan, "header", None) or {}).get("version") or ""
    if layout in V14_LAYOUTS or (layout is None and version.startswith("14.")):
        return ("devm",)        # (a v14 container that did not open here: the layout follows the version)
    if version == "15.0" or version.startswith("15.0."):
        return ("deobf", "devm")
    return ("devm", "deobf")


ENGINE_TEXT = {
    "devm": "[devm] devirtualizing with devm (python3 -m devm.run); big scripts take several minutes",
    "deobf": "[deobf] running the dynamic engine (python3 deobf/deob.py)",
    "trace": "[deobf] running the dynamic engine (python3 deobf/deob.py --no-devirt)",
}
DEFAULT_TIMEOUT = 3600      # seconds per engine when --timeout is not given


def hand_over(inp, out, engine, work=None, timeout=None, extra=()):
    """Run the engines for `engine` in order and print what came of it.  `engine`: 'devm'
    (devm, then the deobf engine as fallback), 'deobf', 'trace', or a tuple of engine names to
    try in that order.  Returns the front end's exit code:

      OK         an engine wrote a result and reports it complete (and it parses)
      PARTIAL    a result was written but something is missing: the engine's report says what;
                 a behaviour trace; a lift with unlifted blocks; a result that does not parse
      NO_RESULT  no engine produced a result: the last lines give each engine's own reason

    A complete result ends the sequence.  After a partial result of the deobf engine a later
    engine still gets its turn (and its result is taken only when it is complete); a partial
    result of devm stands, since devm's report says exactly what is missing."""
    out = out or default_output(inp)
    order = {"devm": ("devm", "deobf"), "deobf": ("deobf",), "trace": ("trace",)}.get(engine, engine)
    if isinstance(order, str) or not order or any(e not in ("devm", "deobf", "trace") for e in order):
        raise ValueError("unknown engine %r" % (engine,))
    timeout = timeout or DEFAULT_TIMEOUT
    kept = None             # (result dict, path of its file): a partial result held back
    reasons = []
    tmpdir = tempfile.mkdtemp(prefix="deob_out_")
    try:
        for n, eng in enumerate(order):
            if n:
                say("[!] trying the next engine (%s)" % ("devm" if eng == "devm" else "deobf"))
            say(ENGINE_TEXT[eng])
            cand = os.path.join(tmpdir, "%d_%s.lua" % (n, eng))
            if eng == "devm":
                res = run_devm(inp, cand, work=work, timeout=timeout, extra=extra if n == 0 else ())
            else:
                res = run_deobf(inp, cand, timeout=timeout, trace_only=eng == "trace",
                                extra=extra if n == 0 else ())
            if not res["ok"]:
                reasons.append(res["reason"])
                say("[!] %s gave no result: %s" % (res["engine"], res["reason"]))
                continue
            if res["complete"]:
                kept = (res, cand)
                break
            if kept is None:
                kept = (res, cand)
            if eng == "devm":
                break
            say("[!] %s: PARTIAL result (%s)" % (res["engine"], "; ".join(res["why"]) or "see its report"))
        if kept is None:
            if os.path.exists(out):
                os.remove(out)
            say("[✗] 没有引擎给出结果 / no engine produced a result (exit %d):" % NO_RESULT)
            for r in reasons:
                say("    " + r)
            return NO_RESULT
        res, cand = kept
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        shutil.copyfile(cand, out)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    # the last line is the front end's own verdict, in the engines' `result:` form
    if res["complete"]:
        say("[✓] devirtualized → %s" % out)
        say("result: complete (%s)" % res["engine"])
        return OK
    if res.get("kind") == "trace":
        say("[!] behaviour trace → %s" % out)
        say("    A trace shows what the script did in the mock environment on this run: branches that")
        say("    were not taken are missing.  It is not the script's source.")
        say("result: PARTIAL: a behaviour trace, not the script's source (deobf)")
        return PARTIAL
    why = "; ".join(res.get("why") or ())
    if res["engine"] == "devm":
        say("[!] devirtualized, PARTIAL → %s  (the report above says what is missing)" % out)
        for r in res.get("why") or ():
            say("    " + r)
    else:
        say("[!] devirtualized, PARTIAL → %s  (%s)" % (out, why or "see the engine's report"))
    say("result: PARTIAL: %s (%s)" % (why or "see the engine's report above", res["engine"]))
    return PARTIAL


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run one engine of the all-in-one deobfuscator directly "
                                             "(deob.py picks the engine automatically).")
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    ap.add_argument("--engine", choices=("devm", "deobf", "trace"), required=True)
    ap.add_argument("--work", help="directory for intermediate files (default: a temporary one)")
    ap.add_argument("--timeout", type=int, help="seconds before an engine is stopped (default: %d)" % DEFAULT_TIMEOUT)
    ap.add_argument("--engine-args", default="", help="extra options for the engine, as one string")
    a = ap.parse_args(argv)
    if not os.path.isfile(a.input):
        say("[✗] no such file: %s" % a.input)
        return FAILED
    return hand_over(a.input, a.output, a.engine, work=a.work, timeout=a.timeout, extra=shlex.split(a.engine_args))


if __name__ == "__main__":
    sys.exit(main())
