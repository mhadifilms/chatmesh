"""Configuration-aware preference adapters and snapshot protocol helpers."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from typing import Dict, Mapping, Optional

from . import preferences
from .config import Config
from .store import atomic_write, read_json, slug
from .util import app_running_local, home


def adapters_for_config(cfg: Config, user_home: Optional[str] = None):
    user_home = os.path.abspath(user_home or home())
    selected = []
    for adapter in preferences.curated_adapters():
        if adapter.name == "shared-agent-skills":
            enabled = (
                cfg.preferences.cursor
                or cfg.preferences.claude
                or cfg.preferences.codex
            )
        elif adapter.name.startswith("cursor-"):
            enabled = cfg.preferences.cursor
        elif adapter.name.startswith("claude-"):
            enabled = cfg.preferences.claude
        elif adapter.name.startswith("codex-"):
            enabled = cfg.preferences.codex
        else:
            enabled = False
        if enabled:
            selected.append(adapter)

    for item in cfg.preferences.custom_paths:
        if not item.enabled:
            continue
        path = os.path.abspath(item.path)
        try:
            if os.path.commonpath([user_home, path]) != user_home:
                continue
        except ValueError:
            continue
        relative = os.path.relpath(path, user_home).replace(os.sep, "/")
        name = "custom-" + re.sub(
            r"[^a-zA-Z0-9._-]+", "-", item.name
        ).strip("-")
        selected.append(preferences.PreferenceAdapter(
            name=name,
            source=relative,
            canonical="custom/%s" % slug(item.name),
            tree=item.kind == "tree",
            format="auto",
            rewrite_text=item.rewrite_home,
            kind="file",
            config_scope=False,
            exclude=tuple(item.exclude),
            max_file_size=item.max_file_bytes,
        ))
    return tuple(selected)


def snapshot_preferences(cfg: Config, user_home: Optional[str] = None) -> dict:
    user_home = os.path.abspath(user_home or home())
    adapters = adapters_for_config(cfg, user_home)
    cursor_closed = not app_running_local("cursor")
    manifest, payloads = preferences.scan_preferences(
        user_home,
        adapters=adapters,
        max_file_size=cfg.preferences.max_file_bytes,
        max_total_size=cfg.preferences.max_total_bytes,
        cursor_closed=cursor_closed,
        exclude=cfg.preferences.exclude,
    )
    return {
        "version": 1,
        "home": user_home,
        "manifest": manifest,
        "payloads": preferences.encode_payloads(payloads),
    }


def decode_snapshot(snapshot: Mapping[str, object]):
    manifest = snapshot.get("manifest")
    payloads = snapshot.get("payloads")
    if snapshot.get("version") != 1 or not isinstance(manifest, dict):
        raise ValueError("invalid preference snapshot")
    if not isinstance(payloads, dict) or not isinstance(snapshot.get("home"), str):
        raise ValueError("invalid preference snapshot payloads")
    decoded = preferences.decode_payloads(payloads)
    entries = manifest.get("entries")
    if not isinstance(entries, dict) or set(entries) != set(decoded):
        raise ValueError("preference manifest/payload mismatch")
    for key, entry in entries.items():
        data = decoded[key]
        if entry.get("sha256") != hashlib.sha256(data).hexdigest():
            raise ValueError("preference payload hash mismatch")
    return copy.deepcopy(manifest), decoded, str(snapshot["home"])


def rewrite_snapshot_home(snapshot: Mapping[str, object],
                          destination_home: str) -> dict:
    manifest, payloads, source_home = decode_snapshot(snapshot)
    rewritten_manifest, rewritten_payloads = preferences.rewrite_snapshot(
        manifest, payloads, source_home, destination_home
    )
    return {
        "version": 1,
        "home": destination_home,
        "manifest": rewritten_manifest,
        "payloads": preferences.encode_payloads(rewritten_payloads),
    }


def empty_snapshot(user_home: str) -> dict:
    return {
        "version": 1,
        "home": user_home,
        "manifest": {
            "version": 1,
            "entries": {},
            "blocked": [],
            "total_size": 0,
        },
        "payloads": {},
    }


def baseline_path(cfg: Config, peer: str) -> str:
    return os.path.join(
        cfg.state_dir, "preferences", "baselines", slug(peer) + ".json"
    )


def load_baseline(cfg: Config, peer: str, canonical_home: str) -> dict:
    value = read_json(baseline_path(cfg, peer), default={})
    try:
        decode_snapshot(value)
        if value.get("home") == canonical_home:
            return value
    except (ValueError, TypeError):
        pass
    return empty_snapshot(canonical_home)


def write_baseline(cfg: Config, peer: str, snapshot: Mapping[str, object]) -> None:
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    atomic_write(baseline_path(cfg, peer), payload + b"\n", mode=0o600)


def converged_baseline(local: Mapping[str, object],
                       remote: Mapping[str, object],
                       previous: Mapping[str, object]) -> dict:
    lman, ldata, home_value = decode_snapshot(local)
    rman, rdata, _ = decode_snapshot(remote)
    pman, pdata, _ = decode_snapshot(previous)
    entries: Dict[str, dict] = {}
    payloads: Dict[str, bytes] = {}
    local_entries = lman["entries"]
    remote_entries = rman["entries"]
    for key in sorted(set(local_entries) & set(remote_entries)):
        if (
            local_entries[key].get("sha256")
            == remote_entries[key].get("sha256")
            and ldata[key] == rdata[key]
        ):
            entries[key] = copy.deepcopy(local_entries[key])
            payloads[key] = ldata[key]
    # Preserve old bases for unresolved concurrent edits.
    for key, entry in pman.get("entries", {}).items():
        if key not in entries and key in pdata:
            entries[key] = copy.deepcopy(entry)
            payloads[key] = pdata[key]
    manifest = {
        "version": 1,
        "entries": entries,
        "blocked": [],
        "total_size": sum(len(value) for value in payloads.values()),
    }
    return {
        "version": 1,
        "home": home_value,
        "manifest": manifest,
        "payloads": preferences.encode_payloads(payloads),
    }


def plan_snapshots(base: Mapping[str, object],
                   local: Mapping[str, object],
                   incoming: Mapping[str, object]) -> dict:
    base_manifest, base_payloads, _ = decode_snapshot(base)
    local_manifest, local_payloads, _ = decode_snapshot(local)
    incoming_manifest, incoming_payloads, _ = decode_snapshot(incoming)
    return preferences.plan_preferences(
        base_manifest,
        local_manifest,
        incoming_manifest,
        base_payloads=base_payloads,
        local_payloads=local_payloads,
        incoming_payloads=incoming_payloads,
    )


def gate_running_tools(cfg: Config, plan: Mapping[str, object]) -> dict:
    gated_prefixes = []
    for tool in ("cursor", "claude", "codex"):
        if tool in cfg.process_gate_apps and app_running_local(tool):
            gated_prefixes.append(tool + "-")
    if not gated_prefixes:
        return copy.deepcopy(plan)
    result = copy.deepcopy(plan)
    for action in result.get("actions", []):
        entry = action.get("entry")
        adapter = str(entry.get("adapter", "")) if isinstance(entry, dict) else ""
        if action.get("op") == "apply" and any(
            adapter.startswith(prefix) for prefix in gated_prefixes
        ):
            key = str(action.get("key", ""))
            action.clear()
            action.update({
                "key": key,
                "op": "keep",
                "reason": "process_gate",
            })
    result["counts"] = {
        operation: sum(
            1 for action in result.get("actions", [])
            if action.get("op") == operation
        )
        for operation in ("apply", "keep", "conflict")
    }
    return result


def rebind_plan_destinations(cfg: Config, plan: Mapping[str, object],
                             user_home: str) -> dict:
    result = copy.deepcopy(plan)
    adapters = {
        adapter.name: adapter
        for adapter in adapters_for_config(cfg, user_home)
    }
    for action in result.get("actions", []):
        if action.get("op") != "apply":
            continue
        entry = action.get("entry")
        key = str(action.get("key", ""))
        adapter = adapters.get(
            str(entry.get("adapter", "")) if isinstance(entry, dict) else ""
        )
        if adapter is None or not isinstance(entry, dict):
            payload = action.get("payload")
            action.clear()
            action.update({
                "key": key,
                "op": "conflict",
                "reason": "adapter_not_enabled",
                "conflict_paths": ["$adapter"],
            })
            if isinstance(payload, str):
                action["inbox_payload"] = payload
            continue
        if adapter.tree:
            prefix = adapter.canonical.rstrip("/") + "/"
            if not key.startswith(prefix):
                continue
            suffix = key[len(prefix):]
            entry["destination"] = (
                adapter.source.rstrip("/") + "/" + suffix
            )
        elif key == adapter.canonical:
            entry["destination"] = adapter.source
    result["counts"] = {
        operation: sum(
            1 for action in result.get("actions", [])
            if action.get("op") == operation
        )
        for operation in ("apply", "keep", "conflict")
    }
    return result


def apply_preference_plan(cfg: Config, plan: Mapping[str, object],
                          user_home: Optional[str] = None) -> dict:
    user_home = os.path.abspath(user_home or home())
    rebound = rebind_plan_destinations(cfg, plan, user_home)
    gated = gate_running_tools(cfg, rebound)
    batch = __import__("time").strftime("%Y%m%d-%H%M%S")
    return preferences.apply_preferences(
        gated,
        user_home,
        backup_dir=os.path.join(
            cfg.state_dir, "backups", "preferences", batch
        ),
        inbox_dir=os.path.join(cfg.state_dir, "inbox", "preferences"),
        cursor_closed=not app_running_local("cursor"),
        max_file_size=cfg.preferences.max_file_bytes,
        adapters=adapters_for_config(cfg, user_home),
    )
