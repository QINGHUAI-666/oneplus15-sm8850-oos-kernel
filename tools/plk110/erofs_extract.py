#!/usr/bin/env python3
"""Minimal EROFS (uncompressed / flat layout) reader.
Supports FLAT_PLAIN and FLAT_INLINE inodes; refuses compressed images."""
import struct, sys, os

class Erofs:
    def __init__(self, path):
        self.d = open(path, 'rb').read()
        sb = self.d[1024:1024+128]
        magic, = struct.unpack('<I', sb[0:4])
        assert magic == 0xE0F5E1E2, hex(magic)
        self.feature_compat, = struct.unpack('<I', sb[8:12])
        self.blkszbits = sb[12]
        self.root_nid, = struct.unpack('<H', sb[14:16])
        self.inos, = struct.unpack('<Q', sb[16:24])
        self.blocks, = struct.unpack('<I', sb[36:40])
        self.meta_blkaddr, = struct.unpack('<I', sb[40:44])
        self.feature_incompat, = struct.unpack('<I', sb[80:84])
        self.blksz = 1 << self.blkszbits
        assert self.feature_incompat == 0, 'incompat=%#x (compressed/chunked unsupported)' % self.feature_incompat

    def inode(self, nid):
        off = self.meta_blkaddr * self.blksz + (nid << 5)
        fmt, xattr_icount, mode, nlink = struct.unpack('<HHHH', self.d[off:off+8])
        if fmt & 1:   # extended (64 bytes)
            size, = struct.unpack('<Q', self.d[off+8:off+16])
            layout = (fmt >> 1) & 7
            u, ino = struct.unpack('<II', self.d[off+16:off+24])
            uid, gid = struct.unpack('<II', self.d[off+24:off+32])
            nlink, = struct.unpack('<I', self.d[off+44:off+48])
            hdr = 64
        else:         # compact (32 bytes)
            size, reserved = struct.unpack('<II', self.d[off+8:off+16])
            layout = (fmt >> 1) & 7
            u, ino = struct.unpack('<II', self.d[off+16:off+24])
            uid, gid = struct.unpack('<HH', self.d[off+24:off+28])
            hdr = 32
        xattr_size = 0
        if xattr_icount:
            xattr_size = (xattr_icount - 1) * 4 + 12
        return dict(nid=nid, mode=mode, size=size, layout=layout, u=u, ino=ino,
                    xattr_size=xattr_size, hdr_off=off + hdr)

    def read_data(self, ino):
        size, layout, u = ino['size'], ino['layout'], ino['u']
        if layout == 0:      # FLAT_PLAIN
            off = u * self.blksz
            return self.d[off:off+size]
        if layout == 2:      # FLAT_INLINE
            tail = size % self.blksz
            full = size - tail
            off = u * self.blksz
            data = self.d[off:off+full]
            inline_off = ino['hdr_off'] + ino['xattr_size']
            return data + self.d[inline_off:inline_off+tail]
        raise NotImplementedError('layout %d' % layout)

    def listdir(self, ino):
        data = self.read_data(ino)
        entries = []
        i = 0
        first_nameoff = None
        while i + 12 <= len(data):
            nid, nameoff, ftype, _ = struct.unpack('<QHBB', data[i:i+12])
            if nameoff < 12 or nameoff > len(data):
                break
            if first_nameoff is None:
                first_nameoff = nameoff
            if i == 0:
                n = data[nameoff:].split(b'\0')[0]
            end = None
            # next dirent starts at i+12; its name begins at its nameoff
            if i + 12 + 12 <= len(data):
                _, nxt_nameoff, _, _ = struct.unpack('<QHBB', data[i+12:i+24])
                end = nxt_nameoff
            else:
                end = len(data)
            if end is None or end < nameoff or end > len(data):
                end = len(data)
            name = data[nameoff:end].split(b'\0')[0].decode('utf8', 'replace')
            if name:
                entries.append((name, nid, ftype))
            i += 12
        return entries

def walk(fs, nid, path, out, limit=None):
    ino = fs.inode(nid)
    if (ino['mode'] & 0xF000) == 0x4000:
        for name, cnid, ftype in fs.listdir(ino):
            if name in ('.', '..'):
                continue
            walk(fs, cnid, path + '/' + name, out, limit)
    else:
        out.append((path, ino))

def main():
    img, dest = sys.argv[1], sys.argv[2]
    fs = Erofs(img)
    print(f'block={fs.blksz} root_nid={fs.root_nid} inos={fs.inos} incompat={fs.feature_incompat}')
    files = []
    walk(fs, fs.root_nid, '', files)
    print('files:', len(files))
    kos = [f for f in files if f[0].endswith('.ko')]
    print('.ko:', len(kos))
    os.makedirs(dest, exist_ok=True)
    for path, ino in files:
        p = os.path.join(dest, path.lstrip('/'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as f:
            f.write(fs.read_data(ino))
    print('extracted to', dest)
    for p, _ in files[:0]:
        pass

main()
