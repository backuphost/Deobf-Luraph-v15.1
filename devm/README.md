# devm — Luraph devirtualizer

Turns a Luraph-protected script (as plain Lua, e.g. after a bytecode decompiler) back into readable
source by partially evaluating the VM interpreter over every instruction.

```
# one-time: build the Luau CLI at tag 0.739 with luau_cli.patch applied and point LUAU_BIN at it
# (newer Luau changed how pcall uses the C stack; protected scripts measure that)
export LUAU_BIN=/path/to/luau
python -m devm.run protected.lua -o recovered.lua
```

Pipeline (`run.py`):

1. **Lift the first program** the VM runs. For current builds that is Luraph's *bootstrap*:
   environment checks that derive an XOR key, decrypt the real bytecode and install "key functions".
2. **Run the lifted bootstrap natively** in `harness.lua` with a mocked Roblox runtime
   (`rbxmock.lua`: instances, Enum, PCG32 `Random`, UDim2/Vector2, `Path2D` geometry, `utf8`
   normalisation). The 8-byte bytecode key is recovered statistically, so failed environment
   checks do not matter. The VM state is dumped again when the real program starts.
3. **Lift, structure and tidy** the real program: control-flow unflattening, self-modifying code,
   dead-store elimination, and/or idioms, naming from initialisers.

## Status

One command, no options: `python -m devm.run protected.lua -o recovered.lua`.  The layout, the kind
of environment to emulate and the interpreter binary (`bin/luau.<arch>` or `LUAU_BIN`) are found
automatically, and the run ends with a report of what, if anything, is left unrecovered.

| Layout | Examples | State |
|---|---|---|
| proto-table (`wrap(proto, upvalues)`), decompiled bytecode | HookScript, ClientSynchronizer | complete, 0 lift errors |
| proto-table, Luraph's own one-line output, encrypted second stage | message_1 | complete, 0 lift errors, reproduced by `tests/regress.py` |
| factory (`factory(...)(upvalues)`), decompiled bytecode | Net, TeleportGuard | complete up to a few constants in branches no run reached |
| factory, one-line output, self-decrypting code and lazily decoded constants | 25ms_get | partial: all modules, but unlifted instructions and unstructured exits remain |
| v15.2: compressed container, four interpreters, per-closure keys, flattened and block-wise encrypted main function | examples/v15.2/sample.luau | runs to the key-server request of the loader it contains; everything that ran lifts and structures (about 3,200 lines); code that never ran keeps about 20 unlifted instructions; what the server's reply would unlock is not recoverable offline (see "Luraph v15.2") |
| v15.1 chunk cut out of its container, script mostly plain Lua kept in the VM object | examples/layer3.lua + examples/blob4.bin (`--chunk-arg`) | the script runs to its end; about 38,600 lines, 1 unlifted instruction in code that did not run |
| v15.1 of the v15.2 generation (no wrapper keys; the script on a second interpreter whose dispatch tests go through locals) | the `v15.1` directory of the public sample corpus (not in the repository) | small test scripts come out whole and `complete`; larger scripts lift every instruction that ran, see "The v15.x files of the public sample corpus" for what is open |
| v15.0: the closure maker is a method of the VM object, a state machine | the `v15.0` directory of that corpus | not modelled: refused at once with `no model: Luraph v15.0 method-wrap layout ...` (the second engine lifts these) |

There is no key that opens every Luraph v15.1 script.  Every protected file carries its own virtual
machine: opcode numbers, operand layout and handlers are generated per script, the decryption keys
are computed by the script from its environment while it runs, and in the factory layout code and
constants are only decrypted when they are first used.  What carries over from one script to the
next is this tool's understanding of a *layout*; a layout or option it has not met needs new support.

What no tool can recover from the file alone:

* code or key material the script fetches at run time (`loadstring5`, `loadstring6`: a number from a
  server goes into the key of the next stage);
* code behind a key system: it only decrypts after the server confirms a key, and this tool does not
  forge that confirmation;
* names, comments and formatting, which protection discards (`renamer.py` re-derives names from use).

`python tests/regress.py DIR` re-runs the pipeline on the protected inputs in `DIR` and compares
with the committed results (the inputs themselves are not in the repository).

### The report

`python -m devm.run` ends with a report, and writes the same lines as comments at the head of the
result, so that a file read later still says what it is: how the traced run ended (returned, or
the error it stopped with, after how many VM instructions), the requests the script made (nothing
is sent; a request gets an empty reply), and `result: complete` or `result: PARTIAL` with one
line per kind of thing that is missing:

| Line | Meaning |
|---|---|
| `the text does not parse: line N: <error>` | `main()` runs `bin/luau-ast` on the result; a text it rejects is never complete. The line number is the written file's, report head included (without the parser: `note: not checked that the text parses`) |
| `LIFT ERROR markers` | an instruction that could not be lifted; the code after it is missing (a `-- LIFT ERROR: N instruction(s) ...` head line is not counted here) |
| `N instruction(s) ... did not lift and have no LIFT ERROR marker` | the text's head line: lift errors in code the text does not contain (proto-table layout), or in code the traced run executed (factory layout; `up to N ... may have no marker` when errors with one message are marked only in part: markers name the error, not its place) |
| `the result may not be the script` | the `hint:` line: the proto taken for the script (the first of the second interpreter) is small and calls nothing but pure library functions while the first program did the work; `--root N` lifts the first program |
| `lines index or call nil` | `(nil)[k]`, `t[nil]`: usually a constant of code that never ran |
| `constants shown as __LPH_PROTOS[...]` | a value of the VM state the dump could not describe |
| `uses of LuraphVM` | the table that holds Luraph's interpreter; protected code keeps helper functions and values in its fields. The table itself is not in the result |
| `exit flags`, `state machines` | control flow that was not fully structured |
| `lines use UPVAL_n` | a captured variable of a function whose creation was not lifted |
| `values printed as nil --[[...]]` | a loop variable, argument or operand without a known source |
| `for loops without their loop statement`, `functions printed as a stub` | what the words say |
| `lines of Luraph's key arithmetic are left` | `v - v % 1` on a value cut to 32 bits: numbers the interpreter mixes into its keys, where they did not fold to a constant |
| `functions return a parameter xor-ed with a large literal` | Luraph's per-call keys (`return bit32.bxor(p, K), ...`) where they could not be folded because a caller is not in the result |
| `lines compute a constant into a local nothing reads` | Luraph's dead arithmetic (`local v = <literals, arithmetic, bit32 / string calls>`, v14 self-patch values) that `cleanup.drop_dead_constants` left because it does not evaluate here (removing a line that raises would change the script) |
| `the result is the VM's first program, which begins with Luraph's start-up code` | factory layout: the run made no program on a second interpreter, or the script's output and requests came from the first interpreter's programs, so the lift is the first program; the `start-up code:` line of the run says what the run shows about the script (its top level ran inside it but is not separated, or nothing of it showed) |
| `the script did not end` | factory layout: the traced run did not end (a key system that waits for its key, a GUI script in its event loop, Luraph's tamper response) and was stopped; the `[stalled]` line of the run says after how long and in what. What the script would have done later is not in the result (see "A script that does not end") |
| `the result is the VM's root program including Luraph's start-up code` | v14 single root program lifted from its first entry (`[single-root]`): what the start-up code decodes at run time is still encoded |
| `note: N instruction(s) that never ran did not lift` | factory layout: lift errors in never-executed code the result does not contain (cut as junk or unreachable); errors in code that ran without a marker give a `-- LIFT ERROR` head line instead, which lists only the messages of executed errors that may have no marker |
| `note: N calls of <name>, Luraph's tamper response` | not a defect: the tests in front of those calls are checks of the protection, not of the script (v15.2, see below) |

`run.report(text, notes=...)` gives the second part for any result file (it skips a head written
earlier, and reads the start-up, single-root and hint lines off it when no run notes are given;
the parse check is `run.parse_check`, which `main()` passes in).

### Factory layout: traced end-state lifting

These builds decrypt their own code while it runs (whole-array passes at start-up, instructions that
decrypt themselves or their successors with keys held in registers, one-shot constant decoders), so
a dump taken at the first interpreter entry is mostly ciphertext. `run.py` therefore:

1. runs the script to its end under the recording harness (`harness.lua oracle=1 dumpend=1`): per
   executed instruction it keeps the numbers in registers and interpreter locals, the instruction's
   versions and the successors actually taken;
2. lifts the VM state as it is at the end of that run. Instructions that ran are already plain, so
   their writes to VM state are dropped; writes of the others are applied and journalled. Where an
   instruction needs a run-time value (a decryption key, a key-mixed jump target) the recorded one
   is used;
3. cleans the control flow: state-machine unflattening, opaque predicates on key registers folded,
   tamper-response branches the run never took cut and labelled, boolean diamonds collapsed,
   table-state loops turned back into `for` loops, multi-entry cycles split.

Limits, stated plainly:

* Only what the traced run reached is known to be right. Branches that never ran are lifted
  statically and show `LIFT ERROR` where they need a key the run never produced (loadstring6: 40
  such instructions, 5 of them in code that ran).
* Cutting tamper branches and folding key tests relies on one run; a real `repeat ... until x == n`
  that never looped back would be cut the same way.
* A script whose later stage is decrypted with input the harness cannot supply stops at that
  point: the output is the loader, not what it loads. loadstring6 (a ZekeHub/Luarmor loader) mixes
  two outside inputs into its key: the reply of `game:HttpGet(".../luarmor/precheck")`
  (`--http-reply <value>`), and six results of a global `math_floor_precision` that neither Roblox
  nor executors have, so an earlier loader stage must define it. All other key bytes match the
  reference environment (`--executor`). Without that function the payload does not decrypt.
* Functions whose control flow still comes out as thousands of lines are replaced by an
  `UNSTRUCTURED` stub.

Later findings on the two Luarmor samples (see `deobfuscated/loadstring5.lua`, `loadstring6.lua`):

* loadstring6's script does decrypt and run: its strings come out right and it requests
  `.../luarmor/precheck`. It stops because the reply (a number mixed into the next key) is not
  available offline; a made-up `--http-reply` only yields garbage further on.
* loadstring5 (the Luarmor bootstrapper) keeps its script in a proto of the *bootstrap* interpreter,
  so the automatic payload choice is wrong: use `--root 38 --no-exercise`. `--no-exercise` matters
  when a script stops early: calling its functions again afterwards made it scramble its own code.
  The script needs the global `_bsdata0` from an earlier loader stage.
* A payload interpreter can read plain values (jump states, numbers) out of the list its closure was
  bound to; the harness records that list (`binds=1`, implied by `exercise=1`) and the lifter folds them.
* A function the structurer gives up on is printed as a state machine (one `if state == n` per
  basic block) instead of being dropped.

`antitamper.py` predates the inlined instrumentation and is not wired in.

### A script that does not end

The end-state dump is taken when the script ends, and many scripts never do: a key system waits
for its key, a GUI script sits in its event loop (`while true do task.wait() end`,
`RunService.Heartbeat:Wait()` in a loop), a loader that found no key calls Luraph's tamper
response.  Nothing really waits in the mock (a wait of the main thread returns at once, so does an
event's `:Wait()`), so such a script spins.  Before, the run went on until `start_server`'s limit
of 1800 s killed it, and a killed process has no dump: `run failed`.  Now the harness watches the
traced run (`harness.lua`, `watch`) and, when it stops it, ends the run as if the script had
returned: `[stalled]` line, exercise pass, oracle, end-state dump, serve mode for the lifter's
lazily decoded constants.

* **The script's start.**  The watch works on the script's own code.  That has started when a
  program is made on an interpreter other than the first one (the payload of two-interpreter
  builds), or at the script's first line of output or first request (builds whose script runs on
  the first interpreter, next to Luraph's start-up code).
* **Progress** is a VM instruction that runs for the first time (or in a new version of its code)
  or a program that is made.  VM code is watched through the instruction hook; code that is not VM
  code (the loader, and the script's functions Luraph keeps as plain Lua) through its `while` /
  `repeat` loops, which count their rounds in the instrumented copy (`__LPH_N.l`, no call) and call
  `__LPH_L` every 65,536 rounds (`Model.patched_source`).
* **When the run is stopped** (seconds are the harness's processor time; it never sleeps):
  - `--stall S` (default 90): after the start, S seconds without progress: the script loops over
    code it has run before;
  - `--stall-wait S` (default 600): S seconds of waiting the mock simulated without progress, from
    the start of the run (Luraph's start-up code does not wait in loops).  `task.wait(t)` and
    `wait(t)` count what they report, a frame event's `:Wait()` (`Heartbeat`, `RenderStepped`,
    `Stepped`, ...) one frame, and any other event's `:Wait()` 60 s, since nothing in the mock ever
    fires it: a loop of waits that can never be satisfied stops after ten rounds instead of
    spinning, a single one lets the script go on as before;
  - `--stall-run S` (default 600): S seconds after the start, progress or not;
  - **Luraph's tamper response**: a VM function that holds two or more code arrays of another
    program in its registers (its caller loads its own arrays and passes them on) is stopped as
    soon as they are there (checked at every instruction that runs for the first time: the
    function's first instructions copy its arguments into the registers).  It would scramble those
    arrays and then loop for ever rewriting bytes of the VM's bytecode buffer, which is what the
    lift of code that never ran and the lazily decoded constants are read from: stopping later
    would lift a damaged state.  The exercise pass does not enter it either.
  0 switches a limit off.  The defaults are `vmmodel.STALL`; `--beat S` makes the harness log what
  the run is doing every S seconds (`[beat]` lines in `serve/stderr.txt` of the work directory).
* **Before the start**, 4 x `--stall` seconds without progress, or the tamper response, end the
  run *without* a dump: it spins in Luraph's start-up code (an environment check failed) or in code
  that shows nothing, and a lift would be the start-up code, which is never written out as the
  script.  devm fails with `run failed: ... [stalled] the run did not end and showed nothing of the
  script ...` and the front end tries the next engine.
* **The report.**  `[stalled] the script did not end: it was stopped N s after it started (<what
  showed the start>), <in what: waiting in task.wait() / Heartbeat:Wait() ..., in a loop of its VM
  code, in a loop outside the VM's code, at the entry of Luraph's tamper response, still running>;
  code it would load or fetch later is missing`, and the result is PARTIAL.  `deob.py` /
  `allinone.py` keep that result (the second engine is not tried) and end with
  `result: PARTIAL: the script did not end: ... (devm)`.
* **Which program is the script.**  A run that does not end is often one that never reached the
  script's interpreter, so the choice of the root matters more than before.  The harness notes
  which program ran the last VM instruction before each line of the script's output and each
  request (`[effects]`), and for every program the program of the instruction before its first
  one, i.e. its caller (`caller` in the dump's proto record).
  - The script is taken from the interpreter its output came from (`pipeline.script_programs`).
    A build can make programs on other interpreters that are helpers (a hash routine) while the
    script runs on the first interpreter; the first of those helpers used to be lifted as "the
    script".
  - On the first interpreter, the programs the output came from (for a script that showed
    nothing: the program the run was stopped in, `[where]`) are followed through their callers to
    the first program (`pipeline.entered_program`).  When the program below it on that
    chain was entered by the start-up code at its end (the first program ran at most 32 new
    instructions after that program started) and the first program said nothing itself, that
    program is the script and is lifted on its own, without the start-up code; the report says so
    (`program N is the script: ...`).  Otherwise (the output came from the first program, or it
    went on with code of its own: the script's top level in Luraph's main function, as in the
    v15.2 example) the first program is lifted, with the `[startup]` note.
  (A program's creator is not the evidence: closures of these builds are made while Luraph's
  lazy-constant decoder, itself VM code, runs.)
* **Limits.**  A script whose loop keeps running code it has not run before (every round new) is
  only stopped by `--stall-run`; a plain-Lua `for` loop that never ends is not counted (only
  `while` and `repeat` are) and is stopped by the last-resort limit; the tamper response is
  recognised by what it is handed, so a build whose tamper response looks different is stopped
  by the time limits, after it has damaged the state.

Seen on a v15.2 key-system loader (the script runs on the first interpreter, in a program the
start-up code enters at its end; two helpers on other interpreters): it prints a start message,
reads a few executor globals and a key global, and, without a key, calls its own failure routine,
which warns that a key is required and then passes its own code arrays to Luraph's tamper response.
That function XOR-scrambles them and then rewrites bytes of the bytecode buffer in
`while true do ... end`: a busy loop of VM code, no wait, no environment access, no new
instruction.  Without the watch the run went on for 30 minutes and failed; with it the run stops
at the tamper response and the script's program is lifted.

## Environment fidelity (newer factory builds)

These builds answer a failed environment check by scrambling their own bytecode forever, and the
RC4 key of the real program is built from what the checks observe, so the mock has to match Roblox
exactly rather than approximately:

* every function and metamethod the script can reach is a C closure (`cwrap` in luau_cli.patch):
  one stack frame per call, `[C]` in `debug.info`, errors positioned like `luaL_error` does. The
  script calls whole batteries of builtins with a single frame left on its stack;
* `getfenv()` is a nearly empty table that falls through to the globals; the script enumerates it;
* the executed copy is one line (line numbers in its own error messages are compared);
* Path2D geometry is a float32 model in the engine's order of operations, ported from the
  Studio-verified model of the "deobf" Luraph deobfuscator; the float32 bit patterns are key bytes.
  `GetBoundingRect` is the box of the control polygon (every point and the tangent handles that
  shape a segment) widened by `Thickness`/2, in the parent's pixels: the plausible rule, shared
  with deobf and not measured in Roblox (the v15.1/v15.2 start-up code seen calls it without
  reading the Rect); a path with fewer than 2 points still gets a logged stand-in;
* executor-style globals (`identifyexecutor`, `iscclosure`, `islclosure`) exist, as in the
  reference environment the key material was compared against.

`harness.lua` options for diagnosing a failed check: `stub=<pid>` (suppress the tamper response),
`watch=<pid>` (log the arguments of that proto's last calls, e.g. the comparison helper), `tb=<pid>`,
`depth=<n>`, `beat=<s>` (a `[beat]` line every s seconds: instructions, distinct ones, programs,
loop rounds, waits, the last instructions).

### Two-interpreter builds: the payload

The bootstrap runs on one interpreter, the script on another whose dispatch loops sit inside
`pcall` (errors are re-raised with line information).  `normalize.inline_protected` rewrites that
interpreter so the loops are in the interpreter function itself; `run.py` then lifts from the first
proto of the payload interpreter instead of the bootstrap.  Code decrypts itself as it runs, so
after the script ends the harness calls every payload closure once with permissive stand-in
arguments (`exercise=1`).  `--script-path Service.Folder.Name` places the script where it expects
to be (`script:GetFullName()` is checked).

State of the two samples of this build:

* TeleportGuard: every check passes, the key is right, the payload runs.  Its payload interpreter
  has a single handler (vararg setup): the protected program is empty.  See
  `deobfuscated/TeleportGuard.lua`.
* Net: every check passes, the key is right, the payload runs to the `return` of its API table
  and lifts (`deobfuscated/Net.lua`).  After the exercise pass, probe rounds (`poke=`) start a
  copy of a proto at instructions the lifter reached but the run did not, so their constants
  decrypt.  `--server` runs the script as the server; the file combines a server run with the
  client run's first guard.  Left over: 6 constants of anti-exploit branches no run reached, 2
  lift errors, 3 loops without bounds.

The two proto-table samples recovered earlier (HookScript, ClientSynchronizer) were not re-run
against these changes: their inputs are not in the repository.

### Scripts protected from source (the minified original Luraph layout)

`message_1` is not a decompiler rendering but Luraph's own one-line output.  Differences handled:

* the interpreter's dispatch loop sits in `pcall(function() repeat ... until false end)`:
  `normalize.inline_protected_raw` rewrites it into a plain loop before the model is built;
* the payload patches its own instructions while it runs.  A jump is first aimed at a stub that
  rewrites operands (and the jump itself) and only afterwards at the real code, with the patch
  values kept as counters in operand slots.  The lifter follows constants through registers
  (`known_after`), applies such patches and lifts the patched instructions again;
* every function is guarded by opaque predicates over number variables that only ever hold large
  constants; the false arm scrambles those numbers and runs garbage.  `opaque.prune_ir` cuts the
  arms that lead straight into scrambling, the program is lifted a second time without entering
  them (their patches must not be applied), and `opaque.strip` removes what is left of the
  numbers from the text;
* the payload has no state-machine flattening, so `ircfg.UNFLATTEN` is off for it (the pass would
  split loops on the script's own boolean flags).

Result: 131 functions, no lift errors (`deobfuscated/message_1.lua`).

### Luraph v14 (one-line output, proto-table layout): the VM runs its own init

Every Luraph v14 file is the proto-table layout in one of three spellings: one table per instruction
(v14.0.2 - v14.2), parallel opcode/operand arrays (v14.3), and the same VM with its loader outlined
into the methods of one table (v14.4 - v14.9).  What differs per file is decoration.

* **Loop heads.**  `normalize.normalise_heads` rewrites every spelling of a dispatch-loop head
  (fetch in parentheses, `repeat ... until false`, a truthy literal for `true`, junk constant locals)
  to one of two canonical forms; the model, the evaluator and the container test know only those.
* **The first program is not the script.**  The loader runs a first program and wraps what it
  returns (`P = WRAP(P, U)(args); return WRAP(P, U)`): Luraph's own init (environment probes, lazy
  constants, in v14.8 the decryption of the script data).  `--route a` (the default) lets the VM run
  that init in the harness, on a copy that keeps `pcall(function() <loop> end)` around its loops,
  and dumps the state when exactly the proto the init returned is entered
  (`Model.first_program_call`, `__LPH_MARK`, `vmmodel.run_dump_script`).  Nothing of the script
  runs.  The result is a static lift of the script's program; the report says so.
* **A single root program** (v14.9): no first-program call.  The root program starts with the same
  start-up code, builds the script's program and calls it itself.  The dump is taken when a program
  is entered that did not exist when the root program started (`__LPH_ENTER`).  A run that is
  stopped before that falls back to the second-stage route and, when the lifted root program holds
  no encrypted second stage, ends `devm: init did not hand over`; a root program that returns
  without entering anything is taken for the script and lifted from its first entry, start-up code
  included: that result is always PARTIAL (`[single-root]`).
* **Key functions.**  From v14.8 on the script patches its own operands with the values of small
  functions the init installs.  At the hand-over they are closures of the VM; the harness calls
  each one twice under a budget and writes the value into the dump (`findConstFns`).
* **When the init does not hand over** (the mock answers one of its probes differently from Roblox
  and Luraph spins, or it waits), the run ends by an instruction budget and the tool ends with
  `devm: init did not hand over: ...`, naming the last mocked calls of a second run with call
  logging.  The init is never written out as the script.  `--route b` is the older route: lift the
  init, replay it natively, recover the XOR key (`bootstrap.py`); `auto` tries it only for VMs whose
  data is a buffer: after a failed hand-over, or when the lift from the hand-over state has errors
  and the dump holds key functions the harness could not evaluate.
* **Limits.**  The init may run `max(12 million, 60 per byte of VM text and container data)`
  instructions (`--budget`), the run `max(150 s, budget / 400,000 + 50 s per megabyte)`
  (`--run-timeout`): inits that decrypt their data run up to 32 instructions per byte, and the dump
  decodes every lazy constant of the script.
* **Lift errors without a marker.**  When instructions did not lift and the printed text has no
  marker for them (they sit in code the text does not contain), the text gets a head line
  `-- LIFT ERROR: N instruction(s) did not lift ...` and the result is PARTIAL.  The factory layout
  (v15) does the same for errors in code the traced run executed (`run.hidden_errors`: markers name
  the error, not its place, so per message the executed ones count first); errors only in
  never-executed code that was cut are a `note:` line of the report.
* **Dead arithmetic.**  v14 programs compute the values they patch into their own code with
  literals and pure library calls (`string.unpack`, `string.packsize`, `string.byte`, `bit32.*`,
  `math.modf`).  The lifter folds the patch; the register assignment stays without a reader.
  `cleanup.drop_dead_constants` drops `local vN = E` when vN is read nowhere and E (literals,
  arithmetic, comparisons, and/or/not, calls of `bit32.*`, `math.floor/ceil/abs/min/max/modf/round`,
  `string.unpack/packsize/byte/len/sub/char/rep` on constants) evaluates without an error; what is
  left of that shape is counted by the report.  "Without an error" follows Luau (checked with
  `bin/luau`): `string.unpack` only with two arguments, integer sizes 1..16 (unpack: 1..8), sizes
  and `string.packsize` totals up to 2^30; the zero-argument `bit32.band()` / `bor()` / `bxor()`
  are valid.  Uses are counted from tokens, the expressions inside backtick `{...}` included, and a
  line that a long comment or string runs over (or opens) is never dropped.
* **`;(` first in a block.**  A statement that starts with `(` is printed as `;(...)` (it would
  continue the one before it).  When a cleanup pass removes the statement before it, the `;` can end
  up first in its block, which Luau does not parse; the last step of `cleanup.tidy`
  (`drop_block_semicolons`) drops a `;` that follows `do`, `then`, `else`, `repeat`, the `)` of a
  function's parameter list, another `;` or nothing, and keeps every other one.
* **Failures are one line.**  A run that writes no result ends with `devm: <stage>: <reason>` and a
  non-zero status: `unsupported target` (a build for Lua 5.3/5.4 or LuaJIT: `normalize.foreign_dialect`
  looks before the parser is tried), `damaged input` (text that does not parse, a container that
  does not open), `no model`, `init did not hand over`, `run failed`, `lift failed`.  A failed
  harness run's reason is the meaningful end of its output (`vmmodel.harness_tail`: the `[oracle]`,
  `[budget]`, `[timeout]`, `[error]` lines and the last error, without the per-access `[global]` /
  `[call]` trace lines), printed up to 1000 characters.  A traced run that does not end is
  normally stopped by the harness itself and lifted (see "A script that does not end"); one that
  showed nothing of the script fails with its `[stalled]` line, and one that still did not end at
  the last-resort limit of 1800 s says so in a `[timeout]` line.
* **Containers.**  With `devm/container_static.py` in the tree the container is read without
  running it; otherwise by the sandboxed probe.  Either way the run executes the shipped file.

### Luraph v15.2 and the compressed container

`examples/v15.2/sample.luau` is the reference (the only v15.2 sample so far).

* **Container.**  The shipped file is a small VM object that decodes and decompresses two blobs
  (the real VM chunk as Lua source, and its data buffer), compiles the chunk with
  `loadstring(source, "Luraph")` and calls it with the buffer.  `container.py` recognises that by
  running the file with a recording `loadstring` (`unwrap.lua`: pure libraries only) and builds
  the model from the chunk.  The traced run executes the *shipped file* and the harness's
  `loadstring` compiles the instrumented chunk in place of the original text (`carrier=`,
  `chunksrc=`), so call depth, chunk name (`[string "Luraph"]`, line 1), the chunk's arguments and
  the code in front of the header are those of the real thing.  The chunk and its buffer are kept
  in the work directory (`<name>.chunk1.lua`, `<name>.chunk1.arg1.bin`).
* **A chunk cut out earlier** (`examples/layer3.lua` + `examples/blob4.bin`):
  `python -m devm.run chunk.lua --chunk-arg buffer.bin` runs it through a stand-in carrier with
  the same call shape.  Without its buffer such a chunk stops at its first read.  This pair is
  a v15.1 build whose script is mostly plain Lua kept inside the VM object: the functions the
  payload creates from it (`native(nil, nil, upvalues, nil)`, a wrapper with four parameters)
  are printed with their own text, also the one kept in a wrapper without parameters (4,650
  lines of GUI library that used to be a stub).  Result: about 38,600 lines, 110 functions on
  the VM, 1 unlifted instruction in code that did not run.  The traced run goes to the script's
  `return` (2.4 million VM instructions); its three requests (a file on
  raw.githubusercontent.com, a POST to the script's own server) get no reply offline.  Three
  things this pair needed besides the container stand-in: `GuiService:GetGuiInset()` returns two
  values (`rbxmock.lua`; the script stopped on `nil.X` before); the wrapper of this build's VM
  closures is `function(m, m, m)`, so the upvalue list is the one table among the arguments
  (`pe.call`, 92 of 113 closures had no captured variables before: 330 `UPVAL_n` lines); and
  the for-loop state, see next point.
* **For loops.**  The interpreter that runs the script (both in this v15.1 build and in v15.2)
  keeps the state of `for` loops in interpreter locals and saves them on a stack when a loop
  starts: `tmp = stack; stack, a = {[9]=limit, [6]=tmp, ...}, operand[pc]`.  The stack goes
  through a scratch variable, which `PE._classify_state_vars` did not expect, so the loop
  instructions were lifted as ordinary code and every generic `for` came out as a `while` over
  `nil --[[forval]]` placeholders (123 of them in the layer3 result, 24 in the v15.2 sample,
  where `for k, v in <the VM table>` had lost what it iterates).  With the scratch form
  recognised they are `for` loops again.
* **Numeric `for` loops kept in a table.**  v15.2 writes most numeric loops without a loop
  instruction: a table holds `first - step`, limit and step (in shuffled slots, with a link to
  the enclosing loop's table), and every round does `i = t[a] + t[s]; t[a] = i` and tests
  `(step <= 0 and i >= limit) or (step > 0 and i <= limit)`.  `ircfg._recover_for_loops` turned
  the v15.1 form of this back into `for i = first, limit, step`; the forms v15.2 adds are
  recognised when `ircfg.V152` is set (run.py does that for this generation): the table is built
  in a scratch register and copied (`T = {...}; n = 0; S = T`), the set-up statements in between
  reuse the registers the bounds were read from (the bounds are then kept in registers of their
  own, where the table was built), a bound is a call (`for i = 1, f(x) do`), and a loop inside
  another loop is left by a jump (its set-up block is then reachable from its own body).
  `DEVM_DEBUG_FOR=1` prints why a table-state loop was left as it is.  Left over in the sample:
  13 loop tests of this kind (21 before): 11 in functions that never ran and end in an unlifted
  instruction (the loop's back edge or its table is missing there), 2 in a plain-Lua helper that
  Luraph keeps in the VM object, whose text is printed as it is.
* **Repeated key checks.**  `flag = key ~= 437829938; if flag then <tamper> end; x = "table";
  if flag then <tamper> end`: cutting the first test (`_cut_cold_magic`) leaves the second in a
  block of its own, where the comparison is not visible; its tamper side then runs into the
  state machine with a boolean in the state register (`if true <= 4 then`).  For v15.2 the cut
  is repeated until nothing changes.
* **The tamper response.**  One function of the start-up code loads the operand arrays of its own
  VM function into registers and passes them on (the call itself does not lift: the closure was
  never created in a run that passes the checks).  The second engine's notes (`deobf/V15_2.md`,
  observed there in runs that failed a check) say what it does with them: it scrambles them and
  never returns.  devm recognises the function by those loads (`structure.loads_own_code`),
  labels it in the output, and the report counts its calls (93 in the sample's result: every
  `if not check then ... end` in front of one is the protection's, not the script's).  Behind
  such a call Luraph leaves a jump to an unrelated place; followed as control flow these jumps
  put loops around straight-line code and jump into loop bodies.  With `ircfg.V152` a block ends
  at the call (`_cut_after_traps`; the output says so at each place: "the stray jump Luraph
  leaves behind the call is not followed").  This rests on the function not returning; if a
  build's tamper response did return, the code after these places would be missing from the
  result, at marked places only.
* **Model.**  The v15.2 chunk has four interpreters with 4 + 6 + 3 + 4 dispatch loops; a loop head
  can be `while true do OP = CODE[PC]` with the opcode variable declared outside
  (`vmmodel.is_dispatch_loop(assigned=True)`).  The wrapper between factory and interpreter takes
  per-closure numbers besides the upvalue list (`function(Z,Z,Z,J,i,U)`: keys Z and J, upvalue
  list `i`); `Model.wrapper_numbers` finds them, the lifter binds them from the traced run or from
  the creating instruction (`FactoryLifter.wrapper_values`).  Only the interpreter the sample
  actually runs (the last one) is exercised; the stack-machine interpreter and the two others
  have never been lifted from.  Their wrapper roles were checked against the text of each
  interpreter body (`tests/devm_v152.py`, `test_wrapper_roles`): interpreter 0
  `function(J,i,U,S)`: keys J and i, upvalue list U (`U[F[pc]]`), S allocates the register
  array; interpreter 1 (stack machine) `function(w,e,g,Q)`: w, g, Q numbers, upvalue list e;
  interpreter 2 `function(w,e,g,g)`: key e, upvalue list w, the last g allocates.
* **Executor API.**  In executor mode the script also gets `getrawmetatable`, `setrawmetatable`,
  `setreadonly`, `isreadonly`, `make_writeable`, `make_readonly` and a `clonefunction` that returns
  a new function; `request` behaves like native code (raw field reads, no metamethods, non-string
  `Url` refused).  The first group needs natives in the CLI (`__LPH_NATIVE`, added to
  `luau_cli.patch`): a binary built before that falls back to approximations and says so in an
  `[env]` line of the run.  The mock's own bookkeeping fields are no longer visible as globals.
* **The main function** is Luraph's start-up code and the script's top level in one function,
  protected three times over:
  1. every jump goes through a trampoline (`R219 = target; goto hub`): the lifter keeps one copy
     of the hub per target, as before;
  2. a state machine: one register holds the state, a binary search over it sits at one address
     and every block jumps back there.  `ircfg._unflatten` undoes this (`run.py` switches it on
     for this generation; it stays off for v15.1 one-line builds);
  3. block-wise encryption: a block of instructions is decrypted when it is first reached, with
     keys held in interpreter locals that the program itself keeps changing (`key = R[x]`
     instructions mix the results of the environment checks in), and constants decode lazily and
     patch their own instruction.  The traced run records 289 versions of this function's code.

  The lifter walks paths and gives an instruction the code and the keys of the first path that
  reaches it.  Here that path is often one the program never takes (garbage produced by lifting a
  block that is still ciphertext, or a state block entered with another block's history), and
  instructions that had run came out as garbage: enough to hide the state register from the
  unflattener.  For this generation (`Model.keyed_wrappers`, `Lifter.defer_foreign_state`) an
  instruction that ran is only lifted where the replayed code equals a version the run recorded
  for it, otherwise in the recorded state of its first execution, and with the keys the run
  recorded on entry.  The 39 MB / 78,000 lines the first v15.2 support produced became about
  3,300 lines (the same sample, the same run).
* **Closures** get their captured variables from the list the instruction builds; that list is
  parked in the instruction's argument template only for the duration of the call (`PE.parked`).
* **More than 200 locals.**  The unflattened function has about 500.  `structure.fit_scopes`
  cuts it into `do ... end` sections side by side, only where the limit asks for one
  (`tighten_scopes`, used for v15.1, nests one block per short-lived local).
* **Rewrites that are on for v15.2 only** (`run.py` sets `beautify.MERGE_ARMS` and
  `structure.NEST_ITEMS` when the model has keyed wrappers; the committed v15.1 results were
  written without them and `tests/regress.py` compares text for text):
  `if c then x = c else x = E end` becomes `x = c or E` (and the `and` twin), register set-up
  that both arms of an `if` repeat moves behind the `if`, and a function or table inside a
  multi-line table constructor is indented with it.  Together with the inliner that was already
  there, `local v = getgenv().SCRIPT_KEY or SCRIPT_KEY` is one line instead of nine.
* **A constant test is kept where removing it would change the meaning.**  A function that
  captured the constant `false` starts with `if not false then return end`; folding that to a
  bare `return` in front of the rest of the body would make Luau read `return` and the next
  call as one statement (it parses).  `opaque._fold_const_ifs` now leaves such a test alone.
* **What is not recovered, and why.**  All remaining `LIFT ERROR` markers are in code that did
  not run: there a key was computed from a value that only exists at run time, so the block or
  constant behind it cannot be decrypted.  Branches the run did not take can therefore end in a
  marker or, where a wrong key still produced valid operands, in nonsense statements; nothing in
  them is confirmed by the run.  `--mark-untaken` writes `-- not reached in the traced run`
  where such an arm starts (about 190 places in the sample; off by default because the comments
  keep small arms from folding into `and`/`or` expressions).
* **A blacklist of deobfuscator globals.**  One of the start-up checks, as lifted: it walks
  `getfenv()` and the tables behind its `__index` chain (three levels, about 50 keys each, names
  shorter than 20 characters), hashes every name (FNV-1a, xor a constant) and adds up weights
  from a table of 31 hashes; a sum of 220 or more is a failed check.  Hashing the identifiers of
  this repository against that table names nine of the 31: `HANDLERS`, `__ENT`, `__PA`, `__PF`,
  `__PID`, `__PK`, `__PLAST`, `__SKIPP`, `__SPIN`, the helper globals of the second engine in
  `deobf/`.  No identifier of devm (`__LPH_*` and the rest) hashes to any of the 31 entries;
  22 entries remain unnamed.  Instrumentation must stay out of the script's globals.
* **What the sample is.**  A JNKIE (jnkie.com) key-system loader.  After Luraph's start-up probes
  it tests the executor (`PC-E01..E03`: raw metatable access, `clonefunction`; a `request` call
  whose options carry metamethod traps) and then sends
  `POST https://api.jnkie.com/api/v1/luascripts/precheck/<id>/<id>`.  Without a reply it warns
  "Key verification unavailable" and stops.  The lifted code shows what a reply has to be: status
  200 and a body of at most 10 digits (`^[1-9]%d*$`, at most 2147483647); the script then stores that number and the three
  coordinates of a small `Attachment.WorldPosition` computation in a table.  The instructions
  after that never ran and do not decrypt with the keys of the traced run (the output ends there
  in a `LIFT ERROR`), so what the number is used for is not known.  devm does not forge the reply; everything behind it
  (the protected script) is not in the output.  `examples/v15.2/sample_src.luau` shares no
  strings or structure with what the loader does before that point.

### The v15.x files of the public sample corpus

Checked against the Luraph directories `v15.0`, `v15.1`, `v15.2` of a public sample corpus
(`tests/devm_v15_corpus.py`; the corpus is not part of the repository, `LURAPH_CORPUS` names it).

* **v15.1 builds are the v15.2 generation without wrapper keys.**  Luraph's start-up code (35
  functions, the same in every file) runs on one interpreter, the script on a second one.  That
  second interpreter writes its dispatch tree with every test computed into a local first,
  `while true do local OP = CODE[PC]; local t = OP >= 10; if t then local t = OP >= 15; if t then`.
  `vmmodel.is_dispatch_loop` accepts that form (`_outlined_test`); before, the model saw one
  interpreter, the harness never hooked the one that runs the script, and the result was a few
  lines of start-up code with a `LIFT ERROR`.  With it the usual two-interpreter route applies
  (`payload: proto 36 is the script`).
* **The open-upvalue map of that interpreter.**  Its handlers test and index the map through a
  local copy (`local a, r = open, operand[pc]; if a then local c = a[r] ...`), and the copy's name
  is usually also the name of some unrelated interpreter variable.  `pe._classify_state_vars` took
  the copy's name (and the names of dispatch-test locals, which look like the pattern) for the
  map: in one file the dispatch-mode variable was treated as the map and the lift stopped with
  `interpreter returned before reaching dispatch loop`; in others a key the interpreter keeps in
  a local was unknown to the lifter.  The classifier now follows the copy back to the
  interpreter-level variable.
* **The structuring passes of the generation** (`ircfg.UNFLATTEN`, `ircfg.V152`,
  `structure.FIT_SCOPES`, `NEST_ITEMS`, `NOTE_TRAPS`, `beautify.MERGE_ARMS`) are switched on by
  `run.v152_generation(model)`: wrapper keys, or an interpreter in the spelling above.  The older
  v15.1 builds (`examples/layer3.lua`, the committed results) have neither and keep their results.
* **Builds without a payload.**  Where the script's top level is part of Luraph's main function (as
  in the repository's v15.2 sample; a key-system loader of the corpus is built that way) all
  functions are on the first interpreter and the main function is what gets lifted.  For that, and
  for `--root 1` on any of these files, the lift of the main function had to be made whole:
  - the traced run's snapshots of tables that instructions change in place carry the table's
    identity (`tid`), and a proto's own operand array seen through its lazy view
    (`P = setmetatable({[0] = K}, decode)`) is not a shared table (`harness.lua` `codeVersion`,
    `lifter.shared_snapshot_tables`): other functions' snapshot bases used to overwrite the main
    function's code;
  - trampolines land a few retired instructions in front of the jump hub; the whole line is kept
    once per jump target (`lifter.hub_line`), also when it is reached through a jump that was
    already specialised or through the hub's copy for arrivals without a target (`NO_TARGET`);
  - an executed instruction is lifted in a code state the run recorded when the run recorded
    versions for its function (not only for keyed wrappers), with the interpreter-local numbers
    the run recorded for it (instructions that decrypt themselves with a key held in an
    interpreter local);
  - lazy constant decoders of these builds are state machines the evaluator runs itself: on
    ciphertext they are cut off after `lifter.LAZY_LOOP_LIMIT` rounds.
* **Luraph's per-call key** (v15.2 test scripts): a function takes one more parameter, every caller
  passes a literal, the function returns `bit32.bxor(key, C)` as an extra first result and the
  caller compares it with the number it must be.  `opaque._fold_call_keys` removes parameter,
  argument and result where every caller is in the result and decides the test; where a caller is
  missing the arithmetic stays and the report says so (`functions return a parameter xor-ed with a
  large literal`, never `complete`).
* **Key arithmetic on an interpreter local the lifter has no value for** shows as
  `local v = (73519286 + nil) % 4294967296; local w = bit32.bxor(v - v % 1, 398765431)` (numbers
  made up): in code that never ran (junk arms) the key was never set.  `opaque._drop_blind_key_arith` removes such
  a chain when nothing reads it, `opaque._fold_type_tests` decides the opaque test
  `type(0) == "buffer"` in front of a junk arm.
* **v15.0** is a third layout: the function that makes a VM closure is a method of the VM object,
  written as a state machine that assigns the interpreter closure in one of its states.  devm does
  not model it and says so at once (`no model: Luraph v15.0 method-wrap layout is not supported by
  this engine`, `run.method_wrap_layout`); the second engine (`deobf/`) lifts these files.
* **A VM chunk without its buffer** creates no VM function: `run failed: the VM never started:
  <how the run stopped> (a chunk cut out of a container needs its data: --chunk-arg)`.
* **The `--root` hint** ("the script is probably the first program") is only given when the
  function taken for the script calls nothing but pure library functions (a helper of the build);
  it used to be printed for every small script.  A result with the hint is PARTIAL (`the result may
  not be the script`).
* **No payload interpreter.**  When the run made no program on an interpreter other than the first
  (the run never reached the script's interpreter: a key check, a server reply, a mock difference;
  or a build such as the v15.2 example whose script's top level is flattened into the start-up
  code), the first program is lifted and the result is PARTIAL: the run gets a `[startup]` note
  (`pipeline.startup_note`) that says whether the run shows the script's effects (output, requests)
  from inside that program -- its top level is in the result but not separated -- or nothing of it.
  The same with `--root` set to the first program.  (The v14.9 rule of the proto-table layout, a
  program entered that was not reachable from the root's tables when it started, has no
  counterpart here: every factory program is a closure made at run time.)  Two exceptions, read
  off the run (see "A script that does not end", "Which program is the script"): when the
  script's output came from the first interpreter while programs on other interpreters exist,
  those are helpers and not the script; and when that output came from a program the start-up
  code entered at its end and never came back from, that program is lifted as the script.

### Names

`renamer.py` runs last in `cleanup.tidy` and can be used on its own
(`python -m devm.renamer in.lua -o out.lua`).  It re-derives names for generated identifiers from
how a value is made and used: initialisers (`:Clone()`, `tonumber(x.Field)`, getter calls, table
contents), function bodies (`getX`, `safeRequire`, `create`, the one remote or method a function
wraps), event handler parameters (`dt`, `character`, `input`), loop variables (`_, child`) and
counters (`elapsed`, `count`).  Generated names are unique per file, so this is a token
substitution; what no rule covers keeps its number.
