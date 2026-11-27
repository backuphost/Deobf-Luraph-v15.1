"""End-to-end driver: source -> (hook + dump) -> lift -> decompile, repeated per VM layer."""
import json
import os
import re
import subprocess

from . import vmmodel, pe, lifter, structure, luprint

HERE = os.path.dirname(os.path.abspath(__file__))


class Stage:
    def __init__(self, src, workdir, tag, layers=(), log=print, oneline=False):
        self.src = src
        self.workdir = workdir
        self.tag = tag
        self.layers = list(layers)
        self.log = log
        self.model = vmmodel.Model(src, oneline=oneline)
        self.patched_path = os.path.join(workdir, tag + '.patched.lua')

    def run(self):
        m = self.model
        self.log('[%s] interpreter at line %d, %d dispatch loop(s), %s' % (
            self.tag, m.interp.line, len(m.loops), 'factory' if m.factory_style else 'proto-table'))
        raw = vmmodel.run_dump(m, self.workdir, self.tag, self.layers, timeout=getattr(self, 'dump_timeout', 600))
        self.dump = pe.Dump(raw, m)
        if m.factory_style:
            self.lifter = lifter.FactoryLifter(m, self.dump)
        else:
            self.lifter = lifter.TableProtoLifter(m, self.dump)
        protos = self.lifter.lift_all(self.lifter.root_key)
        nodes = sum(len(p.nodes) for p in protos.values())
        errs = [(k, e) for k, p in protos.items() for e in p.errors]
        conf = [(k, c) for k, p in protos.items() for c in p.conflicts]
        self.log('[%s] lifted %d protos, %d instructions, %d errors, %d temp conflicts' % (
            self.tag, len(protos), nodes, len(errs), len(conf)))
        self.errors = errs
        self.conflicts = conf
        d = structure.Decompiler(protos, self.lifter.root_key, self.native_text)
        self.text = d.run()
        self.stats = d.stats
        self.log('[%s] decompiled: %d lines, %d unstructured' % (self.tag, self.text.count('\n'), d.stats['unstructured']))
        return self.text


def _run_traced(self):
    """Factory-style VMs: run the script to its end under the recording harness and lift the
    final VM state.

    These builds decrypt their own code while it runs, with keys computed from the environment
    (whole-array passes at start-up, instructions that decrypt themselves or their successors,
    one-shot constant decoders).  A dump taken at the first interpreter entry is therefore mostly
    ciphertext.  At the end of a run everything that executed is plain, and the recorded register
    values supply the keys wherever the lifter needs one."""
    m = self.model
    self.log('[%s] interpreter at line %d, %d dispatch loop(s), factory' % (self.tag, m.interp.line, len(m.loops)))
    raw, oracle, notes = vmmodel.run_oracle(m, self.workdir, self.tag, getattr(self, 'harness_options', ()))
    self.log('[%s] traced run: %d protos, %d distinct instructions executed' % (self.tag, len(raw['protos']), len(oracle)))
    self.run_notes = notes
    for n in notes:
        self.log('[%s]   %s' % (self.tag, n))
    if not raw['protos']:
        # no function of the VM was ever created: the script stopped inside Luraph's loader (a VM
        # chunk run without the buffer its container passes to it stops at its first read)
        how = next((n[len('[oracle] '):] for n in notes if n.startswith('[oracle] ')), 'the run recorded nothing')
        raise RuntimeError('run failed: the VM never started: %s (a chunk cut out of a container needs its data: --chunk-arg)' % how)
    self.dump = pe.Dump(raw, m)
    self.dump.server = vmmodel.SERVER
    if vmmodel.SERVER is not None:
        vmmodel.SERVER.data = raw
    self.lifter = lifter.FactoryLifter(m, self.dump)
    self.lifter.oracle = oracle
    self.lifter.end_state = True
    # Two-interpreter builds: the first program is Luraph's bootstrap (environment checks, key
    # derivation, decryption); the script itself is the first proto of the other interpreter.
    root = first_root = self.lifter.root_key
    boot = self.lifter.records[root - 1][0]
    payload = [i + 1 for i, (idx, _) in enumerate(self.lifter.records) if idx != boot]
    # --root overrides: in some builds the other interpreter only runs a helper of the build (a
    # keystream routine) and the script is the first program after all.
    forced = getattr(self, 'force_root', None)
    helpers = entry = None
    # {program: the program that ran the instruction before its first one} (harness.lua, the
    # record's `caller`): script_programs and entered_program follow these links
    callers = {}
    for i, p in enumerate(raw['protos']):
        c = self.dump.conv(p, '__LPH_PROTOS[%d]' % (i + 1)).h.get('caller')
        if isinstance(c, (int, float)):
            callers[i + 1] = int(c)
    reason = None
    if forced:
        payload = [forced] if forced != root else []
    elif payload:
        interps = [idx for idx, _ in self.lifter.records]
        payload, helpers = script_programs(interps, payload, notes, callers, first_root)
        if helpers:
            reason = helpers_reason(interps, helpers, notes, callers, first_root)
            self.log('[%s] %s the %d program(s) on other interpreters (%s)' % (
                self.tag, reason, len(helpers), ', '.join(str(p) for p in helpers[:8]) + (', ...' if len(helpers) > 8 else '')))
    entered = None
    if not forced and not payload:
        # The script runs on the first interpreter.  Its top level can be part of Luraph's main
        # function (the v15.2 example), or a program of its own that the start-up code enters at
        # its end.  Only a run that did not end is lifted from that program (entered_program says
        # why); otherwise the first program is, and its note names the program.
        # (evidence of where the script runs: the programs its output and requests came from, or,
        # for a script that showed nothing, the program the run was stopped in)
        eff = effect_programs(notes)
        evidence = eff or stopped_in(notes)
        entry = entered_program(evidence, callers, oracle, first_root,
                                evidence='the script\'s output came from' if eff else 'the run was stopped in',
                                stalled=any(n.startswith('[stalled] ') for n in notes))
        if entry:
            payload = [entry[0]]
            self.log('[%s] %s' % (self.tag, entry[1]))
        else:
            entered = entered_candidate(evidence, callers, oracle, first_root)
    self.payload_root = payload[0] if payload else None
    if payload:
        root = payload[0]
        self.lifter.root_key = root
        if not entry:
            self.log('[%s] payload: proto %d is the script (protos 1..%d are the bootstrap)' % (self.tag, root, root - 1))
    protos = self.lifter.lift_all(root)
    if vmmodel.SERVER is not None:
        # functions that never ran decrypt themselves with numbers their callers pass: lift those
        # again with the numbers the lifted callers show (see opaque.relift_params)
        from . import opaque
        protos = opaque.relift_params(self.lifter, protos, root, self.native_text,
                                      log=lambda msg: self.log('[%s] %s' % (self.tag, msg)))
    # Values the run proves where the lifted code would show something else: what a call handed its
    # caller's register array wrote there (a helper of the build that decodes a constant into a
    # register of the script's program), and the results of the build's helpers (runvals.py)
    from . import runvals
    helper_set = set(helpers or ())
    sites = runvals.show_run_values(protos, oracle, helper_set)
    self.protos = protos
    self.oracle = oracle
    nodes = sum(len(p.nodes) for p in protos.values())
    errs = [(k, e) for k, p in protos.items() for e in p.errors]
    ran = sum(1 for k, p in protos.items() for e in p.errors if (k, e[0][0], e[0][1]) in oracle)
    self.log('[%s] lifted %d protos, %d instructions, %d resolved with run-time keys, %d errors (%d in code that ran)' % (
        self.tag, len(protos), nodes, sum(len(p.oracle_used) for p in protos.values()), len(errs), ran))
    self.errors = errs
    self.conflicts = [(k, c) for k, p in protos.items() for c in p.conflicts]
    self.hints = []
    if entry:
        self.hints.append('[entry] ' + entry[1])
    if not payload:
        self.hints.append(startup_note(notes, forced, first_root, len(m.interps), helpers=helpers, entered=entered,
                                       reason=reason))
    if payload and not forced and not entry:
        # The proto taken for the script is the first one on the second interpreter.  Where that
        # interpreter only runs a helper, next to nothing is lifted while the run shows the first
        # program doing all the work: say so (the choice itself is left alone, --root decides).
        ran_first = sum(1 for k in oracle if isinstance(k[0], int) and 0 < k[0] <= len(self.lifter.records)
                        and self.lifter.records[k[0] - 1][0] == boot)
        # (A small script is small too -- a test script is a handful of instructions next to the
        # 20,000 of Luraph's start-up code -- so size alone says nothing.  A helper of the build
        # is arithmetic: the hint is for a function that calls nothing but pure library functions.)
        if nodes < 50 and ran_first > 1000 and not _calls_out(protos):
            self.hints.append('[hint] the function taken for the script (proto %d, on the second interpreter) has only %d '
                              'instructions, while the first program executed %d: the script is probably the first '
                              'program; run again with --root %d' % (root, nodes, ran_first, first_root))
    for n in runvals.notes(sites, helper_set):
        self.log('[%s] %s' % (self.tag, n))
        self.hints.append(n)
    d = structure.Decompiler(protos, self.lifter.root_key, self.native_text)
    d.labels = runvals.labels(sites, helper_set, protos)
    self.text = d.run()
    self.stats = d.stats
    self.log('[%s] decompiled: %d lines, %d unstructured' % (self.tag, self.text.count('\n'), d.stats['unstructured']))
    return self.text


Stage.run_traced = _run_traced


def script_votes(interps, payload, notes, callers=None, first_root=1):
    """({interpreter: count}, kind): where the script's code ran, by the run's evidence, counted for
    the interpreters it points at (`interps[i]` is the interpreter of program i + 1, `payload` the
    programs on interpreters other than the first program's, `first_root` the first program).

    kind 'under': the harness's [under] line, which places each line of the script's output and
    each request under the innermost program of another interpreter on its VM call chain (the
    script's program calls routines of the first interpreter too, and the last VM instruction
    before its output can be in one of those), or under none (the first interpreter).
    kind 'effects': without that line, the programs of the [effects] line (the ones that ran the
    last VM instruction before each), each followed through `callers` ({program: the program
    that ran the instruction before its first one}, harness.lua's record) towards the first
    program: where the chain passes a program of `payload`, its count goes to that program's
    interpreter.  These links are only the caller at a program's first entry, so they are weaker
    evidence than the call chain, and every program counts as often as its output and requests.
    kind 'where': a run that did not end and showed no output and no request: the program it was
    stopped in ([where]), placed like the output, by the call chain where the line gives it and
    otherwise through `callers`.  ({}, None) without evidence."""
    first = interps[first_root - 1] if 0 < first_root <= len(interps) else None
    pay, callers = set(payload), callers or {}

    def interp(p):
        return interps[p - 1] if 0 < p <= len(interps) else None

    def placed(p):
        x, seen = p, set()
        while x not in pay and x != first_root and x in callers and x not in seen:
            seen.add(x)
            x = callers[x]
        return interp(x if x in pay else p)

    votes = {}

    def vote(i, n):
        if i is not None:
            votes[i] = votes.get(i, 0) + n

    under = under_programs(notes)
    if under:
        for p, n in under.items():
            vote(interp(p) if p else first, n)
        return votes, 'under'
    eff = effect_programs(notes)
    if eff:
        for p, n in eff.items():
            vote(placed(p), n)
        return votes, 'effects'
    u = stopped_under(notes)
    if u is not None:
        vote(interp(u) if u else first, 1)
        return votes, 'where'
    for p, n in stopped_in(notes).items():
        vote(placed(p), n)
    return votes, ('where' if votes else None)


def script_programs(interps, payload, notes, callers=None, first_root=1):
    """(payload, helpers): the programs on interpreters other than the first one (`payload`, in
    order; `interps[i]` is the interpreter of program i + 1) narrowed to those on the interpreter
    the run's evidence points at (script_votes: the script's output and requests, or for a run
    that did not end and showed neither, the program it was stopped in).  A build can make
    programs on other interpreters that are helpers (routines of the build) while the script's own
    code runs on the first interpreter, its top level next to Luraph's start-up code: then no
    program is left and `helpers` lists the ones set aside (the first program is lifted, with the
    [startup] note).

    The evidence is weighed: an interpreter other than the first is taken only when more of it
    points there than at the first interpreter (the one with the most, if several), so that one
    program among many, say one first called from a helper, does not decide.  Without evidence
    the payload stays as it is."""
    votes, _kind = script_votes(interps, payload, notes, callers, first_root)
    if not votes:
        return payload, None
    first = interps[first_root - 1] if 0 < first_root <= len(interps) else None
    others = {i: n for i, n in votes.items() if i != first}
    best = max(others.values(), default=0)
    take = [p for p in payload if best > votes.get(first, 0) and others.get(interps[p - 1]) == best]
    return take, (payload if not take else None)


def helpers_reason(interps, payload, notes, callers=None, first_root=1):
    """What made script_programs set the programs on other interpreters aside, in words for the
    notes: the start of a sentence that goes on with the programs (`... not from` / `... not in or
    under`)"""
    votes, kind = script_votes(interps, payload, notes, callers, first_root)
    first = interps[first_root - 1] if 0 < first_root <= len(interps) else None
    if kind == 'where':
        p = next(iter(stopped_in(notes)), None)
        return ('the run was stopped in program %s, a program of the first interpreter that does not run under a program of '
                'another interpreter, not in or under' % p)
    own, total = votes.get(first, 0), sum(votes.values())
    if own == total:
        return 'the script\'s output and requests came from programs of the first interpreter, not from'
    return ('%d of the script\'s %d lines of output and requests came from programs of the first interpreter (%d from under '
            'programs of other interpreters), not from' % (own, total, total - own))


# instructions of the first program that may run for the first time after the program it entered
# started, for that program to count as the script (the start-up code's own epilogue)
ENTRY_SLACK = 32


def entered_candidate(effects, callers, oracle, first_root=1):
    """(program, step, after): the program Luraph's start-up code entered at the end of its run, on
    the way from the programs in `effects` to the first program, else None.

    `effects`: {program: count} of the [effects] line (or the program a run that did not end was
    stopped in); `callers`: {program: the program that ran the instruction before its first one}
    (harness.lua, the record's `caller`); `oracle`: the traced run's records ((pid, loop, pc) ->
    {'vers': [[step, text], ...], ...}).  Each program the script's output came from is followed
    through its callers to the first program; the program below it on that chain is the one the
    first program entered, at `step`.  None when the output came from the first program itself, or
    when the first program ran more than ENTRY_SLACK instructions for the first time (`after`)
    after that step: it went on with code of its own."""
    if not effects or first_root in effects:
        return None
    below = {}
    for p, n in effects.items():
        seen, x = set(), p
        while x in callers and callers[x] != first_root and x not in seen:
            seen.add(x)
            x = callers[x]
        if callers.get(x) == first_root and x != first_root:
            below[x] = below.get(x, 0) + n
    if not below:
        return None
    first = {}
    for (pid, _loop, _pc), rec in oracle.items():
        if rec.get('vers'):
            first[pid] = min(first.get(pid, rec['vers'][0][0]), rec['vers'][0][0])
    cand = max(below, key=lambda x: (below[x], -first.get(x, 0)))
    if cand not in first:
        return None
    after = sum(1 for (pid, _l, _p), rec in oracle.items()
                if pid == first_root and rec.get('vers') and rec['vers'][0][0] > first[cand])
    if after > ENTRY_SLACK:
        return None
    return cand, first[cand], after


def entered_program(effects, callers, oracle, first_root=1, evidence='the script\'s output came from', stalled=False):
    """(program, note) when the result is to be lifted from the program Luraph's start-up code
    entered at the end of its run (entered_candidate), else None.

    Only for a run that did not end (`stalled`: the harness stopped it, vmmodel.STALL): the first
    program never came back from that program, and the first program's own code is all ahead of
    the call -- Luraph's start-up code, which a lift of the first program would put in front of
    the script.  A run that ended is lifted from the first program as before: the script's top
    level can be flattened into Luraph's main function (v15.2) and end in a call of a function of
    the script, which a lift of that function alone would drop (its `[startup]` note names the
    program).  Even here the script's top level can be in front of the call, so the note says what
    the result lacks and how to get it, and run.report makes the result PARTIAL."""
    if not stalled:
        return None
    got = entered_candidate(effects, callers, oracle, first_root)
    if got is None:
        return None
    cand, step, after = got
    return cand, ('program %d is taken for the script: the run did not end, and the first program called it at step %d '
                  'and ran %s after that; %s program(s) %s, which run under it.  The first program\'s code before that '
                  'call is not in the result: Luraph\'s start-up code, and any part of the script\'s top level that '
                  'Luraph put in that program (--root %d lifts the first program)' % (
                      cand, step, 'no new instruction' if not after else '%d new instruction(s)' % after,
                      evidence, ', '.join(str(p) for p in sorted(effects)), first_root))


def stopped_in(notes):
    """{program: 1} of the harness's [where] line (the program a run that did not end was stopped
    in), {} without it"""
    for n in notes:
        m = re.match(r'\[where\] the run was stopped in program (\d+)(?:,|$)', n)
        if m:
            return {int(m.group(1)): 1}
    return {}


def stopped_under(notes):
    """The innermost program of another interpreter on the VM call chain where a run that did not
    end was stopped (the harness's [where] line): that program, 0 for none, None when the line does
    not say (or there is none)"""
    for n in notes:
        m = re.match(r'\[where\] the run was stopped in program (\d+)(.*)$', n)
        if not m:
            continue
        rest = m.group(2)
        if rest == ', on another interpreter':
            return int(m.group(1))
        if rest == ', under no program of another interpreter':
            return 0
        u = re.match(r', under program (\d+) of another interpreter$', rest)
        return int(u.group(1)) if u else None
    return None


def effect_programs(notes):
    """{program number: count} of the harness's [effects] line: the programs that ran the last VM
    instruction before each line of the script's output and each request ({} without the line)"""
    for n in notes:
        if n.startswith('[effects] '):
            return {int(a): int(b) for a, b in re.findall(r'(\d+) \((\d+)\)', n)}
    return {}


def under_programs(notes):
    """{program: count} of the harness's [under] line: for each line of the script's output and
    each request, the innermost program of an interpreter other than the first one on its VM call
    chain, 0 for none ({} without the line)"""
    for n in notes:
        if n.startswith('[under] '):
            got = {int(a): int(b) for a, b in re.findall(r'under program (\d+) \((\d+)\)', n)}
            m = re.search(r'under no program of another interpreter \((\d+)\)', n)
            if m:
                got[0] = int(m.group(1))
            return got
    return {}


# the words of harness.lua's [stalled] line for a wait loop before the script showed itself otherwise
WAITED_FIRST = 'before it showed itself otherwise'


def startup_note(notes, forced, first_root, interps, helpers=None, entered=None, reason=None):
    """The run note for a factory-layout result that is the VM's first program: Luraph's start-up
    code (environment checks, key derivation, decryption), possibly with the script's top level
    flattened into it (v15.2 builds without a payload interpreter).  What the run shows about
    where the script is goes with it.

    (The v14.9 rule of the proto-table layout, a program entered that was not reachable from the
    root program's tables when it started, has no counterpart here: every program of a factory
    build is a closure the factory makes at run time.  The interpreter a program runs on is the
    evidence: the script is the first program of an interpreter other than the first one.)"""
    if forced:
        return ('[startup] --root %d: the result is the VM\'s first program, which begins with Luraph\'s start-up code; '
                'the script is not separated from it' % first_root)
    said = [n for n in notes if n.startswith('[script] ') and re.match(r'\[script\] (?:print|warn):', n)]
    asked = [n for n in notes if n.startswith('[http]')]
    shown = ', '.join(x for x in (
        '%d line(s) of output' % len(said) if said else '',
        '%d request(s)' % len(asked) if asked else '') if x)
    if helpers:
        # (reason: helpers_reason, what set them aside)
        head = ('[startup] %s the %d program(s) the run made on other interpreters (%s: helpers): the result is the VM\'s '
                'first program, which begins with Luraph\'s start-up code.  ' % (
                    reason or 'the script\'s output and requests came from programs of the first interpreter, not from',
                    len(helpers), ', '.join(str(p) for p in helpers[:8]) + (', ...' if len(helpers) > 8 else '')))
    else:
        head = ('[startup] the run created no program on an interpreter other than the first one (%d modelled): the result is '
                'the VM\'s first program, which begins with Luraph\'s start-up code.  ' % interps)
    # (entered_candidate: the program the first one entered at the end of its run, which the
    # output came from; a run that ended is not lifted from it, see entered_program)
    tail = ('.  The output came from program %d (and what it calls), which the first program called at the end of its '
            'run: --root %d lifts it on its own, without the first program\'s code before the call' % (
                entered[0], entered[0])) if entered else ''
    if shown:
        return head + ('The script\'s top level ran inside that program (the run shows %s from it), but it is not '
                       'separated from the start-up code' % shown) + tail
    if any(n.startswith('[oracle] script returned') for n in notes):
        return head + ('The run returned and shows nothing the script did (no output, no request): where the script is '
                       'was not found (it may not have run at all: a key check, a server reply or an environment check)') + tail
    if any(n.startswith('[stalled] ') and WAITED_FIRST in n for n in notes):
        # (harness.lua w.stop: a wait loop before anything else showed; Luraph's start-up code does not wait)
        return head + ('The run was stopped while the script waited, before it showed anything else (no output, no '
                       'request): its top level ran inside that program, but it is not separated from the start-up code') + tail
    return head + ('The run was stopped before the script showed anything (no output, no request): where the script '
                   'is was not found') + tail

_PURE_LIBS = ('bit32.', 'math.', 'string.', 'buffer.', 'table.', 'utf8.')


def _calls_out(protos):
    """True when a lifted instruction calls anything other than a pure library function."""
    for p in protos.values():
        for node in p.nodes.values():
            stack = [node.tree] if node.tree else []
            while stack:
                stmts, term = stack.pop()
                for st in stmts:
                    if st[0] == 'call' and not (isinstance(st[2], tuple) and st[2][0] == 'kfn' and str(st[2][1]).startswith(_PURE_LIBS)):
                        return True
                if term[0] == 'br':
                    stack += [term[2], term[3]]
                elif term[0] == 'forbr':
                    stack += [term[3], term[4]]
    return False


def _dump_script(self, options=(), budget=None, timeout=None):
    """Proto-table layout, route A.  The VM's first program is Luraph's own init; it returns the
    proto of the script.  The harness lets the VM run that init and dumps the state when the
    returned proto is entered (vmmodel.run_dump_script).  Nothing of the script itself runs.

    The copy that is executed (self.run_model) keeps the protected call around the dispatch
    loops: inits compare stack frames.  The lifter's model (self.model) is the inlined text;
    function ids of the dump are translated (self.fid_map).
    Returns the dump (as run_dump gives it), or None when the init did not hand over
    (self.run_notes says how the run ended)."""
    import json
    import re
    m = self.model
    rm = getattr(self, 'run_model', None) or m
    self.log('[%s] interpreter at line %d, %d dispatch loop(s) in %d interpreter(s), proto-table%s' % (
        self.tag, m.interp.line, len(m.loops), len(m.interps), ', one table per instruction' if m.insn_tables else ''))
    # what the init works through: the VM text (it holds the script data of a plain file) and, for
    # a container, the data the container hands to the chunk
    size = len(rm.src)
    arg = getattr(getattr(self, 'carrier', None), 'chunk_arg', None)
    if arg and os.path.exists(arg):
        size += os.path.getsize(arg)
    if not budget:
        # Instructions the init may run.  Inits seen take up to 2 instructions per byte of the file
        # where the init deserializes the script itself (v14.0 - v14.2: 2.36 million for 2.2 MB) and
        # 11 to 32 per byte of data where it decrypts it (v14.8, v14.9: 82 million for 2.5 MB); a
        # failed check never ends.
        budget = max(12000000, 60 * size)
    if not timeout:
        # (time for the budget at 400,000 counted instructions a second, which is what a busy
        # machine does, plus the dump: every lazy constant of the script is decoded, 25 to 50 s per
        # megabyte)
        timeout = max(150, budget // 400000 + 50 * size // 1000000)
    text, notes = vmmodel.run_dump_script(rm, self.workdir, self.tag, options, budget=budget, timeout=timeout)
    self.run_notes = notes
    for n in notes:
        self.log('[%s]   %s' % (self.tag, n))
    if text is None:
        return None
    fid_map = getattr(self, 'fid_map', None)
    if fid_map is not None:
        text = re.sub(r'"fid":(\d+)', lambda x: '"fid":%d' % fid_map.get(int(x.group(1)), 10 ** 6), text)
    return json.loads(text)


Stage.dump_script = _dump_script


def _last_calls(self, options=(), budget=12000000, timeout=120):
    """After a hand-over that did not happen: the same run once more with call logging, to say
    which mocked calls the init made last (the probe it failed is usually among them).
    Returns one line, or None when that run told nothing."""
    rm = getattr(self, 'run_model', None) or self.model
    try:
        _text, notes = vmmodel.run_dump_script(rm, self.workdir, self.tag + '.calls', options, budget=budget, timeout=timeout, calls=True)
    except Exception:
        return None
    for n in notes:
        if n.startswith('[calls] '):
            return n[len('[calls] '):]
    return None


Stage.last_calls = _last_calls


def _run_script(self, options=(), budget=None, timeout=None):
    """dump_script, then a plain lift of the script's program (no key-function search, no
    opaque-predicate cuts: run.deobfuscate does those).  Returns the decompiled text or None."""
    import re
    raw = self.dump_script(options, budget=budget, timeout=timeout)
    if raw is None:
        return None
    m = self.model
    self.dump = pe.Dump(raw, m)
    self.lifter = lifter.TableProtoLifter(m, self.dump)
    protos = self.lifter.lift_all(self.lifter.root_key)
    nodes = sum(len(p.nodes) for p in protos.values())
    errs = [(k, e) for k, p in protos.items() for e in p.errors]
    conf = [(k, c) for k, p in protos.items() for c in p.conflicts]
    self.log('[%s] script: lifted %d protos, %d instructions, %d errors, %d temp conflicts' % (
        self.tag, len(protos), nodes, len(errs), len(conf)))
    self.protos = protos
    self.errors = errs
    self.conflicts = conf
    d = structure.Decompiler(protos, self.lifter.root_key, self.native_text)
    self.text = d.run()
    self.stats = d.stats
    self.log('[%s] decompiled: %d lines, %d unstructured' % (self.tag, self.text.count('\n'), d.stats['unstructured']))
    return self.text


Stage.run_script = _run_script


def _poke_list(self):
    """Lines for the harness's poke pass: every instruction of the payload that was lifted but
    never ran, with the interpreter state it is reached in (entry pc, dispatch mode, keys).

    The interpreter copies some factory parameters into locals at its top (`local pc = argN`,
    `local key = argM[1]`); the lifter's snapshot at an instruction holds the values of those
    locals, which is what the parameters have to be for execution to start right there."""
    if not getattr(self, 'payload_root', None):
        return []
    m, L = self.model, self.lifter
    out = []
    for pid, pir in self.protos.items():
        if not isinstance(pid, int):
            continue
        idx, env = L.records[pid - 1]
        it = m.interps[idx]
        params = set(it.chain[-3].params)
        direct, keyed = {}, {}
        for st in it.func.body:
            if st[0] != 'local' or len(st[1]) != 1 or len(st[2]) != 1:
                continue
            e = st[2][0]
            if e[0] == 'name' and e[1] in params and pe.is_num(env.get(e[1])):
                direct[st[1][0]] = e[1]
            elif e[0] == 'index' and e[1][0] == 'name' and e[1][1] in params and e[2][0] == 'num':
                keyed[st[1][0]] = (e[1][1], int(e[2][1]))
        # Only the frontier: instructions that did not run but directly follow one that did (the
        # arm of a branch that was not taken).  Anything further away may still be ciphertext,
        # and executing ciphertext corrupts the shared code and constant buffers.
        frontier = set()
        for key2, node in pir.nodes.items():
            if (pid, key2[0], key2[1]) not in self.oracle:
                continue
            for e in lifter.tree_ends(node.tree):
                if e[0] == 'goto' and (pid, e[1][0], e[1][1]) not in self.oracle:
                    frontier.add(e[1])
        for key2, node in pir.nodes.items():
            if key2 not in frontier or node.error:
                continue
            snap = node.snap_in
            fields = []
            ok = True
            for var, param in direct.items():
                v = snap.get(var)
                if not pe.is_num(v) or v != int(v):
                    ok = False
                    break
                fields.append('%s=%d' % (param, int(v)))
            for var, (param, i) in keyed.items():
                v = snap.get(var)
                if not pe.is_num(v) or v != int(v):
                    ok = False
                    break
                fields.append('%s.%d=%d' % (param, i, int(v)))
            if ok and fields:
                out.append('%d;%s' % (pid, ';'.join(fields)))
    return sorted(set(out))


Stage.poke_list = _poke_list


def _native_text(self, line, upnames, level):
    fs = self.model.func_by_line.get(line, [])
    if len(fs) != 1:
        return None
    return luprint.native_closure_text(fs[0], upnames, level, self.model.cells, self.model.up_base)


Stage.native_text = _native_text


def trace(chain, outpath):
    luau = vmmodel.find_luau()
    r = subprocess.run([luau, os.path.join(HERE, 'harness.lua'), '-a', chain[0], outpath] + list(chain[1:]),
                       capture_output=True, text=True, timeout=900)
    if not os.path.exists(outpath):
        return None, r.stderr
    with open(outpath, encoding='latin-1') as f:
        return f.read().split('\n'), r.stderr
