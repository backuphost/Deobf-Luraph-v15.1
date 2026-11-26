#!/usr/bin/env python3
"""The front end recognises its input and reports truthfully.

Two parts:

  * hand-written miniature inputs (always run): header forms, what owners put around the
    header, the JSON wrapper, lost line breaks, the main-statement shapes, the verdicts
    (not Luraph / damaged / unsupported target) with their exit codes, the judging of an
    engine's result;
  * the public sample corpus, when the environment variable LURAPH_CORPUS names its Luraph
    directory (one sub-directory per version, files named by hash): a few files per version
    are classified and the layout is checked against the version.  Skipped when the variable
    is absent.  No corpus file is named here: files are picked by directory and size.

Nothing of a sample is run except through the tool's own container sandbox.
"""
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "libs"))
import allinone         # noqa: E402
import deob             # noqa: E402
import lphfront         # noqa: E402

HAVE_LUAU = bool(deob.find_luau_runtime()) and bool(deob.find_luau_ast())
CORPUS = os.environ.get("LURAPH_CORPUS")

HEADER = "-- This file was protected using Luraph Obfuscator v14.3 [https://lura.ph/]\n\n"
PAYLOAD = "LPH!" + "5sBg0HmKlQ" * 12            # an "LPH" literal: the VM's bytecode
FN_ARGS = 'return(function(a,b,c)return function(...)return b end end)("%s",{},1)(...)' % PAYLOAD
TABLE = ('return({A=function(t)return function(...)return t.B end end,B=[==[%s]==],C=string.byte})'
         ':A()(...)' % PAYLOAD)
SETMT = ('return setmetatable({A=function(t)return function(...)return t.B end end,B=[=[%s]=]},{})'
         ':A()(...)' % PAYLOAD)
CONTAINER = ('return(function()local s,l=string.sub,loadstring;local p="%s";local ok,f=pcall(l,s(p,5),"Luraph");'
             'assert(ok and f,"Luraph decompression error: (does your environment support load/loadstring?)");'
             'return f end)()(...)' % PAYLOAD)


def classify(text, **kw):
    text, how = lphfront.recover(text)
    return lphfront.classify(text, recovered=how, **kw)


class Header(unittest.TestCase):
    def version(self, text):
        return deob.luraph_header(text)["version"], classify(text)

    def test_wordings(self):
        for line, want in (
                ("-- This file was protected using Luraph Obfuscator v15.2 [https://lura.ph/]", "15.2"),
                ("-- This file was generated using Luraph Obfuscator v14.0.2 [https://lura.ph/]", "14.0.2"),
                ("-- This file was generated using Luraph Obfuscator v14.1", "14.1"),
                ("-- This file was protected using Luraph Obfuscator v14.3 [https://lura.ph/] --owner", "14.3")):
            v, c = self.version(line + "\n\n" + FN_ARGS)
            self.assertEqual(v, want, line)
            self.assertEqual(c["version"], want, line)
            self.assertEqual(c["verdict"], "luraph", c)

    def test_prefix_kinds(self):
        cases = (
            ("\n\n" + HEADER + FN_ARGS, ["blank-before-header"]),
            ("-- credits: somebody\n" + HEADER + FN_ARGS, ["comment-before-header"]),
            (HEADER + "--// note\n" + FN_ARGS, ["comment-after-header"]),
            (HEADER + 'getgenv().key = "abc"\n' + FN_ARGS, ["code-after-header"]),
            (HEADER + '_VERSION = "Luau";\n' + FN_ARGS, ["code-after-header"]),
            ('getgenv().key = "abc";\n' + HEADER + FN_ARGS, ["code-before-header"]),
            ("-- This file is licensed with Luarmor. Use the loadstring.\n"
             "if not is_from_loader then warn('Use the loadstring, do not run this directly') return end;\n"
             + HEADER + FN_ARGS, ["luarmor-comment", "luarmor-guard"]),
            (FN_ARGS + "\n" + HEADER, ["header-at-end"]),
            (HEADER.replace(" [https://lura.ph/]", "") + FN_ARGS, ["header-no-url"]),
        )
        for text, want in cases:
            c = classify(text)
            self.assertEqual(c["prefix"], want, text[:120])
            self.assertEqual(c["verdict"], "luraph", (text[:120], c["reason"]))
            self.assertEqual(c["layout"], "fn-args", text[:120])
            self.assertEqual(c["version"], "14.3", text[:120])

    def test_guard_return_is_not_the_main_statement(self):
        text = ("if not is_from_loader then warn('x') return end;\n" + HEADER + TABLE)
        fx = lphfront.analyze(text)
        self.assertEqual(text[fx.main_return:fx.main_return + 9], "return({A")

    def test_json_wrapper_and_lost_line_breaks(self):
        c = classify(json.dumps({"luaCode": HEADER + FN_ARGS}))
        self.assertEqual((c["verdict"], c["layout"], c["prefix"]), ("luraph", "fn-args", ["json-wrapper"]))
        one = HEADER.strip() + " " + FN_ARGS        # the program sits behind the header's `--`
        self.assertEqual(lphfront.analyze(one).tokens, 0)
        c = classify(one)
        self.assertEqual((c["verdict"], c["layout"], c["prefix"]), ("luraph", "fn-args", ["oneline"]))

    def test_suffix(self):
        c = classify(HEADER + TABLE + "\n%")
        self.assertEqual(c["suffix"], ["trailing-code"])
        self.assertEqual(c["verdict"], "luraph")


class Shapes(unittest.TestCase):
    def test_layouts(self):
        for text, layout, wrapper in ((FN_ARGS, "fn-args", "none"), (TABLE, "table", "none"),
                                      (SETMT, "setmt", "none"), (CONTAINER, "unknown", "container:fn0-lzma-1")):
            c = classify(HEADER + text)
            self.assertEqual((c["verdict"], c["layout"], c["wrapper"]), ("luraph", layout, wrapper), c["reason"])

    def test_payload_size_does_not_make_a_container(self):
        big = TABLE.replace(PAYLOAD, "LPH!" + "x" * 50000)
        c = classify(HEADER + big)
        self.assertEqual((c["wrapper"], c["layout"]), ("none", "table"))

    def test_chunks_taken_out_of_a_container(self):
        chunk_t = "local B=...;return({A=function(t)return function(...)return B end end}):A()"
        chunk_s = "return setmetatable({A=function(t,b)return buffer.len(b) end},{}):A(...)"
        for text, layout in ((chunk_t, "table"), (chunk_s, "setmt"), (HEADER + chunk_s, "setmt")):
            c = classify(text)
            self.assertEqual((c["verdict"], c["layout"], c["bare_chunk"]), ("luraph", layout, True), c["reason"])

    def test_zstd_container_is_named(self):
        z = CONTAINER.replace('local ok,f', 'local e=game:GetService("EncodingService"):DecompressBuffer(p);local ok,f')
        self.assertEqual(classify(HEADER + z)["wrapper"], "container:fn0-zstd-1")


class Verdicts(unittest.TestCase):
    def test_not_luraph(self):
        imitation = "return({m=function(self,...)return function(...)print('hi')end end}):m()(...)"
        fake = HEADER.replace("lura.ph", "lura.phz") + imitation
        cases = (
            ("", "empty"),
            (HEADER + imitation, "imitation"),
            (fake, "lura.phz"),
            (HEADER + SETMT.replace(":A()(...)", ":zz()(...)"), "not a field"),
            (HEADER + "return(function()local F,x=string.char,{}end)", "stub"),
            (HEADER + "local v0=game.Players.LocalPlayer;print(v0)", "no main `return`"),
            (HEADER + "return v15(1,2)", "none of Luraph's shapes"),
        )
        for text, word in cases:
            c = classify(text)
            self.assertEqual(c["verdict"], "not-luraph", text[:100])
            self.assertIn(word, c["reason"], text[:100])

    def test_truncated(self):
        for cut in (len(HEADER) + 60, len(HEADER + TABLE) - 40, len(HEADER + TABLE) - 3):
            c = classify((HEADER + TABLE)[:cut])
            self.assertEqual(c["verdict"], "damaged", cut)
            self.assertIn("not closed", c["reason"])
            self.assertIn("line 3", c["reason"])

    def test_vm_without_its_entry_call(self):
        c = classify(HEADER + TABLE[:TABLE.rindex(":A()")])
        self.assertEqual(c["verdict"], "damaged")
        self.assertIn("call that starts it is missing", c["reason"])

    def test_other_lua_targets(self):
        lua53 = TABLE.replace("return t.B end", "return t.B & 255 >> 1 end")
        luajit = TABLE.replace("return t.B end", "goto done ::done:: return bit.band(t.B, 1) end")
        for text, word in ((lua53, "Lua 5.3/5.4"), (luajit, "LuaJIT")):
            c = classify(HEADER + text)
            self.assertEqual(c["verdict"], "unsupported-target", c)
            self.assertIn(word, c["reason"])
        # Lua 5.1-flavoured text that Luau reads is processed normally
        c = classify(HEADER + TABLE.replace("C=string.byte", "C=getfenv"))
        self.assertEqual((c["verdict"], c["platform"]), ("luraph", "lua5.1-syntax"))

    def test_lua51_escapes(self):
        src = 'local a = "\\x41 \\xzz \\u{41} \\upper \\d \\104"'
        fixed, n = lphfront.fix_lua51_escapes(src)
        self.assertEqual(n, 2)
        self.assertEqual(fixed, 'local a = "\\x41 xzz \\u{41} upper \\d \\104"')


@unittest.skipUnless(HAVE_LUAU, "needs bin/luau and bin/luau-ast (./setup.sh)")
class Tool(unittest.TestCase):
    """deob.py itself, on miniature files."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def write(self, name, text):
        p = os.path.join(self.tmp.name, name)
        with open(p, "wb") as f:
            f.write(text.encode("utf-8"))
        return p

    def run_tool(self, *args):
        r = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "deob.py")] + list(args), cwd=ROOT,
                           capture_output=True, text=True, errors="replace", timeout=600)
        return r.returncode, r.stdout + r.stderr

    def test_classify_mode(self):
        cases = (
            ("vm.lua", HEADER + TABLE, 0, "luraph", "table"),
            ("fake.lua", HEADER + "return({m=function(self)return function()end end}):m()(...)", 2, "not-luraph", "none"),
            ("cut.lua", (HEADER + FN_ARGS)[:-30], 8, "damaged", "fn-args"),
            ("l53.lua", HEADER + TABLE.replace("return t.B end", "return t.B >> 1 end"), 8, "unsupported-target", "table"),
            ("tampered.lua", HEADER + TABLE.replace("return t.B end", "return t.B end false;"), 8, "damaged", "table"),
            ("empty.lua", "", 2, "not-luraph", "none"),
        )
        for name, text, want_rc, verdict, layout in cases:
            rc, out = self.run_tool(self.write(name, text), "--classify")
            line = [l for l in out.splitlines() if l.startswith("{")]
            self.assertEqual(len(line), 1, out)
            row = json.loads(line[0])
            self.assertEqual((rc, row["verdict"], row["layout"]), (want_rc, verdict, layout), out)
            if verdict != "luraph":
                self.assertTrue(row["reason"], out)

    def test_repairs_are_announced(self):
        rc, out = self.run_tool(self.write("e51.lua", HEADER + TABLE.replace("C=string.byte", 'C="\\x\\uraph"')),
                                "--classify")
        row = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((rc, row["verdict"], row["validity"]), (0, "luraph", "ok-after-repair"), out)
        self.assertTrue(row["repairs"][0].startswith("lua5.1-escapes"), row)
        rc, out = self.run_tool(self.write("tail.lua", HEADER + TABLE + "\n%"), "--classify")
        row = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((rc, row["verdict"]), (0, "luraph"), out)
        self.assertTrue(row["repairs"][0].startswith("cut-trailing"), row)

    def test_verdict_exit_codes_of_a_run(self):
        out_path = os.path.join(self.tmp.name, "o.lua")
        for name, text, want, word in (
                ("r_fake.lua", HEADER + "return({m=function(self)return function()end end}):m()(...)", 2, "not Luraph"),
                ("r_cut.lua", (HEADER + TABLE)[:-30], 8, "damaged input"),
                ("r_l53.lua", HEADER + TABLE.replace("return t.B end", "return t.B | 1 end"), 8, "unsupported target")):
            rc, log = self.run_tool(self.write(name, text), "-o", out_path)
            self.assertEqual(rc, want, log[-800:])
            self.assertIn(word, log)
            self.assertNotIn("[devm]", log)             # no engine was started
            self.assertFalse(os.path.exists(out_path))

    def test_v14_vm_is_never_exported_as_the_script(self):
        # a table VM with a big plain function that reads globals: the static export (form A)
        # would have written it out as "the script"
        big = "function(t)" + "".join('game:GetService("Players");Instance.new("Frame");' for _ in range(900)) + "end"
        text = HEADER + TABLE.replace("C=string.byte", "C=" + big)
        out_path = os.path.join(self.tmp.name, "v14.lua")
        rc, log = self.run_tool(self.write("v14.lua.in", text), "-o", out_path, "--engine", "static")
        self.assertEqual(rc, 4, log[-800:])
        self.assertFalse(os.path.exists(out_path))
        self.assertIn("table VM", log)

    def test_container_is_never_exported_as_the_script(self):
        # a container whose payload is not a valid stream: damaged, exit 8, nothing written
        out_path = os.path.join(self.tmp.name, "c.lua")
        rc, log = self.run_tool(self.write("c_bad.lua", HEADER + CONTAINER), "-o", out_path, "--engine", "static")
        self.assertEqual(rc, 8, log[-800:])
        self.assertFalse(os.path.exists(out_path))
        # a container that does not open here (it asks Roblox to inflate its payload) and has a
        # big function reading globals: the static export must not take the container's own
        # code for the script
        big = "local function q()" + "".join('game:GetService("Players");Instance.new("Frame");' for _ in range(900)) + "end;"
        z = CONTAINER.replace("local p=", big + "local p=").replace(
            "local ok,f", 'local e=game:GetService("EncodingService"):DecompressBuffer(p);local ok,f')
        rc, log = self.run_tool(self.write("c_closed.lua", HEADER + z), "-o", out_path, "--engine", "static")
        self.assertIn(rc, (4, 8), log[-800:])           # 4: not opened here; 8: a static decoder calls it damaged
        self.assertFalse(os.path.exists(out_path))
        self.assertNotIn("[9]", log)

    def test_owner_statements_stay_guard_goes(self):
        text = ("-- This file is licensed with Luarmor.\n"
                "if not is_from_loader then warn('Use the loadstring') return end;\n"
                + HEADER + 'getgenv().key = "abc"\n' + FN_ARGS)
        sc = deob.Scan(self.write("guard.lua", text), say=lambda *a, **k: None, allow_run=False)
        try:
            sc.load()
            sc.binaries()
            sc.shells()
            f = sc.front_check()
            sent = sc.engine_text()
        finally:
            sc.cleanup()
        self.assertEqual(f["verdict"], "luraph")
        self.assertIn("luarmor-guard-left-out", sc.repairs)
        self.assertNotIn("is_from_loader then", sent)
        self.assertIn('getgenv().key = "abc"', sent)
        self.assertLess(sent.index("getgenv().key"), sent.index("return(function"))


class Formatter(unittest.TestCase):
    def test_hex_float_is_one_token(self):
        import deobf_plus
        for lit in ("0x1.c8P5", "0X1.f4P6", "0x.8p-1", "0x1p+4"):
            toks = [v for _k, v in deobf_plus.tokenize("x = %s + 1" % lit)]
            self.assertEqual(toks, ["x", "=", lit, "+", "1"], lit)
            self.assertEqual(deobf_plus.norm_num(lit), lit)         # no decimal spelling is made up
        # Luau text reads as before
        self.assertEqual([v for _k, v in deobf_plus.tokenize("a=0x10..s")], ["a", "=", "0x10", "..", "s"])
        self.assertEqual([v for _k, v in deobf_plus.tokenize("0xE+1")], ["0xE", "+", "1"])
        self.assertEqual([v for _k, v in deobf_plus.tokenize("t[0x1].x")], ["t", "[", "0x1", "]", ".", "x"])


class Judging(unittest.TestCase):
    """allinone: an engine's result is judged before it is reported."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._parses = allinone.parses
        allinone.parses = lambda path: True

    def tearDown(self):
        allinone.parses = self._parses
        self.tmp.cleanup()

    def result(self, text):
        p = os.path.join(self.tmp.name, "r.lua")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_deobf_results(self):
        code = "local a = 1\nprint(a)\n"
        cases = (
            (code, "[*]   3 functions, 0 unlifted blocks, 0 unstructured jumps, 0 constant requests\n", "complete"),
            (code, "[*]   3 functions, 21 unlifted blocks, 0 unstructured jumps, 0 constant requests\n", "partial"),
            (code, "[*] run status: aborted: endless loop in the script's own code\n", "partial"),
            (code, "[*] run status: finished\n", "complete"),
            (code, "result: PARTIAL: 4 unlifted blocks\n", "partial"),
            (code, "[*]   3 functions, 9 unlifted blocks, 0 unstructured jumps\nresult: complete\n", "complete"),
            (allinone.TRACE_HEADER + "\n" + code, "result: complete\n", "partial"),
            ("-- a\n-- b\n-- c\n", "[*]   0 functions, 0 unlifted blocks, 0 unstructured jumps\n", "nothing"),
            ("--[[ only\na comment ]]\n\n", "", "nothing"),
        )
        for text, log, want in cases:
            verdict, why = allinone.judge_deobf(self.result(text), log)
            self.assertEqual(verdict, want, (text[:30], log, why))
            if want == "partial":
                self.assertTrue(why)

    def test_unparsable_result_is_partial(self):
        allinone.parses = lambda path: False
        verdict, why = allinone.judge_deobf(self.result("local x = = 1\n"), "result: complete\n")
        self.assertEqual(verdict, "partial")
        self.assertIn("does not parse", " ".join(why))

    @unittest.skipUnless(HAVE_LUAU, "needs bin/luau-ast (./setup.sh)")
    def test_parse_check_reads_stderr_and_status(self):
        allinone.parses = self._parses
        self.assertIs(allinone.parses(self.result("local x = = 1\n")), False)
        self.assertIs(allinone.parses(self.result("local x = 1\n")), True)

    def test_reason_line(self):
        tb = ("traced run: 0 protos\nTraceback (most recent call last):\n  File \"x.py\", line 1, in <module>\n"
              "    main()\nIndexError: list index out of range\n")
        self.assertEqual(allinone.last_reason(tb, "devm"), "devm: IndexError: list index out of range")
        self.assertEqual(allinone.last_reason("a\ndevm: no model: no dispatch loop found\n", "devm"),
                         "devm: no model: no dispatch loop found")

    def test_engine_order(self):
        def scan(layout, version):
            return types.SimpleNamespace(layout=lambda: layout, header={"version": version})
        self.assertEqual(allinone.engine_order(scan("fn-args", "14.3")), ("devm",))
        self.assertEqual(allinone.engine_order(scan("table", "14.7")), ("devm",))
        self.assertEqual(allinone.engine_order(scan("setmt", "15.0")), ("deobf", "devm"))
        self.assertEqual(allinone.engine_order(scan("setmt", "15.2")), ("devm", "deobf"))
        self.assertEqual(allinone.engine_order(scan(None, None)), ("devm", "deobf"))


def _version_key(name):
    return [int(x) for x in name[1:].split(".") if x.isdigit()]


@unittest.skipUnless(CORPUS and os.path.isdir(CORPUS) and HAVE_LUAU,
                     "set LURAPH_CORPUS to the Luraph directory of the sample corpus")
class Corpus(unittest.TestCase):
    """A few files of every v14 / v15 directory: the verdict is one of the four, and a file
    that is Luraph has the layout its version writes (fn-args up to v14.3, table from v14.4
    to v14.9, setmetatable in v15)."""

    PER_DIR = int(os.environ.get("FRONTEND_CORPUS_PER_DIR", "3"))

    def test_layout_follows_the_version(self):
        seen = 0
        for d in sorted(os.listdir(CORPUS)):
            if not (d.startswith("v14") or d.startswith("v15")):
                continue
            folder = os.path.join(CORPUS, d)
            files = sorted((os.path.getsize(os.path.join(folder, f)), f) for f in os.listdir(folder))
            # the smallest, the median and one in between: cheap, and not always the same kind
            picks = {files[0][1], files[len(files) // 2][1], files[len(files) // 4][1]}
            key = _version_key(d)
            want = ("fn-args", "fn-args-vararg") if key < [14, 4] else (("table",) if key < [15] else ("setmt",))
            for name in sorted(picks)[:self.PER_DIR]:
                row = deob.classify_file(os.path.join(folder, name))
                seen += 1
                self.assertIn(row["verdict"], ("luraph", "not-luraph", "damaged", "unsupported-target"), (d, row))
                if row["verdict"] != "luraph":
                    self.assertTrue(row["reason"], (d, row))
                    continue
                self.assertEqual(row["version"], d[1:], (d, row))
                if row["layout"] != "unknown":      # a container this tree cannot open says so
                    self.assertIn(row["layout"], want, (d, row))
                else:
                    self.assertFalse(row["container"]["opened"], (d, row))
        self.assertGreater(seen, 20)


if __name__ == "__main__":
    unittest.main(verbosity=1)
