"""Import an exported SH2 .glb into Blender and hide the alternate hand poses (B, C, ...).

In Blender:  Scripting tab -> open this file, set GLB below (or pass it after '--' on the command line) -> Run.
The game swaps whole hand meshes instead of rigging fingers; the alternates are separate objects
flagged with the custom property 'hidden'. Un-hide them (eye icon) to use another hand pose.
"""
import sys

import bpy

GLB = ""   # path to an exported .glb
if "--" in sys.argv:
    GLB = sys.argv[sys.argv.index("--") + 1]

bpy.ops.import_scene.gltf(filepath=GLB)
for o in bpy.context.scene.objects:
    if o.get("hidden"):
        o.hide_viewport = True
        o.hide_render = True
        o.hide_set(True)
print("imported", GLB)
