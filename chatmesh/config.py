"""Typed TOML configuration for Chatmesh.

``~/.config/chatmesh/config.toml`` is the only user configuration source.
``CHATMESH_HOME`` remains an internal fixture hook and intentionally affects
both config lookup and home-relative paths; no other ``CHATMESH_*`` variable
is interpreted here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Set

from . import tomlutil


REMOTE_REPO = ".local/share/chatmesh/repo"  # relative to remote $HOME
VALID_DIRECTIONS = frozenset(("pull", "push"))
CONFLICT_POLICIES = frozenset(("quarantine", "manual", "keep-both", "skip"))
CUSTOM_PATH_KINDS = frozenset(("file", "tree"))


class ConfigError(ValueError):
    """Raised for a syntactically valid but invalid Chatmesh config."""


def home_dir() -> str:
    """Return the real home, or the fixture home used by Chatmesh tests."""
    return os.path.abspath(
        os.environ.get("CHATMESH_HOME") or os.path.expanduser("~")
    )


def default_config_path() -> str:
    return os.path.join(home_dir(), ".config", "chatmesh", "config.toml")


def default_state_dir() -> str:
    return os.path.join(home_dir(), ".local", "state", "chatmesh")


# Compatibility for existing imports.  Config.load() resolves these defaults
# dynamically so tests may set CHATMESH_HOME after importing this module.
CONFIG_PATH = default_config_path()
STATE_DIR = default_state_dir()


@dataclass
class RepositoryOverride:
    """Optional settings applied to one stable repository identity."""

    identity: str
    enabled: Optional[bool] = None
    sync_branches: Optional[bool] = None
    sync_tags: Optional[bool] = None
    sync_worktrees: Optional[bool] = None
    clone_missing: Optional[bool] = None
    relocate: Optional[bool] = None
    staged: Optional[bool] = None
    unstaged: Optional[bool] = None
    untracked: Optional[bool] = None
    ignored: Optional[bool] = None
    auto_apply: Optional[bool] = None
    max_file_bytes: Optional[int] = None
    max_snapshot_bytes: Optional[int] = None
    conflict_policy: Optional[str] = None

    @property
    def branches(self) -> Optional[bool]:
        return self.sync_branches

    @property
    def tags(self) -> Optional[bool]:
        return self.sync_tags

    @property
    def worktrees(self) -> Optional[bool]:
        return self.sync_worktrees


# Public aliases keep orchestration code readable while the schema calls these
# entries repositories.
RepoOverride = RepositoryOverride
GitRepositoryOverride = RepositoryOverride
PerRepoOverride = RepositoryOverride


@dataclass
class GitProfile:
    """Repository/ref/WIP synchronization policy."""

    enabled: bool = False
    roots: List[str] = field(default_factory=lambda: [_default_github_root()])
    sync_branches: bool = True
    sync_tags: bool = True
    sync_worktrees: bool = True
    clone_missing: bool = False
    relocate: bool = False
    staged: bool = True
    unstaged: bool = True
    untracked: bool = True
    ignored: bool = False
    auto_apply: bool = False
    max_file_bytes: int = 50 * 1024 * 1024
    max_snapshot_bytes: int = 1024 * 1024 * 1024
    conflict_policy: str = "quarantine"
    repositories: List[RepositoryOverride] = field(default_factory=list)

    @property
    def branches(self) -> bool:
        return self.sync_branches

    @property
    def tags(self) -> bool:
        return self.sync_tags

    @property
    def worktrees(self) -> bool:
        return self.sync_worktrees

    @property
    def relocate_repositories(self) -> bool:
        return self.relocate

    def for_repository(self, identity: str) -> "GitProfile":
        """Return this profile with an exact stable-identity override applied."""
        override = next(
            (item for item in self.repositories if item.identity == identity),
            None,
        )
        if override is None:
            return self
        values = {}
        for profile_name, override_name in (
            ("enabled", "enabled"),
            ("sync_branches", "sync_branches"),
            ("sync_tags", "sync_tags"),
            ("sync_worktrees", "sync_worktrees"),
            ("clone_missing", "clone_missing"),
            ("relocate", "relocate"),
            ("staged", "staged"),
            ("unstaged", "unstaged"),
            ("untracked", "untracked"),
            ("ignored", "ignored"),
            ("auto_apply", "auto_apply"),
            ("max_file_bytes", "max_file_bytes"),
            ("max_snapshot_bytes", "max_snapshot_bytes"),
            ("conflict_policy", "conflict_policy"),
        ):
            value = getattr(override, override_name)
            if value is not None:
                values[profile_name] = value
        return replace(self, **values)

    @property
    def repo_overrides(self) -> List[RepositoryOverride]:
        return self.repositories


@dataclass
class CustomPreferencePath:
    """One explicitly opted-in user preference file or tree."""

    name: str
    path: str
    kind: str = "file"
    enabled: bool = True
    rewrite_home: bool = False
    max_file_bytes: Optional[int] = None
    conflict_policy: Optional[str] = None
    exclude: List[str] = field(default_factory=list)


@dataclass
class PreferencesProfile:
    """Curated user-level Cursor, Claude, Codex, and custom preferences."""

    enabled: bool = False
    cursor: bool = False
    claude: bool = False
    codex: bool = False
    conflict_policy: str = "quarantine"
    max_file_bytes: int = 10 * 1024 * 1024
    max_total_bytes: int = 100 * 1024 * 1024
    exclude: List[str] = field(default_factory=list)
    custom_paths: List[CustomPreferencePath] = field(default_factory=list)

    @property
    def paths(self) -> List[CustomPreferencePath]:
        return self.custom_paths


@dataclass
class MeshConfig:
    """Complete validated Chatmesh configuration."""

    peers: List[str] = field(default_factory=list)
    apps: List[str] = field(
        default_factory=lambda: ["cursor", "cursor-cli", "claude", "codex"]
    )
    directions: List[str] = field(default_factory=lambda: ["pull", "push"])
    interval: int = 3600
    file_guard_sec: int = 900
    sync_checkpoints: bool = False
    max_composers_per_run: int = 0
    process_gate_apps: List[str] = field(
        default_factory=lambda: ["cursor", "cursor-cli"]
    )
    log_level: str = "INFO"
    state_dir: str = field(default_factory=default_state_dir)
    git: GitProfile = field(default_factory=GitProfile)
    preferences: PreferencesProfile = field(default_factory=PreferencesProfile)

    @property
    def git_profile(self) -> GitProfile:
        return self.git

    @property
    def preferences_profile(self) -> PreferencesProfile:
        return self.preferences

    @classmethod
    def load(cls, path: Optional[str] = None) -> "MeshConfig":
        """Load *path*, returning safe defaults when it does not exist."""
        actual_path = os.fspath(path) if path is not None else default_config_path()
        if not os.path.exists(actual_path):
            return cls()
        try:
            document = tomlutil.load(actual_path)
        except (OSError, tomlutil.TOMLError) as exc:
            raise ConfigError("cannot read %s: %s" % (actual_path, exc)) from exc
        return cls.from_dict(document)

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "MeshConfig":
        if not isinstance(document, Mapping):
            raise ConfigError("configuration must be a TOML table")
        _reject_unknown(document, {"version", "mesh", "git", "preferences"}, "root")
        version = document.get("version", 1)
        _require_int(version, "version", minimum=1)
        if version != 1:
            raise ConfigError("unsupported configuration version %s" % version)

        mesh = _mapping(document.get("mesh", {}), "mesh")
        _reject_unknown(mesh, _MESH_KEYS, "mesh")
        git_data = _mapping(document.get("git", {}), "git")
        preferences_data = _mapping(
            document.get("preferences", {}), "preferences"
        )
        return cls(
            peers=_string_list(mesh.get("peers", []), "mesh.peers"),
            apps=_string_list(
                mesh.get(
                    "apps", ["cursor", "cursor-cli", "claude", "codex"]
                ),
                "mesh.apps",
            ),
            directions=_directions(
                mesh.get("directions", ["pull", "push"])
            ),
            interval=_mesh_interval(mesh),
            file_guard_sec=_mesh_file_guard(mesh),
            sync_checkpoints=_boolean(
                mesh.get("sync_checkpoints", False),
                "mesh.sync_checkpoints",
            ),
            max_composers_per_run=_integer(
                mesh.get("max_composers_per_run", 0),
                "mesh.max_composers_per_run",
            ),
            process_gate_apps=_string_list(
                mesh.get("process_gate_apps", ["cursor", "cursor-cli"]),
                "mesh.process_gate_apps",
            ),
            log_level=_nonempty_string(
                mesh.get("log_level", "INFO"), "mesh.log_level"
            ).upper(),
            state_dir=_absolute_path(
                mesh.get("state_dir", "~/.local/state/chatmesh"),
                "mesh.state_dir",
            ),
            git=_parse_git_profile(git_data),
            preferences=_parse_preferences_profile(preferences_data),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the stable TOML data model used by :func:`to_toml`."""
        if self.file_guard_sec % 60 == 0:
            guard_key = "file_guard_minutes"
            guard_value = self.file_guard_sec // 60
        else:
            guard_key = "file_guard_seconds"
            guard_value = self.file_guard_sec
        mesh: Dict[str, Any] = {
            "peers": list(self.peers),
            "apps": list(self.apps),
            "directions": list(self.directions),
            "interval": self.interval,
            guard_key: guard_value,
            "sync_checkpoints": self.sync_checkpoints,
            "max_composers_per_run": self.max_composers_per_run,
            "process_gate_apps": list(self.process_gate_apps),
            "log_level": self.log_level,
            "state_dir": _portable_path(self.state_dir),
        }
        git: Dict[str, Any] = {
            "enabled": self.git.enabled,
            "roots": [_portable_path(path) for path in self.git.roots],
            "branches": self.git.sync_branches,
            "tags": self.git.sync_tags,
            "worktrees": self.git.sync_worktrees,
            "clone_missing": self.git.clone_missing,
            "relocate": self.git.relocate,
            "staged": self.git.staged,
            "unstaged": self.git.unstaged,
            "untracked": self.git.untracked,
            "ignored": self.git.ignored,
            "auto_apply": self.git.auto_apply,
            "max_file_bytes": self.git.max_file_bytes,
            "max_snapshot_bytes": self.git.max_snapshot_bytes,
            "conflict_policy": self.git.conflict_policy,
            "repositories": [
                _repository_override_dict(item)
                for item in self.git.repositories
            ],
        }
        preferences: Dict[str, Any] = {
            "enabled": self.preferences.enabled,
            "cursor": self.preferences.cursor,
            "claude": self.preferences.claude,
            "codex": self.preferences.codex,
            "conflict_policy": self.preferences.conflict_policy,
            "max_file_bytes": self.preferences.max_file_bytes,
            "max_total_bytes": self.preferences.max_total_bytes,
            "exclude": list(self.preferences.exclude),
            "custom_paths": [
                _custom_path_dict(item)
                for item in self.preferences.custom_paths
            ],
        }
        return {
            "version": 1,
            "mesh": mesh,
            "git": git,
            "preferences": preferences,
        }

    def to_toml(self) -> str:
        """Return deterministic TOML for this configuration."""
        # Validate programmatically constructed instances before encoding.
        validated = type(self).from_dict(self.to_dict())
        return tomlutil.dumps(validated.to_dict())

    def write(
        self, path: Optional[str] = None, *, overwrite: bool = False
    ) -> str:
        """Atomically write this configuration and return the destination."""
        destination = (
            os.fspath(path) if path is not None else default_config_path()
        )
        if os.path.exists(destination) and not overwrite:
            raise FileExistsError(destination)
        parent = os.path.dirname(destination)
        if parent:
            os.makedirs(parent, exist_ok=True)
        temporary = "%s.tmp.%d" % (destination, os.getpid())
        try:
            with open(
                temporary, "w", encoding="utf-8", newline="\n"
            ) as handle:
                handle.write(self.to_toml())
                handle.flush()
                os.fsync(handle.fileno())
            if os.path.exists(destination) and not overwrite:
                raise FileExistsError(destination)
            os.replace(temporary, destination)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return destination


# Existing modules import Config; new code may use the more explicit name.
Config = MeshConfig


def example_toml() -> str:
    """Return a safe, parseable starter document for ``chatmesh init``."""
    header = (
        "# Chatmesh configuration.\n"
        "# Peers are SSH host aliases from ~/.ssh/config.\n"
        "# Git and preference synchronization are opt-in.\n\n"
    )
    return header + MeshConfig().to_toml()


def write_example(path: Optional[str] = None, *, overwrite: bool = False) -> str:
    """Write :func:`example_toml` atomically without reading user config."""
    destination = os.fspath(path) if path is not None else default_config_path()
    if os.path.exists(destination) and not overwrite:
        raise FileExistsError(destination)
    parent = os.path.dirname(destination)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary = "%s.tmp.%d" % (destination, os.getpid())
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(example_toml())
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.exists(destination) and not overwrite:
            raise FileExistsError(destination)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return destination


def _default_github_root() -> str:
    return os.path.join(home_dir(), "Documents", "GitHub")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError("%s must be a table" % label)
    return value


def _reject_unknown(
    value: Mapping[str, Any], allowed: Set[str], label: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(
            "%s has unknown key%s: %s"
            % (
                label,
                "" if len(unknown) == 1 else "s",
                ", ".join(unknown),
            )
        )


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ConfigError("%s must be a boolean" % label)
    return value


def _require_int(value: Any, label: str, minimum: int = 0) -> int:
    if type(value) is not int:
        raise ConfigError("%s must be an integer" % label)
    if value < minimum:
        raise ConfigError("%s must be at least %d" % (label, minimum))
    return value


def _integer(value: Any, label: str) -> int:
    return _require_int(value, label, minimum=0)


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("%s must be a nonempty string" % label)
    if "\x00" in value:
        raise ConfigError("%s may not contain NUL" % label)
    return value.strip()


def _string_list(value: Any, label: str) -> List[str]:
    if not isinstance(value, list):
        raise ConfigError("%s must be an array of strings" % label)
    result = [
        _nonempty_string(item, "%s[%d]" % (label, index))
        for index, item in enumerate(value)
    ]
    if len(result) != len(set(result)):
        raise ConfigError("%s may not contain duplicates" % label)
    return result


def _directions(value: Any) -> List[str]:
    result = _string_list(value, "mesh.directions")
    invalid = sorted(set(result) - VALID_DIRECTIONS)
    if invalid:
        raise ConfigError(
            "mesh.directions contains invalid direction%s: %s"
            % ("" if len(invalid) == 1 else "s", ", ".join(invalid))
        )
    return result


def _mesh_interval(mesh: Mapping[str, Any]) -> int:
    if "interval" in mesh and "interval_seconds" in mesh:
        raise ConfigError("mesh may not set both interval and interval_seconds")
    return _integer(
        mesh.get("interval", mesh.get("interval_seconds", 3600)),
        "mesh.interval",
    )


def _mesh_file_guard(mesh: Mapping[str, Any]) -> int:
    names = [
        name
        for name in (
            "file_guard_minutes",
            "file_guard_seconds",
            "file_guard_sec",
        )
        if name in mesh
    ]
    if len(names) > 1:
        raise ConfigError("mesh may set only one file guard value")
    if not names:
        return 900
    name = names[0]
    value = _integer(mesh[name], "mesh.%s" % name)
    return value * 60 if name == "file_guard_minutes" else value


def _absolute_path(value: Any, label: str) -> str:
    raw = _nonempty_string(value, label)
    if raw == "~":
        expanded = home_dir()
    elif raw.startswith("~/"):
        expanded = os.path.join(home_dir(), raw[2:])
    elif os.path.isabs(raw):
        expanded = raw
    else:
        raise ConfigError("%s must be absolute or start with ~/" % label)
    normalized = os.path.abspath(os.path.normpath(expanded))
    if normalized == os.path.sep:
        raise ConfigError("%s may not be the filesystem root" % label)
    return normalized


def _portable_path(path: str) -> str:
    home = home_dir()
    normalized = os.path.abspath(path)
    if normalized == home:
        return "~"
    prefix = home + os.sep
    if normalized.startswith(prefix):
        return "~/" + normalized[len(prefix) :]
    return normalized


def _conflict_policy(value: Any, label: str) -> str:
    policy = _nonempty_string(value, label)
    if policy not in CONFLICT_POLICIES:
        raise ConfigError(
            "%s must be one of: %s"
            % (label, ", ".join(sorted(CONFLICT_POLICIES)))
        )
    return policy


def _parse_git_profile(data: Mapping[str, Any]) -> GitProfile:
    allowed = {
        "enabled",
        "roots",
        "branches",
        "tags",
        "worktrees",
        "clone_missing",
        "relocate",
        "staged",
        "unstaged",
        "untracked",
        "ignored",
        "auto_apply",
        "max_file_bytes",
        "max_snapshot_bytes",
        "conflict_policy",
        "repositories",
    }
    _reject_unknown(data, allowed, "git")
    enabled = _boolean(data.get("enabled", False), "git.enabled")
    roots_raw = data.get("roots", ["~/Documents/GitHub"])
    roots_text = _string_list(roots_raw, "git.roots")
    roots = [
        _absolute_path(root, "git.roots[%d]" % index)
        for index, root in enumerate(roots_text)
    ]
    if enabled and not roots:
        raise ConfigError("git.roots must not be empty when git is enabled")
    if len(roots) != len(set(roots)):
        raise ConfigError("git.roots resolve to duplicate paths")
    repositories_raw = data.get("repositories", [])
    if not isinstance(repositories_raw, list):
        raise ConfigError("git.repositories must be an array of tables")
    repositories = [
        _parse_repository_override(item, index)
        for index, item in enumerate(repositories_raw)
    ]
    identities = [item.identity for item in repositories]
    if len(identities) != len(set(identities)):
        raise ConfigError("git.repositories identities must be unique")
    return GitProfile(
        enabled=enabled,
        roots=roots,
        sync_branches=_boolean(
            data.get("branches", True), "git.branches"
        ),
        sync_tags=_boolean(data.get("tags", True), "git.tags"),
        sync_worktrees=_boolean(
            data.get("worktrees", True), "git.worktrees"
        ),
        clone_missing=_boolean(
            data.get("clone_missing", False), "git.clone_missing"
        ),
        relocate=_boolean(data.get("relocate", False), "git.relocate"),
        staged=_boolean(data.get("staged", True), "git.staged"),
        unstaged=_boolean(data.get("unstaged", True), "git.unstaged"),
        untracked=_boolean(data.get("untracked", True), "git.untracked"),
        ignored=_boolean(data.get("ignored", False), "git.ignored"),
        auto_apply=_boolean(
            data.get("auto_apply", False), "git.auto_apply"
        ),
        max_file_bytes=_integer(
            data.get("max_file_bytes", 50 * 1024 * 1024),
            "git.max_file_bytes",
        ),
        max_snapshot_bytes=_integer(
            data.get("max_snapshot_bytes", 1024 * 1024 * 1024),
            "git.max_snapshot_bytes",
        ),
        conflict_policy=_conflict_policy(
            data.get("conflict_policy", "quarantine"),
            "git.conflict_policy",
        ),
        repositories=repositories,
    )


def _optional_bool(
    data: Mapping[str, Any], key: str, label: str
) -> Optional[bool]:
    if key not in data:
        return None
    return _boolean(data[key], "%s.%s" % (label, key))


def _optional_int(
    data: Mapping[str, Any], key: str, label: str
) -> Optional[int]:
    if key not in data:
        return None
    return _integer(data[key], "%s.%s" % (label, key))


def _parse_repository_override(
    value: Any, index: int
) -> RepositoryOverride:
    label = "git.repositories[%d]" % index
    data = _mapping(value, label)
    allowed = {
        "identity",
        "enabled",
        "branches",
        "tags",
        "worktrees",
        "clone_missing",
        "relocate",
        "staged",
        "unstaged",
        "untracked",
        "ignored",
        "auto_apply",
        "max_file_bytes",
        "max_snapshot_bytes",
        "conflict_policy",
    }
    _reject_unknown(data, allowed, label)
    identity = _nonempty_string(data.get("identity"), "%s.identity" % label)
    policy = (
        _conflict_policy(
            data["conflict_policy"], "%s.conflict_policy" % label
        )
        if "conflict_policy" in data
        else None
    )
    return RepositoryOverride(
        identity=identity,
        enabled=_optional_bool(data, "enabled", label),
        sync_branches=_optional_bool(data, "branches", label),
        sync_tags=_optional_bool(data, "tags", label),
        sync_worktrees=_optional_bool(data, "worktrees", label),
        clone_missing=_optional_bool(data, "clone_missing", label),
        relocate=_optional_bool(data, "relocate", label),
        staged=_optional_bool(data, "staged", label),
        unstaged=_optional_bool(data, "unstaged", label),
        untracked=_optional_bool(data, "untracked", label),
        ignored=_optional_bool(data, "ignored", label),
        auto_apply=_optional_bool(data, "auto_apply", label),
        max_file_bytes=_optional_int(data, "max_file_bytes", label),
        max_snapshot_bytes=_optional_int(
            data, "max_snapshot_bytes", label
        ),
        conflict_policy=policy,
    )


def _parse_preferences_profile(
    data: Mapping[str, Any],
) -> PreferencesProfile:
    allowed = {
        "enabled",
        "cursor",
        "claude",
        "codex",
        "conflict_policy",
        "max_file_bytes",
        "max_total_bytes",
        "exclude",
        "custom_paths",
    }
    _reject_unknown(data, allowed, "preferences")
    custom_raw = data.get("custom_paths", [])
    if not isinstance(custom_raw, list):
        raise ConfigError(
            "preferences.custom_paths must be an array of tables"
        )
    custom = [
        _parse_custom_path(item, index)
        for index, item in enumerate(custom_raw)
    ]
    names = [item.name for item in custom]
    if len(names) != len(set(names)):
        raise ConfigError("preferences.custom_paths names must be unique")
    return PreferencesProfile(
        enabled=_boolean(
            data.get("enabled", False), "preferences.enabled"
        ),
        cursor=_boolean(
            data.get("cursor", False), "preferences.cursor"
        ),
        claude=_boolean(
            data.get("claude", False), "preferences.claude"
        ),
        codex=_boolean(data.get("codex", False), "preferences.codex"),
        conflict_policy=_conflict_policy(
            data.get("conflict_policy", "quarantine"),
            "preferences.conflict_policy",
        ),
        max_file_bytes=_integer(
            data.get("max_file_bytes", 10 * 1024 * 1024),
            "preferences.max_file_bytes",
        ),
        max_total_bytes=_integer(
            data.get("max_total_bytes", 100 * 1024 * 1024),
            "preferences.max_total_bytes",
        ),
        exclude=_string_list(
            data.get("exclude", []), "preferences.exclude"
        ),
        custom_paths=custom,
    )


def _parse_custom_path(value: Any, index: int) -> CustomPreferencePath:
    label = "preferences.custom_paths[%d]" % index
    data = _mapping(value, label)
    allowed = {
        "name",
        "path",
        "kind",
        "enabled",
        "rewrite_home",
        "max_file_bytes",
        "conflict_policy",
        "exclude",
    }
    _reject_unknown(data, allowed, label)
    kind = _nonempty_string(data.get("kind", "file"), "%s.kind" % label)
    if kind == "directory":
        kind = "tree"
    if kind not in CUSTOM_PATH_KINDS:
        raise ConfigError("%s.kind must be file or tree" % label)
    policy = (
        _conflict_policy(
            data["conflict_policy"], "%s.conflict_policy" % label
        )
        if "conflict_policy" in data
        else None
    )
    return CustomPreferencePath(
        name=_nonempty_string(data.get("name"), "%s.name" % label),
        path=_absolute_path(data.get("path"), "%s.path" % label),
        kind=kind,
        enabled=_boolean(data.get("enabled", True), "%s.enabled" % label),
        rewrite_home=_boolean(
            data.get("rewrite_home", False), "%s.rewrite_home" % label
        ),
        max_file_bytes=_optional_int(data, "max_file_bytes", label),
        conflict_policy=policy,
        exclude=_string_list(data.get("exclude", []), "%s.exclude" % label),
    )


def _repository_override_dict(item: RepositoryOverride) -> Dict[str, Any]:
    data: Dict[str, Any] = {"identity": item.identity}
    values = (
        ("enabled", item.enabled),
        ("branches", item.sync_branches),
        ("tags", item.sync_tags),
        ("worktrees", item.sync_worktrees),
        ("clone_missing", item.clone_missing),
        ("relocate", item.relocate),
        ("staged", item.staged),
        ("unstaged", item.unstaged),
        ("untracked", item.untracked),
        ("ignored", item.ignored),
        ("auto_apply", item.auto_apply),
        ("max_file_bytes", item.max_file_bytes),
        ("max_snapshot_bytes", item.max_snapshot_bytes),
        ("conflict_policy", item.conflict_policy),
    )
    for key, value in values:
        if value is not None:
            data[key] = value
    return data


def _custom_path_dict(item: CustomPreferencePath) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "name": item.name,
        "path": _portable_path(item.path),
        "kind": item.kind,
        "enabled": item.enabled,
        "rewrite_home": item.rewrite_home,
        "exclude": list(item.exclude),
    }
    if item.max_file_bytes is not None:
        data["max_file_bytes"] = item.max_file_bytes
    if item.conflict_policy is not None:
        data["conflict_policy"] = item.conflict_policy
    return data


_MESH_KEYS = {
    "peers",
    "apps",
    "directions",
    "interval",
    "interval_seconds",
    "file_guard_minutes",
    "file_guard_seconds",
    "file_guard_sec",
    "sync_checkpoints",
    "max_composers_per_run",
    "process_gate_apps",
    "log_level",
    "state_dir",
}
EXAMPLE_TOML = example_toml()
