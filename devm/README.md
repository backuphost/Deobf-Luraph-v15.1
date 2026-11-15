# devm — Luraph devirtualizer

Turns a Luraph-protected script (as plain Lua, e.g. after a bytecode decompiler) back into readable
source by partially evaluating the VM interpreter over every instruction.

```
# one-time: build the Luau CLI with the file helpers (see luau_cli.patch) and point LUAU_BIN at it
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
| factory, multi-interpreter (`factory(...)(upvalues)`) | Net, TeleportGuard | work in progress |

The factory layout already has: multi-interpreter model, harness hooks, `FactoryLifter`,
lift-time constant propagation, trampoline-jump specialisation and self-rewriting instructions.
Still missing: evaluating the lazy constant metatable (`__index` closures of the loader), lazily
loaded child protos, and the second stage for that layout.
