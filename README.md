# Luraph deobfuscator, all in one (Luraph v14.x and v15.x)

One command for a Luraph-protected Roblox script:

```bash
./setup.sh                                  # once: builds the interpreter the engines run on (~3 min)
python3 deob.py protected.lua -o out.lua    # looks at the file, picks the engine, writes the result
```

`deob.py` recognises the file (header, owner-added lines around it, the shape of its main `return`
statement), opens Luraph's compressed container without running it (`devm/container_static.py`:
LZMA and Zstandard payloads, v14 and v15 containers), decides what kind of script is inside, and
hands it to the engine that can recover it:

| What the file is | Engine | What you get |
|---|---|---|
| Luraph **v14.0 - v14.9** (the whole script runs on Luraph's VM) | `devm` (devirtualizer) | the VM runs Luraph's own start-up program in the mock environment until it hands the script's program over; that program is lifted to Luau. The script itself is not run. |
| Luraph **v15.0** | `deobf` first (dynamic engine), then `devm` | the script as Luau, lifted from a traced run |
| Luraph script whose code runs on the VM (v15.1, **v15.2**) | `devm` (devirtualizer) | the script as Luau: control flow, strings, calls, closures. Local names are re-derived from use (protection discards them). Ends with a report of what, if anything, is left unrecovered. |
| Luraph script that kept some of its code as plain Lua inside the VM object ("form A") | static extraction (`deob.py` itself), then `devm` | the static path cuts those functions out, formats and renames them (every step is checked: formatting must keep the token stream, renaming must keep the syntax tree up to local names). The script's main flow still runs on the VM, so the file then goes to the engines like a VM script; the static extraction is the result only when no engine recovers the script, and it is reported **partial**. |
| IronBrew1, or anything else | `deobf` (dynamic engine) | IronBrew1: devirtualized Luau. Unknown obfuscators: a behaviour trace on request (`--engine trace`). |

If `devm` gives up on a Luraph script, the second, independent Luraph lifter in `deobf/` is tried
before anything is reported as failed.

Nothing here contacts the network and nothing touches Roblox. Scripts are run only inside mock
environments (`devm/harness.lua` + `rbxmock.lua`, `deobf/envlog.luau`, `luasandbox.py`). In
`devm`'s mock the script never gets the interpreter's own file, log and module functions:
`readfile` / `writefile` are the mock's empty, read-only stand-ins in executor mode and absent
otherwise, as in Roblox, and `require` is a Roblox-like stand-in. Text the script hands over (what
it prints or warns, a URL, a member name, an error message) appears in the run's log only inside
a line the harness or the mock started (`[script] ...`, `[http] ...`), with its line breaks
neutralised. The harness and the mock call their own copies of the standard libraries, so a script
that unfreezes and rewrites the shared `string` or `table` library with an executor's
`setreadonly` (allowed, as on a real executor) changes neither what they write nor what they
compute. One route is still open: devm's instrumentation hooks (the `__LPH_*` globals that the
instrumented copy of the script calls) are reachable by name from script code. A script written
against devm could call them and make the harness write its own fixed-format lines (`[handover]`,
`[root]`, `[budget]`, `[fixkey]`, `[bootstrap]`) or change the hooks' counters, which the tool
would read as the run's own events.

## Setup

Python 3.8+ (no third-party packages), git, cmake and a C++ compiler.

```bash
./setup.sh                      # installs a toolchain if one is missing, then runs build_luau.py
python3 build_luau.py --check   # tells you whether the installed interpreter is usable
```

The engines need **Luau 0.739 with two patches**, not a stock or newer Luau: protected scripts
measure their interpreter (C stack use of `pcall`, how a C function looks in `debug.info`, error
positions, the vector type's metatable) and derive decryption keys from what they see.
`build_luau.py` clones Luau, applies `devm/luau_cli.patch` and the writable-vector-metatable patch,
builds `luau` and `luau-ast`, and installs them into `bin/` and `deobf/bin/`. The binaries are not
in the repository. `LUAU_BIN` / `LUAU_AST_BIN` point the tools at binaries kept elsewhere.

## Usage

```bash
python3 deob.py INPUT [-o OUT]              # automatic
python3 deob.py INPUT --engine static       # only unwrap + static extraction (fast, never starts an engine)
python3 deob.py INPUT --engine devm         # force the devirtualizer
python3 deob.py INPUT --engine deobf        # force the dynamic engine (its Luraph v15 lifter, IronBrew1)
python3 deob.py INPUT --engine trace        # behaviour trace only (what the script did in the mock)
python3 deob.py INPUT --list                # what is inside (functions, VM dispatchers), no output
python3 deob.py INPUT --classify            # one JSON line: version, container, VM layout, verdict; runs nothing
python3 deob.py INPUT --verify              # also compare what the result does with what the protected file does
```

Other options: `--work DIR` keeps intermediate files (the unwrapped VM chunk and its data buffer,
the engines' dumps), `--timeout S` (per engine, default 3600; on timeout, Ctrl-C or SIGTERM the
engine and everything it started is stopped), `--engine-args "..."` passes options through to the engine
(for example `--engine-args "--http-map replies.txt"` for `devm`, see below), `--no-run` forbids
every step that executes the input: a container is opened only by the static decoder (never in
the sandbox), the old dynamic paths of the static tool (bytecode capture, the "form C" capture
harness) are skipped, no engine is started, and `--engine devm|deobf|trace --no-run` is refused
(exit 1), since every engine runs the input in its mock environment.
`python3 deob.py -h` lists the options of the static path.

An input that is not recognised as the output of a known obfuscator is **not executed**; the tool
says so and names `--engine trace` for a behaviour trace.

Exit status:

| Code | Meaning |
|---|---|
| 0 | complete result written: the engine reports nothing missing (see "Limits" for what that does and does not mean) |
| 6 | result written, but **partial**: the last line (`result: PARTIAL: ...`) and the report before it say what is missing (unlifted code, a run that stopped early, only a behaviour trace, a result that does not parse, or only the functions a form-A script kept as plain Lua while its main flow runs on the VM: with `--engine static` / `--no-run`, or when no engine recovered the script; also every `--force` export of a VM script: its plain functions, or else a skeleton of the VM, `result: PARTIAL: a VM skeleton, not the script's code (static)`) |
| 4 | a VM-only script and no engine produced a result (or `--engine static` / `--no-run` on such a script); the last lines are the engines' own reasons |
| 8 | Luraph, but it cannot be processed as given: cut-off or edited text, a payload that does not decode, a build for Lua 5.3/5.4 or LuaJIT, a VM chunk without its data; the last line says what and where |
| 3 | static result written, but it failed its own syntax check |
| 2 | not Luraph (another obfuscator behind a Luraph header, an imitation, an empty file), or nothing recognised; the reason is the last line; nothing was run |
| 1 | error (file or interpreter missing, engine crashed without a result), or `--engine devm/deobf/trace` together with `--no-run` |
| 5 | the interpreter binary in `bin/` is for another CPU: run `./setup.sh` |
| 7 | "form C" capture harness written and run (old static path, see `docs/static-tool.zh-CN.md`; never with `--no-run`) |

Each engine can also be run on its own: `python3 -m devm.run`, `python3 deobf/deob.py`.

## Luraph v14

All v14 builds are one VM family in three spellings (details: `devm/README.md`):

| Versions | Main statement | VM |
|---|---|---|
| v14.0.2 - v14.2 | `return(function(P1..Pn) ... end)(A1..An)(...)` | one table per instruction |
| v14.3 | same | parallel operand arrays |
| v14.4 - v14.9 | the loader is split into methods of one table | parallel operand arrays |

Most files are wrapped in a compressed container (base85 text, LZMA; v14.7 also Zstandard through
Roblox's `EncodingService:DecompressBuffer`), opened statically. The VM's first program is not the
script: it is Luraph's own start-up code (environment probes, lazily decoded constants), which ends
by handing the script's program to the VM. `devm` normalises every spelling of the dispatch loop,
lets the VM run that start-up code inside the mock environment, stops when the program it returned
is entered, and lifts that program. Luraph's start-up code is never written out as the script.

From v14.6 on the start-up code probes Roblox (a GUI tree whose `AbsolutePosition`/`AbsoluteSize`
must match numbers carried in the file, `Path2D`, enum items, instance members and their errors).
The mock answers these the way Roblox does (`devm/rbxmock.lua`, built from Roblox's API table in
`deobf/roblox_api.luau`); a wrong answer would make the VM scramble itself, so a file whose probes
are not passed ends with `devm: init did not hand over: ...` instead of a result.

v14.9 builds have a single root program (no hand-over); `devm` lifts what it can and reports it as
partial.

## Luraph v15.2

What v15.2 changed, and what was added for it (details: `devm/README.md`, section "Luraph v15.2";
`deobf/V15_2.md`):

- **Compressed container.** The shipped file is only a decoder: base85 + range decoder produce a
  Lua source (the VM, loaded with `loadstring(src, "Luraph")`) and a data buffer (the script's
  bytecode). Both tools unwrap it on their own. `devm` runs the *shipped* file and swaps the
  instrumented chunk in at `loadstring`, so call depth, chunk name and error positions are exactly
  the shipped file's.
- **Four interpreters in one chunk, three-level closure makers** with per-closure keys, dispatch
  loop heads without `local`.
- **A much larger start-up probe** (a battery of CFrame/Vector3/Vector2/UDim/Enum/Random/Path2D
  calls run at C-stack depth 199, coroutine and `task` scheduling states, `IsA`/`IsDescendantOf`,
  library fingerprints). Any wrong answer gives a wrong key and the script scrambles itself; both
  mock environments were corrected until every probe passes.
- **A main function flattened three ways**: jump-register trampolines, a state variable with a
  compare tree, and block-wise self-decryption with rolling keys held in interpreter locals.

The sample in `examples/v15.2/sample.luau` is a Luraph v15.2 build of a **key-system loader**
(JNKIE): it checks its environment and executor, then POSTs `SCRIPT_KEY` to the key server and
mixes the server's numeric answer into the key of the next stage. The tool recovers the loader
and stops at the request:

```
$ python3 deob.py examples/v15.2/sample.luau -o out.lua
...
traced run: the script stopped ... (984140 VM instructions)
  request: POST https://api.jnkie.com/api/v1/luascripts/precheck/... -> ... (no network)
result: PARTIAL
```

`examples/v15.2/sample_src.luau` (a fly script) is **not** what this file contains before the key
check; whatever the loader starts afterwards is encrypted with a key that needs the server's reply.

### Scripts behind a key system or a server

The tool never sends a request and never invents a server's confirmation. If you hold a valid key
and have the reply the server gave *you*, you can replay it: `devm` takes canned replies
(`--http-map FILE` with `<url substring><TAB><reply body>` lines, or `--http-reply VALUE`), e.g.

```bash
python3 deob.py protected.lua -o out.lua --engine-args "--http-map replies.txt"
```

Without the reply, the stage behind the check stays encrypted and is reported as missing.

## The engines

| Directory | Engine | Notes |
|---|---|---|
| repo root (`deob.py`, `libs/`, `fastsolve.py`, `luasandbox.py`, ...) | static tool: container unwrap, form detection, extraction of plain-Lua functions, formatter and renamer with self-checks | `docs/static-tool.zh-CN.md` (Chinese; the original README of this repository, with a list of what changed since) |
| `devm/` | devirtualizer: static model of the interpreter, instrumented run in a mock Roblox/executor, partial evaluation of the interpreter per instruction, structuring, cleanup, naming | `devm/README.md` |
| `deobf/` | dynamic engine: mock environment that records behaviour, plugins `luraph_v15`, `ironbrew1`, `generic`, a lifter back end shared by the plugins | `deobf/CLAUDE.md` (architecture and hard-won facts), `deobf/LURAPH.md`, `deobf/V15_2.md`, `deobf/IRONBREW1.md` |

`allinone.py` is the hand-over between them; `luaubin.py` is the one place that finds the
interpreter binaries.

Earlier results recovered with `devm` are in `deobfuscated/` (their protected inputs are not in
the repository; `tests/regress.py DIR` re-runs them if you have the inputs).

## Limits, stated plainly

- **Per-script VMs.** Every Luraph file carries its own interpreter: opcodes, operand layout and
  handlers are generated per script. What carries over is the tool's understanding of a *layout*;
  an option or layout it has not met needs new support, and the tool says so instead of guessing.
- **Only what a run reached is known to be right.** Code that never ran is lifted statically; where
  it needs a key the run never produced it is marked (`LIFT ERROR`, a count in the report).
- **Remote code and key systems**: see above.
- **Names, comments, formatting** do not survive protection and cannot be recovered by any tool.
  Names are re-derived from use (`starterGui`, `getService`), the rest are numbered (`v12`).
- **"complete" is the engine's own judgement**: no unlifted instruction, no missing constant, all
  control flow structured, nothing left that it knows to be Luraph's. It is not a proof that the
  result behaves like the protected file; `--verify` compares the two in the mock environment
  (`python3 -m devm.diffcheck ORIGINAL RESULT`).
- **Leftovers that are not the script**: Luraph inserts dead arithmetic between statements (of the
  shape `local v4 = bit32.bxor(17, 3) * 250 - 61`, never read). `devm` removes such a line when
  it evaluates without an error (`devm/cleanup.py`, `drop_dead_constants`); a line of that shape
  that is left (it does not evaluate here, so removing it could change what the script does) is
  counted in the report and makes the result **partial**.
- **Damaged files** (cut off, edited, recompressed by a third party) and builds for Lua 5.3/5.4 or
  LuaJIT are reported as such (exit 8), not processed.
- **A behaviour trace is not source.** It shows the branches one run took in a mock environment.
- The v15.1 results in `deobfuscated/` could not be re-run here (no inputs), so changes to shared
  code paths were kept behind checks on the layout; `tests/regress.py` is the way to confirm on
  your own inputs.

## Tests

```bash
python3 tests/run_all.py            # quick checks (a few minutes)
python3 tests/run_all.py --full     # also the slow ones: full devirtualization of the v15.2 sample
LURAPH_CORPUS=/path/to/obfuscator-samples/Luraph python3 tests/run_all.py   # also checks on a sample corpus
```

The checks need no samples. With `LURAPH_CORPUS` pointing at a directory that holds one directory
per Luraph version (as in the public `obfuscator-samples` collection), some checks also pick a few
files of it at run time; nothing of the corpus is stored in this repository. `tests/corpus.py DIR`
runs `deob.py` over a directory and tabulates the outcomes (`--classify` for recognition only).

## Where the code came from

- The static tool and `devm/` are this repository's own history.
- `deobf/`, its notes and `examples/v15.2/` were imported from
  [pro628713-arch/luraph-v15.2-deobf](https://github.com/pro628713-arch/luraph-v15.2-deobf)
  (commit `ce58767`) and extended here. That repository carries no licence file; check with its
  owner before redistributing that part.
- Luau is © Roblox Corporation (MIT); it is downloaded and built by `build_luau.py`, not vendored.

Licence of this repository: MIT (`LICENSE`).
