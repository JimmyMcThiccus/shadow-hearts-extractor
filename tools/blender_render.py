"""Render a .glb with Blender (workbench, textured).
blender -b --python tools/blender_render.py -- <glb> <out.png> [action=] [frame=0] [az=0] [el=0] [zoom=1] [cx= cy= cz=] [cull=0]
az: camera azimuth in degrees around the model (0 = looking at the glTF front, +Z side), zoom: >1 = closer.
"""
import math
import sys

import bpy
from mathutils import Vector

args = sys.argv[sys.argv.index("--") + 1:]
glb, out = args[0], args[1]
opt = dict(a.split("=") for a in args[2:])
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()
bpy.ops.import_scene.gltf(filepath=glb)
sc = bpy.context.scene
for o in list(bpy.data.objects):
    if o.get("hidden"):          # alternate hand poses
        bpy.data.objects.remove(o, do_unlink=True)
arm =next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
if arm and opt.get("action"):
    act = bpy.data.actions.get(opt["action"])
    arm.animation_data_create().action = act
if arm and not opt.get("action") and arm.animation_data:
    arm.animation_data.action = None
sc.frame_set(int(float(opt.get("frame", 0))))
bpy.context.view_layer.update()
for o in bpy.data.objects:
    if o.type == "MESH":
        for m in o.data.materials:
            m.use_backface_culling = opt.get("cull", "0") == "1"
pts = []
dg = bpy.context.evaluated_depsgraph_get()
for o in bpy.data.objects:
    if o.type == "MESH":
        ev = o.evaluated_get(dg)
        me = ev.to_mesh()
        pts += [ev.matrix_world @ v.co for v in me.vertices]
if "top" in opt:                 # focus on the top fraction of the model (head close-ups)
    zmin, zmax = min(p.z for p in pts), max(p.z for p in pts)
    cut = zmax - (zmax - zmin) * float(opt["top"])
    pts = [p for p in pts if p.z >= cut]
lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
c = (lo + hi) / 2
if "cx" in opt:
    c = Vector((float(opt["cx"]), float(opt["cy"]), float(opt["cz"])))
size = max(hi.x - lo.x, hi.y - lo.y, hi.z - lo.z)
cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
sc.collection.objects.link(cam)
cam.data.type = "ORTHO"
cam.data.ortho_scale = size * 1.05 / float(opt.get("zoom", 1))
az, el = math.radians(float(opt.get("az", 0))), math.radians(float(opt.get("el", 0)))
d = size * 3
cam.location = c + Vector((math.sin(az) * math.cos(el) * d, math.cos(az) * math.cos(el) * d, math.sin(el) * d))
cam.rotation_euler = (c - cam.location).to_track_quat("-Z", "Y").to_euler()
sc.camera = cam
sc.render.engine = "BLENDER_WORKBENCH"
sc.display.shading.light = "FLAT" if opt.get("shade") == "flat" else "STUDIO"
sc.display.shading.color_type = "TEXTURE"
sc.display.shading.show_backface_culling = opt.get("cull", "0") == "1"
sc.render.resolution_x = int(opt.get("w", 900))
sc.render.resolution_y = int(opt.get("h", 900))
sc.render.film_transparent = False
sc.world = bpy.data.worlds.new("w")
sc.world.color = (0.12, 0.13, 0.15)
sc.render.filepath = out
bpy.ops.render.render(write_still=True)
print("RENDERED", out)
