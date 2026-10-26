"""Extract the voice / sound track of an LSD cutscene to a WAV file.

The audio sub-chunks found by lsd_demux.find_audio are concatenated into one SPU-ADPCM (VAG) stream.  The PCMS directory
record says 48000 Hz, 2 channels, interleave 0x800 bytes (verified by block-boundary continuity) (channel blocks alternate L R L R ...).

usage: python lsd_audio.py <file.LSD> <out.wav>
"""
import os
import struct
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lsd_demux  # noqa: E402

COEF = np.array([(0, 0), (60, 0), (115, -52), (98, -55), (122, -60)], np.float64) / 64.0


def vag_decode(data):
    """SPU-ADPCM bytes (16-byte blocks) -> int16 samples."""
    n = len(data) // 16
    B = np.frombuffer(data[:n * 16], np.uint8).reshape(n, 16)
    shift = (B[:, 0] & 15).astype(np.int64)
    pred = np.minimum(B[:, 0] >> 4, 4)
    nib = np.empty((n, 28), np.int64)
    nib[:, 0::2] = B[:, 2:] & 15
    nib[:, 1::2] = B[:, 2:] >> 4
    nib = np.where(nib >= 8, nib - 16, nib)
    out = np.zeros(n * 28, np.float64)
    h1 = h2 = 0.0
    for b in range(n):
        c1, c2 = COEF[pred[b]]
        s = nib[b] * (1 << (12 - shift[b])) if shift[b] <= 12 else nib[b] >> (shift[b] - 12)
        blk = np.empty(28)
        for i in range(28):
            v = s[i] + c1 * h1 + c2 * h2
            v = max(-32768.0, min(32767.0, v))
            blk[i] = v
            h2, h1 = h1, v
        out[b * 28:b * 28 + 28] = blk
    return out.astype(np.int16)


def extract(L, rate=48000, channels=2, interleave=0x800):
    parts = []
    for s, e, rows in lsd_demux.find_audio(L):
        # payload = the `rows` ADPCM rows right before the pad row / trailer
        hdr = 64 if (s + 64 <= len(L) and struct.unpack("<I", L[s:s + 4])[0] == 0x3D) else 16
        parts.append(L[s + hdr:s + hdr + 16 * rows])
    raw = b"".join(parts)
    if channels == 1:
        return vag_decode(raw), rate, 1
    nb = len(raw) // (2 * interleave) * 2 * interleave
    blocks = np.frombuffer(raw[:nb], np.uint8).reshape(-1, 2, interleave)
    left = vag_decode(blocks[:, 0, :].tobytes())
    right = vag_decode(blocks[:, 1, :].tobytes())
    return np.stack([left, right], 1), rate, 2


def write_wav(path, pcm, rate, ch):
    with wave.open(path, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.ascontiguousarray(pcm).tobytes())


if __name__ == "__main__":
    L = open(sys.argv[1], "rb").read()
    pcm, rate, ch = extract(L)
    write_wav(sys.argv[2], pcm, rate, ch)
    print("%d samples, %.1f s, peak %d" % (len(pcm), len(pcm) / rate, int(np.abs(pcm).max())))

