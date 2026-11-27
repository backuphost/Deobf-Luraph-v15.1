#!/usr/bin/env python3
"""Checks of the second engine (deobf/): its contract with the front end and the lifter fixes
that the public Luraph corpus asked for.

    python3 tests/deobf_corpus.py [NAME ...]      only the checks whose name contains one of NAME

Without the corpus: the final `result:` / `deobf:` line, the time limit, no harness process left
behind when the engine is killed, unit checks of the lifter changes on hand-made input, and the
environment's answers on hand-made scripts (instances a script made, Path2D bounding boxes).
With the corpus (environment variable LURAPH_CORPUS = the directory that holds one directory per
Luraph version): the engine on files picked by directory and property at run time. Nothing of a
corpus file is kept or printed here; the scripts are run only inside the engine's own sandbox.

Exit status 0 when every check passes (skipped checks do not fail).
"""
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEOBF = os.path.join(ROOT, "deobf")
DEOB = os.path.join(DEOBF, "deob.py")
PY = sys.executable
CORPUS = os.environ.get("LURAPH_CORPUS")
SLOW = 4 if os.environ.get("DEOBF_TEST_SLOW") else 1     # stretch the time limits on a loaded machine

sys.path.insert(0, DEOBF)
sys.path.append(HERE)     # (tests/path2d_cases.py; after deobf/, whose modules come first)


class Skip(Exception):
    pass


def engine(args, timeout=120):
    """(exit status, stdout lines, stderr lines) of deob.py."""
    r = subprocess.run([PY, "-B", DEOB] + args, capture_output=True, text=True, errors="replace",
                       timeout=timeout * SLOW, cwd=ROOT)
    return r.returncode, r.stdout.strip().split("\n"), r.stderr.strip().split("\n")


def parses(path):
    ast = os.path.join(ROOT, "bin", "luau-ast")
    r = subprocess.run([ast, path], capture_output=True, text=True, errors="replace", timeout=300)
    return r.returncode == 0 and "Parse errors" not in r.stderr[:4000]


def write(d, name, text):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


# --------------------------------------------------------------------------
# without the corpus

def check_no_syntax_warnings():
    """Every module compiles without a warning (an invalid escape sequence is one)."""
    bad = []
    for base, _, files in os.walk(DEOBF):
        for name in files:
            if not name.endswith(".py"):
                continue
            p = os.path.join(base, name)
            with open(p, encoding="utf-8") as f:
                src = f.read()
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                compile(src, p, "exec")
            bad += ["%s:%s %s" % (os.path.relpath(p, ROOT), x.lineno, x.message) for x in w]
    assert not bad, bad


def check_result_line_trace():
    """A behaviour trace is reported as PARTIAL, on the last line of standard output, status 0."""
    with tempfile.TemporaryDirectory() as d:
        src = write(d, "own.lua", 'print("own test script")\n')
        out = os.path.join(d, "out.lua")
        rc, so, se = engine([src, "-o", out])
        assert rc == 0, (rc, se[-3:])
        assert so[-1].startswith("result: PARTIAL: the output is the behaviour trace"), so[-1]
        assert "run status: finished" in so[-1], so[-1]
        assert os.path.getsize(out) > 0
        assert any(ln.startswith("[*] run status: finished") for ln in se), se


def check_failure_line():
    """Nothing produced: status 1 and one last line `deobf: <reason>`, no traceback."""
    with tempfile.TemporaryDirectory() as d:
        rc, so, se = engine([os.path.join(d, "missing.lua"), "-o", os.path.join(d, "out.lua")])
        assert rc == 1, rc
        assert se[-1].startswith("deobf: cannot read the input"), se[-1]
        assert not any("Traceback" in ln for ln in se), se
        assert not any(ln.startswith("result:") for ln in so), so
        # a script that fails to compile still gives a trace of that (the engine's own report)
        src = write(d, "broken.lua", "this is not Lua (\n")
        rc, so, se = engine([src, "-o", os.path.join(d, "out.lua")])
        assert (rc == 0 and so[-1].startswith("result: PARTIAL")) or (rc == 1 and se[-1].startswith("deobf: ")), \
            (rc, so[-1:], se[-1:])


def check_time_limit_run():
    """--time-limit ends a run that does not end by itself, with the reason in the last line."""
    with tempfile.TemporaryDirectory() as d:
        # (half a minute of the script's own computing: the run's other limits are set far above)
        src = write(d, "own_clock.lua", "local t = os.clock() while os.clock() - t < 30 do end print('done')\n")
        t = time.time()
        rc, so, se = engine([src, "-o", os.path.join(d, "out.lua"), "--time-limit", "3", "--budget", "600",
                             "--timeout", "600"])
        took = time.time() - t
        assert 2.5 < took < 25 * SLOW, took
        assert rc == 1 and se[-1] == "deobf: time limit of 3 s reached during a traced run", (rc, se[-1])
        assert not any(ln.startswith("result:") for ln in so), so


def check_time_limit_lift():
    """The lift thread is given up at the deadline (backend.run_big_stack raises limits.TimeLimit)."""
    code = ("import sys, time; sys.path.insert(0, %r)\n"
            "import limits, backend\n"
            "limits.set_limit(1)\n"
            "t = time.time()\n"
            "try:\n"
            "    backend.run_big_stack(time.sleep, 30)\n"
            "    print('no limit')\n"
            "except limits.TimeLimit as e:\n"
            "    print('limit %%.1f %%s' %% (time.time() - t, e))\n"
            "sys.stdout.flush(); import os; os._exit(0)\n" % DEOBF)
    r = subprocess.run([PY, "-B", "-c", code], capture_output=True, text=True, timeout=60 * SLOW)
    m = re.match(r"limit (\d+\.\d) time limit of 1 s reached while lifting", r.stdout.strip())
    assert m and float(m.group(1)) < 10 * SLOW, (r.stdout, r.stderr[-300:])
    # without a limit nothing changes
    code = ("import sys; sys.path.insert(0, %r)\nimport backend\nprint(backend.run_big_stack(lambda a, b: a + b, 2, 3))\n"
            % DEOBF)
    r = subprocess.run([PY, "-B", "-c", code], capture_output=True, text=True, timeout=60 * SLOW)
    assert r.stdout.strip() == "5", (r.stdout, r.stderr[-300:])


def _harness_pids(tag):
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                cmd = f.read()
        except OSError:
            continue
        if tag.encode() in cmd and b"harness" in cmd:
            out.append(int(pid))
    return out


def check_no_orphan():
    """Killing the engine (SIGKILL, SIGTERM) leaves no harness process behind."""
    if not sys.platform.startswith("linux"):
        raise Skip("needs /proc")
    for sig in (signal.SIGKILL, signal.SIGTERM):
        with tempfile.TemporaryDirectory() as d:
            tag = "own_spin_%d_%d" % (os.getpid(), int(sig))
            # (a busy loop: the harness process never ends by itself within the test)
            src = write(d, tag + ".lua", "while true do end\n")
            # (TMPDIR: the engine's work directory is made in here; a SIGKILL leaves it behind)
            work = os.path.join(d, "tmp")
            os.mkdir(work)
            proc = subprocess.Popen([PY, "-B", DEOB, src, "-o", os.path.join(d, "out.lua")], cwd=ROOT,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                    env=dict(os.environ, TMPDIR=work))
            try:
                deadline = time.time() + 15 * SLOW
                while not _harness_pids(tag) and time.time() < deadline and proc.poll() is None:
                    time.sleep(0.1)
                assert _harness_pids(tag), "the harness did not start"
                proc.send_signal(sig)
                err = proc.communicate(timeout=20 * SLOW)[1]
                deadline = time.time() + 5 * SLOW
                while _harness_pids(tag) and time.time() < deadline:
                    time.sleep(0.1)
                left = _harness_pids(tag)
                assert not left, "harness left running after signal %d: %s" % (sig, left)
                if sig == signal.SIGTERM:
                    assert proc.returncode == 128 + sig, proc.returncode
                    assert err.strip().split("\n")[-1] == "deobf: terminated by signal %d" % sig, err[-200:]
                    assert os.listdir(work) == [], "work directory left after SIGTERM: %s" % os.listdir(work)
            finally:
                if proc.poll() is None:
                    proc.kill()
                for pid in _harness_pids(tag):
                    os.kill(pid, signal.SIGKILL)


def check_pack_site():
    """A pack's identity for the walk does not depend on the visit that made it."""
    from obfuscators.luraph_v15 import devirt
    a = devirt.pack_site("pack(T2651_13556_1[1...])")
    assert a == devirt.pack_site("pack(T2651_17910_1[1...])") == "pack(T2651_1[1...])", a
    assert devirt.pack_site("pack(T2651_7_1[1...])") != devirt.pack_site("pack(T2652_7_1[1...])")
    assert devirt.pack_site("pack(vararg[1...])") == "pack(vararg[1...])"
    assert devirt.pack_site("pack(T-12_5_2[1], T3_9_1[2...])") == "pack(T-12_2[1], T3_1[2...])"


def check_not_reached_unknown():
    """When the script's main function cannot be told (the check raises), not_reached says so (None):
    the driver then adds the lack that the output may start with Luraph's start-up code, instead of
    taking the lift for the script's."""
    import contextlib
    import io
    from obfuscators.luraph_v15 import driver

    class Job:
        source_path = os.path.join(tempfile.gettempdir(), "no-such-dir-%d" % os.getpid(), "missing.lua")
    with contextlib.redirect_stderr(io.StringIO()) as err:
        got = driver.not_reached(Job(), Job.source_path + ".protos.json", [])
    assert got is None, got
    assert "could not tell the script's main function" in err.getvalue(), err.getvalue()


def check_nested_root_lack():
    """Luraph v15.2 nested shapes: the lack 'the output includes Luraph's start-up code' only when
    the lifted root is the program the VM entered first (__seq 1, no root_callee); a payload VM
    made apart from the start-up VM is lifted without that code."""
    from obfuscators.luraph_v15 import devirt, driver

    def prog(protos, root_callee=None):
        dump = type("Dump", (), {})()
        dump.protos = protos
        dump.raw = {"root_callee": root_callee} if root_callee is not None else {}
        p = type("Prog", (), {})()
        p.dump = dump
        return p
    one_vm = {"1": {"__maker": b"m@0,1", "__seq": 1}, "5": {"__maker": b"m@0,1", "__seq": 2}}
    assert devirt.lifts_first_program(prog(one_vm))
    # the payload VM is made after the start-up VM: its root (seq 3) is lifted, not the first program
    two_vms = dict(one_vm, **{"9": {"__maker": b"p@0,9", "__seq": 3}, "12": {"__maker": b"p@0,9", "__seq": 4}})
    assert [pid for _t, pid, _c in devirt.program_roots(prog(two_vms))] == ["9"]
    assert not devirt.lifts_first_program(prog(two_vms))
    # the run named the script's main function: the lift starts there
    assert not devirt.lifts_first_program(prog(one_vm, root_callee="5"))
    assert devirt.nested_root_kind(prog(one_vm)) == "first"
    assert devirt.nested_root_kind(prog(two_vms)) == "separate"
    # the run named a function of the first program's VM: the lift starts there and leaves out the
    # script's top level, which sits inside the first program
    assert devirt.nested_root_kind(prog(one_vm, root_callee="5")) == "callee"
    assert driver.root_lack("nested", "callee") == driver.NESTED_CALLEE
    assert driver.root_lack("nested", "first") == driver.NESTED_START_UP
    assert driver.root_lack("nested", "separate") is None
    assert driver.root_lack("nested", True) == driver.NESTED_START_UP
    assert driver.root_lack("nested", False) is None
    assert driver.root_lack("nested", None) == driver.MAIN_UNKNOWN
    assert driver.root_lack(None, None) == driver.MAIN_UNKNOWN
    assert driver.root_lack(False, None) is None


def check_start_up_only_lack():
    """A Luraph v15.2 lift that starts with Luraph's start-up code (NESTED_START_UP) from a run the spin
    watchdog stopped (the start-up code's tamper loop: VM code that spins without touching the
    environment): the result line says first, plainly, that only the start-up code was recovered and the
    run did not get past it, instead of "includes Luraph's start-up code, not separated from the script"
    and "the traced run did not end (... endless loop in the script's own code ...)"; the lifted file
    says so in its header."""
    from obfuscators.luraph_v15 import driver
    spin = "aborted: endless loop in the script's own code (no environment access)"
    got = driver.kept_lift_lacks(["3 unlifted blocks", driver.NESTED_START_UP], spin, True)
    assert got == [driver.START_UP_ONLY, "3 unlifted blocks"], got
    assert driver.START_UP_ONLY.startswith("only Luraph's start-up code was recovered: the traced run did not "
                                           "get past it"), driver.START_UP_ONLY
    assert "tamper response" in driver.START_UP_ONLY
    # another way of stopping, or a lift that does not start with the start-up code: as before
    got = driver.kept_lift_lacks([driver.NESTED_START_UP], "aborted: statement limit reached", True)
    assert got == [driver.NESTED_START_UP, "the traced run did not end (aborted: statement limit reached)"], got
    assert driver.kept_lift_lacks(["x"], spin, True) == ["x", "the traced run did not end (%s)" % spin]
    assert driver.kept_lift_lacks([driver.NESTED_START_UP], "finished", True) == [driver.NESTED_START_UP]
    assert driver.kept_lift_lacks([], "script error: boom", True) == [
        "the traced run stopped (script error: boom): Luraph v15.2 code is recovered only as far as the run got"]
    assert driver.kept_lift_lacks([], "script error: boom", False) == []
    # a run stopped in the start-up code of an earlier layout (the trace is written): the loop is named
    assert driver.start_up_status(spin) == "Luraph's tamper response, an endless loop without environment " \
                                           "access; run status: " + spin
    assert driver.start_up_status("aborted: statement limit reached") == "aborted: statement limit reached"
    # the header note: comment lines after the file's own header, the code untouched
    out = driver.header_note("-- a\n-- b\n\nlocal x = 1\n", driver.START_UP_ONLY).split("\n")
    assert out[:2] == ["-- a", "-- b"] and out[2].startswith("-- NOTE: only Luraph's start-up code"), out
    assert out[-3:] == ["", "local x = 1", ""] and all(ln.startswith("--") for ln in out[:-3]), out
    assert " ".join(ln[9:] for ln in out[2:-3]) == driver.START_UP_ONLY


def check_chunk_params():
    """The main function's parameters at the top level never give `local ... = ...`."""
    from obfuscators.luraph_v15 import devirt
    assert devirt.chunk_lines([], ["x()"]) == ["x()"]
    assert devirt.chunk_lines(["..."], ["x(...)"]) == ["x(...)"]
    assert devirt.chunk_lines(["a", "b"], ["x(a)"]) == ["local a, b = ...", "x(a)"]
    assert devirt.chunk_lines(["a", "..."], ["x(a, ...)"]) == ["return (function(a, ...)", "\tx(a, ...)", "end)(...)"]
    with tempfile.TemporaryDirectory() as d:
        for params in ([], ["..."], ["a", "b"], ["a", "..."]):
            p = write(d, "c.lua", "\n".join(devirt.chunk_lines(params, ["print(%s)" % ", ".join(params or ["1"])])) + "\n")
            assert parses(p), params


def check_run_once_forin():
    """A generic `for` whose body always returns: the hidden iterator call becomes `f(s, ctl)`."""
    import ir as D
    import loops
    import structure
    from luasym import Const, Reg, Pseudo, Bin, TempVal, Multi

    def graph(extra=()):
        h, body, ex = structure.Block(1), structure.Block(2), structure.Block(3)
        it = Pseudo("a", 1)
        h.stmts = [D.Assign(Reg(9), Const(1)),
                   D.Assign(it, D.GenIter(Multi([Reg(0), Reg(4), Reg(5), Reg(6)]))),
                   D.Assign(Reg(7), Const(None))] + list(extra) + [D.CallStmt("T1", Pseudo("a", 1), Multi([]))]
        h.kind, h.cond, h.succ = "cond", TempVal("T1", 1), [2, 3]
        body.stmts = [D.Assign(Reg(7), TempVal("T1", 2)), D.Assign(Reg(8), TempVal("T1", 3))]
        body.kind, body.values = "ret", Multi([Reg(7), Bin("Add", TempVal("T1", 3), Const(1))])
        ex.kind, ex.values = "ret", Multi([])
        return {1: h, 2: body, 3: ex}

    blocks = graph()
    h, body = blocks[1], blocks[2]
    assert loops._run_once_forin(h, blocks, D) is False
    call = h.stmts[1]
    assert [type(s).__name__ for s in h.stmts] == ["Assign", "CallStmt", "Assign"], h.stmts
    assert isinstance(call.fn, Reg) and call.fn.n == 4 and [x.n for x in call.args.items] == [5, 6], call.__dict__
    assert call.t == "T1" and call.args.tail is None
    c = h.cond
    assert isinstance(c, Bin) and c.op == "CompareNe" and c.a == TempVal("T1", 1) and isinstance(c.b, Const) \
        and c.b.v is None, c.__dict__
    assert body.stmts[0].value == TempVal("T1", 1) and body.stmts[1].value == TempVal("T1", 2)
    assert body.values.items[1].a == TempVal("T1", 2)
    # not when something else than register moves stands between the prep and the call
    blocks = graph([D.CallStmt("T0", Reg(3), Multi([]))])
    before = list(blocks[1].stmts)
    loops._run_once_forin(blocks[1], blocks, D)
    assert blocks[1].stmts == before and isinstance(blocks[1].cond, TempVal)
    # the whole of recognize(): no call of the hidden slot is left
    blocks = graph()
    for b in blocks.values():
        b.preds = []
    blocks[2].preds = blocks[3].preds = [1]
    loops.recognize(1, blocks, D)
    assert not any(isinstance(s, D.CallStmt) and isinstance(s.fn, Pseudo) for s in blocks[1].stmts)


INSTANCE_SCRIPT = """
local f = Instance.new("Folder")
print("t1", f:HasTag("a"))
f:AddTag("a"); f:AddTag("b"); f:AddTag("a"); f:AddTag(7)
print("t2", f:HasTag("a"), #f:GetTags(), f:GetTags()[1], f:GetTags()[2], f:GetTags()[3], f:HasTag("7"))
f:RemoveTag("a")
print("t3", f:HasTag("a"), #f:GetTags(), f:GetTags()[1])
local cs = game:GetService("CollectionService")
cs:AddTag(f, "z")
print("t4", cs:HasTag(f, "z"), f:HasTag("z"), #cs:GetTags(f), cs:HasTag(f, "nope"))
print("t5", pcall(f.AddTag, f))
print("t6", workspace:HasTag("x") and "guess" or "no")
workspace:AddTag("mine")
print("t7", workspace:HasTag("mine"), type(f:GetTags()), rawequal(f:GetTags(), f:GetTags()))
print("a1", f:GetAttribute("k"), type(f:GetAttributes()), next(f:GetAttributes()))
f:SetAttribute("k", 5)
print("a2", f:GetAttribute("k"), next(f:GetAttributes()))
f:SetAttribute("k", nil)
print("a3", f:GetAttribute("k"), next(f:GetAttributes()))
print("c1", f:FindFirstChildOfClass("Part"), f:FindFirstChildWhichIsA("BasePart"))
local p = Instance.new("Part", f)
print("c2", f:FindFirstChildOfClass("Part") == p, f:FindFirstChildWhichIsA("BasePart") == p,
	f:FindFirstChildOfClass("BasePart"), f:FindFirstChildOfClass("Folder"))
"""

INSTANCE_WANT = [
    'print("t1", false)',
    'print("t2", true, 3, "a", "b", "7", true)',
    'print("t3", false, 2, "b")',
    'print("t4", true, true, 3, false)',
    'print("t5", false, "Argument 1 missing or nil")',
    'print("t6", "guess")',
    'print("t7", true, "table", false)',
    'print("a1", nil, "table", nil)',
    'print("a2", 5, "k", 5)',
    'print("a3", nil, nil)',
    'print("c1", nil, nil)',
    'print("c2", true, true, nil, nil)',
]


def check_instance_model():
    """What a script did to an instance it made is what it reads back (tags, attributes, children by
    class): Luraph v15.1/v15.2 probe exactly this, and a stand-in answer sends them into their tamper loop.
    On an instance of the place an unknown answer stays the usual guess."""
    with tempfile.TemporaryDirectory() as d:
        src = write(d, "own.luau", INSTANCE_SCRIPT)
        out = os.path.join(d, "out.luau")
        rc, so, se = engine([src, "--obfuscator", "generic", "--no-devirt", "-o", out])
        assert rc == 0 and os.path.exists(out), (rc, so[-1:], se[-1:])
        with open(out, encoding="utf-8") as f:
            got = [l.strip() for l in f if l.strip().startswith("print(")]
        assert got == INSTANCE_WANT, [g for g in got if g not in INSTANCE_WANT] + ["missing:"] + \
            [w for w in INSTANCE_WANT if w not in got]


PATH2D_SCRIPT = """
local sg = Instance.new("ScreenGui")
local f = Instance.new("Frame")
f.Size = UDim2.fromOffset(300, 200)
f.Parent = sg
local p = Instance.new("Path2D")
p.Parent = f
-- a straight segment (10, 20) - (110, 70)
p:SetControlPoints({ Path2DControlPoint.new(UDim2.fromOffset(10, 20)), Path2DControlPoint.new(UDim2.fromOffset(110, 70)) })
local r = p:GetBoundingRect()
print("line", typeof(r), r.Min.X, r.Min.Y, r.Max.X, r.Max.Y, r.Width, r.Height)
p.Thickness = 3
r = p:GetBoundingRect()
print("thick", r.Min.X, r.Min.Y, r.Max.X, r.Max.Y)
p.Thickness = 1
-- handles (50, 10) and (150, 10): the box holds them (the curve itself only reaches y = 20)
p:SetControlPoints({ Path2DControlPoint.new(UDim2.fromOffset(20, 50), UDim2.new(), UDim2.fromOffset(30, -40)),
    Path2DControlPoint.new(UDim2.fromOffset(180, 50), UDim2.fromOffset(-30, -40), UDim2.new()) })
r = p:GetBoundingRect()
print("curve", r.Min.X, r.Min.Y, r.Max.X, r.Max.Y, tostring(r))
-- Scale and Offset in the 300 x 200 frame: points (85, 80), (220, 50); handles (120, 30), (220, 190);
-- the first point's left handle (-65, 80) shapes no segment
p:SetControlPoints({ Path2DControlPoint.new(UDim2.new(0.25, 10, 0.5, -20), UDim2.new(-0.5, 0, 0, 0), UDim2.new(0.1, 5, 0, -50)),
    Path2DControlPoint.new(UDim2.new(0.75, -5, 0.25, 0), UDim2.new(0, 0, 0.5, 40), UDim2.new()) })
r = p:GetBoundingRect()
print("mixed", r.Min.X, r.Min.Y, r.Max.X, r.Max.Y, r.Width, r.Height)
-- the point list edits move the curve; GetControlPoint gives the stored point
local cp = p:GetControlPoint(1)
print("cp", typeof(cp), cp.RightTangent.Y.Offset)
p:InsertControlPoint(2, Path2DControlPoint.new(UDim2.new(0.5, 0, 1, 10)))
r = p:GetBoundingRect()
print("insert", r.Max.Y, #p:GetControlPoints())
p:RemoveControlPoint(2)
r = p:GetBoundingRect()
print("remove", r.Max.Y, #p:GetControlPoints())
"""

# hand-computed: the box of each point and of the tangent handles that shape a segment, in the parent
# Frame's pixels, widened by Thickness / 2 on every side (envlog.luau, CHAIN.p2d.rect)
PATH2D_WANT = [
    'print("line", "Rect", 9.5, 19.5, 110.5, 70.5, 101, 51)',
    'print("thick", 8.5, 18.5, 111.5, 71.5)',
    'print("curve", 19.5, 9.5, 180.5, 50.5, "19.5, 9.5, 180.5, 50.5")',
    'print("mixed", 84.5, 29.5, 220.5, 190.5, 136, 161)',
    'print("cp", "Path2DControlPoint", -50)',
    'print("insert", 210.5, 3)',
    'print("remove", 190.5, 2)',
]


def check_path2d_rect():
    """Path2D:GetBoundingRect, GetControlPoint and the point list edits come from the offline model (Luraph
    v15.1/v15.2 start-up code calls them): a Rect with the model's numbers, no "does not cover" warning."""
    with tempfile.TemporaryDirectory() as d:
        src = write(d, "own.luau", PATH2D_SCRIPT)
        out = os.path.join(d, "out.luau")
        rc, so, se = engine([src, "--obfuscator", "generic", "--no-devirt", "-o", out])
        assert rc == 0 and os.path.exists(out), (rc, so[-1:], se[-1:])
        assert not any("offline model does not cover" in ln for ln in se), se
        with open(out, encoding="utf-8") as f:
            got = [l.strip() for l in f if l.strip().startswith("print(")]
        assert got == PATH2D_WANT, [g for g in got if g not in PATH2D_WANT] + ["missing:"] + \
            [w for w in PATH2D_WANT if w not in got]


def _print_args(line):
    """The arguments of an output line `print(a, "b, c", 1.5)`: strings unquoted, numbers as written."""
    inner = line[line.index("(") + 1:line.rindex(")")]
    out = []
    for tok in re.findall(r'"(?:[^"\\]|\\.)*"|[^,\s][^,]*', inner):
        tok = tok.strip()
        out.append(tok[1:-1] if tok.startswith('"') else tok)
    return out


def _path2d_answers(script):
    """{tag: [printed values]} of a hand-written Path2D script run in the engine's environment, and its stderr."""
    with tempfile.TemporaryDirectory() as d:
        src = write(d, "own.luau", script)
        out = os.path.join(d, "out.luau")
        rc, so, se = engine([src, "--obfuscator", "generic", "--no-devirt", "-o", out])
        assert rc == 0 and os.path.exists(out), (rc, so[-1:], se[-1:])
        with open(out, encoding="utf-8") as f:
            lines = [_print_args(l.strip()) for l in f if l.strip().startswith("print(")]
    return {a[0]: a[1:] for a in lines if a}, se


def check_path2d_stored():
    """Points as the engine stores them (float32 Scale, int32 Offset truncated toward zero; UDim2:Lerp
    rounds the offsets): the same Rect and length as devm's mock gives (tests/path2d_cases.py, also run
    by tests/mock_test.py)."""
    from path2d_cases import PATH2D_STORED, PATH2D_STORED_WANT
    got, se = _path2d_answers(PATH2D_STORED)
    for tag, want in PATH2D_STORED_WANT.items():
        vals = got.get(tag)
        assert vals and vals[:-1] == want[:-1], (tag, vals, want)
        assert abs(float(vals[-1]) - want[-1]) < 0.005, (tag, vals[-1], want[-1])


def check_path2d_query_time():
    """The curve is measured against the parent Frame as it is when asked (points set before the parent
    and before a resize, as devm does); GetBoundingRect of a one-point path is a stand-in of type Rect,
    with the "does not cover" warning (tests/path2d_cases.py, also run by tests/mock_test.py)."""
    from path2d_cases import PATH2D_LATE, PATH2D_LATE_WANT
    got, se = _path2d_answers(PATH2D_LATE)
    for tag, want in PATH2D_LATE_WANT.items():
        assert got.get(tag) == [str(w) for w in want], (tag, got.get(tag), want)
    assert any("offline model does not cover" in ln for ln in se), se


def _block(stmts, values=None):
    import structure
    b = structure.Block(1)
    b.stmts = list(stmts)
    b.kind = "ret"
    b.values = values
    return b


def check_operand_order():
    """`f(a(), K.g())` with the multret call already in place: `a()` does not move across the load
    of `K.g` (which then could not follow it and stayed a local in front of the statement)."""
    import codegen as CG
    from luasym import Const, Reg, Global, Index, TempVal, TempTail, Multi

    def call(fn, *args, tail=None):
        return CG.CallE(fn, Multi(list(args), tail))

    b = _block([CG.AssignS([Reg(1)], Multi([Global("f")])),
                CG.AssignS([Reg(2)], Multi([Global("a")])),
                CG.TempDef("T1", call(Reg(2))),
                CG.AssignS([Reg(2)], Multi([TempVal("T1", 1)])),
                CG.AssignS([Reg(3)], Multi([Index(Global("K"), Const(b"g"))])),
                CG.TempDef("T2", call(Reg(3), Const(1))),
                CG.TempDef("T3", call(Reg(1), Reg(2), tail=TempTail("T2", 1)))],
               Multi([], TempTail("T3", 1)))
    blocks = {1: b}
    for _ in range(3):
        CG.simplify_block(b, set(), CG.count_temp_uses(blocks))
    assert b.stmts == [], [type(s).__name__ for s in b.stmts]
    outer = b.values.tail.call
    assert isinstance(outer.fn, Global) and outer.fn.name == "f", outer.fn
    first, inner = outer.args.items[0], outer.args.tail.call
    assert isinstance(first, CG.CallE) and first.fn.name == "a"
    assert isinstance(inner.fn, Index) and inner.fn.key.v == b"g", inner.fn      # not a register


def check_table_constructors():
    """Array items with calls fold into the constructor: the fixed items of a multret SETLIST,
    an empty table that moves down to its first array store; a table that one of its functions
    names keeps its functions outside."""
    import codegen as CG
    from luasym import Const, Reg, Global, Index, TempVal, TempTail, Multi, ClosureExpr

    def call(fn, *args, tail=None):
        return CG.CallE(fn, Multi(list(args), tail))

    def new():
        return CG.AssignS([Reg(1)], Multi([CG.NewTableE([])]))

    def store(key, value):
        return CG.AssignS([Index(Reg(1), Const(key))], Multi([value]))

    # {f(), g(1)}: Luraph stores the fixed item, then moves the results of the last call
    b = _block([new(),
                CG.TempDef("T1", call(Global("f"))),
                CG.AssignS([Reg(2)], Multi([TempVal("T1", 1)])),
                CG.AssignS([Reg(3)], Multi([Global("g")])),
                CG.TempDef("T2", call(Reg(3), Const(1))),
                store(1, Reg(2)),
                CG.SetListS(Reg(1), 2, Multi([], TempTail("T2", 1)))],
               Multi([Reg(1)]))
    CG.merge_setlist_items(b)
    assert isinstance(b.stmts[-1], CG.SetListS) and b.stmts[-1].start == 1 and len(b.stmts[-1].values.items) == 1
    blocks = {1: b}
    for _ in range(3):
        CG.simplify_block(b, set(), CG.count_temp_uses(blocks))
        CG.fold_tables(b)
    tbl = b.values.items[0] if not b.stmts else b.stmts[-1].values.items[0]
    assert isinstance(tbl, CG.NewTableE) and len(tbl.items) == 1 and isinstance(tbl.tail, CG.InlineTail), \
        [type(s).__name__ for s in b.stmts]
    assert isinstance(tbl.items[0][1], CG.CallE) and tbl.tail.call.fn.name == "g"
    assert not any(isinstance(s, CG.SetListS) for s in b.stmts)
    # {a, b} whose items stay registers (used again): the empty table moves down to the stores
    use = CG.CallS(call(Global("use"), Reg(2), Reg(3)))
    b = _block([new(), CG.AssignS([Reg(2)], Multi([call(Global("f"))])), CG.AssignS([Reg(3)], Multi([call(Global("f"))])),
                store(1, Reg(2)), store(2, Reg(3)), use], Multi([Reg(1)]))
    assert CG.fold_tables(b) is True
    kinds = [type(s.values.items[0]).__name__ if isinstance(s, CG.AssignS) else "use" for s in b.stmts]
    assert kinds == ["CallE", "CallE", "NewTableE", "use"], kinds
    assert len(b.stmts[2].values.items[0].items) == 2
    # ... but `local M = {}`, other statements, `M.name = v` stays as written
    b = _block([new(), CG.CallS(call(Global("f"))), store(b"name", Reg(2))], Multi([Reg(1)]))
    assert CG.fold_tables(b) is False and len(b.stmts) == 3
    # ... and not while a closure made earlier holds the register (it could read the table)
    b = _block([new(), CG.CallS(call(Global("f"))), store(1, Reg(2))], Multi([Reg(1)]))
    assert CG.fold_tables(b, frozenset([1])) is False
    # a function that names its table: no function is folded in, plain fields are
    plain, named = ClosureExpr(object(), []), ClosureExpr(object(), [Reg(1)])
    b = _block([new(), store(b"x", Const(1)), store(b"f", plain), store(b"g", named)], Multi([Reg(1)]))
    assert CG.fold_tables(b) is True
    assert len(b.stmts) == 3 and [k.v for k, _ in b.stmts[0].values.items[0].items] == [b"x"], len(b.stmts)
    b = _block([new(), store(b"x", Const(1)), store(b"f", plain)], Multi([Reg(1)]))
    assert CG.fold_tables(b) is True and len(b.stmts) == 1


def check_shifted_pack():
    """`f(a, g())` on a pack the front end stopped tracking: the handler's `P.n = 2 + P.n - 1;
    table.move(P, 1, P.n, 2, P); P[1] = a; f(table.unpack(P, 1, P.n))` becomes the call again."""
    import codegen as CG
    from luasym import Const, Reg, Global, Index, Bin, TempVal, TempTail, Multi

    def call(fn, *args, tail=None):
        return CG.CallE(fn, Multi(list(args), tail))

    def n(p):
        return Index(Reg(p), Const(b"n"))

    def graph(k=2, extra_read=False):
        pack = CG.AssignS([Reg(5)], Multi([call(Global("table.pack"), tail=TempTail("T1", 1))]))
        pack.pack = True
        st = [CG.TempDef("T1", call(Global("g"))), pack,
              CG.AssignS([Reg(6)], Multi([Global("f")])),
              CG.AssignS([n(5)], Multi([Bin("Sub", Bin("Add", Const(k), n(5)), Const(1))])),
              CG.TempDef("T2", call(Global("table.move"), Reg(5), Const(1), n(5), Const(k), Reg(5)))]
        if k == 2:
            st.append(CG.AssignS([Index(Reg(5), Const(1))], Multi([Reg(7)])))
        st += [CG.TempDef("T3", call(Global("table.unpack"), Reg(5), Const(1), n(5))),
               CG.TempDef("T4", call(Reg(6), tail=TempTail("T3", 1))),
               CG.AssignS([Reg(8)], Multi([TempVal("T4", 1)]))]
        if extra_read:
            st.append(CG.CallS(call(Global("use"), Reg(5))))
        return {1: _block(st, Multi([Reg(8)]))}

    for k in (1, 2):
        blocks = graph(k)
        assert CG.fold_shifted_packs(blocks) is True, k
        b = blocks[1]
        calls = [s for s in b.stmts if isinstance(s, CG.TempDef)]
        assert [s.t for s in calls] == ["T1", "T4"], [s.t for s in calls]
        args = calls[1].call.args
        assert isinstance(args.tail, TempTail) and args.tail.t == "T1", args.tail
        assert [type(x).__name__ for x in args.items] == (["Reg"] if k == 2 else []), args.items
        assert not any(isinstance(s, CG.AssignS) and isinstance(s.targets[0], Index) for s in b.stmts)
    # the pack table is read afterwards: it must keep what the handler did to it
    blocks = graph(2, extra_read=True)
    assert CG.fold_shifted_packs(blocks) is False and len(blocks[1].stmts) == 10


def check_declarations():
    """Locals that live across `do ... end` segments are declared in front only where a segment
    closes over them (no `local x` directly followed by `x = v`), in declaration order whatever
    the hash seed is; a local named like a global is numbered `v2`, not `v_2`."""
    import codegen as CG
    import variables as V
    from luasym import Const, Multi

    def local(names, value):
        st = CG.AssignS([CG.LocalName(n) for n in names], Multi([value]))
        st.is_local = True
        return st

    def use(*names):
        return CG.CallS(CG.CallE(CG.LocalName("print"), Multi([CG.LocalName(n) for n in names])))

    # closed groups of statements with a long-lived local between them: the long-lived ones are
    # hoisted so that the groups can close, but a lone `a = v` between two groups closes nothing
    stmts = []
    for i in range(40):
        stmts += [local(["b%d" % i], Const(i)), local(["c%d" % i], CG.LocalName("b%d" % i)), use("c%d" % i),
                  local(["a%d" % i], Const(i))]
    stmts.append(use(*["a%d" % i for i in range(40)]))
    out = V.wrap_segments(stmts, 30)

    def pairs(lst):
        n = 0
        for x, y in zip(lst, lst[1:]):
            if isinstance(x, CG.LocalS) and x.values is None and isinstance(y, CG.AssignS) \
                    and not getattr(y, "is_local", False) \
                    and any(isinstance(t, CG.LocalName) and t.name in x.names for t in y.targets):
                n += 1
        for x in lst:
            if isinstance(x, V.DoBlock):
                n += pairs(x.body)
        return n
    assert any(isinstance(x, V.DoBlock) for x in out), "no segment was closed"
    assert pairs(out) == 0, pairs(out)
    # every name is still declared exactly once
    seen = []

    def decls(lst):
        for x in lst:
            seen.extend(V.declared_here(x))
            if isinstance(x, V.DoBlock):
                decls(x.body)
    decls(out)
    assert sorted(seen) == sorted(["a%d" % i for i in range(40)] + ["b%d" % i for i in range(40)]
                                  + ["c%d" % i for i in range(40)]), len(seen)
    # the order of hoisted names of one statement does not depend on the hash seed
    prog = ("import sys; sys.path.insert(0, %r)\n"
            "import codegen as CG, variables as V\n"
            "from luasym import Const, Multi\n"
            "def local(names, v):\n"
            "    st = CG.AssignS([CG.LocalName(n) for n in names], Multi([v])); st.is_local = True; return st\n"
            "def use(*names):\n"
            "    return CG.CallS(CG.CallE(CG.LocalName('print'), Multi([CG.LocalName(n) for n in names])))\n"
            "names = ['alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta']\n"
            "stmts = [local(names, Const(1))]\n"
            "for i in range(12):\n"
            "    stmts += [local(['t%%d' %% i], Const(i)), use('t%%d' %% i)]\n"
            "stmts.append(use(*names))\n"
            "out = V.split_block(stmts, 8)\n"
            "print(','.join(n for x in (out or []) if isinstance(x, CG.LocalS) and x.values is None for n in x.names))\n"
            % DEOBF)
    got = set()
    for seed in ("1", "2", "3", "4"):
        r = subprocess.run([PY, "-B", "-c", prog], capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, PYTHONHASHSEED=seed))
        assert r.returncode == 0, r.stderr[-400:]
        got.add(r.stdout.strip())
    assert got == {"alpha,beta,gamma,delta,epsilon,zeta"}, got
    # names
    import names
    text = names.rename_text("local r1 = foo()\nlocal r2 = foo()\nlocal r3 = foo()\nprint(v, r1, r2, r3)\n")
    assert "v_2" not in text and "v_3" not in text and "print(v, v_, v2, v3)" in text, text


# --------------------------------------------------------------------------
# with the corpus (files picked by directory and property)

def _version_files(version):
    if not CORPUS:
        raise Skip("LURAPH_CORPUS is not set")
    d = os.path.join(CORPUS, version)
    if not os.path.isdir(d):
        raise Skip("no %s in the corpus" % version)
    files = [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.endswith(".lua")]
    return sorted(files, key=lambda p: (os.path.getsize(p), p))


def _is_container(path):
    with open(path, "rb") as f:
        head = f.read(4000)
    body = re.sub(rb"\A\s*--[^\n]*\n\s*", b"", head)
    return body.startswith(b"return(function()") and b"loadstring" in body


def check_corpus_small_script():
    """The smallest v15.0 file (a plain VM object): lifted whole, `result: complete`, the text parses."""
    path = next((p for p in _version_files("v15.0") if not _is_container(p)), None)
    if path is None:
        raise Skip("no plain v15.0 file")
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "out.lua")
        rc, so, se = engine([path, "-o", out, "--time-limit", str(600 * SLOW)], timeout=700)
        assert rc == 0 and so[-1] == "result: complete", (rc, so[-1:], se[-2:])
        assert any("0 unlifted blocks, 0 unstructured jumps" in ln for ln in se), se[-6:]
        with open(out, encoding="utf-8") as f:
            text = f.read()
        assert "dynamic trace" not in text[:300] and parses(out)


def check_corpus_stopped_loader():
    """The smallest v15.0 container: its run stops inside Luraph's start-up code. The engine must
    end (it used to walk for ever), say PARTIAL with the reason, and write the trace, not Luraph's
    start-up program as the script."""
    path = next((p for p in _version_files("v15.0") if _is_container(p)), None)
    if path is None:
        raise Skip("no v15.0 container")
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "out.lua")
        t = time.time()
        rc, so, se = engine([path, "-o", out, "--time-limit", str(600 * SLOW)], timeout=700)
        assert rc == 0 and so[-1].startswith("result: PARTIAL: "), (rc, so[-1:], se[-2:])
        assert time.time() - t < 400 * SLOW
        if "start-up code" in so[-1]:
            assert "behaviour trace" in so[-1], so[-1]
            with open(out, encoding="utf-8") as f:
                assert "dynamic trace" in f.read(300)
        assert any(ln.startswith("[*] run status: ") for ln in se), se[-6:]
        assert parses(out)


CHECKS = [
    ("syntax-warnings", check_no_syntax_warnings),
    ("pack-site", check_pack_site),
    ("not-reached-unknown", check_not_reached_unknown),
    ("nested-root-lack", check_nested_root_lack),
    ("start-up-only-lack", check_start_up_only_lack),
    ("chunk-params", check_chunk_params),
    ("run-once-forin", check_run_once_forin),
    ("operand-order", check_operand_order),
    ("table-constructors", check_table_constructors),
    ("shifted-pack", check_shifted_pack),
    ("declarations", check_declarations),
    ("instance-model", check_instance_model),
    ("path2d-rect", check_path2d_rect),
    ("path2d-stored", check_path2d_stored),
    ("path2d-query-time", check_path2d_query_time),
    ("result-line", check_result_line_trace),
    ("failure-line", check_failure_line),
    ("time-limit-lift", check_time_limit_lift),
    ("time-limit-run", check_time_limit_run),
    ("no-orphan", check_no_orphan),
    ("corpus-small-script", check_corpus_small_script),
    ("corpus-stopped-loader", check_corpus_stopped_loader),
]


def main(argv):
    only = [a for a in argv if not a.startswith("--")]
    bad = 0
    for name, fn in CHECKS:
        if only and not any(o in name for o in only):
            continue
        t = time.time()
        try:
            fn()
            res = "ok"
        except Skip as e:
            res = "skipped (%s)" % e
        except Exception as e:  # noqa: BLE001 - one line per check
            res = "FAILED: %s: %s" % (type(e).__name__, str(e)[:600])
            bad += 1
        print("%-24s %s  (%.0f s)" % (name, res, time.time() - t), flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
