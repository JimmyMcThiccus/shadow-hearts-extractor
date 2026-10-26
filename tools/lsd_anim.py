"""Decode the control-chunk stream of an LSD cutscene into per-model animation tracks.

Each control chunk covers 3 frames of one model slot ('type').  Records (see lsd_demux.py / FORMATS.md):
  kind 1..3        rotation keys of joint `id`   s16 x,y,z,w / 4096   (keys sit on the set bits of `count`)
  kind 0x21..0x23  translation keys of joint `id` (same encoding as SQHD position keys)
  kind 0x41..0x43 / 0xc1..0xc3   face morph units, 8 bytes each: [set][n targets][6 weights 0..127]
                   set 0 / n 7 = mouth (bytes 2..6 = A I U E O), set 1 / n 4 = eyelid (bytes 2..5 = O M C L)
"""
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lsd_demux  # noqa: E402
from ssh_model import decode_pos  # noqa: E402


def decode(L):
    """-> {type: {"rot": {id: {frame: q}}, "pos": {id: {frame: xyz}}, "morph": {set: {frame: weights}}, "range": (f0, f1)}}"""
    chunks = lsd_demux.find_chunks(L)
    out = {}
    for c, _e in chunks:
        T = out.setdefault(c["type"], {"rot": {}, "pos": {}, "morph": {}, "range": [c["first_frame"], c["first_frame"]]})
        T.setdefault("frames", set()).add(c["first_frame"])
        ff = c["first_frame"]
        T["range"][1] = max(T["range"][1], ff + 2)
        p = c["start"] + 32
        for _ in range(c["nrec"]):
            if p + 8 > c["end"]:
                break
            i, k, _a, _b, cnt = struct.unpack("<BBBBI", L[p:p + 8])
            if k in (0x41, 0x42, 0x43, 0xc1, 0xc2, 0xc3):
                # the face-morph tail: all joint records (sorted by id) are behind us.  Its headers are followed by a variable
                # number of 8-byte units (set 0/1/2), so walking it as joint records yields garbage rotation keys (e.g. N125 joint 2
                # at frames 573/603/618/1038).  Scan it on the 8-byte grid for morph headers only.
                while p + 16 <= c["end"]:
                    i, k, _a, _b, cnt = struct.unpack("<BBBBI", L[p:p + 8])
                    if i == 1 and k in (0x41, 0x42, 0x43, 0xc1, 0xc2, 0xc3) and _b == 0 and cnt == 0:
                        t = ff + (1 if k & 0x80 else 0)
                        for u in range(k & 15):
                            d = L[p + 8 + 8 * u:p + 16 + 8 * u]
                            if len(d) == 8 and (d[0], d[1]) in ((0, 7), (1, 4)):
                                T["morph"].setdefault(d[0], {}).setdefault(t, np.array(list(d[2:2 + d[1]]), np.float64))
                                break
                    p += 8
                break
            nk = k & 15
            body = nk * 8 if k != 0x80 else cnt * 8
            data = L[p + 8:p + 8 + body]
            if k in (1, 2, 3) or k in (0x21, 0x22, 0x23):
                frames = [f for f in range(3) if cnt >> f & 1][:nk]
                if len(frames) < nk:
                    frames = list(range(nk))
                keys = np.frombuffer(data, "<i2").reshape(-1, 4)
                if k < 0x20:
                    for f, q in zip(frames, keys):
                        T["rot"].setdefault(i, {})[ff + f] = q.astype(np.float64) / 4096.0
                else:
                    for f, h in zip(frames, keys):
                        T["pos"].setdefault(i, {})[ff + f] = decode_pos(h)
            elif i == 1 and k in (0x41, 0x42, 0x43, 0xc1, 0xc2, 0xc3):
                # first unit of the record = one weight sample (c-kinds: the later sample of the slice)
                t = ff + (1 if k & 0x80 else 0)
                for u in range(nk):
                    d = data[8 * u:8 * u + 8]
                    if (d[0], d[1]) in ((0, 7), (1, 4)):
                        T["morph"].setdefault(d[0], {}).setdefault(t, np.array(list(d[2:2 + d[1]]), np.float64))
                        break
            p += 8 + body
    return out


def active_ranges(T, gap=9):
    """Frame ranges in which the slot receives chunks (the engine only updates, and shows, a model while it does)."""
    fr = sorted(T.get("frames", ()))
    out = []
    for f in fr:
        if out and f - out[-1][1] <= gap:
            out[-1][1] = f + 2
        else:
            out.append([f, f + 2])
    return out


def match_type(tracks, first, last, njoints, used=()):
    """Pick the control-chunk type that animates a model instance.  The skeleton size is the strongest cue (a type that
    animates joint ids beyond the model's joint count belongs to a bigger skeleton), then the frame range; slots already
    given to another instance are skipped."""
    best, bs = None, 1e18
    for t, T in tracks.items():
        if t in used:
            continue
        ids = set(T["rot"]) | set(T["pos"])
        if ids and max(ids) >= 100:        # camera / event slot (ids are not joints)
            continue
        if len(ids) < 2 or (T["range"][1] - T["range"][0]) < 6:
            continue
        top = max(ids) + 1
        s = 500 * max(top - njoints, 0) + 5 * max(njoints - top, 0)
        s += abs(T["range"][0] - first) * 4 + abs(T["range"][1] - last)
        if s < bs:
            best, bs = t, s
    return best
