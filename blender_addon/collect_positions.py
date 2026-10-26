"""blender -b --python collect_positions.py -- <scene.json> <out.json> <step>
Dumps, for sampled frames, the world position of each visible character's pelvis-ish joint (for camera calibration)."""
import json
import os
import sys
import types

import bpy

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("sh2_cutscene_local", os.path.join(here, "sh2_cutscene.py"))   # not an installed copy
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)

path, out, step = sys.argv[sys.argv.index("--") + 1], sys.argv[sys.argv.index("--") + 2], int(sys.argv[sys.argv.index("--") + 3])
o = types.SimpleNamespace(import_camera=False, import_audio=False, hide_outside=True, camera_track="all", fov=0.0,
                          scale=1.0, flip_x=-1.0, flip_y=-1.0, flip_z=-1.0, quat_order="0123", invert_rot=False,
                          fwd_axis="0", fwd_sign=-1.0, up_axis="1", up_sign=1.0)
bpy.ops.wm.read_factory_settings(use_empty=True)
d = A.import_scene(bpy.context, path, o)
sc = bpy.context.scene
arms = [ob for ob in bpy.data.objects if ob.type == "ARMATURE"]
res = []
for f in range(0, d["frames"], step):
    sc.frame_set(f)
    pts = []
    for ob in arms:
        if ob.hide_render:
            continue
        pb = ob.pose.bones
        if len(pb) < 3:
            continue
        # head of the highest-index of the first few bones that is not the root: use bone named like pelvis else index 2
        b = pb[2]
        w = ob.matrix_world @ b.head
        pts.append({"name": ob.name, "p": [w.x, w.y, w.z]})
    res.append({"frame": f, "chars": pts})
json.dump(res, open(out, "w"))
print("saved", len(res))
