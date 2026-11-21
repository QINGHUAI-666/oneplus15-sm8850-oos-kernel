#!/usr/bin/env python3
"""Zero-build compatibility verdict for a kernel Image against this phone's
stock module contract.

    python3 verify_image.py <Image> fw/stock_module_oracle.json

Prints, for every symbol the stock .ko files demand from vmlinux:
    matched / missing / CRC-mismatched
"""
import json, sys
import kcrctab_extract as K


def main():
    img, oracle_path = sys.argv[1], sys.argv[2]
    oracle = json.load(open(oracle_path))
    need = oracle['vmlinux_required']
    table, n1, n2, ba, bb, _ = K.crc_table(img, need)
    have = [s for s in need if s in table]
    miss = [s for s in need if s not in table]
    bad = [(s, need[s], table[s]) for s in have if need[s] != table[s]]
    print(f'image           : {img}')
    print(f'exported symbols: {len(table)} (normal {n1} @ {hex(ba)}, gpl {n2} @ {hex(bb)})')
    print(f'required symbols: {len(need)}')
    print(f'  matched       : {len(have) - len(bad)}')
    print(f'  missing       : {len(miss)}')
    print(f'  CRC mismatch  : {len(bad)}')
    for s, a, b in sorted(bad)[:20]:
        print(f'    {s:45} need={a:#010x} image={b:#010x}')
    for s in miss[:20]:
        print(f'    MISSING {s}')
    print('VERDICT:', 'PASS' if not bad and not miss else 'FAIL')
    return 0 if (not bad and not miss) else 1


if __name__ == '__main__':
    sys.exit(main())
