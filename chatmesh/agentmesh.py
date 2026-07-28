"""Cross-agent chat and resource meshing.

Claude Code and Codex keep incompatible private JSONL schemas.  Chatmesh
projects the human-visible user/assistant text from each native session into a
separate session understood by the other tool.  Generated records are marked,
so imports never echo back, and an imported session is never rewritten after a
user continues it.

The resource layer incorporates Vibelink's useful compatibility model: a
curated agent/path registry, one canonical skill tree, repo manifests, relative
symlinks, planning, and health reporting.  Unlike Vibelink, ambiguous or
different live content is quarantined rather than force-overwritten.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from . import VERSION
from .config import AgentMeshProfile, Config, home_dir
from .store import record_conflict, slug


MESH_VERSION = 1
MESH_KEY = "chatmesh"
NAMESPACE = uuid.UUID("595853a4-e62f-5f6c-aec4-07b49eb40618")
MANIFEST_NAMES = ("chatmesh.json", "vibelink.json")

# Ported and normalized from Vibelink's verified agent registry.  The first
# path is preferred; later paths preserve compatibility with older releases.
AGENTS = {
    "claude": {
        "skills": (".claude/skills",),
        "rules": (".claude/rules",),
        "instruction": "CLAUDE.md",
    },
    "codex": {
        "skills": (".agents/skills",),
        "rules": (),
        "instruction": "AGENTS.md",
    },
    "cursor": {
        "skills": (".cursor/skills-cursor", ".cursor/skills"),
        "rules": (".cursor/rules",),
        "instruction": ".cursorrules",
    },
    "opencode": {
        "skills": (".config/opencode/skills", ".opencode/skills"),
        "rules": (),
        "instruction": "AGENTS.md",
    },
    "gemini": {
        "skills": (".gemini/skills",),
        "rules": (),
        "instruction": "GEMINI.md",
    },
    "antigravity": {
        "skills": (".gemini/antigravity/skills",),
        "rules": (),
        "instruction": "GEMINI.md",
    },
    "copilot": {
        "skills": (".copilot/skills",),
        "rules": (),
        "instruction": "AGENTS.md",
    },
    "windsurf": {
        "skills": (".codeium/windsurf/skills",),
        "rules": (),
        "instruction": "AGENTS.md",
    },
}


def _marker(source_agent: str, source_id: str) -> dict:
    return {
        "version": MESH_VERSION,
        "source_agent": source_agent,
        "source_session": source_id,
    }


def _json_lines(path: str) -> Iterator[dict]:
    """Yield JSONL objects without retaining tool-heavy source records."""
    with open(path, "r", encoding="utf-8", errors="replace") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid JSONL at line %d: %s" % (number, exc))
            if isinstance(value, dict):
                yield value


def _text_blocks(value, allowed: Iterable[str]) -> List[str]:
    allowed = set(allowed)
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    out = []
    for block in value:
        if not isinstance(block, dict) or block.get("type") not in allowed:
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            out.append(text)
    return out


def _append_message(
    messages: List[dict], role: str, text: str, timestamp: object,
    retained_bytes: int, limit: int,
) -> int:
    retained_bytes += len(text.encode("utf-8"))
    if limit and retained_bytes > limit:
        raise ValueError(
            "projected conversation exceeds max_session_bytes"
        )
    messages.append({
        "role": role,
        "text": text,
        "timestamp": timestamp,
    })
    return retained_bytes


def read_claude_session(path: str, limit: int) -> Optional[dict]:
    messages = []
    cwd = None
    session_id = None
    retained_bytes = 0
    first_record = True
    for record in _json_lines(path):
        if first_record:
            first_record = False
            if isinstance(record.get(MESH_KEY), dict):
                return None
        cwd = cwd or record.get("cwd")
        session_id = session_id or record.get("sessionId")
        kind = record.get("type")
        message = record.get("message")
        if kind not in ("user", "assistant") or not isinstance(message, dict):
            continue
        if kind == "user":
            content = message.get("content")
            # Tool results are represented as user messages but are not human
            # conversation and may contain secrets or very large command output.
            if isinstance(content, list) and any(
                isinstance(item, dict) and item.get("type") == "tool_result"
                for item in content
            ):
                continue
            texts = _text_blocks(content, ("text",))
        else:
            texts = _text_blocks(message.get("content"), ("text",))
        if texts:
            retained_bytes = _append_message(
                messages, kind, "\n\n".join(texts),
                record.get("timestamp"), retained_bytes, limit,
            )
    if not messages:
        return None
    source_id = str(session_id or os.path.splitext(os.path.basename(path))[0])
    return {
        "agent": "claude",
        "id": source_id,
        "cwd": str(cwd or home_dir()),
        "messages": messages,
    }


def read_codex_session(path: str, limit: int) -> Optional[dict]:
    cwd = None
    session_id = None
    messages = []
    fallback_messages = []
    retained_bytes = 0
    fallback_bytes = 0
    fallback_too_large = False
    first_record = True
    for record in _json_lines(path):
        if first_record:
            first_record = False
            if isinstance(record.get(MESH_KEY), dict):
                return None
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "session_meta":
            cwd = cwd or payload.get("cwd")
            session_id = session_id or payload.get("id") or payload.get("session_id")
        if (
            record.get("type") == "response_item"
            and payload.get("type") == "message"
        ):
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            allowed = ("input_text",) if role == "user" else ("output_text",)
            texts = _text_blocks(payload.get("content"), allowed)
            if texts:
                if not messages:
                    fallback_messages = []
                    fallback_bytes = 0
                    fallback_too_large = False
                retained_bytes = _append_message(
                    messages, role, "\n\n".join(texts),
                    record.get("timestamp"), retained_bytes, limit,
                )
            continue
        if (
            record.get("type") != "event_msg"
            or messages
            or fallback_too_large
        ):
            continue
        kind = payload.get("type")
        role = "user" if kind == "user_message" else (
            "assistant" if kind == "agent_message" else None
        )
        text = payload.get("message")
        if role and isinstance(text, str) and text.strip():
            try:
                fallback_bytes = _append_message(
                    fallback_messages, role, text,
                    record.get("timestamp"), fallback_bytes, limit,
                )
            except ValueError:
                fallback_messages = []
                fallback_too_large = True
    if not messages:
        # Older Codex rollouts may only retain the display event stream.
        if fallback_too_large:
            raise ValueError(
                "projected conversation exceeds max_session_bytes"
            )
        messages = fallback_messages
    if not messages:
        return None
    source_id = str(session_id or os.path.splitext(os.path.basename(path))[0])
    return {
        "agent": "codex",
        "id": source_id,
        "cwd": str(cwd or home_dir()),
        "messages": messages,
    }


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _stable_uuid(*parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, "\0".join(parts)))


def codex_projection(session: Mapping[str, object]) -> Tuple[str, bytes]:
    source_id = str(session["id"])
    marker = _marker("claude", source_id)
    messages = list(session["messages"])
    first = _parse_timestamp(messages[0].get("timestamp"))
    session_id = _stable_uuid("claude", source_id, "codex")
    timestamp = first.isoformat().replace("+00:00", "Z")
    records = [{
        "timestamp": timestamp,
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "session_id": session_id,
            "timestamp": timestamp,
            "cwd": session["cwd"],
            "originator": "chatmesh",
            "cli_version": VERSION,
            "source": "cli",
            "thread_source": "user",
            "model_provider": "openai",
        },
        MESH_KEY: marker,
    }]
    for index, message in enumerate(messages):
        role = message["role"]
        records.append({
            "timestamp": message.get("timestamp") or timestamp,
            "type": "event_msg",
            "payload": (
                {
                    "type": "user_message",
                    "message": message["text"],
                    "images": [],
                    "local_images": [],
                    "text_elements": [],
                }
                if role == "user"
                else {
                    "type": "agent_message",
                    "message": message["text"],
                    "phase": "final_answer",
                }
            ),
            MESH_KEY: marker,
        })
        records.append({
            "timestamp": message.get("timestamp") or timestamp,
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": _stable_uuid("claude", source_id, str(index)),
                "role": role,
                "content": [{
                    "type": "input_text" if role == "user" else "output_text",
                    "text": message["text"],
                }],
            },
            MESH_KEY: marker,
        })
    relative = os.path.join(
        ".codex", "sessions", first.strftime("%Y"), first.strftime("%m"),
        first.strftime("%d"),
        "rollout-%s-%s.jsonl" % (
            first.strftime("%Y-%m-%dT%H-%M-%S"), session_id
        ),
    )
    return relative, _encode_records(records)


def claude_projection(session: Mapping[str, object]) -> Tuple[str, bytes]:
    source_id = str(session["id"])
    marker = _marker("codex", source_id)
    session_id = _stable_uuid("codex", source_id, "claude")
    messages = list(session["messages"])
    first = _parse_timestamp(messages[0].get("timestamp"))
    parent = None
    records = []
    for index, message in enumerate(messages):
        record_id = _stable_uuid("codex", source_id, str(index))
        role = message["role"]
        message_value = (
            {"role": "user", "content": message["text"]}
            if role == "user"
            else {
                "id": _stable_uuid("codex", source_id, "message", str(index)),
                "type": "message",
                "role": "assistant",
                "model": "chatmesh-import",
                "content": [{"type": "text", "text": message["text"]}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        )
        records.append({
            "parentUuid": parent,
            "isSidechain": False,
            "type": role,
            "message": message_value,
            "uuid": record_id,
            "timestamp": message.get("timestamp") or first.isoformat(),
            "userType": "external",
            "cwd": session["cwd"],
            "sessionId": session_id,
            "version": "chatmesh-%s" % VERSION,
            MESH_KEY: marker,
        })
        parent = record_id
    encoded_cwd = str(session["cwd"]).replace("/", "-") or "-"
    relative = os.path.join(
        ".claude", "projects", encoded_cwd, "%s.jsonl" % session_id
    )
    return relative, _encode_records(records)


def _encode_records(records: Sequence[dict]) -> bytes:
    return (
        "\n".join(
            json.dumps(item, sort_keys=True, separators=(",", ":"))
            for item in records
        ) + "\n"
    ).encode("utf-8")


def _owned_projection(path: str, expected: Mapping[str, object]) -> bool:
    found = False
    try:
        for record in _json_lines(path):
            found = True
            if record.get(MESH_KEY) != expected:
                return False
    except (OSError, ValueError):
        return False
    return found


def _file_matches_bytes(path: str, data: bytes) -> bool:
    try:
        if os.path.getsize(path) != len(data):
            return False
        offset = 0
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                if chunk != data[offset:offset + len(chunk)]:
                    return False
                offset += len(chunk)
        return offset == len(data)
    except OSError:
        return False


def _atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".chatmesh-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _assert_safe_parent(base: str, path: str) -> None:
    """Reject writes whose nearest existing parent resolves outside *base*."""
    root = os.path.realpath(base)
    parent = os.path.dirname(os.path.abspath(path))
    while not os.path.exists(parent):
        higher = os.path.dirname(parent)
        if higher == parent:
            break
        parent = higher
    resolved = os.path.realpath(parent)
    try:
        inside = os.path.commonpath([root, resolved]) == root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("destination parent escapes mesh root: %s" % path)


def _message_conflict(
    cfg: Config, source: str, destination: str, reason: str
) -> dict:
    return {
        "source": source,
        "destination": destination,
        "reason": reason,
        "record": record_conflict(
            "agent-mesh", "local", os.path.basename(source),
            {"source": source, "destination": destination, "reason": reason},
            state_dir=cfg.state_dir,
        ),
    }


def mesh_messages(
    cfg: Config, *, user_home: Optional[str] = None,
    dry_run: bool = False, now: Optional[float] = None,
) -> dict:
    profile = cfg.agent_mesh
    actual_home = os.path.abspath(user_home or home_dir())
    cutoff = (now or time.time()) - cfg.file_guard_sec
    result = {"created": [], "updated": [], "kept": [], "conflicts": [], "blocked": []}
    sources = []
    for root, agent, reader, projector in (
        (os.path.join(actual_home, ".claude", "projects"), "claude",
         read_claude_session, codex_projection),
        (os.path.join(actual_home, ".codex", "sessions"), "codex",
         read_codex_session, claude_projection),
    ):
        if not os.path.isdir(root):
            continue
        for directory, dirnames, files in os.walk(root, followlinks=False):
            dirnames[:] = [name for name in dirnames if not os.path.islink(
                os.path.join(directory, name)
            ) and name != "subagents"]
            for filename in files:
                if filename.endswith(".jsonl"):
                    sources.append((os.path.join(directory, filename), agent, reader, projector))
    for source, agent, reader, projector in sorted(sources):
        try:
            if os.path.getmtime(source) > cutoff:
                result["kept"].append({"source": source, "reason": "active-session"})
                continue
            session = reader(source, profile.max_session_bytes)
            if session is None:
                continue
            relative, data = projector(session)
            destination = os.path.join(actual_home, relative)
            _assert_safe_parent(actual_home, destination)
            expected = _marker(agent, str(session["id"]))
            if os.path.exists(destination):
                if _file_matches_bytes(destination, data):
                    result["kept"].append({"source": source, "destination": destination})
                    continue
                if os.path.getmtime(destination) > cutoff:
                    if not dry_run:
                        result["conflicts"].append(_message_conflict(
                            cfg, source, destination, "destination session is active"
                        ))
                    else:
                        result["conflicts"].append({
                            "source": source, "destination": destination,
                            "reason": "destination session is active",
                        })
                    continue
                if not _owned_projection(destination, expected):
                    if not dry_run:
                        result["conflicts"].append(_message_conflict(
                            cfg, source, destination,
                            "imported session was continued or replaced",
                        ))
                    else:
                        result["conflicts"].append({
                            "source": source, "destination": destination,
                            "reason": "imported session was continued or replaced",
                        })
                    continue
                action = "updated"
            else:
                action = "created"
            result[action].append({"source": source, "destination": destination})
            if not dry_run:
                _atomic_write(destination, data)
        except (OSError, ValueError) as exc:
            result["blocked"].append({"source": source, "reason": str(exc)})
    return result


def _safe_name(value: str) -> str:
    name = re.sub(r"[^\w.-]+", "-", value).strip("-")
    if not name or name in (".", ".."):
        raise ValueError("unsafe resource name")
    return name


def _skill_root(agent: str, base: str) -> str:
    paths = AGENTS[agent]["skills"]
    for relative in paths:
        candidate = os.path.join(base, relative)
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(base, paths[0])


def _skill_entries(root: str) -> Dict[str, str]:
    entries = {}
    if not os.path.isdir(root):
        return entries
    for name in sorted(os.listdir(root)):
        if name.startswith("."):
            continue
        path = os.path.join(root, name)
        resolved = os.path.realpath(path)
        if os.path.isfile(os.path.join(resolved, "SKILL.md")):
            entries[_safe_name(name)] = resolved
    return entries


def _tree_digest(root: str) -> str:
    digest = hashlib.sha256()
    base = os.path.realpath(root)
    for directory, dirnames, files in os.walk(base, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if not os.path.islink(os.path.join(directory, name))
        )
        for filename in sorted(files):
            path = os.path.join(directory, filename)
            relative = os.path.relpath(path, base).replace(os.sep, "/")
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                raise ValueError("skill contains unsupported file: %s" % relative)
            digest.update(relative.encode("utf-8") + b"\0")
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def _backup_path(cfg: Config, target: str) -> str:
    root = os.path.join(
        cfg.state_dir, "backups", time.strftime("%Y%m%d"), "agent-mesh"
    )
    os.makedirs(root, exist_ok=True)
    base = os.path.join(root, slug(target))
    candidate = base
    index = 1
    while os.path.lexists(candidate):
        candidate = "%s-%d" % (base, index)
        index += 1
    return candidate


def _relative_symlink(source: str, target: str) -> None:
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.symlink(os.path.relpath(source, os.path.dirname(target)), target)


def mesh_global_skills(
    cfg: Config, *, user_home: Optional[str] = None, dry_run: bool = False,
) -> dict:
    actual_home = os.path.abspath(user_home or home_dir())
    agents = cfg.agent_mesh.resource_agents
    canonical_root = os.path.join(actual_home, ".agents", "skills")
    discovered: Dict[str, List[Tuple[str, str]]] = {}
    for agent in agents:
        root = _skill_root(agent, actual_home)
        for name, source in _skill_entries(root).items():
            discovered.setdefault(name, []).append((agent, source))
    result = {"created": [], "linked": [], "kept": [], "conflicts": []}
    for name, entries in sorted(discovered.items()):
        canonical = os.path.join(canonical_root, name)
        try:
            _assert_safe_parent(actual_home, canonical)
        except ValueError as exc:
            result["conflicts"].append({"name": name, "reason": str(exc)})
            continue
        digests = {}
        try:
            for _agent, source in entries:
                digests.setdefault(_tree_digest(source), []).append(source)
        except (OSError, ValueError) as exc:
            result["conflicts"].append({"name": name, "reason": str(exc)})
            continue
        if len(digests) != 1:
            detail = {
                "name": name,
                "reason": "different skill content exists across agents",
                "sources": [source for _agent, source in entries],
            }
            if not dry_run:
                detail["record"] = record_conflict(
                    "agent-mesh", "local", "skill:" + name, detail,
                    state_dir=cfg.state_dir,
                )
            result["conflicts"].append(detail)
            continue
        source = entries[0][1]
        if not os.path.exists(canonical):
            result["created"].append({"name": name, "source": source, "target": canonical})
            if not dry_run:
                os.makedirs(canonical_root, exist_ok=True)
                shutil.copytree(source, canonical, symlinks=False)
        canonical_digest = next(iter(digests))
        for agent in agents:
            target = os.path.join(_skill_root(agent, actual_home), name)
            try:
                _assert_safe_parent(actual_home, target)
            except ValueError as exc:
                result["conflicts"].append({
                    "agent": agent, "name": name, "target": target,
                    "reason": str(exc),
                })
                continue
            if os.path.normpath(target) == os.path.normpath(canonical):
                continue
            if os.path.islink(target) and os.path.realpath(target) == os.path.realpath(canonical):
                result["kept"].append({"agent": agent, "name": name, "target": target})
                continue
            if os.path.lexists(target):
                try:
                    same = (
                        os.path.isdir(os.path.realpath(target))
                        and _tree_digest(os.path.realpath(target)) == canonical_digest
                    )
                except (OSError, ValueError):
                    same = False
                if not same:
                    detail = {
                        "agent": agent, "name": name, "target": target,
                        "reason": "target differs from canonical skill",
                    }
                    if not dry_run:
                        detail["record"] = record_conflict(
                            "agent-mesh", "local", "skill:%s:%s" % (name, agent),
                            detail, state_dir=cfg.state_dir,
                        )
                    result["conflicts"].append(detail)
                    continue
                backup = _backup_path(cfg, target) if not dry_run else None
                if not dry_run:
                    os.replace(target, backup)
            result["linked"].append({"agent": agent, "name": name, "target": target})
            if not dry_run:
                _relative_symlink(canonical, target)
    return result


def _manifest_paths(roots: Sequence[str]) -> List[str]:
    found = []
    skip = {".git", ".worktrees", "node_modules", ".venv", "venv", "vendor"}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for directory, dirnames, files in os.walk(root, followlinks=False):
            dirnames[:] = [
                name for name in dirnames if name not in skip
                and not os.path.islink(os.path.join(directory, name))
            ]
            for name in MANIFEST_NAMES:
                if name in files:
                    found.append(os.path.join(directory, name))
                    dirnames[:] = []
                    break
    return sorted(set(found))


def _manifest_source(repo: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("manifest resource paths must be nonempty strings")
    path = os.path.abspath(os.path.join(repo, value))
    real_repo = os.path.realpath(repo)
    real_path = os.path.realpath(path)
    if os.path.commonpath([real_repo, real_path]) != real_repo:
        raise ValueError("manifest resource escapes repository: %s" % value)
    if not os.path.exists(path):
        raise ValueError("manifest resource is missing: %s" % value)
    return path


def _resource_source(path: str, kind: str) -> Tuple[str, str]:
    if kind == "skill":
        if os.path.isdir(path):
            entry = os.path.join(path, "SKILL.md")
            if not os.path.isfile(entry):
                raise ValueError("skill directory has no SKILL.md: %s" % path)
            name = os.path.basename(path)
            return path, _safe_name(name)
        if os.path.basename(path).lower() == "skill.md":
            return os.path.dirname(path), _safe_name(os.path.basename(os.path.dirname(path)))
    if not os.path.isfile(path):
        raise ValueError("%s must be a file: %s" % (kind, path))
    return path, _safe_name(os.path.splitext(os.path.basename(path))[0])


def _link_manifest_target(
    cfg: Config, source: str, target: str, identity: str, dry_run: bool
) -> Tuple[str, dict]:
    if os.path.islink(target) and os.path.realpath(target) == os.path.realpath(source):
        return "kept", {"source": source, "target": target}
    if os.path.lexists(target):
        same = False
        try:
            if os.path.isdir(source) and os.path.isdir(os.path.realpath(target)):
                same = _tree_digest(source) == _tree_digest(os.path.realpath(target))
            elif os.path.isfile(source) and os.path.isfile(os.path.realpath(target)):
                with open(source, "rb") as left, open(os.path.realpath(target), "rb") as right:
                    same = left.read() == right.read()
        except OSError:
            same = False
        if not same:
            detail = {
                "source": source, "target": target,
                "reason": "manifest target has different live content",
            }
            if not dry_run:
                detail["record"] = record_conflict(
                    "agent-mesh", "local", identity, detail,
                    state_dir=cfg.state_dir,
                )
            return "conflicts", detail
        if not dry_run:
            os.replace(target, _backup_path(cfg, target))
    if not dry_run:
        _relative_symlink(source, target)
    return "linked", {"source": source, "target": target}


def mesh_manifests(cfg: Config, *, dry_run: bool = False) -> dict:
    result = {"linked": [], "kept": [], "conflicts": [], "blocked": []}
    profile = cfg.agent_mesh
    for path in _manifest_paths(profile.roots):
        repo = os.path.dirname(path)
        try:
            with open(path, "r", encoding="utf-8") as stream:
                manifest = json.load(stream)
            if not isinstance(manifest, dict):
                raise ValueError("manifest must be a JSON object")
            agents = manifest.get("agents") or profile.resource_agents
            if not isinstance(agents, list) or any(agent not in AGENTS for agent in agents):
                raise ValueError("manifest agents contain unsupported values")
            for kind, enabled in (("skill", profile.skills), ("rule", profile.rules)):
                if not enabled:
                    continue
                values = manifest.get(kind + "s", [])
                if not isinstance(values, list):
                    raise ValueError("manifest %ss must be an array" % kind)
                for value in values:
                    raw = _manifest_source(repo, value)
                    source, name = _resource_source(raw, kind)
                    for agent in agents:
                        locations = AGENTS[agent][kind + "s"]
                        if not locations:
                            continue
                        root = os.path.join(repo, locations[0])
                        target = (
                            os.path.join(root, name)
                            if kind == "skill"
                            else os.path.join(
                                root, name + (".mdc" if agent == "cursor" else ".md")
                            )
                        )
                        _assert_safe_parent(repo, target)
                        bucket, detail = _link_manifest_target(
                            cfg, source, target,
                            "%s:%s:%s" % (path, kind, name), dry_run,
                        )
                        detail.update({"manifest": path, "agent": agent, "kind": kind})
                        result[bucket].append(detail)
            instruction = manifest.get("instructions")
            if profile.instructions and instruction:
                source = _manifest_source(repo, instruction)
                if not os.path.isfile(source):
                    raise ValueError("instructions source must be a file")
                for agent in agents:
                    target = os.path.join(repo, AGENTS[agent]["instruction"])
                    if os.path.normpath(target) == os.path.normpath(source):
                        continue
                    _assert_safe_parent(repo, target)
                    bucket, detail = _link_manifest_target(
                        cfg, source, target, "%s:instructions" % path, dry_run
                    )
                    detail.update({
                        "manifest": path, "agent": agent, "kind": "instructions"
                    })
                    result[bucket].append(detail)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result["blocked"].append({"manifest": path, "reason": str(exc)})
    return result


def run_agent_mesh(
    cfg: Config, *, user_home: Optional[str] = None, dry_run: bool = False,
) -> dict:
    if not cfg.agent_mesh.enabled:
        return {"enabled": False}
    result = {"enabled": True, "dry_run": dry_run}
    if cfg.agent_mesh.messages:
        result["messages"] = mesh_messages(
            cfg, user_home=user_home, dry_run=dry_run
        )
    if cfg.agent_mesh.skills:
        result["skills"] = mesh_global_skills(
            cfg, user_home=user_home, dry_run=dry_run
        )
    if cfg.agent_mesh.rules or cfg.agent_mesh.instructions or cfg.agent_mesh.skills:
        result["manifests"] = mesh_manifests(cfg, dry_run=dry_run)
    return result


def doctor(cfg: Config, *, user_home: Optional[str] = None) -> dict:
    actual_home = os.path.abspath(user_home or home_dir())
    issues = []
    healthy = []
    canonical_root = os.path.join(actual_home, ".agents", "skills")
    canonical = _skill_entries(canonical_root)
    for agent in cfg.agent_mesh.resource_agents:
        root = _skill_root(agent, actual_home)
        for name in _skill_entries(root):
            if name not in canonical:
                issues.append({
                    "agent": agent, "name": name,
                    "target": os.path.join(root, name),
                    "reason": "resource is absent from the canonical skill tree",
                })
        for name, source in canonical.items():
            target = os.path.join(root, name)
            if agent == "codex" and os.path.realpath(target) == source:
                healthy.append({"agent": agent, "name": name, "target": target})
            elif (
                os.path.islink(target)
                and os.path.realpath(target) == os.path.realpath(source)
            ):
                healthy.append({"agent": agent, "name": name, "target": target})
            elif not os.path.lexists(target):
                issues.append({
                    "agent": agent, "name": name, "target": target,
                    "reason": "resource link is missing",
                })
            else:
                issues.append({
                    "agent": agent, "name": name, "target": target,
                    "reason": "resource has drifted from the canonical skill tree",
                })
    return {"healthy": healthy, "issues": issues, "ok": not issues}
