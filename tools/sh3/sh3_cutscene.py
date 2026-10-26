"""Export SH3 FtNW in-engine cutscenes (LSD) to the SH2 Blender add-on's scene package layout:

  <out>/cutscene/<scene>/NN_<name>.glb   characters / weapons (MCES_MDL models + the scene's ANMS track as action 'cutscene')
                         stage_<name>.glb set models out of the stage map (MPS track, cx-compressed SBD)
  <out>/audio/<scene>.wav                voice track (PCMS)
  <out>/scenes/<scene>.json              instances, camera keys, fps/frames -> File > Import > Shadow Hearts 2 Cutscene

Camera (CAMF track, one chunk per shot): see camera_keys (decoded from the engine's camera evaluator).

usage: python sh3_cutscene.py <Extracted dir> <out dir> [scene filter]
"""
import glob
import json
import os
import struct
import sys
import traceback
import warnings

import numpy as np

warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # shared tools/ (ssh_model, ssh_gltf)
import cx_decompress as cx  # noqa: E402
import sh3_lsd  # noqa: E402
from sh3_movies import write_wav  # noqa: E402
from ssh_gltf import build  # noqa: E402
from ssh_model import decode_pos, parse_mdl  # noqa: E402


def model_index(ext):
    idx = {}
    for p in glob.glob(os.path.join(ext, "**", "*.MDL"), recursive=True):
        idx.setdefault(os.path.basename(p).upper(), p)
    return idx


def stream_for(sc, anms, mdl_bytes, t0):
    T = sh3_lsd.decode_anim(sc, anms)
    J, _ = parse_mdl(mdl_bytes)
    names = [j.name.upper() for j in J]
    mp = {i: names.index(n.upper()) for i, n in enumerate(anms["joints"]) if n.upper() in names}
    return {"name": "cutscene", "t0": t0, "morph": T["morph"], "morph_div": 255.0,
            "morph_sets": {"mouth": 0, "eyelid": 1, "eyebrow": 5},
            "rot": {mp[i]: v for i, v in T["rot"].items() if i in mp},
            "pos": {mp[i]: v for i, v in T["pos"].items() if i in mp}}, T


def stage_models(sc, rec):
    raw = sh3_lsd.track_bytes(sc, rec)
    if not raw:
        return []
    m = cx.decompress(raw) if raw[:2] == b"cx" else raw
    mi = m.find(b"MODEL   ")
    if mi < 0:
        return []
    ents = []
    for k in range(min(struct.unpack_from("<I", m, mi + 8)[0], 256)):
        e = mi + 0x20 + 0x40 * k
        nm = m[e:e + 16].split(b"\0")[0].decode("latin1")
        off = struct.unpack_from("<I", m, e + 20)[0]
        if m[e + off:e + off + 8] == b"SH3_1.01":
            ents.append((nm, e + off))
    ents.sort(key=lambda x: x[1])
    return [(nm, m[o:(ents[i + 1][1] if i + 1 < len(ents) else len(m))]) for i, (nm, o) in enumerate(ents)]


POS_SCALE = (0.0001, 0.005, 0.001, 0.005, 0.01, 0.05, 0.1, 1.0)   # SLUS_213.26 0x003bfe60, by header byte 10 & 15


def camera_keys(sc):
    """CAMF shots -> add-on camera keys.  Layout from the engine's evaluator FUN_002e7be0 / key lookup FUN_002e8590:
    shot header: u32 first frame, u16 frames, u16 segments, u16 keys, u8 scale index (& 15), u8, u16 header rows;
    +16 f32 base position; +32 segments of 8 bytes: u16 start frame, u16 first key, u32 frame mask (a key on every set bit);
    key (32 B at header rows * 16): s16 quaternion x,y,z,w / 4096, 8 B packed position offset (decode_pos, times
    POS_SCALE[index], added to the base), s16 focal (pixels = v / 1024 * 665.7), u8, u8 flags (bit 0: hold, no
    interpolation to the next key), ...  The engine lerps position/focal and slerps rotation between keys."""
    d = sc["data"]
    cams = [r for r in sc["records"] if r["tag"] == "CAMF"]
    keys = []
    for rec in cams:
        for o, n, _seg in rec["chunks"]:
            f0, nf, nseg, nk, sidx, _b, nh = struct.unpack_from("<IHHHBBH", d, o)
            if nk == 0 or 16 * nh + 32 * nk > n or 32 + 8 * nseg > 16 * nh:
                continue
            base = np.array(struct.unpack_from("<3f", d, o + 16))
            scale = POS_SCALE[sidx & 15] if (sidx & 15) < len(POS_SCALE) else 1.0
            frames = {}
            for s in range(nseg):
                start, k0, mask = struct.unpack_from("<HHI", d, o + 32 + 8 * s)
                k = k0
                for bit in range(32):
                    if mask >> bit & 1:
                        frames[k] = start + bit
                        k += 1
            for k in range(nk):
                if k not in frames:
                    continue
                p = o + 16 * nh + 32 * k
                q = struct.unpack_from("<4h", d, p)
                off = decode_pos(np.array(struct.unpack_from("<4h", d, p + 8), np.int64)) * scale
                focal, = struct.unpack_from("<h", d, p + 16)
                hold = d[p + 0x13] & 1
                keys.append({"frame": frames[k], "rot_raw": [q[0], q[1], q[2], q[3] * 8],
                             "pos": [float(x) for x in base + off],
                             "focal": focal * 665.7 / 1024.0 * 512.0 / 682.0,   # add-on: px = focal / 512 * 682
                             "cut": int(hold), "has_pos": True})
            if keys:
                keys[-1]["cut"] = 1          # last key of a shot holds until the next shot (hard cut)
    keys.sort(key=lambda k: k["frame"])
    return keys


def apply_visible(rng, events):
    """Cut the shown ranges by the 'Visible' events (id 8, value u16 1); a model starts visible."""
    vis = sorted((f, a1) for f, i, _a0, a1 in events if i == 8)
    if not vis:
        return rng
    hidden, start = [], None
    for f, v in vis:
        if not v and start is None:
            start = f
        elif v and start is not None:
            hidden.append((start, f - 1))
            start = None
    if start is not None:
        hidden.append((start, 1 << 30))
    out = []
    for a, b in rng:
        segs = [[a, b]]
        for h0, h1 in hidden:
            nxt = []
            for s0, s1 in segs:
                if h1 < s0 or h0 > s1:
                    nxt.append([s0, s1])
                    continue
                if s0 < h0:
                    nxt.append([s0, h0 - 1])
                if h1 < s1:
                    nxt.append([h1 + 1, s1])
            segs = nxt
        out += segs
    return out


def eye_frames(mdl_bytes, anms, events, gdir, stem):
    """TXA texture animations (eye blinks) of a model + their keys from the scene's property events (TXA<name> = frame)."""
    from ssh_gltf import png_rgba, txa_frames
    if not anms:
        return []
    out = []
    for name, ti, frames in txa_frames(mdl_bytes):
        prop = "TXA" + name
        if prop not in anms.get("props", []):
            continue
        pi = anms["props"].index(prop)
        keys = sorted({f: v for f, i, a0, v in events if i == 0 and a0 == pi}.items())
        if not keys:
            continue
        files = []
        for k, rgba in enumerate(frames, start=1):
            fn = "%s_txa_%s_%d.png" % (stem, name, k)
            open(os.path.join(gdir, fn), "wb").write(png_rgba(rgba.shape[1], rgba.shape[0], np.ascontiguousarray(rgba)))
            files.append(fn)
        out.append({"material": "tex%d" % ti, "name": name, "frames": files, "keys": [[int(f), int(v)] for f, v in keys]})
    return out


def export_scene(lsd, out, idx):
    scene = os.path.splitext(os.path.basename(lsd))[0]
    sc = sh3_lsd.parse(lsd)
    gdir = os.path.join(out, "cutscene", scene)
    os.makedirs(gdir, exist_ok=True)
    recs = sc["records"]
    anms = {}
    for r in recs:
        if r["tag"] == "ANMS":
            anms.setdefault(r["name"], []).append(r)
    inst = []
    seen = set()
    n = 0
    ranges = {}
    for r in recs:
        if r["tag"] != "MDLS" or not r.get("file"):
            continue
        key = (r["name"], r["first"])
        if key in seen:                   # the second MDLS of a name is the shadow model (*_MS.MDL)
            continue
        seen.add(key)
        path = idx.get(r["file"].upper())
        if not path:
            print("   missing model", r["file"])
            continue
        b = open(path, "rb").read()
        a = next((x for x in anms.get(r["name"], ()) if x["first"] <= r["first"] <= x["last"] or x["first"] == r["first"]), None)
        st = None
        T = {"events": []}
        rng = [[r["first"], r["last"]]]
        if a:
            st, T = stream_for(sc, a, b, r["first"])
            fr = [f for f in T["frames"] if r["first"] <= f <= r["last"]]
            if fr:
                rng = sh3_lsd.active_ranges(fr)
        rng = apply_visible(rng, T["events"])
        fn = "%02d_%s.glb" % (n, r["name"])
        n += 1
        try:
            build(path, [], os.path.join(gdir, fn), name=r["name"], stream=st)
        except Exception as e:            # noqa: BLE001
            print("   %s failed: %r" % (fn, e))
            continue
        ranges[r["name"]] = rng
        item = {"name": r["name"], "glb": "../cutscene/%s/%s" % (scene, fn), "first": r["first"], "last": r["last"],
                "ranges": rng}
        txa = eye_frames(b, a, T["events"], gdir, fn[:-4])
        if txa:
            item["txa"] = [dict(t, frames=["../cutscene/%s/%s" % (scene, p) for p in t["frames"]]) for t in txa]
        inst.append(item)
    for si, r in enumerate(x for x in recs if x["tag"] == "MPS"):
        # a scene can change stage (N001: street until 299, office from 300); each map is shown in its own window
        a = next((x for x in anms.get(r["name"], ()) if x["first"] <= r["first"] <= x["last"]), None)
        for nm, blob in stage_models(sc, r):
            mp = os.path.join(gdir, "stage%d_%s.mdl" % (si, nm))
            open(mp, "wb").write(blob)
            st = None
            if a:
                try:
                    st, _T = stream_for(sc, a, blob, r["first"])
                    if not st["rot"] and not st["pos"]:
                        st = None
                except Exception:          # noqa: BLE001
                    st = None
            fn = "stage%d_%s.glb" % (si, nm)
            try:
                build(mp, [], os.path.join(gdir, fn), name=nm, stream=st)
            except Exception as e:        # noqa: BLE001
                print("   %s failed: %r" % (fn, e))
                continue
            finally:
                os.remove(mp)
            inst.append({"name": nm, "glb": "../cutscene/%s/%s" % (scene, fn), "first": r["first"], "last": r["last"],
                         "ranges": [[r["first"], r["last"]]], "set": True})
    audio = None
    pcm = [r for r in recs if r["tag"] == "PCMS"]
    if pcm:
        d = sc["data"]
        raw = b"".join(d[o:o + n_] for o, n_, _s in sorted(c for r in pcm for c in r["chunks"]))
        if raw:
            adir = os.path.join(out, "audio")
            os.makedirs(adir, exist_ok=True)
            pr = d[pcm[0]["off"]:pcm[0]["off"] + 0x30]
            il = struct.unpack_from("<I", pr, 0x10)[0] or 0x1000          # PCMS interleave (0x1000)
            ch, rate = struct.unpack_from("<HH", pr, 0x2c)
            write_wav(os.path.join(adir, scene + ".wav"), raw, rate or 48000, ch or 2, il)
            audio = "../audio/%s.wav" % scene
    cam = camera_keys(sc)
    json.dump(ranges, open(os.path.join(gdir, "ranges.json"), "w"))
    os.makedirs(os.path.join(out, "scenes"), exist_ok=True)
    lights = []
    for r in recs:
        if r["tag"] == "LGTF":
            lights += sh3_lsd.decode_lights(sc, r)
    pkg = {"scene": scene, "sh3": True, "fps": sc["fps"], "frames": sc["frames"], "fov": 35.0, "aspect": 0.75, "audio": audio,
           "instances": inst, "camera": cam, "lights": lights}
    json.dump(pkg, open(os.path.join(out, "scenes", scene + ".json"), "w"))
    return len(inst), len(cam)


def main():
    ext, out = sys.argv[1], sys.argv[2]
    flt = sys.argv[3].upper() if len(sys.argv) > 3 else ""
    idx = model_index(ext)
    for lsd in sorted(glob.glob(os.path.join(ext, "LSD", "*.LSD"))):
        name = os.path.basename(lsd).upper()
        if flt and flt not in name:
            continue
        with open(lsd, "rb") as f:
            if b"MOVS" in f.read(0x600):
                continue                  # pre-rendered movie: sh3_movies.py
        try:
            ni, nc = export_scene(lsd, out, idx)
            print("%-10s %3d instances  %4d camera keys" % (name[:-4], ni, nc))
        except Exception:                 # noqa: BLE001
            print("%-10s FAILED" % name[:-4])
            traceback.print_exc()


if __name__ == "__main__":
    main()
