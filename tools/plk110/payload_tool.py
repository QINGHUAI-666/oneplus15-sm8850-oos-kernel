#!/usr/bin/env python3
"""Minimal Android OTA payload.bin parser (no protobuf dependency).
Reads DeltaArchiveManifest, lists partitions, extracts full-OTA REPLACE partitions.
"""
import sys, struct, os, hashlib

def rv(buf, i):
    r = 0; s = 0
    while True:
        b = buf[i]; i += 1
        r |= (b & 0x7f) << s
        if not b & 0x80: return r, i
        s += 7

def fields(buf):
    i = 0; n = len(buf)
    while i < n:
        key, i = rv(buf, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = rv(buf, i); yield f, wt, v
        elif wt == 2:
            l, i = rv(buf, i); yield f, wt, buf[i:i+l]; i += l
        elif wt == 5:
            yield f, wt, buf[i:i+4]; i += 4
        elif wt == 1:
            yield f, wt, buf[i:i+8]; i += 8
        else:
            raise ValueError('wt %d' % wt)

def parse_manifest(man):
    parts = []
    for f, wt, v in fields(man):
        if f == 13 and wt == 2:
            pname = None; ops = []; ninfo = None
            for pf, pwt, pv in fields(v):
                if pf == 1: pname = pv.decode()
                elif pf == 8 and pwt == 2:
                    op = {'type': None, 'data_offset': None, 'data_length': None,
                          'dst': [], 'src': []}
                    for of, owt, ov in fields(pv):
                        if of == 1: op['type'] = ov
                        elif of == 2: op['data_offset'] = ov
                        elif of == 3: op['data_length'] = ov
                        elif of == 6 and owt == 2:
                            ext = {}
                            for ef, ewt, ev in fields(ov):
                                if ef == 1: ext['start'] = ev
                                elif ef == 2: ext['num'] = ev
                            op['dst'].append(ext)
                        elif of == 4 and owt == 2:
                            ext = {}
                            for ef, ewt, ev in fields(ov):
                                if ef == 1: ext['start'] = ev
                                elif ef == 2: ext['num'] = ev
                            op['src'].append(ext)
                    ops.append(op)
                elif pf == 7 and pwt == 2:
                    for nf, nwt, nv in fields(pv):
                        if nf == 1: ninfo = nv
            parts.append({'name': pname, 'size': ninfo, 'ops': ops})
    return parts

def main():
    pw = sys.argv[1]
    action = sys.argv[2] if len(sys.argv) > 2 else 'list'
    with open(pw, 'rb') as f:
        hdr = f.read(24)
        magic, ver, ms, sig = hdr[:4], struct.unpack('>Q', hdr[4:12])[0], struct.unpack('>Q', hdr[12:20])[0], struct.unpack('>I', hdr[20:24])[0]
        assert magic == b'CrAU', magic
        man = f.read(ms)
        base = 24 + ms + sig
        parts = parse_manifest(man)
        if action == 'list':
            print('payload version=%d manifest=%d sig=%d data_base=%d total_parts=%d' % (ver, ms, sig, base, len(parts)))
            tot = 0
            for p in parts:
                n = p['size'] or 0; tot += n
                print('%-24s %12.2f MB  ops=%3d  types=%s' % (p['name'], n/1e6, len(p['ops']), sorted({o['type'] for o in p['ops']})))
            print('sum partitions: %.2f GB' % (tot/1e9))
            return
        # extract selected partitions
        want = sys.argv[3:] if len(sys.argv) > 3 else [p['name'] for p in parts]
        outdir = os.path.join(os.path.dirname(pw), 'parts')
        os.makedirs(outdir, exist_ok=True)
        for p in parts:
            if p['name'] not in want: continue
            path = os.path.join(outdir, p['name'] + '.img')
            print('extracting', p['name'], 'size %.2f MB' % ((p['size'] or 0)/1e6))
            with open(path, 'wb') as out:
                out.truncate(p['size'] or 0)
                for op in p['ops']:
                    if op['type'] == 6:  # ZERO
                        continue
                    if op['type'] not in (0, 1, 8):
                        raise SystemExit('non-full-OTA op type %s in %s' % (op['type'], p['name']))
                    f.seek(base + op['data_offset'])
                    data = f.read(op['data_length'])
                    if op['type'] == 8:
                        import lzma
                        data = lzma.decompress(data)
                    elif op['type'] == 1:
                        import bz2
                        data = bz2.decompress(data)
                    pos = 0
                    for ext in op['dst']:
                        off = ext['start'] * 4096
                        ln = ext['num'] * 4096
                        out.seek(off)
                        out.write(data[pos:pos+ln])
                        pos += ln
            print('  ->', path, os.path.getsize(path))
            # quick kernel version scan if boot-ish
            if any(k in p['name'] for k in ('boot', 'dtbo', 'dlkm')):
                with open(path, 'rb') as g:
                    blob = g.read()
                idx = blob.find(b'Linux version ')
                if idx >= 0:
                    print('  kernel banner:', blob[idx:idx+200].split(b'\x00')[0].decode('utf8', 'replace'))

main()
