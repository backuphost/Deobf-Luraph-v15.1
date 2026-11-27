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

from . import normalize, vmmodel

HERE = os.path.dirname(os.path.abspath(__file__))


def has_dispatch_loop(text):
    """True when the text holds a Luraph dispatch loop: the canonical head (a regex, cheap) or any
    other spelling of it (normalize.find_heads, the one place that knows them)."""
    return vmmodel.LOOP_HEAD_RE.search(text) is not None or normalize.count_heads(text) > 0


class Carrier:
    """A VM chunk plus the file that loads it."""

    def __init__(self, carrier, chunk_src, chunk_arg=None, chunk_name=None, synthetic=False, data_kind=None):
        self.carrier = carrier          # the file the harness executes
        self.chunk_src = chunk_src      # the chunk's original text (what the carrier loadstrings)
        self.chunk_arg = chunk_arg      # bytes of the data argument (file), if known
        self.chunk_name = chunk_name    # the name the chunk is loaded under ("Luraph" plus 0-4 spaces)
        self.synthetic = synthetic
        self.data_kind = data_kind      # 'buffer' | 'string' | None: how the container hands the data over

    def harness_options(self):
        out = ['carrier=' + self.carrier, 'chunksrc=' + self.chunk_src]
        if self.synthetic and self.chunk_arg:
            out.append('chunkarg=' + self.chunk_arg)
        return out


# The two shapes a container has: Luraph v14's `return(function()local ... end)()(...)` (a function
# without parameters; a VM in the fn-args layout takes its helpers as parameters, one in the table
# layout starts `return({`) and v15's `return setmetatable({...},{}):M()(...)`.
FN_SHAPE = re.compile(r'\breturn\s*\(\s*function\s*\(\s*\)')      # the v14 container
_SHAPE = re.compile(r'\breturn\s*\(\s*function\s*\(\s*\)|\breturn\s+setmetatable\s*\(')


def may_be_container(src):
    """Cheap test before running anything: the file can load code, has the outline of a container
    and holds no dispatch loop itself.  (A plain VM also mentions loadstring when the script uses
    it; that alone is no reason to run the container probe on it.)"""
    return (re.search(r'\bloadstring\b', src) is not None and _SHAPE.search(src) is not None
            and not has_dispatch_loop(src))


class Damaged(Exception):
    """The file is a compressed container that cannot be opened: cut off, or a payload is corrupt."""


def open_static(path, src, workdir, tag, log=print):
    """Open a compressed container without running it (devm/container_static.py, when the tree has
    it).  Returns a Carrier, or None when the text is no such container or the decoder is absent.
    Raises Damaged for a container that cannot be opened.

    The chunk and its data are written next to the other intermediate files; the run still
    executes the shipped file, which decodes the same text itself (see the module comment)."""
    try:
        from . import container_static
    except ImportError:
        return None
    try:
        res = container_static.decode(src)
    except container_static.ContainerError as e:
        raise Damaged(' '.join(str(e).split())[:300])
    if res is None:
        return None
    prefix = os.path.join(workdir, tag)
    chunk = prefix + '.chunk1.lua'
    with open(chunk, 'wb') as f:
        f.write(res.chunk)
    arg = None
    if res.data is not None:
        arg = prefix + '.chunk1.arg1.bin'
        with open(arg, 'wb') as f:
            f.write(res.data)
    log('[%s] Luraph container (%s), read without running it: a VM chunk of %d bytes, loaded under the chunk name "%s"%s' % (
        tag, res.codec, len(res.chunk), res.chunk_name,
        ' and called with (%s of %d bytes)' % (res.data_kind, len(res.data)) if res.data is not None else ''))
    for n in getattr(res, 'notes', None) or ():
        log('[%s]   %s' % (tag, n))
    log('[%s]   chunk saved as %s%s' % (tag, chunk, ', its data as ' + arg if arg else ''))
    return Carrier(os.path.abspath(path), chunk, arg, res.chunk_name, data_kind=res.data_kind)


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
        # the VM chunk: Luraph's container loads it under the name "Luraph" (plus padding); a chunk
        # under another name counts when a dispatch loop is found in it
        if not (name.startswith('Luraph') or has_dispatch_loop(text)):
            continue
        arg = None
        kind = None
        kinds = args.split(',') if args else []
        for i, k in enumerate(kinds):
            # (the data of the VM: a buffer from v14.7 on, a string in v14.6)
            if k.startswith(('buffer:', 'string:')):
                arg = '%s.chunk%s.arg%d.bin' % (prefix, n, i + 1)
                kind = k.split(':')[0]
                break
        log('[%s] Luraph container: it loads a VM chunk of %s bytes under the chunk name "%s" and calls it with (%s)' % (
            tag, size, name, ', '.join(k.replace(':', ' of ') + (' bytes' if ':' in k else '') for k in kinds)))
        log('[%s]   chunk saved as %s%s' % (tag, chunk, ', its %s as %s' % (kind, arg) if arg else ''))
        return Carrier(os.path.abspath(path), chunk, arg, name, data_kind=kind)
    return None


STAND_IN = (
    '-- stand-in for the Luraph container this VM chunk was cut out of (written by devm)\n'
    'return setmetatable({A=function(z)local s,b=__LPH_CARRY();local ok,f=pcall(loadstring,s,%s,nil);'
    'assert(ok and type(f)=="function",f);return(f(b));end},{}):A()(...);\n')


def stand_in(chunk_path, chunk_arg, workdir, tag, chunk_name='Luraph'):
    """Carrier for an already unwrapped chunk: same call shape as Luraph's container (the main
    chunk calls a method, the method compiles the chunk under its name, "Luraph" plus the padding
    the container used, and calls it with the buffer; the main chunk calls the result with `...`)."""
    if not re.fullmatch(r'Luraph {0,8}', chunk_name or ''):
        chunk_name = 'Luraph'
    path = os.path.join(workdir, tag + '.carrier.lua')
    with open(path, 'w') as f:
        f.write(STAND_IN % ('"%s"' % chunk_name))
    return Carrier(path, os.path.abspath(chunk_path), os.path.abspath(chunk_arg) if chunk_arg else None, chunk_name, synthetic=True,
                   data_kind='buffer' if chunk_arg else None)
