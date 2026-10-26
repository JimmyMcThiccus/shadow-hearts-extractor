"""Export the cutscene models extracted by lsd_extract.py to export/cutscene/<scene>/<NN>_<name>.glb

Models whose mesh data has other LSD stream chunks interleaved (audio / control data, see FORMATS.md) are
skipped mesh-by-mesh; a model is written when at least one skinned mesh is clean.
usage: python export_cutscene.py [LSD_models dir] [out dir] [scene filter]
"""
import glob
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ssh_gltf import build  # noqa: E402
from ssh_model import parse_mdl  # noqa: E402

root = os.environ.get("SH2_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # SH2_ROOT=disk2 -> disk2/Extracted/LSD, disk2/LSD_models, disk2/export
src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "LSD_models")
dst = sys.argv[2] if len(sys.argv) > 2 else os.path.join(root, "export", "cutscene")
flt = sys.argv[3].lower() if len(sys.argv) > 3 else ""

import json  # noqa: E402
import numpy as np  # noqa: E402
import lsd_anim  # noqa: E402

_cache = {}
_types = {}
_ranges = {}


def anms_joints(scene, model):
    """Joint-name list of the scene's ANMS record for `model` (stream joint ids index this list, not the model's joint order)."""
    import struct
    import re
    b = open(os.path.join(root, "Extracted", "LSD", scene + ".LSD"), "rb").read()
    first = min(i for i in (b.find(m) for m in (b"SSH1.01a", b"SBD 0", b"SQHD")) if i > 0)
    for o in range(0x60, first, 16):
        if b[o:o + 4] == b"ANMS" and b[o + 0x20:o + 0x30].split(b"\0")[0].decode("latin1") == model:
            n = struct.unpack("<H", b[o + 0x10:o + 0x12])[0]
            js = []
            for i in range(min(n, 400)):
                nme, _, rest = b[o + 0x40 + 16 * i:o + 0x50 + 16 * i].partition(b"\0")
                if not nme or any(rest) or not re.fullmatch(rb"[\x21-\x7e]+", nme):
                    break
                js.append(nme.decode("latin1"))
            return js
    return None


def stream_for(scene, model, njoints, jnames=None):
    ov = dict(x.split("=") for x in os.environ.get("SH2_ANIMFROM", "").split(",") if "=" in x)
    if model in ov:                                   # experiment: drive this model with another model's animation slot, joints matched by name
        src_model = ov[model]
        al = anms_joints(scene, src_model)
        tr, sc = _cache.get(scene) or (None, None)
        if tr is None:
            stream_for(scene, src_model, njoints, None)
            tr, sc = _cache[scene]
        inst = next(i for i in sc["instances"] if i["name"] == src_model)
        t = lsd_anim.match_type(tr, inst["first_frame"], inst["last_frame"], len(al))
        T = tr[t]
        mp = {i: jnames.index(n) for i, n in enumerate(al) if n in jnames}
        return {"name": "cutscene", "rot": {mp[i]: v for i, v in T["rot"].items() if i in mp},
                "pos": {mp[i]: v for i, v in T["pos"].items() if i in mp}, "morph": T["morph"], "t0": inst["first_frame"]}
    """Animation (joint keys + face morph weights) of the first scene instance named `model`, or None."""
    if scene not in _cache:
        tr = sc = None
        try:
            tr = lsd_anim.decode(open(os.path.join(root, "Extracted", "LSD", scene + ".LSD"), "rb").read())
            sc = json.load(open(os.path.join(src, scene, "scene.json")))
        except OSError:
            pass
        _cache[scene] = (tr, sc)
    tr, sc = _cache[scene]
    if not tr:
        return None
    cache = _types.setdefault(scene, {})
    insts = list(sc["instances"])
    if not any(i["name"] == model for i in insts) and anms_joints(scene, model):
        insts.append({"name": model, "first_frame": 0, "last_frame": sc["frames"]})      # stage model: animated for the whole scene
    for inst in insts:
        if inst["name"] == model and inst["last_frame"] > inst["first_frame"]:
            key = (model, njoints)
            if key not in cache:
                # a stage model's ANMS set (N125 Gy_07_u: 12 group joints) is larger than the 2-joint model: match on the set's size,
                # otherwise a small camera/event slot with garbage keys wins
                nm_ = len(anms_joints(scene, model) or ()) or njoints
                cache[key] = lsd_anim.match_type(tr, inst["first_frame"], inst["last_frame"], max(nm_, njoints),
                                                 used={v for v in cache.values() if v is not None})
            t = cache[key]
            if os.environ.get('SH2_SWAP'):
                a, b = [int(v) for v in os.environ['SH2_SWAP'].split(',')]
                t = b if t == a else a if t == b else t
            if t is None:
                return None
            T = tr[t]
            _ranges.setdefault(scene, {})[model] = lsd_anim.active_ranges(T)
            rot, pos = T["rot"], T["pos"]
            al = anms_joints(scene, model) if jnames else None
            if al and al != jnames[:len(al)]:
                mp = {}
                for i, nm in enumerate(al):
                    if nm in jnames:
                        mp[i] = jnames.index(nm)
                rot = {mp[i]: v for i, v in rot.items() if i in mp}
                pos = {mp[i]: v for i, v in pos.items() if i in mp}
            return {"name": "cutscene", "rot": rot, "pos": pos, "morph": T["morph"],
                    "t0": inst["first_frame"]}
    return None


_seqs = {}


def seq_stream(scene, jnames, nframes):
    """Looping stage motion from the scene's SQHD sequences (N125 'fidl': lc_GRAND scrolls 1600 units along z in 200 frames and
    repeats, the scenery rushing past the train).  A model gets it when it owns one of the sequence's moving joints.
    Returned positions are pre-multiplied by (1,-1,-1) so that, with the exporter's stream sign (-1,1,-1), a stage joint ends up
    in the stage mapping (-x,-y,z) like its meshes."""
    import lsd_demux
    from ssh_model import parse_sequence
    if scene not in _seqs:
        mm = lsd_demux.demux(open(os.path.join(root, "Extracted", "LSD", scene + ".LSD"), "rb").read())[0]
        out, i = [], mm.find(b"SQHD")
        while i >= 0:
            try:
                out.append(parse_sequence(mm, i))
            except Exception:                      # noqa: BLE001
                pass
            i = mm.find(b"SQHD", i + 1)
        _seqs[scene] = out
    for sq in _seqs[scene]:
        moving = {n: t for n, t in sq.nodes.items() if n in jnames and n != "ROOT" and len(t["pt"]) > 1}
        if not moving or sq.frames < 2:
            continue
        pos = {}
        for n, t in moving.items():
            keys = {}
            for c in range(int(nframes // sq.frames) + 1):
                for tt, v in zip(t["pt"], t["pv"]):
                    f = c * sq.frames + float(tt) - (0.01 if tt >= t["pt"][-1] and tt > 0 else 0.0)    # the last key of a cycle sits just before the wrap
                    keys[f] = v * np.array([1.0, -1.0, -1.0])
            pos[jnames.index(n)] = keys
        return {"name": sq.name, "rot": {}, "pos": pos, "morph": {}, "t0": 0}
    return None


ok = part = none = fail = 0
report = []
for p in sorted(glob.glob(os.path.join(src, "*", "*.mdl"))):
    scene = os.path.basename(os.path.dirname(p))
    if flt and flt not in scene.lower():
        continue
    try:
        b = open(p, "rb").read()
        J, M = parse_mdl(b)
        skinned = [m for m in M if m.rigid_bone is None]
        good = [m for m in skinned if not m.error and m.pos is not None]
        if not good:
            good = [m for m in M if not m.error and m.pos is not None]       # set (stage) model: rigid meshes only
            skinned = M
        if not good:
            none += 1
            continue
        out = os.path.join(dst, scene, os.path.basename(p)[:-4] + ".glb")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        base_name = os.path.basename(p)[3:-4]
        parent = None
        if base_name.endswith("_acc"):
            base_name = base_name[:-4]
            while base_name.endswith("_acc"):
                base_name = base_name[:-4]
            cands = glob.glob(os.path.join(os.path.dirname(p), "*_" + base_name + ".mdl"))
            parent = cands[0] if cands else None
        st = stream_for(scene, base_name, len(J), [j.name for j in J])
        if st is None and len(J) <= 3:
            st = seq_stream(scene, [j.name for j in J], json.load(open(os.path.join(src, scene, "scene.json")))["frames"])
        nm, _ = build(p, [], out, stream=st, tex_from=parent)
        if len(good) == len(skinned):
            ok += 1
        else:
            part += 1
            report.append((scene, os.path.basename(p), len(good), len(skinned)))
    except Exception as e:                       # noqa: BLE001
        fail += 1
        report.append((scene, os.path.basename(p), "ERR", repr(e)[:80]))
for _sc, _r in _ranges.items():
    with open(os.path.join(dst, _sc, "ranges.json"), "w") as _f:
        json.dump(_r, _f)
print("complete models:", ok, " partial (some meshes interleaved, skipped):", part, " no usable skinned mesh:", none, " failed:", fail)
for r in report[:15]:
    print("  ", r)
