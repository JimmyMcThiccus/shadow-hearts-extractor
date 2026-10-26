# Shadow Hearts 2 cutscene importer (Blender 4.0+, tested on 5.2)

1. Build the data once (from the project root):
   - `python tools/export_cutscene.py`           models + animation + face morphs -> export/cutscene/<scene>/
   - `python tools/lsd_audio.py <LSD> export/audio/<scene>.wav`   (or the batch that fills export/audio)
   - `python tools/build_scene_package.py [scene]`   -> export/scenes/<scene>.json
2. Blender: Edit > Preferences > Add-ons > Install from Disk > `blender_addon/sh2_cutscene.py`
3. File > Import > Shadow Hearts 2 Cutscene (.json) > pick `export/scenes/<scene>.json`

Models are placed on the timeline at their instance's first frame (30 fps, frame 0 = scene start), hidden outside their
range; face morphs are shape keys (A I U E O H L / eyelid / eyebrow); audio is a sequencer strip.

**Camera.** Decoded from the stream and checked against the running game (PCSX2, scene N001): the engine's camera
rotation matrix and position equal the decoded key for the same frame (max error 0.001). Each key = unit quaternion
(x,y,z /4096, w /32768), position (engine world), focal length (value/512*682 px on a 448-line screen) and a cut flag.
The engine builds a row-vector matrix, so cam->world is the transpose of the quaternion matrix; the camera looks along
local +Z with +Y pointing down. Engine world -> Blender is (x,y,z) -> (-x,-z,-y); this mapping is chosen so the
camera stays upright and aims at the characters (upright in 99% of sampled frames) - it is the one part that is not
read directly from game code. Camera chunk type differs per scene and is auto-detected.

`test_import.py` renders check frames headless; `collect_positions.py` dumps character positions.
