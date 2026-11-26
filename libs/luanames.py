#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
luanames.py: what to call a local, given the expression it is initialised with

The name is meant to say what the VALUE is.  It is read off the syntax tree of the whole
initialising expression (luau-ast), and the LAST operation of the expression decides:

    game:GetService("TweenService")                       -> TweenService   (looked up by name)
    game:GetService("HttpService"):JSONDecode(readfile(p)) -> decoded        (result of the last call)
    game:GetService("ReplicatedStorage").Assets.UI:FindFirstChild(n) -> child
    N("GuiService"):GetGuiInset().Y                        -> Y              (a field)
    workspace:GetServerTimeNow()                           -> serverTimeNow
    os.clock() + 3                                         -> clockRes       (computed from it)
    a.Name == true or a.Username == true                   -> cond

The token-based rule this replaces (readable3.derive) looked at the first name chain of the
expression only, so the first method of a chain named the variable whatever came after it:
all of the lines above but the first two forms were called `GetService`, `GuiService`,
`clock` or `Name`.

Names are a reading aid and nothing else: luascope.Renamer makes each one unique where it is
declared, and the renamed text is compared with the input tree (luascope.alpha_equal), so a
poor name cannot change the program.  The rules below are about Lua and the Roblox API in
general (how a call, a field read, an index or an operator is named); none is about a script.
"""
import re

# constructors of Roblox value types: `Color3.fromRGB(...)`, `Vector3.new(...)`, `CFrame.Angles(...)`
TYPE_NOUN = {
    'Color3': 'color', 'CFrame': 'cf', 'Vector2': 'vec2', 'Vector3': 'vec3', 'UDim2': 'dim',
    'UDim': 'dim', 'Rect': 'rect', 'NumberSequence': 'seq', 'ColorSequence': 'colorSeq',
    'Content': 'content', 'Font': 'font', 'BrickColor': 'brickColor', 'TweenInfo': 'tweenInfo',
    'Ray': 'ray', 'Region3': 'region', 'NumberRange': 'range', 'Random': 'rng',
    'DateTime': 'dateTime', 'RaycastParams': 'rayParams', 'OverlapParams': 'overlapParams',
    'Drawing': 'drawing',
}

# methods whose first argument NAMES what they return: the result is called after that string
BY_NAME = frozenset((
    'GetService', 'FindService', 'FindFirstChild', 'WaitForChild', 'FindFirstChildOfClass',
    'FindFirstChildWhichIsA', 'FindFirstAncestor', 'FindFirstAncestorOfClass',
    'FindFirstAncestorWhichIsA', 'FindFirstDescendant', 'GetAttribute',
))

# what a call of this method returns, when its argument does not name it
RESULT_NOUN = {
    'GetService': 'service', 'FindService': 'service',
    'FindFirstChild': 'child', 'FindFirstChildOfClass': 'child', 'FindFirstChildWhichIsA': 'child',
    'WaitForChild': 'child', 'FindFirstDescendant': 'descendant',
    'FindFirstAncestor': 'ancestor', 'FindFirstAncestorOfClass': 'ancestor',
    'FindFirstAncestorWhichIsA': 'ancestor',
    'GetChildren': 'children', 'GetDescendants': 'descendants', 'GetPlayers': 'players',
    'GetPlayerFromCharacter': 'player', 'GetMouseLocation': 'mousePos',
    'WorldToViewportPoint': 'screenPos', 'WorldToScreenPoint': 'screenPos', 'Raycast': 'hit',
    'GetPartsInPart': 'parts', 'Connect': 'conn', 'Once': 'conn', 'ConnectParallel': 'conn',
    'Clone': 'clone', 'JSONDecode': 'decoded', 'JSONEncode': 'json',
}

# names that say nothing by themselves: nothing is derived from them (no `numRes`, `condItem`)
GENERIC = frozenset(('v', 'val', 'Res', 'Item', 'num', 'str', 'cond', 'flag', 'len', 'obj', 'inst'))
_KEYWORDS = frozenset((
    'and', 'break', 'continue', 'do', 'else', 'elseif', 'end', 'false', 'for', 'function', 'if',
    'in', 'local', 'nil', 'not', 'or', 'repeat', 'return', 'then', 'true', 'until', 'while',
    'type', 'export',
))
_ARITH = frozenset(('Add', 'Sub', 'Mul', 'Div', 'FloorDiv', 'Mod', 'Pow'))
_COMPARE = frozenset(('CompareLt', 'CompareLe', 'CompareGt', 'CompareGe', 'CompareEq', 'CompareNe'))
MAX_LEN = 40


def _ident(text):
    return (isinstance(text, str) and text.isidentifier() and text.isascii()
            and text not in _KEYWORDS and len(text) <= MAX_LEN)


def _lower_first(text):
    """PascalCase -> camelCase, a leading acronym as a whole: GuiInset -> guiInset,
    JSONData -> jsonData, UIScale -> uiScale."""
    n = 0
    while n < len(text) and text[n].isupper():
        n += 1
    if n == 0:
        return text
    if n == len(text) or n == 1:
        return text[:n].lower() + text[n:]
    return text[:n - 1].lower() + text[n - 1:]


def is_generic(name):
    """True for a name that says nothing about the value (GENERIC, also numbered: `num3`)."""
    return not name or name.rstrip('0123456789') in GENERIC


_SUFFIXED = re.compile(r'(?:Res|Item)\d*$')


def _derived(base, suffix):
    """`base` + suffix for a value computed from the thing called `base`.  Suffixes are not
    stacked (the result of a computation on `clockRes2` is again a `clockRes`, not a
    `clockRes2Res`), and nothing is derived from a name that says nothing."""
    if is_generic(base):
        return suffix
    cut = _SUFFIXED.search(base)
    if cut and cut.start() > 0:
        base = base[:cut.start()]
    return (base + suffix)[:MAX_LEN]


def _string_arg(call):
    args = call.get('args') or []
    if args and args[0].get('type') == 'AstExprConstantString':
        val = args[0].get('value')
        if _ident(val):
            return val
    return None


def value_name(node, resolve, function_name=None):
    """A base name for the value of the expression `node` (an AST node of luau-ast), or None
    when the expression gives no clue (the caller keeps the variable's own name).

    `resolve(local_node)`: the name by which the local an AstExprLocal refers to is now
    known.  `function_name(node)`: name for a function expression (the caller has the
    function's text; None -> 'fn')."""
    if not isinstance(node, dict):
        return None
    ty = node.get('type')
    if ty in ('AstExprGroup', 'AstExprTypeAssertion'):
        return value_name(node.get('expr'), resolve, function_name)
    if ty == 'AstExprLocal':
        return resolve(node)
    if ty == 'AstExprGlobal':
        return node.get('global')
    if ty == 'AstExprConstantNumber':
        return 'num'
    if ty in ('AstExprConstantString', 'AstExprInterpString'):
        return 'str'
    if ty == 'AstExprConstantBool':
        return 'flag'
    if ty == 'AstExprFunction':
        return (function_name(node) if function_name else None) or 'fn'
    if ty == 'AstExprTable':
        # the first field name is the clue: {conns = {}, drawings = {}} -> conns
        for item in (node.get('items') or [])[:8]:
            key = item.get('key') or {}
            if item.get('kind') == 'record' and _ident(key.get('value')):
                return key['value']
        return 'tbl'
    if ty == 'AstExprUnary':
        op = node.get('op')
        if op == 'Not':
            return 'cond'
        if op == 'Len':
            return 'len'
        inner = node.get('expr') or {}
        if inner.get('type') == 'AstExprConstantNumber':
            return 'num'                # -1
        nm = value_name(inner, resolve, function_name)
        return 'num' if is_generic(nm) else _derived(nm, 'Res')
    if ty == 'AstExprBinary':
        op = node.get('op')
        if op in _COMPARE:
            return 'cond'
        if op == 'Concat':
            return 'str'
        left, right = node.get('left'), node.get('right')
        if op == 'And':                 # `a and b`: b, when a holds
            return value_name(right, resolve, function_name) or value_name(left, resolve, function_name)
        if op == 'Or':                  # `a or b`: a, with b as the fallback
            return value_name(left, resolve, function_name) or value_name(right, resolve, function_name)
        if op in _ARITH:
            for side in (left, right):
                nm = value_name(side, resolve, function_name)
                if not is_generic(nm):
                    return _derived(nm, 'Res')
            return 'num'
        return None
    if ty == 'AstExprIfElse':
        return (value_name(node.get('trueExpr'), resolve, function_name)
                or value_name(node.get('falseExpr'), resolve, function_name))
    if ty == 'AstExprIndexName':
        field = node.get('index')
        if not _ident(field):
            return None
        if field == 'new':              # a constructor, not a value of the type
            owner = value_name(node.get('expr'), resolve, function_name)
            return 'new' if is_generic(owner) else (owner + '_new')[:MAX_LEN]
        return field
    if ty == 'AstExprIndexExpr':
        base, index = node.get('expr') or {}, node.get('index') or {}
        if index.get('type') == 'AstExprConstantString' and _ident(index.get('value')):
            return index['value']           # t["name"] is t.name
        owner = value_name(base, resolve, function_name)
        if (index.get('type') == 'AstExprConstantNumber'
                and base.get('type') in ('AstExprLocal', 'AstExprGlobal') and owner):
            val = index.get('value')
            if isinstance(val, (int, float)) and val == int(val) and 0 <= val < 10 ** 9:
                return '%s_%d' % (owner, int(val))      # tbl[60] -> tbl_60
        return _derived(owner, 'Item')
    if ty == 'AstExprCall':
        fn = node.get('func') or {}
        named = _string_arg(node)
        if fn.get('type') == 'AstExprIndexName':
            meth, owner = fn.get('index'), fn.get('expr') or {}
            if owner.get('type') == 'AstExprGlobal':
                lib = owner.get('global')
                if lib == 'Instance' and meth == 'new':
                    return _lower_first(named) if named else 'inst'
                if lib in TYPE_NOUN:
                    return TYPE_NOUN[lib]
            if meth in BY_NAME and named:
                return named
            if meth in RESULT_NOUN:
                return RESULT_NOUN[meth]
            if not _ident(meth):
                return 'Res'
            if meth == 'new':
                nm = value_name(owner, resolve, function_name)
                return 'obj' if is_generic(nm) else _lower_first(nm)
            for prefix in ('Get', 'Find'):
                rest = meth[len(prefix):]
                if meth.startswith(prefix) and rest[:1].isupper():
                    return _lower_first(rest)
            for prefix in ('Is', 'Has', 'Can'):
                if meth.startswith(prefix) and meth[len(prefix):len(prefix) + 1].isupper():
                    return _lower_first(meth)
            return _derived(meth, 'Res')
        if fn.get('type') in ('AstExprLocal', 'AstExprGlobal'):
            if named:
                return named            # make("Frame", {...}), service("Players")
            return _derived(value_name(fn, resolve, function_name), 'Res')
        return _derived(value_name(fn, resolve, function_name), 'Res')
    return None
