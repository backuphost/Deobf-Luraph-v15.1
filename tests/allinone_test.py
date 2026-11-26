#!/usr/bin/env python3
"""The all-in-one front end: deob.py picks the right engine and says what it did.

    python3 tests/allinone_test.py                   quick (about a minute)
    ALLINONE_FULL=1 python3 tests/allinone_test.py   also devirtualizes the v15.2 sample through
                                                     the front end (several minutes)

Needs bin/luau and bin/luau-ast (./setup.sh).
"""
import os
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
    out = subprocess.run([LUAU_AST, path], capture_output=True, text=True, errors="replace").stdout
    return "Parse errors" not in out[:4000]


class FakeScan:
    """What allinone.after_static reads from deob.Scan."""

    def __init__(self, inp, form, bare=False, buffer=None, evidence=("头注释",)):
        self.inp, self.form, self._bare, self._buffer, self._evidence = Path(inp), form, bare, buffer, list(evidence)

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

    def test_static_result_stands(self):
        self.assertIsNone(allinone.after_static(FakeScan(LAYER3, "A"), 0, flags()))
        self.assertIsNone(allinone.after_static(FakeScan(LAYER3, "A"), 3, flags()))

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

    def test_form_a_stays_static(self):
        rc, log = deob(LAYER3, "-o", self.out("a.lua"))
        self.assertEqual(rc, 0, log[-600:])
        self.assertNotIn("[devm]", log)
        self.assertTrue(parses(self.out("a.lua")))

    def test_missing_file(self):
        rc, _log = deob(self.out("nope.lua"))
        self.assertEqual(rc, 1)

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
