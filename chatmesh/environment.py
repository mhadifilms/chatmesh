"""Declarative, additive-only machine environment synchronization.

Chatmesh inventories package-manager roots and restorable virtual environments.
It never copies interpreters, Homebrew prefixes, ``site-packages``, or venv
directories.  Apply operations only install missing tools or create a missing
venv from an unchanged declaration file.
"""

from __future__ import annotations

import ctypes
import base64
import csv
import errno
import fnmatch
import hashlib
import json
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .config import EnvironmentProfile, home_dir


MANIFEST_VERSION = 1
_VENV_NAMES = frozenset((".venv", "venv", "env"))
_PRUNE_NAMES = frozenset(
    (
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "node_modules",
        "site-packages",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".build",
        ".cache",
        "build",
        "dist",
        "third-party",
        "third_party",
        "vendor",
    )
)
_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@/-]*$")
_PYTHON_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_PEP503 = re.compile(r"[-_.]+")
_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_BREWFILE_ENTRY = re.compile(
    r'^\s*(tap|brew|cask)\s+["\']([^"\']+)["\']'
)
_PINNED_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;]+(?:\s*;.*)?$"
)
Runner = Callable[..., subprocess.CompletedProcess]


class EnvironmentSyncError(RuntimeError):
    """Raised when an environment operation cannot be performed safely."""


def _portable_path(path: str, home: str) -> str:
    normalized = os.path.abspath(path)
    if normalized == home:
        return "~"
    prefix = home + os.sep
    if normalized.startswith(prefix):
        return "~/" + normalized[len(prefix) :]
    return normalized


def resolve_portable_path(path: str, home: Optional[str] = None) -> str:
    base = os.path.abspath(home or home_dir())
    if path == "~":
        return base
    if path.startswith("~/"):
        return os.path.abspath(os.path.join(base, path[2:]))
    if os.path.isabs(path):
        return os.path.abspath(path)
    raise EnvironmentSyncError("environment path is not absolute: %r" % path)


def _normalize_package(value: str) -> Optional[str]:
    name = value.strip()
    if not name or name.startswith("-") or not _PACKAGE_NAME.fullmatch(name):
        return None
    return name


def _valid_brew_name(value: Any) -> bool:
    if not isinstance(value, str) or _normalize_package(value) != value:
        return False
    return all(part not in ("", ".", "..") for part in value.split("/"))


def _normalize_python_package(value: str) -> Optional[str]:
    name = _normalize_package(value)
    return _PEP503.sub("-", name).lower() if name else None


def _valid_python_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and _normalize_python_package(value) == value
        and _PYTHON_PACKAGE_NAME.fullmatch(value) is not None
    )


def manifest_snapshot_id(manifest: Mapping[str, Any]) -> str:
    identity_manifest = dict(manifest)
    identity_manifest.pop("generated_at", None)
    identity_manifest.pop("snapshot_id", None)
    canonical = json.dumps(
        identity_manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _require_string_list(
    value: Any, label: str, validator: Callable[[Any], bool]
) -> List[str]:
    if not isinstance(value, list) or not all(validator(item) for item in value):
        raise EnvironmentSyncError("%s is not a valid package list" % label)
    if len(value) != len(set(value)):
        raise EnvironmentSyncError("%s contains duplicates" % label)
    return value


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    if not isinstance(manifest, Mapping):
        raise EnvironmentSyncError("environment manifest must be an object")
    if manifest.get("version") != MANIFEST_VERSION:
        raise EnvironmentSyncError("unsupported environment manifest")
    snapshot_id = manifest.get("snapshot_id")
    if not isinstance(snapshot_id, str) or snapshot_id != manifest_snapshot_id(
        manifest
    ):
        raise EnvironmentSyncError("environment snapshot ID is invalid")
    brew = manifest.get("brew")
    if not isinstance(brew, Mapping):
        raise EnvironmentSyncError("environment brew inventory is invalid")
    for key in (
        "formulae",
        "casks",
        "taps",
        "installed_formulae",
        "installed_casks",
        "installed_taps",
    ):
        _require_string_list(
            brew.get(key, []), "brew.%s" % key, _valid_brew_name
        )
    _require_string_list(manifest.get("pip", []), "pip", _valid_python_name)
    _require_string_list(manifest.get("pipx", []), "pipx", _valid_python_name)
    _require_string_list(manifest.get("uv", []), "uv", _valid_python_name)
    python = manifest.get("python")
    if not isinstance(python, Mapping):
        raise EnvironmentSyncError("environment Python inventory is invalid")
    completeness = manifest.get("complete", {})
    if (
        not isinstance(completeness, Mapping)
        or not all(
            type(completeness.get(key)) is bool
            for key in ("brew", "pip", "pipx", "uv", "venvs")
        )
    ):
        raise EnvironmentSyncError("environment completeness map is invalid")
    venvs = manifest.get("venvs")
    if not isinstance(venvs, Mapping):
        raise EnvironmentSyncError("environment venv inventory is invalid")
    for path, descriptor in venvs.items():
        if not isinstance(path, str) or not isinstance(descriptor, Mapping):
            raise EnvironmentSyncError("environment venv descriptor is invalid")
        if descriptor.get("path") != path:
            raise EnvironmentSyncError("venv descriptor path does not match key")
        project = descriptor.get("project")
        if (
            not isinstance(project, str)
            or os.path.dirname(path) != project
            or os.path.basename(path) not in _VENV_NAMES
        ):
            raise EnvironmentSyncError("venv project/path relationship is invalid")
        kind = descriptor.get("kind")
        if kind not in (None, "requirements"):
            raise EnvironmentSyncError("unsupported venv declaration kind")
        files = descriptor.get("files")
        if not isinstance(files, list) or not all(
            isinstance(item, str)
            and item
            and not os.path.isabs(item)
            and ".." not in item.split(os.sep)
            for item in files
        ):
            raise EnvironmentSyncError("venv declaration files are invalid")
        if type(descriptor.get("restorable")) is not bool:
            raise EnvironmentSyncError("venv restorable flag is invalid")
        if type(descriptor.get("incomplete", False)) is not bool:
            raise EnvironmentSyncError("venv incomplete flag is invalid")
        digest = descriptor.get("lock_sha256")
        if digest is not None and (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise EnvironmentSyncError("venv declaration hash is invalid")
    if not isinstance(manifest.get("blocked"), list):
        raise EnvironmentSyncError("environment blocked inventory is invalid")


def _excluded(profile: EnvironmentProfile, key: str) -> bool:
    leaf = key.split(":", 1)[-1]
    return any(
        fnmatch.fnmatchcase(key, pattern)
        or fnmatch.fnmatchcase(leaf, pattern)
        for pattern in profile.exclude
    )


def _default_runner(
    argv: List[str],
    *,
    env: Optional[Mapping[str, str]] = None,
    timeout: int = 900,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env) if env is not None else None,
        timeout=timeout,
    )


def _run_inventory(
    argv: List[str],
    blocked: List[dict],
    label: str,
    runner: Runner,
) -> Optional[bytes]:
    try:
        result = runner(argv, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        blocked.append({"source": label, "reason": str(exc)})
        return None
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
        blocked.append(
            {
                "source": label,
                "reason": stderr[:500] or "command exited %d" % result.returncode,
            }
        )
        return None
    return result.stdout or b""


def _command_available(command: str) -> bool:
    return shutil.which(command) is not None


def _read_bounded(path: str, limit: int) -> bytes:
    info = os.stat(path, follow_symlinks=True)
    if not os.path.isfile(path):
        raise EnvironmentSyncError("declaration is not a regular file")
    if info.st_size > limit:
        raise EnvironmentSyncError(
            "declaration exceeds %d-byte limit" % limit
        )
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise EnvironmentSyncError(
            "declaration exceeds %d-byte limit" % limit
        )
    return data


def _validate_requirements_data(data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise EnvironmentSyncError(
            "requirements file is not UTF-8: %s" % exc
        ) from exc
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if (
            line.startswith("-")
            or line.startswith((".", "/", "~"))
            or " @ " in line
            or not _PINNED_REQUIREMENT.fullmatch(line)
        ):
            raise EnvironmentSyncError(
                "requirements line %d is not a flat pinned package" % number
            )


def _brewfile_inventory(
    profile: EnvironmentProfile, blocked: List[dict]
) -> Optional[dict]:
    if not os.path.isfile(profile.brewfile):
        return None
    try:
        raw = _read_bounded(profile.brewfile, profile.max_lock_file_bytes)
        text = raw.decode("utf-8")
    except (OSError, UnicodeError, EnvironmentSyncError) as exc:
        blocked.append(
            {"source": _portable_path(profile.brewfile, home_dir()), "reason": str(exc)}
        )
        return None
    result = {
        "formulae": [],
        "casks": [],
        "taps": [],
        "source": "brewfile",
        "path": _portable_path(profile.brewfile, home_dir()),
    }
    mapping = {"brew": "formulae", "cask": "casks", "tap": "taps"}
    for line in text.splitlines():
        match = _BREWFILE_ENTRY.match(line)
        if not match:
            continue
        trailing = line[match.end() :].strip()
        if (
            match.group(1) == "tap"
            and trailing
            and not trailing.startswith("#")
        ):
            blocked.append(
                {
                    "source": result["path"],
                    "reason": "custom tap origins require manual review",
                }
            )
            continue
        name = _normalize_package(match.group(2))
        key = "%s:%s" % (match.group(1), name or match.group(2))
        if name and not _excluded(profile, key):
            result[mapping[match.group(1)]].append(name)
    for key in ("formulae", "casks", "taps"):
        result[key] = sorted(set(result[key]))
    result["sha256"] = hashlib.sha256(raw).hexdigest()
    return result


def _brew_inventory(
    profile: EnvironmentProfile, blocked: List[dict], runner: Runner
) -> dict:
    brewfile_exists = os.path.lexists(profile.brewfile)
    declared = (
        _brewfile_inventory(profile, blocked)
        if brewfile_exists
        else None
    )
    result = {
        "formulae": [],
        "casks": [],
        "taps": [],
        "installed_formulae": [],
        "installed_casks": [],
        "installed_taps": [],
        "source": "brewfile" if brewfile_exists else "live",
        "declaration_complete": not brewfile_exists or declared is not None,
    }
    if declared is not None:
        for key in ("formulae", "casks", "taps"):
            result[key] = list(declared[key])
        result["path"] = declared["path"]
        result["sha256"] = declared["sha256"]
    if not _command_available("brew"):
        result["available"] = False
        result["installed_complete"] = False
        return result
    result["available"] = True
    complete = True
    for key, argv, prefix in (
        ("installed_formulae", ["brew", "leaves"], "brew"),
        ("installed_casks", ["brew", "list", "--cask", "-1"], "cask"),
        ("installed_taps", ["brew", "tap"], "tap"),
    ):
        output = _run_inventory(argv, blocked, "homebrew.%s" % key, runner)
        if output is None:
            complete = False
            continue
        names = []
        for line in output.decode("utf-8", "replace").splitlines():
            name = _normalize_package(line)
            if name and not _excluded(profile, "%s:%s" % (prefix, name)):
                names.append(name)
        result[key] = sorted(set(names))
    result["installed_complete"] = complete
    if not brewfile_exists:
        result["formulae"] = list(result["installed_formulae"])
        result["casks"] = list(result["installed_casks"])
        result["taps"] = list(result["installed_taps"])
    return result


def _python_version(output: str) -> Tuple[Optional[str], Optional[str]]:
    match = _VERSION.search(output)
    if not match:
        return None, None
    version = ".".join(part for part in match.groups() if part is not None)
    return version, "%s.%s" % (match.group(1), match.group(2))


def _python_inventory(blocked: List[dict], runner: Runner) -> dict:
    output = _run_inventory(
        [sys.executable, "--version"], blocked, "python", runner
    )
    version, major_minor = _python_version(
        (output or b"").decode("utf-8", "replace")
    )
    return {
        "available": output is not None,
        "version": version,
        "major_minor": major_minor,
        "implementation": getattr(sys.implementation, "name", "python"),
    }


def _pip_inventory(
    profile: EnvironmentProfile, blocked: List[dict], runner: Runner
) -> Tuple[List[str], bool]:
    output = _run_inventory(
        [
            sys.executable,
            "-m",
            "pip",
            "list",
            "--user",
            "--not-required",
            "--format=json",
            "--disable-pip-version-check",
        ],
        blocked,
        "pip.user",
        runner,
    )
    if output is None:
        return [], False
    try:
        records = json.loads(output.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        blocked.append({"source": "pip.user", "reason": "invalid JSON: %s" % exc})
        return [], False
    if not isinstance(records, list):
        blocked.append({
            "source": "pip.user",
            "reason": "JSON result is not a package array",
        })
        return [], False
    packages = []
    malformed = False
    for record in records:
        if not isinstance(record, dict):
            malformed = True
            continue
        raw_name = record.get("name")
        name = (
            _normalize_python_package(raw_name)
            if isinstance(raw_name, str)
            else None
        )
        if not name:
            malformed = True
        elif (
            name not in ("pip", "setuptools", "wheel")
            and not _excluded(profile, "pip:%s" % name)
        ):
            packages.append(name)
    if malformed:
        blocked.append({
            "source": "pip.user",
            "reason": "JSON result contains an invalid package record",
        })
    return sorted(set(packages)), not malformed


def _pipx_inventory(
    profile: EnvironmentProfile, blocked: List[dict], runner: Runner
) -> Tuple[List[str], bool]:
    if not _command_available("pipx"):
        return [], False
    output = _run_inventory(
        ["pipx", "list", "--json"], blocked, "pipx", runner
    )
    if output is None:
        return [], False
    try:
        document = json.loads(output.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        blocked.append({"source": "pipx", "reason": "invalid JSON: %s" % exc})
        return [], False
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("venvs"), dict)
    ):
        blocked.append({
            "source": "pipx",
            "reason": "JSON result has no venv map",
        })
        return [], False
    venvs = document["venvs"]
    packages = []
    malformed = False
    for raw_name in venvs:
        name = _normalize_python_package(str(raw_name))
        if name and not _excluded(profile, "pipx:%s" % name):
            packages.append(name)
        elif raw_name is not None:
            malformed = True
    if malformed:
        blocked.append({
            "source": "pipx",
            "reason": "JSON result contains an invalid package name",
        })
    return sorted(set(packages)), not malformed


def _uv_inventory(
    profile: EnvironmentProfile, blocked: List[dict], runner: Runner
) -> Tuple[List[str], bool]:
    if not _command_available("uv"):
        return [], False
    output = _run_inventory(
        ["uv", "tool", "list"], blocked, "uv.tools", runner
    )
    if output is None:
        return [], False
    packages = []
    for line in output.decode("utf-8", "replace").splitlines():
        if not line or line[0].isspace():
            continue
        fields = line.split()
        if len(fields) < 2 or not fields[1].startswith("v"):
            continue
        name = _normalize_python_package(fields[0])
        if name and not _excluded(profile, "uv:%s" % name):
            packages.append(name)
    return sorted(set(packages)), True


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _resolved_path_allowed(path: str, configured_root: str) -> bool:
    logical_root = os.path.abspath(configured_root)
    logical_path = os.path.abspath(path)
    if not _within(logical_path, logical_root):
        return False
    relative = os.path.relpath(logical_path, logical_root)
    first = relative.split(os.sep, 1)[0]
    allowed = [os.path.realpath(logical_root)]
    first_path = os.path.join(logical_root, first)
    if first not in (".", "..") and os.path.islink(first_path):
        allowed.append(os.path.realpath(first_path))
    resolved = os.path.realpath(logical_path)
    return any(_within(resolved, root) for root in allowed)


def _walk_venvs(
    roots: Iterable[str], errors: Optional[List[dict]] = None
) -> Iterable[Tuple[str, str]]:
    visited = set()
    stack = [
        (os.path.abspath(path), os.path.abspath(path)) for path in roots
    ]
    while stack:
        directory, configured_root = stack.pop()
        try:
            info = os.stat(directory, follow_symlinks=True)
        except OSError as exc:
            if errors is not None:
                errors.append({"source": directory, "reason": str(exc)})
            continue
        identity = (info.st_dev, info.st_ino)
        if identity in visited:
            continue
        visited.add(identity)
        name = os.path.basename(directory)
        if name.startswith(".chatmesh-venv-") and os.path.isfile(
            os.path.join(directory, ".chatmesh-incomplete.json")
        ):
            yield directory, os.path.dirname(directory)
            continue
        if name in _VENV_NAMES:
            if os.path.isfile(os.path.join(directory, "pyvenv.cfg")) or os.path.isfile(
                os.path.join(directory, ".chatmesh-incomplete.json")
            ):
                yield directory, os.path.dirname(directory)
                continue
        if name in _PRUNE_NAMES:
            continue
        try:
            with os.scandir(directory) as entries:
                children = []
                for entry in entries:
                    try:
                        if not entry.is_dir(follow_symlinks=True):
                            continue
                    except OSError as exc:
                        if errors is not None:
                            errors.append({
                                "source": os.path.join(directory, entry.name),
                                "reason": str(exc),
                            })
                        continue
                    child = os.path.join(directory, entry.name)
                    if _resolved_path_allowed(child, configured_root):
                        children.append((child, configured_root))
                    elif errors is not None:
                        errors.append({
                            "source": child,
                            "reason": "directory symlink escapes configured root",
                        })
                children.sort(reverse=True)
        except OSError as exc:
            if errors is not None:
                errors.append({"source": directory, "reason": str(exc)})
            continue
        stack.extend(children)


def _venv_version(path: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        with open(
            os.path.join(path, "pyvenv.cfg"), encoding="utf-8", errors="replace"
        ) as handle:
            for line in handle:
                key, separator, value = line.partition("=")
                if separator and key.strip() == "version":
                    return _python_version(value.strip())
    except OSError:
        pass
    return None, None


def _declaration(
    project: str, profile: EnvironmentProfile
) -> Tuple[Optional[str], List[str], Optional[str], Optional[str]]:
    candidates = [("requirements", ["requirements.txt"])]
    requirement_files = sorted(
        name
        for name in os.listdir(project)
        if name.startswith("requirements")
        and name.endswith(".txt")
        and name != "requirements.txt"
    ) if os.path.isdir(project) else []
    candidates.extend(("requirements", [name]) for name in requirement_files)
    for kind, names in candidates:
        paths = [os.path.join(project, name) for name in names]
        if kind == "uv" and not all(os.path.isfile(path) for path in paths):
            continue
        if kind == "requirements" and not os.path.isfile(paths[0]):
            continue
        digest = hashlib.sha256()
        try:
            for name, path in zip(names, paths):
                data = _read_bounded(path, profile.max_lock_file_bytes)
                _validate_requirements_data(data)
                digest.update(name.encode("utf-8"))
                digest.update(b"\0")
                digest.update(data)
                digest.update(b"\0")
        except (OSError, EnvironmentSyncError) as exc:
            return kind, names, None, str(exc)
        return kind, names, digest.hexdigest(), None
    if os.path.isfile(os.path.join(project, "uv.lock")):
        return (
            None,
            [],
            None,
            "uv project venv recreation requires explicit manual review",
        )
    return None, [], None, "no supported flat pinned requirements file"


def _venv_inventory(
    profile: EnvironmentProfile, blocked: List[dict], home: str
) -> Tuple[Dict[str, dict], bool]:
    result = {}
    traversal_errors: List[dict] = []
    for path, project in _walk_venvs(profile.roots, traversal_errors):
        portable = _portable_path(path, home)
        if os.path.basename(path).startswith(".chatmesh-venv-"):
            blocked.append({
                "source": portable,
                "reason": "incomplete venv recovery directory requires review",
            })
            continue
        if _excluded(profile, "venv:%s" % portable):
            continue
        version, major_minor = _venv_version(path)
        try:
            kind, files, digest, error = _declaration(project, profile)
        except OSError as exc:
            kind, files, digest, error = None, [], None, str(exc)
        descriptor = {
            "path": portable,
            "project": _portable_path(project, home),
            "kind": kind,
            "files": files,
            "lock_sha256": digest,
            "python_version": version,
            "python_major_minor": major_minor,
            "incomplete": os.path.isfile(
                os.path.join(path, ".chatmesh-incomplete.json")
            ),
        }
        descriptor["restorable"] = bool(
            kind and digest and not descriptor["incomplete"]
        )
        result[portable] = descriptor
        if descriptor["incomplete"]:
            blocked.append({
                "source": portable,
                "reason": "venv has an incomplete Chatmesh creation marker",
            })
        if error:
            blocked.append({"source": portable, "reason": error})
    blocked.extend(traversal_errors)
    return dict(sorted(result.items())), not traversal_errors


def snapshot_environment(
    profile: EnvironmentProfile,
    *,
    home: Optional[str] = None,
    runner: Optional[Runner] = None,
) -> dict:
    """Return a JSON-safe declarative inventory for one machine."""

    actual_home = os.path.abspath(home or home_dir())
    invoke = runner or _default_runner
    blocked: List[dict] = []
    pip_packages, pip_complete = (
        _pip_inventory(profile, blocked, invoke)
        if profile.pip
        else ([], True)
    )
    pipx_packages, pipx_complete = (
        _pipx_inventory(profile, blocked, invoke)
        if profile.pipx
        else ([], True)
    )
    uv_packages, uv_complete = (
        _uv_inventory(profile, blocked, invoke)
        if profile.uv
        else ([], True)
    )
    brew = (
        _brew_inventory(profile, blocked, invoke)
        if profile.homebrew
        else {
            "formulae": [],
            "casks": [],
            "taps": [],
            "installed_formulae": [],
            "installed_casks": [],
            "installed_taps": [],
            "installed_complete": True,
            "declaration_complete": True,
            "disabled": True,
        }
    )
    venvs, venv_complete = (
        _venv_inventory(profile, blocked, actual_home)
        if profile.venvs
        else ({}, True)
    )
    manifest = {
        "version": MANIFEST_VERSION,
        "home": actual_home,
        "generated_at": int(time.time()),
        "brew": brew,
        "python": (
            _python_inventory(blocked, invoke)
            if profile.python
            else {"disabled": True}
        ),
        "pip": pip_packages,
        "pipx": pipx_packages,
        "uv": uv_packages,
        "venvs": venvs,
        "blocked": blocked,
        "complete": {
            "brew": bool(
                brew.get("installed_complete", False)
                and brew.get("declaration_complete", False)
            ),
            "pip": pip_complete,
            "pipx": pipx_complete,
            "uv": uv_complete,
            "venvs": venv_complete,
        },
    }
    manifest["snapshot_id"] = manifest_snapshot_id(manifest)
    validate_manifest(manifest)
    return manifest


def _add_missing_actions(
    actions: List[dict],
    kept: List[dict],
    *,
    kind: str,
    source: Iterable[str],
    destination: Iterable[str],
) -> None:
    existing = set(destination)
    for name in sorted(set(source)):
        record = {"kind": kind, "name": name}
        (kept if name in existing else actions).append(record)


def plan_environment(
    source: Mapping[str, Any],
    destination: Mapping[str, Any],
    profile: EnvironmentProfile,
) -> dict:
    """Plan source additions without ever planning removals or forced upgrades."""

    validate_manifest(source)
    validate_manifest(destination)
    actions: List[dict] = []
    kept: List[dict] = []
    conflicts: List[dict] = []
    blocked: List[dict] = []
    destination_complete = destination["complete"]
    if profile.homebrew:
        source_brew = source.get("brew", {})
        destination_brew = destination.get("brew", {})
        for section, kind in (
            ("taps", "brew-tap"),
            ("formulae", "brew-formula"),
            ("casks", "brew-cask"),
        ):
            desired = source_brew.get(section, [])
            if not destination_complete["brew"] and desired:
                blocked.append({
                    "kind": kind,
                    "reason": "destination Homebrew inventory is incomplete",
                })
            else:
                _add_missing_actions(
                    actions,
                    kept,
                    kind=kind,
                    source=desired,
                    destination=destination_brew.get(
                        "installed_%s" % section,
                        destination_brew.get(section, []),
                    ),
                )
    if profile.pip:
        if not destination_complete["pip"] and source.get("pip"):
            blocked.append({
                "kind": "pip-user",
                "reason": "destination pip inventory is incomplete",
            })
        else:
            _add_missing_actions(
                actions,
                kept,
                kind="pip-user",
                source=source.get("pip", []),
                destination=destination.get("pip", []),
            )
    if profile.pipx:
        if not destination_complete["pipx"] and source.get("pipx"):
            blocked.append({
                "kind": "pipx",
                "reason": "destination pipx inventory is incomplete",
            })
        else:
            _add_missing_actions(
                actions,
                kept,
                kind="pipx",
                source=source.get("pipx", []),
                destination=destination.get("pipx", []),
            )
    if profile.uv:
        if not destination_complete["uv"] and source.get("uv"):
            blocked.append({
                "kind": "uv-tool",
                "reason": "destination uv inventory is incomplete",
            })
        else:
            _add_missing_actions(
                actions,
                kept,
                kind="uv-tool",
                source=source.get("uv", []),
                destination=destination.get("uv", []),
            )
    if profile.venvs:
        destination_venvs = destination.get("venvs", {})
        for path, descriptor in sorted(source.get("venvs", {}).items()):
            if not destination_complete["venvs"]:
                blocked.append({
                    "kind": "venv-create",
                    "path": path,
                    "reason": "destination venv inventory is incomplete",
                })
                continue
            existing = destination_venvs.get(path)
            if existing is None:
                if descriptor.get("restorable"):
                    actions.append(
                        {"kind": "venv-create", "path": path, "venv": descriptor}
                    )
                else:
                    blocked.append(
                        {
                            "kind": "venv-create",
                            "path": path,
                            "reason": "source venv has no supported declaration",
                        }
                    )
                continue
            if existing.get("incomplete"):
                conflicts.append(
                    {
                        "kind": "venv",
                        "path": path,
                        "reason": "existing venv creation is incomplete",
                        "source": descriptor,
                        "destination": existing,
                    }
                )
            elif (
                descriptor.get("lock_sha256") == existing.get("lock_sha256")
                and descriptor.get("python_major_minor")
                == existing.get("python_major_minor")
            ):
                kept.append({"kind": "venv", "path": path})
            else:
                conflicts.append(
                    {
                        "kind": "venv",
                        "path": path,
                        "reason": "existing venv declaration or Python differs",
                        "source": descriptor,
                        "destination": existing,
                    }
                )
    source_python = source.get("python", {})
    destination_python = destination.get("python", {})
    if (
        profile.python
        and source_python.get("major_minor")
        and destination_python.get("major_minor")
        and source_python.get("major_minor") != destination_python.get("major_minor")
    ):
        blocked.append(
            {
                "kind": "python-runtime",
                "reason": "runtime differs; interpreters are never replaced",
                "source": source_python.get("version"),
                "destination": destination_python.get("version"),
            }
        )
    return {
        "version": MANIFEST_VERSION,
        "source_snapshot": source.get("snapshot_id"),
        "actions": actions,
        "kept": kept,
        "conflicts": conflicts,
        "blocked": blocked,
        "counts": {
            "install": len(actions),
            "keep": len(kept),
            "conflict": len(conflicts),
            "blocked": len(blocked),
        },
    }


def _declaration_digest(
    project: str, files: Iterable[str], limit: int
) -> str:
    digest = hashlib.sha256()
    for name in files:
        if (
            os.path.isabs(name)
            or name in (".", "..")
            or ".." in name.split(os.sep)
        ):
            raise EnvironmentSyncError("unsafe declaration path: %r" % name)
        path = os.path.join(project, name)
        data = _read_bounded(path, limit)
        _validate_requirements_data(data)
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _authorized_project(
    target: str, project: str, roots: Iterable[str]
) -> Tuple[str, str]:
    if os.path.dirname(target) != project:
        raise EnvironmentSyncError("venv project must be the target parent")
    basename = os.path.basename(target)
    if basename not in _VENV_NAMES:
        raise EnvironmentSyncError("venv target name is not allowed")
    for configured in roots:
        if _resolved_path_allowed(project, configured):
            return os.path.realpath(project), basename
    raise EnvironmentSyncError(
        "venv project resolves outside configured roots"
    )


def _invoke_apply(
    runner: Runner,
    argv: List[str],
    *,
    env: Optional[Mapping[str, str]] = None,
    timeout: int = 1800,
) -> subprocess.CompletedProcess:
    try:
        return runner(argv, env=env, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(
            argv, 127, stdout=b"", stderr=str(exc).encode("utf-8", "replace")
        )


def _rewrite_venv_embedded_paths(
    venv: str, old_path: str, new_path: str
) -> None:
    old = os.fsencode(old_path)
    new = os.fsencode(new_path)
    candidates = [os.path.join(venv, "pyvenv.cfg")]
    rewritten_paths = set()
    bin_dir = os.path.join(venv, "bin")
    try:
        candidates.extend(
            os.path.join(bin_dir, name) for name in os.listdir(bin_dir)
        )
    except OSError:
        pass
    for path in candidates:
        try:
            with open(path, "rb") as stream:
                data = stream.read()
        except OSError:
            continue
        if old not in data:
            continue
        if b"\0" in data and not data.startswith(b"#!"):
            continue
        info = os.stat(path, follow_symlinks=False)
        replacement = data.replace(old, new)
        temporary = path + ".chatmesh-rewrite-%d" % os.getpid()
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, info.st_mode & 0o777
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(replacement)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            rewritten_paths.add(os.path.normpath(path))
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
    _update_venv_record_hashes(venv, rewritten_paths)


def _update_venv_record_hashes(venv: str, rewritten_paths: set) -> None:
    if not rewritten_paths:
        return
    lib = os.path.join(venv, "lib")
    try:
        python_dirs = [
            os.path.join(lib, name)
            for name in os.listdir(lib)
            if name.startswith("python")
        ]
    except OSError:
        return
    for python_dir in python_dirs:
        site_packages = os.path.join(python_dir, "site-packages")
        try:
            records = [
                os.path.join(site_packages, name, "RECORD")
                for name in os.listdir(site_packages)
                if name.endswith(".dist-info")
            ]
        except OSError:
            continue
        for record in records:
            try:
                with open(record, "r", encoding="utf-8", newline="") as stream:
                    rows = list(csv.reader(stream))
            except (OSError, UnicodeError, csv.Error):
                continue
            changed = False
            for row in rows:
                if not row:
                    continue
                recorded_path = os.path.normpath(
                    os.path.join(site_packages, row[0])
                )
                if recorded_path not in rewritten_paths:
                    continue
                with open(recorded_path, "rb") as stream:
                    data = stream.read()
                digest = base64.urlsafe_b64encode(
                    hashlib.sha256(data).digest()
                ).rstrip(b"=").decode("ascii")
                while len(row) < 3:
                    row.append("")
                row[1] = "sha256=" + digest
                row[2] = str(len(data))
                changed = True
            if not changed:
                continue
            output = io.StringIO(newline="")
            csv.writer(output, lineterminator="\n").writerows(rows)
            data = output.getvalue().encode("utf-8")
            info = os.stat(record, follow_symlinks=False)
            temporary = record + ".chatmesh-rewrite-%d" % os.getpid()
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                info.st_mode & 0o777,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, record)
            finally:
                if os.path.lexists(temporary):
                    os.unlink(temporary)


def _rename_exclusive(source: str, target: str) -> None:
    try:
        renamex = ctypes.CDLL(None, use_errno=True).renamex_np
    except AttributeError as exc:
        raise EnvironmentSyncError(
            "exclusive venv publication is unsupported on this platform"
        ) from exc
    renamex.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex.restype = ctypes.c_int
    # Darwin's RENAME_EXCL guarantees that a concurrently created target is
    # never replaced.
    if renamex(os.fsencode(source), os.fsencode(target), 0x00000004) != 0:
        error = ctypes.get_errno()
        if error in (errno.EEXIST, errno.ENOTEMPTY):
            raise EnvironmentSyncError("venv target already exists")
        raise OSError(error, os.strerror(error), target)


def _create_venv(
    action: Mapping[str, Any],
    profile: EnvironmentProfile,
    runner: Runner,
    home: str,
) -> dict:
    descriptor = action["venv"]
    target = resolve_portable_path(str(descriptor["path"]), home)
    project = resolve_portable_path(str(descriptor["project"]), home)
    project_real, basename = _authorized_project(
        target, project, profile.roots
    )
    if not os.path.isdir(project_real):
        raise EnvironmentSyncError("venv project does not exist")
    files = [str(item) for item in descriptor.get("files", [])]
    kind = descriptor.get("kind")
    if kind != "requirements" or len(files) != 1:
        raise EnvironmentSyncError("unsupported venv declaration kind")
    source_data = _read_bounded(
        os.path.join(project_real, files[0]),
        profile.max_lock_file_bytes,
    )
    _validate_requirements_data(source_data)
    digest_builder = hashlib.sha256()
    digest_builder.update(files[0].encode("utf-8"))
    digest_builder.update(b"\0")
    digest_builder.update(source_data)
    digest_builder.update(b"\0")
    digest = digest_builder.hexdigest()
    if digest != descriptor.get("lock_sha256"):
        raise EnvironmentSyncError("venv declaration changed since inventory")
    current = "%d.%d" % (sys.version_info[0], sys.version_info[1])
    required = descriptor.get("python_major_minor")
    if required and required != current:
        raise EnvironmentSyncError(
            "venv requires Python %s; local Chatmesh uses %s" % (required, current)
        )
    final_target = os.path.join(project_real, basename)
    if os.path.lexists(final_target):
        raise EnvironmentSyncError("venv target already exists")
    temporary_target = tempfile.mkdtemp(
        prefix=".chatmesh-venv-", dir=project_real
    )
    marker = os.path.join(temporary_target, ".chatmesh-incomplete.json")
    with open(marker, "x", encoding="utf-8") as stream:
        json.dump(
            {
                "version": 1,
                "target": descriptor["path"],
                "lock_sha256": descriptor["lock_sha256"],
                "created_at": int(time.time()),
            },
            stream,
            sort_keys=True,
        )
    staged_requirements = os.path.join(
        temporary_target, ".chatmesh-requirements.txt"
    )
    with open(staged_requirements, "xb") as stream:
        stream.write(source_data)
    result = _invoke_apply(
        runner, [sys.executable, "-m", "venv", temporary_target]
    )
    if result.returncode == 0:
        result = _invoke_apply(
            runner,
            [
                os.path.join(temporary_target, "bin", "python"),
                "-m",
                "pip",
                "install",
                "--requirement",
                staged_requirements,
            ],
        )
    if result.returncode != 0:
        reason = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise EnvironmentSyncError(
            "venv creation failed; recovery directory %s: %s"
            % (temporary_target, reason[:500] or "command failed")
        )
    os.unlink(staged_requirements)
    _rewrite_venv_embedded_paths(
        temporary_target, temporary_target, final_target
    )
    _rename_exclusive(temporary_target, final_target)
    os.unlink(os.path.join(final_target, ".chatmesh-incomplete.json"))
    return {"kind": "venv-create", "path": descriptor["path"]}


def apply_environment_plan(
    profile: EnvironmentProfile,
    plan: Mapping[str, Any],
    *,
    home: Optional[str] = None,
    dry_run: bool = False,
    runner: Optional[Runner] = None,
) -> dict:
    """Apply additive actions and return per-action failures without deletion."""

    invoke = runner or _default_runner
    actual_home = os.path.abspath(home or home_dir())
    applied = []
    failed = []
    for action in plan.get("actions", []):
        if dry_run:
            continue
        kind = action.get("kind")
        name = action.get("name")
        try:
            if kind == "venv-create":
                applied.append(
                    _create_venv(action, profile, invoke, actual_home)
                )
                continue
            argv = {
                "brew-tap": ["brew", "tap", name],
                "brew-formula": ["brew", "install", name],
                "brew-cask": ["brew", "install", "--cask", name],
                "pip-user": [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--user",
                    name,
                ],
                "pipx": ["pipx", "install", name],
                "uv-tool": ["uv", "tool", "install", name],
            }.get(kind)
            validator = (
                _valid_brew_name
                if kind in ("brew-tap", "brew-formula", "brew-cask")
                else _valid_python_name
            )
            if argv is None or not validator(name):
                raise EnvironmentSyncError("invalid environment action")
            result = _invoke_apply(invoke, argv)
            if result.returncode != 0:
                reason = (result.stderr or b"").decode(
                    "utf-8", "replace"
                ).strip()
                raise EnvironmentSyncError(
                    reason[:500] or "command exited %d" % result.returncode
                )
            applied.append({"kind": kind, "name": name})
        except (
            EnvironmentSyncError,
            OSError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            failed.append({"action": action, "reason": str(exc)})
    return {
        "ok": not failed,
        "dry_run": dry_run,
        "applied": applied,
        "failed": failed,
        "conflicts": list(plan.get("conflicts", [])),
        "blocked": list(plan.get("blocked", [])),
    }
