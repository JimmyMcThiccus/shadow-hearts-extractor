bl_info = {
    "name": "Shadow Hearts 2 Cutscene Importer",
    "author": "sh2 decomp tools",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "File > Import > Shadow Hearts 2 Cutscene (.json)",
    "description": "Imports a cutscene package (export/scenes/<scene>.json): models, animation, face morphs, audio, camera",
    "category": "Import-Export",
}

import json
import math
import os

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Quaternion, Vector

AXES = [("0", "X", ""), ("1", "Y", ""), ("2", "Z", "")]


def _quat_matrix(q):
    n = math.sqrt(sum(c * c for c in q)) or 1.0
    x, y, z, w = [c / n for c in q]
    return Matrix(((1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
                   (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
                   (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y))))


# glTF frame (Y up) -> Blender frame (Z up), the same conversion the glTF importer applies to the models
_G2B = Matrix(((1, 0, 0), (0, 0, -1), (0, 1, 0)))


# engine world -> Blender world (calibrated against the game: camera orientation is upright with this mapping)
_E2B = Matrix(((-1, 0, 0), (0, 0, -1), (0, -1, 0)))


def camera_pose(k, o):
    """One decoded camera key -> (Blender location, Blender rotation quaternion).
    Engine: orientation matrix rows = camera axes in world space (row-vector convention), i.e. cam->world = R(q)^T;
    camera looks along local +Z, local +Y points down on screen."""
    r = k["rot_raw"]
    R = _quat_matrix((r[0] / 4096.0, r[1] / 4096.0, r[2] / 4096.0, r[3] / 32768.0))
    A = R.transposed()                                  # columns = camera axes in engine world
    right, down, fwd = A.col[0], A.col[1], A.col[2]
    M = _E2B @ Matrix((right, -down, -fwd)).transposed()  # Blender camera: X right, Y up, -Z forward
    loc = _E2B @ Vector(k["pos"]) * o.scale
    return loc, M.to_quaternion()

def _fcurves(ad):
    """All F-curves of an AnimData's action (Blender 4.x: action.fcurves; 5.x layered actions: channelbags)."""
    act = ad.action if ad else None
    if act is None:
        return []
    if hasattr(act, "fcurves"):
        return list(act.fcurves)
    out = []
    for layer in act.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                out.extend(bag.fcurves)
    return out


def _make_lights(sc, coll, lights, scale=1.0):
    """Stage lights from the scene's SBD LIGHT section: ambient (world colour), one sun, point lights."""
    amb = [0.0, 0.0, 0.0]
    for i, L in enumerate(lights):
        r, g, b = L["rgb"]
        if L["kind"] == "ambient":
            amb = [amb[0] + r * L["k"], amb[1] + g * L["k"], amb[2] + b * L["k"]]
        elif L["kind"] == "sun":
            data = bpy.data.lights.new("SH2Sun%d" % i, "SUN")
            data.color = (r, g, b)
            data.energy = 3.0 * L["k"] * scale
            ob = bpy.data.objects.new("SH2Sun%d" % i, data)
            d = _E2B @ Vector(L["dir"])                          # direction the light travels
            ob.rotation_mode = "QUATERNION"
            ob.rotation_quaternion = d.to_track_quat("-Z", "Y")
            coll.objects.link(ob)
        else:
            data = bpy.data.lights.new("SH2Point%d" % i, "POINT")
            data.color = (r, g, b)
            data.energy = 2000.0 * scale * (1.0 if L["k"] > 5 else 0.3)
            data.shadow_soft_size = 1.0
            ob = bpy.data.objects.new("SH2Point%d" % i, data)
            ob.location = _E2B @ Vector(L["pos"])
            coll.objects.link(ob)
    w = bpy.data.worlds.new("SH2World")
    w.use_nodes = True
    bg = w.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (amb[0], amb[1], amb[2], 1.0)
    bg.inputs[1].default_value = scale
    sc.world = w


def _sh3_unlit(ob):
    """SH3 stages are pre-lit (vertex colour x texture); the scene lights only reach characters.  Draw the stage as
    emission = texture x vertex colour so Blender lights and the world do not light it a second time."""
    if ob.type != "MESH":
        return
    for slot in ob.material_slots:
        m = slot.material
        if m is None or not m.use_nodes or m.get("sh3_unlit"):
            continue
        nt = m.node_tree
        bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is None:
            continue
        src = bsdf.inputs["Base Color"].links[0].from_socket if bsdf.inputs["Base Color"].links else None
        col = nt.nodes.new("ShaderNodeVertexColor")
        if ob.data.color_attributes:
            col.layer_name = ob.data.color_attributes[0].name
        mul = nt.nodes.new("ShaderNodeMixRGB")
        mul.blend_type = "MULTIPLY"
        mul.inputs[0].default_value = 1.0
        if src is not None:
            nt.links.new(src, mul.inputs[1])
        else:
            mul.inputs[1].default_value = bsdf.inputs["Base Color"].default_value
        if ob.data.color_attributes:
            nt.links.new(col.outputs["Color"], mul.inputs[2])
        else:
            mul.inputs[2].default_value = (1, 1, 1, 1)
        bsdf.inputs["Base Color"].default_value = (0, 0, 0, 1)
        for l in list(bsdf.inputs["Base Color"].links):
            nt.links.remove(l)
        nt.links.new(mul.outputs[0], bsdf.inputs["Emission Color"])
        bsdf.inputs["Emission Strength"].default_value = 1.0
        bsdf.inputs["Specular IOR Level"].default_value = 0.0
        m["sh3_unlit"] = True


def _sh3_txa(objs, txa, base):
    """SH3 texture animations (eye blinks): the frame images replace the material's texture while the scene's TXA
    property is k (0 = the plain texture).  A keyed Value node picks the image with COMPARE + mix nodes."""
    for t in txa:
        for ob in objs:
            if ob.type != "MESH":
                continue
            for slot in ob.material_slots:
                m = slot.material
                if m is None or not m.use_nodes or m.name.split(".")[0] != t["material"] or m.get("sh3_txa"):
                    continue
                nt = m.node_tree
                bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
                if bsdf is None or not bsdf.inputs["Base Color"].links:
                    continue
                img = bsdf.inputs["Base Color"].links[0].from_node
                if img.type != "TEX_IMAGE":
                    continue
                uv = img.inputs["Vector"].links[0].from_socket if img.inputs["Vector"].links else None
                val = nt.nodes.new("ShaderNodeValue")
                val.label = "TXA" + t["name"]
                cur = img.outputs["Color"]
                for k, path in enumerate(t["frames"], start=1):
                    tex = nt.nodes.new("ShaderNodeTexImage")
                    tex.image = bpy.data.images.load(os.path.normpath(os.path.join(base, path)), check_existing=True)
                    tex.interpolation = img.interpolation
                    if uv is not None:
                        nt.links.new(uv, tex.inputs["Vector"])
                    cmp = nt.nodes.new("ShaderNodeMath")
                    cmp.operation = "COMPARE"
                    cmp.inputs[1].default_value = float(k)
                    cmp.inputs[2].default_value = 0.5
                    nt.links.new(val.outputs[0], cmp.inputs[0])
                    mix = nt.nodes.new("ShaderNodeMixRGB")
                    nt.links.new(cmp.outputs[0], mix.inputs[0])
                    nt.links.new(cur, mix.inputs[1])
                    nt.links.new(tex.outputs["Color"], mix.inputs[2])
                    cur = mix.outputs[0]
                nt.links.new(cur, bsdf.inputs["Base Color"])
                val.outputs[0].default_value = 0.0
                val.outputs[0].keyframe_insert("default_value", frame=0)
                for f, v in t["keys"]:
                    val.outputs[0].default_value = float(v)
                    val.outputs[0].keyframe_insert("default_value", frame=int(f))
                for fc in _fcurves(nt.animation_data):
                    for kp in fc.keyframe_points:
                        kp.interpolation = "CONSTANT"
                m["sh3_txa"] = True


def _make_lights_sh3(sc, coll, chars, lights):
    """SH3 cutscene lights (LGTF, decoded from the engine's light interpreter): one entry per created light with keys
    {frame, rgb (1.0 = full, may exceed), k (strength), dir (direction the light travels, engine space), pos, cone, step}.
    The engine lights a vertex with ambient + sum(rgb * k * max(0, N.-dir)): sun energy = k * pi matches that for Blender's
    Lambert BSDF; the world colour (keyed per shot) carries the ambient term.  Lights only reach the characters."""
    import math
    w = bpy.data.worlds.new("SH3World")
    w.use_nodes = True
    bg = w.node_tree.nodes["Background"]
    bg.inputs[1].default_value = 1.0
    sc.world = w
    sc.view_settings.view_transform = "Standard"
    def key_world(f, rgb):
        bg.inputs[0].default_value = (rgb[0], rgb[1], rgb[2], 1.0)
        bg.inputs[0].keyframe_insert("default_value", frame=f)
    key_world(0, (0.0, 0.0, 0.0))
    for i, L in enumerate(lights):
        kind = L["kind"]
        if kind == "ambient":
            rgb = next((k["rgb"] for k in L["keys"] if "rgb" in k), (0.0, 0.0, 0.0))
            key_world(L["first"], rgb)
            continue
        btype = {"sun": "SUN", "point": "POINT", "spot": "SPOT"}.get(kind)
        if btype is None:
            continue
        data = bpy.data.lights.new("SH3_%s%d" % (kind, i), btype)
        if btype != "SUN":
            data.shadow_soft_size = 0.0
        data.use_shadow = False
        ob = bpy.data.objects.new(data.name, data)
        coll.objects.link(ob)
        if chars is not None and hasattr(ob, "light_linking"):
            ob.light_linking.receiver_collection = chars
        ob.rotation_mode = "QUATERNION"
        rgb, k, dirv, pos = [1.0, 1.0, 1.0], (2.0 if kind == "sun" else 1.0), [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]
        for K in L["keys"]:
            rgb = K.get("rgb", rgb)
            k = K.get("k", k)
            dirv = K.get("dir", dirv)
            pos = K.get("pos", pos)
            f = K["frame"]
            mx = max(max(rgb), 1e-6)
            data.color = [c / mx for c in rgb]
            if btype == "SUN":
                data.energy = k * mx * math.pi
            else:                                            # engine attenuation unknown: about 10 units of reach
                data.energy = k * mx * math.pi * 4.0 * 100.0
            if btype == "SPOT" and "cone" in K:
                data.spot_size = math.radians(min(max(2.0 * K["cone"], 1.0), 180.0))
            d_ = _E2B @ Vector(dirv)
            if d_.length > 1e-6:
                ob.rotation_quaternion = d_.to_track_quat("-Z", "Y")
            ob.location = _E2B @ Vector(pos)
            for tgt, path in ((data, "color"), (data, "energy"), (ob, "rotation_quaternion"), (ob, "location")):
                tgt.keyframe_insert(path, frame=f)
            if btype == "SPOT":
                data.keyframe_insert("spot_size", frame=f)
            if K.get("step"):
                for ad in (data.animation_data, ob.animation_data):
                    for fc in _fcurves(ad):
                        for kp in fc.keyframe_points:
                            if int(kp.co.x) == f:
                                kp.interpolation = "CONSTANT"
        for attr in ("hide_viewport", "hide_render"):
            setattr(ob, attr, True)
            ob.keyframe_insert(attr, frame=0)
            setattr(ob, attr, False)
            ob.keyframe_insert(attr, frame=int(L["first"]))
            setattr(ob, attr, True)
            ob.keyframe_insert(attr, frame=int(L["last"]))
    for fc in _fcurves(w.node_tree.animation_data):
        for kp in fc.keyframe_points:
            kp.interpolation = "CONSTANT"


def import_scene(context, path, o):
    base = os.path.dirname(path)
    d = json.load(open(path))
    sc = context.scene
    fps = int(d.get("fps", 30))
    sc.render.fps = fps
    sc.frame_start = 0
    if "instances" not in d or "frames" not in d:
        raise ValueError("%s is not a scene package. Pick export/scenes/<scene>.json (e.g. N001.json), "
                         "not a ranges.json / scene.json / other data file." % path)
    sc.frame_end = int(d["frames"])
    coll = bpy.data.collections.new("SH2 " + d["scene"])
    sc.collection.children.link(coll)
    sh3 = bool(d.get("sh3"))
    chars = None
    if sh3:                                              # light-linking target: the characters
        chars = bpy.data.collections.new("SH3 characters " + d["scene"])
        coll.children.link(chars)

    for inst in d["instances"]:
        glb = os.path.normpath(os.path.join(base, inst["glb"]))
        if not os.path.exists(glb):
            continue
        before = set(bpy.data.objects)
        bpy.ops.import_scene.gltf(filepath=glb)
        new = [ob for ob in bpy.data.objects if ob not in before]
        first, last = inst["first"], inst["last"]
        if inst.get("txa"):
            _sh3_txa(new, inst["txa"], base)
        for ob in new:
            for c in list(ob.users_collection):
                c.objects.unlink(ob)
            (chars if chars is not None and not inst.get("set") else coll).objects.link(ob)
            if sh3 and inst.get("set"):
                _sh3_unlit(ob)
            if ob.get("hidden") or ob.name.lower().endswith(("_b", "_c")) and "hand" in ob.name.lower():
                ob.hide_viewport = ob.hide_render = True
                continue
            ob.name = "%s.%s" % (inst["name"], ob.name.split(".")[0])
            # NLA-shift the imported action(s) so that t=0 of the clip lands on the instance's first frame
            for ad in (ob.animation_data, getattr(getattr(ob.data, "shape_keys", None), "animation_data", None)):
                if not ad:
                    continue
                # the glTF importer already made its own NLA track(s) at frame 0 and left an active action: gather every
                # distinct action, clear the importer's tracks, then place each action once at the instance's first frame
                acts = []                                   # (action, slot)
                if ad.action:
                    acts.append((ad.action, getattr(ad, "action_slot", None)))
                for trk in ad.nla_tracks:
                    for stp in trk.strips:
                        if stp.action and all(stp.action is not x[0] for x in acts):
                            acts.append((stp.action, getattr(stp, "action_slot", None)))
                for trk in list(ad.nla_tracks):
                    ad.nla_tracks.remove(trk)
                for act, slot in acts:
                    a0, a1 = act.frame_range
                    if a1 == a0:
                        continue
                    trk = ad.nla_tracks.new()
                    stp = trk.strips.new(act.name, int(first + a0), act)
                    if slot is not None and hasattr(stp, "action_slot"):
                        stp.action_slot = slot
                    stp.action_frame_start, stp.action_frame_end = a0, a1
                    stp.frame_end = stp.frame_start + (a1 - a0)
                ad.action = None
                mine = {int(first + act.frame_range[0]) for act, _s in acts}
                for trk in list(ad.nla_tracks):          # Blender stashes the unassigned action at frame 0: drop that copy
                    if trk.strips and trk.strips[0].frame_start not in mine and trk.strips[0].frame_start == 0:
                        ad.nla_tracks.remove(trk)
            if o.hide_outside:
                rngs = inst.get("ranges") or [[first, last]]
                for attr in ("hide_viewport", "hide_render"):
                    setattr(ob, attr, True)
                    ob.keyframe_insert(attr, frame=0)
                    for ra, rb in rngs:
                        setattr(ob, attr, False)
                        ob.keyframe_insert(attr, frame=int(ra))
                        setattr(ob, attr, True)
                        ob.keyframe_insert(attr, frame=int(rb) + 1)
                    setattr(ob, attr, True)

    if getattr(o, "import_lights", True) and d.get("lights"):
        if sh3:
            _make_lights_sh3(sc, coll, chars, d["lights"])
        else:
            _make_lights(sc, coll, d["lights"], getattr(o, "light_scale", 1.0))

    # camera: keys are {frame, rot_raw, pos, focal, cut}.  Verified against the running game (rotation and position match
    # the engine's camera object exactly); the engine interpolates between keys and holds a key flagged `cut`.
    if o.import_camera and d.get("camera"):
        cam_data = bpy.data.cameras.new("SH2Camera")
        cam_data.sensor_fit = "VERTICAL"
        cam_data.sensor_height = 24.0
        cam_data.clip_start = getattr(o, "clip_start", 2.5)       # near plane: the chair that sits in front of the lens at N125 frames 1002/2803 is clipped away
        cam_data.clip_end = 20000.0           # sky domes are ~2500 units across
        cam = bpy.data.objects.new("SH2Camera", cam_data)
        coll.objects.link(cam)
        sc.camera = cam
        sc.render.resolution_x, sc.render.resolution_y = 1280, 960
        cam.rotation_mode = "QUATERNION"
        prev = None
        cuts = []
        for k in d["camera"]:
            f = k["frame"]
            loc, q = camera_pose(k, o)
            if prev is not None and q.dot(prev) < 0:
                q.negate()
            prev = q
            cam.location, cam.rotation_quaternion = loc, q
            cam.keyframe_insert("location", frame=f)
            cam.keyframe_insert("rotation_quaternion", frame=f)
            focal_px = k.get("focal", 512) / 512.0 * 682.0          # pixels on the 448-line display
            cam_data.lens = focal_px * cam_data.sensor_height / 448.0
            cam_data.keyframe_insert("lens", frame=f)
            if k.get("cut"):
                cuts.append(f)
        if cuts:
            cutset = set(cuts)
            for ad in (cam.animation_data, cam_data.animation_data):
                for fc in _fcurves(ad):
                    for kp in fc.keyframe_points:
                        if int(kp.co.x) in cutset:
                            kp.interpolation = "CONSTANT"
    # audio
    if o.import_audio and d.get("audio"):
        wav = os.path.normpath(os.path.join(base, d["audio"]))
        if os.path.exists(wav):
            if not sc.sequence_editor:
                sc.sequence_editor_create()
            seqs = sc.sequence_editor.sequences if hasattr(sc.sequence_editor, "sequences") else sc.sequence_editor.strips
            seqs.new_sound("SH2 audio", wav, 1, 0)
    return d


class IMPORT_OT_sh2_cutscene(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.sh2_cutscene"
    bl_label = "Import Shadow Hearts 2 Cutscene"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})

    import_camera: BoolProperty(name="Camera", default=True)
    import_audio: BoolProperty(name="Audio", default=True)
    hide_outside: BoolProperty(name="Hide models outside their time range", default=True)
    clip_start: FloatProperty(name="Camera clip start", default=2.5, min=0.01, description="Near clip distance of the imported camera")
    scale: FloatProperty(name="World scale", default=1.0, description="Multiplier for camera positions (models are imported at 1:1)")

    def execute(self, context):
        try:
            import_scene(context, self.filepath, self)
        except ValueError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        return {"FINISHED"}


def menu(self, context):
    self.layout.operator(IMPORT_OT_sh2_cutscene.bl_idname, text="Shadow Hearts 2 Cutscene (.json)")


def register():
    bpy.utils.register_class(IMPORT_OT_sh2_cutscene)
    bpy.types.TOPBAR_MT_file_import.append(menu)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu)
    bpy.utils.unregister_class(IMPORT_OT_sh2_cutscene)


if __name__ == "__main__":
    register()




