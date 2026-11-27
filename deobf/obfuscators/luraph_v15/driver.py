"""
Luraph v15 pipeline (see LURAPH.md).

The protected script runs in the fake environment with every VM closure
instrumented (patch_entries). Checks that cannot be emulated exactly end in
a trap that corrupts the bytecode; the runtime reports which function made
that call and the script runs again with that function turned into a no-op.
Loadstring'd VM chunks are instrumented the same way and passed back.
Then the captured protos are lifted (devirt.py), with constant rounds for
code that never ran.
"""
import os
import re
import sys
import time

import harness
import limits
import traceout as trace

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_RERUNS = 12
SPIN_CHECKS = 24    # watchdog checks (2^20 VM steps each) without environment access = endless loop


def patch_spin(src):
    """Spin watchdog (envlog's --cfg spin): every VM dispatch loop head counts
    steps in __SPIN.n and calls __SPIN.f() every __SPIN.step of them. Table
    operations only, plus that rare call (no locals: see patch_entries)."""
    return re.sub(r"while true do (?:local )?[A-Za-z_]+(?:,[A-Za-z_]+)*=[A-Za-z_]+\[[A-Za-z_]+\];",
                  lambda m: m.group(0) + "__SPIN.n=__SPIN.n+1;if __SPIN.n>=__SPIN.step then __SPIN.f()end;", src)


def patch_chunk(src, tmpdir, shapes=None, hooks=True):
    """Instrument a loadstring'd VM chunk like the main script (hooks=False:
    only note its VM shapes)."""
    path = os.path.join(tmpdir, "_chunk_%s.luau" % harness.chunk_key(src))
    with open(path, "w", encoding="latin-1", newline="") as f:
        f.write(src)
    try:
        return patch_entries(src, path, shapes, hooks)
    except Exception as e:  # not parseable by luau-ast: run it as is
        print("[!] could not instrument chunk (%s)" % e, file=sys.stderr)
        return src
    finally:
        os.remove(path)


def patch_entries(source, path, shapes=None, hooks=True):
    """Give every VM closure an entry hook. It only uses table operations (no
    calls, no locals), so the stack looks exactly as it would without it: it numbers
    protos in first-entry order, logs recent entries and returns early for the
    protos listed in __SKIPP.
    shapes (a set): gets "nested" when a VM has Luraph v15.2's nested closure
    maker (vmmap.maker_info "scope"); hooks=False returns the source as it is."""
    from obfuscators.luraph_v15 import vmmap
    root = vmmap.load_ast(path)
    if shapes is not None and any(info.get("scope") for info in vmmap.maker_info(root)):
        shapes.add("nested")
    if not hooks:
        return source
    lines = source.split("\n")
    edits = []
    for l1, c1, pv in vmmap.closure_entries(root):
        # no locals: they would enlarge every VM frame, and Luraph mixes the
        # depth its stack-overflow probe reaches into its decryption keys.
        # __PLAST[pid] = entry number of the latest entry of that proto (it
        # tells invocations apart when statements are attributed to protos)
        k = "(%s or __PID)" % pv
        edits.append((l1, c1, ("if not __PID[{k}] then __PID.n=__PID.n+1;__PID[{k}]=__PID.n;end;"
                               "__ENT.n=__ENT.n+1;__ENT[__ENT.n%64]=__PID[{k}];__PLAST[__PID[{k}]]=__ENT.n;"
                               "if __SKIPP[__PID[{k}]] then return end;").format(k=k)))
    # closure -> proto, recorded where the closure is made (not per call), so
    # the runtime can map the functions on the Lua stack back to protos
    tag = harness.chunk_key(source)
    for info in vmmap.maker_info(root):
        (l2, c2), var, pv = info["at"], info["var"], info["proto"]
        code = " __PF[%s]=%s " % (var, info.get("pf_key", pv))
        # devirtualizer capture: once per proto, the maker-level values the VM
        # closure uses (operand arrays, constants, helpers). Table operations
        # only, like the entry hook; runs where the closure is made, not per call.
        cap = "".join("__PA[%s].%s=%s;" % (pv, nm, nm) for nm in info["captures"])
        # __PK: one closure per proto, for calls the devirtualizer asks the
        # runtime to evaluate (LPH_ENCSTR-style string decryptors)
        new = ("__PA[%s]={};__PA.n=__PA.n+1;__PA[%s].__seq=__PA.n;__PA[%s].__maker=\"%s@%d,%d\";"
               % (pv, pv, pv, tag, l2, c2))
        if info.get("outer_ret"):
            # v15.2: the function around the maker runs once per proto, when the
            # proto is deserialised (lazily, by the constant decoder). Its hook
            # records maker function -> proto (__PM: a closure op calls that
            # function) and the proto's own values under "^name" (the maker's
            # locals can shadow them), so protos no closure was made of can be
            # lifted too. The maker's hook then only adds what it sees.
            ol, oc, mc, oc2 = info["outer_ret"]
            if lines[ol][oc:mc].strip() != "return":
                print("[!] unexpected text before a closure maker: %r" % lines[ol][oc:mc], file=sys.stderr)
                continue
            ocap = "".join("__PA[%s][\"^%s\"]=%s;" % (pv, nm, nm)
                           for nm in sorted({nm for _, nm in info["outer_decls"]}))
            edits.append((ol, oc, "__MK[2]=", mc))
            edits.append((ol, oc2, " if __PA then __PM[__MK[2]]=%s;if not __PA[%s] then %s%s end end return __MK[2] "
                          % (pv, pv, new, ocap), oc2))
            code += ("if __PA and not __PK[%s] then if not __PA[%s] then %s end;__PK[%s]=%s;"
                     "__PA[%s].__made=true;%s end "
                     % (pv, pv, new, info.get("pf_key", pv), var, pv, cap))
        else:
            code += ("if __PA and not __PA[%s] then %s__PK[%s]=%s;%s end "
                     % (pv, new, info.get("pf_key", pv), var, cap))
        if info.get("ret"):
            # v15.2: `return function...end` -> `__MK[1]=function...end;<hooks> return __MK[1]`
            rl, rc, cc = info["ret"]
            if lines[rl][rc:cc].strip() != "return":
                print("[!] unexpected text before a maker closure: %r" % lines[rl][rc:cc], file=sys.stderr)
                continue
            edits.append((rl, rc, "__MK[1]=", cc))
            # (the return statement's own location already ends after its optional `;`)
            edits.append((l2, c2, code + " return __MK[1] ", c2))
        else:
            edits.append((l2, c2, code, c2))
    if shapes is not None and "nested" in shapes:
        # v15.2: a jump to a register's value (`pc = R[B[pc]]`) can be keyed by
        # what Luraph's environment probes returned, which no static lift
        # knows. Record where each one went (table operations only):
        # __JT[proto key][pc] = the target, false once two targets were seen.
        for info in vmmap.maker_info(root):
            if not info.get("scope"):
                continue
            pk = info.get("pf_key", info["proto"])
            if pk in vmmap._decls_in(info["vm"]).values() or any(
                    pk in vmmap._decls_in(f).values() for f in vmmap.inner_functions(info["vm"])):
                continue    # (the proto key's name is reused inside the interpreter)
            # The same record for a key register set from a register (`Q =
            # R[A[pc]]`, vmmap.key_registers): the value keys the decryption
            # of the code behind it. (One instruction is one or the other.)
            for jl, jc, text in vmmap.register_jumps(info["vm"]) + vmmap.key_registers(info["maker"], info["vm"])[1]:
                edits.append((jl, jc, ("if __JT then if not __JT[{k}] then __JT[{k}]={{}} end;"
                                       "if __JT[{k}][{pc}]==nil then __JT[{k}][{pc}]={v} "
                                       "elseif __JT[{k}][{pc}]~={v} then __JT[{k}][{pc}]=false end end;")
                              .format(k=pk, pc=text[0], v=text[1])))
            # Which instruction followed which in this run (__CV[proto key]
            # [previous pc * 65536 + pc] = true, at every dispatch loop head;
            # table operations only). A flattened v15.2 function is full of
            # checks whose failing side jumps into unrelated code, and of
            # decryptors only a tamper response runs: the lifter tells them
            # from the script's own untaken branches by what the run did.
            for d in vmmap.find_dispatchers(info["vm"]):
                hl, hc, _, _ = vmmap.loc(d["tree"])
                edits.append((hl, hc, ("if __CV then if not __CV[{k}] then __CV[{k}]={{p=0}} end;"
                                       "__CV[{k}][__CV[{k}].p*65536+{pc}]=true;__CV[{k}].p={pc} end;")
                              .format(k=pk, pc=d["pc"])))
        # v15.2: script functions kept as plain Lua (vmmap.plain_factories).
        # The function a closure op calls is recorded with where it is in the
        # source and the values of the function around it.
        for pf in vmmap.plain_factories(root):
            l, c, mc, c2 = pf["ret"]
            if lines[l][c:mc].strip() != "return":
                continue
            ocap = "".join(",[\"^%s\"]=%s" % (nm, nm) for nm in sorted({nm for _, nm in pf["decls"]}))
            edits.append((l, c, "__MK[3]=", mc))
            edits.append((l, c2, " if __PA then __PM[__MK[3]]={__at=\"%s@%d,%d\"%s} end return __MK[3] "
                          % ((tag,) + pf["at"] + (ocap,)), c2))
    edits = [e if len(e) == 4 else e + (e[1],) for e in edits]
    for l, c, code, end in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        lines[l] = lines[l][:c] + code + lines[l][end:]
    return "\n".join(lines)


def devirtualize(job, ppath, dpath, cfg, rerun, chunk_paths=(), live=None):
    """Lift the captured protos; constants that only Luraph's lazy decoder can
    produce (code that never ran) are requested from further runs. With a
    live harness (`live()` -> fetch function, while the long-lived harness
    made the current dump) the walks ask for them right away (a chain of
    constants, each needed to find the next, then takes one round instead
    of one round per link).
    Returns what the lifted text lacks, in words (empty: a whole lift)."""
    from obfuscators.luraph_v15 import devirt
    args = job.args
    requested = set()
    last_bufs = ""
    text = None
    unanswered = None   # why the constant rounds ended before their fixed point
    rounds = args.devirt_rounds
    # Intermediate rounds only need the constant requests and decrypted
    # strings, which come from walking each function: they skip the
    # structuring/codegen/naming and reuse the walks of functions that asked
    # for nothing (devirt.collect_requests). The text comes from one full lift
    # at the fixed point; if that lift still asks for something new, the
    # remaining rounds are full lifts (the old way).
    quick = not os.environ.get("DEVIRT_FULL_ROUNDS")
    cache = devirt.WalkCache()
    t0 = time.time()
    for rnd in range(1, rounds + 1):
        limits.check("lifting (constant round %d)" % rnd)
        t1 = time.time()
        full = not quick
        if quick:
            stats, reqs, bufs = devirt.run_big_stack(devirt.collect_requests, job.source_path, ppath, chunk_paths, cache,
                                                     live and live())
            new = reqs - requested
            print("[*] devirt round %d: %d functions (%d walked), %d unlifted blocks, %d new constant requests%s "
                  "(%.1fs)" % (rnd, stats["functions"], stats["walked"], stats["errors"], len(new),
                               " (%d decoded live)" % stats["fetched"] if stats.get("fetched") else "",
                               time.time() - t1), file=sys.stderr)
            if os.environ.get("DEVIRT_REQS"):
                for rq in sorted(new):
                    print("[*]     request %s" % rq, file=sys.stderr)
            if (not new and devirt.same_patches(last_bufs, bufs)) or rnd == rounds:
                full = True
        if full:
            print("[*] devirtualizing (round %d)..." % rnd, file=sys.stderr)
            t1 = time.time()
            text, stats, reqs, bufs = devirt.run_big_stack(devirt.lift_program, job.source_path, ppath, chunk_paths,
                                                           live and live())
            new = reqs - requested
            print("[*]   %d functions, %d unlifted blocks, %d unstructured jumps, %d new constant requests (%.1fs)"
                  % (stats["functions"], stats["errors"], stats["fallbacks"], len(new), time.time() - t1),
                  file=sys.stderr)
            if stats.get("traps"):
                # (Luraph v15.2, devirt.cut_cold_jumps)
                print("[*]   %d jumps of untaken paths into other code not followed (marked in the output)"
                      % stats["traps"], file=sys.stderr)
            if (not new and devirt.same_patches(last_bufs, bufs)) or rnd == rounds:
                if new or not devirt.same_patches(last_bufs, bufs):
                    unanswered = "the constant rounds ended after %d rounds with %d requests open" % (rnd, len(new))
                break
            if quick:
                print("[*]   the full lift needs more constants: continuing with full lifts", file=sys.stderr)
                quick = False
        requested |= reqs
        last_bufs = bufs
        c = dict(cfg)
        c["force_req"] = ";".join(sorted(requested))
        # strings decrypted in place by lifted code the runtime never ran
        c["force_buf"] = bufs
        t1 = time.time()
        body, err = rerun(c)
        if os.environ.get("DEVIRT_TIMING"):
            print("[*]   constant run %.1fs (total %.0fs)" % (time.time() - t1, time.time() - t0), file=sys.stderr)
        if body is None:
            print("[!] constant request run failed: " + err[-500:], file=sys.stderr)
            unanswered = "a constant request run failed: %d requests not answered" % len(new)
            break
        m = re.search(r"\x00PROTOS ([^\n]*)\n", body)
        if not m or m.group(1).startswith("error:"):
            print("[!] constant request run gave no protos", file=sys.stderr)
            unanswered = "a constant request run gave no protos: %d requests not answered" % len(new)
            break
        with open(ppath, "w", encoding="utf-8", newline="\n") as f:
            f.write(m.group(1))
    if text is None:
        # a request run failed before any full lift: lift what the last dump has
        print("[*] devirtualizing with the constants decoded so far...", file=sys.stderr)
        t1 = time.time()
        text, stats, reqs, bufs = devirt.run_big_stack(devirt.lift_program, job.source_path, ppath, chunk_paths, None)
        print("[*]   %d functions, %d unlifted blocks, %d unstructured jumps, %d constant requests not answered (%.1fs)"
              % (stats["functions"], stats["errors"], stats["fallbacks"], len(reqs - requested) + len(requested & reqs),
                 time.time() - t1), file=sys.stderr)
    header = (job.credit_header() +
              "-- Local names are inferred from use (the original names are not in the bytecode)\n")
    job.write(dpath, header + devirt.finish_text(text) + "\n")
    missing = []
    for key, what in (("errors", "unlifted blocks"), ("fallbacks", "unstructured jumps"),
                      ("stubs", "functions not lifted"),
                      ("traps", "jumps of untaken paths into other code not followed")):
        if stats.get(key):
            missing.append("%d %s" % (stats[key], what))
    undecoded = text.count("--[[ constant not decoded ]]")
    if undecoded:
        missing.append("%d constants not decoded" % undecoded)
    if unanswered:
        missing.append(unanswered)
    return missing


def run(job):
    """The whole Luraph pipeline; returns the result file's path."""
    args = job.args
    devirt_on = not args.no_devirt
    source = job.source
    # VM shapes seen in the script and its chunks: Luraph v15.2 ("nested"
    # closure makers) measures the C stack budget of the script's thread
    # exactly, so its runs start the thread the way Roblox does (envlog
    # --cfg top_thread; V15_2.md)
    shapes = set()
    try:
        patched = patch_entries(source, job.source_path, shapes, not args.no_hooks)
    except SyntaxError as e:
        if args.no_hooks:
            patched = source    # (nothing to instrument: run it as it is, as before)
        else:
            sys.exit("[!] the input is %s: the file is damaged (truncated, or mangled by a paste/upload); "
                     "nothing to run" % e)
    cache_path = harness.load_p2d_cache(job.input, args.studio)
    runner = harness.Runner(job)
    bridge = runner.bridge

    skip = []
    # spin watchdog from the first run: a script ending in a pure-VM endless
    # loop (tamper response, wait for remote code) then stops after a few
    # seconds instead of running into the timeout. DEOB_SPIN_LATE=1: only
    # after a timeout (the old way)
    spin = not os.environ.get("DEOB_SPIN_LATE")
    if spin:
        patched = patch_spin(patched)
    chunks = {}
    raw_chunks = {}     # key -> original source of a loadstring'd VM chunk (for devirt)
    body = None
    prev = None         # the trace of the run before (kept when a rerun runs into the time limit)
    trapped = None      # (body, raw output) of the run that hit the last trap
    cfg = None
    for attempt in range(1, args.max_runs + 1):
        # (same keys as harness.base_cfg, in the order the harness always had)
        cfg = {"time_budget": args.budget, "dump_strings": args.strings, "executor": args.executor,
               "skip_protos": skip}
        if args.input_text is not None:
            cfg["input_text"] = args.input_text
        if args.no_fold:
            cfg["fold"] = False
        if devirt_on:
            cfg["devirt"] = True
        if "nested" in shapes:
            cfg["top_thread"] = True
        harness.user_cfg(args, cfg)
        if spin:
            cfg["spin"] = SPIN_CHECKS
        print("[*] tracing %s (run %d)..." % (job.input, attempt), file=sys.stderr)
        body, err = runner.run(patched, cfg, chunks)
        if body is None and not spin and err.startswith("timed out") and not bridge:
            # stuck in the script's own code (no environment access, so the
            # runtime never regains control): rerun with a spin watchdog
            print("[*] the script never finished; re-running with a spin watchdog", file=sys.stderr)
            spin = True
            patched = patch_spin(patched)
            chunks = {k: patch_spin(v) for k, v in chunks.items()}
            cfg["spin"] = SPIN_CHECKS
            body, err = runner.run(patched, cfg, chunks)
        if body is None and prev is not None and limits.expired():
            # (the lift is cut short by the same limit; the trace stays a result)
            print("[!] %s: keeping the trace of run %d" % (err, attempt - 1), file=sys.stderr)
            body = prev
            break
        if body is None:
            harness.save_raw(args.raw)
            runner.finish()
            sys.exit("[!] " + err)
        body = harness.take_p2d(body, cache_path, bridge is not None)
        found, body = harness.take_chunks(body)
        prev = body
        added = 0
        nested = "nested" in shapes
        for key, src in found:
            if key not in chunks and args.no_hooks:
                chunks[key] = patch_chunk(src, job.outdir, shapes, False)
            elif key not in chunks:
                raw_chunks[key] = src
                chunks[key] = patch_chunk(src, job.outdir, shapes)
                if spin:
                    chunks[key] = patch_spin(chunks[key])
                added += 1
        if added:
            print("[*] script loadstring'd %d new VM chunk(s); instrumenting and re-running" % added, file=sys.stderr)
            continue
        if "nested" in shapes and not nested:
            # (--no-hooks: nothing to instrument, but this run had the wrong thread set-up)
            print("[*] script loadstring'd a Luraph v15.2 VM chunk; re-running as a top-level thread", file=sys.stderr)
            continue
        trig = re.search(r"\x00TRIGGER (\d+)", body)
        if trapped is not None and trace.stmt_count(body) < trace.stmt_count(trapped[0]):
            # disabling that function made the script stop earlier: the trap
            # is the script's own check (e.g. LPH_CRASH() after a failed
            # license check), not an environment divergence. Keep that run.
            print("[*] disabling function #%d made the script stop earlier: it is the script's own "
                  "crash check; keeping run %d" % (skip[-1], attempt - 1), file=sys.stderr)
            body, harness.LAST_RAW[0] = trapped
            skip.pop()
            break
        if not trig:
            break
        trapped = (body, harness.LAST_RAW[0])
        pid = int(trig.group(1))
        if pid in skip:
            print("[!] anti-tamper trigger %d fired again; giving up on reruns" % pid, file=sys.stderr)
            break
        print("[*] anti-tamper trap reached through function #%d; disabling it and re-running" % pid,
              file=sys.stderr)
        skip.append(pid)

    run_text = harness.trace_text(body)     # the run the constant rounds must repeat
    body = harness.p2d_miss(body, cache_path)
    if not devirt_on:
        runner.finish()
    harness.save_raw(args.raw)
    body = re.sub(r"\x00TRIGGER \d+\n?", "", body)
    protos_json, body = trace.take_line(body, "PROTOS")
    force, body = trace.take_line(body, "FORCE")
    if force is not None and devirt_on:
        print("[*] constants decoded on request: " + force, file=sys.stderr)
    unscrambled, body = trace.take_line(body, "UNSCRAMBLED")
    if unscrambled is not None and devirt_on:
        print("[*] %s function(s) scrambled by the script's LPH_CRASH(): dumped as they were "
              "when first created" % unscrambled, file=sys.stderr)
    body, strings = trace.take_strings(body)

    notes = ["anti-tamper trap functions disabled: %s" % ", ".join("#%d" % p for p in skip)] if skip else []
    text = trace.header(job.input, notes) + body
    # (said before the lift, which can fail or be cut short by the time limit)
    status = trace.status_line(body, job)
    lifted_lacks = None     # what a lifted text lacks (a list), None: no lifted text
    failure = ["the run captured no VM functions"]      # why there is none

    def write_trace():
        job.write(job.trace_path, trace.render(text, args))

    # the trace is the result only without devirtualization or when lifting
    # fails: otherwise skip its readability pass (minutes on a 250k-statement trace)
    if not (devirt_on and not job.debug):
        write_trace()
    if strings is not None:
        job.write(job.path(".strings.txt"), strings)
    dpath = job.path(".devirt.luau")
    if protos_json is not None:
        ppath = job.path(".protos.json")
        if protos_json.startswith("error:"):
            print("[!] proto capture failed: " + protos_json, file=sys.stderr)
            failure[0] = "the proto capture failed (%s)" % protos_json[:200]
        else:
            job.write(ppath, protos_json)
            if devirt_on:
                # the lifter needs the original source of every VM chunk
                chunk_paths = [job.write(job.path(".chunk_%s.luau" % key), src, encoding="latin-1")
                               for key, src in raw_chunks.items()]
                # (v15.2 "nested" shapes: one function holds Luraph's start-up code and the
                # script's main function, the check below does not apply.  Whether the lift
                # starts from that function -- the program the VM entered first -- or from a
                # payload VM of its own is the same test: nested_first)
                nested_first = None
                if "nested" in shapes:
                    reached = "nested"
                    nested_first = nested_root(job, ppath, chunk_paths)
                else:
                    reached = not_reached(job, ppath, chunk_paths)
                if reached is True:
                    if status != "finished":
                        # Nothing of the script to lift: the lift would be
                        # Luraph's start-up program, which is not the script.
                        print("[!] the traced run stopped inside Luraph's start-up code, before the script's own "
                              "code (%s): nothing of the script to lift" % status, file=sys.stderr)
                        failure[0] = ("the traced run stopped in Luraph's start-up code, before the script's own "
                                      "code (%s)" % status)
                    else:
                        lifted_lacks, failure[0] = lift(job, runner, patched, cfg, chunks, run_text, ppath, dpath,
                                                        chunk_paths)
                        if lifted_lacks is not None:
                            lifted_lacks.append("the script's main function was not identified: the output starts "
                                                "with Luraph's start-up code")
                else:
                    lifted_lacks, failure[0] = lift(job, runner, patched, cfg, chunks, run_text, ppath, dpath,
                                                    chunk_paths)
                    lack = root_lack(reached, nested_first)
                    if lifted_lacks is not None and lack:
                        lifted_lacks.append(lack)
    runner.finish()
    if devirt_on and lifted_lacks is not None and os.path.exists(dpath):
        # a lift full of calls to nil misread the VM: the trace is the better result
        with open(dpath, encoding="utf-8", errors="replace") as f:
            lifted = f.read()
        nil_calls = lifted.count("(nil)(")
        # a main function whose lift stops on its entry path (its top-level
        # code ends in the lifter's error marker) with less code than the
        # trace has statements shows less than the trace does
        code = [ln for ln in lifted.split("\n") if ln.strip() and not ln.startswith("--")]
        stopped = bool(code) and code[-1].startswith('error("devirt: ') and len(code) < trace.stmt_count(body)
        if not code:
            # (a run that failed before the VM made its first closure: the
            # "lift" of no function is the file header and nothing else)
            print("[!] nothing was lifted: the traced run made no VM function (%s)" % status, file=sys.stderr)
            job.lacks("nothing was lifted: the traced run made no VM function (%s)" % status)
        elif not stopped and (nil_calls < 50 or nil_calls * 100 < lifted.count("\n")):
            for what in lifted_lacks:
                job.lacks(what)
            if not trace.run_ended(status):
                # (a run the tracer had to stop: a tamper response scrambles
                # code, and what was dumped is then not the script's)
                job.lacks("the traced run did not end (%s)" % status)
            elif "nested" in shapes and status != "finished":
                # (v15.2: one function holds Luraph's start-up code and the
                # script, and its lift follows what the run executed: a run
                # that stopped with an error did not show the rest)
                job.lacks("the traced run stopped (%s): Luraph v15.2 code is recovered only as far as "
                          "the run got" % status)
            return dpath
        elif stopped:
            print("[!] the devirtualized output stops after %d lines (%s); the behaviour trace shows more: "
                  "writing it instead" % (len(code) - 1, code[-1][7:-2]), file=sys.stderr)
            job.lacks("the lift stops on the main function's entry path (%s)" % code[-1][7:-2])
        else:
            print("[!] the devirtualized output is broken (%d calls of nil); writing the behaviour trace "
                  "instead" % nil_calls, file=sys.stderr)
            job.lacks("the lift misread the VM (%d calls of nil)" % nil_calls)
        if not job.debug:
            os.remove(dpath)
        write_trace()
        return job.trace_path
    if devirt_on:
        print("[!] no devirtualized output; writing the behaviour trace instead", file=sys.stderr)
        job.lacks(failure[0] or "the lift produced no output")
        if not job.debug and os.path.exists(dpath):
            os.remove(dpath)
        if not os.path.exists(job.trace_path):
            write_trace()
    return job.trace_path


MAIN_UNKNOWN = ("the script's main function could not be identified: the output may start with Luraph's "
                "start-up code")
NESTED_START_UP = ("Luraph v15.2 keeps its start-up code and the script's main function in one function: the "
                   "output includes Luraph's start-up code, not separated from the script")


NESTED_CALLEE = ("Luraph v15.2 keeps its start-up code and the script's top level in one function: the lift "
                 "starts from the function that one called last, so the script's top level is not in the output")


def root_lack(reached, nested_first):
    """What a lift lacks for where it starts: reached is not_reached()'s answer
    (or "nested" for v15.2 nested shapes), nested_first nested_root()'s answer
    for a nested shape ('first' or True, 'callee', 'separate' or False, None).
    A nested shape's lift includes Luraph's start-up code when its root is the
    program the VM entered first; it lacks the script's top level when it
    starts from a function that program called (same VM); a payload VM made
    apart from the start-up VM is lifted without either."""
    if reached is None or reached == "nested" and nested_first is None:
        return MAIN_UNKNOWN
    if reached == "nested" and nested_first in (True, "first"):
        return NESTED_START_UP
    if reached == "nested" and nested_first == "callee":
        return NESTED_CALLEE
    return None


def nested_root(job, ppath, chunk_paths):
    """devirt.nested_root, or None when it could not be told."""
    from obfuscators.luraph_v15 import devirt
    try:
        return devirt.nested_root(job.source_path, ppath, chunk_paths)
    except Exception as e:  # noqa: BLE001 - the lift itself reports what is wrong with the dump
        print("[!] could not tell where the lift starts (%s: %s)" % (type(e).__name__, e), file=sys.stderr)
        return None


def not_reached(job, ppath, chunk_paths):
    """True when the function the lift would start from is the program the
    VM entered first, Luraph's own start-up program, and the run recorded no
    function of it as the script's main function (devirt.script_not_reached);
    None when that could not be told."""
    from obfuscators.luraph_v15 import devirt
    try:
        return devirt.script_not_reached(job.source_path, ppath, chunk_paths)
    except Exception as e:  # noqa: BLE001 - the lift itself reports what is wrong with the dump
        if os.environ.get("DEVIRT_TB"):
            import traceback
            traceback.print_exc()
        print("[!] could not tell the script's main function (%s: %s)" % (type(e).__name__, e), file=sys.stderr)
        return None


def lift(job, runner, patched, cfg, chunks, run_text, ppath, dpath, chunk_paths):
    """devirtualize() with its constant rounds answered by one long-lived
    harness (started now, so the script runs while round 1 walks); a fresh
    run per round if it fails or behaves differently.
    Returns (what the lifted text lacks, None), or (None, why there is no
    lifted text)."""
    args = job.args
    bridge = runner.bridge
    server = [None]
    synced = [False]    # the long-lived harness made the current dump
    if not bridge and not os.environ.get("DEOB_NO_SERVE"):
        server[0] = harness.HarnessServer(runner.luau, patched, cfg, chunks)
        server.append(False)    # its first run not checked yet

    def live():
        hs = server[0]
        if hs is None or not synced[0] or os.environ.get("DEOB_NO_FETCH"):
            return None
        return lambda paths, bufs: hs.fetch(paths, bufs, args.timeout)

    def rerun(c):
        synced[0] = False
        if bridge:
            return runner.run(patched, c, chunks)
        hs = server[0]
        if hs is not None and not server[1]:
            server[1] = True
            first, err = hs.reply(args.timeout)
            if os.environ.get("DEOB_SERVE_DIFF") and first is not None:
                for nm, tx in (("run", run_text), ("served", harness.trace_text(first))):
                    with open(job.path(".serve_%s.txt" % nm), "w", encoding="utf-8") as f:
                        f.write(tx)
            if first is None or not harness.same_trace(harness.trace_text(first), run_text):
                print("[!] the long-lived harness %s; running the script once per round instead"
                      % ("failed: " + err[-300:] if first is None else "traced differently"),
                      file=sys.stderr)
                hs.close()
                hs = server[0] = None
        if hs is not None:
            res = hs.request(c, args.timeout)
            if res[0] is not None:
                synced[0] = True
                return res
            print("[!] the long-lived harness failed: %s; running the script once per round "
                  "instead" % res[1][-300:], file=sys.stderr)
            hs.close()
            server[0] = None
        return runner.run(patched, c, chunks)
    try:
        return devirtualize(job, ppath, dpath, cfg, rerun, chunk_paths, live), None
    except Exception as e:
        # never lose the run over a lifter bug: the trace is still a result
        print("[!] devirtualization failed: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        if os.environ.get("DEVIRT_TB"):
            import traceback
            traceback.print_exc()
        if isinstance(e, limits.TimeLimit):
            return None, str(e)
        return None, "the lift failed (%s: %s)" % (type(e).__name__, str(e)[:200])
    finally:
        if server[0] is not None:
            server[0].close()
