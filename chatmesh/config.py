"""Configuration: env file at ~/.config/chatmesh/env, overridden by process
environment. Machine identity is implicit (local $HOME); peers are ssh host
aliases from ~/.ssh/config, so user@ip lives there, not here."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

CONFIG_PATH = os.path.expanduser("~/.config/chatmesh/env")
STATE_DIR = os.path.expanduser("~/.local/state/chatmesh")
REMOTE_REPO = ".local/share/chatmesh/repo"  # relative to remote $HOME

DEFAULTS = {
    "CHATMESH_PEERS": "",
    "CHATMESH_APPS": "cursor,cursor-cli,claude,codex",
    "CHATMESH_DIRECTIONS": "pull,push",
    "CHATMESH_INTERVAL": "3600",           # launchd StartInterval seconds
    "CHATMESH_FILE_GUARD_MINUTES": "15",   # don't touch files modified this recently
    "CHATMESH_SYNC_CHECKPOINTS": "0",      # agentKv/checkpoint blobs (tens of GB) — off
    "CHATMESH_MAX_COMPOSERS_PER_RUN": "0", # 0 = unlimited
    "CHATMESH_PROCESS_GATE_APPS": "cursor,cursor-cli",  # skip sync while app runs
    "CHATMESH_LOG_LEVEL": "INFO",
}


def _read_env_file(path: str) -> dict:
    out = {}
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


@dataclass
class Config:
    peers: List[str] = field(default_factory=list)
    apps: List[str] = field(default_factory=list)
    directions: List[str] = field(default_factory=list)
    interval: int = 3600
    file_guard_sec: int = 900
    sync_checkpoints: bool = False
    max_composers_per_run: int = 0
    process_gate_apps: List[str] = field(default_factory=list)
    log_level: str = "INFO"
    state_dir: str = STATE_DIR

    @staticmethod
    def load() -> "Config":
        merged = dict(DEFAULTS)
        merged.update(_read_env_file(CONFIG_PATH))
        for k in DEFAULTS:
            if k in os.environ:
                merged[k] = os.environ[k]

        def csv(key):
            return [x.strip() for x in merged[key].split(",") if x.strip()]

        return Config(
            peers=csv("CHATMESH_PEERS"),
            apps=csv("CHATMESH_APPS"),
            directions=csv("CHATMESH_DIRECTIONS"),
            interval=int(merged["CHATMESH_INTERVAL"]),
            file_guard_sec=int(merged["CHATMESH_FILE_GUARD_MINUTES"]) * 60,
            sync_checkpoints=merged["CHATMESH_SYNC_CHECKPOINTS"] == "1",
            max_composers_per_run=int(merged["CHATMESH_MAX_COMPOSERS_PER_RUN"]),
            process_gate_apps=csv("CHATMESH_PROCESS_GATE_APPS"),
            log_level=merged["CHATMESH_LOG_LEVEL"].upper(),
        )
