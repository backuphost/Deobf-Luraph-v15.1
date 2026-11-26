"""
Behaviour trace output, shared by every obfuscator plugin: the rendered
trace of a harness run (envlog.luau) -> readable Luau (tidy.py, fold.py,
spacing.py).
"""
import os
import re
import sys


def stmt_count(body):
    m = re.search(r"-- (\d+) statements recorded", body)
    return int(m.group(1)) if m else 0


def take_line(body, name):
    """(value of the first `\\0NAME value` line, body without it); value is None if absent."""
    m = re.search(r"\x00%s ([^\n]*)\n" % name, body)
    if not m:
        return None, body
    return m.group(1), body[:m.start()] + body[m.end():]


def take_strings(body):
    """(body, the strings section of `dump_strings` or None)."""
    if "\x00ENVLOG-STRINGS" in body:
        body, strings = body.split("\x00ENVLOG-STRINGS\n", 1)
        return body, strings
    return body, None


def header(input_path, notes=()):
    h = ("-- Deobfuscated by deobf (dynamic trace)\n"
         "-- source: %s\n"
         "-- NOTE: reconstructed from observed behaviour; branches that were not taken\n"
         "--       during the trace are missing and conditions are only noted in comments.\n"
         % os.path.basename(input_path))
    return h + "".join("-- %s\n" % n for n in notes)


def render(text, args, preamble=True):
    """The readable trace: tidy (+ fold) + spacing, or only the fold markers
    stripped with --no-tidy. preamble: strip Luraph-style environment probes
    at the top (tidy.strip_preamble)."""
    if os.environ.get("DEOB_PRETIDY"):
        # the text tidy/fold get, markers included (for timing or diffing fold.py alone)
        with open(os.environ["DEOB_PRETIDY"], "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    import limits
    if limits.expired():
        # (the readability pass takes minutes on a long trace)
        print("[!] time limit reached: the trace is written without the readability pass", file=sys.stderr)
    elif not args.no_tidy:
        import tidy
        import spacing
        t = tidy.tidy(text, preamble=preamble and not args.keep_preamble, fold_code=not args.no_fold)
        return spacing.space(t)
    import fold
    return fold.strip_markers(text)


TRACE_HEADER = "-- Deobfuscated by deobf (dynamic trace)"


def run_status(body):
    """The run's status as envlog.luau reports it in the first line of the
    trace: "finished", "script error: ...", "aborted: ...", "stopped in an
    endless loop (truncated)", "Luraph anti-tamper triggered ..."."""
    first = body.splitlines()[0] if body else ""
    m = re.match(r"-- run status: (.*)", first)
    return m.group(1).strip() if m else ""


def run_ended(status):
    """The script ran to its end, normally or by raising an error (as opposed
    to a run the tracer stopped: an endless loop, the budget, a tamper trap)."""
    return status == "finished" or status.startswith("script error:")


def status_line(body, job=None):
    """Print the run status; with a job, record it (job.run_status)."""
    status = body.splitlines()[0] if body else ""
    print("[*] " + status.lstrip("- "), file=sys.stderr)
    if job is not None:
        job.run_status = run_status(body)
    return run_status(body)
