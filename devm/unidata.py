"""Generate the Unicode normalization tables the harness needs for utf8.nfcnormalize / nfdnormalize
(Roblox-only functions that Luraph's bootstrap feeds into its constant decoding)."""
import sys
import unicodedata


def generate(path):
    dec, ccc, comp = [], [], []
    for cp in range(0xA0, 0x30000):
        if 0xAC00 <= cp <= 0xD7A3 or 0xD800 <= cp <= 0xDFFF:
            continue
        ch = chr(cp)
        c = unicodedata.combining(ch)
        if c:
            ccc.append('[%d]=%d' % (cp, c))
        d = unicodedata.normalize('NFD', ch)
        if d != ch:
            dec.append('[%d]={%s}' % (cp, ','.join(str(ord(x)) for x in d)))
        raw = unicodedata.decomposition(ch)
        if raw and not raw.startswith('<'):
            parts = [int(x, 16) for x in raw.split()]
            if len(parts) == 2 and unicodedata.normalize('NFC', ch) == ch:
                comp.append('["%d_%d"]=%d' % (parts[0], parts[1], cp))
    with open(path, 'w') as f:
        f.write('return {dec={%s},ccc={%s},comp={%s}}\n' % (','.join(dec), ','.join(ccc), ','.join(comp)))


if __name__ == '__main__':
    generate(sys.argv[1])
