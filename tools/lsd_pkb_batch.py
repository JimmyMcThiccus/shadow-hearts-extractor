"""Run the LSD extractors straight out of LSD.PKB, one file at a time (no need for ~3 GB of unpacked LSDs).

For every *.LSD leaf of LSD.PKB (via PKFINFO.TBL):
  directory has a MOVS record  -> lsd_movie.extract   -> <out>/movies/<name>.m2v/.wav/.ac3
  otherwise                    -> lsd_extract.extract_scene -> <out>/LSD_models/<scene>/NN_<name>.mdl + scene.json
usage: python lsd_pkb_batch.py <disc extracted dir with PKFINFO.TBL + LSD.PKB> <out dir> [name filter]
"""
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lsd_extract  # noqa: E402
import lsd_movie  # noqa: E402
import pkb_extract  # noqa: E402

iso, out = sys.argv[1], sys.argv[2]
flt = sys.argv[3].lower() if len(sys.argv) > 3 else ""
tmp = tempfile.mkdtemp(prefix="lsd_")
leaves = [l for l in pkb_extract.load_leaves(iso) if l[1] == "LSD.PKB" and l[0].upper().endswith(".LSD")]
pkb = open(os.path.join(iso, "LSD.PKB"), "rb")
for path, _, off, size in leaves:
    name = path.split("/")[-1]
    if flt and flt not in name.lower():
        continue
    f = os.path.join(tmp, name)
    pkb.seek(off)
    with open(f, "wb") as o:
        left = size
        while left:
            b = pkb.read(min(left, 1 << 22))
            o.write(b)
            left -= len(b)
    try:
        head = open(f, "rb").read(0x300)
        if b"MOVS" in head:
            lsd_movie.extract(f, os.path.join(out, "movies"))
        else:
            info = lsd_extract.extract_scene(f, os.path.join(out, "LSD_models"))
            print("%-12s %5d frames, %d models" % (name, info["frames"], len(info["models"])))
    except ValueError as e:
        print("%-12s skipped: %s" % (name, e))
    except Exception:                       # noqa: BLE001
        print("%-12s FAILED" % name)
        traceback.print_exc()
    finally:
        os.remove(f)
os.rmdir(tmp)
print("done")
