"""Extract the in-engine cutscene models (SSH1.01a) embedded in LSD scene files.

LSD layout (partly decoded):
  0x00 'LSDH' u32 0, u16 version(4), u16 entry_count, u16 0, u16 fps(30), u32 frame_count, ...
  0x40 'LSDC' + directory of records (tags seen: MDLS ANMS CAMS LGTS LCDF LIPS PCMS LSCR EVNT BLINK ...).
        'MDLS' = model instance: +0x08 first frame, +0x0c last frame, +0x18 size in 16-byte units, name at +0x20
        'ANMS' = animation set: +0x10 u16 joint count, name at +0x20, joint names (16 bytes each) from +0x40
  Data blobs follow the directory: SSH1.01a models (each with its own textures), SBD maps, SQHD sequences.
Models are named by matching their joint names against the ANMS joint lists.

usage: python lsd_extract.py <LSD dir> <out dir> [scene filter]
Writes <out>/<scene>/<NN>_<name>.mdl and <out>/<scene>/scene.json
"""
import json
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def model_joints(b):
    """Joint names of an SSH model blob (cheap header parse)."""
    off = struct.unpack("<I", b[0x10:0x14])[0]
    if b[off:off + 4] != b"FRHD":
        return []
    n = struct.unpack("<H", b[off + 4:off + 6])[0]
    return [bytes(b[off + 0x10 + 0x40 * i:off + 0x10 + 0x40 * i + 12]).split(b"\0")[0].decode("latin1") for i in range(n)]


def scan_scene(mm):
    first = min(i for i in (mm.find(m) for m in (b"SSH1.01a", b"SBD 0", b"SQHD")) if i > 0)
    hdr = struct.unpack("<8I", mm[:32])
    info = {"entries": hdr[2] & 0xFFFF, "fps": hdr[3] >> 16, "frames": hdr[4], "first_blob": first}
    mdls, anms = [], []
    for o in range(0x60, first, 16):
        t = bytes(mm[o:o + 4])
        if t == b"MDLS":
            u = struct.unpack("<8I", mm[o + 4:o + 36])
            mdls.append({"name": bytes(mm[o + 0x20:o + 0x40]).split(b"\0")[0].decode("latin1"),
                         "first_frame": u[1], "last_frame": u[2], "size_units": u[5]})
        elif t == b"ANMS":
            n = struct.unpack("<H", mm[o + 0x10:o + 0x12])[0]
            nm = bytes(mm[o + 0x20:o + 0x30]).split(b"\0")[0].decode("latin1")
            js = []
            for i in range(min(n, 400)):    # joint rows end at the first row with data after the name (next record)
                row = bytes(mm[o + 0x40 + 16 * i:o + 0x50 + 16 * i])
                nme, _, rest = row.partition(b"\0")
                if not nme or any(rest) or not re.fullmatch(rb"[\x21-\x7e]+", nme):
                    break
                js.append(nme.decode("latin1"))
            if js:
                anms.append({"name": nm, "joints": js})
    blobs = []
    for m in (b"SSH1.01a", b"SQHD", b"SBD 0"):
        i = mm.find(m, 0x40)
        while i >= 0:
            blobs.append((i, m.decode().strip()))
            i = mm.find(m, i + 1)
    blobs.sort()
    return info, mdls, anms, blobs


def extract_scene(path, out_dir):
    import lsd_demux
    raw = open(path, "rb").read()
    if raw.find(b"SSH1.01a") < 0:
        raise ValueError("no models")
    # models stream in while the cutscene plays: strip the interleaved control/animation chunks and voice audio first
    mm, chunks, audio, spans = lsd_demux.demux(raw)
    info, mdls, anms, blobs = scan_scene(mm)
    models = [(o, blobs[i + 1][0] if i + 1 < len(blobs) else len(mm)) for i, (o, t) in enumerate(blobs)
              if t == "SSH1.01a"]
    scene = os.path.splitext(os.path.basename(path))[0]
    os.makedirs(os.path.join(out_dir, scene), exist_ok=True)
    names, used = {}, set()
    joint_sets = [model_joints(mm[o:min(e, o + 0x4000)]) for o, e in models]
    # The MDLS directory records give each model's exact size (16-byte units): several models share one skeleton, so the
    # size (not the joint names) is what ties a name to its blob.
    taken = set()
    for md in sorted(mdls, key=lambda d: -d["size_units"]):
        need = md["size_units"] * 16
        if need < 0x400 or not md["name"] or not re.fullmatch(r"[\x21-\x7e]+", md["name"]):
            continue
        cand = [(e - o - need, i) for i, (o, e) in enumerate(models) if i not in used and e - o >= need]
        if cand:
            ex, i = min(cand)
            if ex <= 0x1000 and i not in names:
                nm = md["name"]
                while nm in taken:                 # hair / cape / hand pieces (dynamic parts) of the same character
                    nm += "_acc"
                taken.add(nm)
                names[i] = nm
                used.add(i)
    for a in anms:                       # name models by their skeleton
        best, bs = None, 0.0
        for i, js in enumerate(joint_sets):
            if i in used or not js:
                continue
            inter = len(set(js) & set(a["joints"]))
            sc = inter / max(len(set(js) | set(a["joints"])), 1)
            if sc > bs:
                best, bs = i, sc
        if best is not None and bs >= 0.85:
            names[best] = a["name"]
            used.add(best)
    # set (stage) models carry no MDLS record: they are stored in the order of the SBD MODEL section (N125: GY_07_FAR_U, GRAND,
    # GY_07_U = the train car, SASH, SHAN, AMI, SEAT, RAY; 'GRAND_move' is a path, not a model).  Name them from that list,
    # preferring the spelling of an ANMS set of the same name (Gy_07_u) so the stage animation finds them.
    o_sbd = mm.find(b"SBD 0")
    mi = mm.find(b"MODEL   ", o_sbd) if o_sbd >= 0 else -1
    if mi > 0:
        sbd = []
        for k in range(min(struct.unpack("<I", mm[mi + 8:mi + 12])[0], 64)):
            nm = bytes(mm[mi + 0x20 + 0x40 * k:mi + 0x2c + 0x40 * k]).split(b"\0")[0].decode("latin1")
            if nm and not nm.lower().endswith("_move"):
                sbd.append(nm)
        free = [i for i in range(len(models)) if i not in names]
        # only when every unnamed model is a set model (rigid, <= 3 joints) and the counts agree
        if sbd and len(free) == len(sbd) and all(len(joint_sets[i]) <= 3 for i in free) and free == sorted(free):
            spell = {a["name"].lower(): a["name"] for a in anms}
            for i, nm in zip(free, sbd):
                names[i] = spell.get(nm.lower(), nm.capitalize())
    out = []
    for i, (o, e) in enumerate(models):
        nm = names.get(i, "m%02d" % i)
        fn = os.path.join(out_dir, scene, "%02d_%s.mdl" % (i, nm))
        with open(fn, "wb") as g:
            g.write(mm[o:e])
        out.append({"index": i, "name": nm, "file": os.path.basename(fn), "offset": o, "size": e - o,
                    "joints": len(joint_sets[i])})
    info.update({"models": out, "instances": mdls, "animation_sets": [a["name"] for a in anms],
                 "stream": {"control_chunks": len(chunks), "audio_chunks": len(audio),
                            "removed_bytes": len(raw) - len(mm)}})
    json.dump(info, open(os.path.join(out_dir, scene, "scene.json"), "w"), indent=1)
    return info


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    flt = sys.argv[3].lower() if len(sys.argv) > 3 else ""
    n = named = 0
    for f in sorted(os.listdir(src)):
        if not f.upper().endswith(".LSD") or (flt and flt not in f.lower()):
            continue
        try:
            info = extract_scene(os.path.join(src, f), dst)
        except ValueError:                   # file without any model blob (movie / stream files)
            continue
        ms = info["models"]
        n += len(ms)
        named += sum(1 for m in ms if not m["name"].startswith("m"))
        print(f"{f:12s} {info['frames']:6d} frames  models {len(ms):3d} (named {sum(1 for m in ms if not m['name'].startswith('m')):3d})")
    print("models written:", n, "named:", named)
