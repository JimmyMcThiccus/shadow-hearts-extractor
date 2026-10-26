"""blender -b --python debug_bbox.py -- <scene.json> <frame>  : print world bbox centre / camera-space position of visible meshes."""
import os
import sys
import types

import bpy
from mathutils import Vector

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("sh2_cutscene_local", os.path.join(here, "sh2_cutscene.py"))   # not an installed copy
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)

path, frame = sys.argv[sys.argv.index("--") + 1], int(sys.argv[sys.argv.index("--") + 2])
o = types.SimpleNamespace(import_camera=True, import_audio=False, hide_outside=True, scale=1.0)
bpy.ops.wm.read_factory_settings(use_empty=True)
A.import_scene(bpy.context, path, o)
sc = bpy.context.scene
sc.frame_set(frame)
dg = bpy.context.evaluated_depsgraph_get()
cam = sc.camera
inv = cam.matrix_world.inverted()
for ob in bpy.data.objects:
    if ob.type != "MESH" or ob.hide_render or "mesh" not in ob.name.lower():
        continue
    e = ob.evaluated_get(dg)
    me = e.to_mesh()
    if not len(me.vertices):
        continue
    pts = [e.matrix_world @ v.co for v in me.vertices]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    c = (lo + hi) / 2
    cc = inv @ c
    print("BBOX %-28s centre %s size %s  cam-space %s" % (ob.name, tuple(round(v, 1) for v in c), tuple(round(v, 1) for v in (hi - lo)), tuple(round(v, 1) for v in cc)))
print("CAM", tuple(round(v, 1) for v in cam.matrix_world.translation))
from mathutils import Vector as V
m = cam.matrix_world.to_3x3()
print("CAMFWD", tuple(round(x, 3) for x in (m @ V((0, 0, -1)))), "UP", tuple(round(x, 3) for x in (m @ V((0, 1, 0)))), "RIGHT", tuple(round(x, 3) for x in (m @ V((1, 0, 0)))))

for ob in bpy.data.objects:
    if ob.type == "ARMATURE" and not ob.hide_render and ob.name.startswith("Kalen_a_m"):
        for bn in ("WAIST0", "BBODY0"):
            if bn in ob.pose.bones:
                w = ob.matrix_world @ ob.pose.bones[bn].head
                print("WAISTPOS", ob.name, bn, tuple(round(v, 1) for v in w))
for ob in bpy.data.objects:
    if ob.type == "MESH" and ob.name.startswith("Rb_00_u") and ob.data:
        pass

for ob in bpy.data.objects:
    if ob.type == "ARMATURE" and not ob.hide_render and ob.name.startswith(("Nicol_h", "Kalen_a_h")):
        for bn in ("ROOT1", "WAIST0", "BBODY0", "UBODY0"):
            if bn in ob.pose.bones:
                w = ob.matrix_world @ ob.pose.bones[bn].head
                print("BONEPOS", ob.name, bn, tuple(round(v, 1) for v in w))

for ob in bpy.data.objects:
    if ob.type == "ARMATURE" and not ob.hide_render and ob.name.startswith("Nicol_h"):
        print("POSEDBG obj", ob.name, tuple(round(v, 2) for v in ob.matrix_world.translation), tuple(round(v, 3) for v in ob.matrix_world.to_quaternion()))
        for bn in ("ROOT", "ROOT1", "WAIST0"):
            pb = ob.pose.bones[bn]
            print("POSEDBG", bn, "loc", tuple(round(v, 2) for v in pb.location), "rotq", tuple(round(v, 3) for v in pb.rotation_quaternion), "head", tuple(round(v, 2) for v in pb.head), "rest head", tuple(round(v, 2) for v in ob.data.bones[bn].head_local), "rest roll/orient", tuple(round(v, 2) for v in ob.data.bones[bn].matrix_local.to_quaternion()))
        ad = ob.animation_data
        print("POSEDBG nla", [(t.name, [(s.name, s.frame_start, s.frame_end, s.scale) for s in t.strips]) for t in ad.nla_tracks] if ad else None)
