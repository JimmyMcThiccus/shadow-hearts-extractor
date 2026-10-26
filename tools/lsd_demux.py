"""De-multiplex an LSD cutscene file: split it into the 'clean' resource stream (models, textures, maps)
and the interleaved stream chunks (animation/control chunks + voice audio).

Stream chunks (reverse engineered, see FORMATS.md).  One time slice = 3 frames (0.1 s):

  voice audio   4 header rows (16 B each; row0 = u32 0x3d ..., row3 = u32 0x3f, u32 counter, u32 audio_rows, 0),
                audio_rows * 16 B of SPU-ADPCM, then one pad row
  control chunk (one per animated model; 'type' = model slot)
        row0 (only the first chunk of a burst)   u32 0, u32 0xc0, u32 0, 'CTRL'     <- ASCII tag at +12
        row1   u32 type, u32 seq, u32 w, u32 0
        row2   u32 first_frame (multiple of 3), u32 3, u16 nrec, u16 w, u32 0
        nrec records: u8 id, u8 kind, 0, 0, u32 count, then (kind & 15) * 8 bytes
                      kind 1..3  = rotation keys  (s16 x,y,z,w / 4096, one per frame),
                      kind 0x21.. = translation keys
        pad to 16 bytes
Control chunks are concatenated; audio sub-chunks sit between them.  Everything that is not one of these chunks is the
resource stream (models, textures, maps ...), which is what the engine's loader sees.
"""
import struct
import sys

import numpy as np


def parse_chunk_at(L, p):
    """Parse a control chunk whose row1 starts at p. Returns (chunk, end) or None."""
    if p + 48 > len(L):
        return None
    typ, seq, w, z = struct.unpack("<4I", L[p:p + 16])
    ff, nfr, nrec, w2, z2 = struct.unpack("<IIHHI", L[p + 16:p + 32])
    if z or z2 or not (1 <= typ <= 255) or nfr != 3 or ff % 3 or w2 != w or nrec > 400:
        return None
    end = p + 16 * (w + 1)          # w = number of 16-byte rows that follow the row1 header row
    if end > len(L):
        return None
    return {"type": typ, "seq": seq, "w": w, "first_frame": ff, "nrec": nrec, "start": p, "end": end}, end


def find_chunks(L):
    """All control chunks, found by scanning every 16-byte row for the header signature."""
    W = np.frombuffer(L[:len(L) // 16 * 16], dtype="<u4").reshape(-1, 4)
    r1, r2 = W[:-2], W[1:-1]
    cand = ((r1[:, 3] == 0) & (r1[:, 0] >= 1) & (r1[:, 0] <= 255) & (r2[:, 1] == 3) & (r2[:, 0] % 3 == 0) &
            ((r2[:, 2] >> 16) == r1[:, 2]) & ((r2[:, 2] & 0xFFFF) <= 400) & (r2[:, 3] == 0))
    chunks = []
    last_end = 0
    for k in np.nonzero(cand)[0]:
        p = int(k) * 16
        if p < last_end:
            continue
        r = parse_chunk_at(L, p)
        if r:
            chunks.append((r[0], r[1]))
            last_end = r[1]
    return chunks


def find_audio(L):
    """Voice audio sub-chunks: [r0 (u32 0x3d..) r1 r2 r3 (0x3f, ctr, rows, 0)] + rows*16 + pad row."""
    W = np.frombuffer(L[:len(L) // 16 * 16], dtype="<u4").reshape(-1, 4)
    hits = []
    R = np.frombuffer(L[:len(L) // 16 * 16], dtype=np.uint8).reshape(-1, 16)
    adpcm = (R[:, 1] == 2) & ((R[:, 0] >> 4) <= 4)           # SPU-ADPCM block: flag byte 2, predictor <= 4
    # header row (the last of the audio header): [id][counter][audio_rows][0]; the first word varies per scene
    cand = np.nonzero((W[:, 3] == 0) & (W[:, 2] >= 16) & (W[:, 2] <= 1024) & (W[:, 2] % 16 == 0) &
                      (W[:, 0] < 0x1000) & (W[:, 1] < 0x10000))[0]
    last_end = 0
    for j in cand:
        j = int(j)
        rows = int(W[j, 2])
        if j + 1 + rows > len(R) or not adpcm[j + 1]:
            continue
        if adpcm[j + 1:j + 1 + rows].mean() < 0.9:             # the audio run must really be ADPCM
            continue
        if j >= 3 and W[j - 3, 0] == 0x3D and W[j - 3, 3] == 0:
            s, hdr = (j - 3) * 16, 64                     # full 4-row header
        else:
            s, hdr = j * 16, 16                           # single-row header
        e = s + hdr + 16 * rows + 16
        if e + 16 <= len(L):                 # optional trailer row [type][counter][0x17xx][0]
            t0, t1, t2, t3 = struct.unpack("<4I", L[e:e + 16])
            if 0x1000 <= t2 < 0x2000 and t3 == 0 and t0 < 0x100:
                e += 16
        if s < last_end or e > len(L):
            continue
        hits.append((s, e, rows))
        last_end = e
    return hits


def demux(L):
    """Return (clean_bytes, chunks, audio, spans). spans = removed (start, end) ranges."""
    chunks = find_chunks(L)
    audio = find_audio(L)
    spans = []
    for c, e in chunks:
        s = c["start"]
        if s >= 16 and L[s - 4:s] == b"CTRL":     # first chunk of a burst also owns the 16-byte row0
            s -= 16
        spans.append((s, e))
    spans += [(s, e) for s, e, _ in audio]
    # stream seams: marker row (bytes 12..14 = 0c 02 00) + descriptor row [type<256][counter<64][0x17xx][0]
    W = np.frombuffer(L[:len(L) // 16 * 16], dtype="<u4").reshape(-1, 4)
    q = np.nonzero(((W[1:, 2] >= 0x1000) & (W[1:, 2] < 0x2000)) & (W[1:, 3] == 0) & (W[1:, 0] < 0x100) & (W[1:, 1] < 64))[0] + 1
    for k in q:
        if L[(k - 1) * 16 + 12:(k - 1) * 16 + 15] == b"\x0c\x02\x00":
            spans.append(((k - 1) * 16, (k + 1) * 16))      # marker row + descriptor row
        else:
            spans.append((k * 16, (k + 1) * 16))             # descriptor row alone
    # long resources are streamed in pieces, each introduced by a descriptor row [type][counter 1,2,3..][piece size][0] whose size
    # is outside the 0x17xx range above (N125: [1,1,32000] and [1,2,24327] sit inside the train-car meshes).  A lone row of this shape
    # is easily data, so only runs with consecutive counters 1..k (k >= 2) of one type count.
    q2 = np.nonzero((W[:, 3] == 0) & (W[:, 0] >= 1) & (W[:, 0] < 0x100) & (W[:, 1] >= 1) & (W[:, 1] < 64) &
                    (W[:, 2] >= 0x100) & (W[:, 2] < 0x10000))[0]
    by_type = {}
    for k in q2:
        by_type.setdefault(int(W[k, 0]), []).append(int(k))
    for rows in by_type.values():
        run = []
        for k in rows + [None]:
            if k is not None and int(W[k, 1]) == len(run) + 1:
                run.append(k)
                continue
            if len(run) >= 2:
                spans += [(r * 16, (r + 1) * 16) for r in run]
            run = [k] if k is not None and int(W[k, 1]) == 1 else []
    # a marker row (bytes 12..14 = 0c 02 00) directly in front of any removed span belongs to the stream as well
    spans = [((s - 16) if (s >= 16 and L[s - 4:s - 1] == b"\x0c\x02\x00") else s, e) for s, e in spans]
    spans.sort()
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    out = bytearray()
    pos = 0
    for s, e in merged:
        out += L[pos:s]
        pos = e
    out += L[pos:]
    return bytes(out), chunks, audio, merged


if __name__ == "__main__":
    L = open(sys.argv[1], "rb").read()
    clean, chunks, audio, spans = demux(L)
    print("file %d B -> clean %d B (removed %d B in %d spans): %d control chunks, %d audio sub-chunks" % (
        len(L), len(clean), len(L) - len(clean), len(spans), len(chunks), len(audio)))

