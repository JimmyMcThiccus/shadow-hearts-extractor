"""Batch-convert every SH3 FtNW .MDL file (already decompressed by sh3_extract_all.py) to .glb.

usage: python sh3_export_models.py <Extracted dir> <out dir> [path-substring filter]
"""
import glob
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # shared tools/ (ssh_model, ssh_gltf)
from ssh_gltf import build  # noqa: E402
from ssh_model import parse_mdl  # noqa: E402


def main():
    src = sys.argv[1]
    dst = sys.argv[2]
    flt = sys.argv[3].lower() if len(sys.argv) > 3 else ""
    ok = part = none = fail = 0
    report = []
    for p in sorted(glob.glob(os.path.join(src, "**", "*.MDL"), recursive=True) +
                     glob.glob(os.path.join(src, "**", "*.mdl"), recursive=True)):
        rel = os.path.relpath(p, src)
        if flt and flt not in rel.lower():
            continue
        try:
            b = open(p, "rb").read()
            if b[:8] not in (b"SSH1.01a", b"SH3_1.01"):
                continue
            J, M = parse_mdl(b)
            good = [m for m in M if m.rigid_bone is None and not m.error and m.pos is not None]
            if not good:
                good = [m for m in M if not m.error and m.pos is not None]
            if not good:
                none += 1
                report.append((rel, "no usable mesh", len(M)))
                continue
            out = os.path.join(dst, os.path.splitext(rel)[0] + ".glb")
            os.makedirs(os.path.dirname(out), exist_ok=True)
            nmeshes, _ = build(p, [], out, name=os.path.splitext(os.path.basename(p))[0])
            if nmeshes == len(M):
                ok += 1
            else:
                part += 1
                report.append((rel, "partial", nmeshes, len(M)))
        except Exception as e:                       # noqa: BLE001
            fail += 1
            report.append((rel, "ERR", repr(e)[:100]))
    print(f"ok={ok} partial={part} no-mesh={none} failed={fail}")
    for r in report[:60]:
        print(" ", r)


if __name__ == "__main__":
    main()
