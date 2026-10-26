import struct, os, sys

ALPHA = "\0.0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_"

def decode_name(key: bytes) -> str:
    w = lambda i: struct.unpack_from('>H', key, i*2)[0]
    h = w(0)
    hi = [h % 40, (h // 40) % 40, (h // 1600) % 40]
    s = ''
    for g in range(3):
        u = (hi[g] << 16) | w(g+1)
        for d in (1, 40, 1600, 64000):
            v = (u // d) % 40
            if v != 0 and v < len(ALPHA):
                s += ALPHA[v]
    return s

def encode_name(name: str) -> bytes:
    digits = [0]*12
    n = 0
    for ch in name.upper():
        if n >= 12: break
        pos = ALPHA.find(ch, 1)
        if pos != -1:
            digits[n] = pos
            n += 1
    hi = [0,0,0]; lo = [0,0,0]
    for g in range(3):
        a = digits[g*4:g*4+4]
        u = a[0] + a[1]*40 + a[2]*1600 + a[3]*64000
        hi[g] = u >> 16
        lo[g] = u & 0xffff
    h = hi[0] + hi[1]*40 + hi[2]*1600
    w = [h, lo[0], lo[1], lo[2]]
    return b''.join(struct.pack('>H', x) for x in w)

class Entry:
    __slots__ = ('key','off16','size_or_dir')
    def __init__(self, key, off16, size_or_dir):
        self.key = key; self.off16 = off16; self.size_or_dir = size_or_dir
    def is_dir(self):
        return (self.size_or_dir & 0x80000000) != 0
    def size(self):
        return self.size_or_dir
    def child_delta(self):
        return (self.size_or_dir & 0x7fffffff) // 16
    def name(self):
        return decode_name(self.key)

class PkfInfo:
    def __init__(self, data: bytes):
        ent_size = struct.unpack_from('<H', data, 2)[0]
        total = struct.unpack_from('<I', data, 4)[0]
        assert ent_size == 16, ent_size
        count = total // 16
        self.entries = []
        for i in range(count):
            o = 0x10 + i*16
            key = data[o:o+8]
            off16, size_or_dir = struct.unpack_from('<II', data, o+8)
            self.entries.append(Entry(key, off16, size_or_dir))
        first_dir = next((e for e in self.entries if e.is_dir()), None)
        self.dircount = first_dir.child_delta() if first_dir else len(self.entries)

    def list_extent(self, start, limit):
        limit = min(limit, len(self.entries))
        i = start
        while i < limit:
            e = self.entries[i]
            i += 1
            if e.is_dir():
                return max(min(start + e.child_delta(), limit), i)
        return limit

    def walk_list(self, start, limit, prefix, base, container, f):
        end = start
        first_child = None
        while end < min(limit, len(self.entries)):
            e = self.entries[end]
            if e.is_dir() and first_child is None:
                first_child = start + e.child_delta()
            end += 1
            if first_child is not None and end >= first_child:
                break
        idxs = list(range(start, end))
        for n, i in enumerate(idxs):
            e = self.entries[i]
            name = e.name()
            path = prefix + '/' + name
            off = base + e.off16*16
            if e.is_dir():
                child = start + e.child_delta()
                nxt = limit
                for j in idxs[n+1:]:
                    if self.entries[j].is_dir():
                        nxt = start + self.entries[j].child_delta()
                        break
                self.walk_list(child, nxt, path, off, container, f)
            else:
                f(path, container, off, e.size())

    def walk(self, f):
        root_end = min(self.dircount, len(self.entries))
        for n in range(root_end):
            e = self.entries[n]
            name = e.name()
            if e.is_dir():
                next_dir_child = len(self.entries)
                for x in self.entries[n+1:root_end]:
                    if x.is_dir():
                        next_dir_child = x.child_delta()
                        break
                self.walk_list(e.child_delta(), next_dir_child, name, 0, name, f)
            else:
                f(name, name, 0, e.size())


def main():
    root = sys.argv[1]
    tbl_path = os.path.join(root, 'PKFINFO.TBL')
    data = open(tbl_path, 'rb').read()
    info = PkfInfo(data)
    print(f"entries={len(info.entries)} dircount={info.dircount}")
    results = []
    def cb(path, container, off, size):
        results.append((path, container, off, size))
    info.walk(cb)
    print(f"files={len(results)}")
    for r in results[:30]:
        print(r)
    # verify archive sizes
    by_container = {}
    for path, container, off, size in results:
        by_container.setdefault(container, []).append((path, off, size))
    for cont, files in by_container.items():
        pkb = os.path.join(root, cont + '.PKB')
        if os.path.exists(pkb):
            sz = os.path.getsize(pkb)
            end = max(off+size for _, off, size in files)
            status = "OK" if sz == end else f"MISMATCH sz={sz} end={end}"
            print(f"{cont}: {len(files)} files, archive {sz} bytes, {status}")
        else:
            plain = os.path.join(root, cont)
            exists = os.path.exists(plain)
            print(f"{cont}: {len(files)} files, no .PKB (plain file exists={exists})")
    return info, results, root

if __name__ == '__main__':
    main()
