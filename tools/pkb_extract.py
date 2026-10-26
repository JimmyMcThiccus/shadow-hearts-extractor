"""Shadow Hearts: Covenant (SLUS-210.41) PKFINFO.TBL / *.PKB extractor.

PKFINFO.TBL layout (reverse engineered from FUN_00228880 / FUN_0022a360 in the ELF):
  header (16 bytes): u16 ?, u16 entry_size(=0x10), u32 table_bytes, 8 pad
  entry (16 bytes): char name[8] (packed base-40, 12 chars), u32 offset, u32 v
      v bit31 clear -> leaf: v = size in bytes, offset (in 16-byte units!) relative to parent
      v bit31 set   -> directory: (v & 0x7fffffff)/16 = index of first child entry
                       (relative to the start of the parent's child block)
  Entries 0..13 are top level: 3 plain .BIN files + 11 .PKB archives.
  Each top-level PKB owns a contiguous region of the table, laid out
  breadth-first. A leaf's absolute offset is the sum of the offsets of every
  directory above it plus its own offset.

usage: python pkb_extract.py <ISO Extracted dir> <out dir> [--list] [--skip LSD.PKB]
"""
import os
import struct
import sys

CS = ".0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_"
TOP = 14


def dec_name(b):
    w = [(b[2 * i] << 8) | b[2 * i + 1] for i in range(4)]
    a = [w[0] % 40, (w[0] // 40) % 40, (w[0] // 1600) % 40]
    out = ""
    for i in range(3):
        v = (a[i] << 16) | w[i + 1]
        for dv in (1, 40, 1600, 64000):
            c = (v // dv) % 40
            if c == 0:
                return out
            out += CS[c - 1]
    return out


def load_leaves(iso_dir):
    d = open(os.path.join(iso_dir, "PKFINFO.TBL"), "rb").read()
    n = (len(d) - 16) // 16
    E = []
    for i in range(n):
        e = d[16 + i * 16:32 + i * 16]
        u = struct.unpack("<4I", e)
        E.append(dict(name=dec_name(e[:8]), off=u[2], v=u[3], dir=bool(u[3] >> 31)))
    child = lambda e: (e["v"] & 0x7FFFFFFF) // 16
    leaves = []  # (path, pkb file, abs offset, size)
    topdirs = [i for i in range(TOP) if E[i]["dir"]]
    for i in range(TOP):
        if not E[i]["dir"]:
            leaves.append((E[i]["name"], E[i]["name"], 0, E[i]["v"]))
    # Blocks are stored depth-first: a directory's block is followed immediately
    # by the block of its first subdirectory, and so on.  A block therefore ends
    # at its first subdirectory's child start, or at `limit` if it has none.
    def walk(path, aoff, s, limit, pk):
        end = limit
        for j in range(s, limit):
            if E[j]["dir"]:
                end = s + child(E[j])
                break
        subdirs = [j for j in range(s, end) if E[j]["dir"]]
        for j in range(s, end):
            e = E[j]
            if not e["dir"]:
                leaves.append(("/".join(path + [e["name"]]), pk, aoff + e["off"] * 16, e["v"]))
        for k, j in enumerate(subdirs):
            nl = s + child(E[subdirs[k + 1]]) if k + 1 < len(subdirs) else limit
            walk(path + [E[j]["name"]], aoff + E[j]["off"] * 16, s + child(E[j]), nl, pk)

    for ti, i in enumerate(topdirs):
        pk = E[i]["name"] + ".PKB"
        limit = child(E[topdirs[ti + 1]]) if ti + 1 < len(topdirs) else n
        walk([pk], 0, child(E[i]), limit, pk)
    return leaves


def main():
    iso, out = sys.argv[1], sys.argv[2]
    skip = set(sys.argv[sys.argv.index("--skip") + 1:]) if "--skip" in sys.argv else set()
    leaves = load_leaves(iso)
    if "--list" in sys.argv:
        for p, pk, off, sz in leaves:
            print(f"{off:010x} {sz:9d} {p}")
        return
    handles = {}
    for p, pk, off, sz in leaves:
        if pk in skip:
            continue
        dst = os.path.join(out, *p.split("/")[1:]) if pk.endswith(".PKB") else os.path.join(out, p)
        if pk.endswith(".PKB"):
            dst = os.path.join(out, pk[:-4], *p.split("/")[1:])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        # duplicate names inside one directory: disambiguate by offset
        if os.path.exists(dst):
            b, e = os.path.splitext(dst)
            dst = f"{b}_{off:x}{e}"
        f = handles.get(pk) or handles.setdefault(pk, open(os.path.join(iso, pk), "rb"))
        f.seek(off)
        with open(dst, "wb") as o:
            left = sz
            while left:
                chunk = f.read(min(left, 1 << 22))
                o.write(chunk)
                left -= len(chunk)
    print("done", len(leaves))


if __name__ == "__main__":
    main()
