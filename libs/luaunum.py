#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
luaunum.py: Luau's number arithmetic, for constant folding

A folded constant must be the number Luau itself computes, on every machine the script may run
on.  Python's own operators are NOT that:

    16 // 0.1     Python 159.0 (exact floor division)   Luau 160  (floor(16 / 0.1))
    2^100 % 3     Python 1.0   (exact remainder)        Luau 0    (a - floor(a / b) * b)
    -7 * 0        both -0.0; written back as `0` it prints "0" instead of "-0" and turns
                  1 / x from -inf into inf

Luau (VM/src/lnumutils.h, Compiler/src/ConstantFolding.cpp) defines

    a + b, a - b, a * b, a / b     IEEE double operations
    a // b                         floor(a / b)
    a % b                          a - floor(a / b) * b
    a ^ b                          pow(a, b)
    -a                             IEEE negation

Every function here returns exactly that double, or raises Unfoldable when the result is not
the same everywhere:

  * `%` when the product floor(a / b) * b is not exact (a compiler may fuse it with the
    subtraction, and the fused result differs);
  * `^` unless the exponent is an integer and the result is exactly representable (pow is not
    correctly rounded in every C library);
  * anything that leaves the finite numbers (division by zero, overflow, NaN): these are
    well defined, but nothing is gained by folding them, and `inf` has no literal.

So "fold or leave alone" is the only choice the caller has; a value is never approximated.
"""
import math
from fractions import Fraction


class Unfoldable(Exception):
    """This operation is left to the interpreter."""


def _finite(x):
    if not math.isfinite(x):
        raise Unfoldable('not finite')
    return x


def is_negative_zero(x):
    return x == 0 and math.copysign(1.0, x) < 0


def c_floor(x):
    """C's floor(): keeps the sign of zero."""
    if x == 0 or not math.isfinite(x):
        return x
    return float(math.floor(x))


def unm(a):
    return -a


def add(a, b):
    return _finite(a + b)


def sub(a, b):
    return _finite(a - b)


def mul(a, b):
    return _finite(a * b)


def div(a, b):
    if b == 0:
        raise Unfoldable('division by zero')
    return _finite(a / b)


def idiv(a, b):
    return _finite(c_floor(div(a, b)))


def mod(a, b):
    q = c_floor(div(a, b))
    p = q * b
    if not math.isfinite(p) or Fraction(q) * Fraction(b) != Fraction(p):
        raise Unfoldable('inexact product in %')
    return _finite(a - p)


def _odd_part(a):
    """a = m * 2**e with m an odd integer (a finite, non-zero)."""
    num, den = a.as_integer_ratio()
    e = -(den.bit_length() - 1)             # den is a power of two
    low = (num & -num).bit_length() - 1     # trailing zero bits of num
    return num >> low, e + low


def power(a, b):
    if not (math.isfinite(a) and math.isfinite(b)) or b != int(b) or abs(b) > 2048:
        raise Unfoldable('exponent')
    n = int(b)
    if n == 0:
        return 1.0
    if a == 0:
        if n < 0 or is_negative_zero(a):
            raise Unfoldable('zero base')
        return 0.0
    m, e = _odd_part(a)
    if n < 0:
        if abs(m) != 1:
            raise Unfoldable('inexact power')
        mant = m if (n % 2) else 1          # (-1) ** n
    else:
        if abs(m) != 1 and abs(n) * math.log2(abs(m)) >= 53:
            raise Unfoldable('inexact power')
        mant = m ** n
        if abs(mant) >= 2 ** 53:
            raise Unfoldable('inexact power')
    exp2 = e * n
    if abs(exp2) > 960:
        raise Unfoldable('out of range')
    return math.ldexp(float(mant), exp2)    # exact: |mant| < 2**53 and far from the range ends


BINARY = {'+': add, '-': sub, '*': mul, '/': div, '//': idiv, '%': mod, '^': power}
# names of the same operators in the luau-ast JSON
BINARY_AST = {'Add': add, 'Sub': sub, 'Mul': mul, 'Div': div, 'FloorDiv': idiv, 'Mod': mod,
              'Pow': power}


def same_number(a, b):
    """Equal as Luau values, the sign of zero included."""
    return a == b and math.copysign(1.0, a) == math.copysign(1.0, b)


def eval_tokens(seg, value_of):
    """Value of a token run made of number literals, `+ - * / // % ^`, unary minus and
    parentheses, with Luau's precedence.  `seg`: [(type, text)]; `value_of(literal)` gives the
    double of a number token (None: unknown).  Raises Unfoldable (also for a run that is not
    one complete expression)."""
    pos = 0
    n = len(seg)

    def peek():
        return seg[pos] if pos < n else (None, None)

    def atom():
        nonlocal pos
        t, v = peek()
        if t == 'n':
            pos += 1
            x = value_of(v)
            if x is None or not math.isfinite(x):
                raise Unfoldable('literal')
            return x
        if t == 'o' and v == '(':
            pos += 1
            x = expr()
            if peek() != ('o', ')'):
                raise Unfoldable('syntax')
            pos += 1
            return x
        raise Unfoldable('syntax')

    def unary():
        # unary minus binds tighter than * and looser than ^ :  -2 ^ 2 == -(2 ^ 2)
        nonlocal pos
        if peek() == ('o', '-'):
            pos += 1
            return unm(unary())
        return pow_expr()

    def pow_expr():
        nonlocal pos
        base = atom()
        if peek() == ('o', '^'):
            pos += 1
            return power(base, unary())     # right associative; the exponent may be signed
        return base

    def term():
        nonlocal pos
        x = unary()
        while True:
            t, v = peek()
            if t == 'o' and v in ('*', '/', '//', '%'):
                pos += 1
                x = BINARY[v](x, unary())
            else:
                return x

    def expr():
        nonlocal pos
        x = term()
        while True:
            t, v = peek()
            if t == 'o' and v in ('+', '-'):
                pos += 1
                x = BINARY[v](x, term())
            else:
                return x

    try:
        val = expr()
    except RecursionError:
        raise Unfoldable('too deep') from None
    if pos != n:
        raise Unfoldable('syntax')
    return val


def eval_ast(node):
    """Value of a luau-ast expression made only of number constants, groups, unary minus and
    arithmetic.  Raises Unfoldable for anything else."""
    out = []
    # post-order without recursion (expressions of a one-line script can be deep)
    work = [(node, False)]
    while work:
        n, done = work.pop()
        ty = n.get('type') if isinstance(n, dict) else None
        if ty == 'AstExprConstantNumber':
            v = n.get('value')
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise Unfoldable('literal')
            out.append(_finite(float(v)))
        elif ty == 'AstExprGroup':
            work.append((n['expr'], False))
        elif ty == 'AstExprUnary':
            if n.get('op') != 'Minus':
                raise Unfoldable('operator')
            if done:
                out.append(unm(out.pop()))
            else:
                work.append((n, True))
                work.append((n['expr'], False))
        elif ty == 'AstExprBinary':
            fn = BINARY_AST.get(n.get('op'))
            if fn is None:
                raise Unfoldable('operator')
            if done:
                b = out.pop()
                a = out.pop()
                out.append(fn(a, b))
            else:
                work.append((n, True))
                work.append((n['right'], False))
                work.append((n['left'], False))
        else:
            raise Unfoldable('node')
    if len(out) != 1:
        raise Unfoldable('shape')
    return out[0]
