#!/usr/bin/env python3
"""Run deob.py over a directory of protected scripts and tabulate what came of each.

    python3 tests/corpus.py DIR [DIR ...] --out results.jsonl
        --jobs N        parallel runs (default: CPUs - 1)
        --timeout S     seconds per file (default 600)
        --engine E      passed to deob.py (default auto)
        --per-dir K     at most K files per directory, spread over the size range (default: all)
        --keep DIR      keep the outputs there (default: discarded)
        --resume        skip files already in the results file

One JSON object per file is appended to the results file:
    path, size, rc, secs, engines, out_bytes, out_lines, parses, lift_errors, tail

`python3 tests/corpus.py --summary results.jsonl` prints the table again.

    python3 tests/corpus.py DIR [DIR ...] --classify --out rows.jsonl [--jobs N] [--per-dir K] [--resume]

only classifies (what `deob.py FILE --classify` prints, one row per file; no engine is started,
a container is opened by the static decoder or in the unwrap sandbox) and tabulates verdict,
wrapper and layout per directory.  `--summary` reads such a file too.

This is a measuring tool, not a test: exit status is always 0.  The inputs are not part of the
repository.
"""
import argparse
import collections
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LUAU_AST = os.path.join(ROOT, "bin", "luau-ast")

RC_NAMES = {0: "complete", 6: "partial", 4: "vm, no result", 3: "syntax check failed", 2: "not Luraph / not recognised",
            8: "Luraph, cannot be processed (damaged / other Lua)",
            1: "error", 5: "wrong binary", 7: "form C harness", None: "timeout"}


def pick(files, k):
    """k files spread over the size range (smallest, largest and quantiles between)."""
    files = sorted(files, key=lambda p: (os.path.getsize(p), p))
    files = [p for p in files if os.path.getsize(p) > 0]
    if not k or len(files) <= k:
        return files
    idx = sorted({round(i * (len(files) - 1) / (k - 1)) for i in range(k)})
    return [files[i] for i in idx]


def gather(dirs, per_dir):
    out = []
    for d in dirs:
        if os.path.isfile(d):
            out.append(d)
            continue
        groups = collections.defaultdict(list)
        for base, _dirs, names in os.walk(d):
            for n in names:
                if n.endswith((".lua", ".luau", ".txt")):
                    groups[base].append(os.path.join(base, n))
        for base in sorted(groups):
            out += pick(groups[base], per_dir)
    return out


def parses(path):
    try:
        r = subprocess.run([LUAU_AST, path], capture_output=True, text=True, errors="replace", timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # luau-ast reports a syntax error on stderr and by its exit status, not on stdout
    return r.returncode == 0 and "Parse errors" not in (r.stderr[:4000] + r.stdout[:4000])


def one(path, engine, timeout, keep):
    tmp = tempfile.mkdtemp(prefix="corpus_")
    out = os.path.join(keep or tmp, os.path.basename(os.path.dirname(path)) + "__" + os.path.basename(path))
    cmd = [sys.executable, "-B", os.path.join(ROOT, "deob.py"), path, "-o", out, "--work", os.path.join(tmp, "work")]
    if engine != "auto":
        cmd += ["--engine", engine]
    t = time.time()
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace", start_new_session=True)
    try:
        log, _ = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            pass
        log, _ = proc.communicate()
        rc = None
    row = {"path": path, "size": os.path.getsize(path), "rc": rc, "secs": round(time.time() - t, 1),
           "engines": sorted(set(re.findall(r"^\[(devm|deobf)\]", log, re.M))),
           "out_bytes": 0, "out_lines": 0, "parses": None, "lift_errors": 0,
           "tail": [l[:300] for l in log.strip().split("\n")[-14:]]}
    if os.path.isfile(out) and os.path.getsize(out):
        with open(out, encoding="utf-8", errors="replace") as f:
            text = f.read()
        row.update(out_bytes=len(text), out_lines=text.count("\n"), parses=parses(out),
                   lift_errors=text.count("LIFT ERROR"))
    subprocess.run(["rm", "-rf", tmp])
    return row


def classify_one(path):
    """One row of the classification table: deob.classify_file(path) plus path, size, secs."""
    sys.path.insert(0, ROOT) if ROOT not in sys.path else None
    import deob
    t = time.time()
    row = deob.classify_file(path)
    row.pop("file", None)
    row.update(path=path, size=os.path.getsize(path), secs=round(time.time() - t, 2))
    return row


def classify_summary(rows):
    by = collections.defaultdict(list)
    for r in rows:
        by[os.path.basename(os.path.dirname(r["path"]))].append(r)

    def vkey(v):
        return [int(x) for x in re.findall(r"\d+", v)]

    def kind(r):
        w = r["wrapper"]
        if w != "none":
            c = r.get("container") or {}
            w += "" if c.get("opened") else " (closed)"
        return "%s | %s | %s" % (r["verdict"], w, r["layout"])
    print("%-10s %5s  %s" % ("dir", "files", "verdict | wrapper | layout of the VM"))
    total = collections.Counter()
    for d in sorted(by, key=vkey):
        c = collections.Counter(kind(r) for r in by[d])
        print("%-10s %5d  %s" % (d, len(by[d]), "; ".join("%s: %d" % kv for kv in c.most_common())))
        total.update(r["verdict"] for r in by[d])
    print("%-10s %5d  %s" % ("all", len(rows), ", ".join("%s: %d" % kv for kv in total.most_common())))
    odd = [r for r in rows if r["verdict"] != "luraph"]
    if odd:
        print("not processed as Luraph (%d):" % len(odd))
        for r in sorted(odd, key=lambda r: (r["verdict"], r["path"])):
            print("  %-18s %-34s %s" % (r["verdict"], "/".join(r["path"].split("/")[-2:]), (r.get("reason") or "")[:150]))


def summary(rows):
    if rows and "verdict" in rows[0] and "rc" not in rows[0]:
        return classify_summary(rows)
    by = collections.defaultdict(list)
    for r in rows:
        by[os.path.basename(os.path.dirname(r["path"]))].append(r)

    def vkey(v):
        return [int(x) for x in re.findall(r"\d+", v)]
    print("%-10s %5s  %s" % ("dir", "files", "outcome"))
    total = collections.Counter()
    for d in sorted(by, key=vkey):
        c = collections.Counter(RC_NAMES.get(r["rc"], "rc %s" % r["rc"]) for r in by[d])
        total.update(c)
        print("%-10s %5d  %s" % (d, len(by[d]), ", ".join("%s: %d" % kv for kv in c.most_common())))
    print("%-10s %5d  %s" % ("all", len(rows), ", ".join("%s: %d" % kv for kv in total.most_common())))
    bad = [r for r in rows if r["out_bytes"] and r["parses"] is False]
    if bad:
        print("results that do not parse: %d" % len(bad))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dirs", nargs="*")
    ap.add_argument("--out", default="corpus_results.jsonl")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--engine", default="auto")
    ap.add_argument("--per-dir", type=int, default=0)
    ap.add_argument("--keep")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--summary")
    ap.add_argument("--classify", action="store_true",
                    help="classify only (deob.classify_file), no engine; the rows are its fields")
    a = ap.parse_args(argv)
    if a.summary:
        with open(a.summary) as f:
            summary([json.loads(l) for l in f if l.strip()])
        return 0
    files = gather(a.dirs, a.per_dir)
    done = set()
    if a.resume and os.path.exists(a.out):
        with open(a.out) as f:
            done = {json.loads(l)["path"] for l in f if l.strip()}
    elif os.path.exists(a.out):
        os.remove(a.out)
    files = [p for p in files if p not in done]
    if a.keep:
        os.makedirs(a.keep, exist_ok=True)
    if a.classify:
        print("%d files to classify, %d at a time" % (len(files), a.jobs), flush=True)
        rows = []
        with concurrent.futures.ProcessPoolExecutor(a.jobs) as pool, open(a.out, "a") as f:
            for n, r in enumerate(pool.map(classify_one, files, chunksize=4), 1):
                rows.append(r)
                f.write(json.dumps(r) + "\n")
                f.flush()
                if n % 200 == 0:
                    print("[%d/%d]" % (n, len(files)), flush=True)
        if done:
            with open(a.out) as f:
                rows = [json.loads(l) for l in f if l.strip()]
        summary(rows)
        return 0
    print("%d files, %d at a time, %d s each at most" % (len(files), a.jobs, a.timeout), flush=True)
    rows = []
    with concurrent.futures.ThreadPoolExecutor(a.jobs) as pool:
        futs = [pool.submit(one, p, a.engine, a.timeout, a.keep) for p in files]
        for n, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            r = fut.result()
            rows.append(r)
            with open(a.out, "a") as f:
                f.write(json.dumps(r) + "\n")
            print("[%d/%d] %-34s %-20s %6.0f s  %s" % (
                n, len(files), "/".join(r["path"].split("/")[-2:]), RC_NAMES.get(r["rc"], r["rc"]), r["secs"],
                "+".join(r["engines"])), flush=True)
    summary(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
