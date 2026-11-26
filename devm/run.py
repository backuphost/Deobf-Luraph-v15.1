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


class TimeLimit(BaseException):
    """Raised by time_limit() (not an Exception: evaluator code that treats any Exception as a
    wrong guess must not swallow it)."""


class time_limit:
    """`with time_limit(seconds):` raises TimeLimit in the block when it runs longer (main thread,
    POSIX; elsewhere no limit)."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.old = None

    def _fire(self, *_):
        raise TimeLimit('more than %d s' % self.seconds)

    def __enter__(self):
        import signal
        import threading
        if self.seconds and hasattr(signal, 'SIGALRM') and threading.current_thread() is threading.main_thread():
            self.old = signal.signal(signal.SIGALRM, self._fire)
            signal.alarm(int(self.seconds))
        return self

    def __exit__(self, *exc):
        import signal
        if self.old is not None:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, self.old)
        return False


class DevmError(Exception):
    """An expected way for a run to end without a result: main() prints `devm: <stage>: <reason>`."""

    def __init__(self, stage, reason):
        Exception.__init__(self, '%s: %s' % (stage, reason))
        self.stage = stage
        self.reason = reason


def deobfuscate(path, workdir, log=print, script_path=None, server=False, root=None, http_reply=None, executor=None, mfp_delta=None, exercise=True, http_map=None, chunk_arg=None, mark_untaken=False, route='auto', budget=None, run_timeout=None, second_stage_limit=90):
    os.makedirs(workdir, exist_ok=True)
    LAST_RUN['notes'] = []
    tag = os.path.splitext(os.path.basename(path))[0]
    with open(path, encoding='latin-1') as f:
        src = f.read()
    # Luraph's compressed container: the VM chunk is data that the shipped file decompresses and
    # loadstrings.  The model is built from the chunk; what is executed is the shipped file, with
    # the instrumented chunk compiled in place of the original (see container.py).
    carrier = None
    unopened = None
    if chunk_arg:
        carrier = container.stand_in(path, chunk_arg, workdir, tag)
        log('[%s] VM chunk with its data buffer (%s): run through a stand-in for Luraph\'s container' % (tag, chunk_arg))
    elif container.may_be_container(src):
        probe = []

        def probe_log(line):
            probe.append(line)
            log(line)
        carrier = container.unwrap(path, workdir, tag, probe_log)
        if carrier:
            with open(carrier.chunk_src, encoding='latin-1') as f:
                src = f.read()
        else:
            # the text holds no dispatch loop and no chunk came out of it: say that, not just "no model"
            unopened = '; '.join(re.sub(r'^\[[^\]]*\] ', '', ln) for ln in probe[-2:]) or 'the container probe saw no chunk being loaded'
    oneline = normalize.needs_reflow(src)
    if executor is None:
        # A script in Luraph's own one-line layout is what an executor is handed, and it expects
        # the executor's functions.  A decompiler's rendering comes out of a game, whose scripts
        # look for those functions to detect exploits.
        executor = oneline
        log('[%s] environment: %s (override with --executor / --no-executor)' % (
            tag, 'executor' if executor else 'plain Roblox client'))
    run_src = None
    removed = []
    if oneline:
        # every spelling of a dispatch-loop head becomes the canonical one (normalize.normalise_heads)
        src, heads, rewritten = normalize.normalise_heads(src)
        if rewritten:
            log('[%s] %d of %d dispatch-loop heads normalised' % (tag, rewritten, heads))
        run_src = src
        src, n, removed = normalize.inline_protected_raw_info(src)
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
    try:
        stage = pipeline.Stage(src, workdir, tag, log=log, oneline=True)
    except RuntimeError as e:
        if unopened is not None and 'no Luraph dispatch loop' in str(e):
            raise DevmError('no model', 'no dispatch loop in the file itself, and the VM chunk it would load at run time was not obtained (%s)' % unopened)
        raise
    if run_src is not None and len(stage.model.chain) >= 2 and not stage.model.factory_style:
        # closure makers that redeclare a name of the loader (Luraph v14.4): see normalize.unshadow
        shadows = stage.model.shadowed_maker_names()
        if shadows:
            gone = set(removed)
            keep = [i for i in range(len(stage.model.funcs_by_id) + len(removed)) if i not in gone]
            new_src, k = normalize.unshadow(run_src, [(keep[fid], names) for fid, names in shadows])
            if k:
                log('[%s] %d local(s) of the closure maker redeclare a name of the loader (%s): renamed' % (
                    tag, k, ', '.join(x for _f, names in shadows for x in names)))
                run_src = new_src
                src, n, removed = normalize.inline_protected_raw_info(run_src)
                stage = pipeline.Stage(normalize.reflow(src), workdir, tag, log=log, oneline=True)
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
        # (v15.2 forms of table-state `for` loops and of repeated key checks, see ircfg.V152)
        ircfg.V152 = structure.NOTE_TRAPS = stage.model.keyed_wrappers
        structure.MARK_COLD = ircfg.KEEP_COLD = bool(mark_untaken)
        try:
            text = stage.run_traced()
        finally:
            ircfg.UNFLATTEN = True
            ircfg.DROP_OPAQUE = False
            structure.FIT_SCOPES = False
            structure.NEST_ITEMS = beautify.MERGE_ARMS = False
            ircfg.V152 = structure.NOTE_TRAPS = False
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
        text = name_vm_object(cleanup.tidy(text))
        if stage.errors:
            # (not every such instruction is in the result: code behind a cut opaque test or
            # behind a tamper response is not printed, and neither are its markers)
            shown = text.count('LIFT ERROR')
            log('[%s] %d instructions could not be lifted: %d marked LIFT ERROR in the output%s' % (
                tag, len(stage.errors), shown,
                ', the others in code the output does not contain (cut as unreachable)' if shown < len(stage.errors) else ''))
        return text
    # ---- proto-table layout (every Luraph v14 build)
    common = (['server=1'] if server else []) + (['scriptpath=' + script_path] if script_path else []) + \
        (['httpreply=' + http_reply] if http_reply else []) + (['httpmap=' + http_map] if http_map else []) + \
        (['executor=1'] if executor else []) + (['mfpdelta=%d' % mfp_delta] if mfp_delta is not None else [])
    # The copy that is executed keeps `pcall(function() <dispatch loop> end)`: inits compare the
    # stack frames they see (debug.info(level, "f")) with the VM's own closures, and the inlined
    # copy does not pass.  The lifter's model is the inlined text; both number their functions in
    # source order, the inlined one without the dissolved closures.
    stage.run_model = stage.model
    stage.fid_map = None
    if removed and run_src is not None:
        rm = vmmodel.Model(normalize.reflow(run_src), oneline=True)
        gone = set(removed)
        keep = [i for i in range(len(rm.funcs_by_id)) if i not in gone]
        if len(keep) == len(stage.model.funcs_by_id):
            rm.run_options = stage.model.run_options
            stage.run_model = rm
            stage.fid_map = {a: b for b, a in enumerate(keep)}
        else:
            log('[%s] the text with and without the protected calls do not line up (%d functions against %d): '
                'the inlined copy is executed' % (tag, len(keep), len(stage.model.funcs_by_id)))
    first = stage.run_model.first_program_call()
    handover = None
    if first and root is None and route in ('auto', 'a'):
        # Route A.  The loader runs a first program and wraps what it returns: that program is
        # Luraph's own init, the returned proto is the script.  Let the VM run its init in the
        # harness and lift from the state at the moment the script's proto is entered.
        data = stage.dump_script(common, **{k: v for k, v in (('budget', budget), ('timeout', run_timeout)) if v})
        LAST_RUN['notes'] = list(getattr(stage, 'run_notes', ()))
        if data is not None:
            text = lift_program(stage, data, tag, log, oneline, 'script')
            if route == 'a' or not stage.errors:
                return text
            # The lift has errors.  Builds whose init decrypts the script data with a key it
            # derives (Luraph v14.8) patch their own operands with values of key functions; the
            # second-stage route recovers those from a native replay of the lifted init.  Try it
            # (time-boxed) and keep whichever result has fewer unlifted instructions.
            a_errs, a_nodes = len(stage.errors), sum(len(p.nodes) for p in stage.protos.values())
            try:
                with time_limit(second_stage_limit):
                    text1 = stage.run()
                    boot_text, info = bootstrap.patch(text1)
                    if info['fixkey']:
                        log('[%s] the init holds an encrypted second stage: lifting it through the native replay as well' % tag)
                        data = run_bootstrap(stage, boot_text, workdir, tag, log)
                        text2 = lift_program(stage, data, tag, log, oneline, 'real program')
                        b_errs, b_nodes = len(stage.errors), sum(len(p.nodes) for p in stage.protos.values())
                        if b_errs < a_errs and b_nodes >= a_nodes:
                            log('[%s] result of the second-stage route kept: %d instructions with %d lift errors, against %d with %d '
                                'from the state at the hand-over' % (tag, b_nodes, b_errs, a_nodes, a_errs))
                            LAST_RUN['notes'] = [n for n in LAST_RUN['notes'] if n.startswith('[http]')]
                            return text2
                        log('[%s] result from the state at the hand-over kept (%d instructions, %d lift errors; second-stage '
                            'route: %d, %d)' % (tag, a_nodes, a_errs, b_nodes, b_errs))
            except (pe.PEError, RuntimeError, bootstrap.BootstrapError, subprocess.TimeoutExpired, TimeLimit) as e:
                log('[%s] second-stage route not usable (%s): result from the state at the hand-over kept' % (
                    tag, (type(e).__name__ + ' ' + str(e)).strip().split('\n')[0][:160]))
            return text
        handover = '; '.join(n for n in stage.run_notes if not n.startswith('[http]'))[-600:] or 'the run ended without entering the script'
        log('[%s] the VM\'s init did not hand over to the script (%s)' % (tag, handover))
        if route == 'a':
            raise DevmError('init did not hand over', handover)
        log('[%s] trying the second-stage route: lift the init, run it natively, recover the key' % tag)
    try:
        text1 = stage.run()
        boot_text, info = bootstrap.patch(text1)
    except (pe.PEError, RuntimeError, bootstrap.BootstrapError) as e:
        if handover is None:
            raise
        raise DevmError('init did not hand over', '%s; second-stage route: %s' % (handover, str(e).strip().split('\n')[0][:200]))
    if not info['fixkey']:
        if first:
            # never the init as "the script"
            raise DevmError('init did not hand over', '%s; the lifted init has no encrypted second stage to recover a key for' % (
                handover or 'route A was not tried'))
        log('[%s] the loader runs a single root program and it holds no encrypted second stage: that program is the script' % tag)
        return cleanup.tidy(text1)
    data = run_bootstrap(stage, boot_text, workdir, tag, log)
    return lift_program(stage, data, tag, log, oneline, 'real program')


def lift_program(stage, data, tag, log, oneline, what):
    """Lift and decompile the program a dump was taken at the start of (proto-table layout):
    the dump of the native bootstrap replay, or the one taken when the VM's own init handed over."""
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
    log('[%s] %s: %d functions, %d instructions, %d lift errors' % (
        tag, what, len(protos), sum(len(p.nodes) for p in protos.values()), len(errs)))
    for k, e in errs[:20]:
        log('    error in proto %s: %s' % (k, e))
    kinds = {}
    for _k, e in errs:
        kind = re.sub(r'\d+', 'N', str(e[1]))[:110]
        kinds[kind] = kinds.get(kind, 0) + 1
    for kind, c in sorted(kinds.items(), key=lambda kv: -kv[1])[:15]:
        log('    lift errors by kind: %4d x %s' % (c, kind))
    stage.protos, stage.errors = protos, errs
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
    ap.add_argument('--route', choices=('auto', 'a', 'b'), default='auto',
                    help='Luraph v14 builds: a = let the VM run its own init and lift the script from the state at the '
                         'hand-over; b = lift the init, run it natively and recover the key of the second stage; '
                         'auto = a, then b when the init does not hand over')
    ap.add_argument('--budget', type=int, default=None, help='route a: VM instructions the init may run before it is stopped')
    ap.add_argument('--run-timeout', type=int, default=None, help='route a: seconds the run may take')
    a = ap.parse_args(argv)
    work = a.work or os.path.join(os.path.dirname(os.path.abspath(a.input)), '.devm')
    try:
        text = deobfuscate(a.input, work, script_path=a.script_path, server=a.server, root=a.root, http_reply=a.http_reply, executor=a.executor, mfp_delta=a.mfp_delta, exercise=not a.no_exercise, http_map=a.http_map, chunk_arg=a.chunk_arg, mark_untaken=a.mark_untaken, route=a.route, budget=a.budget, run_timeout=a.run_timeout)
    except DevmError as e:
        print('devm: %s: %s' % (e.stage, e.reason))
        sys.exit(1)
    except (pe.PEError, RuntimeError, bootstrap.BootstrapError, SyntaxError, subprocess.TimeoutExpired) as e:
        print('devm: %s: %s' % (failure_stage(e), ' '.join(str(e).split())[:400]))
        sys.exit(1)
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


def failure_stage(e):
    """The stage name of the one-line failure message for an exception that is an expected way to fail."""
    msg = str(e)
    if isinstance(e, SyntaxError):
        return 'unsupported target' if re.search(r'bad token|unexpected', msg) else 'damaged input'
    if isinstance(e, subprocess.TimeoutExpired):
        return 'run failed'
    if 'no Luraph dispatch loop' in msg or 'no Luraph interpreter' in msg or 'cannot locate the dispatch loops' in msg:
        return 'no model'
    if msg.startswith(('dump failed', 'oracle run failed', 'bootstrap did not reach')):
        return 'run failed'
    return 'lift failed'


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
    for n in notes:
        m = re.match(r'\[dumpnew\] the script starts at interpreter entry (\d+) \((\d+) VM instructions', n)
        if m:
            out.append('the VM ran its own init (%s VM instructions) and handed over to the script; this result is a static '
                       'lift of the script\'s program from the state at that moment: the script itself did not run' % m.group(2))
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
    # (`(nil)[k]`, `(nil)(...)`, `t[nil]`, `nil .. x`; a call that passes nil, `f(nil)`, is ordinary code)
    blank = len(re.findall(r'(?m)^.*(?:\[nil\]|(?<![\w\)\]])\(nil\)[\[.(:]|= nil \.\.).*$', text))
    # (comment lines are not code: a LIFT ERROR marker can name the VM table it could not write
    # to, and the note of name_vm_object names the table it explains)
    code = '\n'.join(ln for ln in lines if not ln.lstrip().startswith('--'))
    refs = code.count('__LPH_PROTOS')
    vm = len(re.findall(r'\b%s\w*' % VM_NAME, code))
    flat = text.count('not structured')
    flags = len(re.findall(r'exit\d+ = true', text))
    # placeholders the printer writes where it has no value or text to show
    ups = sum(1 for ln in lines if re.search(r'\bUPVAL_\d', ln))
    holes = len(re.findall(r'nil --\[\[', code))
    stubs = code.count('--[[ native function L') + code.count('--[[ UNSTRUCTURED')
    # values the lifter could only name by where the dump holds them, and arguments / registers
    # it could not connect to a variable
    envrefs = code.count('__LPH_ENV[')
    regs = len(re.findall(r'\b(?:ARG|REG)_\d+\b', code))
    preps = sum(1 for ln in lines if '-- for-loop setup:' in ln)
    # Luraph's tamper response (structure.TRAP_NOTE labels the function): its calls mark the
    # protection's own checks, which a reader should not take for the script's
    traps = []
    for m in re.finditer(r'(?m)^[ \t]*(?:local )?(?:function )?([A-Za-z_]\w*)(?: = function)?\(\)\n[ \t]*-- %s'
                         % re.escape(structure.TRAP_NOTE), text):
        calls = len(re.findall(r'(?m)^[ \t]*%s\(\)$' % re.escape(m.group(1)), code))
        traps.append('  note: %d calls of %s, Luraph\'s tamper response: the tests in front of them are checks of the '
                     'protection, not of the script' % (calls, m.group(1)))
    lines = max(1, text.count('\n'))
    out = []
    if not (errs or blank or refs or flat or flags or vm or ups or holes or stubs or preps or envrefs or regs):
        out.append('result: complete (no unlifted instructions, no missing constants, all control flow structured)')
        return out + traps
    out.append('result: PARTIAL')
    if errs:
        out.append('  %d LIFT ERROR markers: an instruction could not be lifted; the code after it is missing' % errs)
    if blank:
        out.append('  %d lines index or call nil: usually a constant in code that never ran and stayed encrypted '
                   '(a script can also do it on purpose: `t[nil]` reaches an __index handler)' % blank)
    if refs:
        out.append('  %d constants shown as __LPH_PROTOS[...]: a value the dump could not describe' % refs)
    if envrefs:
        out.append('  %d values shown as __LPH_ENV[...]: a value of Luraph\'s loader that the code uses and the lift could not name' % envrefs)
    if regs:
        out.append('  %d uses of ARG_n / REG_n: an argument or register the lifter could not connect to a variable' % regs)
    if vm:
        out.append('  %d uses of %s: the table that holds Luraph\'s interpreter, which the protected code also '
                   'uses as storage; the table itself is not in the result' % (vm, VM_NAME))
    if flat or flags:
        out.append('  control flow not fully structured: %d state machines, %d exit flags' % (flat, flags))
    if ups:
        out.append('  %d lines use UPVAL_n: a variable a function captured; which one was not recovered '
                   '(usually the code that creates the function never ran)' % ups)
    if holes:
        out.append('  %d values printed as nil --[[...]]: a loop variable, argument or operand the lifter '
                   'could not connect to its source' % holes)
    if preps:
        out.append('  %d for loops without their loop statement (only the set-up of the bounds was found)' % preps)
    if stubs:
        out.append('  %d functions printed as a stub (their body was not recovered)' % stubs)
    depth = max((len(ln) - len(ln.lstrip('\t')) for ln in text.split('\n')), default=0)
    if depth > 60:
        out.append('  nesting up to %d levels deep: a flattened function came out as a chain of nested blocks, '
                   'not as its original structure' % depth)
    if blank * 5 > lines:
        out.append('  most of the script did not run.  Usual causes: it waits for a server reply (see the [http] '
                   'lines above; --http-map), a key system, or an environment check that failed '
                   '(try --no-executor / --executor)')
    return out + traps


if __name__ == '__main__':
    main()
