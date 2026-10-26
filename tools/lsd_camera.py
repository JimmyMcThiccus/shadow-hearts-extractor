"""Decode the camera track of an LSD cutscene (verified against the engine's camera update code, FUN_003c9fc0).

Camera chunks use kind 0x80 records.  `id` = number of key items in the record, `count` = bit mask of the frames (within
the 3-frame slice) they belong to.  A key item is 24 bytes in the engine:

    s16 x, y, z   (/4096)     quaternion, w = s16 / 32768       (unit quaternion; the engine builds a row-vector matrix
                                                                from it, i.e. the camera's orientation is the INVERSE of
                                                                the usual column-vector rotation)
    f32 x, y, z               camera position (engine world space)
    s16 focal                 focal length in pixels = value / 512 * 682
    s16 flags                 bit 8 (byte +0x17 bit 0) = hard cut (no interpolation towards the next key)

In the stream only the parts that changed are stored: the rotation unit (8 B) is always there, followed optionally by
the position (x,y | z,focal,flags: 16 B).  Missing parts keep their previous value.

usage: python lsd_camera.py <file.LSD> <out.json>
"""
import json
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lsd_demux  # noqa: E402


def _is_rot(u):
    s = struct.unpack("<4h", u)
    q = np.array([s[0] / 4096.0, s[1] / 4096.0, s[2] / 4096.0, s[3] / 32768.0])
    return abs(np.linalg.norm(q) - 1.0) < 0.03


def _is_pos_xy(u):
    a, b = struct.unpack("<2f", u)
    return all(np.isfinite(v) and abs(v) < 5e4 and (abs(v) > 1e-12 or abs(v) < 1e-9) for v in (a, b))   # tiny values are just ~0


def _is_pos_z(u):
    z, = struct.unpack("<f", u[:4])
    foc, fl = struct.unpack("<2h", u[4:8])
    return np.isfinite(z) and abs(z) < 5e4 and 64 <= foc <= 4096 and (fl & 0xFE) == 0 or (np.isfinite(z) and 64 <= foc <= 4096)


def extract(L):
    """-> list of {frame, rot_raw[4], pos[3], focal, cut} (one per keyed frame, all fields filled)."""
    chunks = lsd_demux.find_chunks(L)
    cnt_t = {}
    for c, _e in chunks:
        p = c["start"] + 32
        for _ in range(c["nrec"]):
            if p + 8 > c["end"]:
                break
            i, k, _a, _b, cnt = struct.unpack("<BBBBI", L[p:p + 8])
            if k == 0x80 and 8 <= cnt * 8 and _is_rot(L[p + 8:p + 16]):
                cnt_t[c["type"]] = cnt_t.get(c["type"], 0) + 1
            p += 8 + ((k & 15) * 8 if k != 0x80 else cnt * 8)
    if not cnt_t:
        return []
    cam_type = max(cnt_t, key=cnt_t.get)          # the camera is whichever slot carries the rotation+position records
    keys = {}
    for c, _e in chunks:
        p = c["start"] + 32
        for _ in range(c["nrec"]):
            if p + 8 > c["end"]:
                break
            i, k, _a, _b, cnt = struct.unpack("<BBBBI", L[p:p + 8])
            body = (k & 15) * 8 if k != 0x80 else cnt * 8
            if k == 0x80 and c["type"] == cam_type:
                d = L[p + 8:c["end"]]            # the item count is not stored: a record runs to the end of its chunk
                d = d[:len(d) // 8 * 8]
                units = [d[u:u + 8] for u in range(0, len(d), 8)]
                frames = [f for f in range(3) if cnt >> f & 1]
                u = 0
                for f in frames:
                    if u >= len(units) or not _is_rot(units[u]):
                        break
                    it = {"rot_raw": list(struct.unpack("<4h", units[u]))}
                    u += 1
                    if u + 1 < len(units) and _is_pos_xy(units[u]) and not _is_rot(units[u]) and _is_pos_z(units[u + 1]) and not _is_rot(units[u + 1]):
                        x, y = struct.unpack("<2f", units[u])
                        z, = struct.unpack("<f", units[u + 1][:4])
                        foc, fl = struct.unpack("<2h", units[u + 1][4:8])
                        it.update({"pos": [x, y, z], "focal": foc, "cut": (fl >> 8) & 1})
                        u += 2
                    keys[c["first_frame"] + f] = it
            p += 8 + body
    out, pos, focal = [], [0.0, 0.0, 0.0], 512
    for f in sorted(keys):
        it = keys[f]
        if "pos" in it and not it["cut"]:
            pos, focal = it["pos"], it["focal"]          # the running camera position
        # a key flagged `cut` carries its own position for that key only; later rotation-only keys continue from the
        # running position (checked against the live game: after the cut key the camera returns to it)
        out.append({"frame": f, "rot_raw": it["rot_raw"], "pos": it.get("pos", pos) if it.get("cut") else pos,
                    "focal": it.get("focal", focal) if it.get("cut") else focal, "cut": it.get("cut", 0),
                    "has_pos": "pos" in it})
    first = next((i for i in out if i["has_pos"]), None)
    if first:                      # keys before the first stored position use it
        for i in out:
            if i["has_pos"]:
                break
            i["pos"], i["focal"] = first["pos"], first["focal"]
    return out


if __name__ == "__main__":
    t = extract(open(sys.argv[1], "rb").read())
    json.dump(t, open(sys.argv[2], "w"))
    print(len(t), "camera keys, frames", t[0]["frame"], "..", t[-1]["frame"], "cuts", sum(i["cut"] for i in t))


