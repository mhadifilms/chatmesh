"""Config-aware environment snapshot, quarantine, and apply helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile

from . import environment, store
from .config import Config


def snapshot_environment(cfg: Config) -> dict:
    return environment.snapshot_environment(cfg.environment)


def plan_snapshots(cfg: Config, source: dict, destination: dict) -> dict:
    return environment.plan_environment(
        source, destination, cfg.environment
    )


def pending_root(cfg: Config) -> str:
    return os.path.join(cfg.state_dir, "inbox", "environment")


def list_pending(cfg: Config):
    root = pending_root(cfg)
    found = []
    if os.path.isdir(root):
        for directory, _subdirs, files in os.walk(root):
            for filename in files:
                if filename == "snapshot.json" or filename.startswith("plan-"):
                    found.append(os.path.join(directory, filename))
    return sorted(found)


def _quarantine(
    cfg: Config, peer: str, source: dict, plan: dict
) -> str:
    environment.validate_manifest(source)
    snapshot_id = str(source.get("snapshot_id") or "unknown")
    directory = store.inbox_path(
        "environment",
        peer,
        "machine",
        snapshot_id,
        state_dir=cfg.state_dir,
    )
    snapshot_path = os.path.join(directory, "snapshot.json")
    plan_payload = json.dumps(
        plan, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    plan_id = hashlib.sha256(plan_payload).hexdigest()
    plan_path = os.path.join(directory, "plan-%s.json" % plan_id)
    parent = os.path.dirname(directory)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if not os.path.exists(directory):
        staging = tempfile.mkdtemp(prefix=".chatmesh-inbox-", dir=parent)
        try:
            _write_json_file(
                os.path.join(staging, "snapshot.json"), source
            )
            _write_json_file(
                os.path.join(staging, os.path.basename(plan_path)), plan
            )
            try:
                environment._rename_exclusive(staging, directory)
                return directory
            except environment.EnvironmentSyncError:
                # Another process may have published this snapshot first.
                for name in os.listdir(staging):
                    os.unlink(os.path.join(staging, name))
                os.rmdir(staging)
                if not os.path.isdir(directory):
                    raise
        finally:
            if os.path.isdir(staging):
                for name in os.listdir(staging):
                    os.unlink(os.path.join(staging, name))
                os.rmdir(staging)
    existing = store.read_json(snapshot_path, default=None)
    if (
        not isinstance(existing, dict)
        or environment.manifest_snapshot_id(existing) != snapshot_id
    ):
        raise environment.EnvironmentSyncError(
            "environment inbox snapshot ID collision"
        )
    _publish_json_exclusive(plan_path, plan)
    return directory


def _write_json_file(path: str, value: dict) -> None:
    payload = (
        json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    )
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_json_exclusive(path: str, value: dict) -> None:
    if os.path.exists(path):
        if store.read_json(path, default=None) == value:
            return
        raise environment.EnvironmentSyncError(
            "environment inbox plan collision"
        )
    descriptor, temporary = tempfile.mkstemp(
        prefix=".chatmesh-plan-", dir=os.path.dirname(path)
    )
    try:
        payload = (
            json.dumps(value, sort_keys=True, indent=2).encode("utf-8")
            + b"\n"
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if store.read_json(path, default=None) != value:
                raise environment.EnvironmentSyncError(
                    "environment inbox plan collision"
                )
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def apply_environment_snapshot(
    cfg: Config,
    peer: str,
    source: dict,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    """Inventory locally, quarantine the incoming snapshot, then add safely."""

    if not cfg.environment.enabled:
        raise environment.EnvironmentSyncError(
            "environment synchronization is disabled"
        )
    environment.validate_manifest(source)
    destination = snapshot_environment(cfg)
    plan = plan_snapshots(cfg, source, destination)
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "pending": len(plan["actions"]),
            "applied": [],
            "failed": [],
            "conflicts": plan["conflicts"],
            "blocked": plan["blocked"],
            "counts": plan["counts"],
            "inbox": None,
        }
    has_pending = bool(
        plan["actions"] or plan["conflicts"] or plan["blocked"]
    )
    inbox = (
        _quarantine(cfg, peer, source, plan)
        if has_pending and cfg.environment.conflict_policy != "skip"
        else None
    )
    if cfg.environment.conflict_policy != "skip":
        for conflict in plan["conflicts"]:
            store.record_conflict(
                "environment",
                peer,
                str(
                    conflict.get("path")
                    or conflict.get("kind")
                    or "machine"
                ),
                conflict,
                state_dir=cfg.state_dir,
            )
    should_apply = bool(
        force or cfg.environment.auto_apply
    )
    if not should_apply:
        return {
            "ok": True,
            "pending": len(plan["actions"]),
            "applied": [],
            "failed": [],
            "conflicts": plan["conflicts"],
            "blocked": plan["blocked"],
            "counts": plan["counts"],
            "inbox": inbox,
        }
    result = environment.apply_environment_plan(
        cfg.environment, plan, dry_run=False
    )
    result.update(
        {
            "pending": len(result.get("failed", [])),
            "counts": plan["counts"],
            "inbox": inbox,
        }
    )
    return result


def fetch_remote_snapshot(cfg: Config, peer: str) -> dict:
    # Kept here as a narrow import seam for user-facing CLI commands.
    from .sync import _remote_json

    return _remote_json(peer, ["environment-export"], timeout=900)


def plan_with_peer(cfg: Config, peer: str) -> dict:
    from .sync import _remote_plan_environment

    local = snapshot_environment(cfg)
    remote = fetch_remote_snapshot(cfg, peer)
    return {
        "pull": plan_snapshots(cfg, remote, local),
        "push": _remote_plan_environment(peer, local),
        "local": local,
        "remote": remote,
    }


def apply_with_peer(
    cfg: Config, peer: str, direction: str
) -> dict:
    if direction == "pull":
        source = fetch_remote_snapshot(cfg, peer)
        return apply_environment_snapshot(cfg, peer, source, force=True)
    if direction == "push":
        from .sync import _remote_apply_environment

        return _remote_apply_environment(
            peer, snapshot_environment(cfg), force=True
        )
    raise ValueError("direction must be pull or push")
