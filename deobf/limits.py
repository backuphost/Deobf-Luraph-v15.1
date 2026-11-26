"""
The engine's wall-clock limit (deob.py --time-limit) and its child processes.

One deadline for the whole command. Every long step asks it: a harness run
gets the time that is left as its timeout, the lifter's walks and the thread
the lift runs in (backend.run_big_stack) raise TimeLimit, and the plugin then
writes what it has (normally the behaviour trace) instead of the lifted text.

Child processes (the Luau harness runs) are started with spawn(): they die
with this process, also when it is killed. Work directories made with
temp_dir() are removed when the engine exits or is terminated (after a
SIGKILL they stay: set TMPDIR to choose where they are made).
"""
import atexit
import ctypes
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

DEADLINE = [None]       # time.time() value, or None: no limit
LIMIT = [0]             # the limit in seconds as given (for messages)
CHILDREN = []           # running child processes (subprocess.Popen)
TEMP_DIRS = []          # work directories to remove at the end


class TimeLimit(Exception):
    """The time limit was reached (not an `Unsupported`: nothing may swallow
    it as "this one function could not be lifted")."""


def set_limit(seconds):
    LIMIT[0] = seconds or 0
    DEADLINE[0] = time.time() + seconds if seconds else None


def left():
    """Seconds until the deadline (never below 0), or None without a limit."""
    return None if DEADLINE[0] is None else max(0.0, DEADLINE[0] - time.time())


def expired():
    return DEADLINE[0] is not None and time.time() >= DEADLINE[0]


def check(what):
    if expired():
        raise TimeLimit("time limit of %d s reached while %s" % (LIMIT[0], what))


def clamp(timeout):
    """A step's own timeout, cut to the time that is left (at least 1 s, so
    that the step still reports a time-out instead of a bad argument)."""
    rest = left()
    return timeout if rest is None else max(1, min(timeout, rest))


# --------------------------------------------------------------------------
# child processes

try:
    _LIBC = ctypes.CDLL(None, use_errno=True) if sys.platform.startswith("linux") else None
except OSError:
    _LIBC = None
_PR_SET_PDEATHSIG = 1


def _die_with_parent():
    # (in the child, between fork and exec: the kernel kills it when the
    # thread that started it is gone, however that came about)
    _LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)


def spawn(cmd, **kw):
    """subprocess.Popen for a child that must not outlive the engine: on
    Linux the kernel kills it when the engine dies (even by SIGKILL);
    everywhere it is killed when the engine exits or is terminated."""
    if _LIBC is not None and "preexec_fn" not in kw:
        kw["preexec_fn"] = _die_with_parent
    proc = subprocess.Popen(cmd, **kw)
    CHILDREN.append(proc)
    return proc


def reap(proc):
    """Forget a child that has ended (or kill it first)."""
    try:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    except OSError:
        pass
    if proc in CHILDREN:
        CHILDREN.remove(proc)


def kill_children():
    for proc in list(CHILDREN):
        try:
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass
    del CHILDREN[:]


def temp_dir(prefix):
    """tempfile.mkdtemp() for a work directory that is removed when the
    engine ends, also when it is terminated by a signal."""
    d = tempfile.mkdtemp(prefix=prefix)
    TEMP_DIRS.append(d)
    return d


def remove_temp(path=None):
    """Remove one work directory (or all of them)."""
    for d in ([path] if path else list(TEMP_DIRS)):
        shutil.rmtree(d, ignore_errors=True)
        if d in TEMP_DIRS:
            TEMP_DIRS.remove(d)


def _on_signal(signum, frame):
    kill_children()
    remove_temp()
    try:
        sys.stderr.write("deobf: terminated by signal %d\n" % signum)
        sys.stderr.flush()
    except OSError:
        pass
    os._exit(128 + signum)


def install():
    """Kill the children at exit and when the engine is asked to stop."""
    atexit.register(kill_children)
    atexit.register(remove_temp)
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                pass    # (not the main thread, or not supported here)
