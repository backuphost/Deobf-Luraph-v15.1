"""Get a Luraph bootstrap past its environment checks without a perfect Roblox mock.

The bootstrap interleaves its real work with checks of the runtime environment.  A failed check
ends in the *tamper response*: a closure that scrambles the VM's code arrays and never returns.
Outside Roblox some checks inevitably fail, so:

  * the tamper response is replaced by a no-op (harness option `stub=<pid>`), and
  * for every remaining call of it, the branch decision that led there is located and forced the
    other way (harness option `flip=<n>`; every data-dependent branch condition of the interpreters
    is routed through a counting hook, see Model.cond_sites).

The search is purely behavioural: a candidate decision is accepted when forcing it makes the run
get further (in VM closure entries) before the next tamper response.
"""
import os
import re
import subprocess

from . import vmmodel

HERE = os.path.dirname(os.path.abspath(__file__))
FAIL_RE = re.compile(r'^\[fail\] conds=(\d+) run=(\d+) last: (.*)$', re.M)


class Outcome:
    def __init__(self, stderr, timed_out=False):
        self.stderr = stderr
        self.timed_out = timed_out
        m = FAIL_RE.search(stderr)
        self.fail = m is not None
        self.conds = int(m.group(1)) if m else None
        self.run = int(m.group(2)) if m else None
        self.ring = []
        if m:
            for part in m.group(3).split():
                n, kind, val, at = part.split(':')
                self.ring.append((int(n), int(kind), int(val), int(at)))
        self.finished = '[done]' in stderr
        tail = [l for l in stderr.strip().split('\n') if l and not l.startswith('[')]
        self.error = None if (self.fail or self.finished) else (tail[0] if tail else 'no output')


def run(patched, stubs, flips, extra=(), timeout=300):
    args = [vmmodel.find_luau(), os.path.join(HERE, 'harness.lua'), '-a', patched, patched + '.trace']
    args += ['stub=%d' % s for s in stubs] + ['flip=%d' % f for f in flips] + list(extra)
    try:
        r = subprocess.run(args, capture_output=True, text=True, errors='replace', timeout=timeout)
        return Outcome(r.stderr)
    except subprocess.TimeoutExpired as e:
        err = e.stderr.decode('latin-1') if isinstance(e.stderr, bytes) else (e.stderr or '')
        return Outcome(err, timed_out=True)


def neutralise(patched, stubs, log=print, max_checks=400, window=3, flips=None, on_progress=None):
    """Returns (flips, final Outcome).  `flips` resumes an earlier search."""
    flips = list(flips or [])
    cur = run(patched, stubs, flips, ['stopatfail=1'])
    n = 0
    while cur.fail and n < max_checks:
        n += 1
        # The tamper response is preceded by a call that records the failure; the decision was
        # taken before that call, i.e. before the last closure entry ahead of the response.
        # Compares of a register against an immediate (kind 5) are what the flattened state machine
        # dispatches with: never touched.  What remains, newest first, is the decision itself or a
        # condition inside the comparison helper it called.
        cands = [c for c in reversed(cur.ring) if c[0] not in flips and c[3] < cur.run - 1]
        ordered = [c for c in cands if c[1] != 5][:window]
        best = None
        for cid, kind, val, at in ordered:
            o = run(patched, stubs, flips + [cid], ['stopatfail=1'])
            if o.timed_out or o.error:
                continue
            if o.finished or o.conds > cur.conds:
                best = (cid, kind, o)
                break
        if best is None:
            log('[antitamper] check %d (closure entry %d): no branch decision found that avoids it' % (n, cur.run))
            break
        cid, kind, o = best
        flips.append(cid)
        if on_progress:
            on_progress(flips)
        log('[antitamper] check %d at closure entry %d: forced decision #%d (kind %d) -> %s' % (
            n, cur.run, cid, kind, 'no further tamper response' if not o.fail else 'next at entry %d' % o.run))
        cur = o
    return flips, cur
