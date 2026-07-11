"""Lossless protobuf wire-format transformer for editing path strings inside
binary blobs (e.g. Cursor's ``agentKv:blob:*`` records).

Why this exists: some chat data is stored as protobuf where string fields are
**length-prefixed** (``\\n=<61 bytes of path>``). A naive byte replacement that
changes the path length leaves the length varint — and every *enclosing*
message length — wrong, corrupting the blob. This module walks the wire format,
lets a callback rewrite leaf strings, and re-serialises bottom-up so all nested
length varints are recomputed.

Safety properties (verified against real data before trusting it):

* **Canonical / lossless:** transforming a buffer with a no-op callback returns
  byte-identical output. Cursor's encoder emits canonical varints, so this holds
  in practice and is asserted by the caller on a sample before any writes.
* **Leaf vs message disambiguation:** a length-delimited payload that contains
  the path marker is first *attempted* as a sub-message; if it doesn't parse, or
  parses but round-trips to different bytes without a real change (non-canonical
  / misparse), it is treated as a string/bytes leaf instead. Path strings begin
  with ``/`` (0x2f → wiretype 7, invalid) so they always fall through to leaf
  handling — they are never silently reinterpreted as a message.
"""

from __future__ import annotations

from typing import Callable, Tuple

MARKER = b"Documents/GitHub/"  # default; override via transform(..., marker=)

LeafFn = Callable[[bytes], Tuple[bytes, int]]


def _read_varint(b: bytes, i: int) -> Tuple[int, int]:
    shift = 0
    result = 0
    while True:
        if i >= len(b):
            raise ValueError("varint overrun")
        c = b[i]
        i += 1
        result |= (c & 0x7F) << shift
        if not c & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _write_varint(out: bytearray, v: int) -> None:
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def transform(buf: bytes, leaf: LeafFn, marker: bytes = MARKER) -> Tuple[bytes, int]:
    """Return (new_buf, num_substitutions). Raises ValueError if ``buf`` is not a
    well-formed protobuf message at this level (caller treats that as "not a
    message, handle as a leaf")."""
    out = bytearray()
    count = 0
    i = 0
    n = len(buf)
    while i < n:
        tag, i = _read_varint(buf, i)
        wt = tag & 7
        if wt not in (0, 1, 2, 5):
            raise ValueError(f"bad wiretype {wt}")
        _write_varint(out, tag)
        if wt == 0:  # varint
            v, i = _read_varint(buf, i)
            _write_varint(out, v)
        elif wt == 1:  # 64-bit
            if i + 8 > n:
                raise ValueError("i64 overrun")
            out += buf[i : i + 8]
            i += 8
        elif wt == 5:  # 32-bit
            if i + 4 > n:
                raise ValueError("i32 overrun")
            out += buf[i : i + 4]
            i += 4
        else:  # wt == 2, length-delimited
            ln, i = _read_varint(buf, i)
            if i + ln > n:
                raise ValueError("ld overrun")
            payload = buf[i : i + ln]
            i += ln
            if marker in payload:
                new_payload = None
                c = 0
                try:
                    sub, c = transform(payload, leaf, marker)
                    if c == 0 and sub != payload:
                        sub = None  # non-canonical / misparse → not a message
                    new_payload = sub
                except ValueError:
                    new_payload = None
                if new_payload is None:
                    new_payload, c = leaf(payload)
                count += c
            else:
                new_payload = payload
            _write_varint(out, len(new_payload))
            out += new_payload
    return bytes(out), count
