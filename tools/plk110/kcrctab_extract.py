#!/usr/bin/env python3
"""Extract the exported-symbol CRC table (__kcrctab + __kcrctab_gpl) from an
arm64 kernel Image — no build, no Module.symvers, no symbol table.

Layout (include/asm-generic/vmlinux.lds.h), each array SORT()ed by symbol name:
    __ksymtab (N1 x 12B PREL32) | __ksymtab_gpl (N2 x 12B PREL32)
    __kcrctab (N1 x u32)        | __kcrctab_gpl (N2 x u32)
    __ksymtab_strings
"""
import struct, re, sys, json, collections

NAME_RE = re.compile(rb'^[A-Za-z_][A-Za-z0-9_.$]{1,120}$')
STEP = 12


def entry_name(d, p):
    v_off, n_off, ns_off = struct.unpack_from('<iii', d, p)
    na = p + 4 + n_off
    if not (0 <= na < len(d) - 2):
        return None
    e = d.find(b'\0', na)
    if e < 0 or not (2 <= e - na <= 120):
        return None
    nm = d[na:e]
    if not NAME_RE.match(nm):
        return None
    return nm.decode()


def find_ksymtab(d):
    """Return list of (offset, name) for the densest dense 12-byte array."""
    best = None
    p = 0
    n = len(d)
    while p < n - STEP:
        if entry_name(d, p):
            start = p
            ents = []
            q = p
            misses = 0
            while q < n - STEP:
                nm = entry_name(d, q)
                if nm:
                    ents.append((q, nm))
                    q += STEP
                    misses = 0
                else:
                    q += STEP
                    misses += 1
                    if misses > 2:
                        break
            if best is None or len(ents) > len(best):
                best = ents
            p = q
        else:
            p += 4
    return best


def split_sorted(ents):
    for i in range(1, len(ents)):
        if ents[i][1] < ents[i - 1][1]:
            return i
    return len(ents)


def crc_table(img_path, expect=None):
    d = open(img_path, 'rb').read()
    ents = find_ksymtab(d)
    N1 = split_sorted(ents)
    N2 = len(ents) - N1
    kt_end = ents[-1][0] + STEP
    baseA = baseB = None

    if expect:
        tally = collections.Counter()
        idxs = list(range(0, len(ents) - 1, max(1, len(ents) // 60)))
        for i in idxs:
            a, b = ents[i][1], ents[i + 1][1]
            if a in expect and b in expect and (i < N1 - 1 or i >= N1):
                pat = struct.pack('<II', expect[a] & 0xffffffff, expect[b] & 0xffffffff)
                off = d.find(pat, kt_end)
                while off != -1:
                    base = off - 4 * (i if i < N1 else i - N1)
                    tally[(base, 'A' if i < N1 else 'B')] += 1
                    off = d.find(pat, off + 1)
                    if tally.total() > 400: break
        for (base, arr), votes in tally.most_common(50):
            if arr == 'A' and baseA is None and votes >= 20:
                baseA = base
            if arr == 'B' and baseB is None and votes >= 20:
                baseB = base
            if baseA is not None and baseB is not None:
                break
    if baseA is None:
        for pad in (0, 4, 8, 12, 16, 32):
            cand = kt_end + pad
            candB = cand + 4 * N1
            if candB + 4 * N2 <= len(d):
                baseA, baseB = cand, candB
                break
    assert baseA is not None and baseB is not None, 'kcrctab not located'
    crcA = struct.unpack_from('<%dI' % N1, d, baseA)
    crcB = struct.unpack_from('<%dI' % N2, d, baseB)
    table = {}
    for i in range(N1):
        table[ents[i][1]] = crcA[i]
    for i in range(N2):
        table[ents[N1 + i][1]] = crcB[i]
    return table, N1, N2, baseA, baseB, d


def main():
    img = sys.argv[1]
    expect = None
    if len(sys.argv) > 3 and sys.argv[3] != '-':
        expect = json.load(open(sys.argv[3])).get('need') or json.load(open(sys.argv[3]))
    table, N1, N2, baseA, baseB, d = crc_table(img, expect)
    print(f'{img}: normal={N1} @{hex(baseA)}  gpl={N2} @{hex(baseB)}  total={len(table)}')
    json.dump({'table': table, 'n1': N1, 'n2': N2}, open(sys.argv[2], 'w'))
    print('written', sys.argv[2])


if __name__ == '__main__':
    main()
