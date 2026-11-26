"""Humanise generated names (v184, p7) in decompiled output.

Protection removes every local name, so names are re-derived from how a value is made and used:

    local v3 = game:GetService("Players")              -> local Players = ...
    local v225 = humanoid:Clone()                      -> local humanoidClone = ...
    local v24 = function() return char and char:FindFirstChild("HumanoidRootPart") end
                                                       -> local getHumanoidRootPart = function()
    RunService.Heartbeat:Connect(function(p7)          -> function(dt)
    for v1, v2 in ipairs(folder:GetChildren()) do      -> for _, child in ipairs(...)

These are guesses about intent, not the author's names.  A value nothing can be said about keeps
its number.  Generated names are unique in a file, so renaming is a token substitution and cannot
change behaviour.

    python -m devm.renamer in.lua [-o out.lua]
"""
import re
import sys

from . import cleanup
from .cleanup import camel, tokens, KEYWORDS, RESERVED, STR

GEN = r'[vp]\d+'
ID = r'[A-Za-z_]\w*'

# event -> names of the handler's parameters
EVENT_PARAMS = {
    'Heartbeat': ['dt'], 'RenderStepped': ['dt'], 'PreRender': ['dt'], 'PreSimulation': ['dt'],
    'PostSimulation': ['dt'], 'Stepped': ['time', 'dt'],
    'CharacterAdded': ['character'], 'CharacterRemoving': ['character'], 'CharacterAppearanceLoaded': ['character'],
    'PlayerAdded': ['player'], 'PlayerRemoving': ['player'],
    'InputBegan': ['input', 'gameProcessed'], 'InputChanged': ['input', 'gameProcessed'],
    'InputEnded': ['input', 'gameProcessed'],
    'Touched': ['hit'], 'TouchEnded': ['hit'],
    'ChildAdded': ['child'], 'ChildRemoved': ['child'], 'DescendantAdded': ['descendant'],
    'DescendantRemoving': ['descendant'], 'AnimationPlayed': ['track'], 'Changed': ['property'],
    'StateChanged': ['oldState', 'newState'], 'HealthChanged': ['health'], 'Chatted': ['message'],
    'FocusLost': ['enterPressed'], 'Triggered': ['player'], 'OnClientEvent': None, 'OnServerEvent': None,
}
ITER_ITEM = {'GetChildren': 'child', 'GetDescendants': 'descendant', 'GetPlayers': 'player',
             'GetPlayingAnimationTracks': 'track', 'GetTagged': 'instance', 'GetPartsInPart': 'part',
             'GetTouchingParts': 'part', 'GetConnectedParts': 'part', 'GetAttributes': 'value'}
# members that say what an untyped value is
MEMBER_HINTS = [
    ({'Health', 'WalkSpeed', 'FloorMaterial', 'MoveTo', 'GetState', 'ChangeState', 'JumpPower', 'PlatformStand'}, 'humanoid'),
    ({'UserInputType', 'KeyCode', 'UserInputState'}, 'input'),
    ({'AssemblyLinearVelocity', 'CanCollide', 'Anchored'}, 'part'),
    ({'UserId', 'Character', 'DisplayName'}, 'player'),
    ({'IsA', 'GetPropertyChangedSignal', 'IsDescendantOf', 'GetFullName'}, 'instance'),
]
BORING = {'IsA', 'FindFirstChild', 'FindFirstChildOfClass', 'FindFirstChildWhichIsA', 'WaitForChild', 'Connect',
          'Disconnect', 'Wait', 'GetChildren', 'GetDescendants', 'Once', 'IsDescendantOf', 'GetService', 'Destroy',
          'InvokeServer', 'FireServer', 'Clone', 'GetPropertyChangedSignal', 'GetPivot', 'GetState', 'Play', 'Stop'}


def cap(s):
    return s[:1].upper() + s[1:]


def singular(s):
    if s.endswith('ies') and len(s) > 4:
        return s[:-3] + 'y'
    if s.endswith('s') and not s.endswith('ss') and len(s) > 2:
        return s[:-1]
    return None


def strip_guard(rhs):
    """`a and a.B or 16` -> `a.B` ; `tonumber(x) or 0` -> `tonumber(x)`"""
    for _ in range(4):
        m = re.match(r'^(%s) and (.+)$' % ID, rhs)
        if m and (m.group(2).startswith(m.group(1)) or re.fullmatch(r'ok\d*', m.group(1))):
            rhs = m.group(2)
            continue
        m = re.match(r'^type\((%s)\) == "\w+" and (.+)$' % ID, rhs)
        if m and m.group(2).startswith(m.group(1)):
            rhs = m.group(2)
            continue
        m = re.match(r'^(.+) or (?:nil|-?[\d.]+|"[^"]*"|true|false|\{\})$', rhs)
        if m and ' and ' not in m.group(1) and ' or ' not in m.group(1):
            rhs = m.group(1)
            continue
        break
    return rhs


def suggest_value(rhs, creators=()):
    rhs = strip_guard(rhs)
    m = re.match(r'^.+:Get([A-Z]\w*)\(\)$', rhs)
    if m:
        return camel(m.group(1))
    base = cleanup.suggest(rhs)
    if base:
        return base
    m = re.match(r'^(?:get|find|fetch|read)([A-Z]\w*)\(.*\)$', rhs)
    if m:
        name = camel(m.group(1))
        return 'rootPart' if name == 'humanoidRootPart' else name
    if rhs.startswith('workspace:Raycast('):
        return 'raycastResult'
    m = re.match(r'^(%s):Clone\(\)$' % ID, rhs)
    if m and not re.fullmatch(GEN, m.group(1)):
        return m.group(1) + 'Clone'
    if rhs in ('os.clock()', 'tick()', 'time()', 'os.time()'):
        return 'now'
    m = re.match(r'^(?:tonumber|tostring)\((.+)\)$', rhs)
    if m:
        inner = suggest_value(m.group(1))
        if inner:
            return inner
        return 'text' if rhs.startswith('tostring') else 'number'
    if re.match(r'^\(.+\)\.Magnitude$', rhs) or re.match(r'^Vector3\.new\(.+\)\.Magnitude$', rhs):
        return 'distance'
    m = re.match(r'^.+\.(%s)$' % ID, rhs)
    if m and rhs.count('(') == rhs.count(')') and not rhs.endswith(')'):
        return camel(m.group(1))
    m = re.match(r'^#(%s)(?:\.(%s))?$' % (ID, ID), rhs)
    if m:
        return 'count'
    if rhs.startswith('string.format('):
        return 'text'
    m = re.match(r'^string\.(lower|upper)\(', rhs)
    if m:
        return m.group(1) + 'ed'
    for prefix, name in (('Vector3.new(', 'vector'), ('CFrame.new(', 'cframe'), ('Color3.', 'color'),
                         ('TweenInfo.new(', 'tweenInfo'), ('RaycastParams.new(', 'raycastParams')):
        if rhs.startswith(prefix) and rhs.endswith(')') and rhs.count('(') - rhs[:-1].count(')') == 1:
            return name
    m = re.match(r'^(%s)\(%s' % (ID, STR), rhs)
    if m and m.group(1) in creators:
        return camel(m.group(2))
    return None


def table_name(fields):
    """name for a table literal from its field values"""
    vals = [v for _, v in fields]
    if len(vals) < 2:
        return None
    if sum(v.startswith('Color3.') for v in vals) * 2 > len(vals):
        return 'colors'
    if all(re.fullmatch(r'-?[\d.e+-]+', v) for v in vals):
        return 'config'
    if all(re.fullmatch(r'-?[\d.e+-]+|true|false|nil|"[^"]*"|\{\}', v) for v in vals) and \
            any(v in ('true', 'false', 'nil') for v in vals):
        return 'state'
    return None


def function_name(params, body, top, creators):
    """body: all lines of the function; top: its own statements (not nested ones)"""
    text = '\n'.join(body)
    if params and re.search(r'\bInstance\.new\(%s\)' % re.escape(params[0]), text):
        return 'create'
    if len(top) == 1:
        m = re.match(r'^return (.+)$', top[0])
        if m:
            rhs = strip_guard(m.group(1))
            base = cleanup.suggest(rhs)
            if base:
                return 'get' + cap(base)
            m2 = re.match(r'^(%s):(FindFirstChild|WaitForChild)\(' % ID, rhs)
            if m2 and not re.fullmatch(GEN, m2.group(1)):
                return 'findIn' + cap(m2.group(1))
    if 'require(' in text and 'pcall(' in text and len(body) <= 8:
        return 'safeRequire'
    if 'TweenInfo.new(' in text and ':Create(' in text and len(body) <= 6:
        return 'tween'
    paths = set(re.findall(r'"((?:\w+/)+\w+)"', text))
    if len(paths) == 1 and re.search(r':(InvokeServer|FireServer)\(', text):
        return camel(next(iter(paths)).split('/')[-1])
    made = set(re.findall(r'\b(%s)\("(\w+)"' % ID, text))
    made = {c for f, c in made if f in creators}
    if len(made) == 1 and len(body) <= 12 and params:
        return 'add' + cap(next(iter(made)))
    members = set(re.findall(r'[\w\])][.:]([A-Z]\w+)\(', text)) | set(re.findall(r'pcall\([\w.]+\.([A-Z]\w+)\b', text))
    members -= BORING
    if len(members) == 1 and len(body) <= 14:
        return camel(next(iter(members)))
    rets = [t for t in top if t.startswith('return ')]
    if rets and all(t.startswith('return string.format(') for t in rets):
        return 'formatText'
    last = rets[-1][7:] if rets else None
    if last and re.fullmatch(ID, last) and not re.fullmatch(GEN + r'|ok\d*|result\d*|nil|true|false', last) \
            and not re.fullmatch(r'(text|number|count|vector|now|item|value|list)\d*', last):
        return 'get' + cap(re.sub(r'\d+$', '', last))
    if last and len(rets) == 1:
        base = suggest_value(last)
        if base and not re.fullmatch(GEN, base) and base not in ('text', 'number', 'count', 'vector'):
            return 'get' + cap(base)
    return None


def _indent(ln):
    return len(ln) - len(ln.lstrip('\t'))


def _block(lines, i):
    """index of the line closing the block opened on line i"""
    ind = _indent(lines[i])
    j = i + 1
    while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) > ind):
        j += 1
    return j


def _pass(text):
    toks = tokens(text)
    count = {}
    free = set()
    prev = None
    for kind, s in toks:
        if kind == 'ident':
            count[s] = count.get(s, 0) + 1
            if prev not in ('.', ':'):
                free.add(s)
        if kind != 'other' or not s.isspace():
            prev = s
    used = set(free) | RESERVED
    mapping = {}

    def claim(old, base):
        if not base or old in mapping or not re.fullmatch(GEN, old):
            return
        base = re.sub(r'\W', '', base)
        if not base or base[0].isdigit():
            return
        name, n = base, 2
        while name in used or name in KEYWORDS:
            name = '%s%d' % (base, n)
            n += 1
        used.add(name)
        mapping[old] = name

    lines = text.split('\n')
    creators = set()
    for i, ln in enumerate(lines):
        m = re.match(r'^\t*local (%s) = function\((%s)' % (ID, ID), ln)
        if m and any(re.search(r'\bInstance\.new\(%s\)' % m.group(2), x) for x in lines[i + 1:_block(lines, i)]):
            creators.add(m.group(1))

    for i, ln in enumerate(lines):
        s = ln.strip()
        ind = _indent(ln)
        # ---- a function only ever called with text
        m = re.match(r'^local (%s) = function\((%s)\)$' % (ID, GEN), s)
        if m:
            args = re.findall(r'\b%s\(([^\n]*)' % re.escape(m.group(1)), text)
            lit = [a for a in args if re.match(r'^("|string\.format\()', a)]
            if len(args) >= 3 and len(lit) * 2 > len(args):
                claim(m.group(2), 'message')
        # ---- functions
        m = re.match(r'^local (%s) = function\((.*)\)$' % GEN, s)
        if m:
            end = _block(lines, i)
            body = [x.strip() for x in lines[i + 1:end]]
            top = [x.strip() for x in lines[i + 1:end] if _indent(x) == ind + 1]
            params = [p.strip() for p in m.group(2).split(',') if p.strip()]
            name = function_name(params, body, top, creators)
            claim(m.group(1), name)
            if name == 'create' and len(params) >= 1:
                claim(params[0], 'className')
                if len(params) >= 2:
                    claim(params[1], 'props')
        # ---- event handlers and callbacks
        m = re.search(r'[.:](\w+):(?:Connect|Once|ConnectParallel)\(function\(([^)]*)\)$', s)
        if m and EVENT_PARAMS.get(m.group(1)):
            for p, name in zip([p.strip() for p in m.group(2).split(',')], EVENT_PARAMS[m.group(1)]):
                claim(p, name)
        m = re.search(r'table\.sort\(.*function\((%s), (%s)\)$' % (GEN, GEN), s)
        if m:
            claim(m.group(1), 'a')
            claim(m.group(2), 'b')
        # ---- parameters, from the members used on them
        m = re.search(r'function\(([^)]*)\)$', s)
        if m and m.group(1):
            end = _block(lines, i)
            body = '\n'.join(lines[i + 1:end])
            for p in [p.strip() for p in m.group(1).split(',')]:
                if not re.fullmatch(GEN, p) or p in mapping:
                    continue
                mem = set(re.findall(r'\b%s[.:](\w+)' % p, body))
                for keys, name in MEMBER_HINTS:
                    if mem & keys:
                        claim(p, name)
                        break
        # ---- loops
        m = re.match(r'^for (%s), (%s) in (i?pairs)\((.+)\) do$' % (ID, ID), s)
        if m:
            k, v, kind, src = m.groups()
            item = None
            m2 = re.search(r':(\w+)\(\)$', src)
            if m2:
                item = ITER_ITEM.get(m2.group(1))
            if item is None:
                m2 = re.search(r'(?:^|\.)(%s)$' % ID, src)
                if m2 and not re.fullmatch(GEN, m2.group(1)):
                    base = camel(m2.group(1))       # (None for a name that is not usable as one)
                    item = singular(base) if base else None
            if item is None and kind == 'ipairs':
                item = 'item'
            if re.fullmatch(r'value\d*', v) or re.fullmatch(GEN, v):
                if item:
                    mapping.pop(v, None)
                    _claim_any(mapping, used, v, item)
            if count.get(k) == 1 and (re.fullmatch(r'key\d*', k) or re.fullmatch(GEN, k)):
                mapping[k] = '_'
            elif kind == 'ipairs' and re.fullmatch(r'key\d*', k):
                _claim_any(mapping, used, k, 'index')
        # ---- values
        m = re.match(r'^local (%s) = (.+)$' % GEN, s)
        if m and not m.group(2).startswith('function('):
            rhs = m.group(2)
            if rhs.endswith('{') and not rhs.startswith('{'):
                # creator("Frame", {  ... Name = "Header"
                m2 = re.match(r'^(%s)\(%s, \{$' % (ID, STR), rhs)
                if m2 and m2.group(1) in creators:
                    end = _block(lines, i)
                    nm = [re.match(r'^Name = %s,$' % STR, x.strip()) for x in lines[i + 1:end]]
                    nm = [x.group(1) for x in nm if x]
                    cls = m2.group(2)
                    base = camel(nm[0]) if nm else camel(cls)
                    if nm and cls.lower() not in base.lower() and not base.lower().endswith(('button', 'label', 'frame')):
                        short = {'TextLabel': 'Label', 'TextButton': 'Button', 'ImageLabel': 'Image',
                                 'ScrollingFrame': 'Scroll', 'ScreenGui': 'Gui', 'TextBox': 'Box'}.get(cls, cls)
                        base += short
                    claim(m.group(1), base)
            elif rhs == '{':
                end = _block(lines, i)
                fields = [re.match(r'^(\w+) = (.+?),?$', x.strip()) for x in lines[i + 1:end] if _indent(x) == ind + 1]
                if fields and all(fields):
                    claim(m.group(1), table_name([f.groups() for f in fields]))
            elif rhs.startswith('{') and rhs.endswith('}') and len(rhs) > 2:
                fields = re.findall(r'(\w+) = ([^,{}]+)', rhs)
                claim(m.group(1), table_name(fields))
            elif re.match(r'^%s\(function\(\)$' % ID, rhs) and i + 2 < len(lines) \
                    and lines[i + 2].strip() == 'end)':
                # loader(function() return A.B.Module end)
                m2 = re.match(r'^return [\w.]+\.(%s)$' % ID, lines[i + 1].strip())
                if m2:
                    claim(m.group(1), camel(m2.group(1), lower=False))
            elif rhs in ('0', '1', 'nil', 'false', 'true', '{}'):
                v = m.group(1)
                later = [x.strip() for x in lines[i + 1:i + 400] if re.match(r'^\t*%s\b' % v, x)]
                ups = [re.match(r'^%s ([-+])= (.+)$' % v, x) for x in later]
                ups = [u for u in ups if u]
                sets = [re.match(r'^%s = (.+)$' % v, x) for x in later]
                sets = [u.group(1) for u in sets if u]
                body = '\n'.join(lines[i + 1:i + 400])
                if rhs in ('0', '1') and ups:
                    if any(':Wait()' in u.group(2) or re.fullmatch(r'dt\d*', u.group(2)) for u in ups):
                        claim(v, 'elapsed')
                    elif all(u.group(2) == '1' for u in ups):
                        claim(v, 'count')
                elif rhs == '{}':
                    if re.search(r'\b%s\[#%s \+ 1\] = ' % (v, v), body) or re.search(r'table\.insert\(%s,' % v, body):
                        claim(v, 'list')
                    elif re.search(r'\b%s\[[^\]]+\] = true\b' % v, body):
                        claim(v, 'seen')
                elif rhs == 'nil' and sets:
                    for r in sets:
                        base = suggest_value(r, creators)
                        if base:
                            claim(v, base)
                            break
            else:
                claim(m.group(1), suggest_value(rhs, creators))
    if not mapping:
        return text
    return ''.join(mapping.get(s, s) if kind == 'ident' else s for kind, s in toks)


def _claim_any(mapping, used, old, base):
    name, n = base, 2
    while name in used or name in KEYWORDS:
        name = '%s%d' % (base, n)
        n += 1
    used.add(name)
    mapping[old] = name


def humanize(text):
    for _ in range(6):
        new = _pass(text)
        if new == text:
            break
        text = new
    return text


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('input')
    ap.add_argument('-o', '--output')
    a = ap.parse_args(argv)
    with open(a.input, encoding='utf-8') as f:
        text = humanize(f.read())
    if a.output:
        with open(a.output, 'w', encoding='utf-8') as f:
            f.write(text)
    else:
        sys.stdout.write(text)


if __name__ == '__main__':
    main()
