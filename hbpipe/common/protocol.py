"""biosignal-mock wire protocol: newline-delimited JSON over TCP.

The CRC is computed over the `data` text *exactly as sent*, so we locate the raw
substring in the received line instead of re-serialising the parsed JSON (which
would not reproduce the original spacing and would reject good replies).
"""
from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass, field
from typing import Optional

from .codec import CHANNELS

INT24_MIN, INT24_MAX = -(1 << 23), (1 << 23) - 1

# A JSON string, so we can blank strings out before counting brackets.
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
# The `"data"` key followed by its colon.
_DATA_KEY_RE = re.compile(r'"data"\s*:\s*')
# The data value: an array of flat arrays of numbers (no nested strings/objects).
_DATA_VALUE_RE = re.compile(r'\[\s*(?:\[[^\[\]"{}]*\]\s*(?:,\s*\[[^\[\]"{}]*\]\s*)*)?\]')


class ProtocolError(ValueError):
    """Line is not a usable reply (malformed, wrong shape)."""


class CorruptReply(ProtocolError):
    """Reply parsed but its crc32 does not match the data text."""

    def __init__(self, msg: str, sensor: Optional[int] = None, session_id: Optional[str] = None):
        super().__init__(msg)
        self.sensor = sensor
        self.session_id = session_id


@dataclass
class LostRange:
    from_index: int
    to_index: Optional[int]  # None = end unknown
    reason: str


@dataclass
class Reply:
    session_id: str
    sensor: int
    first_index: int
    count: int
    session_end: bool
    data: list
    lost: list = field(default_factory=list)


def raw_data_text(text: str) -> str:
    """Return the `data` array text exactly as it appears at the top level of `text`."""
    for m in _DATA_KEY_RE.finditer(text):
        prefix = _STRING_RE.sub('""', text[:m.start()])
        if prefix.count('"') % 2:
            continue  # the match sits inside a string
        depth = (prefix.count("{") + prefix.count("[")
                 - prefix.count("}") - prefix.count("]"))
        if depth != 1:
            continue  # a nested "data" key, not the reply's
        v = _DATA_VALUE_RE.match(text, m.end())
        if not v:
            raise ProtocolError("data value is not an array of arrays")
        return v.group(0)
    raise ProtocolError("no top-level data field")


def crc32_hex(text: str) -> str:
    return "%08x" % (zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF)


def _int(v, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ProtocolError(f"{name} is not an integer")
    return v


def parse_reply(line) -> Reply:
    """Parse and validate one reply line. Raises ProtocolError / CorruptReply."""
    if isinstance(line, (bytes, bytearray)):
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ProtocolError("not utf-8") from e
    else:
        text = line
    text = text.strip()
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise ProtocolError("not json") from e
    if not isinstance(obj, dict):
        raise ProtocolError("reply is not an object")
    if "error" in obj and "data" not in obj:
        raise ProtocolError("mock error: %r" % obj.get("error"))

    session_id = obj.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError("missing session_id")
    sensor = _int(obj.get("sensor"), "sensor")
    if sensor not in CHANNELS:
        raise ProtocolError("unknown sensor %r" % sensor)

    # Integrity first: anything after this point is trusted only if the CRC matches.
    crc = obj.get("crc32")
    raw = raw_data_text(text)
    try:
        crc_ok = isinstance(crc, str) and int(crc, 16) == int(crc32_hex(raw), 16)
    except ValueError:
        crc_ok = False
    if not crc_ok:
        raise CorruptReply("crc32 mismatch", sensor=sensor, session_id=session_id)

    first_index = _int(obj.get("first_index"), "first_index")
    count = _int(obj.get("count"), "count")
    data = obj.get("data")
    if first_index < 0 or count < 0 or not isinstance(data, list) or len(data) != count:
        raise CorruptReply("count/data mismatch", sensor=sensor, session_id=session_id)
    nch = CHANNELS[sensor]
    for row in data:
        if not isinstance(row, list) or len(row) != nch:
            raise CorruptReply("wrong channel count", sensor=sensor, session_id=session_id)
        for v in row:
            if isinstance(v, bool) or not isinstance(v, int) or not INT24_MIN <= v <= INT24_MAX:
                raise CorruptReply("value out of 24-bit range", sensor=sensor, session_id=session_id)

    lost = []
    for item in obj.get("lost") or []:
        if not isinstance(item, dict):
            raise ProtocolError("bad lost entry")
        frm = _int(item.get("from_index"), "lost.from_index")
        to = item.get("to_index")
        if to is not None:
            to = _int(to, "lost.to_index")
            if to < frm:
                raise ProtocolError("lost range reversed")
        reason = item.get("reason")
        lost.append(LostRange(frm, to, str(reason) if reason else "reported_by_mock"))

    return Reply(session_id=session_id, sensor=sensor, first_index=first_index, count=count,
                 session_end=bool(obj.get("session_end")), data=data, lost=lost)


def read_request(sensor: int, from_index: Optional[int] = None) -> bytes:
    msg = {"cmd": "read", "sensor": sensor}
    if from_index is not None:
        msg["from_index"] = from_index
    return (json.dumps(msg) + "\n").encode("ascii")
