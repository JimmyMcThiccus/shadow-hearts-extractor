"""Export a Shadow Hearts: Covenant model set (.mdl + .anm files) to a binary glTF (.glb).

Coordinate conversion (all verified against the engine data, see FORMATS.md):
    mesh space  m = (mx,my,mz)  ->  glTF  (mx, -my, -mz)   (joint space = point inversion of mesh space)
    joint bind pos b            ->  glTF  (-bx, by, bz)      (joint-space is rot180Z of mesh space, then mirrored)
    animation rotation q        ->  glTF  q unchanged
    animation translation p     ->  glTF  (-px, -py, -pz)
Skinning: every corner picks one of its group's 4 bones (flags & 3; SH3_1.01: 5 bones, flags & 7); bone ids >= joint count are
virtual blend bones (WFRH chunk) and are expanded into several glTF influences.
"""
import json
import os
import re
import struct
import sys
import zlib

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ssh_model import (parse_mdl, parse_wfrh, parse_sequence, write_png)  # noqa: E402


# ------------------------------------------------------------------ textures
def png_rgba(w, h, rgba):
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw.tobytes() if hasattr(raw, "tobytes") else raw, 6)) + chunk(b"IEND", b""))


_CSM1 = np.arange(256)
_CSM1 = (_CSM1 & 0xE7) | ((_CSM1 & 0x08) << 1) | ((_CSM1 & 0x10) >> 1)


def txa_frames(b):
    """SH3_1.01 TXAH (header +0x24) texture animations, e.g. 'eye' blinks driven by the LSD property TXAeye:
    entry = 'TXAD' + name[12], u8 texture, u8 frames, u16 w, u16 h, u16 stride, u16 frame bytes, u16 x,
    u16 y-from-bottom (y = texture height - value - h), +0x20 u16 size / 16, frames of 8-bit pixels at +0x30.
    Property value 0 = the plain texture, k = frame k - 1.  -> [(name, texture index, [rgba of the whole texture per frame])]"""
    if b[:8] != b"SH3_1.01":
        return []
    o = struct.unpack("<I", b[0x24:0x28])[0]
    if not o or b[o:o + 4] != b"TXAH":
        return []
    raw = []
    texs = parse_textures(b, raw)
    out = []
    q = o + 16
    for _ in range(struct.unpack("<I", b[o + 4:o + 8])[0]):
        if b[q:q + 4] != b"TXAD":
            break
        name = b[q + 4:q + 16].split(b"\0")[0].decode("latin1")
        ti, nf, w, h, stride, fbytes, x, yb = struct.unpack_from("<BBHHHHHH", b, q + 16)
        size = struct.unpack("<H", b[q + 32:q + 34])[0] * 16
        if ti < len(raw) and raw[ti] is not None:
            pal, idx = raw[ti]
            th = idx.shape[0]
            y = th - yb - h
            frames = []
            for k in range(nf):
                f = np.frombuffer(b, np.uint8, stride * h, q + 0x30 + k * fbytes).reshape(h, stride)[:, :w]
                img = idx.copy()
                img[y:y + h, x:x + w] = f
                frames.append(pal[img])
            out.append((name, ti, frames))
        q += 0x30 + size
    return out


def parse_textures_sh3(b, off, raw_out=None):
    """SH3_1.01 TXHD: [16 B hdr][16 B VRAM summary][n x 0x30 descriptors][CLUT raster][pixels].

    Descriptor: +0 u16 w, +2 u16 h, +4 u16 bpp (low byte), +6 u16 clut word (bits0-7 = GS block of the 16x16 CLUT,
    bits8-12 = CSA for 4-bit), +0x0d bit 7 = pixel group, +0x1c u32 pixel offset within the group.
    The CLUT area is a PSMCT32 VRAM raster just big enough for the CLUTs used, pages stacked vertically: block b ->
    x = 16 (b&4) + 32 (b&16), y = 32 * (b // 32) + 16 (b&8); width/height = max + 16.  The pixel area follows it:
    group 0 (bit clear) first, then group 1, each group as long as its furthest texture end (stage maps use both groups).
    """
    n = struct.unpack("<I", b[off + 4:off + 8])[0]
    start = off + 32 + 0x30 * n
    raw_ds = []
    for i in range(n):
        d = b[off + 32 + 0x30 * i:off + 32 + 0x30 * (i + 1)]
        w, h, f, c = struct.unpack("<4H", d[:8])
        po = struct.unpack("<I", d[0x1c:0x20])[0]
        raw_ds.append((w, h, f & 0xFF, c, po, d[0x0d] >> 7))
    ext = {}
    for w, h, bpp, c, po, g in raw_ds:
        ext[g] = max(ext.get(g, 0), po + w * h * bpp // 8)
    base, acc = {}, 0
    for g in sorted(ext):
        base[g] = acc
        acc += ext[g]
    ds = [(w, h, bpp, c, base[g] + po) for w, h, bpp, c, po, g in raw_ds]
    xy = lambda blk: ((16 if blk & 4 else 0) + (32 if blk & 16 else 0), 32 * (blk // 32) + (16 if blk & 8 else 0))
    pts = [xy(c & 0xFF) for _w, _h, _b, c, _p in ds] or [(0, 0)]
    cw = max(x for x, _ in pts) + 16
    ch = max(y for _, y in pts) + 16
    pix0 = start + cw * 4 * ch
    out = []
    for w, h, bpp, c, po in ds:
        blk, csa = c & 0xFF, (c >> 8) & 0x1F
        bx, by = xy(blk)
        raw = np.zeros((256, 4), np.uint8)
        for y in range(16):
            o = start + (by + y) * cw * 4 + bx * 4
            if o + 64 <= pix0:
                raw[y * 16:(y + 1) * 16] = np.frombuffer(b, np.uint8, 64, o).reshape(16, 4)
        pal = raw[_CSM1].copy()
        if bpp == 4:
            pal = pal[csa * 16:csa * 16 + 16]
        pal[:, 3] = np.minimum(pal[:, 3].astype(np.int32) * 2, 255)
        data = np.frombuffer(b, np.uint8, offset=pix0 + po, count=w * h * bpp // 8)
        if bpp == 4:
            px = np.empty(w * h, np.uint8)
            px[0::2], px[1::2] = data & 15, data >> 4
        else:
            px = data
        out.append((w, h, pal[px.reshape(h, w)]))
        if raw_out is not None:
            raw_out.append((pal, px.reshape(h, w)) if bpp == 8 else None)
    return out


def parse_textures(b, raw=None):
    """TXHD section at header +0x18 -> [(w, h, rgba uint8 (h,w,4))]."""
    off = struct.unpack("<I", b[0x18:0x1c])[0]
    out = []
    if off not in (0, 0xFFFFFFFF) and b[off:off + 4] != b"TXHD":
        nx = b.find(b"TXHD", off, off + 0x100)      # set models: filler bytes in front of the section
        if nx > 0:
            off = nx
    if off in (0, 0xFFFFFFFF) or b[off:off + 4] != b"TXHD":
        return out
    if b[:8] == b"SH3_1.01" and b[off + 16:off + 20] != b"TIHD":
        return parse_textures_sh3(b, off, raw)
    n = struct.unpack("<I", b[off + 4:off + 8])[0]
    o = off + 0x10
    for _ in range(n):
        if b[o:o + 4] != b"TIHD":
            nx = b.find(b"TIHD", o, o + 0x40)         # some textures are followed by a few filler bytes
            if nx < 0:
                break
            o = nx
        w, h = struct.unpack("<2H", b[o + 4:o + 8])
        bpp, f2, hs, tot = struct.unpack("<4I", b[o + 8:o + 24])
        hdr = (f2 >> 16) & 0xFF
        ncol = f2 & 0xFFFF
        pal = np.frombuffer(b, np.uint8, 4 * ncol, o + hdr).reshape(ncol, 4).copy()
        if ncol == 256:   # CSM1 palette swizzle (swap bits 3 and 4)
            idx = np.arange(256)
            pal = pal[(idx & 0xE7) | ((idx & 0x08) << 1) | ((idx & 0x10) >> 1)]
        pal[:, 3] = np.minimum(pal[:, 3].astype(np.int32) * 2, 255)   # PS2 alpha 0x80 = opaque
        data = np.frombuffer(b, np.uint8, offset=o + hs, count=(w * h if bpp == 8 else w * h // 2))
        if bpp == 4:
            px = np.empty(w * h, np.uint8)
            px[0::2], px[1::2] = data & 15, data >> 4
        else:
            px = data
        out.append((w, h, pal[px.reshape(h, w)]))
        o += tot
    return out


# ------------------------------------------------------------------ glb writer
class GLB:
    def __init__(self):
        self.bin = bytearray()
        self.views, self.accs = [], []

    def view(self, data, target=None):
        while len(self.bin) % 4:
            self.bin.append(0)
        v = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        if target:
            v["target"] = target
        self.bin += data
        self.views.append(v)
        return len(self.views) - 1

    def acc(self, arr, ctype, typ, target=None, minmax=False):
        arr = np.ascontiguousarray(arr)
        a = {"bufferView": self.view(arr.tobytes(), target), "componentType": ctype,
             "count": int(arr.shape[0]), "type": typ}
        if minmax:
            a["min"] = np.atleast_1d(arr.min(0)).astype(float).tolist()
            a["max"] = np.atleast_1d(arr.max(0)).astype(float).tolist()
        self.accs.append(a)
        return len(self.accs) - 1

    def save(self, path, gltf):
        gltf["buffers"] = [{"byteLength": len(self.bin)}]
        gltf["bufferViews"], gltf["accessors"] = self.views, self.accs
        js = json.dumps(gltf, separators=(",", ":")).encode()
        js += b" " * (-len(js) % 4)
        bn = bytes(self.bin) + b"\0" * (-len(self.bin) % 4)
        with open(path, "wb") as f:
            f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(bn)))
            f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
            f.write(struct.pack("<II", len(bn), 0x004E4942) + bn)


FLOAT, USHORT, UINT, UBYTE = 5126, 5123, 5125, 5121
TS = np.array([float(v) for v in os.environ.get('SH2_TS', '-1,1,-1').split(',')])   # stream translation signs


def build(mdl_path, anm_paths, out_path, name=None, skip=(), stream=None, yaw180=None, tex_from=None):
    b = open(mdl_path, "rb").read()
    joints, meshes = parse_mdl(b, bsah=True)
    virt = parse_wfrh(b, len(joints))
    texs = parse_textures(b)
    sh3 = b[:8] == b"SH3_1.01"
    if not texs and tex_from:                   # accessory models (hair, cape ...) use the character's texture
        texs = parse_textures(open(tex_from, "rb").read())
    nj = len(joints)
    good = [m for m in meshes if m.pos is not None and not m.error and not m.bsah]
    # set (stage) models consist of rigid meshes only; they are placed with the engine-world mapping (-x,-y,z)
    setmode = bool(good) and all(m.rigid_bone is not None for m in good)
    MP = np.array([-1.0, -1.0, 1.0]) if setmode else np.array([1.0, -1.0, -1.0])      # mesh -> glTF
    BPv = np.array([1.0, 1.0, -1.0]) if setmode else np.array([-1.0, 1.0, 1.0])       # joint bind position -> glTF
    G = GLB()
    gltf = {"asset": {"version": "2.0", "generator": "ssh_gltf.py"}, "scene": 0}
    if setmode:
        gltf["asset"]["extras"] = {"sh2_set": True, "meshes": len(good)}

    bp = np.array([j.quat[:3] for j in joints], np.float64)
    bpf = bp * BPv                                        # final-space bind positions

    # nodes: joints first (index == joint id), then mesh node
    nodes = []
    for i, j in enumerate(joints):
        par = j.parent if j.parent != 255 else None
        t = bpf[i] - (bpf[par] if par is not None else 0)
        nodes.append({"name": j.name, "translation": t.tolist(), "children": []})
    pivot = np.zeros(3)                                    # turn the model around its own centre (x,z of the mesh), not the joint
    pm = [np.stack([m.pos[:, 0], -m.pos[:, 1], -m.pos[:, 2]], 1) for m in meshes
          if m.pos is not None and not m.error and not m.bsah and m.rigid_bone is None]
    if pm:
        c = np.concatenate(pm).mean(0)
        pivot = np.array([2 * c[0], 0.0, 2 * c[2]])
    if yaw180 is None:
        yaw180 = os.environ.get("SH2_YAW180", "1") == "1"
    r1 = next((i for i, j in enumerate(joints) if j.name == "ROOT1"), None)
    jr = next((i for i, j in enumerate(joints) if j.name.startswith("WAIST") and (r1 is None or j.parent == r1)), None)   # turn the body around its waist: the walking offset lives in WAIST0 translation
    yawq = lambda q: np.stack([q[:, 2], q[:, 3], -q[:, 0], -q[:, 1]], 1)       # Ry180 * q
    if yaw180 and jr is not None:
        nodes[jr]["rotation"] = [0.0, 1.0, 0.0, 0.0]
    roots = []
    for i, j in enumerate(joints):
        if j.parent == 255 or j.parent >= nj:
            roots.append(i)
        else:
            nodes[j.parent]["children"].append(i)
    for n in nodes:
        if not n["children"]:
            del n["children"]

    ibm = np.zeros((nj, 4, 4), np.float32)
    for i in range(nj):
        ibm[i] = np.eye(4)
        ibm[i][3, :3] = -bpf[i]                            # column-major: translation in last row of the array
    gltf["skins"] = [{"inverseBindMatrices": G.acc(ibm.reshape(nj, 16), FLOAT, "MAT4"),
                      "joints": list(range(nj)), "skeleton": roots[0] if roots else 0}]

    # textures / materials
    images, gtex, mats = [], [], []
    for (w, h, rgba) in texs:
        if setmode and not sh3 and rgba[..., 3].mean() < 80:   # SH2 window light shafts: additive white beams in the game
            rgba = rgba.copy()
            rgba[..., :3] = 255
            rgba[..., 3] = np.minimum(rgba[..., 3].astype(np.int32) * 3, 255)
        img = png_rgba(w, h, rgba)
        images.append({"bufferView": G.view(img), "mimeType": "image/png"})
        gtex.append({"source": len(images) - 1, "sampler": 0})
        a = rgba[..., 3]
        mode = "OPAQUE" if (a == 255).all() else "MASK"   # BLEND would disable depth writes in most viewers
        if setmode and a.mean() < 170:                       # stage shadow / light-shaft planes are translucent textures
            mode = "BLEND"
        mat = {"name": "tex%d" % (len(gtex) - 1),
               "pbrMetallicRoughness": {"baseColorTexture": {"index": len(gtex) - 1}, "metallicFactor": 0.0,
                                        "roughnessFactor": 1.0}, "doubleSided": True, "alphaMode": mode}
        if mode == "MASK":
            mat["alphaCutoff"] = 0.5
        mats.append(mat)
    if not mats:
        mats.append({"pbrMetallicRoughness": {"baseColorFactor": [0.7, 0.7, 0.7, 1], "metallicFactor": 0.0},
                     "doubleSided": True})
    gltf["images"], gltf["textures"], gltf["materials"] = images, gtex, mats
    gltf["samplers"] = [{"magFilter": 9729, "minFilter": 9729, "wrapS": int(os.environ.get("SH2_WRAPS", "10497")), "wrapT": 10497}]

    skip = set(skip) | {n for n in os.environ.get('SH2_SKIP', '').split(',') if n}
    prims = []
    for m in meshes:
        if m.error or m.pos is None or (m.rigid_bone is not None and not setmode) or m.name in skip:
            continue
        if tex_from and not np.any(m.uv):            # untextured dynamic piece (hair strands): needs the physics, hidden for now
            continue
        if m.name.lower().startswith("kage") and not (sh3 and setmode):   # character's projected-shadow proxy
            continue                                                      # (SH3 stage 'kage' meshes are baked decals)
        if sh3 and setmode and ("shadow" in m.name.lower() or "kage" in m.name.lower()) and texs:
            im = texs[min(getattr(m, "hdr_b2", 0), len(texs) - 1)][2]
            if (im == im[0, 0]).all():       # run-time shadow target: the file holds only a flat placeholder
                continue                     # (NY_01_U 'shadow': navy, alpha 35 -> a pale sheet over the street)
        T = m.triangles()
        if not len(T):
            continue
        n = len(m.pos)
        pos = (m.pos * MP).astype(np.float32)
        fn = np.cross(pos[T[:, 1]] - pos[T[:, 0]], pos[T[:, 2]] - pos[T[:, 0]])
        if m.nrm is not None:
            nrm = (m.nrm * MP).astype(np.float32)
            # re-orient triangles in final space against the final normals
            flip = (fn * (nrm[T[:, 0]] + nrm[T[:, 1]] + nrm[T[:, 2]])).sum(1) < 0
            T = T.copy()
            T[flip] = T[flip][:, [1, 0, 2]]
        else:                                    # rigid mesh: strip winding is already consistent, derive vertex normals
            nrm = np.zeros((n, 3), np.float64)
            for k in range(3):
                np.add.at(nrm, T[:, k], fn)
            nrm = (nrm / np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-9)).astype(np.float32)
        ti = min(getattr(m, "hdr_b2", 0), len(texs) - 1) if texs else -1
        tw, th = (texs[ti][0], texs[ti][1]) if ti >= 0 else (256, 256)
        tw, th = 256, 256                  # UVs are always in a 256x256 space; narrower textures are stretched to it
        uvm = m.uv
        uv = np.stack([uvm[:, 0] / tw, uvm[:, 1] / th], 1).astype(np.float32)
        J = np.zeros((n, 4), np.uint16)
        W = np.zeros((n, 4), np.float32)
        if m.rigid_bone is not None:
            J[:, 0] = m.rigid_bone
            W[:, 0] = 1.0
        for ids, start, count in ([] if m.rigid_bone is not None else m.groups):
            for i in range(start, start + count):
                bid = ids[m.flags[i] & (7 if len(ids) > 4 else 3)]     # SH3: 5 bones per group
                if bid == 0xFF:
                    infl = [(0, 1.0)]
                elif bid < nj:
                    infl = [(bid, 1.0)]
                else:
                    infl = virt[bid - nj][:4] if bid - nj < len(virt) else [(0, 1.0)]
                tot = sum(w for _, w in infl) or 1.0
                for k, (jb, w) in enumerate(infl[:4]):
                    J[i, k], W[i, k] = jb, w / tot
        attrs = {"POSITION": G.acc(pos, FLOAT, "VEC3", 34962, True), "NORMAL": G.acc(nrm, FLOAT, "VEC3", 34962),
                 "TEXCOORD_0": G.acc(uv, FLOAT, "VEC2", 34962), "JOINTS_0": G.acc(J, USHORT, "VEC4", 34962),
                 "WEIGHTS_0": G.acc(W, FLOAT, "VEC4", 34962)}
        if m.col is not None:                    # PS2 vertex colour: 0x80 = 1.0
            attrs["COLOR_0"] = G.acc(np.minimum(m.col.astype(np.float32) / 128.0, 1.0), FLOAT, "VEC4", 34962)
        idx = G.acc(T.reshape(-1).astype(np.uint32), UINT, "SCALAR", 34963)
        prim = {"attributes": attrs, "indices": idx, "material": max(ti, 0) if texs else 0}
        tnames = []
        if getattr(m, "morphs", None):
            tg = []
            for fn_, fp in m.morphs:
                d = np.stack([fp[:, 0] - m.pos[:, 0], -(fp[:, 1] - m.pos[:, 1]), -(fp[:, 2] - m.pos[:, 2])], 1)
                tg.append({"POSITION": G.acc(d.astype(np.float32), FLOAT, "VEC3", 34962, True)})
                tnames.append(fn_)
            prim["targets"] = tg
        prims.append({"name": m.name, "primitive": prim, "targets": tnames, "bsah": m.bsah})
    # The game swaps whole hand meshes (open / fist / ...) instead of rigging fingers:
    # *_hand_A is the default pose, B/C/... are alternates -> separate nodes, hidden by default.
    is_alt = lambda nm: re.match(r"^[LR]_hand_[B-Z]$", nm, re.I) is not None
    faces = [p for p in prims if p["bsah"]]
    prims = [p for p in prims if not p["bsah"]]
    main = [p for p in prims if not is_alt(p["name"])]
    alts = [p for p in prims if is_alt(p["name"])]
    gltf["meshes"] = [{"name": name or os.path.basename(mdl_path), "primitives": [p["primitive"] for p in main]}]
    nodes.append({"name": "mesh", "mesh": 0, "skin": 0})
    scene_nodes = roots + [len(nodes) - 1]
    for p in alts:
        gltf["meshes"].append({"name": p["name"], "primitives": [p["primitive"]]})
        nodes.append({"name": p["name"], "mesh": len(gltf["meshes"]) - 1, "skin": 0, "extras": {"hidden": True},
                      "extensions": {"KHR_node_visibility": {"visible": False}}})
        scene_nodes.append(len(nodes) - 1)
    face_nodes = []
    for fi, p in enumerate(faces):
        # one mesh per blend-shape piece so each owns its own morph weights (visemes / blink / brow)
        t0 = p["targets"][0] if p["targets"] else ""
        kind = ("eyelid" if t0.startswith("eyelid") else "eyebrow" if t0.startswith("eyeblow") or "_eb_" in t0.lower()
                else "mouth")                                   # SH3 eyebrow targets: J_L_eb_U4 / _E4 / _A4
        nm = "face_%s_%d" % (kind, fi)
        msh = {"name": nm, "primitives": [p["primitive"]], "weights": [0.0] * len(p["targets"]),
               "extras": {"targetNames": [t for t in p["targets"]]}}
        p["primitive"].setdefault("extras", {})["targetNames"] = list(p["targets"])
        gltf["meshes"].append(msh)
        nodes.append({"name": nm, "mesh": len(gltf["meshes"]) - 1, "skin": 0})
        scene_nodes.append(len(nodes) - 1)
        face_nodes.append((len(nodes) - 1, kind, p["targets"]))
    if alts:
        gltf["extensionsUsed"] = ["KHR_node_visibility"]
    gltf["nodes"] = nodes
    gltf["scenes"] = [{"nodes": scene_nodes}]

    # animations
    anims = []
    jidx = {j.name: i for i, j in enumerate(joints)}
    for ap in anm_paths:
        try:
            seq = parse_sequence(open(ap, "rb").read())
        except Exception as e:                      # noqa: BLE001
            print("  skip", os.path.basename(ap), e)
            continue
        samplers, channels = [], []
        for nm, tr in seq.nodes.items():
            if nm not in jidx:
                continue
            i = jidx[nm]
            q = tr["rv"].copy()
            if yaw180 and i == jr:
                q = yawq(q)
            for k in range(1, len(q)):
                if np.dot(q[k - 1], q[k]) < 0:
                    q[k] = -q[k]
            q /= np.maximum(np.linalg.norm(q, axis=1, keepdims=True), 1e-9)
            samplers.append({"input": G.acc((tr["rt"] / seq.fps).astype(np.float32), FLOAT, "SCALAR", None, True),
                             "output": G.acc(q.astype(np.float32), FLOAT, "VEC4"), "interpolation": "LINEAR"})
            channels.append({"sampler": len(samplers) - 1, "target": {"node": i, "path": "rotation"}})
            if np.abs(tr["pv"]).max() > 0:
                par = joints[i].parent
                base = bpf[i] - (bpf[par] if par != 255 and par < nj else 0)
                tv = base + tr["pv"] * np.array([-1, -1, -1])
                samplers.append({"input": G.acc((tr["pt"] / seq.fps).astype(np.float32), FLOAT, "SCALAR", None, True),
                                 "output": G.acc(tv.astype(np.float32), FLOAT, "VEC3"), "interpolation": "LINEAR"})
                channels.append({"sampler": len(samplers) - 1, "target": {"node": i, "path": "translation"}})
        if channels:
            anims.append({"name": seq.name, "samplers": samplers, "channels": channels})
    if stream:
        samplers, channels = [], []
        fps, t0 = 30.0, stream["t0"]
        for i, keys in stream["rot"].items():
            if i >= nj or not keys:
                continue
            fr = sorted(keys)
            q = np.array([keys[f] for f in fr])
            if os.environ.get("SH2_ROTCONJ") == "1":
                q = q * np.array([-1, -1, -1, 1])
            if setmode:                     # stage joints live in the mirrored (-x,-y,z) frame
                q = q * np.array([-1, -1, 1, 1])
                if os.environ.get("SH2_SETCONJ") == "1":
                    q = q * np.array([-1, -1, -1, 1])
            if os.environ.get("SH2_ROOTCONJ", "0") == "1" and i == r1:      # placement yaw: engine uses row vectors
                q = q * np.array([-1, -1, -1, 1])
            if yaw180 and i == jr:
                q = yawq(q)
            for k in range(1, len(q)):
                if np.dot(q[k - 1], q[k]) < 0:
                    q[k] = -q[k]
            q /= np.maximum(np.linalg.norm(q, axis=1, keepdims=True), 1e-9)
            samplers.append({"input": G.acc(((np.array(fr) - t0) / fps).astype(np.float32), FLOAT, "SCALAR", None, True),
                             "output": G.acc(q.astype(np.float32), FLOAT, "VEC4"), "interpolation": "LINEAR"})
            channels.append({"sampler": len(samplers) - 1, "target": {"node": i, "path": "rotation"}})
        for i, keys in stream["pos"].items():
            if i >= nj or not keys:
                continue
            fr = sorted(keys)
            pv = np.array([keys[f] for f in fr])
            if np.abs(pv).max() == 0:
                continue
            par = joints[i].parent
            base = bpf[i] - (bpf[par] if par != 255 and par < nj else 0)
            tv = base + pv * TS
            samplers.append({"input": G.acc(((np.array(fr) - t0) / fps).astype(np.float32), FLOAT, "SCALAR", None, True),
                             "output": G.acc(tv.astype(np.float32), FLOAT, "VEC3"), "interpolation": "LINEAR"})
            channels.append({"sampler": len(samplers) - 1, "target": {"node": i, "path": "translation"}})
        sets = stream.get("morph_sets", {"mouth": 0, "eyelid": 1})       # SH3: eyebrow units are set 5
        div = stream.get("morph_div", 127.0)                             # SH3 weights run 0..255
        for node, kind, targets in face_nodes:
            s = sets.get(kind)
            keys = stream["morph"].get(s) if s is not None else None
            if not keys:
                continue
            fr = sorted(keys)
            w = np.zeros((len(fr), len(targets)), np.float32)
            for r, f in enumerate(fr):
                v = np.minimum(keys[f] / div, 1.0)
                w[r, :min(len(v), len(targets))] = v[:len(targets)]
            samplers.append({"input": G.acc(((np.array(fr) - t0) / fps).astype(np.float32), FLOAT, "SCALAR", None, True),
                             "output": G.acc(w.reshape(-1), FLOAT, "SCALAR"), "interpolation": "STEP"})   # stream stores state changes: hold until the next sample
            channels.append({"sampler": len(samplers) - 1, "target": {"node": node, "path": "weights"}})
        if channels:
            anims.append({"name": stream.get("name", "cutscene"), "samplers": samplers, "channels": channels})
    if anims:
        gltf["animations"] = anims
    G.save(out_path, gltf)
    return len(prims), len(anims)


if __name__ == "__main__":
    import glob
    prefix, out = sys.argv[1], sys.argv[2]            # prefix like ...\000.set  (without .NN.ext)
    mdl = prefix + ".00.mdl"
    anms = sorted(glob.glob(prefix + ".*.anm"))
    print(build(mdl, anms, out), "(meshes, animations) ->", out)



