"""blender -b --python blender_addon/test_import.py -- <scene.json> <outdir> <frame,frame,...> [key=value ...]
Imports a scene with the add-on code and renders the camera view at the given frames (workbench)."""
import os
import sys
import math
import types

import bpy

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("sh2_cutscene_local", os.path.join(here, "sh2_cutscene.py"))   # not an installed copy
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)

args = sys.argv[sys.argv.index("--") + 1:]
path, outdir, frames = args[0], args[1], [int(x) for x in args[2].split(",")]
o = types.SimpleNamespace(import_camera=True, import_audio=False, import_lights=bool(os.environ.get('LIGHTS')), light_scale=float(os.environ.get('LSCALE','1')), hide_outside=True, camera_track="all", fov=0.0,
                          scale=1.0, flip_x=1.0, flip_y=1.0, flip_z=1.0, quat_order="0123", invert_rot=False,
                          fwd_axis="2", fwd_sign=1.0, up_axis="1", up_sign=-1.0)
for kv in args[3:]:
    k, v = kv.split("=", 1)
    cur = getattr(o, k)
    setattr(o, k, type(cur)(v) if not isinstance(cur, bool) else v in ("1", "true", "True"))
bpy.ops.wm.read_factory_settings(use_empty=True)
d = A.import_scene(bpy.context, path, o)
sc = bpy.context.scene
sc.render.engine = os.environ.get("ENGINE", "BLENDER_WORKBENCH")
if sc.render.engine != "BLENDER_WORKBENCH" and not os.environ.get("LIGHTS"):
    w = bpy.data.worlds.new("w")
    w.use_nodes = True
    bg = w.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (1, 1, 1, 1)
    bg.inputs[1].default_value = float(os.environ.get("WORLD", "1.0"))
    sc.world = w
    sc.view_settings.view_transform = "Standard"
    if hasattr(sc, "eevee"):
        sc.eevee.taa_render_samples = 8
sc.display.shading.light = "STUDIO"
sc.display.shading.color_type = "TEXTURE"
sc.render.resolution_x, sc.render.resolution_y = [int(v) for v in os.environ.get('RES', '640x480').split('x')]
os.makedirs(outdir, exist_ok=True)
if os.environ.get("MANUAL"):
    import mathutils
    c = sc.camera
    c.animation_data_clear()
    c.location = (0, -160, 30)
    c.rotation_mode = "XYZ"
    c.rotation_euler = (math.radians(86), 0, 0)
    c.data.sensor_fit = "VERTICAL"

import mathutils
if os.environ.get("LIVEPOS"):
    cam = sc.camera
    cam.animation_data_clear()
    cam.location = A._E2B @ mathutils.Vector((-22.4, -12.9, -18.9))
    live = mathutils.Matrix(((0.5413, -7.9e-5, -0.8406), (0.0959, 0.9944, 0.0605), (0.8346, -0.1139, 0.5405)))
    right, down, fwd = live.transposed().col[0], live.transposed().col[1], live.transposed().col[2]
    cam.rotation_mode = "QUATERNION"
    cam.rotation_quaternion = (A._E2B @ mathutils.Matrix((right, -down, -fwd)).transposed()).to_quaternion()
for ob in bpy.data.objects:
    for pref in os.environ.get("HIDE", "").split(","):
        if pref and ob.name.startswith(pref):
            ob.animation_data_clear()
            ob.hide_render = ob.hide_viewport = True
for f in frames:
    sc.frame_set(f)
    sc.render.filepath = os.path.join(outdir, "f%05d.png" % f)
    bpy.ops.render.render(write_still=True)
print("done", len(bpy.data.objects), "objects")
for f in frames[:1]:
    sc.frame_set(f)
    cam = sc.camera
    print("CAM", f, tuple(round(v, 2) for v in cam.matrix_world.translation), "fwd", tuple(round(v, 2) for v in (cam.matrix_world.to_3x3() @ __import__("mathutils").Vector((0, 0, -1)))))
    vis = [ob for ob in bpy.data.objects if ob.type == "ARMATURE" and not ob.hide_render]
    for ob in vis[:8]:
        print("ARM", ob.name, tuple(round(v, 1) for v in ob.matrix_world.translation), [round(v, 1) for v in ob.pose.bones[min(2, len(ob.pose.bones)-1)].head] if ob.pose else None)



from bpy_extras.object_utils import world_to_camera_view
import mathutils
sc.frame_set(frames[0])
cam = sc.camera
print("CAMLENS", cam.data.lens, cam.data.sensor_height, cam.data.clip_start, cam.data.clip_end, tuple(round(v,1) for v in cam.matrix_world.translation))
for ob in bpy.data.objects:
    if ob.type == "ARMATURE" and not ob.hide_render:
        w = ob.matrix_world @ ob.pose.bones[min(2, len(ob.pose.bones)-1)].head
        print("NDC", ob.name, [round(v, 2) for v in world_to_camera_view(sc, cam, w)])
for ob in bpy.data.objects:
    if ob.type == "MESH":
        print("MESH", ob.name, ob.hide_render, ob.hide_viewport, [round(v, 1) for v in ob.dimensions], ob.parent.name if ob.parent else None)

