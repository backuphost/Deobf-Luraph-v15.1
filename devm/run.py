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
import re
import subprocess
import sys

from . import beautify, bootstrap, cleanup, container, ircfg, lifter, normalize, opaque, pe, pipeline, structure, unidata, vmmodel

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


def deobfuscate(path, workdir, log=print, script_path=None, server=False, root=None, http_reply=None, executor=None, mfp_delta=None, exercise=True, http_map=None, chunk_arg=None, mark_untaken=False):
    os.makedirs(workdir, exist_ok=True)
    tag = os.path.splitext(os.path.basename(path))[0]
    with open(path, encoding='latin-1') as f:
        src = f.read()
    # Luraph's compressed container: the VM chunk is data that the shipped file decompresses and
    # loadstrings.  The model is built from the chunk; what is executed is the shipped file, with
    # the instrumented chunk compiled in place of the original (see container.py).
    carrier = None
    if chunk_arg:
        carrier = container.stand_in(path, chunk_arg, workdir, tag)
        log('[%s] VM chunk with its data buffer (%s): run through a stand-in for Luraph\'s container' % (tag, chunk_arg))
    elif container.may_be_container(src):
        carrier = container.unwrap(path, workdir, tag, log)
        if carrier:
            with open(carrier.chunk_src, encoding='latin-1') as f:
                src = f.read()
    oneline = normalize.needs_reflow(src)
    if executor is None:
        # A script in Luraph's own one-line layout is what an executor is handed, and it expects
        # the executor's functions.  A decompiler's rendering comes out of a game, whose scripts
        # look for those functions to detect exploits.
        executor = oneline
        log('[%s] environment: %s (override with --executor / --no-executor)' % (
            tag, 'executor' if executor else 'plain Roblox client'))
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
    stage.model.run_options = carrier.harness_options() if carrier else []
    stage.carrier = carrier
    if stage.model.factory_style:
        # self-decrypting builds: lift the VM state at the end of a traced run (see Stage.run_traced).
        # exercise=1: functions of the script that nobody called are called once, so that their
        # code decrypts itself too.
        stage.harness_options = (['exercise=1'] if exercise else ['binds=1']) + (['server=1'] if server else []) + (['scriptpath=' + script_path] if script_path else []) + (['httpreply=' + http_reply] if http_reply else []) + (['httpmap=' + http_map] if http_map else []) + (['executor=1'] if executor else []) + (['mfpdelta=%d' % mfp_delta] if mfp_delta is not None else [])
        # (a script in its shipped layout keeps its source's control flow: nothing to unflatten.
        # Luraph v15.2 does flatten: its start-up code and the script's main function share one
        # state machine, see README "Luraph v15.2")
        ircfg.UNFLATTEN = not oneline or stage.model.keyed_wrappers
        ircfg.DROP_OPAQUE = oneline
        # (... and that function has several hundred locals once it is unflattened)
        structure.FIT_SCOPES = stage.model.keyed_wrappers
        # (layout and and/or rewrites that came with v15.2 support; the committed v15.1 results
        # were written without them and tests/regress.py compares text for text)
        structure.NEST_ITEMS = beautify.MERGE_ARMS = stage.model.keyed_wrappers
        structure.MARK_COLD = ircfg.KEEP_COLD = bool(mark_untaken)
        try:
            text = stage.run_traced()
        finally:
            ircfg.UNFLATTEN = True
            ircfg.DROP_OPAQUE = False
            structure.FIT_SCOPES = False
            structure.NEST_ITEMS = beautify.MERGE_ARMS = False
            structure.MARK_COLD = ircfg.KEEP_COLD = False
        LAST_RUN['notes'] = list(getattr(stage, 'run_notes', ())) + list(getattr(stage, 'hints', ()))
        # Paths that never ran keep encrypted instructions and constants.  With the traced run
        # kept alive (one-line builds) the lifter replays the decryption itself and asks the run
        # for constants as it goes.  Otherwise: execute each lifted instruction that did not run
        # once, from the interpreter state the lifter found for it, and lift again; repeat while
        # that uncovers more.
        base = list(stage.harness_options)
        allpokes = []
        for rnd in range(1, 41 if vmmodel.SERVER is None else 0):
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
        return name_vm_object(cleanup.tidy(text))
    text1 = stage.run()
    boot_text, info = bootstrap.patch(text1)
    if not info['fixkey']:
        log('[%s] no encrypted second stage found: the first program is the script' % tag)
        return cleanup.tidy(text1)
    data = run_bootstrap(stage, boot_text, workdir, tag, log)
    # First with the key function values the bootstrap replay produced.  Junk arms behind opaque
    # predicates do not lift cleanly and must not be mistaken for wrong keys, so they are cut
    # before anything is solved for.
    overrides = {}
    L = make_lifter(stage.model, data, overrides)
    protos = L.lift_all(L.root_key)
    cuts = {}
    cut = opaque.prune_ir(protos, cuts)
    if any(p.errors and p.keyfns for p in protos.values()):
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


VM_OBJECT = re.compile(r'__LPH_PROTOS\[(\d+)\]\["self"\]')
VM_NAME = 'LuraphVM'


def name_vm_object(text):
    """The table that holds Luraph's interpreter is also used by the protected code: start-up code
    stores helper functions and probe results in its fields and reads them back.  The dump names it
    by where it was found (`__LPH_PROTOS[n]["self"]`: the first factory argument of proto n); the
    result calls it LuraphVM and says so in a comment."""
    ids = sorted({int(m.group(1)) for m in VM_OBJECT.finditer(text)})
    if not ids:
        return text
    base = VM_NAME
    while re.search(r'\b%s\w*' % base, text):
        base += '_'         # (a name the script uses itself)
    names = {n: base + (str(i + 1) if i else '') for i, n in enumerate(ids)}
    text = VM_OBJECT.sub(lambda m: names[int(m.group(1))], text)
    text = re.sub(r'\b(%s\d*)\["([A-Za-z_]\w*)"\]' % base,
                  lambda m: m.group(1) + '.' + m.group(2) if m.group(2) not in structure.KEYWORDS else m.group(0), text)
    note = ('-- %s: the table that holds Luraph\'s interpreter.  The protected code keeps helper functions\n'
            '-- and values of its own in fields of that table; the table itself is not part of this file.\n' % ', '.join(names[n] for n in ids))
    return note + text


def main(argv=None):
    ap = argparse.ArgumentParser(description='Luraph devirtualizer')
    ap.add_argument('input')
    ap.add_argument('-o', '--output')
    ap.add_argument('--work', default=None, help='directory for intermediate files')
    ap.add_argument('--mfp-delta', type=int, default=None, help='make math_floor_precision(seed) equal the script\'s own expected value plus this constant')
    ap.add_argument('--http-reply', default=None, help='what game:HttpGet returns (scripts that mix a server reply into their key)')
    ap.add_argument('--http-map', default=None, help='file of <url substring><TAB><reply body> lines: canned replies for the offline request mock')
    ap.add_argument('--executor', action='store_true', default=None,
                    help='run the script as an executor would (getgenv, request, hookfunction, ... exist). '
                         'Default: yes for scripts in Luraph\'s own one-line layout, no for decompiled ones')
    ap.add_argument('--no-executor', dest='executor', action='store_false', help='run the script as a plain Roblox client')
    ap.add_argument('--root', type=int, default=None, help='proto number to lift from (1 = the first program)')
    ap.add_argument('--script-path', default=None,
                    help='where the script sits in the game, e.g. ReplicatedStorage.Packages.Net.init '
                         '(scripts check their own GetFullName())')
    ap.add_argument('--server', action='store_true', help='analyse the script as server code (RunService:IsServer() is true)')
    ap.add_argument('--no-exercise', action='store_true', help='do not call the script\'s uncalled functions after the run (the calls can make a script that stopped early scramble its code)')
    ap.add_argument('--mark-untaken', action='store_true',
                    help='put a comment where a branch arm starts that the traced run never took: what follows '
                         'was lifted without the run to check it against')
    ap.add_argument('--chunk-arg', default=None, metavar='FILE',
                    help='the input is a VM chunk cut out of a Luraph container (`return setmetatable({...},{}):NAME(...)`); '
                         'FILE holds the bytes of the data buffer the container calls it with')
    a = ap.parse_args(argv)
    work = a.work or os.path.join(os.path.dirname(os.path.abspath(a.input)), '.devm')
    text = deobfuscate(a.input, work, script_path=a.script_path, server=a.server, root=a.root, http_reply=a.http_reply, executor=a.executor, mfp_delta=a.mfp_delta, exercise=not a.no_exercise, http_map=a.http_map, chunk_arg=a.chunk_arg, mark_untaken=a.mark_untaken)
    out = a.output or os.path.splitext(a.input)[0] + '.deobf.lua'
    lines = run_report(LAST_RUN['notes']) + report(text)
    # the report is also the head of the file: a result read later, without the run's output next
    # to it, must still say what it is and what it lacks
    head = ['-- %s, %s.  How the run went and what this file lacks:' % (os.path.basename(a.input), HEAD_MARK)]
    head += ['--   ' + ln for ln in lines]
    text = '\n'.join(head) + '\n' + text
    with open(out, 'w', encoding='utf-8') as f:
        f.write(text)
    print('written', out, '(%d lines)' % text.count('\n'))
    for line in lines:
        print(line)


HEAD_MARK = 'recovered by devm (python -m devm.run)'      # first line of a file main() writes
LAST_RUN = {'notes': []}       # what the traced run of the last deobfuscate() call reported


def run_report(notes):
    """How the traced run ended, for the final report: the lift only knows what that run reached."""
    import re
    out = []
    for n in notes:
        m = re.match(r'\[oracle\] script (returned|stopped: .*?) after (\d+) instructions', n)
        if m:
            out.append('traced run: the script %s (%s VM instructions)' % (
                'returned' if m.group(1) == 'returned' else 'stopped with "%s"' % m.group(1)[len('stopped: '):], m.group(2)))
    reqs = [n for n in notes if n.startswith('[http]')]
    for n in reqs[:6]:
        out.append('  request: ' + re.sub(r' \[step \d+\]$', '', n[len('[http] '):]))
    if any('no network' in n for n in reqs):
        out.append('  the script asked a server and got no reply (nothing is sent, replies are not forged): whatever '
                   'depends on the answer (a key check, a later stage) did not run and is not in the result')
    for n in notes:
        if n.startswith('[env]'):
            out.append('  ' + n[len('[env] '):])
        elif n.startswith('[hint]'):
            out.append('  ' + n[len('[hint] '):])
    return out


def report(text):
    """What is left unrecovered in a result, in plain words."""
    lines = text.split('\n')
    if lines and HEAD_MARK in lines[0]:
        # a file main() wrote: its head is an earlier report, not part of the result
        del lines[0]
        while lines and lines[0].startswith('--   '):
            del lines[0]
        text = '\n'.join(lines)
    errs = text.count('LIFT ERROR')
    blank = len(re.findall(r'(?m)^.*(?:\[nil\]|\(nil\)|= nil \.\.).*$', text))
    # (comment lines are not code: a LIFT ERROR marker can name the VM table it could not write
    # to, and the note of name_vm_object names the table it explains)
    code = '\n'.join(ln for ln in lines if not ln.lstrip().startswith('--'))
    refs = code.count('__LPH_PROTOS')
    vm = len(re.findall(r'\b%s\w*' % VM_NAME, code))
    flat = text.count('not structured')
    flags = len(re.findall(r'exit\d+ = true', text))
    lines = max(1, text.count('\n'))
    out = []
    if not (errs or blank or refs or flat or flags or vm):
        out.append('result: complete (no unlifted instructions, no missing constants, all control flow structured)')
        return out
    out.append('result: PARTIAL')
    if errs:
        out.append('  %d LIFT ERROR markers: an instruction could not be lifted; the code after it is missing' % errs)
    if blank:
        out.append('  %d lines index or call nil: usually a constant in code that never ran and stayed encrypted '
                   '(a script can also do it on purpose: `t[nil]` reaches an __index handler)' % blank)
    if refs:
        out.append('  %d constants shown as __LPH_PROTOS[...]: a value the dump could not describe' % refs)
    if vm:
        out.append('  %d uses of %s: the table that holds Luraph\'s interpreter, which the protected code also '
                   'uses as storage; the table itself is not in the result' % (vm, VM_NAME))
    if flat or flags:
        out.append('  control flow not fully structured: %d state machines, %d exit flags' % (flat, flags))
    depth = max((len(ln) - len(ln.lstrip('\t')) for ln in text.split('\n')), default=0)
    if depth > 60:
        out.append('  nesting up to %d levels deep: a flattened function came out as a chain of nested blocks, '
                   'not as its original structure' % depth)
    if blank * 5 > lines:
        out.append('  most of the script did not run.  Usual causes: it waits for a server reply (see the [http] '
                   'lines above; --http-map), a key system, or an environment check that failed '
                   '(try --no-executor / --executor)')
    return out


if __name__ == '__main__':
    main()
