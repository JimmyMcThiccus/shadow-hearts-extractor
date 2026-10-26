"""One-shot extractor: game ISO -> files -> every cutscene model, written to a folder next to the ISO.

  1. reads the ISO9660 filesystem directly (no external tools) and dumps every file to <out>/iso/
  2. for every cutscene LSD scene in LSD.PKB (via PKFINFO.TBL): extracts the embedded SSH1.01a models (lsd_extract)
     and converts each one to a rigged, textured glTF with NO animation (skeleton + skin + meshes + face morph targets only)
  3. removes duplicates: the same character shows up in many scenes, so identical models are written once
     -> <out>/cutscene_models/<Name>.glb  and  <out>/cutscene_models/index.json (which scenes use which file)

Intermediate LSD files are processed one scene at a time in <out>/_work and deleted again.

usage: python extract_game.py <game.iso> [--out DIR] [--scene FILTER] [--only-models]
  --out DIR      output folder (default: "<iso name>_extracted" next to the ISO)
  --scene NAME   only process scenes whose name contains NAME (e.g. N001)
  --only-models  dump only PKFINFO.TBL + LSD.PKB from the ISO instead of every file (saves ~1.5 GB)
"""
import argparse
import hashlib
import json
import os
import shutil
import struct
import sys
import time
import traceback
import warnings

warnings.filterwarnings("ignore")
TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)

SECTOR = 2048
CHUNK = 1 << 22


class Iso:
    """Minimal ISO9660 reader. Handles cooked 2048-byte images and raw 2352-byte MODE1/MODE2 form 1 images."""

    def __init__(self, path):
        self.f = open(path, "rb")
        self.stride, self.skip = SECTOR, 0
        for stride, skip in ((2048, 0), (2352, 16), (2352, 24)):
            self.stride, self.skip = stride, skip
            if self.sector(16)[1:6] == b"CD001":
                break
        else:
            raise SystemExit("not an ISO9660 image: " + path)
        pvd = self.sector(16)
        self.root = self._record(pvd[156:190])

    def sector(self, lba):
        self.f.seek(lba * self.stride + self.skip)
        return self.f.read(SECTOR)

    def read_extent(self, lba, size):
        """Yield the bytes of a file extent in chunks."""
        if self.stride == SECTOR:
            self.f.seek(lba * SECTOR)
            while size:
                b = self.f.read(min(size, CHUNK))
                if not b:
                    raise EOFError("ISO truncated")
                size -= len(b)
                yield b
        else:
            while size:
                b = self.sector(lba)[:min(size, SECTOR)]
                lba += 1
                size -= len(b)
                yield b

    @staticmethod
    def _record(r):
        flags = r[25]
        return {"lba": struct.unpack("<I", r[2:6])[0], "size": struct.unpack("<I", r[10:14])[0], "dir": bool(flags & 2),
                "name": r[33:33 + r[32]]}

    def listdir(self, d):
        data = b"".join(self.read_extent(d["lba"], d["size"]))
        out, i = [], 0
        while i < len(data):
            n = data[i]
            if n == 0:                                   # records never straddle a sector: skip the padding
                i = (i // SECTOR + 1) * SECTOR
                continue
            e = self._record(data[i:i + n])
            i += n
            if e["name"] in (b"\x00", b"\x01"):
                continue
            e["name"] = e["name"].decode("latin1").split(";")[0]
            out.append(e)
        return out

    def walk(self, d=None, prefix=""):
        """Yield (relative path, entry) for every file."""
        for e in self.listdir(d or self.root):
            p = prefix + e["name"]
            if e["dir"]:
                yield from self.walk(e, p + "/")
            else:
                yield p, e


def dump_iso(iso, dst, only_models):
    keep = {"PKFINFO.TBL", "LSD.PKB"}
    n = 0
    for p, e in iso.walk():
        if only_models and p.split("/")[-1].upper() not in keep:
            continue
        out = os.path.join(dst, *p.split("/"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if os.path.exists(out) and os.path.getsize(out) == e["size"]:
            print("  have", p)
            continue
        print("  %-30s %8.1f MB" % (p, e["size"] / 1e6))
        with open(out + ".part", "wb") as o:
            for b in iso.read_extent(e["lba"], e["size"]):
                o.write(b)
        os.replace(out + ".part", out)
        n += 1
    return n


def find_file(root, name):
    for dp, _, fs in os.walk(root):
        for f in fs:
            if f.upper() == name:
                return dp
    raise SystemExit("%s not found in the ISO dump (%s)" % (name, root))


class ModelSet:
    """Writes each distinct model once. Identical models (same bytes, or same glTF output) are shared across scenes."""

    def __init__(self, out_dir):
        self.dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.by_key = {}        # (mdl hash, parent mdl hash) -> file name
        self.by_glb = {}        # glb hash -> file name
        self.names = {}         # lower-case file name -> glb hash (Windows file names are case-insensitive)
        self.index = {}         # file name -> {"scenes": [...], "joints": n, "meshes": n}
        self.scenes = {}        # scene -> {model name in scene: file name}

    def _free_name(self, base, h):
        fn, k = base + ".glb", 2
        while fn.lower() in self.names:
            fn, k = "%s_%d.glb" % (base, k), k + 1
        return fn

    def add(self, scene, label, mdl_path, parent_path):
        from ssh_gltf import build
        from ssh_model import parse_mdl
        b = open(mdl_path, "rb").read()
        key = (hashlib.sha1(b).hexdigest(), hashlib.sha1(open(parent_path, "rb").read()).hexdigest() if parent_path else "")
        fn = self.by_key.get(key)
        if fn is None:
            joints, meshes = parse_mdl(b)
            good = [m for m in meshes if m.rigid_bone is None and not m.error and m.pos is not None]
            if not good:                                                 # set (stage) model: rigid meshes only
                good = [m for m in meshes if not m.error and m.pos is not None]
            if not good:
                return None
            tmp = os.path.join(self.dir, "_tmp.glb")
            nmeshes, _ = build(mdl_path, [], tmp, name=label, tex_from=parent_path)       # no anm files, no stream: rig only
            h = hashlib.sha1(open(tmp, "rb").read()).hexdigest()
            fn = self.by_glb.get(h)
            if fn is None:
                fn = self._free_name(label, h)
                os.replace(tmp, os.path.join(self.dir, fn))
                self.by_glb[h], self.names[fn.lower()] = fn, h
                self.index[fn] = {"scenes": [], "joints": len(joints), "meshes": nmeshes}
            else:
                os.remove(tmp)
            self.by_key[key] = fn
        if scene not in self.index[fn]["scenes"]:
            self.index[fn]["scenes"].append(scene)
        self.scenes.setdefault(scene, {})[label] = fn
        return fn

    def save_index(self):
        with open(os.path.join(self.dir, "index.json"), "w") as f:
            json.dump({"models": self.index, "scenes": self.scenes}, f, indent=1, sort_keys=True)


def extract_models(modules_dir, out_root, flt):
    """Cut every cutscene LSD out of LSD.PKB, extract its models and add them to the de-duplicated model set."""
    import lsd_extract
    import pkb_extract
    work = os.path.join(out_root, "_work")
    os.makedirs(work, exist_ok=True)
    ms = ModelSet(os.path.join(out_root, "cutscene_models"))
    leaves = [l for l in pkb_extract.load_leaves(modules_dir) if l[1] == "LSD.PKB" and l[0].upper().endswith(".LSD")]
    scenes = total = 0
    try:
        with open(os.path.join(modules_dir, "LSD.PKB"), "rb") as pkb:
            for path, _, off, size in leaves:
                name = path.split("/")[-1]
                if flt and flt not in name.lower():
                    continue
                pkb.seek(off)
                if b"MOVS" in pkb.read(0x300):               # FMV file: no models
                    continue
                lsd = os.path.join(work, name)
                pkb.seek(off)
                with open(lsd, "wb") as o:
                    left = size
                    while left:
                        b = pkb.read(min(left, CHUNK))
                        o.write(b)
                        left -= len(b)
                try:
                    info = lsd_extract.extract_scene(lsd, os.path.join(work, "mdl"))
                    scene = os.path.splitext(name)[0]
                    sdir = os.path.join(work, "mdl", scene)
                    files = {m["name"]: os.path.join(sdir, m["file"]) for m in reversed(info["models"])}   # first (largest) of a name wins
                    n_all, before = 0, len(ms.index)
                    for m in info["models"]:
                        label = m["name"]
                        if label.startswith("m") and label[1:].isdigit():       # unnamed: only unique within its scene
                            label = "%s_%s" % (scene, label)
                        parent, base = None, m["name"]
                        if base.endswith("_acc"):                    # hair / cape piece: textures come from the character
                            while base.endswith("_acc"):
                                base = base[:-4]
                            parent = files.get(base)
                        try:
                            if ms.add(scene, label, os.path.join(sdir, m["file"]), parent):
                                n_all += 1
                        except Exception as e:                       # noqa: BLE001  (one unreadable model must not lose the scene)
                            print("  %s/%s skipped: %r" % (scene, m["file"], e))
                    scenes += 1
                    total += n_all
                    print("%-10s %3d models, %3d new" % (scene, n_all, len(ms.index) - before))
                except ValueError:                               # no SSH model in this scene
                    pass
                except Exception:                                # noqa: BLE001
                    print("%-10s FAILED" % name)
                    traceback.print_exc()
                finally:
                    os.remove(lsd)
                    shutil.rmtree(os.path.join(work, "mdl"), ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        ms.save_index()
    return scenes, total, ms


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("iso")
    ap.add_argument("--out")
    ap.add_argument("--scene", default="")
    ap.add_argument("--only-models", action="store_true")
    a = ap.parse_args()

    iso_path = os.path.abspath(a.iso)
    out = os.path.abspath(a.out) if a.out else os.path.join(os.path.dirname(iso_path),
                                                            os.path.splitext(os.path.basename(iso_path))[0] + "_extracted")
    t0 = time.time()
    print("output folder:", out)

    print("[1/2] extracting ISO")
    n = dump_iso(Iso(iso_path), os.path.join(out, "iso"), a.only_models)
    print("      %d files written" % n)

    print("[2/2] extracting cutscene models (rig only, duplicates removed)")
    modules = find_file(os.path.join(out, "iso"), "PKFINFO.TBL")
    scenes, total, ms = extract_models(modules, out, a.scene.lower())
    print("      %d scenes, %d model instances -> %d unique files in %s" % (scenes, total, len(ms.index), ms.dir))
    print("done in %.0f s" % (time.time() - t0))


if __name__ == "__main__":
    main()
