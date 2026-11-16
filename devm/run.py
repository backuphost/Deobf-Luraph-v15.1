"""Whole pipeline: Luraph-protected script -> readable Lua.

    python -m devm.run <protected.lua> [-o out.lua] [--work DIR]

Stages
  1. lift the VM's first program.  For current Luraph builds that is only the *bootstrap*:
     environment checks that derive an XOR key, decrypt the real program and install a table of
     "key functions" used by self-modifying code.
  2. make the lifted bootstrap runnable outside Roblox (bootstrap.py), execute it natively inside
     the harness (mocked Roblox runtime, statistical key recovery) and dump the VM state again at
     the moment the real program starts.
  3. lift + decompile the real program from that second dump.  Key functions whose inputs could not
     be reproduced outside Roblox are solved for: only the right value turns the self-modifying
     patches into code that lifts without errors.
"""
import argparse
import json
import os
import subprocess
import sys

from . import bootstrap, cleanup, ircfg, lifter, normalize, opaque, pe, pipeline, structure, unidata, vmmodel

sys.setrecursionlimit(100000)
HERE = os.path.dirname(os.path.abspath(__file__))


def lifter_class(model):
    return lifter.FactoryLifter if model.factory_style else lifter.TableProtoLifter


def make_lifter(model, data, overrides):
    dump = pe.Dump(data, model)
    L = lifter_class(model)(model, dump)
    L.pe.keyfn_override = dict(overrides)
    return L


def lift_one(model, data, overrides, key):
    L = make_lifter(model, data, overrides)
    L.register(key)
    return L.lift_proto(key)


def solve_keyfns(model, data, log):
    """Lift everything; search values for key functions until no proto has lift errors."""
    overrides = {}
    L = make_lifter(model, data, overrides)
    protos = L.lift_all(L.root_key)
    for _ in range(6):
        bad = [k for k, p in protos.items() if p.errors and p.keyfns]
        if not bad:
            break
        changed = False
        for key in bad:
            base = len(protos[key].errors)
            for fn in sorted(protos[key].keyfns, key=str):
                good = []
                best = (base, None)
                for cand in range(-64, 513):
                    try:
                        pir = lift_one(model, data, {**overrides, fn: cand}, key)
                        e = len(pir.errors)
                    except Exception:      # a wrong guess can derail the evaluator in arbitrary ways
                        e = 10 ** 6
                    if e == 0:
                        good.append(cand)
                    if e < best[0]:
                        best = (e, cand)
                if best[1] is None:
                    continue
                pick = good[0] if good else best[1]
                log('[keyfn] function %s: solved value %s for proto %s (%d -> %d errors%s)' % (
                    fn, pick, key, base, 0 if good else best[0],
                    '' if len(good) <= 1 else '; other consistent values: %s' % good[1:6]))
                overrides[fn] = pick
                changed = True
                base = 0 if good else best[0]
                if base == 0:
                    break
        if not changed:
            break
        L = make_lifter(model, data, overrides)
        protos = L.lift_all(L.root_key)
    return L, protos, overrides


def decompile(stage, L, protos):
    d = structure.Decompiler(protos, L.root_key, stage.native_text)
    text = d.run()
    return text, d.stats


def run_bootstrap(model_stage, boot_text, workdir, tag, log):
    boot = os.path.join(workdir, tag + '.boot.lua')
    with open(boot, 'w') as f:
        f.write(boot_text)
    nf = os.path.join(workdir, 'nfdata.lua')
    if not os.path.exists(nf):
        unidata.generate(nf)
    out = os.path.join(workdir, tag + '.stage2.json')
    if os.path.exists(out):
        os.remove(out)
    r = subprocess.run([vmmodel.find_luau(), os.path.join(HERE, 'harness.lua'), '-a', model_stage.patched_path, out, boot],
                       capture_output=True, text=True, errors='replace', timeout=900)
    failed = 0
    for line in r.stderr.split('\n'):
        if 'failed environment check' in line:
            failed += 1
        elif line.startswith('[fixkey]') or line.startswith('[keyfn]'):
            log(line)
    log('[bootstrap] %d environment checks answered differently from Roblox (neutralised)' % failed)
    if not os.path.exists(out):
        raise RuntimeError('bootstrap did not reach the real program:\n' + r.stderr[-3000:])
    with open(out) as f:
        return json.load(f)


def deobfuscate(path, workdir, log=print, script_path=None, server=False, root=None, http_reply=None, executor=False, mfp_delta=None, exercise=True):
    os.makedirs(workdir, exist_ok=True)
    tag = os.path.splitext(os.path.basename(path))[0]
    with open(path, encoding='latin-1') as f:
        src = f.read()
    oneline = normalize.needs_reflow(src)
    if oneline:
        src, n = normalize.inline_protected_raw(src)
        if n:
            log('[%s] %d interpreter(s) ran their dispatch loops inside pcall: inlined for lifting' % (tag, n))
        src = normalize.reflow(src)
    else:
        src, n = normalize.inline_protected(src)
        if n:
            log('[%s] %d interpreter(s) ran their dispatch loops inside pcall: inlined for lifting' % (tag, n))
        log('[%s] minified input: reflowed to %d lines for analysis (it still runs as shipped)' % (tag, src.count('\n') + 1))
    # Luraph ships one line and the script checks that (line numbers in its own error messages and
    # in debug.info must all agree), so the copy that is executed is always laid out as one line,
    # also when the input is a decompiler's multi-line rendering of the bytecode.
    stage = pipeline.Stage(src, workdir, tag, log=log, oneline=True)
    stage.force_root = root
    if stage.model.factory_style:
        # self-decrypting builds: lift the VM state at the end of a traced run (see Stage.run_traced).
        # exercise=1: functions of the script that nobody called are called once, so that their
        # code decrypts itself too.
        stage.harness_options = (['exercise=1'] if exercise else ['binds=1']) + (['server=1'] if server else []) + (['scriptpath=' + script_path] if script_path else []) + (['httpreply=' + http_reply] if http_reply else []) + (['executor=1'] if executor else []) + (['mfpdelta=%d' % mfp_delta] if mfp_delta is not None else [])
        text = stage.run_traced()
        # Paths that never ran keep encrypted instructions and constants.  Execute each lifted
        # instruction that did not run once, from the interpreter state the lifter found for it,
        # and lift again; repeat while that uncovers more.
        base = list(stage.harness_options)
        allpokes = []
        for rnd in range(1, 41):
            new = [p for p in stage.poke_list() if p not in allpokes]
            if not new:
                break
            allpokes.extend(new)        # earlier probes are repeated in order: each run starts afresh
            pf = os.path.join(workdir, tag + '.poke')
            with open(pf, 'w') as f:
                f.write('\n'.join(allpokes) + '\n')
            log('[%s] round %d: %d more branch arms of the script did not run; probing %d in all' % (tag, rnd, len(new), len(allpokes)))
            stage.harness_options = base + ['poke=' + pf]
            text = stage.run_traced()
        if stage.errors:
            log('[%s] %d instructions could not be lifted (marked LIFT ERROR in the output)' % (tag, len(stage.errors)))
        return cleanup.tidy(text)
    text1 = stage.run()
    boot_text, info = bootstrap.patch(text1)
    if not info['fixkey']:
        log('[%s] no encrypted second stage found: the first program is the script' % tag)
        return cleanup.tidy(text1)
    data = run_bootstrap(stage, boot_text, workdir, tag, log)
    L, protos, overrides = solve_keyfns(stage.model, data, log)
    cuts = {}
    cut = opaque.prune_ir(protos, cuts)
    for _ in range(4):
        if not cuts:
            break
        # lift again without entering the junk arms: they patch instructions too, and applying
        # patches that never happen at run time damages the real code
        n0 = sum(len(t) for c in cuts.values() for t in c.values())
        L = make_lifter(stage.model, data, overrides)
        L.blocked = cuts
        protos = L.lift_all(L.root_key)
        cut += opaque.prune_ir(protos, cuts)
        if sum(len(t) for c in cuts.values() for t in c.values()) == n0:
            break
    if cut:
        log('[%s] %d opaque predicates resolved (junk branches removed)' % (tag, cut))
    errs = [(k, e) for k, p in protos.items() for e in p.errors]
    log('[%s] real program: %d functions, %d instructions, %d lift errors' % (
        tag, len(protos), sum(len(p.nodes) for p in protos.values()), len(errs)))
    for k, e in errs[:20]:
        log('    error in proto %s: %s' % (k, e))
    ircfg.UNFLATTEN = not oneline
    try:
        text, stats = decompile(stage, L, protos)
    finally:
        ircfg.UNFLATTEN = True
    return cleanup.tidy(text)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Luraph devirtualizer')
    ap.add_argument('input')
    ap.add_argument('-o', '--output')
    ap.add_argument('--work', default=None, help='directory for intermediate files')
    ap.add_argument('--mfp-delta', type=int, default=None, help='make math_floor_precision(seed) equal the script\'s own expected value plus this constant')
    ap.add_argument('--http-reply', default=None, help='what game:HttpGet returns (scripts that mix a server reply into their key)')
    ap.add_argument('--executor', action='store_true', help='provide executor globals (identifyexecutor, iscclosure, islclosure)')
    ap.add_argument('--root', type=int, default=None, help='proto number to lift from (1 = the first program)')
    ap.add_argument('--script-path', default=None,
                    help='where the script sits in the game, e.g. ReplicatedStorage.Packages.Net.init '
                         '(scripts check their own GetFullName())')
    ap.add_argument('--server', action='store_true', help='analyse the script as server code (RunService:IsServer() is true)')
    ap.add_argument('--no-exercise', action='store_true', help='do not call the script\'s uncalled functions after the run (the calls can make a script that stopped early scramble its code)')
    a = ap.parse_args(argv)
    work = a.work or os.path.join(os.path.dirname(os.path.abspath(a.input)), '.devm')
    text = deobfuscate(a.input, work, script_path=a.script_path, server=a.server, root=a.root, http_reply=a.http_reply, executor=a.executor, mfp_delta=a.mfp_delta, exercise=not a.no_exercise)
    out = a.output or os.path.splitext(a.input)[0] + '.deobf.lua'
    with open(out, 'w', encoding='utf-8') as f:
        f.write(text)
    print('written', out, '(%d lines)' % text.count('\n'))


if __name__ == '__main__':
    main()
