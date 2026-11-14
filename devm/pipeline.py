"""End-to-end driver: source -> (hook + dump) -> lift -> decompile, repeated per VM layer."""
import json
import os
import subprocess

from . import vmmodel, pe, lifter, structure, luprint

HERE = os.path.dirname(os.path.abspath(__file__))


class Stage:
    def __init__(self, src, workdir, tag, layers=(), log=print, simplify=True):
        self.simplify = simplify
        self.src = src
        self.workdir = workdir
        self.tag = tag
        self.layers = list(layers)
        self.log = log
        self.model = vmmodel.Model(src)
        self.patched_path = os.path.join(workdir, tag + '.patched.lua')

    def run(self):
        m = self.model
        self.log('[%s] interpreter at line %d, %d dispatch loop(s), %s' % (
            self.tag, m.interp.line, len(m.loops), 'factory' if m.factory_style else 'proto-table'))
        raw = vmmodel.run_dump(m, self.workdir, self.tag, self.layers)
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
        d = structure.Decompiler(protos, self.lifter.root_key, self.native_text, simplify=self.simplify)
        self.text = d.run()
        self.stats = d.stats
        self.log('[%s] decompiled: %d lines, %d unstructured' % (self.tag, self.text.count('\n'), d.stats['unstructured']))
        return self.text


def _native_text(self, line, upnames, level):
    fs = self.model.func_by_line.get(line, [])
    if len(fs) != 1:
        return None
    return luprint.native_closure_text(fs[0], upnames, level)


Stage.native_text = _native_text


def trace(chain, outpath, timeout=120):
    """Run the sample (optionally with lifted layers swapped in) and record its observable behaviour."""
    luau = vmmodel.find_luau()
    if os.path.exists(outpath):
        os.remove(outpath)
    err = ''
    try:
        r = subprocess.run([luau, os.path.join(HERE, 'harness.lua'), '-a', chain[0], outpath] + list(chain[1:]),
                           capture_output=True, text=True, timeout=timeout)
        err = r.stderr
    except subprocess.TimeoutExpired:
        err = 'timeout'
    if not os.path.exists(outpath):
        return None, err
    with open(outpath, encoding='latin-1') as f:
        return f.read().split('\n'), err


OBSERVABLE = ('call ', 'get ', 'set ', 'setglobal ', 'RESULT', 'task error', 'deferred error')


def verify(stage, lifted_path, workdir):
    """Run the VM and the recovered code under the same stub environment and compare the
    observable events (calls into / reads and writes of environment objects, final result).
    Returns (identical?, description)."""
    a, e1 = trace([stage.patched_path], os.path.join(workdir, stage.tag + '.orig.trace'))
    b, e2 = trace([stage.patched_path, lifted_path], os.path.join(workdir, stage.tag + '.lift.trace'))
    if a is None or b is None:
        return False, 'could not run (%s)' % ((e1 or e2 or '').strip()[-200:] or 'no trace produced')
    globals_a = [x for x in a if x and not x.startswith(OBSERVABLE)]
    a = [x for x in a if x.startswith(OBSERVABLE)]
    b = [x for x in b if x.startswith(OBSERVABLE)]
    note = '%d observable events, %d distinct globals read' % (len(a), len(set(globals_a)))
    if a == b:
        return True, 'identical behaviour (%s)' % note
    n = min(len(a), len(b))
    d = next((i for i in range(n) if a[i] != b[i]), n)
    return False, 'behaviour differs at event %d: original %r, recovered %r' % (
        d, a[d] if d < len(a) else '<end>', b[d] if d < len(b) else '<end>')
