"""Make a lifted Luraph bootstrap layer runnable outside Roblox.

The bootstrap is ordinary Lua once lifted.  Two edits are needed before it can be executed
natively by the harness in place of the VM:
  * its "tamper detected" routine (wipes the stream readers, then spins forever) becomes a
    logging no-op, so a failed environment check no longer kills the run;
  * the 8-byte XOR key that decrypts the main bytecode is recovered statistically right before
    the decrypt loop (see __LPH_FIXKEY in harness.lua), because the key the bootstrap computes
    itself depends on every environment check passing.
"""
import re


class BootstrapError(Exception):
    pass


def _func_end(lines, i, indent):
    for j in range(i + 1, len(lines)):
        if lines[j].startswith(indent + 'end') and len(lines[j]) - len(lines[j].lstrip('\t')) == len(indent):
            return j
    raise BootstrapError('unterminated function at line %d' % (i + 1))


def patch(text):
    lines = text.split('\n')
    info = {'crash': 0, 'fixkey': 0}

    # -- tamper routine
    head = re.compile(r'^(\t*)(local )?(\w+) = function\(\)$')
    spans = []
    for i, ln in enumerate(lines):
        m = head.match(ln)
        if not m:
            continue
        j = _func_end(lines, i, m.group(1))
        body = lines[i + 1:j]
        spin = any(body[k].strip() == 'while true do' and body[k + 1].strip() == 'end' for k in range(len(body) - 1))
        if spin and any('string.rep(" ", 8)' in b for b in body):
            spans.append((i, j, m.group(1)))
    # keep innermost matches only
    spans = [s for s in spans if not any(o is not s and s[0] <= o[0] and o[1] <= s[1] for o in spans)]
    for i, j, indent in sorted(spans, reverse=True):
        lines[i + 1:j] = [indent + '\t__LPH_CRASH()']
        info['crash'] += 1

    # -- key recovery in front of the decrypt loop
    keyre = re.compile(r'\[(\w+) % 8 \+ 1\]')
    for i, ln in enumerate(lines):
        m = keyre.search(ln)
        if not m or 'bit32.bxor' not in ln:
            continue
        ivar = m.group(1)
        km = re.search(r'(\w+)\[' + ivar + r' % 8 \+ 1\]', ln)
        wm = re.search(r'\((\w+), (\w+) \+ ' + ivar + r', \(?bit32\.bxor', ln)
        if not km or not wm:
            continue
        for j in range(i - 1, max(i - 40, -1), -1):
            fm = re.match(r'^(\t*)for ' + ivar + r' = 0, (\w+) - 1 do$', lines[j])
            if fm:
                lines.insert(j, '%s__LPH_FIXKEY(%s, %s, %s, %s)' % (fm.group(1), km.group(1), wm.group(1), wm.group(2), fm.group(2)))
                info['fixkey'] += 1
                break
        break
    return '\n'.join(lines), info
