"""Command line: python -m devm <decompiled-luraph-script.lua> [-o out.lua] [--verify]"""
import argparse
import os
import sys
import tempfile

sys.setrecursionlimit(1000000)

from . import pipeline, vmmodel  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='python -m devm',
        description='Devirtualise a Luraph-protected script (given as decompiled Luau source) back to Lua.')
    ap.add_argument('input', help='decompiled script that still contains the Luraph VM')
    ap.add_argument('-o', '--output', help='where to write the recovered source (default: <input>.devm.lua)')
    ap.add_argument('--verify', action='store_true',
                    help='run the original and the recovered code side by side and compare what they do')
    ap.add_argument('--no-simplify', action='store_true',
                    help='keep constant tables, cached library locals and dead assignments')
    ap.add_argument('--workdir', help='directory for intermediate files (default: a temporary directory)')
    args = ap.parse_args(argv)

    try:
        vmmodel.check_luau()
    except RuntimeError as e:
        print('error: %s' % e, file=sys.stderr)
        return 2
    with open(args.input, encoding='latin-1') as f:
        src = f.read()
    workdir = args.workdir or tempfile.mkdtemp(prefix='devm-')
    tag = os.path.splitext(os.path.basename(args.input))[0]
    try:
        stage = pipeline.Stage(src, workdir, tag, simplify=not args.no_simplify)
    except RuntimeError as e:
        print('error: %s' % e, file=sys.stderr)
        return 2
    text = stage.run()
    out = args.output or os.path.splitext(args.input)[0] + '.devm.lua'
    with open(out, 'w', encoding='utf-8') as f:
        f.write(text)
    problems = len(stage.errors) + stage.stats['unstructured']
    print('[%s] wrote %s (%d lines)' % (tag, out, text.count('\n')))
    if stage.errors:
        print('[%s] %d instruction(s) could not be lifted; they are marked "LIFT ERROR" in the output:' % (
            tag, len(stage.errors)))
        for key, (node, msg) in stage.errors[:10]:
            print('    proto %s, instruction %s: %s' % (key, node, msg[:120]))
    if stage.stats['unstructured']:
        print('[%s] %d function(s) could not be structured; they are marked "UNSTRUCTURED"' % (
            tag, stage.stats['unstructured']))
    if args.verify:
        same, detail = pipeline.verify(stage, out, workdir)
        print('[%s] verify: %s' % (tag, detail))
        if not same:
            problems += 1
    return 0 if problems == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
