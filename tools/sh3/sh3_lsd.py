"""Parse a Shadow Hearts: From the New World in-engine cutscene (LSD).

File:
  0x00 'LSDH' u32 1, u32 frames, u16 ?, u8 fps(+0x13) ...
  0x40.. directory: records `u16 2, u16 rows, u16 track, u16 id, char tag[4], ...` (rows * 16 bytes each), starting with
         LSDC.  Common fields: +0x18 u32 first frame, +0x1c u32 last frame, +0x28 char name[16].
         MDLS +0x3c char file[] (e.g. CAPO_M.MDL, the *_MS.MDL twin is the shadow), ANMS +0x48.. char joint[16] each,
         MPS (stage = a field map, streamed cx-compressed on its track), CAMF camera, LGTF lights, PCMS voice, LIPS, LSCR.
  stream: 3-frame slices
    slice table : u32 slice, u32 rows, u32 flags, u32 frame + rows * 16 bytes   (byte 3 == 0)
                  the rows hold events and, mid-scene, more directory records (models that join later)
    chunk       : u16 seq, 0, u8 track, u32 rows, u32 segment, u32 0 + rows * 16 bytes
                  animation payload (ANMS tracks): u32 first_frame, u32 3, u16 nrec, u16 w, u32 0, then SH2-style records
                  `u8 joint, u8 kind, u8, u8, u32 frame mask, (kind & 15) * 8 bytes`
"""
import os
import re
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # shared tools/ (ssh_model, ssh_gltf)
from ssh_model import decode_pos  # noqa: E402

TAG = re.compile(rb"[A-Z.][A-Z0-9 ]{3}")          # e.g. MDLS, MPS , .EFD


def _cstr(b):
    s = b.split(b"\0")[0]
    return s.decode("latin1") if re.fullmatch(rb"[\x21-\x7e]*", s) else ""


def _record(d, q):
    """Directory record at q -> dict, or None."""
    if q + 16 > len(d):
        return None
    two, rows, track, rid = struct.unpack_from("<4H", d, q)
    tag = d[q + 8:q + 12]
    if two != 2 or not rows or not TAG.fullmatch(tag):
        return None
    raw = d[q:q + rows * 16]
    r = {"tag": tag.decode().strip(), "track": track, "id": rid, "rows": rows, "off": q,
         "first": struct.unpack_from("<I", raw, 0x18)[0] if len(raw) >= 0x20 else 0,
         "last": struct.unpack_from("<I", raw, 0x1c)[0] if len(raw) >= 0x20 else 0,
         "name": _cstr(raw[0x28:0x38]) if len(raw) >= 0x38 else ""}
    if r["tag"] == "MDLS" and len(raw) >= 0x50:
        r["file"] = _cstr(raw[0x3c:0x50])
    if r["tag"] == "ANMS":
        js = []
        njoints = struct.unpack_from("<H", raw, 0x3a)[0] if len(raw) >= 0x40 else rows - 4
        for i in range(min(rows - 4, njoints)):
            nm = raw[0x48 + 16 * i:0x58 + 16 * i].split(b"\0")[0]       # char name[16]
            if not nm or not re.fullmatch(rb"[\x21-\x7e]+", nm):
                break
            js.append(nm.decode("latin1"))
        r["joints"] = js
        # property table (events of id 0 set these by index): after the joint table, 16 bytes each, char name[10]
        if len(raw) >= 0x40:
            nj, npr = struct.unpack_from("<H", raw, 0x3a)[0], struct.unpack_from("<H", raw, 0x3e)[0]
            pt = 0x48 + nj * 16
            r["props"] = [_cstr(raw[pt + 16 * i:pt + 16 * i + 10]) for i in range(npr) if pt + 16 * i + 16 <= len(raw)]
    return r


def parse(path):
    d = open(path, "rb").read()
    frames = struct.unpack_from("<I", d, 8)[0]
    fps = d[0x13] or 30
    q = d.find(b"LSDC", 0x40, 0x4000) - 8
    if q < 0:
        raise ValueError("no LSDC directory")
    recs = []
    cur = {}                              # track -> record currently using it (tracks are reused mid-scene)
    while True:
        r = _record(d, q)
        if r is None:
            break
        r["chunks"] = []
        recs.append(r)
        cur[r["track"]] = r
        q += r["rows"] * 16
    start = q
    chunks = {}
    pos = start
    while pos + 16 <= len(d):
        h = d[pos:pos + 16]
        if h[3] == 0:
            n = struct.unpack_from("<I", h, 4)[0]
            if n > 0x4000:
                break
            body = d[pos + 16:pos + 16 + 16 * n]
            k = 0
            while k < n:
                r = _record(body, 16 * k)
                if r:
                    r["off"] += pos + 16
                    r["chunks"] = []
                    recs.append(r)
                    cur[r["track"]] = r
                    k += r["rows"]
                else:
                    k += 1
            pos += 16 + 16 * n
            continue
        if h[2] or any(h[12:16]):
            break
        rows, seg = struct.unpack_from("<II", h, 4)
        c = (pos + 16, rows * 16, seg)
        chunks.setdefault(h[3], []).append(c)
        if h[3] in cur:
            cur[h[3]]["chunks"].append(c)
        pos += 16 + rows * 16
    return {"path": path, "data": d, "frames": frames, "fps": fps, "records": recs, "chunks": chunks, "end": pos}


def _chunks(sc, rec):
    """Chunks of a record (dict) -- or, given a track number, of every record that used the track."""
    return rec["chunks"] if isinstance(rec, dict) else sc["chunks"].get(rec, ())


def track_bytes(sc, rec):
    d = sc["data"]
    return b"".join(d[o:o + n] for o, n, _ in _chunks(sc, rec))


def decode_anim(sc, rec):
    """SH2-style animation records of one ANMS record -> {"rot": {joint: {frame: q}}, "pos": ..., "morph": ..., "frames": set}."""
    d = sc["data"]
    T = {"rot": {}, "pos": {}, "morph": {}, "frames": set(), "events": []}
    for o, n, _seg in _chunks(sc, rec):
        if n < 16:
            continue
        ff, three, nrec = struct.unpack_from("<IIH", d, o)
        if three != 3:
            continue
        T["frames"].add(ff)
        end = o + n
        p = o + 16
        face = False
        for _ in range(nrec):
            if p + 8 > end:
                break
            i, k, _a, _b, cnt = struct.unpack_from("<BBBBI", d, p)
            if face and k not in (0x41, 0x42, 0x43, 0xc1, 0xc2, 0xc3):
                break                         # face records close the chunk; what follows is not joint data
            if k in (0x41, 0x42, 0x43, 0xc1, 0xc2, 0xc3):
                face = True
                # event records (engine FUN_002e38b0 / FUN_002e2670); the id is the event type:
                #   1 face units, 8 bytes each: [set][n][weights 0..255]; set 0 = mouth (A I U E O L H), set 5 = eyebrow (U E A)
                #   0 set property props[u16 0] = u16 1 (e.g. TXAeye = eye texture frame, DYNA, BLINK)
                #   8 'Visible' = u16 1;  9 toggle joint interpolation (off across camera cuts);  10 drop shadow (1 model, 2 simple, 0 off)
                if i in (0, 8, 9, 10):
                    a0, a1 = struct.unpack_from("<HH", d, p + 8)
                    T["events"].append((ff + (1 if k & 0x80 else 0), i, a0, a1))
                if i == 1:
                    t = ff + (1 if k & 0x80 else 0)
                    for u in range(k & 15):
                        m = d[p + 8 + 8 * u:p + 16 + 8 * u]
                        if len(m) == 8 and m[0] in (0, 1, 5) and 1 <= m[1] <= 7:
                            T["morph"].setdefault(m[0], {}).setdefault(t, np.array(list(m[2:2 + min(m[1], 6)]), np.float64))
                p += 8 + (k & 15) * 8
                continue
            nk = k & 15
            body = nk * 8
            data = d[p + 8:p + 8 + body]
            if k in (1, 2, 3, 0x21, 0x22, 0x23):
                fr = [f for f in range(3) if cnt >> f & 1][:nk]
                if len(fr) < nk:
                    fr = list(range(nk))
                keys = np.frombuffer(data, "<i2").reshape(-1, 4)
                for f, v in zip(fr, keys):
                    if k < 0x20:
                        T["rot"].setdefault(i, {})[ff + f] = v.astype(np.float64) / 4096.0
                    else:
                        T["pos"].setdefault(i, {})[ff + f] = decode_pos(v)
            p += 8 + body
    return T


LIGHT_KINDS = ("ambient", "sun", "point", "spot")


def decode_lights(sc, rec):
    """LGTF light track -> [{"slot", "kind", "first", "last", "keys": [{frame, rgb, k, pos, dir, cone, p6c, step}]}],
    one entry per created light (a slot is re-created with another kind later in a scene).

    From the engine's interpreter FUN_002e8d10 (SLUS_213.26).  Chunk: +2 u8 fps, +4 u32 first frame, +8 u16 frames; at +0x10
    groups of [u16 frame][ops...], the group's last op has bit 15 set.  Op: bits 0-6 slot, bits 7-9 type:
      1 create:  u16 cfg (bits 0-2 kind: 0 ambient, 1 directional, 2 point, 3 spot), u16 mask
      2 key:     u16 flags + fields in this order -- bit 6 colour 3 x s16 / 16384, bit 7 f32 (light +0x64), bit 8 position
                 3 x f32 (else bit 9 packed decode_pos), bit 10 direction 4 x s16 / 32768, bit 11 cone s16 / 4096 rad,
                 bit 12 s16 / 4096 (light +0x6c); flags bit 15 = no interpolation towards the next key of the slot
      4 delete, 7 end of stream.
    The engine lerps every field towards the slot's next key."""
    d = sc["data"]
    cur, out = {}, []
    for o, n, _seg in _chunks(sc, rec):
        if n < 0x12:
            continue
        p, end = o + 0x10, o + n
        while p + 4 <= end:
            frame = struct.unpack_from("<H", d, p)[0]
            p += 2
            last = False
            while not last and p + 2 <= end:
                op = struct.unpack_from("<H", d, p)[0]
                p += 2
                slot, typ, last = op & 0x7f, (op >> 7) & 7, bool(op & 0x8000)
                S = cur.get(slot)
                if typ == 1:
                    cfg, _mask = struct.unpack_from("<HH", d, p)
                    p += 4
                    if _mask != 0xffff:
                        if S is not None and S["last"] is None:
                            S["last"] = frame
                        S = {"slot": slot, "kind": LIGHT_KINDS[cfg & 7] if (cfg & 7) < 4 else "light%d" % (cfg & 7),
                             "first": frame, "last": None, "keys": []}
                        cur[slot] = S
                        out.append(S)
                elif typ == 2:
                    if S is None:
                        S = {"slot": slot, "kind": None, "first": frame, "last": None, "keys": []}   # skipped below
                        cur[slot] = S
                    fl = struct.unpack_from("<H", d, p)[0]
                    p += 2
                    k = {"frame": frame, "step": bool(fl & 0x8000)}
                    if fl & 0x40:
                        k["rgb"] = [v / 16384.0 for v in struct.unpack_from("<3h", d, p)]
                        p += 6
                    if fl & 0x80:
                        k["k"] = struct.unpack_from("<f", d, p)[0]
                        p += 4
                    if fl & 0x100:
                        k["pos"] = list(struct.unpack_from("<3f", d, p))
                        p += 12
                    elif fl & 0x200:
                        k["pos"] = [float(x) for x in decode_pos(np.array(struct.unpack_from("<4h", d, p), np.int64))]
                        p += 8
                    if fl & 0x400:
                        k["dir"] = [v / 32768.0 for v in struct.unpack_from("<3h", d, p)]
                        p += 8
                    if fl & 0x800:
                        k["cone"] = struct.unpack_from("<h", d, p)[0] / 4096.0 * 180.0 / np.pi
                        p += 2
                    if fl & 0x1000:
                        k["p6c"] = struct.unpack_from("<h", d, p)[0] / 4096.0
                        p += 2
                    S["keys"].append(k)
                elif typ == 4:
                    if S is not None and S["last"] is None:
                        S["last"] = frame
                    cur.pop(slot, None)
                elif typ == 7:
                    p = end
                    break
    for S in out:
        S["keys"].sort(key=lambda k: k["frame"])
        if S["last"] is None:
            S["last"] = sc["frames"]
    return [S for S in out if S["kind"]]


def active_ranges(frames, gap=9):
    out = []
    for f in sorted(frames):
        if out and f - out[-1][1] <= gap:
            out[-1][1] = f + 2
        else:
            out.append([f, f + 2])
    return out


if __name__ == "__main__":
    sc = parse(sys.argv[1])
    print("frames", sc["frames"], "fps", sc["fps"], "parsed to", hex(sc["end"]), "of", hex(len(sc["data"])))
    for r in sc["records"]:
        n = len(r["chunks"])
        print(f"  track {r['track']:3d} {r['tag']:5s} {r['name']:16s} {r.get('file', ''):14s} f{r['first']}-{r['last']}"
              f" chunks {n}" + (f" joints {len(r['joints'])}" if "joints" in r else ""))
