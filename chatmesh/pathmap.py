"""Boundary-safe, idempotent absolute-path remapper.

This is the heart of the whole tool. Given a ``move_map`` of
``{old_absolute_path: new_absolute_path}`` it rewrites occurrences of the old
paths to the new ones inside arbitrary text or bytes, while being careful about
two things that naive ``str.replace`` gets wrong:

1. **Component boundaries.** Replacing ``/repos/sync-toolkit`` must not corrupt
   ``/repos/sync-toolkit-helper``. We only match when the character after the
   path is *not* a filename-continuation character. Filename chars are
   ``[A-Za-z0-9_-]`` plus ``.`` when it is followed by another alphanumeric
   (an extension / sub-name, e.g. ``dvr.old``). So a trailing ``/``, space,
   quote, ``&``, ``.`` used as sentence punctuation, or end-of-string all count
   as a boundary and *do* match — which matters because chat logs contain bare
   paths like ``cd /repos/foo 2>/dev/null`` and ``the repo at /repos/foo.``.

2. **Idempotency.** A mapping is *ambiguous* when ``new`` starts with
   ``old + "/"`` — i.e. the directory moved *into* a child whose path contains
   its own old path as a prefix (the classic case: a repo named ``mhadifilms``
   moving into the ``mhadifilms/`` group dir → ``mhadifilms/mhadifilms``).
   For those, re-running the remap would match the already-migrated path again.
   We defend against that by additionally forbidding a following ``/`` for
   ambiguous mappings, so only the *bare* old path is rewritten and migrated
   paths are left alone. Non-ambiguous mappings are naturally idempotent because
   the old path simply no longer exists once migrated.

Works on ``str`` and ``bytes`` (paths are ASCII, so the same logic applies to
both; URL-encoded ``%20`` variants are added automatically for paths with
spaces).
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Dict, List, Tuple

# A repo path is "still part of a name" if the next char is one of these.
_NAME_BOUNDARY = r"(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9])"
# Ambiguous (new startswith old + "/") additionally must not be followed by "/".
_AMBIG_BOUNDARY = r"(?![A-Za-z0-9_/-])(?!\.[A-Za-z0-9])"


def _variants(old: str, new: str) -> List[Tuple[str, str]]:
    """Raw + URL-encoded (%20) variants of a single mapping."""
    out = [(old, new)]
    eo, en = urllib.parse.quote(old), urllib.parse.quote(new)
    if eo != old:
        out.append((eo, en))
    return out


class PathMap:
    """Compiled remapper for a given move_map."""

    def __init__(self, move_map: Dict[str, str]):
        normal: List[Tuple[str, str]] = []
        ambiguous: List[Tuple[str, str]] = []
        for old, new in move_map.items():
            bucket = ambiguous if new.startswith(old + "/") else normal
            bucket.extend(_variants(old, new))

        # Longest old-path first so the most specific mapping wins.
        normal.sort(key=lambda p: len(p[0]), reverse=True)
        ambiguous.sort(key=lambda p: len(p[0]), reverse=True)

        self._norm = dict(normal)
        self._ambig = dict(ambiguous)
        self._norm_t = self._compile(normal, _NAME_BOUNDARY, False)
        self._ambig_t = self._compile(ambiguous, _AMBIG_BOUNDARY, False)
        self._norm_b = self._compile(normal, _NAME_BOUNDARY, True)
        self._ambig_b = self._compile(ambiguous, _AMBIG_BOUNDARY, True)
        # A cheap "is this even worth scanning" probe (common prefix marker).
        self._marker_t = "/" + "Documents/GitHub/"  # overridden below if set
        self._has = bool(normal or ambiguous)

    @staticmethod
    def _compile(pairs, boundary, as_bytes):
        if not pairs:
            return None
        pat = "(" + "|".join(re.escape(o) for o, _ in pairs) + ")" + boundary
        return re.compile(pat.encode() if as_bytes else pat)

    # -- text --------------------------------------------------------------
    def remap_text(self, text: str) -> Tuple[str, int]:
        if not self._has or "/" not in text:
            return text, 0
        n = [0]
        if self._norm_t is not None:
            text = self._norm_t.sub(
                lambda m: (n.__setitem__(0, n[0] + 1) or self._norm[m.group(1)]), text
            )
        if self._ambig_t is not None:
            text = self._ambig_t.sub(
                lambda m: (n.__setitem__(0, n[0] + 1) or self._ambig[m.group(1)]), text
            )
        return text, n[0]

    # -- bytes -------------------------------------------------------------
    def remap_bytes(self, data: bytes) -> Tuple[bytes, int]:
        if not self._has:
            return data, 0
        n = [0]
        if self._norm_b is not None:
            data = self._norm_b.sub(
                lambda m: (n.__setitem__(0, n[0] + 1) or self._norm[m.group(1).decode()].encode()),
                data,
            )
        if self._ambig_b is not None:
            data = self._ambig_b.sub(
                lambda m: (n.__setitem__(0, n[0] + 1) or self._ambig[m.group(1).decode()].encode()),
                data,
            )
        return data, n[0]
