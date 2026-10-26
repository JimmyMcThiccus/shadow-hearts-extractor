"""Batch-export every model in Unpacked/ to export/models/<area>/<name>.glb

usage: python export_all.py [Unpacked dir] [out dir] [name filter substring]
Groups: every '<stem>.NN.mdl' inside one package shares the animations '<stem>.NN.anm' of that package;
battle characters (BTLDATA/CHR/NNN.bcm) additionally get BTLDATA/CHRANM/NNN.anm.* .
"""
import glob
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ssh_gltf import build  # noqa: E402

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "Unpacked")
dst = sys.argv[2] if len(sys.argv) > 2 else os.path.join(root, "export", "models")
flt = sys.argv[3].lower() if len(sys.argv) > 3 else ""

ok = fail = skipped = 0
log = []
for mdl in sorted(glob.glob(os.path.join(src, "**", "*.mdl"), recursive=True)):
    rel = os.path.relpath(mdl, src)
    if flt and flt not in rel.lower():
        continue
    m = re.match(r"(.*)\.(\d\d)\.mdl$", mdl)
    if not m:
        continue
    stem, idx = m.group(1), m.group(2)
    anms = sorted(glob.glob(stem + ".*.anm"))
    r = os.path.relpath(stem, src).replace("\\", "/")
    if r.startswith("BTLDATA/CHR/"):
        n = os.path.basename(stem).split(".")[0]
        anms += sorted(glob.glob(os.path.join(src, "BTLDATA", "CHRANM", n + ".anm.*.anm")))
    out = os.path.join(dst, os.path.dirname(r), os.path.basename(r).replace(".", "_") + "_%s.glb" % idx)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        nm, na = build(mdl, anms, out)
        if nm == 0:
            os.remove(out)
            skipped += 1
            log.append((rel, "no exportable meshes"))
        else:
            ok += 1
    except Exception as e:                        # noqa: BLE001
        fail += 1
        log.append((rel, repr(e)[:100]))
        if os.path.exists(out):
            os.remove(out)
print("exported", ok, "skipped (no skinned meshes)", skipped, "failed", fail)
for r, e in log[:40]:
    print("  ", r, "-", e)
