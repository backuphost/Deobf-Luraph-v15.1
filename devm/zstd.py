"""Zstandard decompression for the devirtualizer (no third-party package).

    from devm import zstd
    data = zstd.decompress(frames)          # bytes -> bytes; one or more frames, skippable frames allowed

Luraph v14.7 can leave the inflating of its payloads to Roblox (`EncodingService:DecompressBuffer(buf,
Enum.CompressionAlgorithm.Zstd)`): 235 compressed containers of the public corpus do, and so do 135 plain
VMs.  Two back ends, tried in this order:

* the system's libzstd through ctypes (streaming API, so a frame without a content size is no problem);
* `devm/zstd.lua`, the pure-Luau decoder the Roblox mock uses, run with the repository's `bin/luau`
  on temporary files (slower: see devm/zstd.lua for numbers).

`decompress(data, backend="lua")` forces the second one; `backend()` says which one is in use.
Both raise `ZstdError` (a ValueError) on anything that is not a complete, valid sequence of frames.
Dictionaries are not supported by either.

    python3 -m devm.zstd IN OUT [--lua]     decompress a file
"""
import ctypes
import ctypes.util
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ZSTD_LUA = os.path.join(HERE, 'zstd.lua')
MAGIC = b'\x28\xb5\x2f\xfd'
LIMIT = 1 << 30                 # refuse to produce more than this (a few bytes of zstd can announce gigabytes)
LUA_TIMEOUT = 600

__all__ = ['decompress', 'backend', 'ZstdError', 'MAGIC']


class ZstdError(ValueError):
    pass


class _Buf(ctypes.Structure):       # ZSTD_inBuffer and ZSTD_outBuffer have the same layout
    _fields_ = [('ptr', ctypes.c_void_p), ('size', ctypes.c_size_t), ('pos', ctypes.c_size_t)]


_lib = None                         # None = not tried, False = not available


def _libzstd():
    global _lib
    if _lib is None:
        _lib = False
        if os.environ.get('DEVM_NO_LIBZSTD'):
            return _lib
        names = [ctypes.util.find_library('zstd'), 'libzstd.so.1', 'libzstd.so', 'libzstd.1.dylib', 'libzstd.dll']
        for name in names:
            if not name:
                continue
            try:
                lib = ctypes.CDLL(name)
                lib.ZSTD_createDStream.restype = ctypes.c_void_p
                lib.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
                lib.ZSTD_freeDStream.restype = ctypes.c_size_t
                lib.ZSTD_initDStream.argtypes = [ctypes.c_void_p]
                lib.ZSTD_initDStream.restype = ctypes.c_size_t
                lib.ZSTD_decompressStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Buf), ctypes.POINTER(_Buf)]
                lib.ZSTD_decompressStream.restype = ctypes.c_size_t
                lib.ZSTD_DStreamOutSize.restype = ctypes.c_size_t
                lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
                lib.ZSTD_isError.restype = ctypes.c_uint
                lib.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
                lib.ZSTD_getErrorName.restype = ctypes.c_char_p
            except (OSError, AttributeError):
                continue
            _lib = lib
            break
    return _lib


def backend():
    """'libzstd' or 'lua' (which decoder `decompress` uses by default), or None when neither can run."""
    if _libzstd():
        return 'libzstd'
    try:
        _luau()
    except ZstdError:
        return None
    return 'lua'


def _lib_decompress(lib, data, limit):
    ds = lib.ZSTD_createDStream()
    if not ds:
        raise ZstdError('zstd: libzstd could not create a decompression stream')
    try:
        lib.ZSTD_initDStream(ds)
        src = ctypes.create_string_buffer(data, len(data))
        osize = max(int(lib.ZSTD_DStreamOutSize()), 1 << 17)
        dst = ctypes.create_string_buffer(osize)
        inb = _Buf(ctypes.cast(src, ctypes.c_void_p), len(data), 0)
        outb = _Buf(ctypes.cast(dst, ctypes.c_void_p), osize, 0)
        parts = []
        total = 0

        def step():
            nonlocal total
            outb.pos = 0
            ret = lib.ZSTD_decompressStream(ds, ctypes.byref(outb), ctypes.byref(inb))
            if lib.ZSTD_isError(ret):
                raise ZstdError('zstd: ' + lib.ZSTD_getErrorName(ret).decode('ascii', 'replace'))
            if outb.pos:
                total += outb.pos
                if total > limit:
                    raise ZstdError('zstd: the data expands to more than %d bytes' % limit)
                parts.append(ctypes.string_at(dst, outb.pos))
            return ret                          # 0: a frame is complete and all of it was handed out

        while inb.pos < inb.size:
            ret = step()
            while ret != 0 and inb.pos >= inb.size:
                # all input is read and the frame is not finished: output is still waiting, or the input is cut off
                if outb.pos < outb.size:
                    raise ZstdError('zstd: the input ends inside a frame')
                ret = step()
        return b''.join(parts)
    finally:
        lib.ZSTD_freeDStream(ds)


def _luau():
    here = os.path.dirname(HERE)
    try:
        if here not in sys.path:
            sys.path.insert(0, here)
        import luaubin
        exe = luaubin.find_luau()
    except Exception:       # noqa: BLE001  (luaubin is the front end's module; devm also runs without it)
        exe = None
    if not exe:
        import platform
        import shutil
        for cand in (os.environ.get('LUAU_BIN'), os.path.join(here, 'bin', 'luau'),
                     os.path.join(here, 'bin', 'luau.' + platform.machine()), shutil.which('luau')):
            if cand and os.path.isfile(cand):
                exe = cand
                break
    if not exe:
        raise ZstdError('zstd: neither libzstd nor the Luau interpreter (bin/luau) is available')
    if not os.path.isfile(ZSTD_LUA):
        raise ZstdError('zstd: devm/zstd.lua is missing')
    return exe


def _lua_decompress(data, limit, timeout=LUA_TIMEOUT):
    exe = _luau()
    with tempfile.TemporaryDirectory(prefix='devm-zstd-') as tmp:
        src, dst = os.path.join(tmp, 'in.zst'), os.path.join(tmp, 'out.bin')
        with open(src, 'wb') as f:
            f.write(data)
        try:
            r = subprocess.run([exe, ZSTD_LUA, '-a', src, dst, str(limit)], capture_output=True, text=True,
                               errors='replace', timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ZstdError('zstd: devm/zstd.lua did not finish within %d s' % timeout) from None
        if r.returncode != 0 or not os.path.exists(dst):
            msg = [l for l in (r.stderr + r.stdout).split('\n') if 'zstd: ' in l]
            raise ZstdError(msg[0][msg[0].index('zstd: '):].strip() if msg else
                            'zstd: devm/zstd.lua failed: ' + (r.stderr or r.stdout).strip()[:300])
        with open(dst, 'rb') as f:
            return f.read()


def decompress(data, limit=LIMIT, backend=None):
    """The bytes the zstd frame(s) in `data` hold.  ZstdError when `data` is not exactly a sequence of frames."""
    data = bytes(data)
    lib = _libzstd() if backend in (None, 'libzstd') else None
    if backend == 'libzstd' and not lib:
        raise ZstdError('zstd: libzstd is not available')
    if lib:
        return _lib_decompress(lib, data, limit)
    return _lua_decompress(data, limit)


def main(argv):
    lua = '--lua' in argv
    args = [a for a in argv if not a.startswith('--')]
    if len(args) != 2:
        sys.stderr.write(__doc__)
        return 2
    with open(args[0], 'rb') as f:
        data = f.read()
    try:
        out = decompress(data, backend='lua' if lua else None)
    except ZstdError as e:
        sys.stderr.write('%s\n' % e)
        return 1
    with open(args[1], 'wb') as f:
        f.write(out)
    print('%d -> %d bytes (%s)' % (len(data), len(out), 'lua' if lua else backend()))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
