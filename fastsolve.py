#!/usr/bin/env python3
"""fastsolve.py <target.lua> [outdir] [--arg FILE]... [--no-call] [--timeout SEC]
两段式快速解：
  A) 2 秒：buffer 模拟跑一遍，dump 所有解码后的缓冲区 —— 能拿到外层 loader 解出来的下一层源码
  B) 可选：DEOB_FAST=1 + DEVIRT_TIME_BUDGET 跑 deob.js 做符号执行（慢，但那是唯一的"源码"来源）
只用 A，不碰符号执行。

Runs the target in the sandbox that mkfast.py builds (a global table of its own, see
luasandbox.py: Luau's libraries only, no file access, no require; what the target loads is
captured and not run) and writes into outdir:

  chunk<n>.lua            code the target handed to loadstring/load (n-th call); for a Luraph
                          container this is the VM chunk.  The code is NOT run.
  chunk<n>.arg<k>.bin     k-th argument (a buffer or a string) of the first call of that chunk:
                          the data buffer the container starts the VM chunk with
  chunk<n>.name.txt       the chunk name given to loadstring ("Luraph"); no file when none was given
  blob<i>.bin             every buffer of at least 48 bytes that existed at the end
  layer<i>.lua            the same, for buffers that look like Lua source
  _out.txt                raw log of the run ([TAG] lines of the runner; what the target
                          itself printed is in "[OUT] ..." lines and is never parsed)

--arg / --no-call are passed on to mkfast.py (start an inner chunk with its buffer).
"""
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import luaubin      # noqa: E402


def run_capture(target, workdir, stub=False, minlen=48, arg_files=(), call_result=True,
                timeout=300):
    """Returns (log text, seconds, timed out?)."""
    luau = luaubin.find_luau()
    if not luau:
        raise RuntimeError('找不到 luau 运行时；' + luaubin.MISSING_HINT)
    import mkfast
    runner = os.path.join(workdir, '_runner.lua')
    try:
        with open(target, 'rb') as fh:
            src = fh.read().decode('latin-1')
        text, _pieces = mkfast.build(src, str(minlen), stub, arg_files, call_result)
    except (OSError, ValueError) as e:
        raise RuntimeError('mkfast.build failed: %s' % e) from None
    with open(runner, 'wb') as fh:
        fh.write(text.encode('latin-1'))
    t0 = time.time()
    timed_out = False
    out_path = os.path.join(workdir, '_out.txt')
    with open(out_path, 'wb') as fh:
        try:
            subprocess.run([luau, runner], stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    dt = time.time() - t0
    with open(out_path, 'rb') as fh:
        txt = fh.read().decode('latin-1')
    return txt, dt, timed_out


def read_varint(d, o):
    v, sh = 0, 0
    while o < len(d) and sh < 35:
        b = d[o]; o += 1; v |= (b & 0x7F) << sh; sh += 7
        if not (b & 0x80): break
    return v, o


def bc_strings(data):
    """尽力从 Luau 字节码里抠字符串表；两种 header 长度都试，取解析最干净的。"""
    best = None
    for skip in (2, 3):
        try:
            cnt, o = read_varint(data, skip)
            if not (0 < cnt < 5000): continue
            start, out = o, []
            for _ in range(cnt):
                ln, o = read_varint(data, o)
                if ln < 0 or o + ln > len(data): raise ValueError
                if ln: out.append(data[o:o+ln].decode('utf-8', 'replace'))
                o += ln
            if best is None or o - start > best[0]:
                best = (o - start, out)
        except Exception:
            continue
    return best[1] if best else []


def classify(data):
    if not data:
        return 'empty'
    head = data[:400]
    printable = sum(1 for b in head if 32 <= b < 127 or b in (9, 10, 13))
    ratio = printable / max(1, len(head))
    if ratio > 0.9 and re.match(rb'^\s*(--|return|local|function|\w+\s*=)', head):
        return 'lua-source'
    if data[:3] == b'\x1bLua' or (len(data) > 1 and data[0] < 32 and data[0] > 0 and data[1] < 32):
        return 'bytecode?'
    if ratio > 0.85:
        return 'encoded-ascii'
    return 'binary'


def solve(target, outdir, arg_files=(), call_result=True, timeout=300, say=print):
    """Run the target and write what it loads / decodes into outdir.

    Returns a dict:
      'chunks'   [{'n', 'path', 'size', 'name' (None: none given), 'args': [{'k', 'kind', 'path', 'size'}]}]
      'blobs'    [{'i', 'path', 'size', 'kind', 'layer': path or None}]
      'error'    first line of the run-time error that ended the run, or None
      'prelude'  None, or {'bytes': n, 'ok': bool} for text in front of the Luraph header
      'timeout'  True when the run was stopped after `timeout` seconds
      'seconds'  run time
    """
    os.makedirs(outdir, exist_ok=True)
    txt, dt, timed_out = run_capture(target, outdir, arg_files=arg_files,
                                     call_result=call_result, timeout=timeout)
    res = {'chunks': [], 'blobs': [], 'error': None, 'prelude': None,
           'timeout': timed_out, 'seconds': dt}
    m = re.search(r'^\[PRELUDE\] bytes=(\d+) ok=(\w+)(?: err=(.*))?', txt, re.M)
    if m:
        res['prelude'] = {'bytes': int(m.group(1)), 'ok': m.group(2) == 'true',
                          'error': m.group(3)}
        say('    前导代码 %s 字节（Luraph 头注释之前）：%s' % (
            m.group(1), '已执行' if m.group(2) == 'true' else '执行失败，已跳过：' + (m.group(3) or '')[:90]))
    m = re.search(r'^\[RUNERR\] (.+)', txt, re.M)
    if m:
        res['error'] = m.group(1)
        say(f'[!] 运行时中断: {m.group(1)[:110]}')
    if timed_out:
        say(f'[!] 超时（{timeout} 秒），已终止')

    loads = {int(n): (int(ln), None if nh == '-' else bytes.fromhex(nh).decode('latin-1'))
             for n, ln, nh in re.findall(r'^\[LOAD (\d+)\] len=(\d+) namehex=([0-9a-f]*|-)$', txt, re.M)}
    args = {}
    for n, k, kind in re.findall(r'^\[ARG (\d+)\.(\d+)\] (\w+)', txt, re.M):
        args.setdefault(int(n), {})[int(k)] = kind
    arghex = {(int(n), int(k)): h
              for n, k, h in re.findall(r'^\[ARGHEX (\d+)\.(\d+)\] ([0-9a-f]*)', txt, re.M)}
    for n, hexs in re.findall(r'^\[LOADHEX (\d+)\] ([0-9a-f]*)', txt, re.M):
        n = int(n)
        data = bytes.fromhex(hexs)
        path = os.path.join(outdir, f'chunk{n}.lua')
        with open(path, 'wb') as fh:
            fh.write(data)
        name = loads.get(n, (0, None))[1]       # None: loadstring was called without a name
        if name is not None:
            with open(os.path.join(outdir, f'chunk{n}.name.txt'), 'wb') as fh:
                fh.write(name.encode('latin-1'))
        entry = {'n': n, 'path': path, 'size': len(data), 'name': name, 'args': []}
        for k, kind in sorted(args.get(n, {}).items()):
            a = {'k': k, 'kind': kind, 'path': None, 'size': 0}
            if (n, k) in arghex:
                adata = bytes.fromhex(arghex[(n, k)])
                a['path'] = os.path.join(outdir, f'chunk{n}.arg{k}.bin')
                a['size'] = len(adata)
                with open(a['path'], 'wb') as fh:
                    fh.write(adata)
            entry['args'].append(a)
        res['chunks'].append(entry)
        desc = ', '.join('%s %s' % (a['kind'], format(a['size'], ',') + ' B' if a['path'] else '')
                         for a in entry['args']) or '未被调用'
        shown = f'loadstring 名 "{name}"' if name is not None else 'loadstring 未给名字'
        say(f'    chunk{n}: {len(data):>8d} B  {shown}  参数: {desc}')

    for i, hexs in re.findall(r'^\[HEX (\d+)\] ([0-9a-f]+)', txt, re.M):
        data = bytes.fromhex(hexs)
        kind = classify(data)
        path = os.path.join(outdir, f'blob{i}.bin')
        with open(path, 'wb') as fh:
            fh.write(data)
        entry = {'i': int(i), 'path': path, 'size': len(data), 'kind': kind, 'layer': None}
        extra = ''
        if kind == 'lua-source':
            lp = os.path.join(outdir, f'layer{i}.lua')
            with open(lp, 'wb') as fh:
                fh.write(data)
            entry['layer'] = lp
            extra = f'  -> {os.path.basename(lp)}'
        res['blobs'].append(entry)
        say(f'    blob{i}: {len(data):>8d} B  {kind:<13s}{extra}')
        if kind in ('bytecode?', 'binary') and len(data) < 200000:
            strs = bc_strings(data)
            if strs:
                show = [s for s in strs if len(s) > 2][:6]
                # only a real string table is worth showing (random bytes parse as one too)
                if show and all(x.isprintable() and x.isascii() for x in show):
                    say('        字符串表: ' + ' | '.join(repr(x[:40]) for x in show))
    return res


def main():
    argv = sys.argv[1:]
    arg_files, pos = [], []
    call_result = True
    timeout = 300
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--arg' and i + 1 < len(argv):
            arg_files.append(argv[i + 1]); i += 2; continue
        if a == '--timeout' and i + 1 < len(argv):
            timeout = int(argv[i + 1]); i += 2; continue
        if a == '--no-call':
            call_result = False
        elif not a.startswith('--'):
            pos.append(a)
        i += 1
    if not pos:
        print(__doc__)
        return 1
    target = pos[0]
    if not os.path.isfile(target):
        print(f'[✗] 文件不存在：{target}')
        return 1
    outdir = pos[1] if len(pos) > 1 else os.path.join(
        os.getcwd(), 'fast_' + os.path.basename(target).split('.')[0])
    print(f'[*] {target}')
    try:
        res = solve(target, outdir, arg_files, call_result, timeout)
    except RuntimeError as e:
        sys.stderr.write(f'[fastsolve] {e}\n')
        return 3
    got = len(res['chunks']) + len(res['blobs'])
    print(f'[+] {len(res["chunks"])} 个 loadstring 块 + {len(res["blobs"])} 块 buffer / '
          f'{res["seconds"]:.1f} 秒  ->  {outdir}')
    return 0 if got else 4


if __name__ == '__main__':
    sys.exit(main())
