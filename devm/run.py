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


def unknown_keyfns(data):
    """Keys of the key functions a dump holds without a value (`{"cfn": null, "k": K}`: the
    harness could not evaluate the function)."""
    out = set()
    stack = [data]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            if 'cfn' in v and 'k' in v:
                if v['cfn'] is None:
                    out.add(v['k'])
                continue
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return out


def solve_keyfns(model, data, log, only=None):
    """Lift everything; search values for key functions until no proto has lift errors.
    only: the key functions to search values for (default: all a proto with errors uses)."""
    overrides = {}
    L = make_lifter(model, data, overrides)
    protos = L.lift_all(L.root_key)
    for _ in range(6):
        bad = [k for k, p in protos.items() if p.errors and p.keyfns and (only is None or p.keyfns & only)]
        if not bad:
            break
        changed = False
        for key in bad:
            base = len(protos[key].errors)
            for fn in sorted(protos[key].keyfns, key=str):
                if only is not None and fn not in only:
                    continue
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


def run_bootstrap(model_stage, boot_text, workdir, tag, log, options=()):
    """Run the lifted init natively in the harness, in place of the VM's first program, and dump
    the state when the real program starts.  options: the harness options of the run (numeric
    ones in front of the layer file, see harness.lua); the options of a container (its carrier,
    the chunk text and the data it is called with) come from the model."""
    boot = os.path.join(workdir, tag + '.boot.lua')
    with open(boot, 'w') as f:
        f.write(boot_text)
    nf = os.path.join(workdir, 'nfdata.lua')
    if not os.path.exists(nf):
        unidata.generate(nf)
    out = os.path.join(workdir, tag + '.stage2.json')
    if os.path.exists(out):
        os.remove(out)
    numeric = [o for o in options if re.match(r'^\w+=\d+$', o)]
    named = [o for o in options if o not in numeric] + list(getattr(model_stage.model, 'run_options', ()))
    try:
        r = subprocess.run([vmmodel.find_luau(), os.path.join(HERE, 'harness.lua'), '-a', model_stage.patched_path, out] + numeric + [boot] + named,
                           capture_output=True, text=True, errors='replace', timeout=900)
    except subprocess.TimeoutExpired:
        raise RuntimeError('bootstrap did not reach the real program: the native replay of the init did not end within 900 s')
    failed = 0
    for line in r.stderr.split('\n'):
        if line.startswith('[bootstrap] failed environment check'):
            failed += 1
        elif line.startswith('[fixkey]') or line.startswith('[keyfn]'):
            log(line)
    log('[bootstrap] %d environment checks answered differently from Roblox (neutralised)' % failed)
    if not os.path.exists(out):
        # (the first lines of the harness's output say why; its last lines are the stack trace)
        why = [l for l in r.stderr.split('\n') if l.strip() and not l.startswith(('[fixkey]', '[keyfn]', 'stacktrace', '[C]'))]
        raise RuntimeError('bootstrap did not reach the real program: ' + '; '.join(why[:3])[:600])
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


class Terminated(BaseException):
    """devm was told to stop (SIGTERM / SIGHUP).  Raised in the main thread so that the run unwinds:
    every place that waits for the harness kills it on the way out."""


def stop_on_signals():
    import signal
    import threading
    if threading.current_thread() is not threading.main_thread():
        return

    def fire(signum, _frame):
        raise Terminated(signum)
    for name in ('SIGTERM', 'SIGHUP'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), fire)


class DevmError(Exception):
    """An expected way for a run to end without a result: main() prints `devm: <stage>: <reason>`."""

    def __init__(self, stage, reason):
        Exception.__init__(self, '%s: %s' % (stage, reason))
        self.stage = stage
        self.reason = reason


def method_wrap_layout(model):
    """Luraph v15.0: the VM object is `setmetatable({...}, {})` and the function that makes a VM
    closure is one of its methods, written as a state machine that assigns the interpreter closure
    in one of its states (`while true do if S <= 0 then ...; S, f = 1, function(...) ... end`)
    instead of returning it from a wrapper.  Neither of the two layouts devm models."""
    if model.factory_style or len(model.chain) != 2 or not re.match(r'(?:\s|--[^\n]*\n)*return\s*\(?\s*setmetatable\b', model.src):
        return False
    maker = model.chain[0]
    if maker.body and maker.body[-1][0] == 'return' and maker.body[-1][1] and maker.body[-1][1][0][0] == 'func':
        return False

    def holds(node):
        if isinstance(node, (list, tuple)):
            if len(node) == 2 and node[0] == 'func' and node[1] is model.interp:
                return True
            return any(holds(x) for x in node if isinstance(x, (list, tuple)))
        return False
    return any(st[0] == 'while' and holds(st[2]) for st in maker.body)


def v152_generation(model):
    """Builds of the v15.2 generation: start-up code and (in some builds) the script's top level in
    one flattened main function behind trampolines, tamper traps, numeric loops kept in tables.
    v15.2 itself is known by its wrapper keys.  The v15.1 builds of this generation have none; they
    are known by the interpreter that runs their script, whose dispatch tree computes every test
    into a local first (vmmodel._outlined_test).  Earlier v15.1 builds have neither."""
    if model.keyed_wrappers:
        return True
    return any(l[2][0][0] == 'local' and vmmodel._outlined_test(l[2], l[2][0]) for l in model.loops)


def deobfuscate(path, workdir, log=print, script_path=None, server=False, root=None, http_reply=None, executor=None, mfp_delta=None, exercise=True, http_map=None, chunk_arg=None, chunk_name=None, mark_untaken=False, route='auto', budget=None, run_timeout=None, second_stage_limit=90, preliminary=None, diagnose=True):
    os.makedirs(workdir, exist_ok=True)
    LAST_RUN['notes'] = []
    tag = os.path.splitext(os.path.basename(path))[0]
    with open(path, encoding='latin-1') as f:
        src = f.read()
    # builds for Lua 5.3/5.4 or LuaJIT are not Luau text: say so before the parser is tried
    why = normalize.foreign_dialect(src)
    if why:
        raise DevmError('unsupported target', why)
    # Luraph's compressed container: the VM chunk is data that the shipped file decompresses and
    # loadstrings.  The model is built from the chunk; what is executed is the shipped file, with
    # the instrumented chunk compiled in place of the original (see container.py).
    carrier = None
    unopened = None
    def read_static():
        """the container read without running it (the tree's static decoder, when it has one)"""
        try:
            c = container.open_static(path, src, workdir, tag, log)
        except container.Damaged as e:
            raise DevmError('damaged input', 'the file is a compressed Luraph container that cannot be opened: %s' % e)
        if c:
            with open(c.chunk_src, encoding='latin-1') as f:
                text = f.read()
            why = normalize.foreign_dialect(text)
            if why:
                raise DevmError('unsupported target', 'the VM chunk inside the container: ' + why)
            return c, text
        return None, src

    # Luraph v14's container is a function without parameters: read statically first, the
    # sandboxed probe (container.unwrap) is the fallback.  The v15 container is an object
    # (`return setmetatable({...},{}):M()(...)`): there the probe comes first, as it always has,
    # and the static decoder is asked when the probe did not get the chunk.
    fn_container = not chunk_arg and container.FN_SHAPE.search(src) is not None
    if chunk_arg:
        carrier = container.stand_in(path, chunk_arg, workdir, tag, chunk_name or 'Luraph')
        log('[%s] VM chunk with its data buffer (%s): run through a stand-in for Luraph\'s container' % (tag, chunk_arg))
    elif fn_container:
        carrier, src = read_static()
    if carrier is None and not chunk_arg and container.may_be_container(src):
        probe = []

        def probe_log(line):
            probe.append(line)
            log(line)
        carrier = container.unwrap(path, workdir, tag, probe_log)
        if carrier:
            with open(carrier.chunk_src, encoding='latin-1') as f:
                src = f.read()
            why = normalize.foreign_dialect(src)
            if why:
                raise DevmError('unsupported target', 'the VM chunk inside the container: ' + why)
        else:
            # the text holds no dispatch loop and no chunk came out of it: say that, not just "no model"
            unopened = '; '.join(re.sub(r'^\[[^\]]*\] ', '', ln) for ln in probe[-2:]) or 'the container probe saw no chunk being loaded'
            if not fn_container:
                carrier, src = read_static()
                if carrier:
                    unopened = None
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
        if 'no Luraph dispatch loop' in str(e):
            # what the text does have, for whoever reads the message: endless loops in vararg functions
            loops = len(re.findall(r'\bwhile\b|\brepeat\b', src))
            raise DevmError('no model', 'no Luraph dispatch loop found%s: none of the %d loops of the text fetches an instruction and '
                            'tests its opcode in a spelling devm knows (`local OP=CODE[PC]; if`, `local I=CODE[PC]; local OP=I[n]; '
                            'PC=PC+1; if`, in `while <true>`/`repeat ... until false`)' % (
                                ' in the VM chunk of the container' if carrier else '', loops))
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
    if run_timeout:
        stage.dump_timeout = run_timeout
    stage.model.run_options = carrier.harness_options() if carrier else []
    stage.carrier = carrier
    if method_wrap_layout(stage.model):
        # (the other engine lifts these; here the proto-table route would run for minutes and fail)
        raise DevmError('no model', 'Luraph v15.0 method-wrap layout is not supported by this engine')
    if stage.model.factory_style:
        # self-decrypting builds: lift the VM state at the end of a traced run (see Stage.run_traced).
        # exercise=1: functions of the script that nobody called are called once, so that their
        # code decrypts itself too.
        stage.harness_options = (['exercise=1'] if exercise else ['binds=1']) + (['server=1'] if server else []) + (['scriptpath=' + script_path] if script_path else []) + (['httpreply=' + http_reply] if http_reply else []) + (['httpmap=' + http_map] if http_map else []) + (['executor=1'] if executor else []) + (['mfpdelta=%d' % mfp_delta] if mfp_delta is not None else [])
        # (a script in its shipped layout keeps its source's control flow: nothing to unflatten.
        # Luraph v15.2 does flatten: its start-up code and the script's main function share one
        # state machine, see README "Luraph v15.2")
        gen152 = v152_generation(stage.model)
        ircfg.UNFLATTEN = not oneline or gen152
        ircfg.DROP_OPAQUE = oneline
        # (... and that function has several hundred locals once it is unflattened)
        structure.FIT_SCOPES = gen152
        # (layout and and/or rewrites that came with v15.2 support; the committed v15.1 results
        # were written without them and tests/regress.py compares text for text)
        structure.NEST_ITEMS = beautify.MERGE_ARMS = gen152
        # (v15.2 forms of table-state `for` loops and of repeated key checks, see ircfg.V152)
        ircfg.V152 = structure.NOTE_TRAPS = gen152
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
            head, cold = hidden_head(stage.errors, stage.oracle, text)
            if head:
                # Code that ran cannot have been unreachable: the result must not pass for complete
                # (the same head line as lift_program writes for the proto-table layout)
                text = head + '\n' + text
            if cold:
                # (where the head line is an upper bound, this count is the matching lower bound)
                LAST_RUN['notes'].append('[cut] %s%d instruction(s) that never ran did not lift; the result does not contain the code '
                                         'they are in (cut as never-executed junk or as unreachable)' % (
                                             'at least ' if head and ' up to ' in head else '', cold))
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
    # The second-stage route recovers the key of script data that the init decrypts in place.  Data
    # that can be rewritten in place is a buffer (Luraph v14.7 and later); a VM that reads its data
    # from a string has no such stage, and lifting its init to look for one is time spent for nothing.
    in_place = re.search(r'\bbuffer\b', stage.model.src) is not None
    single = not first
    if root is None and route in ('auto', 'a'):
        # Route A.  The loader runs a first program and wraps what it returns: that program is
        # Luraph's own init, the returned proto is the script.  Let the VM run its init in the
        # harness and lift from the state at the moment the script's proto is entered.
        # A loader that runs a single root program (Luraph v14.9) has the same start-up code in
        # that program; it builds the script's program and calls it itself.  There the dump is
        # taken when a program is entered that did not exist when the root program started.
        if single:
            log('[%s] the loader runs a single root program: looking for the program it builds and enters' % tag)
        data = stage.dump_script(common, **{k: v for k, v in (('budget', budget), ('timeout', run_timeout)) if v})
        LAST_RUN['notes'] = list(getattr(stage, 'run_notes', ()))
        if data is not None:
            text = lift_program(stage, data, tag, log, oneline, 'script', replayed=False)
            if route == 'a' or not stage.errors or not in_place or single or not unknown_keyfns(data):
                return text
            # The lift has errors, and the dump holds key functions the harness could not
            # evaluate.  Builds whose init decrypts the script data with a key it derives (Luraph
            # v14.8) patch their own operands with values of those functions; the second-stage
            # route searches the values after a native replay of the lifted init.  Try it
            # (time-boxed) and keep whichever result has fewer unlifted instructions.
            # (Lift errors with every key function known, or with none in the dump, have other
            # causes; the second stage lifts with the same lifter and would not remove them.)
            a_errs, a_nodes = len(stage.errors), sum(len(p.nodes) for p in stage.protos.values())
            if preliminary:
                preliminary(text)       # (the result so far is on disk should the second try be cut short)
            try:
                with time_limit(second_stage_limit):
                    text1 = stage.run()
                    boot_text, info = bootstrap.patch(text1)
                    if info['fixkey']:
                        log('[%s] the init holds an encrypted second stage: lifting it through the native replay as well' % tag)
                        data = run_bootstrap(stage, boot_text, workdir, tag, log, common)
                        text2 = lift_program(stage, data, tag, log, oneline, 'real program')
                        b_errs, b_nodes = len(stage.errors), sum(len(p.nodes) for p in stage.protos.values())
                        if b_errs < a_errs and b_nodes >= a_nodes:
                            log('[%s] result of the second-stage route kept: %d instructions with %d lift errors, against %d with %d '
                                'from the state at the hand-over' % (tag, b_nodes, b_errs, a_nodes, a_errs))
                            LAST_RUN['notes'] = [n for n in LAST_RUN['notes'] if n.startswith('[http]')]
                            return text2
                        log('[%s] result from the state at the hand-over kept (%d instructions, %d lift errors; second-stage '
                            'route: %d, %d)' % (tag, a_nodes, a_errs, b_nodes, b_errs))
            except (Exception, TimeLimit) as e:      # whatever goes wrong in the second try, the first result stands
                log('[%s] second-stage route not usable (%s): result from the state at the hand-over kept' % (
                    tag, (type(e).__name__ + ' ' + str(e)).strip().split('\n')[0][:160]))
            return text
        if any(n.startswith('[dumpnew]') for n in stage.run_notes):
            # the init did hand over; what did not get done is the dump of the state (every lazy
            # constant of the script is decoded for it: millions in a script of several megabytes)
            raise DevmError('run failed', 'the VM\'s init handed over to the script, but the dump of the state at that moment '
                            'was not finished (%s)' % '; '.join(n for n in stage.run_notes if n.startswith(('[dump]', '[timeout]', '[end]', '[error]')))[-300:])
        handover = '; '.join(n for n in stage.run_notes if not n.startswith('[http]'))[-600:] or 'the run ended without entering the script'
        if diagnose and not any(n.startswith('[handover]') for n in stage.run_notes):
            # which probe did the init fail?  One more run with call logging names its last calls.
            calls = stage.last_calls(common, timeout=min(run_timeout or 120, 120))
            if calls:
                handover += '; in a second run with call logging: ' + calls
        stopped = any(n.startswith(('[budget]', '[timeout]', '[end]', '[error]', '[stderr]', 'COMPILE ERROR')) for n in stage.run_notes)
        if single and not stopped:
            # the root program ran to its end and entered nothing else: it is all there is
            log('[%s] the root program returned without entering another program: it is the script itself' % tag)
            handover = None
            LAST_RUN['notes'] = []
        else:
            log('[%s] the VM\'s init did not hand over to the script (%s)' % (tag, handover))
            if single:
                # The root program was stopped inside its start-up code.  Older builds keep an
                # encrypted second stage behind that code, which the second-stage route recovers
                # without the checks having to pass; without one there is nothing to write out
                # (never the start-up code as "the script").
                handover = ('the loader runs a single root program that begins with Luraph\'s start-up code, and the run '
                            'was stopped before that code entered the script: ' + handover)
                if route == 'a':
                    raise DevmError('init did not hand over', handover)
            elif route == 'a' or route == 'auto' and not in_place:
                raise DevmError('init did not hand over', handover)
            log('[%s] trying the second-stage route: lift the init, run it natively, recover the key' % tag)
    try:
        # (after a failed hand-over of a first program this is a second try and gets the time of
        # one; for a single root program it is the route such builds always took)
        with time_limit(second_stage_limit if handover is not None and not single else 0):
            text1 = stage.run()
            boot_text, info = bootstrap.patch(text1)
            if info['fixkey']:
                data = run_bootstrap(stage, boot_text, workdir, tag, log, common)
    except (Exception, TimeLimit) as e:
        if handover is None:
            raise
        raise DevmError('init did not hand over', '%s; second-stage route: %s' % (
            handover, (type(e).__name__ + ': ' + str(e)).strip().split('\n')[0][:200]))
    if not info['fixkey']:
        if first or handover is not None:
            # never the init as "the script"
            raise DevmError('init did not hand over', '%s; the lifted init has no encrypted second stage to recover a key for' % (
                handover or 'route A was not tried'))
        log('[%s] the loader runs a single root program and it holds no encrypted second stage: that program is the script' % tag)
        LAST_RUN['notes'] = ['[single-root] lifted from the state at the first interpreter entry']
        return cleanup.tidy(text1)
    return lift_program(stage, data, tag, log, oneline, 'real program')


def hidden_errors(errors, oracle, text, detail=False):
    """Lift errors of the factory route that have no LIFT ERROR marker in the text: (in code the
    traced run executed, in code that never ran).  Markers carry the error's message, not its
    place: per message, the errors of code that ran are counted first among those without a
    marker (which of them have one is not known, and code that ran must not be taken for cut), so
    the first number is an upper bound where a message has markers and errors of both kinds.
    detail: (that bound, the second number, the least number of executed errors without a marker
    -- every marker on one that ran --, the messages of executed errors that may have none)."""
    import collections
    marks = collections.Counter(' '.join(m.split()) for m in re.findall(r'LIFT ERROR: ([^\n]*)', text))
    by_msg = {}
    for k, e in errors:
        ran = isinstance(e[0], tuple) and (k, e[0][0], e[0][1]) in oracle
        by_msg.setdefault(' '.join(str(e[1]).split()), [0, 0])[0 if ran else 1] += 1
    hidden_ran = hidden_cold = least = 0
    msgs = []
    for msg, (r, c) in by_msg.items():
        m = marks.get(msg, 0)
        hidden = max(0, r + c - m)
        hidden_ran += min(r, hidden)
        hidden_cold += hidden - min(r, hidden)
        least += max(0, r - m)
        if min(r, hidden):
            msgs.append(msg)
    if detail:
        return hidden_ran, hidden_cold, least, sorted(msgs)
    return hidden_ran, hidden_cold


def hidden_head(errors, oracle, text):
    """(the head line for executed lift errors without a marker, or None; the number of errors
    without a marker in code that never ran).  Where errors with one message are marked only in
    part, which of them have the markers is not known: the count is then an upper bound ('up
    to'), and the line lists the messages of the executed errors that may have none."""
    ran, cold, least, msgs = hidden_errors(errors, oracle, text, detail=True)
    if not ran:
        return None, cold
    if least == ran:
        what = '%d instruction(s) that the traced run executed did not lift and have no marker in the text below' % ran
    else:
        what = ('up to %d instruction(s) that the traced run executed did not lift and may have no marker in the text '
                'below (%s: markers name the error, not its place, and some errors with the same message are marked)'
                % (ran, 'at least %d have none' % least if least else 'possibly none'))
    texts = sorted({m[:100] for m in msgs})
    return '-- LIFT ERROR: %s.  Their lift errors: %s' % (what, '; '.join(texts[:12])), cold


def lift_program(stage, data, tag, log, oneline, what, replayed=True):
    """Lift and decompile the program a dump was taken at the start of (proto-table layout):
    the dump of the native bootstrap replay, or the one taken when the VM's own init handed over.

    replayed: the dump comes from the native replay of the lifted init.  Its environment checks
    were neutralised, so a key function can have a value and still the wrong one: values are
    searched for every key function a proto with lift errors uses.  After the VM's own init
    (replayed=False) the values are the ones the VM computed; only functions the harness could not
    evaluate are searched (577 trial lifts per function and proto otherwise)."""
    # First with the key function values the bootstrap replay produced.  Junk arms behind opaque
    # predicates do not lift cleanly and must not be mistaken for wrong keys, so they are cut
    # before anything is solved for.
    overrides = {}
    L = make_lifter(stage.model, data, overrides)
    protos = L.lift_all(L.root_key)
    cuts = {}
    cut = opaque.prune_ir(protos, cuts)
    only = None if replayed else unknown_keyfns(data)
    if any(p.errors and p.keyfns and (only is None or p.keyfns & only) for p in protos.values()):
        L, protos, overrides = solve_keyfns(stage.model, data, log, only)
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
    text = cleanup.tidy(text)
    hidden = len(errs) - text.count('LIFT ERROR')
    if hidden > 0:
        # Instructions that did not lift and are not in the printed text either (in a function or
        # a branch the printer left out).  The result must not pass for complete on that account.
        log('[%s] %d of the %d lift errors are in code the text does not contain: said at its head' % (tag, hidden, len(errs)))
        texts = sorted({' '.join(str(e[1]).split())[:100] for _k, e in errs})
        text = ('-- LIFT ERROR: %d instruction(s) did not lift and have no marker in the text below (they are in code the text '
                'does not contain).  Lift errors of this run: %s\n' % (hidden, '; '.join(texts[:12]))) + text
    return text


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
    ap.add_argument('--chunk-name', default=None, metavar='NAME',
                    help='with --chunk-arg: the name the container loads the chunk under ("Luraph" plus 0-4 spaces; '
                         'default "Luraph")')
    ap.add_argument('--route', choices=('auto', 'a', 'b'), default='auto',
                    help='Luraph v14 builds: a = let the VM run its own init and lift the script from the state at the '
                         'hand-over; b = lift the init, run it natively and recover the key of the second stage; '
                         'auto = a, then b when the init does not hand over')
    ap.add_argument('--budget', type=int, default=None, help='route a: VM instructions the init may run before it is stopped')
    ap.add_argument('--run-timeout', type=int, default=None, help='route a: seconds the run may take')
    ap.add_argument('--no-diagnosis', action='store_true',
                    help='route a: when the init does not hand over, do not run it a second time with call logging '
                         '(the run that names the last mocked calls in the failure message)')
    a = ap.parse_args(argv)
    work = a.work or os.path.join(os.path.dirname(os.path.abspath(a.input)), '.devm')
    out = a.output or os.path.splitext(a.input)[0] + '.deobf.lua'

    def write_result(text):
        """the result file: the report first (see below), then the text; returns the report lines"""
        # (a text that does not parse is never complete, whatever else it has: bin/luau-ast says)
        parsed = parse_check(text, work if os.path.isdir(work) else None)
        lines = run_report(LAST_RUN['notes']) + report(text, returned=any(
            n.startswith('[oracle] script returned') for n in LAST_RUN['notes']), notes=LAST_RUN['notes'], parse=parsed)
        if parsed[0] is False and parsed[1] is not None:
            # (the line of the error as the written file has it: below the head of 1 + len(lines) lines)
            lines = [PARSE_PREFIX + 'line %d: %s' % (parsed[1] + 2 + len(lines), parsed[2]) if ln.startswith(PARSE_PREFIX) else ln
                     for ln in lines]
        head = ['-- %s, %s.  How the run went and what this file lacks:' % (os.path.basename(a.input), HEAD_MARK)]
        head += ['--   ' + ln for ln in lines]
        text = '\n'.join(head) + '\n' + text
        with open(out, 'w', encoding='utf-8') as f:
            f.write(text)
        return lines, text

    stop_on_signals()
    try:
        text = deobfuscate(a.input, work, script_path=a.script_path, server=a.server, root=a.root, http_reply=a.http_reply, executor=a.executor, mfp_delta=a.mfp_delta, exercise=not a.no_exercise, http_map=a.http_map, chunk_arg=a.chunk_arg, chunk_name=a.chunk_name, mark_untaken=a.mark_untaken, route=a.route, budget=a.budget, run_timeout=a.run_timeout, preliminary=write_result, diagnose=not a.no_diagnosis)
    except Terminated as e:
        if vmmodel.SERVER is not None:
            vmmodel.SERVER.close()
        print('devm: terminated: signal %s' % e.args[0])
        sys.exit(128 + int(e.args[0]))
    except DevmError as e:
        print('devm: %s: %s' % (e.stage, e.reason))
        sys.exit(1)
    except (pe.PEError, RuntimeError, bootstrap.BootstrapError, SyntaxError, subprocess.TimeoutExpired) as e:
        stage = failure_stage(e)
        # (a failed run's message carries the end of the harness output, vmmodel.harness_tail: room for it)
        msg = ' '.join(str(e).split())[:1000 if stage == 'run failed' else 400]
        if isinstance(e, SyntaxError):
            msg = 'the text does not parse as Luau (%s)' % msg
        if msg.startswith(stage + ': '):
            msg = msg[len(stage) + 2:]
        print('devm: %s: %s' % (stage, msg))
        sys.exit(1)
    except Exception as e:
        # not an expected way to fail: the traceback for whoever debugs it, then the one-line form
        import traceback
        traceback.print_exc()
        sys.stderr.flush()
        print('devm: internal error: %s: %s' % (type(e).__name__, ' '.join(str(e).split())[:300]))
        sys.exit(1)
    # the report is also the head of the file: a result read later, without the run's output next
    # to it, must still say what it is and what it lacks
    lines, text = write_result(text)
    print('written', out, '(%d lines)' % text.count('\n'))
    for line in lines:
        print(line)


STAGES = ('unsupported target', 'damaged input', 'no model', 'init did not hand over', 'run failed', 'lift failed')


def failure_stage(e):
    """The stage name of the one-line failure message for an exception that is an expected way to fail."""
    msg = str(e)
    for stage in STAGES:
        if msg.startswith(stage + ': '):
            # (raised with the stage in front, e.g. pipeline: 'run failed: the VM never started: ...')
            return stage
    if isinstance(e, SyntaxError):
        # (a build for another Lua dialect was turned away before the parser, see normalize.foreign_dialect:
        # what does not parse here is cut off, edited or corrupt)
        return 'damaged input'
    if isinstance(e, subprocess.TimeoutExpired):
        return 'run failed'
    if 'no Luraph dispatch loop' in msg or 'no Luraph interpreter' in msg or 'cannot locate the dispatch loops' in msg:
        return 'no model'
    if msg.startswith(('dump failed', 'oracle run failed', 'bootstrap did not reach')):
        return 'run failed'
    return 'lift failed'


HEAD_MARK = 'recovered by devm (python -m devm.run)'      # first line of a file main() writes
# (how run_report starts the lines of the run notes report() also reads off the head of a file)
SINGLE_ROOT_LINE = 'the VM has a single root program'
STARTUP_LINE = 'start-up code: '
HINT_LINE = '  hint: '
PARSE_PREFIX = '  the text does not parse: '
LAST_RUN = {'notes': []}       # what the traced run of the last deobfuscate() call reported


def _luau_ast():
    """bin/luau-ast (luaubin.find, the repository's one lookup), or None."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location('luaubin', os.path.join(HERE, '..', 'luaubin.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.find('luau-ast')
    except Exception:       # (no lookup module, or it failed: no parser)
        return None


def parse_check(text, workdir=None):
    """Does the text parse as Luau?  bin/luau-ast reports errors on stderr and by its exit status.
    Returns (True, None, '') when it parses, (False, line, first error) when it does not (line:
    0-based in the text, None when the message names none), (None, None, why) when it was not
    checked (no parser, or the parser did not finish)."""
    ast = _luau_ast()
    if not ast:
        return None, None, 'bin/luau-ast was not found'
    import tempfile
    fd, path = tempfile.mkstemp(suffix='.luau', prefix='parse-', dir=workdir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        r = subprocess.run([ast, path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors='replace',
                           timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, None, 'luau-ast did not finish (%s)' % type(e).__name__
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if r.returncode == 0 and 'Parse errors' not in r.stderr[:4000]:
        return True, None, ''
    if r.returncode < 0:
        return None, None, 'luau-ast was stopped by signal %d' % -r.returncode
    m = re.search(r'(?m)^\s*\((\d+), \d+\) - \(\d+, \d+\) - (.*)$', r.stderr)
    if m:
        return False, int(m.group(1)), ' '.join(m.group(2).split())[:300]
    first = next((ln.strip() for ln in r.stderr.split('\n') if ln.strip() and not ln.startswith('Parse errors')), '')
    return False, None, ' '.join(first.split())[:300] or 'luau-ast exit status %d' % r.returncode


def run_report(notes):
    """How the traced run ended, for the final report: the lift only knows what that run reached."""
    import re
    out = []
    for n in notes:
        m = re.match(r'\[oracle\] script (returned|stopped: .*?) after (\d+) instructions', n)
        if m:
            out.append('traced run: the script %s (%s VM instructions)' % (
                'returned' if m.group(1) == 'returned' else 'stopped with "%s"' % m.group(1)[len('stopped: '):], m.group(2)))
    single = any(n.startswith('[root]') for n in notes)
    for n in notes:
        m = re.match(r'\[dumpnew\] the script starts at interpreter entry (\d+) \((\d+) VM instructions', n)
        if m and single:
            out.append('the VM\'s root program began with Luraph\'s start-up code, which ran (%s VM instructions) and then '
                       'entered a program it had built: the script.  This result is a static lift of that program from the '
                       'state at that moment: the script itself did not run' % m.group(2))
        elif m:
            out.append('the VM ran its own init (%s VM instructions) and handed over to the script; this result is a static '
                       'lift of the script\'s program from the state at that moment: the script itself did not run' % m.group(2))
        elif n.startswith('[single-root]'):
            out.append(SINGLE_ROOT_LINE + ' (no separate init that hands over): this result is a static lift of '
                       'that program from the state at its first entry; nothing ran, and whatever Luraph\'s start-up code '
                       'in it would have decoded at run time is still as the file stores it')
        elif n.startswith('[startup] '):
            out.append(STARTUP_LINE + n[len('[startup] '):])
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
            out.append(HINT_LINE + n[len('[hint] '):])
        elif n.startswith('[cut] '):
            out.append('  note: ' + n[len('[cut] '):])
    # what the script itself printed or read, and where the mock had no Roblox answer (rbxmock.lua)
    said = [n for n in notes if n.startswith('[script]')]
    for n in said[:12]:
        out.append('  script: ' + n[len('[script] '):])
    if len(said) > 12:
        out.append('  script: ... %d more lines of its output in the run log' % (len(said) - 12))
    for n in notes:
        if n.startswith('[stub] permissive'):
            out.append('  mock: ' + n[len('[stub] '):])
    return out


# (by the lifter's own names for parameters and locals: a checksum routine of the script, or a
# plain-Lua helper printed with its own text, also ends in `return bit32.bxor(x, 4294967295)`)
KEY_LEFT = re.compile(r'(?m)^[ \t]*return bit32\.bxor\(p\d+, \d{7,}\)(?:,.*)?$')


KEY_MIX = re.compile(r'(?m)^.*\b(v\d+) - \1 % 1\b.*$')


# the head line deobfuscate / lift_program put in front of a text whose lift errors are not all
# marked in it (not a marker itself)
HIDDEN_HEAD = re.compile(r'^-- LIFT ERROR: (up to )?(\d+) instruction\(s\) (that the traced run executed )?did not lift and')


def report(text, returned=None, notes=None, parse=None):
    """What is left unrecovered in a result, in plain words.  `returned`: the traced run went to
    the script's end; `notes`: the run notes (LAST_RUN; None: read both off the head of a file
    main() wrote); `parse`: what parse_check said about the text (None: not asked)."""
    lines = text.split('\n')
    head_notes = []
    if lines and HEAD_MARK in lines[0]:
        # a file main() wrote: its head is an earlier report, not part of the result
        del lines[0]
        while lines and lines[0].startswith('--   '):
            if returned is None and lines[0].startswith('--   traced run: the script returned'):
                returned = True
            if lines[0].startswith('--   ' + SINGLE_ROOT_LINE):
                head_notes.append('[single-root]')
            elif lines[0].startswith('--   ' + STARTUP_LINE):
                head_notes.append('[startup] ' + lines[0][len('--   ' + STARTUP_LINE):])
            elif lines[0].startswith('--   ' + HINT_LINE):
                head_notes.append('[hint] ' + lines[0][len('--   ' + HINT_LINE):])
            del lines[0]
        text = '\n'.join(lines)
    if notes is None:
        notes = head_notes
    single_root = any(n.startswith('[single-root]') for n in notes)
    startup = any(n.startswith('[startup]') for n in notes)
    # pipeline's hint: the proto taken for the script is tiny and calls nothing but pure library
    # functions, while the first program did all the work (the script is probably that program)
    hint = next((n for n in notes if n.startswith('[hint]')), None)
    unparsed = parse is not None and parse[0] is False
    # instructions without a marker, said in a head line of the text (not one more marker)
    hidden = [HIDDEN_HEAD.match(ln) for ln in lines[:2]]
    hidden = [m for m in hidden if m]
    errs = text.count('LIFT ERROR') - sum(m.string.count('LIFT ERROR') for m in hidden)
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
    # Luraph's per-call keys (opaque._fold_call_keys) where they could not be folded: a parameter
    # xor-ed with a literal into an extra first result
    keys = len(KEY_LEFT.findall(code))
    # ... and the interpreter's own key arithmetic where it did not fold to a constant: a number
    # cut to 32 bits and to an integer by hand (`v = x % 4294967296; w = v - v % 1`)
    mixes = len(KEY_MIX.findall(code))
    # ... and its dead arithmetic: constants computed into locals nothing reads (v14 self-patch
    # values; cleanup.drop_dead_constants removes those it can show to be harmless)
    dead = sum(len(x) for x in cleanup.dead_constant_lines(code))
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
    unchecked = ['  note: not checked that the text parses (%s)' % parse[2]] if parse is not None and parse[0] is None else []
    if not (errs or blank or refs or flat or flags or vm or ups or holes or stubs or preps or envrefs or regs
            or keys or mixes or dead or single_root or startup or hint or hidden or unparsed):
        out.append('result: complete (no unlifted instructions, no missing constants, all control flow structured)')
        return out + unchecked + traps
    out.append('result: PARTIAL')
    if unparsed:
        out.append(PARSE_PREFIX + ('line %d: ' % (parse[1] + 1) if parse[1] is not None else '') + parse[2])
    out += unchecked
    if hint:
        m = re.search(r'--root (\d+)', hint)
        out.append('  the result may not be the script: the function taken for it is small and calls nothing but pure '
                   'library functions, while the VM\'s first program did the work of the run (see the hint above%s)'
                   % ('; --root %s lifts the first program' % m.group(1) if m else ''))
    for m in hidden:
        if m.group(3):
            out.append('  %s%s instruction(s) that the traced run executed did not lift and %s no LIFT ERROR marker in the '
                       'text (its first line lists their errors)' % (m.group(1) or '', m.group(2),
                                                                     'may have' if m.group(1) else 'have'))
        else:
            out.append('  %s instruction(s) did not lift and have no LIFT ERROR marker: they are in code the text does not '
                       'contain (its first line lists the errors)' % m.group(2))
    if startup:
        out.append('  the result is the VM\'s first program, which begins with Luraph\'s start-up code: the script was not '
                   'found as a program of its own (what the run shows about it is in the start-up code line above)')
    if single_root:
        out.append('  the result is the VM\'s root program including Luraph\'s start-up code, lifted from the state before '
                   'it ran: what that code decodes at run time is still encoded, and the script is not separated from it')
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
    if keys:
        out.append('  %d functions return a parameter xor-ed with a large literal as their first result (`return bit32.bxor(p, K), ...`): '
                   'this is how Luraph\'s per-call keys look, a number every caller passes and gets back changed.  They are folded '
                   'away where every caller is in the result; these were left because that could not be shown (or the script does '
                   'such arithmetic itself)' % keys)
    if mixes:
        out.append('  %d lines of Luraph\'s key arithmetic are left (`v - v %% 1` on a value cut to 32 bits): numbers the '
                   'interpreter mixes into its keys, not part of the script' % mixes)
    if dead:
        out.append('  %d lines compute a constant into a local nothing reads (`local v = <literals, arithmetic, bit32 / '
                   'string calls>`): Luraph\'s dead arithmetic, left because it does not evaluate here (an operand '
                   'printed as nil, an unsupported format or call: removing a line that raises could change what the '
                   'script does)' % dead)
    depth = max((len(ln) - len(ln.lstrip('\t')) for ln in text.split('\n')), default=0)
    if depth > 60:
        out.append('  nesting up to %d levels deep: a flattened function came out as a chain of nested blocks, '
                   'not as its original structure' % depth)
    if blank * 5 > lines and returned:
        out.append('  the traced run went to the script\'s end, so those lines are in code it did not execute (branches '
                   'not taken, functions nobody called): their constants stayed encrypted')
    elif blank * 5 > lines:
        out.append('  most of the script did not run.  Usual causes: it waits for a server reply (see the [http] '
                   'lines above; --http-map), a key system, or an environment check that failed '
                   '(try --no-executor / --executor)')
    return out + traps


if __name__ == '__main__':
    main()
