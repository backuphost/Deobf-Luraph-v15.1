# Luraph deobfuscator, all in one (v15.1 and v15.2)

One command for a Luraph-protected Roblox script:

```bash
./setup.sh                                  # once: builds the interpreter the engines run on (~3 min)
python3 deob.py protected.lua -o out.lua    # looks at the file, picks the engine, writes the result
```

`deob.py` unwraps Luraph's compressed container, decides what kind of script is inside, and hands it
to the engine that can recover it:

| What the file is | Engine | What you get |
|---|---|---|
| Luraph script whose code runs on the VM (v15.1, **v15.2**) | `devm` (devirtualizer) | the script as Luau: control flow, strings, calls, closures. Local names are re-derived from use (protection discards them). Ends with a report of what, if anything, is left unrecovered. |
| Luraph script that kept its code as plain Lua inside the VM object ("form A") | static extraction (`deob.py` itself) | those functions cut out, formatted and renamed. Every step is checked: formatting must keep the token stream, renaming must keep the syntax tree up to local names. |
| IronBrew1, or anything else | `deobf` (dynamic engine) | IronBrew1: devirtualized Luau. Unknown obfuscators: a behaviour trace on request (`--engine trace`). |

If `devm` gives up on a Luraph script, the second, independent Luraph lifter in `deobf/` is tried
before anything is reported as failed.

Nothing here contacts the network and nothing touches Roblox. Scripts are run only inside mock
environments (`devm/harness.lua` + `rbxmock.lua`, `deobf/envlog.luau`, `luasandbox.py`).

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
python3 deob.py INPUT --engine static       # only unwrap + static extraction (fast, never runs the VM)
python3 deob.py INPUT --engine devm         # force the devirtualizer
python3 deob.py INPUT --engine deobf        # force the dynamic engine (its Luraph v15 lifter, IronBrew1)
python3 deob.py INPUT --engine trace        # behaviour trace only (what the script did in the mock)
python3 deob.py INPUT --list                # what is inside (functions, VM dispatchers), no output
```

Other options: `--work DIR` keeps intermediate files (the unwrapped VM chunk and its data buffer,
the engines' dumps), `--timeout S`, `--engine-args "..."` passes options through to the engine
(for example `--engine-args "--http-map replies.txt"` for `devm`, see below), `--no-run` forbids
every step that executes the input (the container then stays closed and no engine is started).
`python3 deob.py -h` lists the options of the static path.

An input that is not recognised as the output of a known obfuscator is **not executed**; the tool
says so and names `--engine trace` for a behaviour trace.

Exit status:

| Code | Meaning |
|---|---|
| 0 | complete result written |
| 6 | result written, but **partial**: the report that was printed says what is missing |
| 4 | a VM-only script and no engine produced a result (or `--engine static` on such a script) |
| 3 | static result written, but it failed its own syntax check |
| 2 | nothing recognised (not a Luraph product, no extractable function); nothing was run |
| 1 | error (file or interpreter missing, engine crashed without a result) |
| 5 | the interpreter binary in `bin/` is for another CPU: run `./setup.sh` |
| 7 | "form C" capture harness written (old static path, see `docs/static-tool.zh-CN.md`) |

Each engine can also be run on its own: `python3 -m devm.run`, `python3 deobf/deob.py`.

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
- **Names, comments, formatting** do not survive protection. Names are re-derived from use.
- **A behaviour trace is not source.** It shows the branches one run took in a mock environment.
- The v15.1 results in `deobfuscated/` could not be re-run here (no inputs), so changes to shared
  code paths were kept behind checks on the layout; `tests/regress.py` is the way to confirm on
  your own inputs.

## Tests

```bash
python3 tests/run_all.py            # quick checks (a few minutes)
python3 tests/run_all.py --full     # also the slow ones: full devirtualization of the v15.2 sample
```

## Where the code came from

- The static tool and `devm/` are this repository's own history.
- `deobf/`, its notes and `examples/v15.2/` were imported from
  [pro628713-arch/luraph-v15.2-deobf](https://github.com/pro628713-arch/luraph-v15.2-deobf)
  (commit `ce58767`) and extended here. That repository carries no licence file; check with its
  owner before redistributing that part.
- Luau is © Roblox Corporation (MIT); it is downloaded and built by `build_luau.py`, not vendored.

Licence of this repository: MIT (`LICENSE`).
