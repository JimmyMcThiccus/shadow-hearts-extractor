"""Extract every file out of SH3 FtNW's PKFINFO.TBL / *.PKB archives, decompressing the 'cx'-compressed
ones (cx_decompress.py) on the way, into a plain directory tree mirroring the SH2 decomp's layout
(<out>/<ARCHIVE>/<path...>), so the existing SH2 tools (same MWo3/SBD/EFMG/SSH-model formats) can run
against it unmodified.

usage: python sh3_extract_all.py <SH3 'ISO Extracted' dir> <out dir> [path-substring filter]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sh3_pkb as pk
import cx_decompress as cx


def main():
    root = sys.argv[1]
    out = sys.argv[2]
    flt = sys.argv[3].upper() if len(sys.argv) > 3 else ""
    data = open(os.path.join(root, "PKFINFO.TBL"), "rb").read()
    info = pk.PkfInfo(data)
    results = []
    info.walk(lambda path, c, o, s: results.append((path, c, o, s)))
    print(f"{len(results)} files in table")

    by_container = {}
    for path, c, o, s in results:
        by_container.setdefault(c, []).append((path, o, s))

    n_ok = n_decomp = n_raw = n_fail = 0
    fails = []
    skip = {"LSD"}  # huge (3.2 GB) and not cx-compressed; handled separately by lsd tooling directly against LSD.PKB
    for container, files in by_container.items():
        if container in skip:
            continue
        plain_names = {"FIELD.CPX", "BATTLE.CPX", "OPENING.CPX"}
        src_path = os.path.join(root, container) if container in plain_names else os.path.join(root, container + ".PKB")
        if not os.path.exists(src_path):
            print("missing archive:", src_path)
            continue
        with open(src_path, "rb") as f:
            for path, off, size in files:
                if flt and flt not in path.upper():
                    continue
                f.seek(off)
                raw = f.read(size)
                dst = os.path.join(out, path.replace("/", os.sep))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    hdr = cx.header_info(raw)
                    if hdr is not None:
                        body = cx.decompress(raw)
                        n_decomp += 1
                    else:
                        body = raw
                        n_raw += 1
                    with open(dst, "wb") as g:
                        g.write(body)
                    n_ok += 1
                except Exception as e:                      # noqa: BLE001
                    n_fail += 1
                    fails.append((path, repr(e)[:120]))
                    with open(dst, "wb") as g:
                        g.write(raw)       # fall back to raw bytes so the file still exists
    print(f"ok={n_ok} (decompressed={n_decomp} raw={n_raw}) failed={n_fail}")
    for p, e in fails[:30]:
        print("  FAIL", p, e)


if __name__ == "__main__":
    main()
