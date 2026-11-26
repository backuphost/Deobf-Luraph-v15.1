#!/usr/bin/env python3
"""Does the static tool's output still mean what its input means?

    python3 tests/static_roundtrip.py                 # examples/layer3.lua + examples/v15.2/sample.luau
    python3 tests/static_roundtrip.py FILE [FILE...]  # your own inputs
    python3 tests/static_roundtrip.py --corpus        # also: formatter over every Lua file of the repo

For every input, deob.py is run twice (as a subprocess, exactly as a user runs it) and its
output file is checked from the outside, without using the tool's own bookkeeping:

  --no-rename   Every extracted part must be the input function token for token.  The part is
                cut out of the input at the offsets printed in the output ("原始偏移 s - e"),
                both texts are lexed, and the two token lists are compared.  Whitespace and
                comments are the only things allowed to differ; number and string literals are
                compared by value (the tool rewrites 0x10 as 16 and "\\65" as "A"), and the
                number of tokens whose spelling changed is reported.
  default       (renaming and slot comments on)  Every part is parsed with luau-ast and
                compared with the tree of the input function: the trees must be identical up
                to a consistent renaming of local variables.
  both          the output compiles (luau's loadstring on the text; nothing is executed).

An input whose script is virtualized (deob.py exits 4, e.g. the v15.2 sample) is run with
--force, which exports the small functions that were not virtualized (or, when there are
none, the VM's own big functions): the same checks apply to them.

What is exported is checked against the input as well: every outermost function of the VM
object that reads a global, has the closure-factory shape `function(..)return function(..)`
or mentions the business API five times must be among the parts (dispatch loops aside), every
function that is left out must read no global at all (counted here on the luau-ast tree, not
taken from the tool's log), and every `HttpGet` of the input must be in the output.

The labels are checked too: a part called a VM dispatcher must be mostly dispatch loop, the
number of globals / API mentions a label gives is counted again here, and the number of
dispatch loops the tool reports must be the number of loop heads in the text (the loops of
the script's own functions are not interpreter loops).

--corpus formats every .lua / .luau file under the repository with the formatter alone and
checks that the result lexes back to the same tokens (comments included) and that luau still
compiles it when it compiled the original; every file that compiles is also renamed and must
parse to the same tree up to local names.

Needs bin/luau and bin/luau-ast (./setup.sh).  Exit status 0 when every check passes.
"""
import os
import re
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'libs'))
sys.path.insert(0, ROOT)
import deobf_plus as dp        # noqa: E402
import luascope                # noqa: E402
import luaubin                 # noqa: E402

PART_HEAD = re.compile(r'^-- 第 (\d+) 段  原始偏移 (\d+) - (\d+)  \(', re.M)
DEFAULT_INPUTS = ['examples/layer3.lua', 'examples/v15.2/sample.luau']


def read(path):
    with open(path, 'rb') as fh:
        return fh.read().decode('utf-8', errors='surrogateescape')


def compiles(luau, text):
    """Compile only (the text is handed to loadstring as a string; nothing of it runs)."""
    import luasandbox
    return luasandbox.compile_only(text, luau, timeout=300)


def run_deob(inp, out, work, extra):
    cmd = [sys.executable, '-B', os.path.join(ROOT, 'deob.py'), '--engine', 'static', inp, '-o', out, '--work', work] + extra
    t = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    return r.returncode, r.stdout + r.stderr, time.time() - t


def parts_of(out_text):
    """[(part number, start offset, end offset, text of the part)] from an output file."""
    heads = list(PART_HEAD.finditer(out_text))
    parts = []
    for k, m in enumerate(heads):
        seg_end = heads[k + 1].start() if k + 1 < len(heads) else out_text.rindex('\nreturn {')
        body = out_text[m.end():seg_end]
        marker = 'local __part%s = ' % m.group(1)
        body = body[body.index(marker) + len(marker):]
        # drop the separator line of the next section
        body = re.sub(r'\n-- =+\s*$', '', body.rstrip())
        parts.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), body))
    return parts


def global_names(node):
    """Names of the globals used below an AST node (own walk, independent of the tool)."""
    names = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            if n.get('type') == 'AstExprGlobal':
                names.add(n.get('global'))
            stack.extend(n.values())
        elif isinstance(n, list):
            stack.extend(n)
    return names


FACTORY_TEXT = re.compile(r'function\s*\([^()]*\)\s*return\s+function\s*\(')
# head of an interpreter dispatch loop, as text (the tool counts them on the syntax tree):
# opcode fetched from an array by a program counter, then compared with a number
LOOP_HEAD = re.compile(r'\bwhile true do (?:local )?(\w+)(?:,\w+)*=[A-Za-z_]\w*\[[A-Za-z_]\w*\];if \1(?:[<>]=?|==|~=)\d')


def source_of(inp, work):
    """The text deob.py cut the parts out of: the input itself, or the chunk it unwrapped."""
    chunk = os.path.join(work, os.path.splitext(os.path.basename(inp))[0] + '.chunk.lua')
    return read(chunk) if os.path.exists(chunk) else read(inp)


def check_input(inp, luau, ast, report):
    name = os.path.relpath(inp, ROOT) if inp.startswith(ROOT) else inp
    with tempfile.TemporaryDirectory(prefix='static_rt_') as tmp:
        # ---------------------------------------------------------------- --no-rename
        out1 = os.path.join(tmp, 'plain.lua')
        rc, log, dt = run_deob(inp, out1, tmp, ['--no-rename'])
        extra = []
        if rc == 4:
            extra = ['--force']
            rc, log, dt = run_deob(inp, out1, tmp, ['--no-rename'] + extra)
            report(name + ': virtualized, checked with --force', True)
        report('%s: deob.py --no-rename exits 0 (%.0fs)' % (name, dt), rc == 0,
               log.strip().splitlines()[-1] if log.strip() else '')
        if rc != 0 or not os.path.exists(out1):
            return
        src = source_of(inp, tmp)
        text1 = read(out1)
        parts = parts_of(text1)
        report(name + ': output has parts', bool(parts))
        root0, _e0 = luascope.parse(src, ast)
        idx0 = luascope.LineIndex(src)
        node_at = {idx0.span(f['location']): f for f in luascope.collect_functions(root0)}
        ok, msg = compiles(luau, text1)
        report(name + ': --no-rename output compiles', ok, msg)
        total = spelled = 0
        for num, s, e, body in parts:
            frag = src[s:e]
            a, b = dp.tokenize(frag), dp.tokenize(body)
            a = [t for t in a if t[0] != 'c']
            b = [t for t in b if t[0] != 'c']
            same = len(a) == len(b) and all(dp.canon(x) == dp.canon(y) for x, y in zip(a, b))
            diff = dp.first_token_difference([dp.canon(t) for t in a], [dp.canon(t) for t in b])
            report('%s: part %d is the input function token for token (%d tokens)'
                   % (name, num, len(a)), same, 'first difference: %r' % (diff,))
            total += len(a)
            spelled += sum(1 for x, y in zip(a, b) if x != y)
            report('%s: part %d starts at a function and ends at its end' % (name, num),
                   frag.startswith('function') and frag.rstrip().endswith('end'))
            # the heading's label is true: what it calls business code mentions the API,
            # and a part with a dispatch loop head is not called business code
            label = re.search(r'^-- 第 %d 段 .*\)  (.*)$' % num, text1, re.M)
            napi = len(re.findall(r'GetService|Instance\s*[.:]\s*new|UDim2|Color3|TweenService', frag))
            lab = label.group(1) if label else ''
            globs = global_names(node_at[(s, e)]) if (s, e) in node_at else None
            # loops of the interpreter's shape in this part, found here on the text
            heads = list(LOOP_HEAD.finditer(frag))
            extra_loop = re.search(r'；另含 (\d+) 个分派循环形态的循环（占函数 \d+%）$', lab)
            core = lab[:extra_loop.start()] if extra_loop else lab
            both = re.match(r'业务 API (\d+) 处，读全局 (\d+) 个$', core)
            named = re.match(r'读全局 (.*?)(?: 等 (\d+) 个)?（未见业务 API 名）$', core)
            api_only = re.match(r'业务 API 名 (\d+) 处，不读全局（', core)
            disp = re.match(r'VM 分派器（(\d+) 个分派循环，占函数 (\d+)%），不是业务源码$', core)
            good = bool(label) and globs is not None and (
                (bool(both) and int(both.group(1)) == napi > 0 and int(both.group(2)) == len(globs) > 0)
                or (bool(named) and napi == 0 and bool(globs)
                    and set(named.group(1).split(', ')) <= globs
                    and (named.group(2) is None or int(named.group(2)) == len(globs)))
                or (bool(api_only) and int(api_only.group(1)) == napi > 0 and not globs)
                or (core.startswith('闭包工厂形态，不读全局') and napi == 0 and not globs
                    and bool(FACTORY_TEXT.match(frag)))
                or (core.startswith('未见业务 API，不读全局') and napi == 0 and not globs)
                or (bool(disp) and int(disp.group(1)) == len(heads) > 0 and int(disp.group(2)) > 50))
            # what the label says about dispatch loops is what the text shows
            good = good and (bool(disp) or (int(extra_loop.group(1)) if extra_loop else 0) == len(heads))
            report('%s: part %d label %r matches its content (%d API mentions, %d globals, %d loop heads)'
                   % (name, num, lab, napi, len(globs or ()), len(heads)), good)
        report('%s: %d tokens compared, %d literals respelled, 0 changed in value'
               % (name, total, spelled), True)
        said_loops = re.search(r'(\d+) 个解释器分派循环', log)
        if said_loops:
            report('%s: the VM block says %s dispatch loops; the text has %d loop heads'
                   % (name, said_loops.group(1), len(LOOP_HEAD.findall(src))),
                   int(said_loops.group(1)) == len(LOOP_HEAD.findall(src)))
        if not extra:
            # nothing with business API calls is left out silently: every mention of the API
            # names in the source is inside an exported part or counted in the log
            api = re.compile(r'GetService|Instance\s*[.:]\s*new|UDim2|Color3|TweenService')
            total_api = len(api.findall(src))
            inside = sum(len(api.findall(src[s:e])) for _n, s, e, _b in parts)
            m = re.search(r'不导出、但出现业务 API 名的地方：(.*)', log)
            said = sum(int(x) for pair in re.findall(r'共 (\d+) 处|函数之外 (\d+) 处', m.group(1))
                       for x in pair if x) if m else 0
            report('%s: %d of %d business-API mentions are in the exported parts, the log counts the other %d'
                   % (name, inside, total_api, total_api - inside), said == total_api - inside,
                   'the log says %d' % said)
            # ... and no function that is left out is script code by any of the three marks
            spans = sorted(node_at, key=lambda x: (x[0], -x[1]))
            outer = []
            for sp in spans:
                if outer and sp[0] >= outer[-1][0] and sp[1] <= outer[-1][1]:
                    continue
                outer.append(sp)
            exported = {(s, e) for _n, s, e, _b in parts}
            dropped = {tuple(int(x) for x in mm) for mm in
                       re.findall(r'^\s+(\d+) - (\d+)\s+[\d,]+ 字节  （查表跳转', log, re.M)}
            # what was dropped as a VM dispatcher is one by its text: from its first dispatch
            # loop head (found here with a regular expression) to its end is most of the
            # function; no exported part has such a head at all
            def loop_share(a, b):
                hs = [m.start() for m in LOOP_HEAD.finditer(src, a, b)]
                return (len(hs), (b - hs[0]) / float(b - a)) if hs else (0, 0.0)
            report('%s: each of the %d functions dropped as dispatchers is mostly dispatch loop'
                   % (name, len(dropped)), all(loop_share(a, b)[1] > 0.5 for a, b in dropped),
                   str([(sp, loop_share(*sp)) for sp in dropped]))
            report('%s: no exported part has a dispatch loop head' % name,
                   all(loop_share(a, b)[0] == 0 for a, b in exported),
                   str([(sp, loop_share(*sp)) for sp in exported if loop_share(*sp)[0]]))
            # the loop count the tool reports (deob.probe) is the number of those heads: the
            # loops of the script's own functions are not interpreter loops
            import deob as deob_mod
            info = deob_mod.probe(inp, work_dir=os.path.join(tmp, 'probe'))
            heads_all = len(LOOP_HEAD.findall(src))
            in_dropped = sum(loop_share(a, b)[0] for a, b in dropped)
            report('%s: probe() reports %d dispatch loops; the text has %d loop heads, %d of them in the '
                   'dropped dispatchers' % (name, info['vm']['dispatch_loops'], heads_all, in_dropped),
                   info['vm']['dispatch_loops'] == heads_all == in_dropped > 0)
            left = [sp for sp in outer if sp not in exported and sp not in dropped]
            missed = [sp for sp in left if len(api.findall(src[sp[0]:sp[1]])) >= 5]
            report('%s: %d outermost functions; none with 5 or more API mentions is left out '
                   '(dispatchers aside: %d)' % (name, len(outer), len(dropped)), not missed, str(missed[:3]))
            reads = [sp for sp in left if global_names(node_at[sp])]
            report('%s: none of the %d functions left out reads a global' % (name, len(left)),
                   not reads, str(reads[:3]))
            shaped = [sp for sp in left if FACTORY_TEXT.match(src[sp[0]:sp[1]])]
            report('%s: none of the functions left out has the closure-factory shape' % name,
                   not shaped, str(shaped[:3]))
            n_fact = sum(1 for sp in exported if FACTORY_TEXT.match(src[sp[0]:sp[1]]))
            n_glob = sum(1 for sp in exported if global_names(node_at[sp]))
            report('%s: %d parts exported: %d in closure-factory shape, %d read globals'
                   % (name, len(exported), n_fact, n_glob), True)
            for word in ('HttpGet', 'loadstring', 'getgenv'):
                n_in, n_parts, n_out = src.count(word), sum(src[a:b].count(word) for a, b in exported), \
                    sum(body.count(word) for _n, _s, _e, body in parts)
                report('%s: %d of %d %r in the input are inside exported parts, the output has %d'
                       % (name, n_parts, n_in, word, n_out), n_out == n_parts and
                       (word != 'HttpGet' or n_parts == n_in))

        # ---------------------------------------------------------------- default (rename)
        out2 = os.path.join(tmp, 'renamed.lua')
        rc, log, dt = run_deob(inp, out2, tmp, extra)
        report('%s: deob.py (rename) exits 0 (%.0fs)' % (name, dt), rc == 0,
               log.strip().splitlines()[-1] if log.strip() else '')
        if rc != 0 or not os.path.exists(out2):
            return
        text2 = read(out2)
        ok, msg = compiles(luau, text2)
        report(name + ': renamed output compiles', ok, msg)
        root_in, err_in = luascope.parse(src, ast)
        idx_in = luascope.LineIndex(src)
        by_span = {idx_in.span(f['location']): f for f in luascope.collect_functions(root_in)}
        root_out, err_out = luascope.parse(text2, ast)
        report(name + ': renamed output parses', not err_out, str(err_out[:1]))
        out_fns = {}
        for st in root_out.get('body', []):
            if st.get('type') == 'AstStatLocal' and st.get('vars') and st.get('values'):
                m = re.fullmatch(r'__part(\d+)', st['vars'][0]['name'])
                if m:
                    out_fns[int(m.group(1))] = st['values'][0]
        renamed = 0
        for num, s, e, body in parts_of(text2):
            fin = by_span.get((s, e))
            fout = out_fns.get(num)
            report('%s: part %d found in both trees' % (name, num), bool(fin and fout))
            if not (fin and fout):
                continue
            eq, why = luascope.alpha_equal(fin, fout)
            report('%s: part %d is the input function up to local names' % (name, num), eq, why)
            a = [t for t in dp.tokenize(src[s:e]) if t[0] == 'w']
            b = [t for t in dp.tokenize(body) if t[0] == 'w']
            renamed += sum(1 for x, y in zip(a, b) if x != y)
        report('%s: %d identifier tokens renamed' % (name, renamed), True)


def corpus(luau, ast, report):
    skip_dirs = {'.git', '__pycache__', 'bin', 'node_modules', '.devm', 'output', '.build'}
    files = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        files += [os.path.join(base, n) for n in names if n.endswith(('.lua', '.luau'))]
    done = compiled = renamed_files = renamed_locals = 0
    for path in sorted(files):
        text = read(path)
        if len(text) > 4_000_000:
            continue
        toks = dp.tokenize(text)
        out, ok = dp.format_checked(toks)
        rel = os.path.relpath(path, ROOT)
        if not ok or not dp.same_tokens(dp.tokenize(out), toks, comments=True):
            report('corpus %s: layout keeps tokens' % rel, False)
            continue
        done += 1
        was, _ = compiles(luau, text)
        if was:
            now, msg = compiles(luau, out)
            if not now:
                report('corpus %s: still compiles' % rel, False, msg)
                continue
            compiled += 1
            # the renamer on real, un-obfuscated code: same tree up to local names
            try:
                root, errors = luascope.parse(text, ast)
                if errors:
                    continue
                ptoks = dp.tokenize_pos(text)
                ren = luascope.Renamer(luascope.Analysis(root, luascope.LineIndex(text), ptoks))
                rtext, rok = dp.format_checked(ren.apply(ptoks))
                root2, errors2 = luascope.parse(rtext, ast)
                eq, why = (False, str(errors2[:1])) if errors2 else luascope.alpha_equal(root, root2)
            except Exception as e:      # noqa: BLE001
                rok, eq, why = False, False, '%s: %s' % (type(e).__name__, e)
            if not (rok and eq):
                report('corpus %s: renamed text binds the same' % rel, False, why)
                continue
            renamed_files += 1
            renamed_locals += ren.renamed
    report('corpus: %d files keep their tokens; %d of them compile before and after'
           % (done, compiled), done > 0)
    report('corpus: %d files renamed (%d locals), each the same tree up to local names'
           % (renamed_files, renamed_locals), renamed_files > 0)


def main(argv):
    luau, ast = luaubin.find_luau(), luaubin.find_luau_ast()
    if not luau or not ast:
        print('SKIP: luau / luau-ast not found: ' + luaubin.MISSING_HINT)
        return 2
    args = [a for a in argv[1:] if not a.startswith('--')]
    inputs = args or [os.path.join(ROOT, p) for p in DEFAULT_INPUTS
                      if os.path.exists(os.path.join(ROOT, p))]
    bad = [0]

    def report(what, ok, detail=''):
        if not ok:
            bad[0] += 1
        print('%s  %s%s' % ('ok  ' if ok else 'FAIL', what, ('   <- ' + detail) if (detail and not ok) else ''),
              flush=True)

    if not inputs and '--corpus' not in argv:
        print('no input: examples/layer3.lua and examples/v15.2/sample.luau are not in this checkout; '
              'pass a protected script')
        return 2
    for inp in inputs:
        check_input(os.path.abspath(inp), luau, ast, report)
    if '--corpus' in argv:
        corpus(luau, ast, report)
    print('%d failure(s)' % bad[0])
    return 1 if bad[0] else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
