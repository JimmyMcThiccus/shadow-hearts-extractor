"""Decoder for Shadow Hearts 3 (From the New World) 'cx' compression (new vs SH2).

Reverse-engineered from SLUS_213.26 (FUN_002dfbf0 = header size, FUN_002dfca0 = decoder init,
FUN_002e0190 = block decode loop; Ghidra MCP, 2026-10-16).

Header (little-endian):
  +0x00 u16 magic ("cx\0" = 0x7863; 0x5843 "Xc" = already-raw marker, size unknown -> -1)
  +0x02 u8  flags: bit5 (0x20) = extended header (header=0x10 instead of 8)
                   bits2-4 ((flags>>2)&7) = huffman-channel bitmask: bit0 literal/length table, bit1 offset table
  +0x03 u8  (unidentified / unused by the decoder in the no-channel case)
  +0x04 u32 decompressed size
  [if channel bitmask != 0: u16 table-section size follows at the (8 or 0x10) header boundary, XORed 0x1234
   when actually consumed; the huffman tables occupy that many bytes, then the bitstream follows]
  else: bitstream starts immediately at the header boundary (8 or 0x10 bytes in)

Stored chunks (flag bits 3-6 clear) are XOR-scrambled, see stored(); checked byte-exact against live RAM.

Bitstream (LSB-first bit packing, byte-at-a-time refill), channel bitmask == 0 case (plain fixed-width LZ,
every sample file we checked in SH3 uses this -- no huffman channels):
  read 1 bit:
    1 -> literal: read 8 bits, emit as byte
    0 -> match:   read 8 bits  -> length  = value + 3   (3..258)
                  read 14 bits -> distance = value + 1   (1..16384), copy `length` bytes from
                                   (output_pos - distance), one byte at a time (self-overlapping copy allowed)
"""
import struct


class BitReader:
    __slots__ = ("data", "pos", "acc", "nbits")

    def __init__(self, data: bytes, start: int):
        self.data = data
        self.pos = start
        self.acc = 0
        self.nbits = 0

    def _fill(self, n):
        while self.nbits < n:
            self.acc |= self.data[self.pos] << self.nbits
            self.pos += 1
            self.nbits += 8

    def read(self, n):
        self._fill(n)
        v = self.acc & ((1 << n) - 1)
        self.acc >>= n
        self.nbits -= n
        return v


def header_info(data: bytes):
    magic = struct.unpack_from("<H", data, 0)[0]
    if magic == 0x5843:
        return None  # already raw
    if magic != 0x7863:
        return None
    flags = data[2]
    header_size = 0x10 if (flags & 0x20) else 8
    chan_mask = (flags >> 2) & 7
    decomp_size = struct.unpack_from("<I", data, 4)[0]
    table_size = 0
    if chan_mask != 0:
        raw = struct.unpack_from("<H", data, header_size)[0]
        table_size = raw ^ 0x1234
        header_size += table_size + 2
    return {"flags": flags, "chan_mask": chan_mask, "decomp_size": decomp_size, "payload_off": header_size}


_KEY = bytes((((i + 1) * 11) & 0xFF) ^ 0xDD for i in range(0x10000))


def stored(chunk: bytes) -> bytes:
    """Stored chunks are scrambled: FUN_002dfab0 XORs byte i with ((i + 1) * 11 & 0xff) ^ 0xdd (period 256)."""
    n = len(chunk)
    return (int.from_bytes(chunk, "little") ^ int.from_bytes(_KEY[:n], "little")).to_bytes(n, "little")


def decompress(data: bytes) -> bytes:
    """The compressed payload is itself split into chunks, each with a 6-byte sub-header
    (FUN_002dfdd0 / FUN_002e0190): +0 u8 flags (bits3-6 nonzero -> bit-packed LZ, else stored, scrambled -- see stored()),
    +2 u16 payload_len-1 (bitstream byte length for this chunk), +4 u16 output_len-1 (decompressed
    bytes this chunk produces). Next chunk header = this header + 6 + payload_len."""
    info = header_info(data)
    if info is None:
        raise ValueError("not a cx-compressed blob")
    if info["chan_mask"] != 0:
        raise NotImplementedError("huffman-channel variant not implemented (chan_mask=%d)" % info["chan_mask"])
    out = bytearray()
    want = info["decomp_size"]
    pos = info["payload_off"]
    while len(out) < want:
        flags = data[pos]
        payload_len = struct.unpack_from("<H", data, pos + 2)[0] + 1
        out_len = struct.unpack_from("<H", data, pos + 4)[0] + 1
        payload_start = pos + 6
        if (flags & 0x7f) >> 3 == 0:
            out += stored(data[payload_start:payload_start + out_len])
        else:
            br = BitReader(data, payload_start)
            produced = 0
            while produced < out_len:
                ctrl = br.read(1)
                if ctrl:
                    out.append(br.read(8))
                    produced += 1
                else:
                    length = br.read(8) + 3
                    dist = br.read(14) + 1
                    src = len(out) - dist
                    if src < 0:
                        raise ValueError("bad back-reference: dist=%d pos=%d" % (dist, len(out)))
                    for _ in range(length):
                        out.append(out[src])
                        src += 1
                    produced += length
        pos = payload_start + payload_len
    return bytes(out[:want])


if __name__ == "__main__":
    import sys
    data = open(sys.argv[1], "rb").read()
    out = decompress(data)
    open(sys.argv[2], "wb").write(out)
    print(f"{sys.argv[1]}: {len(data)} -> {len(out)} bytes -> {sys.argv[2]}")
