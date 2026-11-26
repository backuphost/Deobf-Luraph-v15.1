#!/usr/bin/env python3
"""Constant folding of libs/deobf_plus.py, checked against the interpreter.

    python3 tests/static_fold.py [--n 6000] [--seed 1]

1. Directed cases: expressions where Python's arithmetic and Luau's differ (`//` and `%` on
   inexact quotients, negative zero, powers) and expressions that must not be touched because
   of precedence.
2. Fuzz: random expressions made of number literals in every spelling, arithmetic, comparison,
   concatenation, logic, calls and non-constant operands are folded; the original and the
   folded text are both evaluated by bin/luau and must give the same value: same type, same
   number including the sign of zero, NaN for NaN, or an error in both.
3. The folded token list must parse to the input's tree with luascope.alpha_equal(folds=True),
   i.e. the folds are whole sub-expressions (no precedence slip).

Needs bin/luau and bin/luau-ast (./setup.sh).  Exit status 0 when everything passes.
"""
import os
import random
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'libs'))
sys.path.insert(0, ROOT)
import deobf_plus as dp        # noqa: E402
import luascope                # noqa: E402
import luaubin                 # noqa: E402

# expression  ->  what fold_consts must make of it (tokens joined by one space)
DIRECTED = [
    # Python's // and % are exact, Luau's go through an IEEE division
    ('0x10 // 0.1', '160'),
    ('2 ^ 100 % 3', '2 ^ 100 % 3'),
    ('1e22 % 7', '1e22 % 7'),
    ('7 % 3', '1'),
    ('-7 % 3', '2'),
    ('7 % -3 + 3', '1'),
    ('7.5 // 2', '3'),
    ('-7 // 2 + 10', '6'),
    ('5.3 % 1 * 0', '0'),              # 5 * 1 is exact: one rounding, the same everywhere
    ('1e22 % 7 * 0', '1e22 % 7 * 0'),  # floor(1e22 / 7) * 7 is not exact: left alone
    # negative zero is not `0`
    ('-7 * 0 .. ""', '- 7 * 0 .. ""'),
    ('1 / ( 0 * -1 )', '1 / ( 0 * - 1 )'),
    ('0 / -5', '0 / - 5'),
    ('-0 + 0', '0'),
    ('0 * 1', '0'),
    # leaving the finite numbers
    ('1 / 0', '1 / 0'),
    ('5 % 0', '5 % 0'),
    ('1e308 * 10', '1e308 * 10'),
    ('1 / ( 1 / 0 )', '1 / ( 1 / 0 )'),
    # powers: exact ones only
    ('2 ^ 10', '1024'),
    ('2 ^ -1 * 4', '2'),
    ('2 ^ 0.5 * 2', '2 ^ 0.5 * 2'),
    ('3 ^ 40 * 0', '3 ^ 40 * 0'),
    ('( -2 ) ^ 3 + 9', '1'),
    ('-2 ^ 2 + 5', '1'),
    ('2 ^ 3 ^ 2', '512'),
    ('2 ^ -2 ^ 2 * 16', '1'),
    ('0.5 ^ 2 * 8', '2'),
    ('10 ^ 15', '1000000000000000'),
    ('10 ^ 16', '10 ^ 16'),
    ('2 ^ 53', '2 ^ 53'),
    ('2 ^ 53 - 1', '9007199254740991'),
    # results that are not a non-negative integer stay
    ('1 - 2', '1 - 2'),
    ('1 / 2', '1 / 2'),
    ('0.1 + 0.2', '0.1 + 0.2'),
    # precedence: only whole operands
    ('a - 1 + 2', 'a - 1 + 2'),
    ('a * 2 + 3', 'a * 2 + 3'),
    ('a ^ 1 + 2', 'a ^ 1 + 2'),
    ('1 + 2 * a', '1 + 2 * a'),
    ('1 + 2 ^ a', '1 + 2 ^ a'),
    ('not 1 + 2', 'not 1 + 2'),
    ('# t + 1 + 2', '# t + 1 + 2'),
    ('- a + 1 + 2', '- a + 1 + 2'),
    ('a .. 1 + 2', 'a .. 3'),
    ('1 + 2 .. a', '3 .. a'),
    ('a == 1 + 2', 'a == 3'),
    ('a and 1 + 2 or 4 * 5', 'a and 3 or 20'),
    ('a * ( 1 + 2 )', 'a * ( 3 )'),
    ('( 1 + 2 ) * a', '( 3 ) * a'),
    ('( 1 + 2 ) : f ( )', '( 3 ) : f ( )'),
    ('( 1 + 2 ) "s"', '( 3 ) "s"'),
    ('( 1 + 2 ) { }', '( 3 ) { }'),
    ('( 1 + 2 ) [ 1 ]', '( 3 ) [ 1 ]'),
    ('f ( 1 + 2 , 3 * 4 )', 'f ( 3 , 12 )'),
    ('t [ 1 + 2 ]', 't [ 3 ]'),
    ('- - 5', '5'),
    ('( ( 0b1100 + 0b1 ) + ( 0B101111 - 0b100001 ) )', '27'),
    ('0xFFFFFFFFFFFFFFFFF + 1 - 0xFFFFFFFFFFFFFFFFF', '0xFFFFFFFFFFFFFFFFF + 1 - 0xFFFFFFFFFFFFFFFFF'),
    ('9007199254740993 - 9007199254740992', '0'),
    ('1_000 + 1', '1001'),
]

NUMBERS = ['0', '1', '2', '3', '7', '10', '16', '255', '0x10', '0xFF', '0b11', '0B101', '1e2', '1E3',
           '.5', '0.5', '2.5', '0.1', '3.', '1_000', '0x7fffffff', '4294967296', '9007199254740992',
           '9007199254740993', '1e15', '1e16', '1e22', '1e308', '1e-5', '5e-324', '0.3', '64', '100']
ARITH = ['+', '-', '*', '/', '//', '%', '^']
OTHER = ['..', '==', '~=', '<', '<=', '>', '>=', 'and', 'or']
ATOMS = ['x', 'y', 'f(1)', '#t', 't.n', 'x', 'y', 't.n', 'nil', '"s"']


def gen(rng, depth):
    """A random expression as a list of token texts."""
    r = rng.random()
    if depth <= 0 or r < 0.18:
        if rng.random() < 0.82:
            return [rng.choice(NUMBERS)]
        return [rng.choice(ATOMS)]
    if r < 0.30:
        return ['-'] + gen(rng, depth - 1)
    if r < 0.40:
        return ['('] + gen(rng, depth - 1) + [')']
    if r < 0.44:
        return [rng.choice(['not', '#'])] + gen(rng, depth - 1)
    op = rng.choice(ARITH) if rng.random() < 0.85 else rng.choice(OTHER)
    return gen(rng, depth - 1) + [op] + gen(rng, depth - 1)


def fold_text(expr):
    toks = dp.tokenize('return ' + expr)
    report = []
    out = dp.fold_consts(toks, report)
    return toks, out, report


def join(toks):
    return ' '.join(v for _t, v in toks)


def main():
    n = 6000
    seed = 1
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == '--n':
            n = int(argv[i + 1])
        elif a == '--seed':
            seed = int(argv[i + 1])
    luau, ast = luaubin.find_luau(), luaubin.find_luau_ast()
    if not luau or not ast:
        print('needs luau and luau-ast: ' + luaubin.MISSING_HINT)
        return 2
    failures = 0
    checks = 0

    def check(name, cond, detail=''):
        nonlocal failures, checks
        checks += 1
        if not cond:
            failures += 1
            print('FAIL', name, detail)

    # ---- 1. directed
    for expr, want in DIRECTED:
        _toks, out, _rep = fold_text(expr)
        got = join(out)[len('return '):]
        check('directed %r' % expr, got == want, '-> %r, expected %r' % (got, want))

    # ---- 2. fuzz against the interpreter
    rng = random.Random(seed)
    cases = []
    folded_exprs = 0
    folds = 0
    all_reports = []
    for k in range(n):
        expr = ' '.join(gen(rng, rng.randint(1, 5)))
        toks, out, rep = fold_text(expr)
        if rep:
            folded_exprs += 1
            folds += len(rep)
            all_reports.append((toks, rep))
        cases.append((expr, join(out)[len('return '):], bool(rep)))
    lines = [
        'local x, y = 3.5, -2',
        'local t = setmetatable({n = 4, 1, 2, 3}, {__len = function() return 3 end})',
        'local function f(a) return a + 0.25 end',
        'local bad, same, err = 0, 0, 0',
        'local function eq(a, b)',
        '  if type(a) ~= type(b) then return false end',
        '  if a ~= a then return b ~= b end',
        '  if type(a) == "number" then return a == b and 1 / a == 1 / b end',
        '  return a == b',
        'end',
        'local function chk(k, f1, f2)',
        '  local ok1, a = pcall(f1); local ok2, b = pcall(f2)',
        '  if ok1 ~= ok2 then bad += 1; print("BAD", k, "error", tostring(a), tostring(b))',
        '  elseif not ok1 then err += 1',
        '  elseif not eq(a, b) then bad += 1; print("BAD", k, tostring(a), tostring(b))',
        '  else same += 1 end',
        'end',
    ]
    todo = [(k, c) for k, c in enumerate(cases) if c[2]]
    for start in range(0, len(todo), 200):
        lines.append('do')
        for k, (expr, folded, _f) in todo[start:start + 200]:
            lines.append('chk(%d, function() return %s end, function() return %s end)' % (k, expr, folded))
        lines.append('end')
    lines.append('print("FUZZ", bad, same, err)')
    fd, path = tempfile.mkstemp(prefix='static_fold_', suffix='.lua')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(lines) + '\n')
        r = subprocess.run([luau, path], capture_output=True, text=True, timeout=300)
    finally:
        os.unlink(path)
    out = r.stdout + r.stderr
    summary = [ln for ln in out.splitlines() if ln.startswith('FUZZ')]
    bad_lines = [ln for ln in out.splitlines() if ln.startswith('BAD')]
    check('fuzz ran', bool(summary), out[:300])
    for ln in bad_lines[:10]:
        k = int(ln.split('\t')[1])
        print('   ', ln.replace('\t', ' '), '|', cases[k][0], '=>', cases[k][1])
    check('fuzz: folded text evaluates like the original', not bad_lines, '%d differ' % len(bad_lines))
    check('fuzz: enough folds to mean something', folded_exprs >= n // 10,
          '%d of %d expressions were folded' % (folded_exprs, n))

    # ---- interpreter check used by the CLI (dp.check_folds) on the same folds
    n_checked = 0
    wrong = []
    for toks, rep in all_reports:
        c, bad = dp.check_folds(toks, rep, luau)
        n_checked += c
        wrong.extend(bad)
        if n_checked > 400:
            break
    check('check_folds agrees', not wrong, str(wrong[:3]))

    # ---- 3. folds are whole sub-expressions
    tree_checked = 0
    for k, (expr, folded, did) in enumerate(cases):
        if not did or tree_checked >= 300:
            continue
        ra, ea = luascope.parse('return ' + expr, ast)
        rb, eb = luascope.parse('return ' + folded, ast)
        if ea or eb:
            check('tree %d parses' % k, not ea and not eb, '%s | %s | %r %r' % (expr, folded, ea[:1], eb[:1]))
            continue
        okk, why = luascope.alpha_equal(ra, rb, folds=True)
        check('tree %d' % k, okk, '%s => %s : %s' % (expr, folded, why))
        tree_checked += 1
    # a wrong value must be rejected by the tree comparison
    ra, _ = luascope.parse('return 0x10 // 0.1', ast)
    rb, _ = luascope.parse('return 159', ast)
    check('alpha_equal(folds) rejects a wrong value', not luascope.alpha_equal(ra, rb, folds=True)[0])
    ra, _ = luascope.parse('return -7 * 0', ast)
    rb, _ = luascope.parse('return 0', ast)
    check('alpha_equal(folds) rejects 0 for -0', not luascope.alpha_equal(ra, rb, folds=True)[0])
    ra, _ = luascope.parse('return a - 1 + 2', ast)
    rb, _ = luascope.parse('return a - 3', ast)
    check('alpha_equal(folds) rejects a precedence slip', not luascope.alpha_equal(ra, rb, folds=True)[0])

    print('%d directed cases; fuzz: %d expressions, %d folded (%d folds), luau: %s; '
          '%d trees compared' % (len(DIRECTED), n, folded_exprs, folds,
                                 summary[0] if summary else 'no result', tree_checked))
    print('%d checks, %d failure(s)' % (checks, failures))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
