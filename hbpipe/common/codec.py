"""Lossless compact encoding of sample blocks.

Values are signed 24-bit integers; we store them as little-endian int32, row-major.
A block of n samples for a sensor with c channels is exactly 4*n*c bytes.
"""
from __future__ import annotations

import sys
from array import array
from itertools import chain

CHANNELS = {1: 3, 2: 2}
MAX_CHUNK = 400  # the mock never returns more than 1 s (400 samples) per reply

_LITTLE = sys.byteorder == "little"
assert array("i").itemsize == 4, "int32 array type required"


def pack(rows) -> bytes:
    a = array("i", chain.from_iterable(rows))
    if not _LITTLE:
        a.byteswap()
    return a.tobytes()


def unpack_flat(blob: bytes) -> array:
    a = array("i")
    a.frombytes(blob)
    if not _LITTLE:
        a.byteswap()
    return a


def unpack(blob: bytes, channels: int) -> list:
    a = unpack_flat(blob)
    return [tuple(a[i:i + channels]) for i in range(0, len(a), channels)]
