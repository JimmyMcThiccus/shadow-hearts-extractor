# Shadow Hearts: Covenant (SLUS-210.41) – file format notes

Tools live in `tools/`. Output dirs: `Extracted/` (leaf files from the PKBs), `Unpacked/` (nested packages split).

    python tools\pkb_extract.py "ISO Extracted" Extracted
    python tools\pkg_unpack.py Extracted Unpacked SET,BCM,ANM,BIN,DAT,MMS,MMD,MED

## PKFINFO.TBL  (SOLVED)  – decoded from FUN_00228880 / FUN_0022a360 in the ELF
- 16-byte header, then 16-byte entries (4479 of them; 42 dirs + 4437 leaves).
- Entry: `char name[8]` packed base-40 (charset `.0123456789A-Z_`, 0 = end, 12 chars max),
  `u32 off` (**units of 16 bytes**, relative to parent dir), `u32 v`.
- `v` bit31 set = directory, `(v&0x7fffffff)/16` = index of its first child, relative to the
  parent's block start. Bit31 clear = leaf, `v` = size in bytes.
- Entries 0..13 are top level: FIELD.BIN, BATTLE.BIN, OPENING.BIN (plain files) and 11 PKBs.
- Blocks are depth-first: a dir's block ends where its first sub-dir's block starts.
- Verified: zero overlaps, every PKB fully covered.

## Generic package  (SOLVED)
`u32 count, u32 total_size, count * { char ext[4] ("mdl\0"), u32 offset }`, entries are contiguous.
Used by SET, BCM, ANM, BIN, DAT, MMS, MMD, MED. Inner types: mdl anm tex sdw dse wse mse cse ase bbx ft1-3 eff efd msg fnt ...

## Top-level file types (by extension in the table)
| ext | magic / structure | what it is |
|-----|-------------------|-----------|
| SET | package (mdl, anm, tex) | field character model sets (FIELD/MODEL) |
| BCM | package (mdl, sdw, dse, mse, ase, bbx, ft1-3, anm) | battle characters (BTLDATA/CHR) |
| ANM | package of `anm` | animation banks (BTLDATA/CHRANM) |
| MPS | `SBD 0.11` | field maps (FIELD/MPS), paired with .MSC scripts |
| EFD | `EFMG` | effects (EFFECT.PKB) |
| SSE, SSQ | `SDRH` | sound banks / music sequences (not decoded yet) |
| SSV | `VS\x02\x00` | voice/stream audio (not decoded yet) |
| LSD | `LSDH`, `LSDC`, chunks `MPS `, `ANMS`, `CTRL`, `MDLS` | in-engine cutscene scene data, NOT movies |
| TEX | custom header, GS images | textures (not decoded yet) |
| FIELD/BATTLE/OPENING.BIN | `MWo3` | code overlays loaded at 0x45ea80, not yet in Ghidra |

## SSH1.01a model (.mdl)  (SOLVED for characters; verified in a glTF viewer)
Header 0x40 bytes: `"SSH1.01a"`, u32 offsets: +0x0c meshes, +0x10 skeleton, +0x18 textures (-1 = none),
+0x24 texture-animation section (`TXAH`/`TXAD`, e.g. eye textures, not decoded), +0x28 `EXHD` chunk list.

**Skeleton** @+0x10: `FRHD`, u16 joint count, u16 extra; 0x40-byte joints:
name[12], u8 parent/child/?/sibling (0xff = none), +0x10 vec4 (0), **+0x20 vec4 = bind-pose world position**,
+0x30 quaternion (-1,0,0,~0) = 180 deg about X (the animation frame, see below).
Bone ids >= joint count are **virtual blend bones** from the `WFRH` EXHD chunk: 16-byte records
`u8 ?, u8 nbones, u16 0, u8 bone[4], u16 weight[4] (12-bit, 4096 = 1.0)`; bone id = joint_count + record index.

**Meshes** @+0x0c: `ATHD`, u16 count, entries 16-byte aligned. Entry:
`+0x00 name[12]; +0x0c u32 n; +0x10 5c 00 <tex index> <kind>; +0x14 u32 flags; +0x20 u32 1`, data at +0x30.
- `kind == 0xff`: skinned. Parallel arrays, each padded to 16 bytes: pos `n*16` (f32 xyz, u16 flags
  [bit15 = GS strip ADC "no kick", bits0-1 = which of the group's 4 bones], u16 id), uv `n*4` (u16 s,t, 12.4 fixed texels),
  normals `n*8` (s16 xyz/32767, w).
- `kind != 0xff`: rigid mesh attached to joint `kind`: s16 xyz+flags (8 B), uv (4 B) if flags&0x40, RGBA8 if flags&0x20.
  Scale unknown (assumed 1/16). A few variants (billboards) have a different count field, not decoded.
- Footer: `u32 g` + 12 zero bytes, then g records `u8 bone[4], u32 flags, u32 first_corner, u32 corner_count`;
  the groups tile [0,n). Triangles = GS triangle strips over each group (3-vertex window, ADC suppresses a kick).
- Header byte +0x12 = texture index in the model's `TXHD` section.
- EXHD chunks: `EXHD`, tag, u16 version, u16 count, u32 size in 16-byte units. `WFRH` blend bones, `DYNH` belt/hair
  physics, `LABL`, `DHSH` (not decoded).

**Textures** `TXHD`: u32 count, then `TIHD` entries: `u16 w, u16 h, u32 bpp(4/8), u32 (hdr_bytes<<16 | ncolours),
u32 header+palette size, u32 entry size`; palette (RGBA8, alpha 0x80 = opaque) at `+hdr_bytes`, pixels at `+header+palette size`.
8-bit palettes use the PS2 CSM1 swizzle (swap index bits 3 and 4). 4-bit: low nibble first.

**Coordinate frames** (verified: planted-foot speed, knee bend, gait symmetry, and the toe-bound shoe vertices lining up
with the toe joint): mesh space -> joint space = **(-x, -y, -z)** (point inversion; an earlier (-x,-y,z) guess tore the shoes).
Animation values live in a frame rotated 180 deg about X relative to joint space (q -> (x,-y,-z,w), translation -> (x,-y,-z)).
Engine: joint matrix = T(bind pos) * R_anim * R_bind with R_bind = quaternion (-1,0,0,0) (FUN_002404e0).
glTF export mirrors X: mesh (x,-y,-z), bind pos (-x,y,z), anim rotation unchanged, anim translation (-x,-y,-z).
Foot-planted check: stance foot speed 0.09 vs root speed 0.6 (the alternative rotation convention slides at 1.2).

**Hands**: the game does not rig fingers. `*_hand_A/B/C` are alternate whole-hand meshes (open / fist / ...);
header bytes +0x17 (0x10 = L, 0x20 = R, low nibble = variant). Only variant A is visible in the exports; B/C are
separate nodes flagged `extras.hidden` (+ `KHR_node_visibility`).

**glTF notes**: never use alphaMode BLEND (three.js disables depth writes -> inside faces show through); accessor
`min`/`max` must be arrays even for SCALAR (Blender rejects bare numbers).

## Animation sequence (.anm)  (SOLVED, from FUN_00242840 / FUN_0023fa10 / FUN_0023a250)
`SQHD` + name[12]; +0x10 u16 frame count, +0x12 u16 node count, +0x15 u8 fps, +0x16 u16 aux-track count;
nodes start at +0x20. Node: name[12], +0x10 u16 rot key count, +0x12 u16 pos key count, data at +0x20:
rot times `u16[n]` (pad 16), rot values `n*8` (s16 x,y,z,w / 4096, slerp, pad 16),
pos times `u16[n]` (pad 16), pos values `n*8`: per axis `(s16 << 5) + 5 low bits of the 4th short`,
times 0.01 (or unscaled if bit15 of the 4th short is set). Aux tracks (events / other channels) follow, not decoded.
Rotations are local, rest orientation is identity; translation is an additive offset to the bind-local offset.

## Tools
- `tools/pkb_extract.py`, `tools/pkg_unpack.py`: archive layer.
- `tools/ssh_model.py`: mdl / WFRH / sequence parsers, FK + skinning, software preview renderer.
- `tools/ssh_gltf.py`: one model + animations -> .glb. `tools/export_all.py`: batch (677 models in `export/models`).
- `tools/lsd_extract.py` (cutscene models out of LSD scenes), `tools/lsd_demux.py` (strips the interleaved stream), `tools/export_cutscene.py` (-> `export/cutscene/<scene>/*.glb`).
- `export/viewer.html` + `.claude/launch.json` (`sh2-viewer`): three.js viewer for the exported models.
- `tools/blender_import.py` (import + hide alternate hands), `tools/blender_render.py` (headless Blender render, used for verification; Blender 5.2).

## LSD cutscene scenes  (partly decoded)
`N###.LSD` / `G##.LSD` are in-engine cutscenes (`OPI_E00`/`OPI_J00` are short video-only movies, see below); `M##.LSD` and `MOVIE_A-D.LSD` hold no models (FMV/stream data).
- Header `LSDH`: u16 version 4, u16 directory entries, u16 fps (30), u32 frame count. Directory `LSDC` from 0x40; records seen:
  `MDLS` model instance (first/last frame, size in 16-byte units at +0x18, name at +0x20), `ANMS` animation set
  (name at +0x20, +0x10 u16 count, joint-name rows from +0x40 until the first row with data after the name),
  `CAMS` camera, `LGTS` lights, `LCDF`, `BLINK`, `LIPS` lip-sync, `PCMS` audio, `LSCR` script, `EVNT` events, plus per-bone
  `DYNA`/`BREAST`/`SKIRT`-style records. Names are the cast: `Uru`=Yuri, `Kalen`=Karin, `Geppet`, `Luchia`, `Yoahim`, `Roger`,
  `Anastasia`, `Nicol`, `Haimman`, `Yowis`, suffix `_m` body, `_h` detailed head (up to 88 joints).
- After the directory: embedded `SSH1.01a` models (each with its own `TXHD` textures), `SBD 0.11` maps and `SQHD` sequences.
  2,058 models in 90 scenes; `tools/lsd_extract.py` writes them to `LSD_models/<scene>/NN_<name>.mdl` (named by matching the
  model's joint names to the `ANMS` joint lists).
- **The file is a multiplexed stream (SOLVED, `tools/lsd_demux.py`).** After the models start loading, the engine streams the
  resource data (models, textures, maps) in ~105 KB periods, and in each period it inserts the animation / voice data for the next
  3 frames (0.1 s). Those chunks land in the middle of model arrays at arbitrary offsets. Grammar (16-byte rows):
  - **control chunk** (animation): optional row0 `u32 0, u32 0xc0, u32 0, 'CTRL'`; row1 `u32 type, u32 seq, u32 w, 0`;
    row2 `u32 first_frame (multiple of 3), u32 3, u16 nrec, u16 w, 0`; then `w` rows (so the chunk is `16*(w+1)` bytes from row1).
    `type` = model slot (animated model). Records: `u8 id, u8 kind, 0,0, u32 count`, then `(kind&15)` 8-byte items:
    kind 1-3 = rotation keys (s16 x,y,z,w / 4096, one per frame), kind 0x21.. = translation keys, 0x41/0xc1 = u16 index + 2 units
    (event/facial flags), 0x80 = `count` items. Body rigs (types 4,5,11) have ~35 bones; **face rigs (types 37,39,45,47,60 ...) have
    66-76 bones, one chunk per 3 frames = facial animation for the detailed `_h` head models.**
  - **voice audio**: header row `[id][counter][audio_rows][0]` (the id word varies per scene, 0x3d row0 optional), then
    `audio_rows` rows of SPU-ADPCM (flag byte 2), then one pad row; an optional descriptor row `[type][n][0x1000-0x1fff][0]`
    and a marker row (`.. 0c 02 00 NN`) can precede/follow.
  - de-multiplexing makes 1,382 of 1,490 skinned cutscene models clean (was 672).
- CTRL tags with `01 00 06 00 ..` (u16 1, u16 6 + list of `01 00 idx 00` entries) are a different, directory-like variant
  (not yet decoded, not removed).

## Known gaps
- ~100 cutscene models still have unremoved stream data (other chunk variants), 47 are partial.
- Cutscene animation: the control chunks are parsed for framing only; their rotation/translation keys are not yet turned into
  animations (type -> model slot mapping, camera chunks, lip-sync, event records, audio extraction still to do).
- Rigid-only models (battle stage props `BTL_001.bin`, weapons `WP*.bin`, effects) are skipped (204 mdl).
- Dynamic hair/belt physics (DYNH), face/eye texture animation (TXAD), aux animation tracks, morph targets.
- MPS field maps (`SBD 0.11`), EFD effects, audio (SDRH/SSV), standalone TEX files not decoded yet.

## BSAH blend-shape block (hi-poly face)

Cutscene `_h` heads keep the face skin **outside** the normal mesh list. Header +0x20 points at a `BSAH` chunk
(`BSAH`, u16 6, u16 1, ...) that holds morph sets: `mouth` (tongue `SITA`, teeth `HA`, lower face), `eyelid`
(`..._O3/_M3/_C3/_L3` frames, `matuge` = lashes), `eyeblow` (`..._U3/_D3`). Each set is a normal mesh entry
(name usually `N` = neutral) with `5c 00 <tex> ff`, but a different data layout at +0x30:

    [u32 frame offsets, padded to 16 B, 1 per frame (base + morph targets)]
    [uv  u16x2 * n, padded to 16 B]
    [16-byte row]
    [pos f32x3 + u16 flags + u16 id  * n]   (same as skinned meshes)
    [normals s16x4 * n, padded]
    [delta streams for the morph frames]
    [footer: strip groups, as usual]

`ssh_model.parse_mdl(b, bsah=True)` returns these as extra meshes (`Mesh.bsah = True`, `frame_offsets`) and the
glTF exporter now includes the neutral shape, so Yuri's `Uru_h` / `m31` get forehead, eye, nose-bridge and
mouth plates (they use texture 0 like the eyeballs). Morph deltas (blink / lip-sync visemes) and `matuge`
(footer not found) are not decoded yet.

## Face morph targets and cutscene animation (decoded from the LSD stream)

**BSAH morph frames.** After a BSAH mesh's base arrays come `k-1` morph frames (k = number of non-zero entries of the
frame-offset table): `[name 16 B][pos f32x3+u16+u16 * n][nrm s16x4 * n]`, each padded to 16 B. Positions are absolute
(in mesh space, subtract the base to get the delta). Sets: mouth = A I U E O H L, eyelid = O M C L, eyebrow = U D.
`export` writes one glTF mesh per set (`face_mouth_*`, `face_eyelid_*`, `face_eyebrow_*`) with morph targets.

**Control chunks -> animation** (`tools/lsd_anim.py`). Chunk `type` is the model slot (usually `2*instance-1`; the
exporter matches by frame range + joint-id range instead). Per record `[id][kind][0 0][count]`:

| kind | meaning |
|---|---|
| 1..3 | rotation keys of joint `id`, s16x4 / 4096 (x,y,z,w); `count` = bit mask of the 3 frames that carry a key |
| 0x21..0x23 | translation keys (same encoding as SQHD) |
| 0x41..0x43, 0xc1..0xc3 (id 1) | face morph units, 8 B each: `[set][n][6 weights, 127 = 1.0]`. set 0/n 7: bytes 2..6 = A I U E O; set 1/n 4: bytes 2..5 = O M C L. 0xcN = the later sample of the slice |
| 0x80 (types 7/8/61 etc.) | camera: units of 8 B, see below |

Weights: the exporter assumes bytes map to targets in order and that 255 means fully closed (clamped to 1).
The `BLINK` flag in the LSD directory probably means the engine adds procedural blinks; not emulated.

**Camera** (`tools/lsd_camera.py`, raw JSON only): per frame `[s16x4 rotation/angles][f32 x, f32 y][f32 z, s16 a, s16 b]`
(a,b about 0x20c/0x200 = FOV/roll?). Scale and axes are not verified.

**Audio** (`tools/lsd_audio.py`): the audio sub-chunks of a scene concatenated = SPU-ADPCM, 48 kHz stereo, channel
blocks interleaved every 0x800 bytes (checked by ADPCM continuity at block boundaries and L/R correlation 0.9).
Written to `export/audio/<scene>.wav` (full scene length, so it lines up with frame/30 s).


## Camera (corrected, verified against the engine)

Supersedes the earlier "Camera" paragraph. Decoded by `tools/lsd_camera.py`; matches the engine camera update
(FUN_003c9fc0 in FIELD.BIN) and live PCSX2 memory (scene N001: rotation rows and position equal the stream key of the same
frame, max error 0.001). Camera chunks are kind-0x80 records of one slot per scene (auto-detected). `id` = number of key
items, `count` = frame mask. Key = `[s16 x,y,z /4096, s16 w /32768][f32 x,y,z][s16 focal][s16 flags]` (24 B in the
engine); the stream stores the rotation unit always and the 16-byte position+focal+flags part only when it changes.
focal px = value/512*682 (448-line screen); flags bit 8 = hard cut (hold). The engine builds a row-vector matrix
R(q) (rows = camera axes in world space), camera looks along local +Z, local +Y points down on screen. Engine world ->
Blender: (x,y,z) -> (-x,-z,-y) (chosen empirically: camera upright in 99% of frames, aims at the characters).


## LSD movie files (MOVIE_A-D.LSD, M##.LSD): MPEG-2 FMV  (`tools/lsd_movie.py`)

Same multiplexed `CTRL` chunk stream as the cutscene LSDs, but the payload is video, not models. After the `LSDH`/`LSDC`
directory (`MOVS` video, `PCMS` 48 kHz stereo audio, `DLBY`, `LSCR`, `EVNT`) the stream starts at the first `CTRL` row
(0x280 in MOVIE_D; the walk starts there and skips every `CTRL` row0). Chunk = `row1 [u32 type, u32 seq, u32 nrows, 0]` +
`nrows*16` payload bytes. One slice (`seq`) = 3 frames (0.1 s @ 30 fps), made of:

| type | contents |
|---|---|
| 0 | slice header (1 row; 5 rows on the first slice; frame counter) |
| video slot | **MPEG-2 elementary stream bytes**, concatenated in order = a valid `.m2v` (512x448, `00 00 01 B3` sequence header + `B5` extensions, GOP/I-P-B) |
| stereo slot | SPU-ADPCM, 48 kHz, 2 ch, L/R blocks alternate every 0x800 bytes (same as `lsd_audio.py`); length per channel = `PCMS` record u32 at +0x28 |
| surround slot | **5.1 Dolby Digital** (`DLBY` record; only the MOVIE_A-D files have it, M##.LSD are stereo only): IEC 61937 data bursts, one every 0x800 bytes (preamble `72 F8 1F 4E`, Pc=1 = AC-3, Pd = bits) holding one 1536-byte AC-3 frame (384 kbps) with each 16-bit word byte-swapped; swap back and concatenate = `.ac3` |

The video slot number is not in the directory and varies per file (1, 2 or 3), so the tool picks the type of the first chunk
whose payload starts with `00 00 01 B3`. The stream is video-only for the first N slices; the last few slices are audio only
(MOVIE_D: video ends at slice 185 of 202, ~558 frames, LSDH says 600). Roles are told apart by content (video = payload starts `00 00 01 B3`; surround = starts with the burst preamble; remaining = stereo). Output: `export/movies/<name>.m2v`, `.wav`, `.ac3`. All video and AC-3 files decode error-free in ffmpeg; the stereo WAV matches an AC-3 stereo downmix (corr ~0.68 at the 869-sample decoder delay).
Picture counts check out (e.g. M05: 720 pictures, 40 GOPs, 24 s; MOVIE_A: 12,607 pictures = 7:00).


Disk 2 (disk2extracted/, only packed as PKBs): 	ools/lsd_pkb_batch.py disk2extracted disk2 streams each LSD out of LSD.PKB (via PKFINFO.TBL) and runs lsd_movie (files with a MOVS record) or lsd_extract (the rest) -> disk2/movies/, disk2/LSD_models/<scene>/. Disk 2 movies: BAD_END, GOOD_END (stereo only), GY_MOV, M16, MOVIE_E/F/G (with 5.1 AC-3), OPI_J00 (video only; OPI_E00 on disk 1 is the same). 66 cutscene scenes (G01-G07, M17, N077-N130) -> 1,625 models.

Disk 2 cutscene export: `SH2_ROOT=disk2` makes export_cutscene.py / build_scene_package.py use disk2/Extracted/LSD (unpacked from LSD.PKB), disk2/LSD_models and write disk2/export/{cutscene,audio,scenes}. Import `disk2/export/scenes/<scene>.json` with the Blender add-on.

**Parser fix (ssh_model.is_entry):** a 12-character mesh name has no NUL terminator (e.g. `alice_HI_eye`, the face skin of Alice's _h head); the old name regex required a NUL, so such entries were skipped and the neighbouring meshes reported 'footer not found'. Models re-exported since this fix: disk2 only (disk 1 export/ was not regenerated).
