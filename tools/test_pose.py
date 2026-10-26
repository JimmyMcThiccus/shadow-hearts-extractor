"""Render a model posed by an animation: python test_pose.py <set prefix e.g. FIELD\\MODEL\\000.set> <anm index> [frames...]"""
import sys

import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from ssh_model import *

U = os.path.join(ROOT, "Unpacked") + os.sep
prefix, anm = sys.argv[1], int(sys.argv[2])
frames = [float(x) for x in sys.argv[3:]] or [0, 7, 14, 21]
b = open(U + prefix + ".00.mdl", "rb").read()
J, M = parse_mdl(b)
virt = parse_wfrh(b, len(J))
seq = parse_sequence(open(U + prefix + ".%02d.anm" % anm, "rb").read())
print(seq.name, seq.frames, "frames", seq.fps, "fps", len(seq.nodes), "nodes; virtual bones", len(virt))
miss = [j.name for j in J if j.name not in seq.nodes]
print("joints without a track:", miss)
bounds = None
for k, f in enumerate(frames):
    wp, wq = pose(J, seq, f, qs=eval(__import__("os").environ.get("QS","(1,-1,-1)")))
    pos = [skin_mesh(m, J, virt, wp, wq) for m in M]
    allp = np.concatenate(pos)
    if bounds is None:
        r = (allp.max(0) - allp.min(0)).max() * 1.0
    c = (allp.max(0) + allp.min(0)) / 2
    bounds = [c - r / 2, c + r / 2]
    render(M, os.path.join(ROOT, "preview", "pose_%d.png" % k), "side", 384, pos, bounds)
    print("frame", f, "done")


