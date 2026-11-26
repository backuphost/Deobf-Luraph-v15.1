"""Luraph's compressed container, and how a VM chunk inside one is run.

Luraph (seen in v15.2; the v15.1 chunk examples/layer3.lua was cut out of the same kind of file)
can ship the VM as data:

    -- This file was protected using Luraph Obfuscator v15.2 [https://lura.ph/]
    return setmetatable({..., o=[=[LPH...]=], A=function(z) ... end, h=function(z,M,e) ... end, ...},{}):A()(...);

`A` base85-decodes two blobs, `h` decompresses them (an LZMA-style range decoder) into a Lua
source and a data buffer, the source is compiled with `pcall(loadstring, source, "Luraph", nil)`
and called with the buffer; what that returns is finally called with the script's `...`.  The
compiled source is the real VM object (`return setmetatable({...},{}):NAME(...)`).

The traced run executes the *shipped file* (the "carrier") unchanged and lets the harness's
loadstring compile the instrumented copy of the chunk where the script loads the original text
(harness.lua: carrier=, chunksrc=).  That keeps everything the VM can observe as it is in the real
thing: the Lua call depth of the chunk (main chunk -> A -> chunk -> NAME) and of the script closure
(main chunk -> closure), the chunk name in error positions and debug.info (`[string "Luraph"]`,
line 1), the chunk's `...` (the buffer) and whatever the owner put in front of the header (e.g.
`getgenv().SCRIPT_KEY = "..."`).  Running the chunk as the main function instead would put its
start-up two frames higher and lose the rest.

For a chunk that was cut out of its container earlier (the root tool's unwrap, --chunk-arg FILE)
a stand-in carrier with the same call shape is written.
"""
import os
import re
import subprocess

from . import vmmodel

HERE = os.path.dirname(os.path.abspath(__file__))

# `while true do [local] OP[, X] = CODE[PC]; if`: the head of a dispatch loop in Luraph's own layout
_HEAD = vmmodel.LOOP_HEAD_RE


class Carrier:
    """A VM chunk plus the file that loads it."""

    def __init__(self, carrier, chunk_src, chunk_arg=None, chunk_name=None, synthetic=False):
        self.carrier = carrier          # the file the harness executes
        self.chunk_src = chunk_src      # the chunk's original text (what the carrier loadstrings)
        self.chunk_arg = chunk_arg      # bytes of the buffer argument (file), if known
        self.chunk_name = chunk_name
        self.synthetic = synthetic

    def harness_options(self):
        out = ['carrier=' + self.carrier, 'chunksrc=' + self.chunk_src]
        if self.synthetic and self.chunk_arg:
            out.append('chunkarg=' + self.chunk_arg)
        return out


def may_be_container(src):
    """Cheap test before running anything: the file can load code, and holds no dispatch loop itself."""
    return re.search(r'\bloadstring\b', src) is not None and _HEAD.search(src) is None


def unwrap(path, workdir, tag, log=print, timeout=120):
    """Run `path` until it calls a chunk it has loaded with loadstring.

    Returns a Carrier for the first loaded chunk that contains a Luraph dispatch loop, or None."""
    prefix = os.path.join(workdir, tag)
    info = prefix + '.info'
    if os.path.exists(info):
        os.remove(info)
    try:
        r = subprocess.run([vmmodel.find_luau(), os.path.join(HERE, 'unwrap.lua'), '-a', path, prefix],
                           capture_output=True, text=True, errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        log('[%s] container probe: no chunk loaded within %d s' % (tag, timeout))
        return None
    for line in r.stderr.split('\n'):
        if line.startswith('[unwrap]'):
            log('[%s] container probe: %s' % (tag, line[len('[unwrap] '):]))
    if not os.path.exists(info):
        return None
    with open(info, encoding='latin-1') as f:
        rows = [l.split('\t') for l in f.read().split('\n') if l]
    for n, name, size, args in rows:
        chunk = '%s.chunk%s.lua' % (prefix, n)
        with open(chunk, encoding='latin-1') as f:
            text = f.read()
        if not _HEAD.search(text):
            continue
        arg = None
        kinds = args.split(',') if args else []
        for i, k in enumerate(kinds):
            if k.startswith('buffer:'):
                arg = '%s.chunk%s.arg%d.bin' % (prefix, n, i + 1)
                break
        log('[%s] Luraph container: it loads a VM chunk of %s bytes under the chunk name "%s" and calls it with (%s)' % (
            tag, size, name, ', '.join(k.replace(':', ' of ') + (' bytes' if ':' in k else '') for k in kinds)))
        log('[%s]   chunk saved as %s%s' % (tag, chunk, ', its buffer as ' + arg if arg else ''))
        return Carrier(os.path.abspath(path), chunk, arg, name)
    return None


STAND_IN = (
    '-- stand-in for the Luraph container this VM chunk was cut out of (written by devm)\n'
    'return setmetatable({A=function(z)local s,b=__LPH_CARRY();local ok,f=pcall(loadstring,s,"Luraph",nil);'
    'assert(ok and type(f)=="function",f);return(f(b));end},{}):A()(...);\n')


def stand_in(chunk_path, chunk_arg, workdir, tag):
    """Carrier for an already unwrapped chunk: same call shape as Luraph's container (the main
    chunk calls a method, the method compiles the chunk as "Luraph" and calls it with the buffer,
    the main chunk calls the result with `...`)."""
    path = os.path.join(workdir, tag + '.carrier.lua')
    with open(path, 'w') as f:
        f.write(STAND_IN)
    return Carrier(path, os.path.abspath(chunk_path), os.path.abspath(chunk_arg) if chunk_arg else None, 'Luraph', synthetic=True)
