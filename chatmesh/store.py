"""Durable state, inbox, conflict, and transaction helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from typing import Any, Optional

from .config import default_state_dir


def slug(value: str, limit: int = 80) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-").lower()
    text = text[:limit].rstrip("-")
    if text:
        return text
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def atomic_write(path: str, data: bytes, mode: int = 0o600) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".chatmesh-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def write_json(path: str, value: Any, mode: int = 0o600) -> None:
    payload = json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    atomic_write(path, payload, mode=mode)


def read_json(path: str, default: Optional[Any] = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError, TypeError):
        return {} if default is None else default


def inbox_path(kind: str, peer: str, identity: str, snapshot_id: str,
               state_dir: str = "") -> str:
    return os.path.join(
        state_dir or default_state_dir(),
        "inbox",
        slug(kind),
        slug(peer),
        slug(identity),
        slug(snapshot_id),
    )


def conflict_path(kind: str, peer: str, identity: str,
                  state_dir: str = "") -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(
        ("%s\0%s\0%s\0%d" % (kind, peer, identity, time.time_ns())).encode()
    ).hexdigest()[:10]
    return os.path.join(
        state_dir or default_state_dir(), "conflicts", slug(kind), slug(peer),
        "%s-%s-%s.json" % (stamp, slug(identity), digest),
    )


def record_conflict(kind: str, peer: str, identity: str, detail: dict,
                    state_dir: str = "") -> str:
    path = conflict_path(kind, peer, identity, state_dir=state_dir)
    value = {
        "version": 1,
        "kind": kind,
        "peer": peer,
        "identity": identity,
        "created_at": int(time.time()),
        "detail": detail,
    }
    write_json(path, value)
    return path


def transaction_path(kind: str, identity: str, state_dir: str = "") -> str:
    return os.path.join(
        state_dir or default_state_dir(), "transactions", slug(kind),
        slug(identity) + ".json",
    )
