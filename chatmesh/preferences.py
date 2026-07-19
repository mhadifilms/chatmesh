"""Safe, user-level preference inventory and three-way synchronization.

This module deliberately has no knowledge of project directories.  Its
adapters enumerate a small set of files below a user's home directory and
produce content-addressed manifests plus separate payload dictionaries.  The
separation makes manifests cheap to exchange over SSH and keeps mutation in
``apply_preferences``; inventory, rewriting, and planning remain independently
testable.

The implementation is conservative:

* literal credentials and machine/project state are never put in a payload;
* directory symlinks are not followed and file symlinks must stay inside their
  declared adapter root;
* concurrent edits become inbox records instead of replacing live files;
* JSON and the TOML subset used by the supported tools merge recursively;
* an apply backs up the affected value/file and uses atomic replacement; and
* Cursor's SQLite adapter reads and writes one allow-listed value only.

Python 3.9 and the standard library are sufficient.
"""

from __future__ import annotations

import ast
import base64
import copy
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import secrets
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote


MANIFEST_VERSION = 1
DEFAULT_MAX_FILE_SIZE = 2 * 1024 * 1024
DEFAULT_MAX_TOTAL_SIZE = 64 * 1024 * 1024
CURSOR_USER_RULE_KEY = "aicontext.personalContext"
CURSOR_STATE_DB = (
    "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
)

_TEXT_SUFFIXES = {
    ".cfg",
    ".conf",
    ".css",
    ".fish",
    ".ini",
    ".js",
    ".json",
    ".jsonc",
    ".md",
    ".mdc",
    ".plist",
    ".prompt",
    ".py",
    ".rb",
    ".rules",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
    ".zsh",
}
_TEXT_NAMES = {
    "AGENTS.md",
    "CLAUDE.md",
    "SKILL.md",
    "config",
    "settings",
}
_STRUCTURED_SUFFIXES = {".json", ".jsonc", ".toml"}
_PRUNE_NAMES = {
    ".cache",
    ".git",
    ".hg",
    ".managed",
    ".svn",
    ".system",
    ".venv",
    "__pycache__",
    "cache",
    "caches",
    "dist",
    "managed",
    "node_modules",
    "plugins",
    "runtime",
    "site-packages",
    "vendor",
    "venv",
}
_AUTH_NAME_RE = re.compile(
    r"(^|[._-])(auth|oauth|token|tokens|credential|credentials|keychain|"
    r"password|passwd|secret|secrets|session)([._-]|$)",
    re.IGNORECASE,
)
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|"
    r"token|secret|password|passwd|credential|oauth|authorization|bearer|"
    r"cookie)",
    re.IGNORECASE,
)
_SECRET_ARGUMENT_RE = re.compile(
    r"^--?(?:api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|"
    r"access[_-]?token|refresh[_-]?token|token|secret|password|credential|"
    r"authorization)(?:=|$)",
    re.IGNORECASE,
)
_SECRET_TEXT_RES = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b"),
    re.compile(
        r"(?i)[?&](?:api[_-]?key|access[_-]?token|token|secret|password)="
        r"(?!\$(?:\{|[A-Za-z_]))[^&\s]{8,}"
    ),
    re.compile(r"(?i)https?://[^/\s:@]+:[^/\s@]{6,}@"),
    re.compile(
        r"(?im)^\s*(?:api[_-]?key|access[_-]?token|password|secret|"
        r"authorization)\s*[:=]\s*[\"']?(?!\$\{?[A-Za-z_][A-Za-z0-9_]*\}?)"
        r"[^\s\"']{8,}"
    ),
)
_MACHINE_LOCAL_KEYS = {
    "account",
    "accounts",
    "auth",
    "device",
    "device_id",
    "installation_id",
    "keychain",
    "last_workspace",
    "machine",
    "machine_id",
    "projects",
    "project_state",
    "recent",
    "session",
    "sessions",
    "trust",
    "trusted_folders",
    "trusted_workspaces",
    "workspace",
    "workspaces",
}
_MISSING = object()


@dataclass(frozen=True)
class PreferenceAdapter:
    """One fixed user-level file or tree.

    ``source`` and ``canonical`` always use POSIX, home-relative paths.  Tree
    adapters append the same relative suffix to both values.
    """

    name: str
    source: str
    canonical: str
    tree: bool = False
    format: str = "auto"
    rewrite_text: bool = True
    kind: str = "file"
    mcp_only: bool = False
    config_scope: bool = False
    exclude: Tuple[str, ...] = ()
    max_file_size: Optional[int] = None


DEFAULT_ADAPTERS: Tuple[PreferenceAdapter, ...] = (
    # Cursor IDE (macOS and the portable Linux location).
    PreferenceAdapter(
        "cursor-ide-settings",
        "Library/Application Support/Cursor/User/settings.json",
        "cursor/ide/settings.json",
        format="json",
        config_scope=True,
    ),
    PreferenceAdapter(
        "cursor-ide-keybindings",
        "Library/Application Support/Cursor/User/keybindings.json",
        "cursor/ide/keybindings.json",
        format="json",
    ),
    PreferenceAdapter(
        "cursor-ide-snippets",
        "Library/Application Support/Cursor/User/snippets",
        "cursor/ide/snippets",
        tree=True,
    ),
    PreferenceAdapter(
        "cursor-ide-settings-linux",
        ".config/Cursor/User/settings.json",
        "cursor/ide-linux/settings.json",
        format="json",
        config_scope=True,
    ),
    PreferenceAdapter(
        "cursor-ide-keybindings-linux",
        ".config/Cursor/User/keybindings.json",
        "cursor/ide-linux/keybindings.json",
        format="json",
    ),
    PreferenceAdapter(
        "cursor-ide-snippets-linux",
        ".config/Cursor/User/snippets",
        "cursor/ide-linux/snippets",
        tree=True,
    ),
    # Cursor CLI and user-authored extension points.
    PreferenceAdapter(
        "cursor-cli-settings",
        ".cursor/settings.json",
        "cursor/cli/settings.json",
        format="json",
        config_scope=True,
    ),
    PreferenceAdapter(
        "cursor-cli-config",
        ".cursor/cli-config.json",
        "cursor/cli/cli-config.json",
        format="json",
        config_scope=True,
    ),
    PreferenceAdapter(
        "cursor-cli-config-legacy",
        ".cursor/config.json",
        "cursor/cli/config.json",
        format="json",
        config_scope=True,
    ),
    PreferenceAdapter(
        "cursor-hooks-declaration",
        ".cursor/hooks.json",
        "cursor/hooks.json",
        format="json",
        config_scope=True,
    ),
    PreferenceAdapter("cursor-hooks", ".cursor/hooks", "cursor/hooks", tree=True),
    PreferenceAdapter(
        "cursor-commands", ".cursor/commands", "cursor/commands", tree=True
    ),
    PreferenceAdapter(
        "cursor-user-skills", ".cursor/skills", "cursor/skills", tree=True
    ),
    PreferenceAdapter(
        "shared-agent-skills", ".agents/skills", "agents/skills", tree=True
    ),
    PreferenceAdapter(
        "cursor-legacy-rules", ".cursor/rules", "cursor/rules", tree=True
    ),
    PreferenceAdapter(
        "cursor-mcp",
        ".cursor/mcp.json",
        "cursor/mcp.json",
        format="json",
        kind="json-fragment",
        mcp_only=True,
        config_scope=True,
    ),
    PreferenceAdapter(
        "cursor-global-user-rule",
        CURSOR_STATE_DB,
        "cursor/global-user-rule",
        format="text",
        kind="sqlite-key",
    ),
    # Claude Code.
    PreferenceAdapter(
        "claude-memory", ".claude/CLAUDE.md", "claude/CLAUDE.md", format="text"
    ),
    PreferenceAdapter(
        "claude-settings",
        ".claude/settings.json",
        "claude/settings.json",
        format="json",
        config_scope=True,
    ),
    PreferenceAdapter(
        "claude-skills", ".claude/skills", "claude/skills", tree=True
    ),
    PreferenceAdapter(
        "claude-commands", ".claude/commands", "claude/commands", tree=True
    ),
    PreferenceAdapter(
        "claude-agents", ".claude/agents", "claude/agents", tree=True
    ),
    PreferenceAdapter("claude-hooks", ".claude/hooks", "claude/hooks", tree=True),
    PreferenceAdapter(
        "claude-mcp",
        ".claude.json",
        "claude/mcp.json",
        format="json",
        kind="json-fragment",
        mcp_only=True,
        config_scope=True,
    ),
    # Codex.  ~/.agents/skills is listed above; ~/.codex/skills is legacy.
    PreferenceAdapter(
        "codex-instructions",
        ".codex/AGENTS.md",
        "codex/AGENTS.md",
        format="text",
    ),
    PreferenceAdapter(
        "codex-config",
        ".codex/config.toml",
        "codex/config.toml",
        format="toml",
        config_scope=True,
    ),
    PreferenceAdapter(
        "codex-legacy-skills", ".codex/skills", "codex/skills", tree=True
    ),
    PreferenceAdapter("codex-hooks", ".codex/hooks", "codex/hooks", tree=True),
    PreferenceAdapter("codex-rules", ".codex/rules", "codex/rules", tree=True),
    PreferenceAdapter(
        "codex-prompts", ".codex/prompts", "codex/prompts", tree=True
    ),
)


def curated_adapters() -> Tuple[PreferenceAdapter, ...]:
    """Return the immutable built-in adapter catalog."""

    return DEFAULT_ADAPTERS


def _slash(path: str) -> str:
    return path.replace(os.sep, "/")


def _safe_relative(path: str) -> str:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError("empty or invalid relative path")
    value = path.replace("\\", "/")
    if value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        raise ValueError("absolute path is not allowed: %r" % path)
    normalized = posixpath.normpath(value)
    if normalized in (".", "..") or normalized.startswith("../"):
        raise ValueError("path traversal is not allowed: %r" % path)
    return normalized


def _safe_join(root: str, relative: str) -> str:
    rel = _safe_relative(relative)
    root_abs = os.path.abspath(root)
    result = os.path.abspath(os.path.join(root_abs, *rel.split("/")))
    if os.path.commonpath((root_abs, result)) != root_abs:
        raise ValueError("path escapes root: %r" % relative)
    return result


def _existing_parent(path: str) -> str:
    current = os.path.abspath(os.path.dirname(path))
    while not os.path.lexists(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current


def _assert_parent_inside_home(home: str, path: str) -> None:
    home_real = os.path.realpath(os.path.abspath(home))
    parent_real = os.path.realpath(_existing_parent(path))
    if os.path.commonpath((home_real, parent_real)) != home_real:
        raise ValueError("destination parent escapes home through a symlink")


def _blocked_path_reason(relative: str) -> Optional[str]:
    parts = [part for part in relative.replace("\\", "/").split("/") if part]
    for part in parts:
        lower = part.lower()
        if lower in _PRUNE_NAMES:
            if lower in {".system", ".managed", "managed"}:
                return "managed_content"
            if lower in {"plugins", "runtime"}:
                return "plugin_runtime"
            if lower in {"vendor", "node_modules", "site-packages", "dist"}:
                return "vendor_import"
            if "cache" in lower or lower == "__pycache__":
                return "cache"
            if lower in {".git", ".hg", ".svn"}:
                return "repository_metadata"
            return "managed_content"
        if lower == ".ds_store":
            return "metadata"
        if lower == ".env" or lower.startswith(".env."):
            return "secret_path"
        stem = lower.rsplit(".", 1)[0]
        if _AUTH_NAME_RE.search(stem):
            return "secret_path"
    return None


def _is_declared_text(adapter: PreferenceAdapter, path: str) -> bool:
    if not adapter.rewrite_text:
        return False
    if adapter.format in {"json", "toml", "text"}:
        return True
    name = os.path.basename(path)
    return name in _TEXT_NAMES or os.path.splitext(name)[1].lower() in _TEXT_SUFFIXES


def _format_for(adapter: PreferenceAdapter, path: str) -> str:
    if adapter.format != "auto":
        return adapter.format
    suffix = os.path.splitext(path)[1].lower()
    if suffix in {".json", ".jsonc"}:
        return "json"
    if suffix == ".toml":
        return "toml"
    if _is_declared_text(adapter, path):
        return "text"
    return "binary"


def _secret_reference(value: Any) -> bool:
    if value is None or value == "":
        return True
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return bool(
        re.fullmatch(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})", stripped)
        or re.fullmatch(
            r"\$\{(?:env:)?[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}", stripped
        )
        or re.fullmatch(r"\$\{env:[A-Za-z_][A-Za-z0-9_]*\}", stripped)
    )


def _looks_like_secret_text(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_TEXT_RES)


def _machine_local_path(path: Sequence[str]) -> bool:
    for component in path:
        for token in re.split(r"[./]", component):
            snake = re.sub(r"(?<!^)(?=[A-Z])", "_", token)
            normalized = snake.lower().replace("-", "_")
            if normalized in _MACHINE_LOCAL_KEYS:
                return True
            if normalized.startswith("project_") or normalized.startswith(
                "workspace_"
            ):
                return True
            if normalized.endswith("_trust") or normalized.endswith("_state"):
                return True
    return False


def _preserve_path(path: Sequence[str]) -> bool:
    if not path:
        return False
    key = path[-1]
    return _machine_local_path(path) or bool(_SECRET_KEY_RE.search(key))


def _blocked_record(
    adapter: PreferenceAdapter,
    logical: str,
    reason: str,
    field: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "adapter": adapter.name,
        "path": logical,
        "reason": reason,
    }
    if field:
        result["field"] = ".".join(field)
    return result


def _sanitize_structure(
    value: Any,
    adapter: PreferenceAdapter,
    logical: str,
    blocked: List[Dict[str, Any]],
    path: Tuple[str, ...] = (),
) -> Any:
    if isinstance(value, dict):
        result = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            child_path = path + (key,)
            if adapter.config_scope and _machine_local_path(child_path):
                blocked.append(
                    _blocked_record(adapter, logical, "machine_local_field", child_path)
                )
                continue
            if _SECRET_KEY_RE.search(key) and not _secret_reference(item):
                blocked.append(
                    _blocked_record(adapter, logical, "secret_field", child_path)
                )
                continue
            clean = _sanitize_structure(
                item, adapter, logical, blocked, child_path
            )
            if clean is not _MISSING:
                result[key] = clean
        return result
    if isinstance(value, list):
        result_list = []
        index = 0
        while index < len(value):
            item = value[index]
            if isinstance(item, str) and _SECRET_ARGUMENT_RE.search(item):
                if "=" in item:
                    _flag, possible_value = item.split("=", 1)
                    if not _secret_reference(possible_value):
                        blocked.append(
                            _blocked_record(
                                adapter,
                                logical,
                                "secret_argument",
                                path + (str(index),),
                            )
                        )
                        index += 1
                        continue
                elif index + 1 < len(value):
                    possible_value = value[index + 1]
                    if not _secret_reference(possible_value):
                        blocked.append(
                            _blocked_record(
                                adapter,
                                logical,
                                "secret_argument",
                                path + (str(index),),
                            )
                        )
                        index += 2
                        continue
            clean = _sanitize_structure(
                item, adapter, logical, blocked, path + (str(index),)
            )
            if clean is not _MISSING:
                result_list.append(clean)
            index += 1
        return result_list
    if isinstance(value, str) and _looks_like_secret_text(value):
        if _secret_reference(value):
            return value
        blocked.append(_blocked_record(adapter, logical, "literal_secret", path))
        return _MISSING
    return value


def _strip_json_comments(text: str) -> str:
    output: List[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < len(text):
            following = text[index + 1]
            if following == "/":
                index += 2
                while index < len(text) and text[index] not in "\r\n":
                    index += 1
                continue
            if following == "*":
                index += 2
                while index + 1 < len(text) and text[index : index + 2] != "*/":
                    index += 1
                index = min(len(text), index + 2)
                continue
        output.append(char)
        index += 1
    without_comments = "".join(output)
    return re.sub(r",(\s*[}\]])", r"\1", without_comments)


def json_loads_compatible(text: str) -> Any:
    """Parse JSON, accepting JSONC comments and trailing commas."""

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_strip_json_comments(text))


class _TomlBare(str):
    """A TOML scalar the compatibility parser should emit without quotes."""


def _split_unquoted(text: str, delimiter: str) -> List[str]:
    parts: List[str] = []
    start = 0
    quote_char: Optional[str] = None
    escaped = False
    square = curly = 0
    for index, char in enumerate(text):
        if quote_char is not None:
            if escaped:
                escaped = False
            elif char == "\\" and quote_char == '"':
                escaped = True
            elif char == quote_char:
                quote_char = None
            continue
        if char in {"'", '"'}:
            quote_char = char
        elif char == "[":
            square += 1
        elif char == "]":
            square -= 1
        elif char == "{":
            curly += 1
        elif char == "}":
            curly -= 1
        elif char == delimiter and square == 0 and curly == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return parts


def _strip_toml_comment(line: str) -> str:
    quote_char: Optional[str] = None
    escaped = False
    for index, char in enumerate(line):
        if quote_char is not None:
            if escaped:
                escaped = False
            elif char == "\\" and quote_char == '"':
                escaped = True
            elif char == quote_char:
                quote_char = None
            continue
        if char in {"'", '"'}:
            quote_char = char
        elif char == "#":
            return line[:index]
    return line


def _toml_key_parts(text: str) -> List[str]:
    parts = _split_unquoted(text, ".")
    result = []
    for part in parts:
        part = part.strip()
        if not part:
            raise ValueError("empty TOML key")
        if part[0:1] in {"'", '"'}:
            value = ast.literal_eval(part)
            if not isinstance(value, str):
                raise ValueError("invalid TOML key")
            result.append(value)
        else:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", part):
                raise ValueError("unsupported TOML key: %s" % part)
            result.append(part)
    return result


def _toml_value(text: str) -> Any:
    value = text.strip()
    if not value:
        raise ValueError("missing TOML value")
    if value[0:1] in {"'", '"'}:
        parsed = ast.literal_eval(value)
        if not isinstance(parsed, str):
            raise ValueError("invalid TOML string")
        return parsed
    lower = value.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [] if not inner else [_toml_value(x) for x in _split_unquoted(inner, ",")]
    if value.startswith("{") and value.endswith("}"):
        result: Dict[str, Any] = {}
        inner = value[1:-1].strip()
        if not inner:
            return result
        for item in _split_unquoted(inner, ","):
            pair = _split_unquoted(item, "=")
            if len(pair) != 2:
                raise ValueError("invalid TOML inline table")
            _assign_toml(result, _toml_key_parts(pair[0]), _toml_value(pair[1]))
        return result
    compact = value.replace("_", "")
    try:
        if re.fullmatch(r"[+-]?\d+", compact):
            return int(compact)
        if re.fullmatch(
            r"[+-]?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[eE][+-]?\d+)?", compact
        ):
            return float(compact)
    except ValueError:
        pass
    # Datetimes and other valid-but-uninteresting TOML scalars survive
    # round-tripping instead of turning into quoted strings.
    if re.fullmatch(r"[0-9TtZz:+.\-]+", value):
        return _TomlBare(value)
    raise ValueError("unsupported TOML value: %s" % value[:80])


def _assign_toml(target: Dict[str, Any], parts: Sequence[str], value: Any) -> None:
    current = target
    for part in parts[:-1]:
        existing = current.setdefault(part, {})
        if not isinstance(existing, dict):
            raise ValueError("TOML key conflicts with a scalar")
        current = existing
    leaf = parts[-1]
    if leaf in current:
        raise ValueError("duplicate TOML key: %s" % leaf)
    current[leaf] = value


def _toml_balanced(value: str) -> bool:
    quote_char: Optional[str] = None
    escaped = False
    square = curly = 0
    for char in value:
        if quote_char is not None:
            if escaped:
                escaped = False
            elif char == "\\" and quote_char == '"':
                escaped = True
            elif char == quote_char:
                quote_char = None
            continue
        if char in {"'", '"'}:
            quote_char = char
        elif char == "[":
            square += 1
        elif char == "]":
            square -= 1
        elif char == "{":
            curly += 1
        elif char == "}":
            curly -= 1
    return quote_char is None and square == 0 and curly == 0


def toml_loads_compatible(text: str) -> Dict[str, Any]:
    """Parse the TOML structures used by Codex without third-party packages."""

    root: Dict[str, Any] = {}
    current: Dict[str, Any] = root
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = _strip_toml_comment(lines[index]).strip()
        index += 1
        if not line:
            continue
        if line.startswith("[[") and line.endswith("]]"):
            parts = _toml_key_parts(line[2:-2].strip())
            parent = root
            for part in parts[:-1]:
                child = parent.setdefault(part, {})
                if not isinstance(child, dict):
                    raise ValueError("array-table parent is not a table")
                parent = child
            array = parent.setdefault(parts[-1], [])
            if not isinstance(array, list):
                raise ValueError("array-table conflicts with existing value")
            item: Dict[str, Any] = {}
            array.append(item)
            current = item
            continue
        if line.startswith("[") and line.endswith("]"):
            parts = _toml_key_parts(line[1:-1].strip())
            current = root
            for part in parts:
                child = current.setdefault(part, {})
                if not isinstance(child, dict):
                    raise ValueError("table conflicts with existing value")
                current = child
            continue
        pair = _split_unquoted(line, "=")
        if len(pair) < 2:
            raise ValueError("invalid TOML assignment")
        key_text = pair[0]
        value_text = "=".join(pair[1:]).strip()
        while not _toml_balanced(value_text) and index < len(lines):
            value_text += "\n" + _strip_toml_comment(lines[index]).strip()
            index += 1
        _assign_toml(current, _toml_key_parts(key_text), _toml_value(value_text))
    return root


def _toml_key(key: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
        return key
    return json.dumps(key, ensure_ascii=False)


def _toml_scalar(value: Any) -> str:
    if isinstance(value, _TomlBare):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list) and not any(isinstance(item, dict) for item in value):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    if isinstance(value, dict):
        items = (
            "%s = %s" % (_toml_key(str(key)), _toml_scalar(item))
            for key, item in sorted(value.items())
        )
        return "{ " + ", ".join(items) + " }"
    raise ValueError("unsupported TOML scalar: %r" % (value,))


def toml_dumps_compatible(value: Mapping[str, Any]) -> str:
    """Write deterministic TOML for parsed preference structures."""

    lines: List[str] = []

    def emit(table: Mapping[str, Any], path: Tuple[str, ...]) -> None:
        scalars = []
        subtables = []
        arrays = []
        for key, item in sorted(table.items()):
            if isinstance(item, dict):
                subtables.append((str(key), item))
            elif isinstance(item, list) and item and all(
                isinstance(element, dict) for element in item
            ):
                arrays.append((str(key), item))
            else:
                scalars.append((str(key), item))
        for key, item in scalars:
            lines.append("%s = %s" % (_toml_key(key), _toml_scalar(item)))
        for key, item in subtables:
            if lines and lines[-1] != "":
                lines.append("")
            child_path = path + (key,)
            lines.append("[%s]" % ".".join(_toml_key(part) for part in child_path))
            emit(item, child_path)
        for key, items in arrays:
            child_path = path + (key,)
            for item in items:
                if lines and lines[-1] != "":
                    lines.append("")
                lines.append(
                    "[[%s]]" % ".".join(_toml_key(part) for part in child_path)
                )
                emit(item, child_path)

    emit(value, ())
    return "\n".join(lines).rstrip() + "\n"


def _structured_payload(
    adapter: PreferenceAdapter,
    logical: str,
    raw: bytes,
    blocked: List[Dict[str, Any]],
) -> Optional[bytes]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        blocked.append(_blocked_record(adapter, logical, "invalid_utf8"))
        return None
    try:
        if adapter.format == "json":
            parsed = json_loads_compatible(text)
        else:
            parsed = toml_loads_compatible(text)
    except (ValueError, TypeError, json.JSONDecodeError, SyntaxError):
        blocked.append(_blocked_record(adapter, logical, "structured_parse_error"))
        return None
    if adapter.mcp_only:
        if not isinstance(parsed, dict):
            blocked.append(_blocked_record(adapter, logical, "invalid_mcp_document"))
            return None
        candidates = ("mcpServers", "mcp_servers", "mcp")
        selected = {key: parsed[key] for key in candidates if key in parsed}
        if not selected:
            return None
        parsed = selected
    clean = _sanitize_structure(parsed, adapter, logical, blocked)
    if clean is _MISSING:
        return None
    if adapter.format == "json":
        return (
            json.dumps(clean, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")
    if not isinstance(clean, dict):
        blocked.append(_blocked_record(adapter, logical, "invalid_toml_document"))
        return None
    return toml_dumps_compatible(clean).encode("utf-8")


def _prepare_payload(
    adapter: PreferenceAdapter,
    logical: str,
    raw: bytes,
    path: str,
    blocked: List[Dict[str, Any]],
) -> Optional[bytes]:
    file_format = _format_for(adapter, path)
    if file_format in _STRUCTURED_SUFFIXES or file_format in {"json", "toml"}:
        structured_adapter = adapter
        if adapter.format == "auto":
            structured_adapter = PreferenceAdapter(
                name=adapter.name,
                source=adapter.source,
                canonical=adapter.canonical,
                tree=adapter.tree,
                format="toml" if file_format == "toml" else "json",
                rewrite_text=adapter.rewrite_text,
                kind=adapter.kind,
                mcp_only=adapter.mcp_only,
                config_scope=adapter.config_scope,
                exclude=adapter.exclude,
                max_file_size=adapter.max_file_size,
            )
        return _structured_payload(structured_adapter, logical, raw, blocked)
    if _is_declared_text(adapter, path):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            blocked.append(_blocked_record(adapter, logical, "invalid_utf8"))
            return None
        if _looks_like_secret_text(text):
            blocked.append(_blocked_record(adapter, logical, "literal_secret"))
            return None
    return raw


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _adapter_root_path(home: str, adapter: PreferenceAdapter) -> str:
    source = _safe_join(home, adapter.source)
    return source if adapter.tree else os.path.dirname(source)


def _entry_destination(
    adapter: PreferenceAdapter, relative: Optional[str] = None
) -> Tuple[str, str]:
    if relative is None:
        return adapter.canonical, adapter.source
    rel = _safe_relative(relative)
    return (
        _safe_relative(adapter.canonical + "/" + rel),
        _safe_relative(adapter.source + "/" + rel),
    )


def _scan_regular_path(
    home: str,
    adapter: PreferenceAdapter,
    path: str,
    logical: str,
    destination: str,
    adapter_root: str,
    max_file_size: int,
    blocked: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[bytes]]:
    home_real = os.path.realpath(os.path.abspath(home))
    root_real = os.path.realpath(adapter_root)
    try:
        root_inside_home = os.path.commonpath((home_real, root_real)) == home_real
    except ValueError:
        root_inside_home = False
    if not root_inside_home:
        blocked.append(_blocked_record(adapter, logical, "adapter_root_escape"))
        return None, None
    try:
        info = os.lstat(path)
    except OSError:
        return None, None
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        if os.path.isabs(target):
            blocked.append(_blocked_record(adapter, logical, "symlink_escape"))
            return None, None
        resolved = os.path.realpath(os.path.join(os.path.dirname(path), target))
        root_real = os.path.realpath(adapter_root)
        try:
            inside = os.path.commonpath((root_real, resolved)) == root_real
        except ValueError:
            inside = False
        if not inside:
            blocked.append(_blocked_record(adapter, logical, "symlink_escape"))
            return None, None
        data = target.encode("utf-8", "surrogateescape")
        entry = {
            "adapter": adapter.name,
            "destination": destination,
            "kind": "symlink",
            "format": "symlink",
            "rewrite_text": False,
            "sha256": _sha256(data),
            "size": len(data),
            "mode": stat.S_IMODE(info.st_mode) & 0o777,
        }
        return entry, data
    if not stat.S_ISREG(info.st_mode):
        blocked.append(_blocked_record(adapter, logical, "unsupported_file_type"))
        return None, None
    if info.st_size > max_file_size:
        blocked.append(_blocked_record(adapter, logical, "size_limit"))
        return None, None
    try:
        with open(path, "rb") as handle:
            raw = handle.read(max_file_size + 1)
    except OSError:
        blocked.append(_blocked_record(adapter, logical, "read_error"))
        return None, None
    if len(raw) > max_file_size:
        blocked.append(_blocked_record(adapter, logical, "size_limit"))
        return None, None
    payload = _prepare_payload(adapter, logical, raw, path, blocked)
    if payload is None:
        return None, None
    entry = {
        "adapter": adapter.name,
        "destination": destination,
        "kind": adapter.kind,
        "format": _format_for(adapter, path),
        "rewrite_text": _is_declared_text(adapter, path),
        "sha256": _sha256(payload),
        "size": len(payload),
        "mode": stat.S_IMODE(info.st_mode) & 0o777,
    }
    if adapter.kind == "sqlite-key":
        entry["sqlite_key"] = CURSOR_USER_RULE_KEY
    return entry, payload


def _sqlite_uri(path: str) -> str:
    return "file:%s?mode=ro" % quote(os.path.abspath(path), safe="/")


def _scan_cursor_rule(
    home: str,
    adapter: PreferenceAdapter,
    cursor_closed: bool,
    max_file_size: int,
    blocked: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[bytes]]:
    path = _safe_join(home, adapter.source)
    if not os.path.isfile(path):
        return None, None
    parent_real = os.path.realpath(os.path.dirname(path))
    home_real = os.path.realpath(os.path.abspath(home))
    try:
        parent_inside_home = (
            os.path.commonpath((home_real, parent_real)) == home_real
        )
    except ValueError:
        parent_inside_home = False
    if not parent_inside_home:
        blocked.append(
            _blocked_record(adapter, adapter.canonical, "adapter_root_escape")
        )
        return None, None
    if not cursor_closed:
        blocked.append(
            _blocked_record(adapter, adapter.canonical, "cursor_must_be_closed")
        )
        return None, None
    try:
        connection = sqlite3.connect(_sqlite_uri(path), uri=True, timeout=5)
        try:
            row = connection.execute(
                "SELECT value FROM ItemTable WHERE key = ?", (CURSOR_USER_RULE_KEY,)
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        blocked.append(_blocked_record(adapter, adapter.canonical, "sqlite_read_error"))
        return None, None
    if row is None:
        return None, None
    value = row[0]
    if isinstance(value, bytes):
        payload = value
        value_type = "blob"
    else:
        payload = str(value).encode("utf-8")
        value_type = "text"
    if len(payload) > max_file_size:
        blocked.append(_blocked_record(adapter, adapter.canonical, "size_limit"))
        return None, None
    entry = {
        "adapter": adapter.name,
        "destination": adapter.source,
        "kind": "sqlite-key",
        "format": "text",
        "rewrite_text": value_type == "text",
        "sha256": _sha256(payload),
        "size": len(payload),
        "mode": None,
        "sqlite_key": CURSOR_USER_RULE_KEY,
        "value_type": value_type,
    }
    return entry, payload


def scan_preferences(
    home: str,
    *,
    adapters: Optional[Sequence[PreferenceAdapter]] = None,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
    max_total_size: int = DEFAULT_MAX_TOTAL_SIZE,
    cursor_closed: bool = False,
    exclude: Optional[Sequence[str]] = None,
) -> Tuple[Dict[str, Any], Dict[str, bytes]]:
    """Inventory curated preferences beneath ``home``.

    The returned manifest contains hashes and metadata only.  The second
    dictionary contains bytes keyed by canonical manifest path.
    """

    if max_file_size <= 0 or max_total_size <= 0:
        raise ValueError("size limits must be positive")
    home_abs = os.path.abspath(home)
    selected = DEFAULT_ADAPTERS if adapters is None else tuple(adapters)
    exclusions = tuple(exclude or ())
    entries: Dict[str, Dict[str, Any]] = {}
    payloads: Dict[str, bytes] = {}
    blocked: List[Dict[str, Any]] = []
    total = 0

    def add(
        logical: str, entry: Optional[Dict[str, Any]], payload: Optional[bytes]
    ) -> None:
        nonlocal total
        if entry is None or payload is None:
            return
        if logical in entries:
            blocked.append(
                {
                    "adapter": entry["adapter"],
                    "path": logical,
                    "reason": "canonical_collision",
                }
            )
            return
        if total + len(payload) > max_total_size:
            blocked.append(
                {
                    "adapter": entry["adapter"],
                    "path": logical,
                    "reason": "total_size_limit",
                }
            )
            return
        entries[logical] = entry
        payloads[logical] = payload
        total += len(payload)

    for adapter in selected:
        adapter_exclusions = exclusions + tuple(adapter.exclude)
        adapter_file_size = min(
            max_file_size,
            adapter.max_file_size
            if adapter.max_file_size is not None
            else max_file_size,
        )
        if any(
            fnmatch.fnmatch(adapter.name, pattern)
            or fnmatch.fnmatch(adapter.source, pattern)
            or fnmatch.fnmatch(adapter.canonical, pattern)
            for pattern in adapter_exclusions
        ):
            blocked.append(
                _blocked_record(adapter, adapter.canonical, "profile_exclusion")
            )
            continue
        source = _safe_join(home_abs, adapter.source)
        if adapter.kind == "sqlite-key":
            entry, payload = _scan_cursor_rule(
                home_abs, adapter, cursor_closed, adapter_file_size, blocked
            )
            add(adapter.canonical, entry, payload)
            continue
        if not adapter.tree:
            reason = _blocked_path_reason(adapter.source)
            if reason:
                blocked.append(_blocked_record(adapter, adapter.canonical, reason))
                continue
            entry, payload = _scan_regular_path(
                home_abs,
                adapter,
                source,
                adapter.canonical,
                adapter.source,
                _adapter_root_path(home_abs, adapter),
                adapter_file_size,
                blocked,
            )
            add(adapter.canonical, entry, payload)
            continue
        if not os.path.isdir(source) or os.path.islink(source):
            if os.path.islink(source):
                blocked.append(
                    _blocked_record(adapter, adapter.canonical, "symlink_tree_root")
                )
            continue
        root_real = os.path.realpath(source)
        home_real = os.path.realpath(home_abs)
        if os.path.commonpath((home_real, root_real)) != home_real:
            blocked.append(
                _blocked_record(adapter, adapter.canonical, "tree_root_escape")
            )
            continue
        for directory, dirnames, filenames in os.walk(
            source, topdown=True, followlinks=False
        ):
            relative_directory = os.path.relpath(directory, source)
            if relative_directory == ".":
                relative_directory = ""
            kept_dirs = []
            for dirname in sorted(dirnames):
                rel = _slash(os.path.join(relative_directory, dirname))
                logical_dir = _entry_destination(adapter, rel)[0]
                if any(
                    fnmatch.fnmatch(rel, pattern)
                    or fnmatch.fnmatch(logical_dir, pattern)
                    for pattern in adapter_exclusions
                ):
                    blocked.append(
                        _blocked_record(
                            adapter, logical_dir, "profile_exclusion"
                        )
                    )
                    continue
                reason = _blocked_path_reason(rel)
                full = os.path.join(directory, dirname)
                if reason:
                    blocked.append(
                        _blocked_record(
                            adapter,
                            _entry_destination(adapter, rel)[0],
                            reason,
                        )
                    )
                    continue
                if os.path.islink(full):
                    logical, destination = _entry_destination(adapter, rel)
                    entry, payload = _scan_regular_path(
                        home_abs,
                        adapter,
                        full,
                        logical,
                        destination,
                        source,
                        adapter_file_size,
                        blocked,
                    )
                    add(logical, entry, payload)
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs
            for filename in sorted(filenames):
                rel = _slash(os.path.join(relative_directory, filename))
                logical, destination = _entry_destination(adapter, rel)
                if any(
                    fnmatch.fnmatch(rel, pattern)
                    or fnmatch.fnmatch(logical, pattern)
                    for pattern in adapter_exclusions
                ):
                    blocked.append(
                        _blocked_record(adapter, logical, "profile_exclusion")
                    )
                    continue
                reason = _blocked_path_reason(rel)
                if reason:
                    blocked.append(_blocked_record(adapter, logical, reason))
                    continue
                entry, payload = _scan_regular_path(
                    home_abs,
                    adapter,
                    os.path.join(directory, filename),
                    logical,
                    destination,
                    source,
                    adapter_file_size,
                    blocked,
                )
                add(logical, entry, payload)

    manifest = {
        "version": MANIFEST_VERSION,
        "entries": dict(sorted(entries.items())),
        "blocked": sorted(
            blocked,
            key=lambda item: (
                item.get("path", ""),
                item.get("reason", ""),
                item.get("field", ""),
            ),
        ),
        "total_size": total,
    }
    return manifest, dict(sorted(payloads.items()))


def build_manifest(
    home: str,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Return only the inventory manifest (convenience for remote listing)."""

    return scan_preferences(home, **kwargs)[0]


inventory_preferences = build_manifest
collect_preferences = scan_preferences


def encode_payloads(payloads: Mapping[str, bytes]) -> Dict[str, str]:
    """Encode payloads for JSON/SSH transport."""

    return {
        key: base64.b64encode(bytes(value)).decode("ascii")
        for key, value in sorted(payloads.items())
    }


def decode_payloads(payloads: Mapping[str, str]) -> Dict[str, bytes]:
    """Decode payloads produced by :func:`encode_payloads`."""

    result = {}
    for key, value in payloads.items():
        _safe_relative(key)
        result[key] = base64.b64decode(value.encode("ascii"), validate=True)
    return result


def rewrite_declared_text(
    entry: Mapping[str, Any], payload: bytes, source_home: str, destination_home: str
) -> bytes:
    """Rewrite homes only when an adapter explicitly declared text content."""

    if not entry.get("rewrite_text") or source_home == destination_home:
        return payload
    text = payload.decode("utf-8")
    # Import lazily so this module remains usable by standalone inventory
    # tooling that only needs its stdlib-only scanner.
    from .rewrite import HomeRewriter

    rewritten, _count = HomeRewriter(source_home, destination_home).text(text)
    return rewritten.encode("utf-8")


def rewrite_snapshot(
    manifest: Mapping[str, Any],
    payloads: Mapping[str, bytes],
    source_home: str,
    destination_home: str,
) -> Tuple[Dict[str, Any], Dict[str, bytes]]:
    """Return a rewritten copy of a manifest/payload pair."""

    result_manifest = copy.deepcopy(dict(manifest))
    result_payloads: Dict[str, bytes] = {}
    entries = result_manifest.get("entries", {})
    for key, entry in entries.items():
        data = bytes(payloads[key])
        rewritten = rewrite_declared_text(
            entry, data, source_home, destination_home
        )
        entry["sha256"] = _sha256(rewritten)
        entry["size"] = len(rewritten)
        result_payloads[key] = rewritten
    result_manifest["total_size"] = sum(map(len, result_payloads.values()))
    return result_manifest, result_payloads


def _same(value_a: Any, value_b: Any) -> bool:
    if value_a is _MISSING or value_b is _MISSING:
        return value_a is value_b
    return value_a == value_b


def semantic_three_way_merge(
    base: Any,
    local: Any,
    incoming: Any,
    *,
    preserve_machine_local: bool = True,
) -> Tuple[Any, List[str]]:
    """Recursively merge dictionaries and report concurrently edited paths.

    Lists are atomic because reordering and command arrays have no generally
    safe element identity.  Deletions are intentionally not propagated.
    """

    conflicts: List[str] = []

    def merge(old: Any, ours: Any, theirs: Any, path: Tuple[str, ...]) -> Any:
        if preserve_machine_local and _preserve_path(path) and ours is not _MISSING:
            return copy.deepcopy(ours)
        if _same(ours, theirs):
            return copy.deepcopy(ours)
        if theirs is _MISSING:
            return copy.deepcopy(ours)
        if ours is _MISSING:
            # Non-destructive behavior resurrects data rather than accepting a
            # deletion from one side.
            return copy.deepcopy(theirs)
        if _same(ours, old):
            return copy.deepcopy(theirs)
        if _same(theirs, old):
            return copy.deepcopy(ours)
        if isinstance(ours, dict) and isinstance(theirs, dict):
            old_dict = old if isinstance(old, dict) else {}
            result = {}
            keys = set(old_dict) | set(ours) | set(theirs)
            for key in sorted(keys, key=str):
                value = merge(
                    old_dict.get(key, _MISSING),
                    ours.get(key, _MISSING),
                    theirs.get(key, _MISSING),
                    path + (str(key),),
                )
                if value is not _MISSING:
                    result[key] = value
            return result
        conflicts.append(".".join(path) if path else "$")
        return copy.deepcopy(ours)

    return merge(base, local, incoming, ()), conflicts


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    raise TypeError("structured merge values must be bytes or text")


def three_way_merge_json(
    base: Any, local: Any, incoming: Any
) -> Tuple[Optional[bytes], List[str]]:
    """Three-way merge JSON/JSONC bytes and return canonical JSON bytes."""

    try:
        parsed_base = json_loads_compatible(_as_text(base))
        parsed_local = json_loads_compatible(_as_text(local))
        parsed_incoming = json_loads_compatible(_as_text(incoming))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None, ["$parse"]
    merged, conflicts = semantic_three_way_merge(
        parsed_base, parsed_local, parsed_incoming
    )
    data = (
        json.dumps(merged, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    return data, conflicts


def three_way_merge_toml(
    base: Any, local: Any, incoming: Any
) -> Tuple[Optional[bytes], List[str]]:
    """Three-way merge compatible TOML bytes and return deterministic TOML."""

    try:
        parsed_base = toml_loads_compatible(_as_text(base))
        parsed_local = toml_loads_compatible(_as_text(local))
        parsed_incoming = toml_loads_compatible(_as_text(incoming))
    except (ValueError, TypeError, UnicodeDecodeError, SyntaxError):
        return None, ["$parse"]
    merged, conflicts = semantic_three_way_merge(
        parsed_base, parsed_local, parsed_incoming
    )
    if not isinstance(merged, dict):
        return None, ["$parse"]
    return toml_dumps_compatible(merged).encode("utf-8"), conflicts


def _entry_state(entry: Optional[Mapping[str, Any]]) -> Any:
    if entry is None:
        return None
    return (
        entry.get("sha256"),
        entry.get("mode"),
        entry.get("kind"),
        entry.get("destination"),
    )


def _payload_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _merged_mode(
    base: Optional[Mapping[str, Any]],
    local: Mapping[str, Any],
    incoming: Mapping[str, Any],
) -> Tuple[Any, bool]:
    old = None if base is None else base.get("mode")
    ours = local.get("mode")
    theirs = incoming.get("mode")
    if ours == theirs:
        return ours, False
    if ours == old:
        return theirs, False
    if theirs == old:
        return ours, False
    return ours, True


def plan_preferences(
    base_manifest: Mapping[str, Any],
    local_manifest: Mapping[str, Any],
    incoming_manifest: Mapping[str, Any],
    *,
    base_payloads: Optional[Mapping[str, bytes]] = None,
    local_payloads: Optional[Mapping[str, bytes]] = None,
    incoming_payloads: Optional[Mapping[str, bytes]] = None,
) -> Dict[str, Any]:
    """Build a hash-based, non-destructive three-way preference plan."""

    base_entries = dict(base_manifest.get("entries", {}))
    local_entries = dict(local_manifest.get("entries", {}))
    incoming_entries = dict(incoming_manifest.get("entries", {}))
    base_data = base_payloads or {}
    local_data = local_payloads or {}
    incoming_data = incoming_payloads or {}
    actions: List[Dict[str, Any]] = []
    keys = sorted(set(base_entries) | set(local_entries) | set(incoming_entries))

    for key in keys:
        old = base_entries.get(key)
        ours = local_entries.get(key)
        theirs = incoming_entries.get(key)
        if theirs is None:
            actions.append({"key": key, "op": "keep", "reason": "no_incoming"})
            continue
        incoming_bytes = incoming_data.get(key)
        if ours is None:
            if incoming_bytes is None:
                actions.append(
                    {
                        "key": key,
                        "op": "conflict",
                        "reason": "incoming_payload_missing",
                        "entry": copy.deepcopy(theirs),
                    }
                )
            else:
                actions.append(
                    {
                        "key": key,
                        "op": "apply",
                        "reason": "missing_local",
                        "entry": copy.deepcopy(theirs),
                        "payload": _payload_b64(incoming_bytes),
                    }
                )
            continue
        if _entry_state(ours) == _entry_state(theirs):
            actions.append({"key": key, "op": "keep", "reason": "identical"})
            continue
        if _entry_state(old) == _entry_state(ours):
            if incoming_bytes is None:
                actions.append(
                    {
                        "key": key,
                        "op": "conflict",
                        "reason": "incoming_payload_missing",
                        "entry": copy.deepcopy(theirs),
                    }
                )
            else:
                actions.append(
                    {
                        "key": key,
                        "op": "apply",
                        "reason": "incoming_only_change",
                        "entry": copy.deepcopy(theirs),
                        "payload": _payload_b64(incoming_bytes),
                    }
                )
            continue
        if _entry_state(old) == _entry_state(theirs):
            actions.append({"key": key, "op": "keep", "reason": "local_only_change"})
            continue

        old_bytes = base_data.get(key)
        local_bytes = local_data.get(key)
        file_format = theirs.get("format")
        merge_result: Optional[bytes] = None
        conflict_paths: List[str] = []
        if (
            old_bytes is not None
            and local_bytes is not None
            and incoming_bytes is not None
            and file_format in {"json", "toml"}
            and ours.get("kind") == theirs.get("kind")
        ):
            if file_format == "json":
                merge_result, conflict_paths = three_way_merge_json(
                    old_bytes, local_bytes, incoming_bytes
                )
            else:
                merge_result, conflict_paths = three_way_merge_toml(
                    old_bytes, local_bytes, incoming_bytes
                )
        else:
            conflict_paths = ["$"]
        merged_mode, mode_conflict = _merged_mode(old, ours, theirs)
        if mode_conflict:
            conflict_paths.append("$mode")
        if merge_result is not None and not conflict_paths:
            merged_entry = copy.deepcopy(theirs)
            merged_entry["sha256"] = _sha256(merge_result)
            merged_entry["size"] = len(merge_result)
            merged_entry["mode"] = merged_mode
            if (
                merge_result == local_bytes
                and merged_mode == ours.get("mode")
                and ours.get("kind") == merged_entry.get("kind")
            ):
                actions.append(
                    {"key": key, "op": "keep", "reason": "semantic_local_result"}
                )
            else:
                actions.append(
                    {
                        "key": key,
                        "op": "apply",
                        "reason": "semantic_merge",
                        "entry": merged_entry,
                        "payload": _payload_b64(merge_result),
                    }
                )
            continue
        conflict: Dict[str, Any] = {
            "key": key,
            "op": "conflict",
            "reason": "concurrent_change",
            "entry": copy.deepcopy(theirs),
            "conflict_paths": sorted(set(conflict_paths)),
        }
        if incoming_bytes is not None:
            conflict["inbox_payload"] = _payload_b64(incoming_bytes)
        if merge_result is not None:
            conflict["candidate_payload"] = _payload_b64(merge_result)
        actions.append(conflict)

    return {
        "version": MANIFEST_VERSION,
        "actions": actions,
        "counts": {
            operation: sum(1 for action in actions if action["op"] == operation)
            for operation in ("apply", "keep", "conflict")
        },
    }


plan_three_way = plan_preferences


def _adapter_by_name(
    name: str,
    adapters: Optional[Sequence[PreferenceAdapter]] = None,
) -> PreferenceAdapter:
    for adapter in (DEFAULT_ADAPTERS if adapters is None else adapters):
        if adapter.name == name:
            return adapter
    raise ValueError("unknown preference adapter: %s" % name)


def _validate_entry(
    key: str,
    entry: Mapping[str, Any],
    home: str,
    adapters: Optional[Sequence[PreferenceAdapter]] = None,
) -> str:
    canonical = _safe_relative(key)
    destination = _safe_relative(str(entry.get("destination", "")))
    adapter = _adapter_by_name(str(entry.get("adapter", "")), adapters)
    kind = entry.get("kind")
    allowed_kinds = {"file", "symlink"} if adapter.tree else {adapter.kind, "symlink"}
    if kind not in allowed_kinds:
        raise ValueError("entry kind does not match adapter")
    if kind == "sqlite-key" and entry.get("sqlite_key") != CURSOR_USER_RULE_KEY:
        raise ValueError("SQLite key is not allow-listed")
    mode = entry.get("mode")
    if kind != "sqlite-key" and (
        isinstance(mode, bool)
        or not isinstance(mode, int)
        or mode < 0
        or mode > 0o777
    ):
        raise ValueError("unsafe or invalid file mode")
    if adapter.tree:
        prefix = adapter.canonical.rstrip("/") + "/"
        if not canonical.startswith(prefix):
            raise ValueError("canonical path does not match tree adapter")
        suffix = _safe_relative(canonical[len(prefix) :])
        expected = _safe_relative(adapter.source.rstrip("/") + "/" + suffix)
        if destination != expected:
            raise ValueError("destination does not match tree adapter")
    else:
        if canonical != adapter.canonical or destination != adapter.source:
            raise ValueError("entry does not match fixed adapter")
    path = _safe_join(home, destination)
    _assert_parent_inside_home(home, path)
    return path


def _available_path(path: str) -> str:
    if not os.path.lexists(path):
        return path
    for index in range(1, 10000):
        candidate = "%s.%d" % (path, index)
        if not os.path.lexists(candidate):
            return candidate
    raise RuntimeError("could not allocate backup path")


def _atomic_write(path: str, data: bytes, mode: Optional[int] = None) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".chatmesh-", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _atomic_symlink(path: str, target: str) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    temporary = ""
    for _attempt in range(100):
        candidate = os.path.join(
            parent, ".chatmesh-link-" + secrets.token_hex(8)
        )
        try:
            os.symlink(target, candidate)
        except FileExistsError:
            continue
        temporary = candidate
        break
    if not temporary:
        raise FileExistsError("could not allocate temporary symlink")
    try:
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _backup_live(path: str, backup_path: str) -> Optional[str]:
    if not os.path.lexists(path):
        return None
    if os.path.isdir(path) and not os.path.islink(path):
        raise IsADirectoryError(path)
    target = _available_path(backup_path)
    if os.path.islink(path):
        _atomic_symlink(target, os.readlink(path))
    else:
        with open(path, "rb") as handle:
            data = handle.read()
        _atomic_write(target, data, stat.S_IMODE(os.stat(path).st_mode))
    return target


def _overlay_live(local: Any, incoming: Any, path: Tuple[str, ...] = ()) -> Any:
    if _preserve_path(path) and local is not _MISSING:
        return copy.deepcopy(local)
    if isinstance(local, dict) and isinstance(incoming, dict):
        result = copy.deepcopy(local)
        for key, value in incoming.items():
            result[key] = _overlay_live(
                local.get(key, _MISSING), value, path + (str(key),)
            )
        return result
    return copy.deepcopy(incoming)


def _rehydrate_structured(
    path: str, entry: Mapping[str, Any], payload: bytes
) -> bytes:
    if not os.path.isfile(path) or os.path.islink(path):
        return payload
    file_format = entry.get("format")
    if file_format not in {"json", "toml"}:
        return payload
    try:
        with open(path, "rb") as handle:
            live_text = handle.read().decode("utf-8")
        incoming_text = payload.decode("utf-8")
        if file_format == "json":
            live = json_loads_compatible(live_text)
            incoming = json_loads_compatible(incoming_text)
        else:
            live = toml_loads_compatible(live_text)
            incoming = toml_loads_compatible(incoming_text)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, SyntaxError):
        return payload
    merged = _overlay_live(live, incoming)
    if file_format == "json":
        return (
            json.dumps(merged, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")
    if not isinstance(merged, dict):
        return payload
    return toml_dumps_compatible(merged).encode("utf-8")


def _safe_symlink_for_apply(
    home: str, adapter: PreferenceAdapter, path: str, target_bytes: bytes
) -> str:
    target = target_bytes.decode("utf-8", "surrogateescape")
    if os.path.isabs(target):
        raise ValueError("absolute symlink target is not allowed")
    if adapter.tree:
        root = _safe_join(home, adapter.source)
    else:
        root = os.path.dirname(_safe_join(home, adapter.source))
    # realpath resolves existing components and normalizes missing ones.
    resolved = os.path.realpath(os.path.join(os.path.dirname(path), target))
    root_real = os.path.realpath(root)
    if os.path.commonpath((root_real, resolved)) != root_real:
        raise ValueError("symlink target escapes adapter root")
    return target


def _apply_sqlite_value(
    path: str,
    entry: Mapping[str, Any],
    payload: bytes,
    backup_path: str,
    cursor_closed: bool,
) -> Optional[str]:
    if not cursor_closed:
        raise RuntimeError("Cursor must be closed before applying its user rule")
    if entry.get("sqlite_key") != CURSOR_USER_RULE_KEY:
        raise ValueError("SQLite key is not allow-listed")
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    connection = sqlite3.connect(path, timeout=10)
    backup: Optional[str] = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT value FROM ItemTable WHERE key = ?", (CURSOR_USER_RULE_KEY,)
        ).fetchone()
        if row is not None:
            old = row[0]
            old_bytes = old if isinstance(old, bytes) else str(old).encode("utf-8")
            backup = _available_path(backup_path)
            _atomic_write(backup, old_bytes, 0o600)
        value: Any = payload
        if entry.get("value_type") != "blob":
            value = payload.decode("utf-8")
        if row is None:
            connection.execute(
                "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
                (CURSOR_USER_RULE_KEY, value),
            )
        else:
            connection.execute(
                "UPDATE ItemTable SET value = ? WHERE key = ?",
                (value, CURSOR_USER_RULE_KEY),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return backup


def _write_inbox(
    inbox_dir: str,
    key: str,
    payload_b64: str,
    metadata: Mapping[str, Any],
) -> str:
    data = base64.b64decode(payload_b64.encode("ascii"), validate=True)
    digest = _sha256(data)[:12]
    relative = _safe_relative(key)
    path = _safe_join(inbox_dir, "%s.%s.incoming" % (relative, digest))
    _atomic_write(path, data, 0o600)
    metadata_path = path + ".json"
    safe_metadata = {
        "key": key,
        "reason": metadata.get("reason"),
        "conflict_paths": metadata.get("conflict_paths", []),
        "sha256": _sha256(data),
    }
    _atomic_write(
        metadata_path,
        (json.dumps(safe_metadata, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        0o600,
    )
    return path


def apply_preferences(
    plan: Mapping[str, Any],
    home: str,
    *,
    backup_dir: Optional[str] = None,
    inbox_dir: Optional[str] = None,
    cursor_closed: bool = False,
    batch_id: Optional[str] = None,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
    adapters: Optional[Sequence[PreferenceAdapter]] = None,
) -> Dict[str, Any]:
    """Apply safe actions atomically and materialize all conflicts in an inbox."""

    home_abs = os.path.abspath(home)
    if max_file_size <= 0:
        raise ValueError("max_file_size must be positive")
    batch = batch_id or time.strftime("%Y%m%d-%H%M%S")
    backup_root = backup_dir or os.path.join(
        home_abs, ".local/state/chatmesh/backups/preferences", batch
    )
    inbox_root = inbox_dir or os.path.join(
        home_abs, ".local/state/chatmesh/inbox/preferences"
    )
    result: Dict[str, Any] = {
        "applied": [],
        "kept": [],
        "conflicts": [],
        "backups": [],
    }
    for action in plan.get("actions", []):
        operation = action.get("op")
        key = str(action.get("key", ""))
        if operation == "keep":
            result["kept"].append(key)
            continue
        if operation == "conflict":
            payload = action.get("inbox_payload")
            if payload:
                path = _write_inbox(inbox_root, key, payload, action)
                result["conflicts"].append({"key": key, "inbox": path})
            else:
                result["conflicts"].append({"key": key, "inbox": None})
            continue
        if operation != "apply":
            raise ValueError("unknown preference operation: %r" % operation)
        entry = action.get("entry")
        if not isinstance(entry, dict):
            raise ValueError("apply action is missing an entry")
        path = _validate_entry(key, entry, home_abs, adapters)
        encoded = action.get("payload")
        if not isinstance(encoded, str):
            raise ValueError("apply action is missing its payload")
        payload = base64.b64decode(encoded.encode("ascii"), validate=True)
        if len(payload) > max_file_size:
            raise ValueError("payload exceeds size limit for %s" % key)
        if _sha256(payload) != entry.get("sha256"):
            raise ValueError("payload hash mismatch for %s" % key)
        adapter = _adapter_by_name(entry["adapter"], adapters)
        backup_path = _safe_join(backup_root, _safe_relative(key))
        if entry.get("kind") == "sqlite-key":
            backup = _apply_sqlite_value(
                path, entry, payload, backup_path, cursor_closed
            )
            if backup:
                result["backups"].append(backup)
            result["applied"].append(key)
            continue
        backup = _backup_live(path, backup_path)
        if backup:
            result["backups"].append(backup)
        if entry.get("kind") == "symlink":
            target = _safe_symlink_for_apply(home_abs, adapter, path, payload)
            _atomic_symlink(path, target)
        else:
            prepared = _rehydrate_structured(path, entry, payload)
            _atomic_write(path, prepared, entry.get("mode"))
        result["applied"].append(key)
    return result


apply_plan = apply_preferences


__all__ = [
    "CURSOR_STATE_DB",
    "CURSOR_USER_RULE_KEY",
    "DEFAULT_ADAPTERS",
    "DEFAULT_MAX_FILE_SIZE",
    "DEFAULT_MAX_TOTAL_SIZE",
    "MANIFEST_VERSION",
    "PreferenceAdapter",
    "apply_plan",
    "apply_preferences",
    "build_manifest",
    "collect_preferences",
    "curated_adapters",
    "decode_payloads",
    "encode_payloads",
    "inventory_preferences",
    "json_loads_compatible",
    "plan_preferences",
    "plan_three_way",
    "rewrite_declared_text",
    "rewrite_snapshot",
    "scan_preferences",
    "semantic_three_way_merge",
    "three_way_merge_json",
    "three_way_merge_toml",
    "toml_dumps_compatible",
    "toml_loads_compatible",
]
