"""Dump SH3 FtNW pre-rendered movies (LSD scenes with a MOVS track) to raw <name>.m2v + <name>.wav.

LSD directory: records `u16 2, u16 rows, u16 track, u16 x, char tag[4], ...` (MOVS = MPEG-2 video, PCMS = SPU-ADPCM;
PCMS +0x24 = u16 channels, u16 rate).  Stream after the directory, in 3-frame slices:
  slice table : u32 slice, u32 n, u32 flags, u32 frame, then n * 16-byte event records   (byte 3 == 0)
  chunk       : u16 seq, 0, u8 track, u32 rows, u32 segment, 0, then rows * 16 bytes of payload
MOVS payloads concatenate into an MPEG-2 elementary stream; PCMS payloads are stereo ADPCM with L/R blocks of the size in
PCMS +8 (0x1000 in SH3; SH2 used 0x800 -- the wrong size splices the channels about 0.15 s apart, an audible echo).
The segment number splits a scene into parts (MOV_D3: the soundtrack runs on while the video stops); both
streams are written in file order.  Walking starts at the chunk header right before the MPEG-2 sequence header.

usage: python sh3_movies.py <SH3 'ISO Extracted' dir> <out dir> [name filter]
"""
import os
import struct
import sys
import wave
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # shared tools/ (ssh_model, ssh_gltf)
import sh3_pkb as pk  # noqa: E402
from lsd_audio import vag_decode  # noqa: E402


def tracks(d, end):
    """{tag: (track number, record offset)} for the directory records before `end`."""
    out = {}
    for tag in (b"MOVS", b"PCMS"):
        p = d.find(tag, 0x40, end)
        if p >= 8:
            out[tag] = (struct.unpack_from("<H", d, p - 4)[0], p)
    return out


def demux(d):
    v0 = d.find(b"\x00\x00\x01\xb3", 0x40, 0x400000)
    if v0 < 0:
        raise ValueError("no MPEG-2 sequence header")
    tr = tracks(d, v0)
    if b"MOVS" not in tr:
        raise ValueError("no MOVS record")
    vt = tr[b"MOVS"][0]
    at = tr.get(b"PCMS", (None,))[0]
    video, audio = [], []
    other = Counter()
    pos = v0 - 16
    while pos + 16 <= len(d):
        h = d[pos:pos + 16]
        if h[3] == 0:                                   # slice event table
            cnt = struct.unpack_from("<I", h, 4)[0]
            if cnt > 0x1000:
                break
            pos += 16 + 16 * cnt
            continue
        if h[2] or any(h[12:16]):
            break
        size = struct.unpack_from("<I", h, 4)[0] * 16
        body = d[pos + 16:pos + 16 + size]
        if h[3] == vt:
            video.append(body)
        elif h[3] == at:
            audio.append(body)
        else:
            other[h[3]] += 1
        pos += 16 + size
    rate, ch, il = 48000, 2, 0x1000
    if b"PCMS" in tr:
        p = tr[b"PCMS"][1]
        ch, rate = struct.unpack_from("<HH", d, p + 0x24)
        il = struct.unpack_from("<I", d, p + 8)[0] or 0x1000
    return b"".join(video), b"".join(audio), (rate, ch, il), other, pos


def write_wav(path, raw, rate, ch, interleave=0x1000):
    nb = len(raw) // (ch * interleave) * ch * interleave
    blocks = np.frombuffer(raw[:nb], np.uint8).reshape(-1, ch, interleave)
    pcm = np.stack([vag_decode(blocks[:, c, :].tobytes()) for c in range(ch)], 1).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return len(pcm) / rate


def main():
    root, out = sys.argv[1], sys.argv[2]
    flt = sys.argv[3].upper() if len(sys.argv) > 3 else ""
    os.makedirs(out, exist_ok=True)
    info = pk.PkfInfo(open(os.path.join(root, "PKFINFO.TBL"), "rb").read())
    files = []
    info.walk(lambda p, c, o, s: files.append((p, c, o, s)))
    with open(os.path.join(root, "LSD.PKB"), "rb") as f:
        for path, cont, off, size in files:
            if cont != "LSD" or (flt and flt not in path.upper()):
                continue
            f.seek(off)
            if b"MOVS" not in f.read(0x600):
                continue
            f.seek(off)
            d = f.read(size)
            name = os.path.splitext(os.path.basename(path))[0]
            try:
                vid, aud, (rate, ch, il), other, end = demux(d)
            except ValueError as e:
                print(f"{name:10s} skipped: {e}")
                continue
            open(os.path.join(out, name + ".m2v"), "wb").write(vid)
            secs = write_wav(os.path.join(out, name + ".wav"), aud, rate, ch, il) if aud else 0.0
            print(f"{name:10s} video {len(vid) / 1e6:7.1f} MB  audio {secs:6.1f}s @ {rate} Hz x{ch}  other tracks {len(other)}"
                  f"  parsed to {end:#x}/{len(d):#x}")


if __name__ == "__main__":
    main()
