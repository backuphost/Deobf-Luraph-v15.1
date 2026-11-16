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

| VM layout | Example | State |
|---|---|---|
| proto-table (`wrap(proto, upvalues)`) | HookScript, ClientSynchronizer | complete, 0 lift errors |
| factory, multi-interpreter (`factory(...)(upvalues)`), also the raw single-line form | loadstring6 | first stage lifts and decompiles; see below |

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
  Studio-verified model of the "deobf" Luraph deobfuscator; the float32 bit patterns are key bytes;
* executor-style globals (`identifyexecutor`, `iscclosure`, `islclosure`) exist, as in the
  reference environment the key material was compared against.

`harness.lua` options for diagnosing a failed check: `stub=<pid>` (suppress the tamper response),
`watch=<pid>` (log the arguments of that proto's last calls, e.g. the comparison helper), `tb=<pid>`,
`depth=<n>`.

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

### Names

`renamer.py` runs last in `cleanup.tidy` and can be used on its own
(`python -m devm.renamer in.lua -o out.lua`).  It re-derives names for generated identifiers from
how a value is made and used: initialisers (`:Clone()`, `tonumber(x.Field)`, getter calls, table
contents), function bodies (`getX`, `safeRequire`, `create`, the one remote or method a function
wraps), event handler parameters (`dt`, `character`, `input`), loop variables (`_, child`) and
counters (`elapsed`, `count`).  Generated names are unique per file, so this is a token
substitution; what no rule covers keeps its number.
