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
        d = structure.Decompiler(protos, self.lifter.root_key, self.native_text)
        self.text = d.run()
        self.stats = d.stats
        self.log('[%s] decompiled: %d lines, %d unstructured' % (self.tag, self.text.count('\n'), d.stats['unstructured']))
        return self.text


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
