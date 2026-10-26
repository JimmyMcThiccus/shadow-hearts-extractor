"""Shadow Hearts: Covenant 'SSH1.01a' model (.mdl) parser + software preview renderer.

Format notes: see FORMATS.md.  Mesh entry layout (all little endian):
    0x00  char name[12]
    0x0c  u32 corner_count (n)
    0x10  u32 5c 00 00 ff       (constant)
    0x14  u32 flags             (low byte bits 4-7 vary, high word 0/0x1000/0x2000.. = part/attach id?)
    0x18  u32 ffff + u16        (unknown, ~1.76*n)
    0x20  u32 1
    0x30  corners[n] as three parallel arrays:
              pos  : n * 16  (f32 x,y,z, u16 flags[bit15 = strip ADC, bits0-1 = bone slot], u16 id)
              uv   : n * 4   (u16 s,t in GS 12.4 fixed point = texel * 16)
              nrm  : n * 8   (s16 x,y,z, s16 w=0x7fff)
          then padding so that the entry end is 16-byte aligned relative to the entry start,
          then footer: u32 group_count + 12 bytes, followed by group_count * 16 bytes:
              u8 bones[4] (0xff = unused), u32 0x00010000, u32 first_corner, u32 corner_count
"""
import re
import struct
import zlib

import numpy as np


RIGID_SCALE = 1.0 / 16.0   # s16 position scale of rigid meshes (unverified)


class Mesh:
    error = None
    col = None
    rigid_bone = None
    bsah = False

    def __init__(self, name):
        self.name = name
        self.pos = None      # (n,3) float32
        self.flags = None    # (n,) uint16
        self.vid = None      # (n,) uint16
        self.uv = None       # (n,2) float32 in texels
        self.nrm = None      # (n,3) float32
        self.groups = []     # [(bones[4], start, count)]
        self.hdr_flags = 0

    def triangles(self):
        """Decode triangle strips (ADC bit marks vertices that do not kick a triangle).
        Returns an (m,3) int array of corner indices, winding corrected."""
        tris = []
        if self.pos is None:
            return np.zeros((0, 3), np.int64)
        for _, start, count in self.groups or [([], 0, len(self.pos))]:
            # GS semantics: every vertex enters the 3-vertex window; a vertex with ADC only suppresses the kick, and the
            # winding parity restarts after each ADC vertex (checked: ~0% of triangles then disagree with stored normals)
            last = start - 1
            for i in range(start, start + count):
                if self.flags[i] & 0x8000:
                    last = i
                    continue
                if i - start < 2:
                    continue
                a, b, c = i - 2, i - 1, i
                if (i - last - 1) & 1:
                    a, b = b, a
                tris.append((a, b, c))
        T = np.array(tris, dtype=np.int64).reshape(-1, 3)
        if len(T) and self.nrm is not None:
            # orient every triangle so its geometric normal agrees with the stored vertex normals
            p = self.pos
            fn = np.cross(p[T[:, 1]] - p[T[:, 0]], p[T[:, 2]] - p[T[:, 0]])
            vn = self.nrm[T[:, 0]] + self.nrm[T[:, 1]] + self.nrm[T[:, 2]]
            flip = (fn * vn).sum(1) < 0
            T[flip] = T[flip][:, [1, 0, 2]]
        return T


class Joint:
    def __init__(self, name, parent, child, sibling, pos, quat, extra):
        self.name, self.parent, self.child, self.sibling = name, parent, child, sibling
        self.pos, self.quat, self.extra = pos, quat, extra


def parse_mdl(b, bsah=False):
    assert b[:8] in (b"SSH1.01a", b"SH3_1.01"), "not an SSH model"  # SH3_1.01 = Shadow Hearts: FtNW's renamed/compressed successor
    off_mesh, off_skel, off_mat = struct.unpack("<3I", b[0xc:0x18])
    off_bbox = struct.unpack("<I", b[0x1c:0x20])[0]
    # skeleton
    joints = []
    nj, nx = struct.unpack("<2H", b[off_skel + 4:off_skel + 8])
    for i in range(nj):
        o = off_skel + 0x10 + 0x40 * i
        name = b[o:o + 12].split(b"\0")[0].decode("latin1")
        par, chi, _, sib = struct.unpack("4B", b[o + 12:o + 16])
        p = struct.unpack("<4f", b[o + 0x10:o + 0x20])
        q = struct.unpack("<4f", b[o + 0x20:o + 0x30])
        e = struct.unpack("<4f", b[o + 0x30:o + 0x40])
        joints.append(Joint(name, par, chi, sib, p, q, e))
    # meshes
    meshes = []
    cnt = struct.unpack("<H", b[off_mesh + 4:off_mesh + 6])[0]
    a16 = lambda v: (v + 15) // 16 * 16
    name_re = re.compile(rb"[\x21-\x7e]{1,11}\x00+|[\x21-\x7e]{12}")      # a 12-character name has no NUL terminator

    def is_entry(q):
        return (q + 0x30 <= len(b) and name_re.fullmatch(b[q:q + 12]) is not None and
                b[q + 0x10] in (0x5c, 0x7c) and b[q + 0x11] == 0 and b[q + 0x18:q + 0x1a] == b"\xff\xff")

    starts = [off_mesh + 0x10]
    for _ in range(cnt - 1):
        q = starts[-1] + 0x30 + 16
        while q < len(b) - 0x30 and not is_entry(q):
            q += 16
        if q >= len(b) - 0x30:
            break
        starts.append(q)
    bb = b.find(b"BBHD", starts[-1] + 0x30)
    ends = starts[1:] + [bb if bb > 0 else len(b)]
    bsah_lo = 1 << 60
    if bsah:
        bsah_lo = b.find(b"BSAH")
        if bsah_lo < 0:
            bsah_lo = 1 << 60
        # BSAH blend-shape block (header +0x20): morph sets (mouth/eyelid/eyeblow ...) of full mesh entries
        bo = b.find(b"BSAH")
        eo = b.find(b"TXAH", bo) if bo > 0 else -1
        if bo > 0:
            if eo < 0:                       # SH3_1.01 has no TXAH: the block ends where the TXHD texture section starts
                tx = struct.unpack("<I", b[0x18:0x1c])[0]
                eo = tx if bo < tx < len(b) else len(b)
            ex = [q for q in range(bo + 16, eo - 0x30, 16) if is_entry(q)]
            starts += ex
            ends += ex[1:] + [eo]
    for o, e in zip(starts, ends):
        name = b[o:o + 12].split(b"\0")[0].decode("latin1")
        n = struct.unpack("<I", b[o + 12:o + 16])[0]
        kind = b[o + 0x13]
        hf = struct.unpack("<I", b[o + 0x14:o + 0x18])[0]
        m = Mesh(name)
        m.hdr_flags = hf
        m.rigid_bone = None if kind == 0xFF else kind
        m.hdr_b2 = b[o + 0x12]
        # footer: u32 g, 12 zero bytes, g * 16-byte group records that tile [0, n)
        foot = None
        for slack in (range(0, 0x120, 16) if bsah else (0,)):
            for g in range(1, 4096):
                s0 = e - slack - 16 - 16 * g
                if s0 < o + 0x30:
                    break
                if struct.unpack("<I", b[s0:s0 + 4])[0] != g or b[s0 + 4:s0 + 16] != bytes(12):
                    continue
                nb = 5 if b[:8] == b"SH3_1.01" else 4         # SH3 groups carry a 5th bone (corner flag value 4)
                recs = [(list(b[s0 + 16 + 16 * i:s0 + 16 + nb + 16 * i]),) + struct.unpack("<2I", b[s0 + 24 + 16 * i:s0 + 32 + 16 * i])
                        for i in range(g)]
                pos = 0
                for _, st, c in recs:
                    if st != pos:
                        break
                    pos += c
                if pos == n:
                    foot = (s0, recs)
                    break
            if foot:
                break
        if foot is None:
            m.error = "footer not found"
            meshes.append(m)
            continue
        s0, m.groups = foot
        p0 = o + 0x30
        if kind == 0xFF and bsah and o >= bsah_lo:
            # blend-shape mesh: [8 x u32 frame offsets][uv 4n][16-byte row][pos 16n][nrm 8n][delta streams ...]
            tbl = struct.unpack("<8I", b[p0:p0 + 32])
            k = 0
            while k < 8 and tbl[k] != 0 and (k == 0 or tbl[k - 1] < tbl[k] < 0x100000):   # SH3: unused slots hold junk, not 0
                k += 1
            m.frame_offsets = list(tbl[:k])
            uvo = p0 + a16(4 * k)
            po = uvo + a16(4 * n) + 16
            no = po + 16 * n
            if no + a16(8 * n) > s0:
                m.error = "bsah layout does not fit"
                meshes.append(m)
                continue
            raw = np.frombuffer(b, dtype=np.uint8, count=16 * n, offset=po).reshape(n, 16)
            m.pos = raw[:, :12].copy().view("<f4").reshape(n, 3)
            w = raw[:, 12:].copy().view("<u2").reshape(n, 2)
            m.flags, m.vid = w[:, 0].copy(), w[:, 1].copy()
            m.uv = np.frombuffer(b, dtype="<i2", count=2 * n, offset=uvo).reshape(n, 2).astype(np.float32) / 16.0
            m.nrm = np.frombuffer(b, dtype="<i2", count=4 * n, offset=no).reshape(n, 4)[:, :3].astype(np.float32) / 32767.0
            m.bsah = True
            # morph frames follow the base arrays: [name 12][pos 16n][nrm 8n] padded to 16 B each
            m.morphs = []
            q = no + a16(8 * n)
            for _ in range(k - 1):
                if q + 16 + 24 * n > len(b):
                    break
                fname = b[q:q + 12].split(b"\0")[0].decode("latin1")
                fr = np.frombuffer(b, dtype=np.uint8, count=16 * n, offset=q + 16).reshape(n, 16)
                fp = fr[:, :12].copy().view("<f4").reshape(n, 3)
                m.morphs.append((fname, fp))
                q += a16(16 + 24 * n)
            meshes.append(m)
            continue
        if kind == 0xFF:
            uvo = p0 + 16 * n
            no = uvo + a16(4 * n)
            if no + a16(8 * n) > s0:
                m.error = "skinned layout does not fit"
                meshes.append(m)
                continue
            if no + a16(8 * n) != s0 and s0 - no - a16(8 * n) > 0x40:
                # extra bytes between the arrays and the footer: other stream chunks (audio / control data of
                # the LSD cutscene stream) were interleaved into this mesh's data
                m.error = "interleaved stream data (%d extra bytes)" % (s0 - no - a16(8 * n))
                meshes.append(m)
                continue
            raw = np.frombuffer(b, dtype=np.uint8, count=16 * n, offset=p0).reshape(n, 16)
            m.pos = raw[:, :12].copy().view("<f4").reshape(n, 3)
            w = raw[:, 12:].copy().view("<u2").reshape(n, 2)
            m.flags, m.vid = w[:, 0].copy(), w[:, 1].copy()
            m.uv = np.frombuffer(b, dtype="<i2", count=2 * n, offset=uvo).reshape(n, 2).astype(np.float32) / 16.0
            m.nrm = np.frombuffer(b, dtype="<i2", count=4 * n, offset=no).reshape(n, 4)[:, :3].astype(np.float32) / 32767.0
        else:
            # rigid mesh attached to joint `kind`: s16 xyz + flags, optional uv / rgba
            has_uv, has_col = bool(hf & 0x40), bool(hf & 0x20 or b[o + 0x10] & 0x20)
            need = a16(8 * n) + (a16(4 * n) if has_uv else 0) + (a16(4 * n) if has_col else 0)
            if p0 + need != s0:
                # the flags are not always reliable (N125 backdrop meshes carry colours with hdr 0x5c) and big meshes
                # have a colour array one 16-byte row longer than 4n: pick the (uv, colour) combination that fits
                for hu, hc in ((has_uv, has_col), (True, True), (True, False), (False, True), (False, False)):
                    nd = a16(8 * n) + (a16(4 * n) if hu else 0) + (a16(4 * n) if hc else 0)
                    if s0 - (p0 + nd) in (0, 16):
                        has_uv, has_col, need = hu, hc, s0 - p0
                        break
            if p0 + need != s0 and s0 - p0 in (16 * n + 2 * a16(4 * n), 16 * n + 2 * a16(4 * n) + 16):
                # float variant (N125 'grand'): f32 xyz + u16 flags/id like a skinned mesh, then uv (12.4) and rgba
                raw = np.frombuffer(b, dtype=np.uint8, count=16 * n, offset=p0).reshape(n, 16)
                m.pos = raw[:, :12].copy().view("<f4").reshape(n, 3)
                w = raw[:, 12:].copy().view("<u2").reshape(n, 2)
                m.flags, m.vid = w[:, 0].copy(), w[:, 1].copy()
                q = p0 + 16 * n
                m.uv = np.frombuffer(b, dtype="<i2", count=2 * n, offset=q).reshape(n, 2).astype(np.float32) / 16.0
                m.col = np.frombuffer(b, dtype=np.uint8, count=4 * n, offset=q + a16(4 * n)).reshape(n, 4).copy()
                m.nrm = None
                meshes.append(m)
                continue
            if p0 + need != s0:
                m.error = "rigid layout mismatch (flags %#x, have %d need %d)" % (hf & 0xFF, s0 - p0, need)
                meshes.append(m)
                continue
            raw = np.frombuffer(b, dtype="<i2", count=4 * n, offset=p0).reshape(n, 4)
            m.pos = raw[:, :3].astype(np.float32) * RIGID_SCALE
            m.flags = raw[:, 3].astype(np.uint16)
            m.vid = np.zeros(n, np.uint16)
            q = p0 + a16(8 * n)
            m.uv = np.zeros((n, 2), np.float32)
            if has_uv:
                m.uv = np.frombuffer(b, dtype="<i2", count=2 * n, offset=q).reshape(n, 2).astype(np.float32) / 16.0
                q += a16(4 * n)
            if has_col:
                m.col = np.frombuffer(b, dtype=np.uint8, count=4 * n, offset=q).reshape(n, 4).copy()
            m.nrm = None
        meshes.append(m)
    return joints, meshes


def parse_exhd(b, off=None):
    """Return {tag: (payload_offset, payload_size)} for the EXHD chunk list at header +0x28."""
    if off is None:
        off = struct.unpack("<I", b[0x28:0x2c])[0]
    out = {}
    while b[off:off + 4] == b"EXHD":
        tag = b[off + 4:off + 8].decode("latin1")
        units = struct.unpack("<I", b[off + 12:off + 16])[0]  # size in 16-byte units, incl. 16-byte head
        out[tag] = (off + 16, units * 16 - 16)
        off += units * 16
    return out


def parse_wfrh(b, nj):
    """Virtual blend bones: id nj+k = sum(weight_i * bone_i). Returns [[(bone, weight)]]."""
    ex = parse_exhd(b)
    if "WFRH" not in ex:
        return []
    o, size = ex["WFRH"]
    # head (16 bytes at o-16): "EXHD", tag, u16 version, u16 record count, u32 size in 16-byte units
    cnt = struct.unpack("<H", b[o - 6:o - 4])[0]
    out = []
    for i in range(cnt):
        r = b[o + 16 * i:o + 16 * i + 16]
        n = r[1]
        bones = list(r[4:8])
        w = struct.unpack("<4H", r[8:16])
        out.append([(bones[k], w[k] / 4096.0) for k in range(n) if w[k]])
    return out


# ---------------------------------------------------------------- animation
class Sequence:
    def __init__(self, name, frames, fps):
        self.name, self.frames, self.fps = name, frames, fps
        self.nodes = {}   # name -> dict(rt, rv, pt, pv)


def decode_pos(h):
    """8 bytes -> xyz.  Each axis: (s16 << 5) + 5 low bits of the 4th short; bit 15 of the 4th = unscaled."""
    h = np.asarray(h, dtype=np.int64)
    w = h[..., 3] & 0xFFFF
    low = np.stack([w & 31, (w >> 5) & 31, (w >> 10) & 31], -1)
    v = (h[..., :3] << 5) + low
    scale = np.where(w & 0x8000, 1.0, 0.01)[..., None]
    return v * scale


def parse_sequence(b, off=0):
    a16 = lambda v: (v + 15) // 16 * 16
    assert b[off:off + 4] == b"SQHD"
    name = b[off + 4:off + 16].split(b"\0")[0].decode("latin1")
    nfr, nn, _, fps, naux = struct.unpack("<HHBBH", b[off + 0x10:off + 0x18])
    seq = Sequence(name, nfr, fps)
    o = off + 0x20
    for _ in range(nn):
        nm = b[o:o + 12].split(b"\0")[0].decode("latin1")
        nr, npos = struct.unpack("<HH", b[o + 0x10:o + 0x14])
        p = o + 0x20
        rt = np.frombuffer(b, "<u2", nr, p).astype(np.float64)
        p = a16(p + 2 * nr)
        rv = np.frombuffer(b, "<i2", 4 * nr, p).reshape(nr, 4).astype(np.float64) / 4096.0
        p = a16(p + 8 * nr)
        pt = np.frombuffer(b, "<u2", npos, p).astype(np.float64)
        p = a16(p + 2 * npos)
        pv = decode_pos(np.frombuffer(b, "<i2", 4 * npos, p).reshape(npos, 4))
        p = a16(p + 8 * npos)
        seq.nodes[nm] = dict(rt=rt, rv=rv, pt=pt, pv=pv)
        o = p
    seq.end_offset = o
    return seq


def _slerp(q0, q1, t):
    d = float(np.dot(q0, q1))
    if d < 0:
        q1, d = -q1, -d
    if d > 0.9995:
        q = q0 + (q1 - q0) * t
        return q / np.linalg.norm(q)
    th = np.arccos(min(d, 1.0))
    return (np.sin((1 - t) * th) * q0 + np.sin(t * th) * q1) / np.sin(th)


def sample_track(times, vals, f, rot):
    if len(times) == 1 or f <= times[0]:
        return vals[0]
    if f >= times[-1]:
        return vals[-1]
    i = int(np.searchsorted(times, f, side="right") - 1)
    t = (f - times[i]) / max(times[i + 1] - times[i], 1e-9)
    return _slerp(vals[i], vals[i + 1], t) if rot else vals[i] * (1 - t) + vals[i + 1] * t


def q_mul(a, b):
    x1, y1, z1, w1 = a
    x2, y2, z2, w2 = b
    return np.array([w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2, w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                     w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2, w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2])


def q_rot(q, v):
    x, y, z, w = q
    u = np.array([x, y, z])
    return v + 2 * np.cross(u, np.cross(u, v) + w * v)


def pose(joints, seq, f, qs=(1, -1, -1), ps=(1, -1, -1)):
    """Forward kinematics in joint space (y up, +z forward).  Returns (world_pos, world_quat) per joint.
    Animation values live in a frame rotated 180 deg about X (y,z negated)."""
    n = len(joints)
    bp = np.array([j.quat[:3] for j in joints], dtype=np.float64)
    wp = np.zeros((n, 3))
    wq = np.zeros((n, 4))
    done = [False] * n

    def solve(i):
        if done[i]:
            return
        j = joints[i]
        par = j.parent if j.parent != 255 else None
        node = seq.nodes.get(j.name) if seq else None
        q = np.array([0, 0, 0, 1.0])
        d = np.zeros(3)
        if node is not None:
            q = sample_track(node["rt"], node["rv"], f, True)
            q = np.array([qs[0] * q[0], qs[1] * q[1], qs[2] * q[2], q[3]])
            d = sample_track(node["pt"], node["pv"], f, False)
            d = np.array([ps[0] * d[0], ps[1] * d[1], ps[2] * d[2]])
        if par is None:
            wq[i] = q
            wp[i] = bp[i] + d
        else:
            solve(par)
            wq[i] = q_mul(wq[par], q)
            wp[i] = wp[par] + q_rot(wq[par], bp[i] - bp[par] + d)
        done[i] = True

    for i in range(n):
        solve(i)
    return wp, wq


def skin_mesh(m, joints, virt, wp, wq):
    """Skinned corner positions (n,3) in joint space for pose (wp, wq)."""
    bp = np.array([j.quat[:3] for j in joints], dtype=np.float64)
    P = np.stack([-m.pos[:, 0], -m.pos[:, 1], -m.pos[:, 2]], 1).astype(np.float64)   # mesh -> joint space (point inversion)
    out = np.zeros_like(P)
    nj = len(joints)

    def bone_xform(bid, v):
        if bid < nj:
            return wp[bid] + q_rot(wq[bid], v - bp[bid])
        r = np.zeros(3)
        for b2, w in virt[bid - nj]:
            r = r + w * (wp[b2] + q_rot(wq[b2], v - bp[b2]))
        return r

    for ids, start, count in m.groups:
        for i in range(start, start + count):
            bid = ids[m.flags[i] & (7 if len(ids) > 4 else 3)]
            if bid == 0xFF:
                out[i] = P[i]
            else:
                out[i] = bone_xform(bid, P[i])
    return out


# ---------------------------------------------------------------- preview rendering
def write_png(path, img):
    h, w, _ = img.shape
    raw = b"".join(b"\x00" + img[y].tobytes() for y in range(h))

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def render(meshes, path, view="front", size=512, positions=None, bounds=None):
    """positions: optional list (one per mesh) of (n,3) arrays already in joint space (y up)."""
    if positions is None:
        positions = [np.stack([-m.pos[:, 0], -m.pos[:, 1], -m.pos[:, 2]], 1) for m in meshes]
    P = np.concatenate(positions)
    if bounds is not None:
        P = np.array(bounds)
    lo, hi = P.min(0), P.max(0)
    c = (lo + hi) / 2
    ext = max((hi - lo).max(), 1e-6)
    img = np.full((size, size, 3), 24, np.uint8)
    zb = np.full((size, size), -1e9, np.float32)
    axes = {"front": (0, 1, 2), "side": (2, 1, 0), "top": (0, 2, 1)}[view]
    palette = [(230, 120, 90), (110, 200, 120), (100, 150, 230), (230, 210, 100), (200, 110, 220),
               (100, 220, 220), (240, 160, 60), (180, 180, 180), (240, 120, 160)]
    for mi, m in enumerate(meshes):
        T = m.triangles()
        if not len(T):
            continue
        col = np.array(palette[mi % len(palette)], np.float32)
        Q = (positions[mi] - c) / ext
        X = Q[:, axes[0]] * size * 0.9 + size / 2
        Y = size / 2 - Q[:, axes[1]] * size * 0.9      # positions are y-up
        Z = Q[:, axes[2]]
        for a, b_, cc in T:
            xs, ys, zs = X[[a, b_, cc]], Y[[a, b_, cc]], Z[[a, b_, cc]]
            x0, x1 = int(max(xs.min(), 0)), int(min(xs.max() + 1, size - 1))
            y0, y1 = int(max(ys.min(), 0)), int(min(ys.max() + 1, size - 1))
            if x1 <= x0 or y1 <= y0:
                continue
            gx, gy = np.meshgrid(np.arange(x0, x1) + .5, np.arange(y0, y1) + .5)
            d = (ys[1] - ys[2]) * (xs[0] - xs[2]) + (xs[2] - xs[1]) * (ys[0] - ys[2])
            if abs(d) < 1e-9:
                continue
            w0 = ((ys[1] - ys[2]) * (gx - xs[2]) + (xs[2] - xs[1]) * (gy - ys[2])) / d
            w1 = ((ys[2] - ys[0]) * (gx - xs[2]) + (xs[0] - xs[2]) * (gy - ys[2])) / d
            w2 = 1 - w0 - w1
            msk = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
            if not msk.any():
                continue
            z = w0 * zs[0] + w1 * zs[1] + w2 * zs[2]
            sub = zb[y0:y1, x0:x1]
            ok = msk & (z > sub)
            if view == "side":
                pass
            sub[ok] = z[ok]
            n = m.nrm[[a, b_, cc]].mean(0)
            shade = 0.45 + 0.55 * max(0.0, float(n[2] if view == "front" else 0.5))
            img[y0:y1, x0:x1][ok] = (col * shade).astype(np.uint8)
    write_png(path, img)


if __name__ == "__main__":
    import sys
    data = open(sys.argv[1], "rb").read()
    joints, meshes = parse_mdl(data)
    print(len(joints), "joints,", len(meshes), "meshes")
    for m in meshes:
        print(" ", m.name, len(m.pos), "corners,", len(m.groups), "groups,", len(m.triangles()), "tris")
    if len(sys.argv) > 2:
        render(meshes, sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "front")



