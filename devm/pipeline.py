"""End-to-end driver: source -> (hook + dump) -> lift -> decompile, repeated per VM layer."""
import json
import os
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
    if forced:
        payload = [forced] if forced != root else []
    self.payload_root = payload[0] if payload else None
    if payload:
        root = payload[0]
        self.lifter.root_key = root
        self.log('[%s] payload: proto %d is the script (protos 1..%d are the bootstrap)' % (self.tag, root, root - 1))
    protos = self.lifter.lift_all(root)
    if vmmodel.SERVER is not None:
        # functions that never ran decrypt themselves with numbers their callers pass: lift those
        # again with the numbers the lifted callers show (see opaque.relift_params)
        from . import opaque
        protos = opaque.relift_params(self.lifter, protos, root, self.native_text,
                                      log=lambda msg: self.log('[%s] %s' % (self.tag, msg)))
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
    if payload and not forced:
        # The proto taken for the script is the first one on the second interpreter.  Where that
        # interpreter only runs a helper, next to nothing is lifted while the run shows the first
        # program doing all the work: say so (the choice itself is left alone, --root decides).
        ran_first = sum(1 for k in oracle if isinstance(k[0], int) and 0 < k[0] <= len(self.lifter.records)
                        and self.lifter.records[k[0] - 1][0] == boot)
        if nodes < 50 and ran_first > 1000:
            self.hints.append('[hint] the function taken for the script (proto %d, on the second interpreter) has only %d '
                              'instructions, while the first program executed %d: the script is probably the first '
                              'program; run again with --root %d' % (root, nodes, ran_first, first_root))
    d = structure.Decompiler(protos, self.lifter.root_key, self.native_text)
    self.text = d.run()
    self.stats = d.stats
    self.log('[%s] decompiled: %d lines, %d unstructured' % (self.tag, self.text.count('\n'), d.stats['unstructured']))
    return self.text


Stage.run_traced = _run_traced


def _dump_script(self, options=(), budget=None, timeout=150):
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
    if not budget:
        # Instructions the init may run.  Inits seen take up to 2 instructions per byte of the file
        # where the init deserializes the script itself (v14.0 - v14.2: 2.36 million for 2.2 MB) and
        # about 11 per byte of data where it decrypts it (v14.8); a failed check never ends.
        budget = max(12000000, 40 * len(rm.src))
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


def _run_script(self, options=(), budget=None, timeout=150):
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
