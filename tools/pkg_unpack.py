"""Unpack the generic 'package' container used inside many SH2 files.

Layout:  u32 count, u32 total_size, then count * (char ext[4] ('mdl\\0'), u32 offset).
Entry i spans [offset_i, offset_{i+1}) (the last one ends at total_size).
Inner files get named  <stem>.<index>.<ext>  and are unpacked recursively
if they are packages themselves.

usage: python pkg_unpack.py <in dir> <out dir> [exts, e.g. SET,BCM]
"""
import os
import struct
import sys


def parse_pkg(b):
    """Return [(ext, start, end)] if b looks like a package, else None."""
    if len(b) < 16:
        return None
    c, sz = struct.unpack("<II", b[:8])
    if not (0 < c < 4096) or 8 + 8 * c > len(b) or sz > len(b) or sz < 8 + 8 * c:
        return None
    ents, last = [], 8 + 8 * c
    for i in range(c):
        e = b[8 + 8 * i:16 + 8 * i]
        ext = e[:4]
        off = struct.unpack("<I", e[4:])[0]
        if ext[3] != 0 or not all(97 <= x <= 122 or 48 <= x <= 57 for x in ext[:3]):
            return None
        if off < last or off > sz:
            return None
        last = off
        ents.append((ext[:3].decode(), off))
    out = []
    for i, (ext, off) in enumerate(ents):
        end = ents[i + 1][1] if i + 1 < len(ents) else sz
        out.append((ext, off, end))
    return out


def unpack(b, dst_stem, depth=0):
    ents = parse_pkg(b)
    if ents is None or depth > 4:
        return False
    os.makedirs(os.path.dirname(dst_stem), exist_ok=True)
    for i, (ext, s, e) in enumerate(ents):
        data = b[s:e]
        stem = f"{dst_stem}.{i:02d}"
        if not unpack(data, stem, depth + 1):
            with open(f"{stem}.{ext}", "wb") as f:
                f.write(data)
    return True


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    exts = {x.upper() for x in sys.argv[3].split(",")} if len(sys.argv) > 3 else None
    n = 0
    for dp, _, fs in os.walk(src):
        for f in fs:
            if exts and f.rsplit(".", 1)[-1].upper() not in exts:
                continue
            p = os.path.join(dp, f)
            b = open(p, "rb").read()
            rel = os.path.relpath(p, src)
            if unpack(b, os.path.join(dst, os.path.splitext(rel)[0] + "." + f.rsplit(".", 1)[-1].lower())):
                n += 1
    print("unpacked", n)
