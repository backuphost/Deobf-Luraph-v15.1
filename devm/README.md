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
| `the result is program N on its own, which the VM's first program called at the end of its run` | factory layout, a run that did not end: the script's output (or the place the run was stopped) is in a program the first program entered at the end of its run, and that program was lifted without the first program's code before the call (Luraph's start-up code, and any part of the script's top level Luraph put there); `--root 1` lifts the first program (see "Which program is the script") |
| `the script did not end` | factory layout: the traced run did not end (a key system that waits for its key, a GUI script in its event loop, Luraph's tamper response) and was stopped; the `[stalled]` line of the run says after how long and in what. What the script would have done later is not in the result (see "A script that does not end") |
| `the result is the VM's root program including Luraph's start-up code` | v14 single root program lifted from its first entry (`[single-root]`): what the start-up code decodes at run time is still encoded |
| `note: N instruction(s) that never ran did not lift` | factory layout: lift errors in never-executed code the result does not contain (cut as junk or unreachable); errors in code that ran without a marker give a `-- LIFT ERROR` head line instead, which lists only the messages of executed errors that may have no marker |
| `note: N calls of <name>, Luraph's tamper response` | not a defect: the tests in front of those calls are checks of the protection, not of the script (v15.2, see below) |
| `note: N call(s) of program K, set aside as a helper of the build ...` | calls handed their caller's register array (or a helper's results the script keeps) whose values are shown after the calls, each with a comment, and how many of those values are proved (`--[[the run's value after the call]]`) and how many assumed; where a `values assumed: ...` line names the same calls, the values are the run's and only assumed (see "Values the run proves") |
| `variables handed to calls: N call(s) are handed the variables of the code that calls them ...` and `N call(s) are handed the variables ... so the result is not complete` | calls handed their caller's register array (printed `({})`): what they change is shown only as far as the run saw it, so the result is PARTIAL, also where every value they changed is shown and proved. The report also counts `({})` printed as a value in the text (outside strings and comments), so a text with such a call is PARTIAL also without the run note (the proto-table layout, which has no traced run) |
| `N value(s) in the text are the run's after a call, proved ...` | how many of the values shown after such calls are proved (each carries `--[[the run's value after the call]]`); the assumed ones are counted by the `values assumed` lines. After a helper's result alone (no register array handed) a proved value leaves the result complete, with this as a `note:` |
| `values not shown: ...` | such calls where the run does not prove what they wrote or may have written (the call did not run, it went on to more than one instruction or shares it with another such call, what followed it held different values at different times, or what it is handed is not constant or not shown to give the same at every execution): after them a variable the call may have set is printed `nil --[[set by a call handed the register array: value not shown]]`, or the call is kept, and the constant handed to the call stays encrypted |
| `values assumed: ...` | such calls whose values are shown although the check that they depend only on what the call is handed could not be completed: each such value carries a comment `--[[the run's value; assumed to depend only on the call's inputs: ...]]` that ends with what could not be checked (`the function called was not fully lifted`; `the function called uses tostring, next, pairs, table.foreach or string.format`; `the calling code was not fully lifted`; `another call handed the register array was not fully checked`), and the result is PARTIAL |

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

* **The script's start.**  The watch works on the script's own code.  That has started at the
  script's first line of output or first request (the `[started]` line says which, and how many
  waits of the main thread came before it).  A program the run makes on an interpreter other than
  the first one (the payload of two-interpreter builds) starts the limits after the start only
  provisionally: such a program can also be a helper of the build that Luraph's start-up code
  makes and calls.  When a limit is reached and the script showed no output and no request, the
  run counts as started only if it is in that program or under it, i.e. a program of another
  interpreter is on the VM call chain of the instruction it is in (`watch.under`, see "Which
  program is the script" below); otherwise the rules before the start apply, and a spin of the
  start-up code after it made a helper ends like any spin before the start, without a dump.
* **Progress** is a VM instruction that runs for the first time (or in a new version of its code)
  or a program that is made.  VM code is watched through the instruction hook; code that is not VM
  code (the loader, and the script's functions Luraph keeps as plain Lua) through its `while` /
  `repeat` loops, which count their rounds in the instrumented copy (`__LPH_N.l`, no call) and call
  `__LPH_L` every 65,536 rounds (`Model.patched_source`).
* **When the run is stopped** (seconds are the harness's processor time, utime + stime of
  `/proc/self/stat`: Luau's `os.clock` is a wall clock, which would let a loaded machine reach the
  limits after less work; where `/proc` cannot be read the wall clock is used and the `[watch]`
  line says so.  The harness never sleeps):
  - `--stall S` (default 90): after the start, S seconds without progress: the script loops over
    code it has run before;
  - `--stall-wait S` (default 600): S seconds of waiting the mock simulated without progress, from
    the start of the run (Luraph's start-up code does not wait).  `task.wait(t)` and `wait(t)`
    count what they report, a frame event's `:Wait()` (`Heartbeat`, `RenderStepped`, `Stepped`,
    ...) one frame, and any other event's `:Wait()` 60 s, since nothing in the mock ever fires it:
    a loop of waits that can never be satisfied stops after ten rounds instead of spinning, a
    single one lets the script go on as before;
  - `--stall-run S` (default 600): S seconds after the start, progress or not;
  - **Luraph's tamper response** (`--no-tamper-stop` switches this off; harness option
    `tamper=0`): a VM function that holds two or more code arrays of another program in its
    registers (its caller loads its own arrays and passes them on) is stopped as soon as they are
    there (checked at every instruction that runs for the first time: the function's first
    instructions copy its arguments into the registers).  It would scramble those arrays and then
    loop for ever rewriting bytes of the VM's bytecode buffer, which is what the lift of code that
    never ran and the lazily decoded constants are read from: stopping later would lift a damaged
    state.  The exercise pass does not enter it either.
  0 switches a limit off.  The defaults are `vmmodel.STALL`; through `deob.py` the options go in
  `--engine-args` (`--engine-args "--stall 300 --no-tamper-stop"`).  `--beat S` makes the harness
  log what the run is doing every S seconds (`[beat]` lines in `serve/stderr.txt` of the work
  directory).  Once the run is stopped, every VM instruction, plain-Lua loop round and wait of
  the script raises the stop again, so a script that catches it with `pcall` cannot keep the run
  going.
* **The defaults, measured.**  Every factory-layout file of the public sample corpus (the 13
  v15.1 and v15.2 files; v15.0 is refused and v14 builds take the proto-table route, which has no
  traced end-state run) was run with and without the watch.  No run was stopped, by a limit or at
  a tamper response: every run ended with the same `[oracle]` line and instruction count as
  before, at about the same processor time (within the few per cent the runs differ by anyway).
  The `[watch]` line of each run (`N s of processor time; the script started at N s (...); longest
  stretch without new code: N s before the start, N s after it`) gave, measured as wall-clock time
  on a loaded machine, so an upper bound: runs of 30 - 245 s, the script started 30 - 145 s into
  them and ran at most 114 s after that (the start then included the first program made on
  another interpreter, from which the limits after the start are still checked); the longest
  stretch without new code was 26 s before the start (`--stall` x 4 = 360) and 4 s after it
  (`--stall` 90); no run waited.  The trade-off: lower
  limits would end a script that does not end sooner (and a run killed at the last-resort limit
  has no dump at all), higher ones let a long run that ends on its own finish on a slower
  machine; at these values the slowest corpus run has five times the room it needs, and a
  `--stall` or `--stall-run` that is too low for a script shows in the result as a `[stalled]`
  line, never as a silent cut.
* **Before the start** (also after a program made on another interpreter, while the run is not in
  or under one) the run is stopped in two ways, and they end differently:
  - 4 x `--stall` seconds without progress (a busy loop, not a wait loop), or the tamper response:
    the run ends *without* a dump.  It spins in Luraph's start-up code (an environment check that
    failed loops for ever) or in code that shows nothing, and a lift would be the start-up code,
    which is never written out as the script.  devm fails with `run failed: ... [stalled] the run
    did not end and showed nothing of the script ...` and the front end tries the next engine.
    A loop the script's program started with the mock's `task.spawn` / `task.defer` /
    `task.delay` / `spawn` runs under that program (see `watch.under` below), so it is the
    script's start and is lifted like any stop after the start.  On a thread whose start is not
    known (one the script's coroutine library runs, or one a mock function calls back on) the
    run is not placed either way: it still ends without a dump, and the `[stalled]` line says
    that it was stopped on such a thread and that whether the spin is the start-up code's or the
    script's is not known (`... which of the two is not known, and nothing is lifted`).
  - `--stall-wait` seconds of waiting: the state is dumped and lifted.  Luraph's start-up code does
    not wait, so the waits are the script's own code on the first interpreter (its top level in
    Luraph's main function, or a program that function entered): a script that builds its window
    without a word and then sits in its update loop has all of that in the dump, and failing the
    run would lose it.  The `[stalled]` line says `before it showed itself otherwise`, the result
    is the first program with the `start-up code:` note (`the run was stopped while the script
    waited, ...`), and it is PARTIAL.
* **The report.**  `[stalled] the script did not end: it was stopped N s after <what showed the
  start: its first line of output, its first request, the run made program N on another
  interpreter>, <in what: waiting in task.wait() / Heartbeat:Wait() ..., in a loop of its VM
  code, in a loop outside the VM's code, at the entry of Luraph's tamper response, still running>;
  code it would load or fetch later is missing`, and the result is PARTIAL.  Where the start is a
  program made on another interpreter, the one named is the program the run was stopped in or
  under (the `[where]` line), which is also the evidence the script is then taken from: the line
  does not name a program that the report calls a helper.  `deob.py` / `allinone.py` keep that
  result (the second engine is not tried) and end with `result: PARTIAL: the script did not end:
  ... (devm)`.
* **Which program is the script.**  A run that does not end is often one that never reached the
  script's interpreter, so the choice of the root matters more than before.  The harness notes
  which program ran the last VM instruction before each line of the script's output and each
  request (`[effects]`), and for every program the program of the instruction before its first
  one, i.e. its caller (`caller` in the dump's proto record).  With the closures of the programs
  on other interpreters known (`binds=1` or `exercise=1`, which devm's traced run always passes)
  it also places each line of output and request, and the place a stopped run was stopped, on its
  VM call chain (`watch.under`): the innermost program of another interpreter on the Lua stack of
  the thread that ran the last VM instruction (a VM call is a call of the callee's closure, and
  Luau has no tail calls), or none (`[under] the VM call chains of the script's output and
  requests: under program N (count), ..., under no program of another interpreter (count)`;
  `[where] the run was stopped in program N, under program M of another interpreter` / `, on
  another interpreter` / `, under no program of another interpreter`).  A function that a mock
  function calls back runs on that function's own thread (`cwrap`), and a function `task.spawn`
  or a coroutine runs is on a thread of its own as well: such a chain starts at that function.
  For a thread the mock's task library starts (`task.spawn`, `task.defer`, `task.delay`,
  `spawn`), the mock tells the harness (`P.onSpawn`, `watch.spawned`), which notes where the code
  that started it ran at that moment: a stack of that thread without a program of another
  interpreter counts as that place (a script on another interpreter whose plain functions run a
  loop in a spawned thread is under its program there too).  For any other thread other than the
  one the VM started on (the script's coroutine library, a mock callback), such a stack says
  nothing about where the code was started from: those lines are counted as `not placed: on a
  thread whose call chain starts there (count)` and vote for no interpreter, and a run stopped
  there gets `, not placed: on a thread whose call chain starts there` on its `[where]` line.
  - The script is taken from the interpreter its output came from (`pipeline.script_programs`,
    `pipeline.script_votes`).  A build can make programs on other interpreters that are helpers
    (routines of the build) while the script runs on the first interpreter; the first of those
    helpers used to be lifted as "the script".  The `start-up code:` line then names them as
    helpers.  The script's own program can in turn call routines of the first interpreter, and the
    last VM instruction before its output is then in one of those (seen in v15.1 and v15.2 corpus
    builds whose script is a program on the second interpreter): the `[under]` line places that
    output under the script's program.  When the `[under]` line placed none of the lines (all on
    threads whose start is not known) or the `[where]` line says the stop was not placed, the call
    chains were known and do not say: like the harness's own start rule for that case, the first
    interpreter is kept (and the note says why); the first callers are not consulted.  Only
    without the call chains (a run without `binds=1` / `exercise=1`: no `[under]` line, a `[where]`
    line without its placement) is a program of the `[effects]` line (or the program the run was
    stopped in) counted for its own interpreter, and one of the first interpreter that only its
    first callers link to a program on another interpreter counts for neither: a caller is only
    the program of the instruction before a program's first one, which can be a helper that
    happened to call it first, and it says nothing about where its later output ran.  When that is
    all the evidence, the first interpreter is kept and the note says that the link alone did not
    decide.
  - The evidence is weighed: every line of output and request counts once, and an interpreter
    other than the first is taken only when more of it points there than at the first interpreter
    (the one with the most, if several).  One program among several does not decide, and a tie
    keeps the first program.  When not all of it points at the first interpreter, the `start-up
    code:` line gives the counts, and says where they come from (the call chains, or the programs
    themselves when the run did not record the chains).
  - A run that did not end and showed no output and no request is placed by the program it was
    stopped in, the same way: by its call chain where the `[where]` line gives it (a stop that is
    not placed keeps the first interpreter), else by that program's own interpreter.  Without any
    of this evidence (a run that ended without output) the payload stays the first program on
    another interpreter.
  - On the first interpreter, the programs the output came from (for a script that showed
    nothing: the program the run was stopped in, `[where]`) are followed through their callers to
    the first program (`pipeline.entered_candidate`): the program below it on that chain is the
    one the first program entered at the end of its run (it ran at most 32 new instructions after
    that program started, and said nothing itself).
  - **Only for a run that did not end** is that program lifted on its own
    (`pipeline.entered_program`): the first program never came back from it, and everything the
    first program did is ahead of the call -- Luraph's start-up code, which a lift of the first
    program would put in front of the script.  The `script program:` line says so, and the result
    is PARTIAL: the first program's code before the call is not in it, and where Luraph flattened
    part of the script's top level into that program (v15.2), that part is missing too; `--root 1`
    lifts the first program instead.
  - A run that ended is lifted from the first program, with the `[startup]` note, which names the
    entered program and `--root N` for it.  Lifting that program alone would drop whatever the
    first program did before the call, and in a v15.2 build that is the script's top level when
    its last statement calls the script's main function.
  (A program's creator is not the evidence: closures of these builds are made while Luraph's
  lazy-constant decoder, itself VM code, runs.)
* **Limits.**  A script whose loop keeps running code it has not run before (every round new) is
  only stopped by `--stall-run`; a plain-Lua `for` loop that never ends is not counted (only
  `while` and `repeat` are) and is stopped by the last-resort limit; the tamper response is
  recognised by what it is handed, so a build whose tamper response looks different is stopped
  by the time limits, after it has damaged the state.

Taken together: a run that never ends -- the script waits for something the mock never delivers,
sits in its own loop, or the run ends up in Luraph's tamper response, which loops for ever in VM
code without new instructions, waits or environment access -- went on until the last-resort limit
and failed.  With the watch the run is stopped (by a limit, or at the tamper response's entry),
the state at that moment is dumped, the program the script runs in is chosen from the run's
evidence (above), and the result is lifted from it, PARTIAL, with the reason in its report; a run
that showed nothing of the script still fails.

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
  A UDim holds what the engine stores (deobf's Studio-verified `datatypes.luau`): float32 Scale,
  int32 Offset truncated toward zero (a value in (-1, 0) gives 0, not -0), `UDim2:Lerp` offsets
  rounded; the curve is measured against the parent Frame as it is when asked;
* the GUI layout (`AbsoluteSize`, `AbsolutePosition` of the start-up code's probe tree from v14.6 on,
  and the Frame a Path2D is measured against) is computed in float32, every operation rounded as the
  engine's single-precision math rounds it: a Frame of Scale 0.1 in 1920 pixels is 192 wide, not
  192.0000029, and one of Scale 0.7 x 0.3 in 400 x 300 is exactly 280 x 90 (in double precision
  279.99999523 x 90.0000036: a different Path2D curve, and `math.floor(v * 100)` one lower).  The
  order of the engine's operations was not measured (`rbxmock.lua`, "GUI layout");
  `GetBoundingRect` is the box of the control polygon (every point and the tangent handles that
  shape a segment) widened by `Thickness`/2, in the parent's pixels: the plausible rule, shared
  with deobf and not measured in Roblox.  The candidates were the box of the points only, the
  exact box of the curve or the control-polygon box (adopted); widened by 0, `Thickness`/2
  (adopted) or `Thickness`; in Frame-local (adopted) or absolute coordinates; the start-up code
  seen (v15.1/v15.2) calls it but never reads the Rect, so no run tells them apart.  The Rect is
  a locked userdata (`Min`, `Max`, `Width`, `Height`; other members and assignments raise; `Min`
  and `Max` are a new Vector2 on every read); a path with fewer than 2 points gets a logged
  stand-in of type Rect.  UDim, UDim2 and Vector2
  are still plain tables with an open metatable (`type` says "table"), unlike Roblox's;
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
  harness run's reason is the meaningful end of its output (`vmmodel.harness_tail`, at most 600
  characters: the last four verdict lines -- `[oracle]`, `[budget]`, `[timeout]`, `[stalled]`,
  `[end]`, `[error]`, `[fail]` -- and always the error itself, the last line without a `[tag]`,
  whole when they fit and otherwise sharing the room, the longest cut first; then the lines
  before, while they fit; without the per-access `[global]` / `[call]` trace lines.  Those up to
  five lines need 27 characters at their shortest, `...` each and ` | ` between them; a limit
  below that cuts the joined line to the limit).  A traced run that does not end is normally
  stopped by the harness itself and lifted (see "A script that does not end"); one that showed
  nothing of the script fails with its `[stalled]` line, and one that still did not end at the
  last-resort limit of 1800 s says so in a `[timeout]` line.
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
  script's output came from the first interpreter (from programs that do not run under one of
  the programs on other interpreters) while programs on other interpreters exist, those are
  helpers and not the script; and when a run that did not end has that output in a
  program the start-up code entered at the end of its run, that program is lifted on its own
  (PARTIAL: the first program's code before the call is not in it).

### One function made at several places

Luraph's VM makes a closure wherever the code makes one, and some builds make a new closure of the
same program at each place that calls it (a helper of the build, called on the spot at five
places).  Printed as it runs, that is five copies of one function body,
`(function(p1, ...) ... end)(...)`, each lifted and structured on its own.  The printer
(`structure.Decompiler.run`, `HOIST_SHARED`) prints such a function once, as a local function at
the top of the result (one comment line says why), and the places call it by name.  Only where
that cannot change what the code does:

* the function captures nothing: a closure without upvalues made here or there is the same
  function;
* every place calls it on the spot: none keeps it, so no one can compare two of them;
* every place passes the same large literal numbers (10^6 and up): the text passes of
  `opaque.py` fold Luraph's per-call keys and opaque numbers per call site of a function called on
  the spot, and for a named function only where all callers agree;
* a function it calls the same way is defined before it; functions that make each other, a
  function that does not print as one (unstructured), or more than 195 locals at the top level
  keep the printing as it was.

The decision is taken after a first printing; the second one starts from fresh names.  The
printing for `opaque.relift_params` (`DEVM_PROTO_IDS`) is never changed.  `tests/devm_print.py`.

### Values the run proves (helpers that write into their caller's registers)

Some builds decode their string constants at run time with a helper of the build: a routine on
another interpreter (in the builds seen, the interpreter the script's program does not run on) that
the script's code calls with an encrypted constant, its own register array and the number of a
register, and that stores the plain string in that register (in the interpreter's terms a call
`f(..., enc, R, n)`; devm prints the register array as `({})`).  Nothing in the lifted code of the
caller writes register `n`, so the text used to show the encrypted constant wherever the script
read it afterwards: a message built from it printed ciphertext where the run printed plain text.

The traced run knows better.  The harness's oracle now keeps, for every instruction that ran, the
strings and booleans each register held at every visit (`s`, next to the numbers of `r`; strings up
to 4096 bytes; nothing for code only the exercise or probe pass ran) and which registers held the
activation's own register array (`a`).  For an instruction whose call entered a VM function it also
keeps which of the caller's registers had changed when the caller went on (`w`), and at how many
visits that was seen (`h`): the harness copies the caller's registers when the callee's first
instruction runs and compares them at the caller's next instruction (`orcPend`; Luraph's closures
take their arguments as `...`, so the callee's own registers do not show that it was handed the
array).  `runvals.show_run_values` (called by `pipeline` after the lift) looks at every call
handed the register array (an argument that stands for it in the code, or one read from a register
the run shows holding it) and, when every execution of that instruction went on to the same next
instruction, puts `R[r] = V --[[the run's value after the call]]` right after the call for each
register `r` that the instruction itself
does not write after the call, that no function of the program captures by reference, that the code
reads after the call before writing it again, that the next instruction saw holding `V` at every
visit, and that did not hold `V` before the call -- when the rules below show that every execution
of the call writes the same.  The rest of the lift (expression folding, dead
stores) works from there: the decoded value shows where the script reads it.  A helper of the build
whose result the script keeps in a register (`R[a] = helper(enc)`) is treated the same way: the call
stays as a statement, handed its constant, and the register gets the value the run saw.

* **Never complete with a call handed the register array.**  Such a call can change any variable of
  the code that calls it, and what it changes is shown only as far as the run saw it: a variable the
  call does not name and the run did not see it change keeps its value from before the call in the
  text.  So complete is never claimed for a text with such a call, even where every value it changed
  is shown and proved: every call handed the array (each one `show_run_values` found, and every call
  the code shows handed it, also in an arm of a branch or an instruction that lifted with an error:
  `runvals.handed`) gives the run note `variables handed to calls: N call(s) are handed the variables
  of the code that calls them ...`, and the report counts `({})` printed as a value in the text
  (outside strings and comments; `({})[k]` is also how a new table nothing keeps is printed and is
  not counted).  Either alone makes the result PARTIAL: also when the run values did not run (the
  proto-table layout lifts without a traced run, and its texts are checked the same way) or found no
  call.  In the measured sets no complete result had such a call (see the corpus line below).
* **Every value shown says what it is.**  No value of the run goes into the text bare: a proved one
  carries `--[[the run's value after the call]]`, an assumed one the comment that begins with the
  run's value and says what is assumed (below).  Both are literally true: the value is what the run
  had in that register right after the call, at every visit (the next instruction saw it, and the
  instruction writes nothing there after the call).  The notes and the report say how many values are
  proved and how many assumed.
* **Only what the run proves for every execution.**  The run shows what one execution of the call
  wrote.  A value goes in only when the code shows that every execution of the call writes the same
  (`runvals._Proof`):
  1. the function called is a closure the program makes (`Flow.fixed`); a library function handed
     the register array (`table.move` on it, say) is not followed;
  2. every argument besides the register array is a constant (or arithmetic on constants) or a
     standard library function, followed back through every definition that can reach the call; a
     function of the interpreter's environment that is not one (`os.clock`, say) is not a constant;
  3. every variable the closure captures holds a constant, a standard library function or a closure
     that meets these rules itself (followed a few levels deep), and nothing changes it after the
     closure is made (no other statement of the program writes the register, no function assigns it
     through its upvalue);
  4. the closure's lifted code, the functions it makes included, reads no global but the standard
     library's tables and functions (`string`, `table`, `math` but `math.random`, `bit32`, `utf8`,
     `buffer`, `select`, `tostring`, ...; a field the library does not have, such as `string.n`, is a
     place where a script can keep a value, also when it is read through a variable that holds the
     table, with `rawget` / `rawset` on a table the function did not make, called directly or through
     `pcall` / `xpcall`, or through `getmetatable` of a value it did not make, such as
     `getmetatable("").__index`; nor `coroutine.yield`, which returns whatever the code that resumes
     the function hands it), no value of Luraph's
     loader, changes no variable it shares with other functions, and only stores into the register
     array it is handed (never reads it, nor passes it on).  The library's functions count as
     functions of their arguments, except `tostring`, `next`, `pairs`, `table.foreach` and
     `string.format`: `tostring` of a table or a function (and `string.format` with `%s` or `%p`) is
     its address, and `next`, `pairs` and `table.foreach` go through a table's keys in the order they
     are stored in, which can differ between executions.  A function called that uses one of these,
     is handed one or shares a variable that holds one is taken to give the same at every execution
     only as an assumption (below).
  And for all the calls of the function that makes the call together (`runvals._settle`): every
  call handed its register array meets these rules, and the array reaches no other code (a call that
  may be handed it but is not one of those followed, such as a call in one arm of a branch, the
  array kept in a table, a global or a shared variable, returned, or stored into through a register
  that holds it).  A call that fails a rule (`setmetatable` on the array, a helper handed a register
  number read at run time) can keep the array and change any register later -- through any call, or
  a metamethod -- which no statement shows; the rules above follow the statements, so then nothing
  in that function is proved.  A register that a function of the program shares by reference and
  assigns through its upvalue is not a constant anywhere in the program (a call of that function,
  which is not handed the register array, changes it).
  When a rule fails, nothing is substituted: a value the script read at run time (a global, an
  argument, a result, a register of the caller) can differ at another execution, however often this
  run saw the same one.  When no rule fails but the check cannot be completed, the run's value is
  shown with a comment right at it, the report has a `values assumed: ...` line, and the result is
  PARTIAL: that the value depends only on what the call is handed is then assumed, not proved.  The
  comment says what could not be checked:
  * `--[[the run's value; assumed to depend only on the call's inputs: the function called was not
    fully lifted]]`: the code of the function called that ran (by the oracle's records of its
    program; all of it when there are none) has lift errors, instructions that did not lift, or
    values the lift could not describe;
  * `...: the function called uses tostring, next, pairs, table.foreach or string.format]]`: see
    rule 4;
  * `...: the calling code was not fully lifted]]`: an instruction of the function that makes the
    call, one that can be reached from its entry, did not lift or lifted with an error (what it does
    to the registers, and which functions it makes, is not known), or a variable the call depends on
    is shared with a function that did not all lift;
  * `...: another call handed the register array was not fully checked]]`: another call handed the
    same register array is only assumed to meet the rules.
  (Earlier text here said that such a function "does the same at every execution" when it is a
  closure handed constants: that was a claim about its captures and its code that nothing checked.)
* **What the run saw a call change, and what it saw it leave alone.**  The harness sees a call only by
  the VM function it enters.  Where every visit saw one entered (`h` == `n`), each register in its
  record of what changed (`w`) is one the call may have set, and is proved or marked as below,
  whether the instruction has one call or several, the code resolves the callee or not, and the call is
  one of the instruction's own or sits in an arm of a branch (below), save the exemption of a shared
  variable below; `w` is only ever used to add a register, never to leave one out (a callee that is no
  VM function changes registers the harness does not see change).  That the run saw a call leave a
  register alone counts only for the proof of another call, and only where the instruction's one call
  is the one handed the register array, calls a closure of the program and meets the rules itself
  (`runvals._trusted`; in a loop the instructions are taken together, by induction over the run):
  otherwise it can write that register at another execution.
* **What is not proved is marked, not shown stale.**  For a register the call may have set -- one
  that held one value before the call and none after it, one the call names (a constant register
  number handed with the register array), one the run saw the call change -- where the run proves no
  value (no one value after the call, or a rule above fails), `R[r] = nil --[[set by a call handed
  the register array: value not shown]]` goes in after the call, so the text does not print the
  earlier value (the encrypted constant, or nothing) as the current one.  The same after a call that
  did not run or went on to more than one instruction, and after each call of an instruction that
  hands the register array to several, for the registers it names and those the run saw any of them
  change (which of them did is not known).  A register a function of the program shares (captures by
  reference) is marked too when the call names it or the run saw it change (by `w`, else because it
  held one value before the call and another, or none, after it), unless the call does not name it, a
  function of the program assigns it through its upvalue, and that change can be the one the run saw:
  the function called is that function, makes it or shares it (through `pcall` / `xpcall`: the function
  handed to them), or it is not resolved and a function that assigns the variable ran (the oracle has
  a record of its statement that assigns it): it may be that function.  Then that function's text shows
  the change.  A call that names the register writes it through the register array, which no text
  shows; a library function called is no function of the program; and a function that assigns the
  variable but is not the one called (nor made or shared by it), or that never ran, did not make the
  change the call made (`runvals._exempt`).  A register the instruction's own statements after
  the call read before setting it again is marked (the next instruction saw what they made of it,
  not what the call left); one they set again without reading it first is left alone.  One such
  register is enough for the call to count as not shown (`values not shown: ...`, PARTIAL), whatever
  else it shows.  A register nothing reads after the call is not counted.
* **Calls that are no site of their own.**  The values above are those of a call of the instruction's
  own statements.  A call the code shows handed the register array somewhere else -- in an arm of a
  branch (`if x then f(s, R, 2) end`), or inside another value -- gets the mark all the same, for the
  registers it names and those the run saw the instruction's calls change, right after it (in its
  arm), with the same exemption of a shared variable; it counts as not shown (`values not shown`).  And
  where an instruction has no call handed the register array but the run saw its calls change a
  variable (`w`, every visit seen) that no statement of the instruction writes after its first call,
  and that no function that shares it and assigns it through its upvalue in code the run executed can
  have changed, nothing in the text changes it: only the register array reaches a variable nothing
  shares, so the call, or code it ran, had the array somewhere the code does not show (`f({R}, s, 2)`
  lifts the table as a value the lift could not describe).  That variable gets the mark after each of
  the instruction's calls, and the call counts as not shown (`runvals._other_calls`; kind `seen` in
  the devm log).
* **What is not seen.**  A register that held no one value before the call nor after it, that the
  call does not name, and that the run did not see the call change (a callee that is no VM function
  is not seen) is left as the code has it: best effort, which the `variables handed to calls` line
  says, and the reason such a text is never complete.  So is, after a call the code does not show
  handed the register array, a change that the instruction's own statements after its first call can
  explain (the record does not say whether the call or those statements made it).  A call that fails
  a rule but, as far as the run saw, changed nothing the code reads afterwards gets no mark and no
  note of its own (its `why=` is in the devm log); what it can do at another execution is what makes
  every other value of the same function not proved (above), and those are marked.  In a loop, the
  function called is only proved when the run saw the call leave the register that holds it alone
  (the call itself could change it).
* **Known gaps in the marks.**  The marks above are best effort, not a proof.  Synthetic probes still
  find texts where a variable such a call changed is printed with its value from before the call and
  no mark: the register array handed through a temporary in an arm of a branch; a change that a later
  statement of the same instruction also writes; a shared variable that some function of the program
  assigns (the exemption above), also when that function did not run during the call; and a later call
  of the same instruction that changes the register again after a proved site (the value shown is then
  the one the next instruction saw, not the one the site's call wrote).  None of these was found in
  the measured corpus or in the files it was tried on, and every text with a call handed the register
  array is PARTIAL, so none of them can make a result complete.
* Calls are kept: whether a helper has other effects is not known (its own lift can be incomplete).
* Programs the pipeline names helpers of the build (programs on other interpreters set aside
  because the run's weighed evidence points at the first interpreter, see "Which program is the
  script") are labelled in the text, as the first line of their body, with that evidence and not a
  verdict on their code: `-- program N: set aside as a helper of the build (on another interpreter
  than the program taken for the script; first called by program M)`, with what it writes when it
  writes into its caller's registers.  A function the pipeline does not name a helper gets no label
  (its values are shown all the same: the call wrote them).
* The run notes: `note: N call(s) of program K, set aside as a helper of the build ... write into
  its variables: after each of them the text shows the values they wrote as the run saw them (V
  value(s), each with a comment: P proved, ..., A assumed)`; where the run does not prove what such
  calls wrote, `values not shown: ...`; where the values shown are assumed, `values assumed: ...`;
  and for every call handed the register array, `variables handed to calls: ...`.  Each of the last
  three makes the result PARTIAL.  The devm log has a
  `runvals:` line with every call found, also those the run proves complete (with `assumed` and
  the reason, `why=...`, where a call is not proved).
* Cost: the oracle file is a fifth to a third larger, and the traced run a few per cent slower
  (side by side on the same machine: 62 s against 60 s and 88 s against 83 s of processor time).
  The copies of the caller's registers at each VM call cost nothing measurable: the 12 traced
  runs of the corpus's factory-layout files took 714 s and 729 s of processor time in all (two
  runs), against 755 s before, within the spread of the runs themselves.
* Corpus: the factory-layout files of the public sample corpus (the 12 v15.1 / v15.2 files that
  run) come out as before, byte for byte but for the count of VM instructions in the head.  What
  was measured is the `runvals:` line of each run's log, which lists every call handed the
  register array and every call of a helper whose result goes to a register, whether the run
  proves it or not (a missing note alone would not show that there was no such call): 11 runs
  have none; in one, a build without a second interpreter whose result is the first program, the
  start-up code has two calls handed the register array, both of which ran.  One entered a VM
  function (`h` 1, `w` empty), but the code does not show which function it calls, so that record
  is not used (see "Which record of the run covers the call"); the other is `setmetatable` handed
  the register array, a library function whose effect the run cannot see.  Neither is proved
  (`why=` in the log line: the function called is not shown to be one the program makes; the
  function called is a library function), so nothing else in that program could be either, and
  neither gets a mark or a note of its own: no register the code reads after them held one value
  before the call and another (or none) after it, none they name is read afterwards without such a
  value, and the first one's register the run saw change is a variable that a function of the program
  that ran assigns through its upvalue, and the function called (not resolved) may be that one.  Both
  are counted by that result's `variables handed to calls` line (it is PARTIAL in any case: it is the
  start-up code).  No complete result of the
  measured sets has such a call: none of the 6 complete factory-layout results, of the 53 complete
  results of the 126-file hold-out set nor of the 24 of the 44-file regression set prints the
  register array as a value (`({})` as an argument or the value of a variable; 38 of the 67 PARTIAL
  hold-out results, 6 of the 20 PARTIAL regression results and one factory-layout result do), and
  those complete results come out byte for byte as before.
* `tests/devm_runvals.py`: the harness's records, and hand-written programs for the small VM of
  `tests/devm_lift_v14.py` whose helper decodes a constant while the probe runs, with the negative
  cases (values that differ per execution, a call that did not run, two successors, another table in
  the register, a function of the script that keeps its result), a call that writes two registers
  of which the run proves one, a register the call names that holds no one value before or after
  it, a helper handed a value read at run time, and the same probes with the helper's work done by
  a VM function of its own, which the harness sees.  The rules above each have a probe: a helper that
  reads a global, that reads its caller's registers through the array, that captures a value read at
  run time (and one that captures a constant, shown), a library function handed the array, a helper
  whose code did not all lift (shown, assumed, PARTIAL), a call that is no VM function next to one
  that is in the same instruction, a helper called through `pcall`, arguments from a table or from one
  of two paths, two calls handed the array in one instruction, a register shared by a function made
  after the call (and one the function called assigns through its upvalue, not marked), and a
  register the instruction reads again after the call (and one it only sets again).  And what the
  proof follows (`WhatTheCodeShows`): an argument changed through a function that shares it,
  another call handed the array and a register number read at run time (also where the helper
  shares a variable by reference), `setmetatable` on the array, the array kept in a table, a shared
  register changed by a call no record covers (no VM function; a VM function the code does not
  name), a field the library does not have, `tostring` of a table (assumed), calling code that did
  not all lift (assumed), another call only assumed (assumed), a variable shared with a function
  that did not all lift (assumed), and a helper that calls a function it shares that returns a
  constant (proved).  And (`CallersVariables`): a call proved in full still leaves the text PARTIAL,
  by its run note alone and by the printed `({})` alone; the same without the run values and for a
  call in an arm of a branch (no site); every value put in carries its comment and is the run's
  value after the call; a loop whose callee and register number the code does not resolve, where only
  the run's record says the call changed the register (marked), and two such calls in one
  instruction; `os.clock` shared with the helper or handed to it (no constant); `tostring` handed to
  it (assumed); a shared register the call names while another function assigns that variable
  (marked) and the same without that function (proved, with its comment); `rawget` on a library
  table (not proved) and `table.foreach` (assumed).  And (`CallsThatAreNoSite`): a call in an arm of a
  branch that names the register, or is handed a register number read at run time (marked in the arm);
  `pcall` handed the helper, the array and a shared variable another function assigns (marked); a
  callee read from a global with such a variable, where the function that assigns it never ran
  (marked) and where it ran (not marked); the array handed inside a table (marked by the run's record
  alone); a call of the function that assigns a shared variable, handed no array (not marked,
  complete); and `rawget` through `pcall`, `getmetatable("").__index` and `coroutine.yield` in the
  helper (not proved).

### Names

`renamer.py` runs last in `cleanup.tidy` and can be used on its own
(`python -m devm.renamer in.lua -o out.lua`).  It re-derives names for generated identifiers from
how a value is made and used: initialisers (`:Clone()`, `tonumber(x.Field)`, getter calls, table
contents), function bodies (`getX`, `safeRequire`, `create`, the one remote or method a function
wraps), event handler parameters (`dt`, `character`, `input`), loop variables (`_, child`) and
counters (`elapsed`, `count`).  Generated names are unique per file, so this is a token
substitution; what no rule covers keeps its number.
