#!/usr/bin/env python3
"""The all-in-one front end: deob.py picks the right engine and says what it did.

    python3 tests/allinone_test.py                   quick (about a minute)
    ALLINONE_FULL=1 python3 tests/allinone_test.py   also devirtualizes the v15.2 sample through
                                                     the front end (several minutes)

Needs bin/luau and bin/luau-ast (./setup.sh).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import allinone  # noqa: E402

SAMPLE = os.path.join(ROOT, "examples", "v15.2", "sample.luau")
LAYER3 = os.path.join(ROOT, "examples", "layer3.lua")
LUAU_AST = os.path.join(ROOT, "bin", "luau-ast")
FULL = bool(os.environ.get("ALLINONE_FULL"))

PLAIN = '''local Players = game:GetService("Players")
local function greet(name)
    print("hello " .. name)
end
greet(Players.LocalPlayer.Name)
'''


def deob(*args, timeout=600):
    r = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "deob.py")] + list(args), cwd=ROOT,
                       capture_output=True, text=True, errors="replace", timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def parses(path):
    # luau-ast reports a syntax error on stderr and by its exit status, not on stdout
    r = subprocess.run([LUAU_AST, path], capture_output=True, text=True, errors="replace")
    return r.returncode == 0 and "Parse errors" not in (r.stderr[:4000] + r.stdout[:4000])


class FakeScan:
    """What allinone.after_static reads from deob.Scan."""

    def __init__(self, inp, form, bare=False, buffer=None, evidence=("头注释",), layout=None, version=None):
        self.inp, self.form, self._bare, self._buffer, self._evidence = Path(inp), form, bare, buffer, list(evidence)
        self._layout, self.header = layout, {"version": version}

    def layout(self):
        return self._layout

    def bare_chunk(self):
        return self._bare

    def sibling_buffer(self):
        return self._buffer

    def evidence(self):
        return self._evidence


def flags(**kw):
    base = dict(list=False, force=False, no_run=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


class Decide(unittest.TestCase):
    """allinone.after_static: the automatic hand-over rule, without running an engine."""

    def setUp(self):
        self.say = allinone.say
        allinone.say = lambda *a: None

    def tearDown(self):
        allinone.say = self.say

    def test_vm_goes_to_devm(self):
        self.assertEqual(allinone.after_static(FakeScan(SAMPLE, "vm"), 4, flags()), ("devm", []))

    def test_v14_vm_goes_to_devm_only(self):
        for layout in ("fn-args", "table"):
            self.assertEqual(allinone.after_static(FakeScan(SAMPLE, "vm", layout=layout, version="14.3"), 4, flags()),
                             (("devm",), []))
        # a v14 container that did not open in the front end: the layout follows the version
        self.assertEqual(allinone.after_static(FakeScan(SAMPLE, "vm", version="14.7"), 4, flags()), (("devm",), []))

    def test_v150_goes_to_the_second_engine_first(self):
        self.assertEqual(allinone.after_static(FakeScan(SAMPLE, "vm", layout="setmt", version="15.0"), 4, flags()),
                         (("deobf", "devm"), []))
        self.assertEqual(allinone.after_static(FakeScan(SAMPLE, "vm", layout="setmt", version="15.2"), 4, flags()),
                         ("devm", []))

    def test_exit_8_is_final(self):
        self.assertIsNone(allinone.after_static(FakeScan(SAMPLE, "vm"), allinone.UNPROCESSABLE, flags()))

    def test_static_result_stands(self):
        self.assertIsNone(allinone.after_static(FakeScan(LAYER3, "A"), 0, flags()))
        self.assertIsNone(allinone.after_static(FakeScan(LAYER3, "A"), 3, flags()))

    def test_partial_form_a_goes_to_the_engines(self):
        # form A in a file with dispatch loops (deob.py exit 6): the main flow runs on the VM, so
        # the engines get the file, in the documented order; the static result is the fallback
        self.assertEqual(allinone.after_static(FakeScan(SAMPLE, "A", layout="setmt", version="15.1"), 6, flags()),
                         ("devm", []))
        self.assertEqual(allinone.after_static(FakeScan(SAMPLE, "A", layout="setmt", version="15.0"), 6, flags()),
                         (("deobf", "devm"), []))
        self.assertEqual(allinone.after_static(FakeScan(SAMPLE, "A", version="14.7"), 6, flags()), (("devm",), []))
        # a chunk cut out of its container goes with its buffer, or not at all
        self.assertIsNone(allinone.after_static(FakeScan(LAYER3, "A", bare=True), 6, flags()))
        self.assertEqual(allinone.after_static(FakeScan(LAYER3, "A", bare=True, buffer=Path("x.buffer.bin")), 6, flags()),
                         ("devm", ["--chunk-arg", "x.buffer.bin"]))
        for kw in ({"list": True}, {"force": True}, {"no_run": True}):
            self.assertIsNone(allinone.after_static(FakeScan(SAMPLE, "A"), 6, flags(**kw)), kw)


class Fallback(unittest.TestCase):
    """allinone.hand_over with the static result as fallback, engines replaced by stand-ins."""

    def setUp(self):
        self.saved = allinone.say, allinone.run_devm, allinone.run_deobf
        self.said = []
        allinone.say = lambda *a: self.said.append(" ".join(str(x) for x in a))
        self.tmp = tempfile.TemporaryDirectory()
        self.static = os.path.join(self.tmp.name, "static.lua")
        with open(self.static, "w") as f:
            f.write("local function kept() return 1 end\nreturn { kept }\n")
        self.out = os.path.join(self.tmp.name, "out.lua")

    def tearDown(self):
        allinone.say, allinone.run_devm, allinone.run_deobf = self.saved
        self.tmp.cleanup()

    def engines(self, devm, deobf):
        def fake(res, text):
            def run(inp, cand, **kw):
                if res.get("ok"):
                    with open(cand, "w") as f:
                        f.write(text)
                return dict(res)
            return run
        allinone.run_devm = fake(devm, "print('devm')\n")
        allinone.run_deobf = fake(deobf, allinone.TRACE_HEADER + "\nprint('trace')\n")

    def hand_over(self):
        return allinone.hand_over("in.lua", self.out, "devm", fallback=(self.static, "only the plain functions"))

    def test_no_engine_result_keeps_the_static_result_partial(self):
        none = {"ok": False, "complete": False, "reason": "x: no result", "why": [], "engine": "devm"}
        self.engines(none, dict(none, engine="deobf"))
        self.assertEqual(self.hand_over(), allinone.PARTIAL)
        with open(self.out) as f:
            self.assertIn("kept", f.read())
        self.assertEqual(self.said[-1], "result: PARTIAL: only the plain functions (static)")

    def test_a_trace_does_not_replace_the_static_result(self):
        self.engines({"ok": False, "complete": False, "reason": "devm: gave up", "why": [], "engine": "devm"},
                     {"ok": True, "complete": False, "kind": "trace", "why": ["a behaviour trace"], "engine": "deobf"})
        self.assertEqual(self.hand_over(), allinone.PARTIAL)
        with open(self.out) as f:
            self.assertIn("kept", f.read())
        self.assertEqual(self.said[-1], "result: PARTIAL: only the plain functions (static)")

    def test_a_lifted_result_replaces_the_static_one(self):
        self.engines({"ok": True, "complete": False, "why": ["1 LIFT ERROR"], "engine": "devm", "report": []},
                     {"ok": False, "complete": False, "reason": "unused", "why": [], "engine": "deobf"})
        self.assertEqual(self.hand_over(), allinone.PARTIAL)
        with open(self.out) as f:
            self.assertIn("devm", f.read())
        self.assertTrue(self.said[-1].startswith("result: PARTIAL: ") and self.said[-1].endswith("(devm)"), self.said[-1])

    def test_look_only_modes_hand_nothing_over(self):
        for kw in ({"list": True}, {"force": True}, {"no_run": True}):
            self.assertIsNone(allinone.after_static(FakeScan(SAMPLE, "vm"), 4, flags(**kw)), kw)

    def test_bare_chunk_needs_its_buffer(self):
        self.assertIsNone(allinone.after_static(FakeScan(LAYER3, "vm", bare=True), 4, flags()))
        self.assertEqual(allinone.after_static(FakeScan(LAYER3, "vm", bare=True, buffer=Path("x.buffer.bin")), 4, flags()),
                         ("devm", ["--chunk-arg", "x.buffer.bin"]))

    def test_binary_input_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "blob.bin")
            with open(p, "wb") as f:
                f.write(b"\0\1\2\3" * 100)
            self.assertFalse(allinone.looks_like_lua(p))
            self.assertIsNone(allinone.after_static(FakeScan(p, "unknown", evidence=()), 2, flags()))


def alive(pid):
    """True while process `pid` runs (a zombie nobody has reaped yet does not run)."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            stat = f.read().decode("latin-1")
        return stat[stat.rindex(")") + 2:].split()[0] != "Z"
    except (OSError, ValueError, IndexError):
        return False


def gone(pids, within=10.0):
    """True when none of `pids` runs any more `within` seconds from now."""
    import time
    end = time.time() + within
    while time.time() < end:
        if not any(alive(p) for p in pids):
            return True
        time.sleep(0.1)
    return not any(alive(p) for p in pids)


# A stand-in for an engine: it starts a child of its own (as devm starts `bin/luau
# devm/harness.lua`), prints both process ids and its session id, then does what MODE says.
FAKE_ENGINE = r"""
import os, subprocess, sys, time
mode = sys.argv[1]
kid = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("pids %d %d sid %d" % (os.getpid(), kid.pid, os.getsid(0)), flush=True)
if mode == "leave-child":       # the engine ends, its child keeps running
    sys.exit(0)
while True:                     # mode "hang"
    time.sleep(0.2)
"""


@unittest.skipUnless(os.name == "posix" and os.path.isdir("/proc"), "needs POSIX process groups and /proc")
class ProcessHygiene(unittest.TestCase):
    """allinone._stream: an engine and everything it started end with it, whatever happens."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fake = os.path.join(cls.tmp.name, "fake_engine.py")
        with open(cls.fake, "w") as f:
            f.write(FAKE_ENGINE)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.pids = []

    def tearDown(self):
        import signal
        for p in self.pids:             # whatever a failed test left behind
            if alive(p):
                try:
                    os.kill(p, signal.SIGKILL)
                except OSError:
                    pass

    def stream(self, mode, timeout):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            status, text = allinone._stream([sys.executable, self.fake, mode], timeout, cwd=self.tmp.name)
        words = text.split()
        self.pids = [int(words[1]), int(words[2])]
        return status, int(words[4])

    def test_timeout_kills_the_engine_and_its_child(self):
        status, sid = self.stream("hang", 2)
        self.assertIsNone(status)                       # None = stopped by the time limit
        self.assertTrue(gone(self.pids), "engine or its child still runs after the time limit")
        self.assertNotEqual(sid, os.getsid(0))          # the engine had a session of its own
        self.assertEqual(sid, self.pids[0])             # ... which it led

    def test_nothing_stays_behind_an_engine_that_ended(self):
        status, _sid = self.stream("leave-child", 60)
        self.assertEqual(status, 0)
        self.assertTrue(gone(self.pids), "the child of an engine that ended still runs")

    def front_end(self):
        """A front end of its own (a process that runs _stream on the fake engine); returns
        (Popen, [engine pid, child pid])."""
        code = ("import sys; sys.path.insert(0, %r); import allinone; "
                "allinone._stream([sys.executable, %r, 'hang'], 600, cwd=%r)" % (ROOT, self.fake, self.tmp.name))
        fe = subprocess.Popen([sys.executable, "-B", "-c", code], stdout=subprocess.PIPE, text=True,
                              start_new_session=True)
        words = fe.stdout.readline().split()
        self.assertEqual(words[0], "pids", words)
        self.pids = [int(words[1]), int(words[2])]
        self.assertTrue(all(alive(p) for p in self.pids))
        return fe

    def test_killed_front_end_takes_the_engine_along(self):
        import signal
        fe = self.front_end()
        os.killpg(fe.pid, signal.SIGKILL)               # what a supervising tool does on ITS time limit
        fe.wait()
        fe.stdout.close()
        self.assertTrue(gone(self.pids), "engine or its child outlives a front end that was killed (SIGKILL)")

    def test_terminated_front_end_takes_the_engine_along(self):
        import signal
        fe = self.front_end()
        fe.send_signal(signal.SIGTERM)
        self.assertEqual(fe.wait(timeout=20), 128 + signal.SIGTERM)
        fe.stdout.close()
        self.assertTrue(gone(self.pids), "engine or its child outlives a front end that was terminated (SIGTERM)")


@unittest.skipUnless(os.path.exists(LUAU_AST), "needs bin/luau-ast (./setup.sh)")
class FrontEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.plain = os.path.join(cls.tmp.name, "plain.lua")
        with open(cls.plain, "w") as f:
            f.write(PLAIN)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def out(self, name):
        return os.path.join(self.tmp.name, name)

    def test_plain_script_is_not_run(self):
        rc, log = deob(self.plain, "-o", self.out("p.lua"))
        self.assertEqual(rc, 2, log[-600:])
        self.assertIn("--engine trace", log)
        self.assertNotIn("hello", log)                  # the script's print never happened
        self.assertFalse(os.path.exists(self.out("p.lua")))

    def test_trace_on_request(self):
        rc, log = deob(self.plain, "-o", self.out("t.lua"), "--engine", "trace", "--engine-args=--no-pypy")
        self.assertEqual(rc, allinone.PARTIAL, log[-800:])
        with open(self.out("t.lua"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn(allinone.TRACE_HEADER, text)
        self.assertIn('GetService("Players")', text)
        self.assertIn("not the script's source", log)

    def test_static_only_never_starts_an_engine(self):
        rc, log = deob(SAMPLE, "-o", self.out("s.lua"), "--engine", "static")
        self.assertEqual(rc, 4, log[-600:])
        self.assertNotIn("[devm]", log)
        self.assertIn("v15.2", log)

    def test_form_a_without_its_buffer_stays_static_and_partial(self):
        # examples/layer3.lua is a v15.1 chunk cut out of its container (form A: plain functions
        # in the VM object, the main flow on the VM); its buffer is not next to it under the name
        # this tool saves it with, so no engine can run it: the static result stands, PARTIAL
        rc, log = deob(LAYER3, "-o", self.out("a.lua"))
        self.assertEqual(rc, allinone.PARTIAL, log[-600:])
        self.assertNotIn("[devm]", log)
        self.assertTrue(parses(self.out("a.lua")))
        self.assertEqual(log.rstrip().split("\n")[-1], "result: PARTIAL: only the functions kept as plain Lua; "
                         "the virtualized main flow is not in the output (static)")

    def test_form_a_goes_to_the_engines_and_falls_back_to_the_static_result(self):
        # the same chunk next to its buffer (the names deob.py saves them under): automatic mode
        # hands it to the engines; with a limit of a few seconds neither gives a result, and the
        # static result is what is left, PARTIAL
        d = tempfile.mkdtemp(dir=self.tmp.name)
        chunk = os.path.join(d, "l3.chunk.lua")
        shutil.copyfile(LAYER3, chunk)
        shutil.copyfile(os.path.join(ROOT, "examples", "blob4.bin"), os.path.join(d, "l3.buffer.bin"))
        rc, log = deob(chunk, "-o", self.out("fa.lua"), "--timeout", "4", timeout=600)
        self.assertEqual(rc, allinone.PARTIAL, log[-1200:])
        self.assertIn("[devm]", log)
        self.assertIn("--chunk-arg", log)
        self.assertTrue(parses(self.out("fa.lua")))
        with open(self.out("fa.lua"), encoding="utf-8") as f:
            self.assertIn("deob.py", f.read(400))           # the static result's own header
        self.assertEqual(log.rstrip().split("\n")[-1], "result: PARTIAL: only the functions kept as plain Lua; "
                         "the virtualized main flow is not in the output (static)")

    def test_missing_file(self):
        rc, _log = deob(self.out("nope.lua"))
        self.assertEqual(rc, 1)

    def test_time_limit_ends_a_real_engine(self):
        """The v15.2 example needs minutes in devm; with a limit of a few seconds the front end
        stops the engine, says so in the engine's name as its reason, writes no result, and
        leaves no interpreter of this tree running."""
        def harnesses():
            mine = set()
            for name in os.listdir("/proc") if os.path.isdir("/proc") else ():
                if not name.isdigit():
                    continue
                try:
                    with open("/proc/%s/cmdline" % name, "rb") as f:
                        cmd = f.read().decode("utf-8", "replace")
                except OSError:
                    continue
                if "harness.lua" in cmd and (ROOT in cmd or os.path.realpath(ROOT) in cmd) and alive(int(name)):
                    mine.add(int(name))
            return mine
        before = harnesses()
        rc, log = deob(SAMPLE, "-o", self.out("limit.lua"), "--engine", "devm", "--timeout", "6", timeout=300)
        self.assertEqual(rc, allinone.NO_RESULT, log[-800:])
        self.assertIn("devm: timed out after 6 s", log)
        self.assertFalse(os.path.exists(self.out("limit.lua")))
        self.assertNotIn("Traceback", log)
        left = set()
        for _ in range(50):
            left = harnesses() - before
            if not left:
                break
            import time
            time.sleep(0.1)
        self.assertFalse(left, "interpreter processes left behind: %s" % sorted(left))

    @unittest.skipUnless(FULL, "set ALLINONE_FULL=1 (several minutes)")
    def test_v152_goes_through_devm(self):
        rc, log = deob(SAMPLE, "-o", self.out("v152.lua"), timeout=3000)
        self.assertIn("[devm]", log)
        self.assertIn(rc, (allinone.OK, allinone.PARTIAL), log[-1500:])
        self.assertTrue(parses(self.out("v152.lua")))
        self.assertIn("traced run:", log)               # the engine's own report came through
        if rc == allinone.PARTIAL:
            self.assertIn("result: PARTIAL", log)
        with open(self.out("v152.lua"), encoding="utf-8") as f:
            text = f.read()
        # the script inside is a key-system loader that stops at its server request: the request
        # is in the result, and nothing after it was made up
        self.assertIn("SCRIPT_KEY", text)
        self.assertIn("request", log)


if __name__ == "__main__":
    unittest.main(verbosity=1)
