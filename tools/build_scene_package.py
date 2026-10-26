"""Write export/scenes/<scene>.json: everything the Blender add-on needs to import one cutscene.

  instances : model GLB (relative path), first/last frame of the instance, name
  camera    : per-frame camera items from lsd_camera (raw)
  audio     : relative path of the scene WAV (written by lsd_audio.py), if any
  fps/frames, fov (from the CAMS directory record)

usage: python build_scene_package.py [scene filter]
"""
import json
import os
import struct
import sys

root = os.environ.get("SH2_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # SH2_ROOT=disk2 for the second disc
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lsd_camera  # noqa: E402


def cams_record(L):
    """(fov, aspect) of the first CAMS directory record."""
    i = L.find(b"CAMS", 0x40, 0x4000)
    if i < 0:
        return 35.0, 0.75
    a, b = struct.unpack("<2f", L[i + 36:i + 44])
    return a, b


def package(scene):
    sc = json.load(open(os.path.join(root, "LSD_models", scene, "scene.json")))
    L = open(os.path.join(root, "Extracted", "LSD", scene + ".LSD"), "rb").read()
    files = {}
    for m in sc["models"]:
        files.setdefault(m["name"], m["file"])        # first (largest) model of a name is the character itself
    seen, inst = set(), []
    for it in sc["instances"]:
        nm = it["name"]
        if nm in seen or nm not in files or it["last_frame"] <= it["first_frame"]:
            continue
        glb = os.path.join("..", "cutscene", scene, files[nm][:-4] + ".glb")
        if not os.path.exists(os.path.join(root, "export", "scenes", glb)):
            continue
        seen.add(nm)
        inst.append({"name": nm, "glb": glb.replace("\\", "/"), "first": it["first_frame"], "last": it["last_frame"]})
        k = 1
        while (nm + "_acc" * k) in files:              # hair / cape pieces ride along with the character
            an = nm + "_acc" * k
            aglb = os.path.join("..", "cutscene", scene, files[an][:-4] + ".glb")
            if os.path.exists(os.path.join(root, "export", "scenes", aglb)):
                inst.append({"name": an, "glb": aglb.replace("\\", "/"), "first": it["first_frame"], "last": it["last_frame"]})
            k += 1
    # set (stage) models: GLBs flagged asset.extras.sh2_set, placed at the origin for the whole scene
    # only the stage models the scene's SBD MODEL section refers to (matched by name stem, e.g. AP_01_U ~ Ap_01_u)
    stems = set()
    o = L.find(b"SBD 0")
    mi = L.find(b"MODEL   ", o) if o >= 0 else -1
    if mi > 0:
        n = struct.unpack("<I", L[mi + 8:mi + 12])[0]
        for k in range(min(n, 32)):
            nm = L[mi + 0x20 + 0x40 * k:mi + 0x20 + 0x40 * k + 12].split(b"\0")[0].decode("latin1").upper()
            if nm:
                stems.add(nm[:5])
    gdir = os.path.join(root, "export", "cutscene", scene)
    for fn in sorted(os.listdir(gdir)) if os.path.isdir(gdir) else []:
        if fn.endswith(".glb"):
            with open(os.path.join(gdir, fn), "rb") as f:
                head = f.read(20)
                js = json.loads(f.read(struct.unpack("<I", head[12:16])[0]))
            ex = js.get("asset", {}).get("extras", {})
            hit = fn[3:-4][:5].upper() in stems         # name stem without the extension (AMI.glb must not read 'AMI.G')
            if ex.get("sh2_set") and ((ex.get("meshes", 0) >= 20 and not stems) or (stems and hit)):
                inst.append({"name": fn[3:-4], "glb": "../cutscene/%s/%s" % (scene, fn), "first": 0, "last": sc["frames"], "set": True})
    # frames in which each character's animation slot is active (the engine hides a model while its slot is silent)
    try:
        rj = json.load(open(os.path.join(root, "export", "cutscene", scene, "ranges.json")))
    except OSError:
        rj = {}
    for i in inst:
        if i.get("set"):
            continue
        base = i["name"].replace("_acc", "")
        rr = [[a, b2] for a, b2 in rj.get(base, [[i["first"], i["last"]]])]      # the slot activity is the truth; the directory frame range is only approximate
        i["ranges"] = rr or [[i["first"], i["last"]]]
    lights = []
    o_ = L.find(b"SBD 0")
    li = L.find(b"LIGHT   ", o_) if o_ >= 0 else -1
    if li > 0:
        nl = struct.unpack("<I", L[li + 8:li + 12])[0]
        for k in range(min(nl, 64)):
            f = struct.unpack("<16f", L[li + 0x20 + 0x40 * k:li + 0x60 + 0x40 * k])
            t = struct.unpack("<I", L[li + 0x20 + 0x40 * k:li + 0x24 + 0x40 * k])[0] & 0xFF
            kind = {0x28: "ambient", 0xA9: "sun"}.get(t, "point")
            lights.append({"kind": kind, "type": t, "k": f[1], "rgb": [f[4], f[5], f[6]], "pos": [f[8], f[9], f[10]],
                           "dir": [f[12], f[13], f[14]]})
    cam = lsd_camera.extract(L)
    fov, aspect = cams_record(L)
    wav = os.path.join("..", "audio", scene + ".wav")
    out = {"scene": scene, "fps": sc["fps"], "frames": sc["frames"], "fov": fov, "aspect": aspect,
           "audio": wav.replace("\\", "/") if os.path.exists(os.path.join(root, "export", "scenes", wav)) else None,
           "instances": inst, "camera": cam, "lights": lights}
    os.makedirs(os.path.join(root, "export", "scenes"), exist_ok=True)
    json.dump(out, open(os.path.join(root, "export", "scenes", scene + ".json"), "w"))
    return len(inst), len(cam)


if __name__ == "__main__":
    flt = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    for d in sorted(os.listdir(os.path.join(root, "LSD_models"))):
        if flt in d.lower():
            try:
                print(d, package(d))
            except Exception as e:      # noqa: BLE001
                print(d, "ERR", repr(e)[:80])




