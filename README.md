# Shadow Hearts Extractor

Asset extraction tools for **Shadow Hearts: Covenant** (Shadow Hearts II, PS2) and **Shadow Hearts: From the New World**
(PS2), plus a **Blender add-on** that imports in-engine cutscenes with their models, animation, face morphs, camera and
voice track.

Both games run on the same Nautilus engine (PKFINFO.TBL/PKB archives, SSH model format, LSD cutscene streams), so the
model and glTF code is shared; From the New World adds a `cx` compression layer on top.

Everything here was reverse engineered from the game executables (Ghidra) and checked against the running game in
PCSX2. Format notes are in [FORMATS.md](FORMATS.md).

> **No game data is included.** You need your own copy of the game discs. Don't commit extracted assets to this repo.

## What it extracts

| | Covenant (SLUS-210.41; also JP Director's Cut) | From the New World (SLUS-213.26) |
|---|---|---|
| Archives (PKFINFO.TBL / *.PKB) | `pkb_extract.py`, `pkg_unpack.py` | `sh3/sh3_extract_all.py` (decompresses `cx`) |
| Models -> rigged, textured glTF | `export_all.py` | `sh3/sh3_export_models.py` |
| In-engine cutscenes -> Blender | `export_cutscene.py` + `lsd_audio.py` + `build_scene_package.py` | `sh3/sh3_cutscene.py` |
| Pre-rendered movies (MPEG-2 + audio) | `lsd_movie.py` / `lsd_pkb_batch.py` | `sh3/sh3_movies.py` |
| One-shot ISO -> cutscene models | `extract_game.py` | |

Models come out as `.glb` with skeleton, skin weights, textures, face morph targets and (where the game has them)
animations. The game swaps whole hand meshes instead of rigging fingers; alternate hand poses are separate objects
tagged with the custom property `hidden`.

## Requirements

- Python 3.9+ and NumPy (`pip install -r requirements.txt`)
- Blender 4.0 or newer for the add-on (tested on 5.2)
- Optional: an ffmpeg build to play the raw `.m2v` movie streams

## Quick start

All commands run from the repository root. Output folders (`Extracted/`, `Unpacked/`, `export/`, ...) are created next
to `tools/` and are git-ignored.

### Shadow Hearts: Covenant

Fastest route, straight from the disc image to deduplicated cutscene models:

```
python tools/extract_game.py "Shadow Hearts Covenant.iso" --only-models
```

Full pipeline, starting from the files on the disc (copy them to `ISO Extracted/` first, or let `extract_game.py`
dump them):

```
python tools/pkb_extract.py "ISO Extracted" Extracted
python tools/pkg_unpack.py Extracted Unpacked SET,BCM,ANM,BIN,DAT,MMS,MMD,MED
python tools/export_all.py                      # every field/battle model -> export/models/
python tools/lsd_pkb_batch.py "ISO Extracted" .  # cutscene models -> LSD_models/, movies -> movies/
python tools/export_cutscene.py                 # cutscene models + animation -> export/cutscene/
python tools/lsd_audio.py Extracted/LSD/N001.LSD export/audio/N001.wav
python tools/build_scene_package.py N001        # -> export/scenes/N001.json for the Blender add-on
```

Set `SH2_ROOT=disk2` to run the cutscene tools against a second disc's folders.

### Shadow Hearts: From the New World

```
python tools/sh3/sh3_extract_all.py "SH3 ISO Extracted" SH3/Extracted
python tools/sh3/sh3_export_models.py SH3/Extracted SH3/export/models
python tools/sh3/sh3_cutscene.py SH3/Extracted SH3/export        # cutscene/, scenes/, audio/
python tools/sh3/sh3_movies.py "SH3 ISO Extracted" SH3/export/movies
python tools/sh3/cx_decompress.py <in> <out>                     # single cx-compressed file
```

## Blender add-on

1. Edit > Preferences > Add-ons > Install from Disk > `blender_addon/sh2_cutscene.py`
2. File > Import > Shadow Hearts 2 Cutscene (.json) > pick `export/scenes/<scene>.json`

It works for cutscene packages from either game. See [blender_addon/README.md](blender_addon/README.md) for how the
camera, timeline and face morphs are mapped.

To bring a single exported model into Blender with the alternate hands hidden:

```
blender --python tools/blender_import.py -- path/to/model.glb
```

## Repository layout

```
tools/              Covenant extractors + shared model / glTF code (ssh_model.py, ssh_gltf.py)
tools/sh3/          From the New World: cx decompression, archives, models, cutscenes, movies
blender_addon/      cutscene importer add-on and headless test scripts
ghidra_scripts/     ImportMWo3Overlays.java: loads the FIELD/BATTLE/OPENING.BIN code overlays into Ghidra
FORMATS.md          file format notes
```

## Known gaps

- From the New World: one texture layout (UMI_03, 4-bit CSA sub-palettes) isn't decoded yet.
- Cutscene drop shadows and a few per-object properties (DYNA, BLINK, ACR, MCA, MCGT, PolColA) aren't reproduced.
- Sound banks and music sequences (SDRH) aren't converted by these tools.

## Legal

This project contains no copyrighted game data. Shadow Hearts is a trademark of its respective owners; this is an
unofficial fan project and isn't affiliated with or endorsed by them. Use it only with games you own.

Licensed under the GNU GPL v3; see [LICENSE.md](LICENSE.md).
