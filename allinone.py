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
is 4 and the last lines are the engines' own reasons (`devm: <stage>: <reason>`).  A form-A file
that also has dispatch loops (its main flow runs on the VM) goes to the engines the same way; the
static extraction is then only the fallback, reported PARTIAL (exit 6) when no engine lifts the
script.

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


def _kill_tree(proc, group=True):
    """Stop an engine and everything it started.  (An engine that is merely killed leaves its
    interpreter child, `bin/luau devm/harness.lua`, running at full speed.)  The engine leads
    a process group of its own (_stream starts it in a new session): the group is killed,
    then whatever /proc still lists as its descendant (a child that left the group)."""
    import signal
    victims = _descendants(proc.pid)
    if group and hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
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


# The watchdog: a process of its own (own session) that holds the read end of a pipe whose
# write end only the front end has.  End of file on that pipe = the front end is gone, in
# whatever way (SIGKILL included, which nothing can catch): the watchdog kills the engine's
# process group and ends.  One byte on the pipe = the engine is through: the watchdog just ends.
_WATCHDOG = ("import os,signal,sys\n"
             "fd=int(sys.argv[1]);pg=int(sys.argv[2])\n"
             "try:\n"
             "    b=os.read(fd,1)\n"
             "except OSError:\n"
             "    b=b''\n"
             "if not b:\n"
             "    try:\n"
             "        os.killpg(pg,signal.SIGKILL)\n"
             "    except OSError:\n"
             "        pass\n")


def _start_watchdog(pgid):
    """Returns (write end of the watchdog's pipe, its Popen), or None when no watchdog runs."""
    try:
        r, w = os.pipe()
    except OSError:
        return None
    try:
        wd = subprocess.Popen([sys.executable, "-I", "-S", "-c", _WATCHDOG, str(r), str(pgid)], pass_fds=(r,),
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True, cwd="/")
    except (OSError, ValueError):
        os.close(r)
        os.close(w)
        return None
    os.close(r)
    return w, wd


def _stop_watchdog(watchdog):
    if watchdog is None:
        return
    w, wd = watchdog
    try:
        os.write(w, b"x")
    except OSError:
        pass
    try:
        os.close(w)
    except OSError:
        pass
    try:
        wd.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _reap_group(proc):
    """The engine has ended by itself: nothing it started may stay behind.  Wait for the
    leader WITHOUT reaping it (so that its id, which is the group's id, cannot be given to
    another process yet), kill what is left of the group, then reap."""
    import signal
    try:
        if hasattr(os, "waitid") and hasattr(os, "killpg"):
            os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
    except (OSError, ChildProcessError):
        pass
    proc.wait()


def _stream(cmd, timeout, cwd=HERE, env=None):
    """Run cmd, pass its output through line by line, return (exit status or None on timeout,
    the output as text).  A Python traceback of the engine is not passed through line by
    line: its last line (the exception) is printed, marked as such; the full text is in the
    returned output.

    The engine runs in a session of its own, so it and everything it starts form one process
    group that can be killed as a whole: on a timeout, when the front end is interrupted
    (Ctrl-C, SIGTERM), when the engine has ended and left something running, and, through
    the watchdog, when the front end itself is killed."""
    lines = []
    # SIGTERM to the front end must not leave the engine (and the interpreter it started) behind:
    # turn it into an exception, so that the clean-up below runs.  (SIGKILL cannot be caught:
    # that is the watchdog's case.)
    import signal
    old_term = None
    try:
        old_term = signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError, AttributeError):      # not the main thread / no such signal
        old_term = None
    own_session = os.name == "posix"
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                            bufsize=1, start_new_session=own_session)
    watchdog = _start_watchdog(proc.pid) if own_session else None
    timer = None
    timed_out = []
    if timeout:
        import threading

        def kill():
            if proc.returncode is None:     # (still running, or not reaped yet)
                timed_out.append(True)
                _kill_tree(proc, own_session)
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
        if own_session:
            _reap_group(proc)
        else:
            proc.wait()
    except BaseException:
        _kill_tree(proc, own_session)   # interrupted (Ctrl-C, SIGTERM as KeyboardInterrupt/SystemExit): take the engine down too
        raise
    finally:
        if timer:
            timer.cancel()
        _stop_watchdog(watchdog)
        try:
            proc.stdout.close()
        except OSError:
            pass
        if old_term is not None:
            try:
                signal.signal(signal.SIGTERM, old_term)
            except (ValueError, OSError):
                pass
    return (None if timed_out else proc.returncode), "".join(lines)


_REASON_LINE = re.compile(r"^(?:devm|deobf): \S.*$")


def _lines(text):
    """The lines of an engine's output, split at line feeds only: str.splitlines() also splits at
    characters such as U+2028 or a form feed, which text the script printed (and the engine quotes
    in its report) may contain, and a `result:` line must not be made up that way."""
    return [l.rstrip("\r") for l in text.split("\n")]


def last_reason(text, engine):
    """The engine's own last word on why there is no result: its final `devm: <stage>:
    <reason>` / `deobf: <reason>` line when it prints one, else the exception line of a
    Python traceback, else its last non-empty line."""
    lines = [l.rstrip() for l in _lines(text) if l.strip()]
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
    for line in _lines(text):
        if line.startswith("written "):
            seen = True
            continue
        if seen:
            report.append(line)
    ok = status == 0 and os.path.isfile(out) and os.path.getsize(out) > 0
    result = [l for l in _lines(text) if l.startswith("result: ")]
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
    lines = _lines(text)
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


DIFFCHECK = os.path.join(HERE, "devm", "diffcheck.py")


def check_result(inp, out, work=None, timeout=None, cmd=None):
    """Compare what the protected file and the recovered script do in the same mock
    (`python3 -m devm.diffcheck ORIGINAL RESULT [--work DIR]`, another part of the tree: exit 0
    same, 1 different, 2 not comparable).  Returns (how, line): how is 'same', 'different',
    'not-comparable', 'unavailable' (the tree has no devm/diffcheck.py) or 'timeout'; line is
    the checker's last line (what differs, or why the two cannot be compared)."""
    if cmd is None:
        if not os.path.isfile(DIFFCHECK):
            return "unavailable", "devm/diffcheck.py is not in this tree"
        cmd = [sys.executable, "-u", "-m", "devm.diffcheck"]
    cmd = list(cmd) + [os.path.abspath(inp), os.path.abspath(out)]
    if work:
        cmd += ["--work", os.path.abspath(work)]
    say("[verify] comparing the result with the protected file in the mock (python3 -m devm.diffcheck)")
    status, text = _stream(cmd, timeout or DEFAULT_TIMEOUT)
    lines = [l.strip() for l in _lines(text) if l.strip()]
    last = lines[-1] if lines else "no output"
    if status is None:
        return "timeout", "the comparison was stopped after %d s" % (timeout or DEFAULT_TIMEOUT)
    return {0: "same", 1: "different"}.get(status, "not-comparable"), last


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
    if rc == PARTIAL and form == "A":
        # form A: the static result has the functions the script kept as plain Lua, and the VM
        # runs the rest (deob.py returns 6 only when the file has dispatch loops).  The engines
        # recover the whole script; the static result is kept as the fallback (hand_over).
        say()
        say("[auto] 静态结果只有明文函数，主流程在 VM 里 → 交给引擎还原整个脚本（没有结果时保留静态结果，记为 PARTIAL）")
        say("       the static result holds only the functions kept as plain Lua, the main flow runs on the VM:"
            " handing the file to the engines (the static result is kept as the fallback)")
        return _vm_plan(scan, quiet=True)
    if rc == 4 and form == "vm":
        return _vm_plan(scan)
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


def _vm_plan(scan, quiet=False):
    """(engine, extra arguments) for a script that runs on Luraph's VM, or None (a VM chunk cut out
    of its container, without its data buffer: the advice printed before says how to supply it).
    quiet=True: the reason was said already, only the engine order is printed."""
    if scan.bare_chunk():
        buf = scan.sibling_buffer()
        if not buf:
            return None         # the advice printed above says how to supply the buffer
        say()
        say("[auto] VM chunk + 同目录的数据 buffer → 自动交给去虚拟化器 devm"
            " / handing the chunk and its buffer to the devirtualizer")
        return "devm", ["--chunk-arg", str(buf)]
    order = engine_order(scan)
    if quiet:
        say("       engines / 引擎顺序: %s" % " -> ".join(order))
    elif order == ("deobf", "devm"):
        say()
        say("[auto] Luraph v15.0 的 VM 布局 → 先交给第二个引擎 deobf（它能提升这种布局），没有完整结果再试 devm")
        say("       the v15.0 VM layout: the second engine (deobf) lifts it, so it goes first; devm after it"
            " (static only: --engine static)")
    elif order == ("devm",):
        say()
        say("[auto] Luraph v14 的 VM（脚本整体在 VM 里）→ 交给去虚拟化器 devm（只做静态分析：--engine static）")
        say("       a v14 VM: the whole script runs on it; handing over to the devirtualizer (static only: --engine static)")
    else:
        say()
        say("[auto] 脚本主体在 VM 里 → 自动交给去虚拟化器 devm（只做静态分析：--engine static）")
        say("       the script runs on Luraph's VM: handing over to the devirtualizer (static only: --engine static)")
    return ("devm" if order == ("devm", "deobf") else order), []


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


def _use_fallback(out, fallback, reasons, trace=None):
    """No engine lifted the script: the static result `fallback` = (file, what it lacks) becomes
    the result, reported PARTIAL.  `trace`: the engine's result dict when it was only a behaviour
    trace (dropped in favour of the static result, which is source)."""
    path, missing = fallback
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    shutil.copyfile(path, out)
    if trace is not None:
        say("[!] %s gave only a behaviour trace, not the script's source: the static result is kept" % trace["engine"])
    else:
        say("[!] 没有引擎还原出脚本 / no engine recovered the script:")
        for r in reasons:
            say("    " + r)
    say("[!] static result, PARTIAL → %s" % out)
    say("result: PARTIAL: %s (static)" % missing)
    return PARTIAL


def hand_over(inp, out, engine, work=None, timeout=None, extra=(), post=None, verify=False, fallback=None):
    """Run the engines for `engine` in order and print what came of it.  `engine`: 'devm'
    (devm, then the deobf engine as fallback), 'deobf', 'trace', or a tuple of engine names to
    try in that order.  Returns the front end's exit code:

      OK         an engine wrote a result and reports it complete (and it parses)
      PARTIAL    a result was written but something is missing: the engine's report says what;
                 a behaviour trace; a lift with unlifted blocks; a result that does not parse
      NO_RESULT  no engine produced a result: the last lines give each engine's own reason

    A complete result ends the sequence.  After a partial result of the deobf engine a later
    engine still gets its turn (and its result is taken only when it is complete); a partial
    result of devm stands, since devm's report says exactly what is missing.

    `post(out, res)`, when given, is called once the result file is in place and before the
    final `result:` line is printed (`res`: the engine's result dict; deob.py uses the hook
    to put the owner's own statements in front of a lifted result).

    verify=True: a lifted result is then compared with the protected file (check_result);
    a result that behaves differently is not reported complete.

    fallback=(file, what it lacks): a partial result of the static path (deob.py, form A).  When
    no engine lifted the script (no result, or only a behaviour trace), that file becomes the
    result, reported PARTIAL with the reason it lacks."""
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
        if fallback is not None and (kept is None or kept[0].get("kind") == "trace"):
            return _use_fallback(out, fallback, reasons, trace=kept[0] if kept else None)
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
    if post is not None:
        post(out, res)
    if verify and res.get("kind") != "trace":
        how, line = check_result(inp, out, work=os.path.join(work, "diffcheck") if work else None, timeout=timeout)
        if how == "same":
            say("[✓] 对比通过 / verified: the result does what the protected file does in the mock (devm.diffcheck)")
        elif how == "different":
            say("[!] 对比不一致 / the result does NOT do what the protected file does in the mock: " + line)
            res["complete"] = False
            res["why"] = list(res.get("why") or ()) + ["behaves differently from the protected file in the mock "
                                                      "(devm.diffcheck): " + line[:200]]
        else:
            say("[i] 没有对比 / not verified (%s): %s" % (how, line))
    # the last line is the front end's own verdict, in the engines' `result:` form
    if res["complete"]:
        say("[✓] devirtualized → %s" % out)
        if not verify:
            # "complete" is the engine's statement that nothing is missing, checked here for
            # syntax only: say so (a lift can be whole and still wrong in a place)
            say("    (引擎报告没有缺失；结果的行为没有和原文件对比：--verify / the engine reports nothing missing; "
                "what the result does was not compared with the protected file: --verify)")
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
    ap.add_argument("--verify", action="store_true",
                    help="compare the result with the protected file in the mock (needs devm/diffcheck.py)")
    a = ap.parse_args(argv)
    if not os.path.isfile(a.input):
        say("[✗] no such file: %s" % a.input)
        return FAILED
    return hand_over(a.input, a.output, a.engine, work=a.work, timeout=a.timeout, extra=shlex.split(a.engine_args),
                     verify=a.verify)


if __name__ == "__main__":
    sys.exit(main())
