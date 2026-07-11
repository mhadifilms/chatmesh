"""Home-directory rewriting between machines with different usernames.

Text/JSON values go through PathMap (boundary-safe, handles %-encoded URIs);
binary values that fail utf-8 go through the lossless protobuf transformer so
length prefixes stay correct (Cursor stores some rows as protobuf)."""

from __future__ import annotations

from typing import Optional, Tuple

from .pathmap import PathMap
from . import protobuf


class HomeRewriter:
    def __init__(self, src_home: str, dst_home: str):
        self.src_home = src_home
        self.dst_home = dst_home
        self.identity = src_home == dst_home
        self.pm = PathMap({src_home: dst_home}) if not self.identity else None
        self.marker = src_home.encode()
        # Claude Code encodes project paths as dashes: /Users/x/a -> -Users-x-a
        self.enc_src = src_home.replace("/", "-")
        self.enc_dst = dst_home.replace("/", "-")

    # -- plain text ---------------------------------------------------------
    def text(self, s: str) -> Tuple[str, int]:
        if self.identity:
            return s, 0
        out, n = self.pm.remap_text(s)
        if self.enc_src + "-" in out:
            out = out.replace(self.enc_src + "-", self.enc_dst + "-")
            n += 1
        return out, n

    # -- filesystem path segment (claude project dir names) ------------------
    def encoded_name(self, name: str) -> str:
        if self.identity:
            return name
        if name == self.enc_src:
            return self.enc_dst
        if name.startswith(self.enc_src + "-"):
            return self.enc_dst + name[len(self.enc_src):]
        return name

    # -- sqlite value (TEXT or BLOB), preserving type ------------------------
    def value(self, v) -> Tuple[Optional[object], int]:
        """Return (new_value_or_None, count). None means unchanged."""
        if self.identity or v is None:
            return None, 0
        if isinstance(v, str):
            out, n = self.text(v)
            return (out, n) if n else (None, 0)
        if isinstance(v, (bytes, bytearray)):
            b = bytes(v)
            try:
                t = b.decode("utf-8")
            except UnicodeDecodeError:
                if self.marker not in b:
                    return None, 0
                try:
                    nb, n = protobuf.transform(b, self._leaf, self.marker)
                except ValueError:
                    nb, n = self.pm.remap_bytes(b)
                return (nb, n) if n and nb != b else (None, 0)
            out, n = self.text(t)
            return (out.encode("utf-8"), n) if n else (None, 0)
        return None, 0

    def _leaf(self, payload: bytes) -> Tuple[bytes, int]:
        try:
            t = payload.decode("utf-8")
        except UnicodeDecodeError:
            return self.pm.remap_bytes(payload)
        nt, c = self.text(t)
        return (nt.encode("utf-8"), c) if c else (payload, 0)
