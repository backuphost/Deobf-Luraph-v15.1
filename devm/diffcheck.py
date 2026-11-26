"""Does a recovered script do what the protected file does?

    python3 -m devm.diffcheck ORIGINAL RESULT [--work DIR] [--timeout S] [--json]

Runs the protected file and the recovered script in the same mock environment (harness.lua and
rbxmock.lua, through the recording front end diffcheck.lua; nothing is sent anywhere, a request
gets an empty reply) and compares what each one does there: the ordered calls into the
environment with their arguments, property writes on mock objects, the globals each leaves
behind and how each run ends.

Exit status: 0 same, 1 different (the first divergence is printed with context), 2 not
comparable (the reason is printed).  "Same" is a statement about ONE run in ONE mock environment:
code the run did not reach is not compared, and neither is anything a server's reply would
trigger.  The report says how far the comparison went.

How the two logs are lined up.  A protected file runs Luraph's own start-up code first, which
probes the environment (getfenv, type, task.*, Instance.new, a GUI layout, ...); the recovered
script has none of that.  The result's log must equal one contiguous stretch of the original's
log, and when both runs ended, the stretch must reach the original's end.  The calls of the
original in front of it are the start-up; they are only accepted as such when every one of them
is of a kind Luraph's start-up makes (PROBE_CALLS).  A print, a request or a call of anything
else in front of the result's first call is reported as a difference.  This is a rule of thumb
with one known hole: a result that lacks its script's first calls is not noticed when those
calls are themselves of the probe kinds (say a leading `task.wait()`).  The report names how
many calls were skipped and of which kinds.

Time and chance.  The front end replaces the clock by a counter and seeds math.random; both
depend on a "salt".  When the first comparison (salt 1) finds a difference, both files are run
again with salt 2: an argument that differs between the two runs of the same file depends on
time or chance and is not compared.

Partial results (LIFT ERROR markers, __LPH_ENV, ARG_n / REG_n / UPVAL_n placeholders) usually
stop early.  They are compared as far as they run; "same up to call N, then the result stops"
is reported as not comparable beyond that point (exit 2), never as same.
"""
import argparse
import collections
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
US = '\x1f'
ID_RE = re.compile('\x01(\\d+)\x02')

SAME, DIFFERENT, NOT_COMPARABLE = 0, 1, 2
VERDICT_WORD = {SAME: 'same', DIFFERENT: 'different', NOT_COMPARABLE: 'not comparable'}

# Calls that are never compared: they have no effect outside the script, and Luraph's interpreter
# makes them on its own while the script runs (the result, being plain code, does not).
NOISE_CALLS = frozenset(['getfenv', 'setfenv', 'type', 'typeof'])

# Kinds of call Luraph's start-up code makes before the script starts.  Calls of the original in
# front of the result's first call are accepted as start-up only when all of them are in this set.
PROBE_CALLS = frozenset([
    # the loader: the container compiles its chunk, the VM tests loadstring
    'loadstring',
    # scheduler and instance probes of the v14.6+ / v15 start-up
    'task.spawn', 'task.defer', 'task.delay', 'task.wait', 'task.cancel', 'task.synchronize', 'task.desynchronize',
    'spawn', 'delay', 'wait', 'tick', 'time', 'elapsedTime', 'os.clock', 'os.time', 'os.date',
    'Instance.new', 'Instance.fromExisting', 'Destroy', 'Clone', 'IsA', 'GetService', 'FindFirstChild',
    'FindFirstChildOfClass', 'FindFirstChildWhichIsA', 'GetChildren', 'GetDescendants', 'GetFullName',
    'IsDescendantOf', 'IsAncestorOf', 'GetAttribute', 'SetAttribute', 'GetAttributes', 'AddTag', 'RemoveTag',
    'HasTag', 'GetTags', 'GetTagged', 'GetPropertyChangedSignal', 'Connect', 'Once', 'Disconnect', 'Wait',
    'GetEnumItems', 'GetEnums', 'FromName', 'FromValue',
    # datatype constructors and their methods (GUI layout, Path2D geometry)
    'Vector2.new', 'Vector3.new', 'UDim.new', 'UDim2.new', 'UDim2.fromScale', 'UDim2.fromOffset', 'Color3.new',
    'Color3.fromRGB', 'Color3.fromHSV', 'Color3.fromHex', 'Color3.toHSV', 'CFrame.new', 'CFrame.Angles',
    'Random.new', 'NextInteger', 'NextNumber', 'NextUnitVector', 'Shuffle', 'Path2DControlPoint.new',
    'SetControlPoints', 'GetControlPoints', 'GetPositionOnCurve', 'GetPositionOnCurveArcLength',
    'GetTangentOnCurve', 'GetTangentOnCurveArcLength', 'GetBoundingRect', 'GetLength', 'GetMaxControlPoints',
    'GetControlPoint', 'InsertControlPoint', 'RemoveControlPoint', 'UpdateControlPoint', 'GetSegmentCount',
    'Lerp', 'Dot', 'Cross', 'FuzzyEq', 'ToWorldSpace', 'ToObjectSpace', 'GetComponents', 'Inverse',
    # executor probes
    'identifyexecutor', 'iscclosure', 'islclosure', 'isreadonly', 'getrawmetatable', 'require',
    'debug.profilebegin', 'debug.profileend', 'debug.getmemorycategory', 'debug.setmemorycategory',
    'debug.resetmemorycategory', 'DecompressBuffer', 'CompressBuffer',
])

PLACEHOLDER_RE = re.compile(r'LIFT ERROR|__LPH_\w+|\b(?:ARG|REG|UPVAL)_\d+\b|\bLuraphVM\b|nil --\[\[|\bUNSTRUCTURED\b')


def find_luau():
    from . import vmmodel
    return vmmodel.find_luau()


# ---------------------------------------------------------------------------- one run

class Event:
    __slots__ = ('kind', 'name', 'args', 'line')

    def __init__(self, kind, name, args, line):
        self.kind, self.name, self.args, self.line = kind, name, args, line

    def text(self, limit=220):
        """For people: ids as #n, long arguments cut."""
        def show(a):
            a = ID_RE.sub(lambda m: '#' + m.group(1), a)
            return a if len(a) <= limit else a[:limit] + '...'
        if self.kind == 'S':
            a = self.args + ['?'] * (3 - len(self.args))
            return '%s.%s = %s' % (show(a[0]), _key(a[1]), show(a[2]))
        return '%s(%s)' % (self.name, ', '.join(show(a) for a in self.args))


def _key(a):
    m = re.fullmatch(r'"([A-Za-z_]\w*)"', a)
    return m.group(1) if m else '[' + a + ']'


class Run:
    """What one script did in the mock."""

    def __init__(self, path, salt):
        self.path, self.salt = path, salt
        self.events = []          # every C and S event, in order
        self.errors = {}          # index of an event -> message the call raised
        self.notes = []           # (index of the next event, harness line): [http], [env], COMPILE ERROR ...
        self.globals = []         # (name, value) left behind
        self.started = False
        self.end = None           # 'returned' | 'error' | None (cut)
        self.end_message = ''
        self.cut = None           # 'time limit' | 'event limit' | None
        self.fatal = None
        self.seconds = 0.0
        self.returncode = None

    @property
    def requests(self):
        return [line for _, line in self.notes if line.startswith('[http]')]

    def how_ended(self):
        if self.fatal:
            return 'did not run: ' + self.fatal
        if self.end == 'returned':
            return 'returned'
        if self.end == 'error':
            return 'stopped with an error: ' + self.end_message
        if self.cut:
            return 'was still running at the %s' % self.cut
        return 'ended without a report (exit status %s)' % self.returncode


def run_script(path, workdir, tag, salt=1, timeout=60.0, max_events=20000, options=('executor=1',), noise=NOISE_CALLS):
    """Run one file through diffcheck.lua / harness.lua and read its event log.

    max_events counts compared events (not NOISE_CALLS); the run is stopped when it is reached."""
    from . import unidata
    os.makedirs(workdir, exist_ok=True)
    nf = os.path.join(workdir, 'nfdata.lua')
    if not os.path.exists(nf):
        unidata.generate(nf)
    cmd = [find_luau(), os.path.join(HERE, 'diffcheck.lua'), '-a', os.path.join(HERE, 'harness.lua'),
           os.path.abspath(path), os.path.join(workdir, tag + '.out'), 'salt=%d' % salt] + list(options)
    run = Run(path, salt)
    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=workdir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, start_new_session=True)

    def kill():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    timed_out = []
    timer = threading.Timer(timeout, lambda: (timed_out.append(1), kill()))
    timer.start()
    counted = 0
    raw_lines = 0
    log = open(os.path.join(workdir, tag + '.log'), 'wb')
    try:
        for raw in proc.stderr:
            log.write(raw)
            raw_lines += 1
            line = raw.decode('latin-1').rstrip('\n')
            if not line.startswith('[dc] '):
                if line.strip() and len(run.notes) < 2000:
                    run.notes.append((len(run.events), line[:500]))
                continue
            f = line[5:].split(US)
            kind = f[0]
            if kind in ('C', 'S'):
                ev = Event(kind, f[1] if len(f) > 1 else '', f[2:], line)
                run.events.append(ev)
                if not (kind == 'C' and ev.name in noise):
                    counted += 1
                    if counted >= max_events:
                        run.cut = 'event limit'
                        break
                if raw_lines > 200 * max_events:
                    run.cut = 'event limit'
                    break
            elif kind == 'E':
                if run.events:
                    run.errors[len(run.events) - 1] = f[2] if len(f) > 2 else ''
            elif kind == 'G':
                run.globals.append((f[1] if len(f) > 1 else '', f[2] if len(f) > 2 else ''))
            elif kind == 'START':
                run.started = True
            elif kind == 'END':
                run.end = f[1] if len(f) > 1 else 'returned'
                run.end_message = _unquote(f[2]) if len(f) > 2 else ''
            elif kind == 'FATAL':
                run.fatal = f[1] if len(f) > 1 else 'unknown'
    finally:
        timer.cancel()
        kill()
        proc.stderr.close()
        run.returncode = proc.wait()
        log.close()
    run.seconds = round(time.time() - t0, 2)
    if timed_out and run.end is None and run.cut is None:
        run.cut = 'time limit'
    if run.end is not None:
        run.cut = None
    if not run.started and not run.fatal:
        text = '; '.join(line for _, line in run.notes[-3:]) or 'no output'
        run.fatal = ('time limit before the script started' if timed_out else 'the harness did not start the script') + ': ' + text
    return run


def _unquote(a):
    m = re.match(r'"((?:[^"\\]|\\.)*)"', a)
    if not m:
        return a
    return re.sub(r'\\(x[0-9a-f]{2}|.)', lambda g: chr(int(g.group(1)[1:], 16)) if g.group(1)[0] == 'x' and len(g.group(1)) == 3
                  else ('\n' if g.group(1) == 'n' else g.group(1)), m.group(1)) + a[m.end():]


# ---------------------------------------------------------------------------- comparison

def compared(run, noise=NOISE_CALLS):
    """The events of a run that are compared, with their index in run.events."""
    return [(i, e) for i, e in enumerate(run.events) if not (e.kind == 'C' and e.name in noise)]


def volatile_args(a, b, noise=NOISE_CALLS):
    """Arguments that depend on time or chance: positions (k, j) where two runs of the same file
    (different salt) disagree.  k counts compared events.  Second value: the number of leading
    compared events that have the same shape in both runs (beyond it the two runs took different
    paths and nothing is known)."""
    ea, eb = compared(a, noise), compared(b, noise)
    vol = set()
    n = 0
    ca, cb = _Canon(), _Canon()
    for (_, x), (_, y) in zip(ea, eb):
        if x.kind != y.kind or x.name != y.name or len(x.args) != len(y.args):
            break
        for j, (p, q) in enumerate(zip(x.args, y.args)):
            if ca(p) != cb(q):
                vol.add((n, j))
        n += 1
    return vol, n


class _Canon:
    """Renumbers object serials in order of first appearance."""

    def __init__(self):
        self.map = {}

    def __call__(self, text):
        if '\x01' not in text:
            return text
        m = self.map
        return ID_RE.sub(lambda g: '#%d' % m.setdefault(g.group(1), len(m) + 1), text)


def _shape(e):
    return (e.kind, e.name, len(e.args))


def _match_len(eo, p, er, vol_o, vol_r):
    """Number of leading events of er that equal eo[p:], objects numbered from the start of each."""
    co, cr = _Canon(), _Canon()
    n = min(len(eo) - p, len(er))
    for k in range(n):
        x, y = eo[p + k][1], er[k][1]
        if x.kind != y.kind or x.name != y.name or len(x.args) != len(y.args):
            return k
        for j in range(len(x.args)):
            if (p + k, j) in vol_o or (k, j) in vol_r:
                continue
            if co(x.args[j]) != cr(y.args[j]):
                return k
    return n


def align(orig, res, vol_o=frozenset(), vol_r=frozenset(), noise=NOISE_CALLS, max_candidates=4000):
    """Where the result's log starts in the original's.  Returns (p, matched): the original's
    compared events from index p on equal the first `matched` compared events of the result."""
    eo, er = compared(orig, noise), compared(res, noise)
    if not er:
        return len(eo), 0
    first = _shape(er[0][1])
    cands = [p for p in range(len(eo)) if _shape(eo[p][1]) == first]
    both_ended = orig.end is not None and res.end is not None
    tail = len(eo) - len(er)
    if len(cands) > max_candidates:
        keep = cands[:max_candidates // 2] + cands[-max_candidates // 2:]
        if tail in cands and tail not in keep:
            keep.append(tail)
        cands = sorted(set(keep))
    best = None
    for p in cands:
        m = _match_len(eo, p, er, vol_o, vol_r)
        full = m == len(er) or m == len(eo) - p
        if full and (not both_ended or p == tail):
            return p, m
        # otherwise: the longest match; among equals the one that ends at the original's end, then the earliest
        key = (m, p == tail, -p)
        if best is None or key > best[0]:
            best = (key, p, m)
    if best is None:
        return len(eo), 0
    return best[1], best[2]


class Verdict:
    def __init__(self, code, headline):
        self.code = code
        self.headline = headline
        self.lines = []           # further report lines
        self.data = {}

    def add(self, line):
        self.lines.append(line)

    def text(self):
        return '\n'.join(['diffcheck: %s: %s' % (VERDICT_WORD[self.code].upper(), self.headline)] + ['  ' + l for l in self.lines])


def _strip_position(msg):
    """An error message without the position Luau puts in front (it names the chunk and line,
    which differ between a VM and plain code)."""
    prev = None
    while prev != msg:
        prev = msg
        msg = re.sub(r'^(?:\[string "[^"]*"\]|[\w./\\ -]+):\d+: ', '', msg)
    return msg


def _result_markers(text):
    c = collections.Counter()
    for m in PLACEHOLDER_RE.finditer(text):
        t = m.group(0)
        if t.startswith('__LPH_'):
            pass
        elif re.match(r'(ARG|REG|UPVAL)_', t):
            t = t.split('_')[0] + '_n'
        elif t.startswith('nil --'):
            t = 'nil --[[...]]'
        c[t] += 1
    return c


def _placeholder_at(res, text):
    """If the result's run stopped on a line of the result that holds a placeholder: (line no, line)."""
    if res.end != 'error':
        return None
    lines = text.split('\n')
    for m in re.finditer(r'(?:^|[ :])sample:(\d+):', res.end_message):
        n = int(m.group(1))
        if 1 <= n <= len(lines) and PLACEHOLDER_RE.search(lines[n - 1]):
            return n, lines[n - 1].strip()[:200]
    m = PLACEHOLDER_RE.search(res.end_message)
    if m:
        return 0, m.group(0)
    return None


def _context(events, k, before=3, after=2):
    out = []
    for i in range(max(0, k - before), min(len(events), k + after + 1)):
        out.append(('  > ' if i == k else '    ') + '%d  %s' % (i + 1, events[i][1].text()))
    return out


def _kinds(events):
    c = collections.Counter(('property write' if e.kind == 'S' else e.name) for _, e in events)
    return ', '.join('%s x%d' % kv for kv in c.most_common(12)) + (', ...' if len(c) > 12 else '')


def _is_probe(e):
    return e.kind == 'C' and e.name in PROBE_CALLS


def compare(orig, res, result_text='', vol_o=frozenset(), vol_r=frozenset(), noise=NOISE_CALLS):
    """Compare two runs.  Returns a Verdict."""
    for run, who in ((orig, 'original'), (res, 'result')):
        if run.fatal:
            v = Verdict(NOT_COMPARABLE, 'the %s did not run: %s' % (who, run.fatal))
            return v
    eo, er = compared(orig, noise), compared(res, noise)
    markers = _result_markers(result_text)
    partial = sum(markers.values()) > 0
    p, m = align(orig, res, vol_o, vol_r, noise)
    n_o, n_r = len(eo) - p, len(er)
    prefix = eo[:p]
    data = {'original_events': len(eo), 'result_events': n_r, 'skipped': p, 'matched': m,
            'original_end': orig.how_ended(), 'result_end': res.how_ended(),
            'result_markers': dict(markers), 'masked_arguments': len(vol_o) + len(vol_r),
            'requests': orig.requests[:20]}

    def finish(code, headline):
        v = Verdict(code, headline)
        v.data = data
        v.add('original: %d calls and property writes (plus %d not compared: %s), %s' % (
            len(eo), len(orig.events) - len(eo), '/'.join(sorted(noise)), orig.how_ended()))
        v.add('result:   %d calls and property writes, %s' % (n_r, res.how_ended()))
        if p:
            v.add('skipped %d leading calls of the original as Luraph start-up (%s)' % (p, _kinds(prefix)))
        if vol_o or vol_r:
            v.add('%d arguments depend on time or chance (they differ between two runs of the same file) and were not compared' % (len(vol_o) + len(vol_r)))
        if partial:
            v.add('the result is partial: ' + ', '.join('%d x %s' % (n, t) for t, n in markers.most_common(6)))
        reqs = orig.requests
        if reqs:
            v.add('requests of the original (nothing was sent, each got an empty reply; what a real reply would trigger is not compared):')
            for r in reqs[:5]:
                v.add('  ' + r[:200])
            if len(reqs) > 5:
                v.add('  ... %d more' % (len(reqs) - 5))
        return v

    # 1. calls in front of the result's first call must be Luraph's
    odd = [(i, e) for i, e in prefix if not _is_probe(e)]
    if odd and m == n_r and n_r <= n_o:
        # (when the result's own calls do not line up at all, that is the divergence to report)
        v = finish(DIFFERENT, 'the original makes %d call(s) in front of the result\'s first call that Luraph\'s start-up does not make' % len(odd))
        v.add('first of them (call %d of the original):' % (prefix.index(odd[0]) + 1))
        for line in _context(eo, prefix.index(odd[0]), 2, 2):
            v.add(line)
        return v

    # 2. a call that differs
    if m < n_r and m < n_o:
        v = finish(DIFFERENT, 'call %d of the result differs from the original (%d equal before it)' % (m + 1, m))
        v.add('original (call %d):' % (p + m + 1))
        for line in _context(eo, p + m):
            v.add(line)
        v.add('result (call %d):' % (m + 1))
        for line in _context(er, m):
            v.add(line)
        return v

    # 3. the result's log is used up, the original goes on
    if m == n_r and n_o > n_r:
        nxt = _context(eo, p + m, 2, 2)
        if res.cut:
            v = finish(SAME, 'same for the first %d calls; the result\'s run was cut at the %s there, the original goes on' % (m, res.cut)) if m else \
                finish(NOT_COMPARABLE, 'the result made no call before its %s' % res.cut)
            return v
        at = _placeholder_at(res, result_text)
        if at or partial:
            where = ('at a placeholder (line %d: %s)' % at if at and at[0] else 'on %s' % at[1] if at else
                     'early (%s); it is partial' % res.how_ended())
            v = finish(NOT_COMPARABLE, 'same up to call %d of %d, then the result stops %s' % (m, n_o, where))
        else:
            v = finish(DIFFERENT, 'after %d equal calls the result %s, the original goes on (%d more calls)' % (
                m, 'returns' if res.end == 'returned' else 'stops (%s)' % res.how_ended(), n_o - n_r))
        v.add('next calls of the original:')
        for line in nxt:
            v.add(line)
        return v

    # 4. the original's log is used up, the result goes on
    if m == n_o and n_r > n_o:
        if orig.cut:
            if m == 0:
                return finish(NOT_COMPARABLE, 'the original made no call of the script before its %s' % orig.cut)
            return finish(SAME, 'same for the first %d calls; the original\'s run was cut at the %s there, the result goes on' % (m, orig.cut))
        v = finish(DIFFERENT, 'after %d equal calls the original %s, the result goes on (%d more calls)' % (
            m, 'returns' if orig.end == 'returned' else 'stops', n_r - n_o))
        v.add('next calls of the result:')
        for line in _context(er, m, 2, 2):
            v.add(line)
        return v

    # 5. every call lines up (m == n_r == n_o): the endings
    if orig.cut or res.cut:
        if orig.cut and res.cut:
            if m == 0:
                return finish(NOT_COMPARABLE, 'neither run made a call of the script before its limit')
            return finish(SAME, 'same for the first %d calls; both runs were still going at their limit (%s / %s)' % (m, orig.cut, res.cut))
        late, done = ('original', 'result') if orig.cut else ('result', 'original')
        cut = orig.cut or res.cut
        return finish(NOT_COMPARABLE, 'same for the %d calls both made, but the %s was still running at the %s without a further call, while the %s %s' % (
            m, late, cut, done, (res if orig.cut else orig).how_ended()))
    if orig.end != res.end:
        at = _placeholder_at(res, result_text)
        if res.end == 'error' and (at or partial):
            where = 'at a placeholder (line %d: %s)' % at if at and at[0] else 'on %s' % at[1] if at else 'with an error; it is partial'
            return finish(NOT_COMPARABLE, 'same up to call %d, then the result stops %s; the original %s' % (m, where, orig.how_ended()))
        return finish(DIFFERENT, 'the same %d calls, but the original %s and the result %s' % (m, orig.how_ended(), res.how_ended()))
    if orig.end == 'error' and _strip_position(orig.end_message) != _strip_position(res.end_message):
        at = _placeholder_at(res, result_text)
        if at:
            return finish(NOT_COMPARABLE, 'same up to call %d, then the result stops at a placeholder (%s); the original %s' % (
                m, 'line %d: %s' % at if at[0] else at[1], orig.how_ended()))
        return finish(DIFFERENT, 'the same %d calls, but the runs stop with different errors' % m)
    co, cr = _Canon(), _Canon()
    go = sorted((n, ID_RE.sub('#', val)) for n, val in orig.globals)
    gr = sorted((n, ID_RE.sub('#', val)) for n, val in res.globals)
    if go != gr:
        only_o = [g for g in go if g not in gr]
        only_r = [g for g in gr if g not in go]
        v = finish(DIFFERENT, 'the same %d calls and the same ending, but the globals left behind differ' % m)
        for n, val in only_o[:6]:
            v.add('original: %s = %s' % (n, val[:160]))
        for n, val in only_r[:6]:
            v.add('result:   %s = %s' % (n, val[:160]))
        return v
    if n_r == 0 and not go:
        return finish(NOT_COMPARABLE, 'the result makes no call into the environment and leaves no global: there is nothing to compare '
                      '(the original %s after %d start-up calls)' % (orig.how_ended(), p))
    what = 'both return' if orig.end == 'returned' else 'both stop with the same error (%s)' % _strip_position(orig.end_message)[:120]
    return finish(SAME, 'the same %d calls and property writes, %s%s' % (m, what, ', the same %d globals left behind' % len(go) if go else ''))


# ---------------------------------------------------------------------------- the tool

def check(original, result, workdir=None, timeout=120.0, max_events=20000, options=('executor=1',), log=None):
    """Run both files and compare.  Returns a Verdict (code, headline, lines, data)."""
    log = log or (lambda s: None)
    t_end = time.time() + timeout
    own = None
    if workdir is None:
        own = tempfile.TemporaryDirectory(prefix='diffcheck-')
        workdir = own.name
    try:
        try:
            with open(result, encoding='latin-1') as f:
                result_text = f.read()
        except OSError as e:
            return Verdict(NOT_COMPARABLE, 'cannot read the result: %s' % e)
        if not os.path.isfile(original):
            return Verdict(NOT_COMPARABLE, 'cannot read the original: %s' % original)
        if not result_text.strip():
            return Verdict(NOT_COMPARABLE, 'the result file is empty')

        def pair(salt, budget):
            out = {}

            def one(key, path, cap):
                out[key] = run_script(path, workdir, '%s.salt%d' % (key, salt), salt, budget, cap, options)
            # the original gets room for its start-up calls in front of the script's
            ts = [threading.Thread(target=one, args=('original', original, 4 * max_events)),
                  threading.Thread(target=one, args=('result', result, max_events))]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            return out['original'], out['result']

        # two rounds at most (the second only when the first finds a difference): 45 % of the time each
        budget = max(2.0, 0.45 * timeout)
        orig, res = pair(1, budget)
        log('original: %d events in %.1fs, %s' % (len(orig.events), orig.seconds, orig.how_ended()))
        log('result:   %d events in %.1fs, %s' % (len(res.events), res.seconds, res.how_ended()))
        v = compare(orig, res, result_text)
        if v.code == SAME or orig.fatal or res.fatal:
            return v
        left = t_end - time.time() - 1.0
        if left < 2.0:
            v.add('(no time left for the second pair of runs: arguments that depend on time or chance were not told apart)')
            return v
        orig2, res2 = pair(2, min(budget, left))
        if orig2.fatal or res2.fatal:
            return v
        vol_o, same_o = volatile_args(orig, orig2)
        vol_r, same_r = volatile_args(res, res2)
        if not vol_o and not vol_r:
            return v
        log('second runs: %d + %d arguments depend on time or chance' % (len(vol_o), len(vol_r)))
        v2 = compare(orig, res, result_text, vol_o, vol_r)
        n_o, n_r = len(compared(orig)), len(compared(res))
        if same_o < min(n_o, len(compared(orig2))) or same_r < min(n_r, len(compared(res2))):
            v2.add('two runs of the same file took different paths (original after %d calls, result after %d): '
                   'its behaviour depends on time or chance' % (same_o, same_r))
        return v2
    finally:
        if own:
            own.cleanup()


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python3 -m devm.diffcheck', description=__doc__.split('\n\n')[0],
                                 epilog='exit status: 0 same, 1 different, 2 not comparable')
    ap.add_argument('original', help='the protected file')
    ap.add_argument('result', help='the recovered script')
    ap.add_argument('--work', help='directory for the logs of the runs (default: a temporary one)')
    ap.add_argument('--timeout', type=float, default=120.0, help='time limit for the whole check in seconds (default 120)')
    ap.add_argument('--max-events', type=int, default=20000, help='compare at most this many calls of the script (default 20000)')
    ap.add_argument('--no-executor', action='store_true', help='run both as a plain Roblox client (default: executor environment)')
    ap.add_argument('--json', action='store_true', help='print the verdict as one JSON object')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args(argv)
    options = [] if a.no_executor else ['executor=1']
    try:
        v = check(a.original, a.result, a.work, a.timeout, a.max_events, options,
                  log=(lambda s: print('[diffcheck] ' + s, file=sys.stderr)) if a.verbose else None)
    except Exception as e:      # the tool's own failure is "not comparable", with the reason
        v = Verdict(NOT_COMPARABLE, 'diffcheck failed: %s: %s' % (type(e).__name__, e))
    if a.json:
        print(json.dumps({'verdict': VERDICT_WORD[v.code], 'code': v.code, 'headline': v.headline, 'report': v.lines, **v.data}))
    else:
        print(v.text())
    return v.code


if __name__ == '__main__':
    sys.exit(main())
