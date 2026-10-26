"""Extract video and audio from a movie LSD (MOVIE_A-D.LSD, M##.LSD).

Layout (see FORMATS.md, "LSD movie files"):
  LSDH/LSDC directory (0x00-0x28b), then a stream of chunks.  Each chunk is
      [optional row0: u32 0,u32 0xc0,u32 0,'CTRL']   (first chunk of a slice only)
      row1: u32 type, u32 seq, u32 nrows, u32 0
      nrows * 16 bytes of payload
  One slice (seq N) = 3 frames (0.1 s at 30 fps) with up to 4 chunk types.  Slot numbers vary per file, so the roles are
  found by content:
      type 0      slice header
      video       MPEG-2 elementary stream (first payload starts 00 00 01 B3)       -> <name>.m2v
      stereo      SPU-ADPCM, 48 kHz, 2 ch, L/R blocks interleaved every 0x800 (PCMS) -> <name>.wav
      surround    5.1 Dolby Digital (DLBY): IEC 61937 bursts (72 F8 1F 4E, Pc=1) every 0x800 bytes, each holding one
                  AC-3 frame with 16-bit-swapped bytes                               -> <name>.ac3
Usage: lsd_movie.py in.LSD [out_dir] [-a]   (-a also dumps the raw per-type streams)
"""
import os
import struct
import sys
import wave
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lsd_audio import vag_decode  # noqa: E402

BURST = b"\x72\xf8\x1f\x4e"          # IEC 61937 sync words Pa, Pb (little endian)


def walk(d, start=0x290):
    p = start
    n = len(d)
    while p + 16 <= n:
        if d[p + 12:p + 16] == b"CTRL":
            p += 16
            continue
        t, seq, w, z = struct.unpack_from("<4I", d, p)
        if z or t > 255 or p + 16 + w * 16 > n:
            return p
        yield t, seq, d[p + 16:p + 16 + w * 16]
        p += 16 + w * 16
    return p


def ac3_frames(raw):
    """IEC 61937 bursts -> raw AC-3 byte stream (bytes un-swapped)."""
    out = bytearray()
    p = raw.find(BURST)
    while p >= 0:
        pc, pd = struct.unpack_from("<HH", raw, p + 4)
        n = pd // 8                                   # Pd = payload length in bits
        if pc & 0x1F == 1 and n and p + 8 + n <= len(raw):
            f = bytearray(raw[p + 8:p + 8 + n + (n & 1)])
            f[0::2], f[1::2] = f[1::2], f[0::2]
            out += f[:n]
        p = raw.find(BURST, p + 8)
    return bytes(out)


def adpcm_stereo(raw, size, interleave=0x800):
    raw = raw[:size * 2] if size else raw
    nb = len(raw) // (2 * interleave) * 2 * interleave
    blocks = np.frombuffer(raw[:nb], np.uint8).reshape(-1, 2, interleave)
    return np.stack([vag_decode(blocks[:, 0, :].tobytes()), vag_decode(blocks[:, 1, :].tobytes())], 1)


def pcms_size(d):
    """Bytes per channel of the PCMS record (u32 at record +0x28)."""
    p = d.find(b"PCMS", 0x40, 0x280)
    return struct.unpack_from("<I", d, p + 0x28)[0] if p >= 0 else 0


def extract(path, out_dir, raw_dump=False):
    d = open(path, "rb").read()
    name = os.path.splitext(os.path.basename(path))[0]
    start = d.find(b"CTRL", 0x40) - 12     # the stream starts at the first 'CTRL' row0 after the directory
    streams = {}
    cnt = Counter()
    slices = set()
    vtype = None
    gen = walk(d, start)
    while True:
        try:
            t, seq, pl = next(gen)
        except StopIteration as e:
            end = e.value
            break
        cnt[t] += 1
        slices.add(seq)
        if vtype is None and pl[:4] == b"\x00\x00\x01\xb3":
            vtype = t
        if t:
            streams.setdefault(t, bytearray()).extend(pl)
    os.makedirs(out_dir, exist_ok=True)
    ac3t = next((t for t, b in streams.items() if t != vtype and BURST in bytes(b[:0x40])), None)
    adpt = next((t for t in streams if t not in (vtype, ac3t)), None)
    info = ["%d slices, types %s, walked 0x%x/0x%x" % (len(slices), dict(cnt), end, len(d))]
    if vtype is not None:
        open(os.path.join(out_dir, name + ".m2v"), "wb").write(streams[vtype])
        info.append("video type %d (%d B)" % (vtype, len(streams[vtype])))
    if adpt is not None:
        pcm = adpcm_stereo(bytes(streams[adpt]), pcms_size(d))
        with wave.open(os.path.join(out_dir, name + ".wav"), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(np.ascontiguousarray(pcm).tobytes())
        info.append("stereo type %d (%.1f s)" % (adpt, len(pcm) / 48000))
    if ac3t is not None:
        ac3 = ac3_frames(bytes(streams[ac3t]))
        open(os.path.join(out_dir, name + ".ac3"), "wb").write(ac3)
        info.append("ac3 type %d (%d B)" % (ac3t, len(ac3)))
    if raw_dump:
        for t, b in streams.items():
            open(os.path.join(out_dir, "%s.type%d" % (name, t)), "wb").write(b)
    print("%s: %s" % (name, "; ".join(info)))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "-a"]
    extract(args[0], args[1] if len(args) > 1 else "export/movies", "-a" in sys.argv)
