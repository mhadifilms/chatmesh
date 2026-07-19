"""Non-destructive local Git repository discovery and state handling.

This module deliberately contains no peer, SSH, fetch, or push orchestration.
Callers may move archives and Git objects by another mechanism, then use the
helpers here to inspect and safely apply local state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union
from urllib.parse import unquote, urlsplit


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
SNAPSHOT_FORMAT = "chatmesh-wip-v1"
_FS_ENCODING = os.sys.getfilesystemencoding()
_SAFE_REF_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class GitRepoError(RuntimeError):
    """Base error for local Git operations."""


class SnapshotError(GitRepoError):
    """A WIP snapshot is invalid or cannot be created."""


class UnsafeSnapshotError(SnapshotError):
    """Applying a snapshot would overwrite state that was not expected."""


class FastForwardError(GitRepoError):
    """A branch update was not a provable fast-forward."""


class DivergenceError(GitRepoError):
    """A divergence worktree could not safely be prepared."""


def mutation_blocker(
    repo: Union["GitRepository", str, os.PathLike]
) -> Optional[str]:
    """Return the active Git operation/lock that makes mutation unsafe."""
    repository = _coerce_repo(repo)
    checks = (
        (os.path.join(repository.git_dir, "index.lock"), "index.lock"),
        (os.path.join(repository.git_dir, "index.lock"), "worktree index.lock"),
        (os.path.join(repository.git_dir, "MERGE_HEAD"), "merge"),
        (os.path.join(repository.git_dir, "CHERRY_PICK_HEAD"), "cherry-pick"),
        (os.path.join(repository.git_dir, "REVERT_HEAD"), "revert"),
        (os.path.join(repository.git_dir, "BISECT_LOG"), "bisect"),
        (os.path.join(repository.git_dir, "rebase-apply"), "rebase"),
        (os.path.join(repository.git_dir, "rebase-merge"), "rebase"),
        (os.path.join(repository.git_dir, "sequencer"), "sequencer"),
    )
    for path, label in checks:
        if os.path.lexists(path):
            return label
    unmerged = _git(repository, ["ls-files", "-u", "-z"], check=False)
    if unmerged.returncode == 0 and unmerged.stdout:
        return "unmerged-index"
    return None


def require_mutation_safe(
    repo: Union["GitRepository", str, os.PathLike]
) -> None:
    blocker = mutation_blocker(repo)
    if blocker:
        raise UnsafeSnapshotError("repository has active %s state" % blocker)


@dataclass(frozen=True)
class GitRepository:
    """One discovered repository, retaining both namespace and disk paths."""

    logical_path: str
    real_path: str
    git_dir: str
    common_dir: str
    is_bare: bool
    kind: str

    @property
    def work_tree(self) -> Optional[str]:
        return None if self.is_bare else self.real_path


@dataclass(frozen=True)
class GitHubRepository:
    owner: str
    name: str
    repository_id: Optional[str] = None

    @property
    def slug(self) -> str:
        return "%s/%s" % (self.owner, self.name)

    @property
    def canonical(self) -> str:
        return "github.com/%s" % self.slug

    @property
    def identity(self) -> str:
        return (
            "github-id:%s" % self.repository_id
            if self.repository_id
            else "github:%s" % self.slug.lower()
        )

    @property
    def https_url(self) -> str:
        return "https://github.com/%s.git" % self.slug

    @property
    def ssh_url(self) -> str:
        return "git@github.com:%s.git" % self.slug


@dataclass(frozen=True)
class WorktreeMetadata:
    path: str
    head: Optional[str]
    branch: Optional[str]
    detached: bool = False
    bare: bool = False
    locked: Optional[str] = None
    prunable: Optional[str] = None


@dataclass(frozen=True)
class BranchMetadata:
    name: str
    oid: str
    upstream: Optional[str]
    upstream_oid: Optional[str]
    ahead: Optional[int]
    behind: Optional[int]
    worktrees: Tuple[str, ...]


@dataclass(frozen=True)
class TagMetadata:
    name: str
    oid: str
    peeled_oid: Optional[str]


@dataclass(frozen=True)
class RepositoryMetadata:
    identity: str
    origin: Optional[str]
    head: Optional[str]
    branch: Optional[str]
    detached: bool
    status_fingerprint: Optional[str]
    branches: Tuple[BranchMetadata, ...]
    tags: Tuple[TagMetadata, ...]
    worktrees: Tuple[WorktreeMetadata, ...]


@dataclass(frozen=True)
class WipSnapshot:
    manifest: Mapping[str, object]
    staged_patch: bytes
    payloads: Mapping[str, bytes]
    archive_path: Optional[str] = None

    @property
    def snapshot_id(self) -> str:
        return str(self.manifest["snapshot_id"])

    @property
    def status_fingerprint(self) -> str:
        return str(self.manifest["status_fingerprint"])


class Ancestry(str, Enum):
    EQUAL = "equal"
    FAST_FORWARD = "fast-forward"
    AHEAD = "ahead"
    DIVERGED = "diverged"
    UNRELATED = "unrelated"
    MISSING = "missing"


@dataclass(frozen=True)
class FastForwardResult:
    branch: str
    old_oid: str
    new_oid: str
    method: str
    backup_ref: Optional[str]
    worktree: Optional[str]


@dataclass(frozen=True)
class DivergenceWorktree:
    path: str
    branch: str
    local_oid: str
    incoming_oid: str
    incoming_ref: str


def _decode(data: bytes) -> str:
    return data.decode(_FS_ENCODING, "surrogateescape")


def _display(data: bytes) -> str:
    return data.decode("utf-8", "replace").strip()


def _git(
    repo: GitRepository,
    args: Sequence[str],
    *,
    input_data: Optional[bytes] = None,
    env: Optional[Mapping[str, str]] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    if repo.is_bare:
        argv = ["git", "--git-dir", repo.git_dir] + list(args)
    else:
        argv = ["git", "-C", repo.real_path] + list(args)
    proc_env = dict(os.environ)
    proc_env.update({"LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    if env:
        proc_env.update(env)
    proc = subprocess.run(
        argv,
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=proc_env,
    )
    if check and proc.returncode != 0:
        raise GitRepoError(
            "%s failed (%d): %s"
            % (" ".join(argv[:5]), proc.returncode, _display(proc.stderr)[:1000])
        )
    return proc


def _path_from_git_output(value: bytes, base: str) -> str:
    path = _decode(value).strip()
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    return os.path.realpath(path)


def _looks_bare(path: str) -> bool:
    return (
        os.path.isfile(os.path.join(path, "HEAD"))
        and os.path.isdir(os.path.join(path, "objects"))
        and os.path.isdir(os.path.join(path, "refs"))
    )


def open_repository(
    path: Union[str, os.PathLike], logical_path: Optional[Union[str, os.PathLike]] = None
) -> GitRepository:
    """Open a working tree, linked worktree, submodule, or bare repository."""

    logical = os.path.abspath(os.path.expanduser(os.fspath(logical_path or path)))
    real = os.path.realpath(os.path.expanduser(os.fspath(path)))
    if not os.path.isdir(real):
        raise GitRepoError("repository path is not a directory: %s" % logical)

    dotgit = os.path.join(real, ".git")
    is_bare_candidate = not os.path.lexists(dotgit) and _looks_bare(real)
    if not os.path.lexists(dotgit) and not is_bare_candidate:
        raise GitRepoError("not a Git repository boundary: %s" % logical)

    probe = GitRepository(logical, real, real, real, is_bare_candidate, "bare")
    if not is_bare_candidate:
        probe = GitRepository(logical, real, dotgit, dotgit, False, "worktree")

    git_dir_out = _git(probe, ["rev-parse", "--absolute-git-dir"]).stdout.strip()
    git_dir = _path_from_git_output(git_dir_out, real)
    common_out = _git(probe, ["rev-parse", "--git-common-dir"]).stdout.strip()
    common_dir = _path_from_git_output(common_out, real)
    bare_out = _git(probe, ["rev-parse", "--is-bare-repository"]).stdout.strip()
    is_bare = bare_out == b"true"

    kind = "bare" if is_bare else "worktree"
    if not is_bare and os.path.isfile(dotgit):
        normalized_git = git_dir.replace(os.sep, "/")
        if "/modules/" in normalized_git:
            kind = "submodule"
        elif git_dir != common_dir:
            kind = "linked-worktree"
        else:
            kind = "gitfile-worktree"
    return GitRepository(logical, real, git_dir, common_dir, is_bare, kind)


def _directory_identity(path: str) -> Tuple[int, int]:
    info = os.stat(path, follow_symlinks=True)
    return info.st_dev, info.st_ino


def discover_repositories(
    logical_roots: Union[
        str, os.PathLike, Iterable[Union[str, os.PathLike]]
    ]
) -> List[GitRepository]:
    """Recursively discover repositories without collapsing logical symlinks.

    Symlinked directory trees are followed.  Device/inode identities are kept
    per recursion chain, so cycles terminate while two intentional logical
    aliases may both be represented.
    """

    if isinstance(logical_roots, (str, bytes, os.PathLike)):
        roots = [logical_roots]
    else:
        roots = list(logical_roots)
    found: Dict[Tuple[str, str], GitRepository] = {}

    def walk(logical: str, ancestors: Set[Tuple[int, int]]) -> None:
        try:
            if not os.path.isdir(logical):
                return
            identity = _directory_identity(logical)
        except OSError:
            return
        if identity in ancestors:
            return
        next_ancestors = set(ancestors)
        next_ancestors.add(identity)

        repo: Optional[GitRepository] = None
        try:
            repo = open_repository(os.path.realpath(logical), logical)
        except GitRepoError:
            pass
        if repo is not None:
            found[(os.path.normpath(repo.logical_path), repo.git_dir)] = repo
            if repo.is_bare:
                return

        try:
            with os.scandir(logical) as scan:
                entries = sorted(list(scan), key=lambda item: item.name)
        except OSError:
            return
        for entry in entries:
            if entry.name == ".git":
                continue
            if (
                repo is not None
                and entry.name.startswith((".git.", ".git-"))
            ):
                # Archived/replaced Git metadata is not an independent
                # checkout.  Treating .git.old as a nested bare repository
                # would expose refs that Git itself no longer uses.
                continue
            try:
                if entry.is_dir(follow_symlinks=True):
                    walk(os.path.join(logical, entry.name), next_ancestors)
            except OSError:
                continue

    for configured in roots:
        root = os.path.abspath(os.path.expanduser(os.fspath(configured)))
        walk(root, set())
    return sorted(found.values(), key=lambda item: (item.logical_path, item.git_dir))


# Compatibility-friendly short alias for callers that prefer a verb.
discover = discover_repositories


def parse_github_origin(value: str) -> Optional[GitHubRepository]:
    """Parse GitHub SSH/HTTPS remotes while discarding credentials/query data."""

    raw = value.strip()
    if not raw:
        return None
    host = ""
    path = ""
    scp = re.match(r"^(?:[^/@:\s]+@)?([^/:\s]+):(.+)$", raw)
    if scp and "://" not in raw:
        host, path = scp.group(1), scp.group(2)
    else:
        parsed = urlsplit(raw)
        if parsed.scheme not in ("http", "https", "ssh", "git"):
            return None
        host = parsed.hostname or ""
        path = unquote(parsed.path).lstrip("/")
    if host.lower().rstrip(".") not in ("github.com", "www.github.com"):
        return None
    path = path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    owner, name = parts
    if any(part in (".", "..") or "\x00" in part for part in parts):
        return None
    return GitHubRepository(owner=owner, name=name)


def normalize_github_origin(value: str) -> Optional[str]:
    parsed = parse_github_origin(value)
    return parsed.canonical if parsed else None


normalize_github_remote = normalize_github_origin


def origin_url(repo: Union[GitRepository, str, os.PathLike]) -> Optional[str]:
    repository = _coerce_repo(repo)
    result = _git(
        repository, ["config", "--get", "remote.origin.url"], check=False
    )
    if result.returncode != 0:
        return None
    return _decode(result.stdout).strip() or None


def _root_commits(repo: GitRepository) -> List[str]:
    result = _git(
        repo, ["rev-list", "--max-parents=0", "--all"], check=False
    )
    if result.returncode != 0:
        return []
    return sorted(line for line in _display(result.stdout).splitlines() if line)


def derive_stable_identity(
    repo: Union[GitRepository, str, os.PathLike],
) -> str:
    """Return a clone-stable identity, preferring a normalized GitHub origin."""

    repository = _coerce_repo(repo)
    remote = origin_url(repository)
    parsed = parse_github_origin(remote or "")
    if parsed:
        return parsed.identity
    roots = _root_commits(repository)
    if roots:
        digest = hashlib.sha256(("\n".join(roots) + "\n").encode("ascii")).hexdigest()
        return "git-roots:%s" % digest
    # An empty local repository has no cross-machine identity.  This fallback is
    # stable for its common Git directory and explicitly names that limitation.
    digest = hashlib.sha256(
        os.path.realpath(repository.common_dir).encode(_FS_ENCODING, "surrogateescape")
    ).hexdigest()
    return "local-empty:%s" % digest


repo_identity = derive_stable_identity


def _load_resolution_cache(path: Optional[str]) -> Dict[str, object]:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_resolution_cache(path: str, data: Mapping[str, object]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".chatmesh-gh-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def resolve_github_repository(
    value: str,
    cache_path: Optional[Union[str, os.PathLike]] = None,
    *,
    allow_gh: bool = False,
) -> Optional[GitHubRepository]:
    """Resolve a GitHub slug, optionally consulting ``gh`` on explicit opt-in.

    The helper never requires ``gh``.  With ``allow_gh=False`` (the default) it
    is entirely local.  Cache entries contain only canonical public slugs.
    """

    if value.lower().startswith("github.com/"):
        value = "https://" + value
    parsed = parse_github_origin(value)
    candidate = parsed.slug if parsed else value.strip().strip("/")
    if candidate.lower().endswith(".git"):
        candidate = candidate[:-4]
    if len(candidate.split("/")) != 2:
        return None
    key = candidate.lower()
    cache_file = os.fspath(cache_path) if cache_path is not None else None
    cache = _load_resolution_cache(cache_file)
    cached = cache.get(key)
    cached_slug = None
    cached_id = None
    cached_at = 0
    if isinstance(cached, str):
        cached_slug = cached
    elif isinstance(cached, dict):
        cached_slug = cached.get("slug")
        cached_id = cached.get("id")
        cached_at = cached.get("checked_at", 0)
    cached_result = None
    if isinstance(cached_slug, str):
        parsed_cached = parse_github_origin(
            "https://github.com/%s" % cached_slug
        )
        if parsed_cached:
            cached_result = GitHubRepository(
                parsed_cached.owner,
                parsed_cached.name,
                str(cached_id) if cached_id else None,
            )
    if (
        cached_result
        and (
            not allow_gh
            or not isinstance(cached_at, int)
            or time.time() - cached_at < 24 * 60 * 60
        )
    ):
        return cached_result
    if not allow_gh or shutil.which("gh") is None:
        return cached_result or parsed or parse_github_origin(
            "https://github.com/%s" % candidate
        )
    proc = subprocess.run(
        ["gh", "repo", "view", candidate, "--json", "nameWithOwner,id"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ, GH_PROMPT_DISABLED="1", LC_ALL="C"),
    )
    if proc.returncode != 0:
        return cached_result or parsed or parse_github_origin(
            "https://github.com/%s" % candidate
        )
    try:
        response = json.loads(proc.stdout.decode("utf-8"))
        resolved = response["nameWithOwner"]
        repository_id = response.get("id")
    except (ValueError, KeyError, TypeError):
        return None
    parsed_result = parse_github_origin("https://github.com/%s" % resolved)
    result = (
        GitHubRepository(
            parsed_result.owner,
            parsed_result.name,
            str(repository_id) if repository_id else None,
        )
        if parsed_result
        else None
    )
    if result and cache_file:
        cache[key] = {
            "slug": result.slug,
            "id": result.repository_id,
            "checked_at": int(time.time()),
        }
        cache[result.slug.lower()] = {
            "slug": result.slug,
            "id": result.repository_id,
            "checked_at": int(time.time()),
        }
        _save_resolution_cache(cache_file, cache)
    return result


def _coerce_repo(
    repo: Union[GitRepository, str, os.PathLike]
) -> GitRepository:
    return repo if isinstance(repo, GitRepository) else open_repository(repo)


def _head_and_branch(repo: GitRepository) -> Tuple[Optional[str], Optional[str]]:
    head_result = _git(repo, ["rev-parse", "--verify", "HEAD"], check=False)
    head = _display(head_result.stdout) if head_result.returncode == 0 else None
    branch_result = _git(
        repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False
    )
    branch = _decode(branch_result.stdout).strip() if branch_result.returncode == 0 else None
    return head or None, branch or None


def list_worktrees(
    repo: Union[GitRepository, str, os.PathLike]
) -> Tuple[WorktreeMetadata, ...]:
    repository = _coerce_repo(repo)
    result = _git(repository, ["worktree", "list", "--porcelain"])
    records: List[WorktreeMetadata] = []
    current: Dict[str, object] = {}
    for raw_line in result.stdout.splitlines() + [b""]:
        if not raw_line:
            if current:
                records.append(
                    WorktreeMetadata(
                        path=str(current.get("worktree", "")),
                        head=current.get("HEAD") if isinstance(current.get("HEAD"), str) else None,
                        branch=current.get("branch") if isinstance(current.get("branch"), str) else None,
                        detached=bool(current.get("detached")),
                        bare=bool(current.get("bare")),
                        locked=current.get("locked") if isinstance(current.get("locked"), str) else None,
                        prunable=current.get("prunable") if isinstance(current.get("prunable"), str) else None,
                    )
                )
                current = {}
            continue
        key_bytes, _, value_bytes = raw_line.partition(b" ")
        key = _decode(key_bytes)
        value = _decode(value_bytes)
        if key == "branch" and value.startswith("refs/heads/"):
            value = value[len("refs/heads/") :]
        current[key] = value if value else True
    return tuple(records)


def _ahead_behind(
    repo: GitRepository, branch: str, upstream: str
) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    upstream_result = _git(
        repo, ["rev-parse", "--verify", "%s^{commit}" % upstream], check=False
    )
    if upstream_result.returncode != 0:
        return None, None, None
    upstream_oid = _display(upstream_result.stdout)
    counts = _git(
        repo,
        ["rev-list", "--left-right", "--count", "%s...%s" % (branch, upstream)],
        check=False,
    )
    if counts.returncode != 0:
        return None, None, upstream_oid
    fields = _display(counts.stdout).split()
    if len(fields) != 2:
        return None, None, upstream_oid
    return int(fields[0]), int(fields[1]), upstream_oid


def repository_metadata(
    repo: Union[GitRepository, str, os.PathLike],
    *,
    include_status_fingerprint: bool = True,
) -> RepositoryMetadata:
    repository = _coerce_repo(repo)
    head, branch = _head_and_branch(repository)
    worktrees = list_worktrees(repository)
    checked_out: Dict[str, List[str]] = {}
    for worktree in worktrees:
        if worktree.branch:
            checked_out.setdefault(worktree.branch, []).append(worktree.path)

    branches: List[BranchMetadata] = []
    raw_branches = _git(
        repository,
        [
            "for-each-ref",
            "--format=%(refname:short)%00%(objectname)%00%(upstream:short)",
            "refs/heads",
        ],
    )
    for line in raw_branches.stdout.splitlines():
        fields = line.split(b"\x00")
        if len(fields) != 3:
            continue
        name, oid, upstream = (_decode(field) for field in fields)
        ahead = behind = None
        upstream_oid = None
        if upstream:
            ahead, behind, upstream_oid = _ahead_behind(repository, name, upstream)
        branches.append(
            BranchMetadata(
                name=name,
                oid=oid,
                upstream=upstream or None,
                upstream_oid=upstream_oid,
                ahead=ahead,
                behind=behind,
                worktrees=tuple(sorted(checked_out.get(name, []))),
            )
        )

    tags: List[TagMetadata] = []
    raw_tags = _git(
        repository,
        [
            "for-each-ref",
            "--format=%(refname:short)%00%(objectname)%00%(*objectname)",
            "refs/tags",
        ],
    )
    for line in raw_tags.stdout.splitlines():
        fields = line.split(b"\x00")
        if len(fields) == 3:
            tags.append(
                TagMetadata(
                    _decode(fields[0]),
                    _decode(fields[1]),
                    _decode(fields[2]) or None,
                )
            )
    fingerprint = (
        status_fingerprint(repository)
        if include_status_fingerprint and not repository.is_bare
        else None
    )
    normalized_origin = normalize_github_origin(origin_url(repository) or "")
    return RepositoryMetadata(
        identity=derive_stable_identity(repository),
        origin=normalized_origin,
        head=head,
        branch=branch,
        detached=head is not None and branch is None,
        status_fingerprint=fingerprint,
        branches=tuple(sorted(branches, key=lambda item: item.name)),
        tags=tuple(sorted(tags, key=lambda item: item.name)),
        worktrees=worktrees,
    )


collect_metadata = repository_metadata


def _validate_git_path(path: str) -> str:
    if not path or "\x00" in path or os.path.isabs(path):
        raise SnapshotError("unsafe repository-relative path")
    pieces = path.split("/")
    if any(piece in ("", ".", "..") for piece in pieces):
        raise SnapshotError("unsafe repository-relative path: %r" % path)
    return path


def _safe_worktree_path(root: str, git_path: str) -> str:
    path = _validate_git_path(git_path)
    candidate = os.path.abspath(os.path.join(root, *path.split("/")))
    if os.path.commonpath([os.path.abspath(root), candidate]) != os.path.abspath(root):
        raise UnsafeSnapshotError("path escapes worktree: %r" % git_path)
    parent = os.path.abspath(root)
    for component in path.split("/")[:-1]:
        parent = os.path.join(parent, component)
        if os.path.lexists(parent) and os.path.islink(parent):
            raise UnsafeSnapshotError("symlink parent blocks safe write: %r" % git_path)
    return candidate


def _split_nul(data: bytes) -> List[str]:
    return [_decode(item) for item in data.split(b"\x00") if item]


def _has_head(repo: GitRepository) -> bool:
    return _git(repo, ["rev-parse", "--verify", "HEAD"], check=False).returncode == 0


def _staged_patch(repo: GitRepository) -> bytes:
    base = "HEAD" if _has_head(repo) else EMPTY_TREE
    return _git(
        repo,
        [
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--ignore-submodules=none",
            base,
            "--",
        ],
    ).stdout


def _changed_paths(
    repo: GitRepository,
) -> Tuple[Set[str], Set[str], Set[str], Set[str]]:
    base = "HEAD" if _has_head(repo) else EMPTY_TREE
    staged = set(
        _split_nul(
            _git(
                repo,
                [
                    "diff",
                    "--cached",
                    "--name-only",
                    "-z",
                    "--no-renames",
                    "--ignore-submodules=none",
                    base,
                    "--",
                ],
            ).stdout
        )
    )
    unstaged = set(
        _split_nul(
            _git(
                repo,
                [
                    "diff",
                    "--name-only",
                    "-z",
                    "--no-renames",
                    "--ignore-submodules=all",
                    "--",
                ],
            ).stdout
        )
    )
    untracked = set(
        _split_nul(
            _git(
                repo, ["ls-files", "--others", "--exclude-standard", "-z", "--"]
            ).stdout
        )
    )
    ignored = set(
        _split_nul(
            _git(
                repo,
                ["ls-files", "--others", "--ignored", "--exclude-standard",
                 "-z", "--"],
            ).stdout
        )
    )
    return staged, unstaged, untracked, ignored


def _nested_boundaries(repo: GitRepository) -> Set[str]:
    boundaries: Set[str] = set()
    for nested in discover_repositories(repo.real_path):
        if nested.real_path == repo.real_path and nested.git_dir == repo.git_dir:
            continue
        logical = nested.logical_path
        if os.path.islink(logical):
            continue
        try:
            relative = os.path.relpath(logical, repo.real_path)
        except ValueError:
            continue
        if relative != ".." and not relative.startswith(".." + os.sep):
            boundaries.add(relative.replace(os.sep, "/").rstrip("/"))
    return boundaries


def _inside_boundary(path: str, boundaries: Set[str]) -> bool:
    normalized = path.rstrip("/")
    return any(
        normalized == boundary or normalized.startswith(boundary + "/")
        for boundary in boundaries
    )


def _blob_state(repo: GitRepository, oid: str, mode: str) -> Dict[str, object]:
    state: Dict[str, object] = {
        "present": True,
        "mode": mode,
        "type": "gitlink" if mode == "160000" else (
            "symlink" if mode == "120000" else "file"
        ),
        "git_oid": oid,
    }
    if mode == "160000":
        return state
    data = _git(repo, ["cat-file", "blob", oid]).stdout
    state["sha256"] = hashlib.sha256(data).hexdigest()
    state["size"] = len(data)
    state["_data"] = data
    return state


def _index_state(repo: GitRepository, path: str) -> Dict[str, object]:
    output = _git(repo, ["ls-files", "--stage", "-z", "--", path]).stdout
    records = [record for record in output.split(b"\x00") if record]
    if not records:
        return {"present": False}
    parsed: List[Tuple[str, str, str]] = []
    for record in records:
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 3:
            raise SnapshotError("could not parse index entry for %r" % path)
        mode, oid, stage = (_decode(field) for field in fields)
        if _decode(raw_path) == path:
            parsed.append((mode, oid, stage))
    if len(parsed) != 1 or parsed[0][2] != "0":
        raise SnapshotError("unmerged index entry cannot be snapshotted: %r" % path)
    return _blob_state(repo, parsed[0][1], parsed[0][0])


def _head_state(repo: GitRepository, path: str) -> Dict[str, object]:
    if not _has_head(repo):
        return {"present": False}
    output = _git(repo, ["ls-tree", "-z", "HEAD", "--", path]).stdout
    for record in output.split(b"\x00"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split()
        if separator and len(fields) == 3 and _decode(raw_path) == path:
            mode, _object_type, oid = (_decode(field) for field in fields)
            state = _blob_state(repo, oid, mode)
            state.pop("_data", None)
            return state
    return {"present": False}


def _head_state_with_data(repo: GitRepository, path: str) -> Dict[str, object]:
    if not _has_head(repo):
        return {"present": False}
    output = _git(repo, ["ls-tree", "-z", "HEAD", "--", path]).stdout
    for record in output.split(b"\x00"):
        if not record:
            continue
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split()
        if separator and len(fields) == 3 and _decode(raw_path) == path:
            mode, _object_type, oid = (_decode(field) for field in fields)
            return _blob_state(repo, oid, mode)
    return {"present": False}


def _worktree_state(repo: GitRepository, path: str) -> Dict[str, object]:
    assert repo.work_tree is not None
    absolute = _safe_worktree_path(repo.work_tree, path)
    try:
        info = os.lstat(absolute)
    except FileNotFoundError:
        return {"present": False}
    if stat.S_ISLNK(info.st_mode):
        data = os.readlink(absolute).encode(_FS_ENCODING, "surrogateescape")
        mode = "120000"
        kind = "symlink"
    elif stat.S_ISREG(info.st_mode):
        with open(absolute, "rb") as handle:
            data = handle.read()
        mode = "100755" if info.st_mode & 0o111 else "100644"
        kind = "file"
    elif stat.S_ISDIR(info.st_mode):
        raise SnapshotError("refusing to archive directory as a file: %r" % path)
    else:
        raise SnapshotError("unsupported worktree file type: %r" % path)
    return {
        "present": True,
        "mode": mode,
        "type": kind,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "_data": data,
    }


def _public_state(
    state: Dict[str, object], payloads: Dict[str, bytes]
) -> Dict[str, object]:
    public = dict(state)
    data = public.pop("_data", None)
    if isinstance(data, bytes):
        digest = str(public["sha256"])
        payloads.setdefault(digest, data)
        public["payload"] = "payload/%s" % digest
    return public


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _fingerprint(patch: bytes, entries: Sequence[Mapping[str, object]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"chatmesh-status-v1\x00")
    digest.update(hashlib.sha256(patch).digest())
    digest.update(_canonical_json(entries))
    return digest.hexdigest()


def _collect_wip(
    repo: GitRepository,
    *,
    include_staged: bool = True,
    include_unstaged: bool = True,
    include_untracked: bool = True,
    include_ignored: bool = False,
) -> Tuple[bytes, List[Dict[str, object]], Dict[str, bytes]]:
    if repo.is_bare:
        raise SnapshotError("bare repositories have no worktree WIP")
    patch = _staged_patch(repo) if include_staged else b""
    staged_all, unstaged_all, untracked_all, ignored_all = _changed_paths(repo)
    staged = staged_all if include_staged else set()
    unstaged = unstaged_all if include_unstaged else set()
    untracked = untracked_all if include_untracked else set()
    ignored = ignored_all if include_ignored else set()
    boundaries = _nested_boundaries(repo)
    # Nested repository contents belong to that repository, never its parent.
    unstaged = {path for path in unstaged if not _inside_boundary(path, boundaries)}
    untracked = {path for path in untracked if not _inside_boundary(path, boundaries)}
    ignored = {path for path in ignored if not _inside_boundary(path, boundaries)}
    all_paths = staged | unstaged | untracked | ignored
    payloads: Dict[str, bytes] = {}
    entries: List[Dict[str, object]] = []
    for path in sorted(all_paths):
        _validate_git_path(path)
        head_with_data = _head_state_with_data(repo, path)
        head_state = dict(head_with_data)
        head_state.pop("_data", None)
        index_state = (
            _index_state(repo, path) if include_staged else head_with_data
        )
        boundary = _inside_boundary(path, boundaries) or any(
            state.get("type") == "gitlink" for state in (head_state, index_state)
        )
        if path in unstaged or path in untracked or path in ignored:
            worktree_state = _worktree_state(repo, path)
        elif path in staged:
            worktree_state = index_state
        else:
            worktree_state = {"present": False}
        entry: Dict[str, object] = {
            "path": path,
            "staged": path in staged,
            "unstaged": path in unstaged,
            "untracked": path in untracked,
            "ignored": path in ignored,
            "boundary": boundary,
            "head": head_state,
            "index": _public_state(index_state, payloads),
            "worktree": None
            if boundary
            else _public_state(worktree_state, payloads),
        }
        entries.append(entry)
    return patch, entries, payloads


def status_fingerprint(
    repo: Union[GitRepository, str, os.PathLike],
    *,
    include_staged: bool = True,
    include_unstaged: bool = True,
    include_untracked: bool = True,
    include_ignored: bool = False,
) -> str:
    repository = _coerce_repo(repo)
    patch, entries, _payloads = _collect_wip(
        repository,
        include_staged=include_staged,
        include_unstaged=include_unstaged,
        include_untracked=include_untracked,
        include_ignored=include_ignored,
    )
    return _fingerprint(patch, entries)


def _build_manifest(
    repo: GitRepository,
    patch: bytes,
    entries: List[Dict[str, object]],
    policy: Mapping[str, bool],
) -> Dict[str, object]:
    head, branch = _head_and_branch(repo)
    manifest: Dict[str, object] = {
        "format": SNAPSHOT_FORMAT,
        "repository": {
            "identity": derive_stable_identity(repo),
            "head": head,
            "branch": branch,
        },
        "staged_patch": {
            "member": "staged.patch",
            "sha256": hashlib.sha256(patch).hexdigest(),
            "size": len(patch),
        },
        "entries": entries,
        "policy": dict(policy),
        "status_fingerprint": _fingerprint(patch, entries),
    }
    snapshot_seed = _canonical_json(manifest)
    manifest["snapshot_id"] = hashlib.sha256(
        b"chatmesh-snapshot-v1\x00" + snapshot_seed
    ).hexdigest()
    return manifest


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def create_wip_snapshot(
    repo: Union[GitRepository, str, os.PathLike],
    archive_path: Union[str, os.PathLike],
    *,
    max_file_bytes: int = 50 * 1024 * 1024,
    max_snapshot_bytes: int = 1024 * 1024 * 1024,
    include_staged: bool = True,
    include_unstaged: bool = True,
    include_untracked: bool = True,
    include_ignored: bool = False,
) -> WipSnapshot:
    """Write a deterministic, content-addressed WIP archive."""

    repository = _coerce_repo(repo)
    policy = {
        "staged": bool(include_staged),
        "unstaged": bool(include_unstaged),
        "untracked": bool(include_untracked),
        "ignored": bool(include_ignored),
    }
    patch, entries, payloads = _collect_wip(
        repository,
        include_staged=policy["staged"],
        include_unstaged=policy["unstaged"],
        include_untracked=policy["untracked"],
        include_ignored=policy["ignored"],
    )
    if max_file_bytes < 0 or max_snapshot_bytes < 0:
        raise SnapshotError("snapshot size limits must be nonnegative")
    oversized = [digest for digest, data in payloads.items()
                 if len(data) > max_file_bytes]
    if oversized:
        raise SnapshotError("WIP contains a file larger than max_file_bytes")
    total_size = len(patch) + sum(len(data) for data in payloads.values())
    if total_size > max_snapshot_bytes:
        raise SnapshotError("WIP exceeds max_snapshot_bytes")
    manifest = _build_manifest(repository, patch, entries, policy)
    destination = os.path.abspath(os.fspath(archive_path))
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".chatmesh-snapshot-", dir=os.path.dirname(destination)
    )
    os.close(fd)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            members: Dict[str, bytes] = {
                "manifest.json": _canonical_json(manifest) + b"\n",
                "staged.patch": patch,
            }
            for digest, data in payloads.items():
                members["payload/%s" % digest] = data
            for name in sorted(members):
                archive.writestr(_zip_info(name), members[name])
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return WipSnapshot(manifest, patch, payloads, destination)


snapshot_wip = create_wip_snapshot


def _validate_archive_member(name: str) -> None:
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or "\x00" in name
        or any(piece in ("", ".", "..") for piece in name.split("/"))
    ):
        raise SnapshotError("unsafe archive member: %r" % name)
    if name not in ("manifest.json", "staged.patch") and not re.match(
        r"^payload/[0-9a-f]{64}$", name
    ):
        raise SnapshotError("unexpected archive member: %r" % name)


def read_wip_snapshot(
    archive_path: Union[str, os.PathLike],
) -> WipSnapshot:
    path = os.path.abspath(os.fspath(archive_path))
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise SnapshotError("archive contains duplicate members")
            total_size = 0
            for info in infos:
                _validate_archive_member(info.filename)
                if info.flag_bits & 0x1:
                    raise SnapshotError("encrypted archives are not supported")
                total_size += info.file_size
                if total_size > 8 * 1024 * 1024 * 1024:
                    raise SnapshotError("snapshot is unreasonably large")
            if "manifest.json" not in names or "staged.patch" not in names:
                raise SnapshotError("snapshot is missing required members")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            patch = archive.read("staged.patch")
            payloads = {
                name.split("/", 1)[1]: archive.read(name)
                for name in names
                if name.startswith("payload/")
            }
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        if isinstance(error, SnapshotError):
            raise
        raise SnapshotError("could not read snapshot: %s" % error) from error
    _validate_snapshot(manifest, patch, payloads)
    return WipSnapshot(manifest, patch, payloads, path)


read_snapshot = read_wip_snapshot


def _validate_state_descriptor(
    state: object,
    payloads: Mapping[str, bytes],
    *,
    allow_none: bool = False,
    require_payload: bool = True,
    require_git_oid: bool = False,
) -> None:
    if state is None and allow_none:
        return
    if not isinstance(state, dict) or not isinstance(state.get("present"), bool):
        raise SnapshotError("invalid snapshot file descriptor")
    if not state["present"]:
        return
    mode = state.get("mode")
    kind = state.get("type")
    if mode not in ("100644", "100755", "120000", "160000"):
        raise SnapshotError("invalid snapshot mode")
    if kind not in ("file", "symlink", "gitlink"):
        raise SnapshotError("invalid snapshot file type")
    expected_kind = (
        "gitlink" if mode == "160000"
        else "symlink" if mode == "120000"
        else "file"
    )
    if kind != expected_kind:
        raise SnapshotError("snapshot mode and file type disagree")
    oid = state.get("git_oid")
    if (require_git_oid or kind == "gitlink" or oid is not None) and (
        not isinstance(oid, str) or not re.match(r"^[0-9a-f]{40,64}$", oid)
    ):
        raise SnapshotError("invalid Git object id")
    if kind == "gitlink":
        return
    digest = state.get("sha256")
    member = state.get("payload")
    if not isinstance(digest, str) or not _SHA256.match(digest):
        raise SnapshotError("invalid payload hash")
    size = state.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise SnapshotError("invalid payload size")
    if not require_payload:
        if member is not None:
            raise SnapshotError("base descriptor unexpectedly references payload")
        return
    if member != "payload/%s" % digest or digest not in payloads:
        raise SnapshotError("missing content-addressed payload")
    data = payloads[digest]
    if hashlib.sha256(data).hexdigest() != digest or state.get("size") != len(data):
        raise SnapshotError("payload hash or size mismatch")


def _validate_snapshot(
    manifest: object, patch: bytes, payloads: Mapping[str, bytes]
) -> None:
    if not isinstance(manifest, dict) or manifest.get("format") != SNAPSHOT_FORMAT:
        raise SnapshotError("unsupported snapshot format")
    repository = manifest.get("repository")
    if not isinstance(repository, dict):
        raise SnapshotError("snapshot lacks repository identity")
    if not isinstance(repository.get("identity"), str):
        raise SnapshotError("snapshot repository identity is invalid")
    head = repository.get("head")
    branch = repository.get("branch")
    if head is not None and (
        not isinstance(head, str) or not re.match(r"^[0-9a-f]{40,64}$", head)
    ):
        raise SnapshotError("snapshot HEAD is invalid")
    if branch is not None and not isinstance(branch, str):
        raise SnapshotError("snapshot branch is invalid")
    patch_info = manifest.get("staged_patch")
    if (
        not isinstance(patch_info, dict)
        or patch_info.get("member") != "staged.patch"
        or patch_info.get("sha256") != hashlib.sha256(patch).hexdigest()
        or patch_info.get("size") != len(patch)
    ):
        raise SnapshotError("staged patch hash or size mismatch")
    policy = manifest.get("policy")
    if (
        not isinstance(policy, dict)
        or set(policy) != {"staged", "unstaged", "untracked", "ignored"}
        or any(type(value) is not bool for value in policy.values())
    ):
        raise SnapshotError("snapshot capture policy is invalid")
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise SnapshotError("snapshot entries are invalid")
    seen: Set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise SnapshotError("invalid snapshot entry")
        path = _validate_git_path(entry["path"])
        if path in seen:
            raise SnapshotError("duplicate snapshot path: %r" % path)
        seen.add(path)
        for flag in ("staged", "unstaged", "untracked", "ignored", "boundary"):
            if not isinstance(entry.get(flag), bool):
                raise SnapshotError("snapshot status flags are invalid")
        _validate_state_descriptor(
            entry.get("head"),
            {},
            allow_none=False,
            require_payload=False,
            require_git_oid=True,
        )
        _validate_state_descriptor(
            entry.get("index"),
            payloads,
            allow_none=False,
            require_git_oid=True,
        )
        _validate_state_descriptor(entry.get("worktree"), payloads, allow_none=True)
    expected_fingerprint = _fingerprint(patch, entries)
    if manifest.get("status_fingerprint") != expected_fingerprint:
        raise SnapshotError("snapshot status fingerprint mismatch")
    seed = dict(manifest)
    snapshot_id = seed.pop("snapshot_id", None)
    expected_id = hashlib.sha256(
        b"chatmesh-snapshot-v1\x00" + _canonical_json(seed)
    ).hexdigest()
    if snapshot_id != expected_id:
        raise SnapshotError("snapshot id mismatch")
    referenced = {
        state["sha256"]
        for entry in entries
        for state in (entry.get("index"), entry.get("worktree"))
        if isinstance(state, dict)
        and state.get("present")
        and state.get("type") != "gitlink"
    }
    if set(payloads) != referenced:
        raise SnapshotError("archive has unreferenced payloads")


def _temporary_index_preflight(repo: GitRepository, snapshot: WipSnapshot) -> None:
    fd, index_path = tempfile.mkstemp(prefix="chatmesh-index-", dir=repo.common_dir)
    os.close(fd)
    os.unlink(index_path)
    env = {"GIT_INDEX_FILE": index_path}
    try:
        if _has_head(repo):
            _git(repo, ["read-tree", "HEAD"], env=env)
        else:
            _git(repo, ["read-tree", "--empty"], env=env)
        if snapshot.staged_patch:
            check = _git(
                repo,
                ["apply", "--cached", "--binary", "--check", "-"],
                input_data=snapshot.staged_patch,
                env=env,
                check=False,
            )
            if check.returncode != 0:
                raise UnsafeSnapshotError(
                    "staged patch does not apply to the expected HEAD: %s"
                    % _display(check.stderr)
                )
            _git(
                repo,
                ["apply", "--cached", "--binary", "-"],
                input_data=snapshot.staged_patch,
                env=env,
            )
        for entry in snapshot.manifest["entries"]:
            actual_head = _head_state(repo, entry["path"])
            if _state_signature(actual_head) != _state_signature(entry["head"]):
                raise UnsafeSnapshotError(
                    "snapshot base differs for %r" % entry["path"]
                )
            expected = entry["index"]
            actual = _index_state_with_env(repo, entry["path"], env)
            if _state_signature(actual) != _state_signature(expected):
                raise UnsafeSnapshotError(
                    "temporary index differs for %r" % entry["path"]
                )
    finally:
        for suffix in ("", ".lock"):
            candidate = index_path + suffix
            if os.path.exists(candidate):
                os.unlink(candidate)


def _index_state_with_env(
    repo: GitRepository, path: str, env: Mapping[str, str]
) -> Dict[str, object]:
    output = _git(
        repo, ["ls-files", "--stage", "-z", "--", path], env=env
    ).stdout
    records = [record for record in output.split(b"\x00") if record]
    if not records:
        return {"present": False}
    if len(records) != 1:
        raise SnapshotError("temporary index is unmerged")
    header, _, raw_path = records[0].partition(b"\t")
    mode, oid, stage = (_decode(field) for field in header.split())
    if stage != "0" or _decode(raw_path) != path:
        raise SnapshotError("temporary index entry is invalid")
    return _blob_state(repo, oid, mode)


def _state_signature(state: Mapping[str, object]) -> Tuple[object, ...]:
    if not state.get("present"):
        return (False,)
    return (
        True,
        state.get("mode"),
        state.get("type"),
        state.get("git_oid"),
        state.get("sha256"),
        state.get("size"),
    )


def _work_state_signature(state: Mapping[str, object]) -> Tuple[object, ...]:
    if not state.get("present"):
        return (False,)
    return (
        True,
        state.get("mode"),
        state.get("type"),
        state.get("sha256"),
        state.get("size"),
    )


def _preflight_worktree(
    repo: GitRepository,
    snapshot: WipSnapshot,
    prior: Optional[WipSnapshot] = None,
) -> None:
    assert repo.work_tree is not None
    paths = sorted(entry["path"] for entry in snapshot.manifest["entries"])
    for previous, current in zip(paths, paths[1:]):
        if current.startswith(previous + "/"):
            raise UnsafeSnapshotError("snapshot contains a file/directory collision")
    prior_entries = (
        {entry["path"]: entry for entry in prior.manifest["entries"]}
        if prior is not None
        else {}
    )
    for entry in snapshot.manifest["entries"]:
        if entry.get("boundary"):
            continue
        _safe_worktree_path(repo.work_tree, entry["path"])
        incoming_state = entry.get("worktree")
        if (
            isinstance(incoming_state, dict)
            and incoming_state.get("present")
            and incoming_state.get("type") == "symlink"
        ):
            data = snapshot.payloads[str(incoming_state["sha256"])]
            _validate_symlink_target(repo.work_tree, str(entry["path"]), data)
        expected = entry["head"]
        prior_entry = prior_entries.get(entry["path"])
        if prior_entry is not None and not prior_entry.get("boundary"):
            expected = prior_entry["worktree"]
        try:
            actual = _worktree_state(repo, entry["path"])
        except SnapshotError as error:
            raise UnsafeSnapshotError(str(error)) from error
        if expected is None or _work_state_signature(actual) != _work_state_signature(expected):
            raise UnsafeSnapshotError(
                "worktree path has unrecorded state: %r" % entry["path"]
            )


def _atomic_write_file(path: str, data: bytes, mode: str) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".chatmesh-write-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temporary, 0o755 if mode == "100755" else 0o644)
        if os.path.isdir(path) and not os.path.islink(path):
            raise UnsafeSnapshotError("directory blocks snapshot path: %s" % path)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _validate_symlink_target(root: str, path: str, target_data: bytes) -> str:
    target = target_data.decode(_FS_ENCODING, "surrogateescape")
    if not target or "\x00" in target or os.path.isabs(target):
        raise UnsafeSnapshotError("unsafe symlink target for %r" % path)
    parent = os.path.dirname(_safe_worktree_path(root, path))
    resolved = os.path.realpath(os.path.join(parent, target))
    base = os.path.realpath(root)
    try:
        inside = os.path.commonpath([base, resolved]) == base
    except ValueError:
        inside = False
    if not inside:
        raise UnsafeSnapshotError("symlink target escapes repository for %r" % path)
    return target


def _atomic_write_symlink(
    path: str,
    target_data: bytes,
    *,
    root: Optional[str] = None,
    relative_path: str = "",
) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    temporary = os.path.join(parent, ".chatmesh-link-%s" % uuid.uuid4().hex)
    target = (
        _validate_symlink_target(root, relative_path, target_data)
        if root is not None
        else target_data.decode(_FS_ENCODING, "surrogateescape")
    )
    try:
        os.symlink(target, temporary)
        if os.path.isdir(path) and not os.path.islink(path):
            raise UnsafeSnapshotError("directory blocks snapshot symlink: %s" % path)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _remove_file_path(path: str) -> None:
    if not os.path.lexists(path):
        return
    if os.path.isdir(path) and not os.path.islink(path):
        raise UnsafeSnapshotError("refusing to recursively remove directory: %s" % path)
    os.unlink(path)


def _materialize_snapshot_worktree(
    repo: GitRepository, snapshot: WipSnapshot
) -> None:
    assert repo.work_tree is not None
    for entry in snapshot.manifest["entries"]:
        if entry.get("boundary"):
            continue
        state = entry.get("worktree")
        if state is None:
            continue
        path = _safe_worktree_path(repo.work_tree, entry["path"])
        if not state["present"]:
            _remove_file_path(path)
            continue
        if state["type"] == "gitlink":
            continue
        data = snapshot.payloads[state["sha256"]]
        if state["type"] == "symlink":
            _atomic_write_symlink(
                path,
                data,
                root=repo.work_tree,
                relative_path=str(entry["path"]),
            )
        else:
            _atomic_write_file(path, data, state["mode"])


def _apply_snapshot_low_level(repo: GitRepository, snapshot: WipSnapshot) -> None:
    if snapshot.staged_patch:
        _git(
            repo,
            ["apply", "--cached", "--binary", "-"],
            input_data=snapshot.staged_patch,
        )
    _materialize_snapshot_worktree(repo, snapshot)


def _restore_head_paths(
    repo: GitRepository, snapshots: Sequence[WipSnapshot]
) -> None:
    if _has_head(repo):
        _git(repo, ["read-tree", "HEAD"])
    else:
        _git(repo, ["read-tree", "--empty"])
    assert repo.work_tree is not None
    entries: Dict[str, Mapping[str, object]] = {}
    for snapshot in snapshots:
        for entry in snapshot.manifest["entries"]:
            entries[entry["path"]] = entry
    for path, entry in sorted(entries.items()):
        if entry.get("boundary"):
            continue
        destination = _safe_worktree_path(repo.work_tree, path)
        head_state = entry["head"]
        if not head_state["present"]:
            _remove_file_path(destination)
            continue
        if head_state["type"] == "gitlink":
            continue
        data = _git(repo, ["cat-file", "blob", head_state["git_oid"]]).stdout
        if head_state["type"] == "symlink":
            _atomic_write_symlink(destination, data)
        else:
            _atomic_write_file(destination, data, head_state["mode"])


def _journal_root(repo: GitRepository) -> str:
    root = os.path.join(repo.common_dir, "chatmesh")
    os.makedirs(os.path.join(root, "backups"), exist_ok=True)
    os.makedirs(os.path.join(root, "journals"), exist_ok=True)
    return root


def _write_json_atomic(path: str, value: Mapping[str, object]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".chatmesh-journal-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _coerce_snapshot(
    snapshot: Union[WipSnapshot, str, os.PathLike]
) -> WipSnapshot:
    if isinstance(snapshot, WipSnapshot):
        _validate_snapshot(
            snapshot.manifest, snapshot.staged_patch, snapshot.payloads
        )
        return snapshot
    return read_wip_snapshot(snapshot)


def _snapshot_repo_expectations(snapshot: WipSnapshot) -> Tuple[object, object, object]:
    repository = snapshot.manifest["repository"]
    return repository["identity"], repository.get("head"), repository.get("branch")


def _snapshot_policy_kwargs(snapshot: WipSnapshot) -> Dict[str, bool]:
    policy = snapshot.manifest["policy"]
    return {
        "include_staged": bool(policy["staged"]),
        "include_unstaged": bool(policy["unstaged"]),
        "include_untracked": bool(policy["untracked"]),
        "include_ignored": bool(policy["ignored"]),
    }


def apply_wip_snapshot(
    repo: Union[GitRepository, str, os.PathLike],
    snapshot: Union[WipSnapshot, str, os.PathLike],
    *,
    prior_snapshot: Optional[Union[WipSnapshot, str, os.PathLike]] = None,
) -> Mapping[str, object]:
    """Safely apply WIP only to its matching branch and HEAD.

    A dirty destination is accepted only when it exactly matches the supplied
    prior snapshot.  Every mutation is preceded by temporary-index validation,
    a backup snapshot, and a recoverable journal.
    """

    repository = _coerce_repo(repo)
    if repository.is_bare:
        raise UnsafeSnapshotError("cannot apply WIP to a bare repository")
    require_mutation_safe(repository)
    incoming = _coerce_snapshot(snapshot)
    prior = _coerce_snapshot(prior_snapshot) if prior_snapshot is not None else None
    expected_identity, expected_head, expected_branch = _snapshot_repo_expectations(incoming)
    head, branch = _head_and_branch(repository)
    if head != expected_head or branch != expected_branch:
        raise UnsafeSnapshotError(
            "snapshot expects HEAD %s on %r, found %s on %r"
            % (expected_head, expected_branch, head, branch)
        )
    actual_identity = derive_stable_identity(repository)
    if actual_identity != expected_identity:
        raise UnsafeSnapshotError("snapshot repository identity does not match")

    policy_kwargs = _snapshot_policy_kwargs(incoming)
    current_fingerprint = status_fingerprint(repository, **policy_kwargs)
    if current_fingerprint == incoming.status_fingerprint:
        return {
            "ok": True,
            "already_applied": True,
            "snapshot_id": incoming.snapshot_id,
        }
    clean_fingerprint = _fingerprint(b"", [])
    replacing_prior = False
    if current_fingerprint != clean_fingerprint:
        if prior is None or current_fingerprint != prior.status_fingerprint:
            raise UnsafeSnapshotError(
                "destination is dirty and does not match the prior snapshot"
            )
        prior_identity, prior_head, prior_branch = _snapshot_repo_expectations(prior)
        if (prior_identity, prior_head, prior_branch) != (
            expected_identity,
            expected_head,
            expected_branch,
        ):
            raise UnsafeSnapshotError("prior snapshot has a different base")
        replacing_prior = True

    _temporary_index_preflight(repository, incoming)
    _preflight_worktree(repository, incoming, prior if replacing_prior else None)
    root = _journal_root(repository)
    operation_id = "%d-%s" % (int(time.time()), uuid.uuid4().hex)
    backup_path = os.path.join(root, "backups", "%s.zip" % operation_id)
    backup = create_wip_snapshot(
        repository,
        backup_path,
        max_file_bytes=8 * 1024 * 1024 * 1024,
        max_snapshot_bytes=8 * 1024 * 1024 * 1024,
        **policy_kwargs,
    )
    journal_path = os.path.join(root, "journals", "%s.json" % operation_id)
    journal: Dict[str, object] = {
        "format": "chatmesh-apply-journal-v1",
        "state": "prepared",
        "operation_id": operation_id,
        "backup": os.path.relpath(backup_path, root),
        "base_head": head,
        "base_branch": branch,
        "incoming_snapshot_id": incoming.snapshot_id,
        "incoming_status_fingerprint": incoming.status_fingerprint,
        "incoming_manifest": incoming.manifest,
    }
    _write_json_atomic(journal_path, journal)
    try:
        if replacing_prior and prior is not None:
            _restore_head_paths(repository, [prior])
        _apply_snapshot_low_level(repository, incoming)
        if status_fingerprint(repository, **policy_kwargs) != incoming.status_fingerprint:
            raise UnsafeSnapshotError("post-apply status fingerprint mismatch")
        journal["state"] = "completed"
        _write_json_atomic(journal_path, journal)
    except Exception as error:
        try:
            restore_set = [incoming]
            if prior is not None:
                restore_set.append(prior)
            _restore_head_paths(repository, restore_set)
            _apply_snapshot_low_level(repository, backup)
            if status_fingerprint(
                repository, **_snapshot_policy_kwargs(backup)
            ) != backup.status_fingerprint:
                raise UnsafeSnapshotError("rollback fingerprint mismatch")
            journal["state"] = "rolled_back"
            journal["error"] = str(error)[:1000]
            _write_json_atomic(journal_path, journal)
        except Exception as rollback_error:
            journal["state"] = "rollback_failed"
            journal["error"] = str(error)[:1000]
            journal["rollback_error"] = str(rollback_error)[:1000]
            _write_json_atomic(journal_path, journal)
            raise UnsafeSnapshotError(
                "snapshot apply failed and rollback needs recovery via %s: %s"
                % (journal_path, rollback_error)
            ) from error
        raise
    return {
        "ok": True,
        "already_applied": False,
        "snapshot_id": incoming.snapshot_id,
        "backup": backup_path,
        "journal": journal_path,
    }


apply_snapshot = apply_wip_snapshot


def recover_wip_journal(
    repo: Union[GitRepository, str, os.PathLike],
    journal_path: Union[str, os.PathLike],
) -> Mapping[str, object]:
    """Restore the pre-apply backup recorded by a completed/failed journal."""

    repository = _coerce_repo(repo)
    require_mutation_safe(repository)
    path = os.path.abspath(os.fspath(journal_path))
    with open(path, "r", encoding="utf-8") as handle:
        journal = json.load(handle)
    if journal.get("format") != "chatmesh-apply-journal-v1":
        raise SnapshotError("unsupported recovery journal")
    root = _journal_root(repository)
    backup_relative = journal.get("backup")
    if not isinstance(backup_relative, str):
        raise SnapshotError("journal backup path is invalid")
    backup_path = os.path.abspath(os.path.join(root, backup_relative))
    if os.path.commonpath([root, backup_path]) != os.path.abspath(root):
        raise SnapshotError("journal backup escapes repository state")
    backup = read_wip_snapshot(backup_path)
    backup_policy = _snapshot_policy_kwargs(backup)
    current = status_fingerprint(repository, **backup_policy)
    if current == backup.status_fingerprint:
        return {"ok": True, "already_recovered": True, "journal": path}
    if current != journal.get("incoming_status_fingerprint"):
        raise UnsafeSnapshotError("current WIP does not match journal or backup")
    manifest = journal.get("incoming_manifest")
    if not isinstance(manifest, dict):
        raise SnapshotError("journal lacks incoming manifest")
    placeholder = WipSnapshot(manifest, b"", {}, None)
    _restore_head_paths(repository, [placeholder, backup])
    _apply_snapshot_low_level(repository, backup)
    if status_fingerprint(repository, **backup_policy) != backup.status_fingerprint:
        raise UnsafeSnapshotError("recovery fingerprint mismatch")
    journal["state"] = "recovered"
    _write_json_atomic(path, journal)
    return {"ok": True, "already_recovered": False, "journal": path}


recover_snapshot = recover_wip_journal


def _resolve_commit(repo: GitRepository, revision: str) -> Optional[str]:
    if not revision or revision.startswith("-") or "\x00" in revision:
        return None
    result = _git(
        repo,
        ["rev-parse", "--verify", "--end-of-options", "%s^{commit}" % revision],
        check=False,
    )
    return _display(result.stdout) if result.returncode == 0 else None


def classify_branch_ancestry(
    repo: Union[GitRepository, str, os.PathLike],
    current: str,
    incoming: str,
) -> Ancestry:
    repository = _coerce_repo(repo)
    current_oid = _resolve_commit(repository, current)
    incoming_oid = _resolve_commit(repository, incoming)
    if not current_oid or not incoming_oid:
        return Ancestry.MISSING
    if current_oid == incoming_oid:
        return Ancestry.EQUAL
    current_ancestor = _git(
        repository,
        ["merge-base", "--is-ancestor", current_oid, incoming_oid],
        check=False,
    ).returncode
    if current_ancestor == 0:
        return Ancestry.FAST_FORWARD
    incoming_ancestor = _git(
        repository,
        ["merge-base", "--is-ancestor", incoming_oid, current_oid],
        check=False,
    ).returncode
    if incoming_ancestor == 0:
        return Ancestry.AHEAD
    merge_base = _git(
        repository, ["merge-base", current_oid, incoming_oid], check=False
    )
    return Ancestry.DIVERGED if merge_base.returncode == 0 else Ancestry.UNRELATED


classify_ancestry = classify_branch_ancestry


def _sanitize_ref_piece(value: str) -> str:
    sanitized = _SAFE_REF_COMPONENT.sub("-", value.strip()).strip(".-")
    sanitized = re.sub(r"-+", "-", sanitized)
    if not sanitized or sanitized in ("@",):
        raise GitRepoError("value cannot form a safe ref component")
    return sanitized[:100]


def _branch_ref(branch: str) -> Tuple[str, str]:
    short = branch[len("refs/heads/") :] if branch.startswith("refs/heads/") else branch
    if (
        subprocess.run(
            ["git", "check-ref-format", "--branch", short],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        != 0
    ):
        raise GitRepoError("invalid branch name: %r" % branch)
    return short, "refs/heads/%s" % short


def incoming_ref_name(peer: str, branch: str) -> str:
    short, _ = _branch_ref(branch)
    pieces = [_sanitize_ref_piece(piece) for piece in short.split("/")]
    return "refs/chatmesh/incoming/%s/%s" % (
        _sanitize_ref_piece(peer),
        "/".join(pieces),
    )


def backup_ref_name(
    peer: str, branch: str, oid: str, timestamp: Optional[int] = None
) -> str:
    short, _ = _branch_ref(branch)
    pieces = [_sanitize_ref_piece(piece) for piece in short.split("/")]
    stamp = time.strftime(
        "%Y%m%dT%H%M%SZ", time.gmtime(time.time() if timestamp is None else timestamp)
    )
    return "refs/chatmesh/backups/%s/%s/%s-%s" % (
        _sanitize_ref_piece(peer),
        "/".join(pieces),
        stamp,
        _sanitize_ref_piece(oid[:12]),
    )


def write_incoming_ref(
    repo: Union[GitRepository, str, os.PathLike],
    peer: str,
    branch: str,
    target: str,
    *,
    expected_old: Optional[str] = None,
) -> str:
    repository = _coerce_repo(repo)
    target_oid = _resolve_commit(repository, target)
    if target_oid is None:
        raise GitRepoError("incoming target is not a local commit")
    ref = incoming_ref_name(peer, branch)
    argv = ["update-ref", ref, target_oid]
    if expected_old is not None:
        argv.append(expected_old)
    _git(repository, argv)
    return ref


def _raw_worktree_clean(path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", path, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ, LC_ALL="C", GIT_OPTIONAL_LOCKS="0"),
    )
    if result.returncode != 0:
        raise GitRepoError("could not inspect checked-out worktree")
    return not result.stdout


def fast_forward_branch(
    repo: Union[GitRepository, str, os.PathLike],
    branch: str,
    target: str,
    *,
    expected_old: Optional[str] = None,
    peer: str = "local",
    create_backup: bool = True,
) -> FastForwardResult:
    """Fast-forward a branch with compare-and-swap and checkout awareness."""

    repository = _coerce_repo(repo)
    blocker = mutation_blocker(repository)
    if blocker:
        raise FastForwardError("repository has active %s state" % blocker)
    short, ref = _branch_ref(branch)
    old_oid = _resolve_commit(repository, ref)
    new_oid = _resolve_commit(repository, target)
    if old_oid is None or new_oid is None:
        raise FastForwardError("branch and target must both resolve to commits")
    if expected_old is not None and old_oid != expected_old:
        raise FastForwardError("branch moved since it was inspected")
    ancestry = classify_branch_ancestry(repository, old_oid, new_oid)
    if ancestry not in (Ancestry.EQUAL, Ancestry.FAST_FORWARD):
        raise FastForwardError("refusing %s branch update" % ancestry.value)
    checked_out = [
        item.path for item in list_worktrees(repository) if item.branch == short
    ]
    if len(checked_out) > 1:
        raise FastForwardError("branch is unexpectedly checked out more than once")
    backup_ref = None
    if old_oid == new_oid:
        return FastForwardResult(short, old_oid, new_oid, "noop", backup_ref, checked_out[0] if checked_out else None)
    if checked_out:
        worktree = checked_out[0]
        if not _raw_worktree_clean(worktree):
            raise FastForwardError("checked-out branch worktree is dirty")
        # Recheck the ref after cleanliness inspection; merge --ff-only supplies
        # the checkout/index update that update-ref alone cannot safely perform.
        if _resolve_commit(repository, ref) != old_oid:
            raise FastForwardError("branch moved before fast-forward")
    if create_backup:
        backup_ref = backup_ref_name(peer, short, old_oid)
        _git(repository, ["update-ref", backup_ref, old_oid, "0" * 40])
    if checked_out:
        worktree = checked_out[0]
        if _resolve_commit(repository, ref) != old_oid:
            raise FastForwardError("branch moved while recording its backup")
        proc = subprocess.run(
            ["git", "-C", worktree, "merge", "--ff-only", "--no-edit", new_oid],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(os.environ, LC_ALL="C", GIT_MERGE_AUTOEDIT="no"),
        )
        if proc.returncode != 0:
            raise FastForwardError("ff-only merge failed: %s" % _display(proc.stderr))
        method = "merge-ff-only"
    else:
        _git(repository, ["update-ref", ref, new_oid, old_oid])
        worktree = None
        method = "update-ref"
    if _resolve_commit(repository, ref) != new_oid:
        raise FastForwardError("branch did not reach the intended commit")
    return FastForwardResult(short, old_oid, new_oid, method, backup_ref, worktree)


def _resolution_slug(repo: GitRepository, branch: str, incoming_oid: str) -> str:
    repo_name = os.path.basename(repo.real_path.rstrip(os.sep))
    branch_piece = branch.replace("/", "-")
    raw = "%s-%s-%s" % (repo_name, branch_piece, incoming_oid[:8])
    return _sanitize_ref_piece(raw).lower()[:72].rstrip("-")


def prepare_divergence_worktree(
    repo: Union[GitRepository, str, os.PathLike],
    branch: str,
    incoming: str,
    *,
    peer: str = "incoming",
    base_dir: Optional[Union[str, os.PathLike]] = None,
) -> DivergenceWorktree:
    """Create an isolated resolution branch/worktree without switching callers."""

    repository = _coerce_repo(repo)
    short, ref = _branch_ref(branch)
    local_oid = _resolve_commit(repository, ref)
    incoming_oid = _resolve_commit(repository, incoming)
    if local_oid is None or incoming_oid is None:
        raise DivergenceError("local and incoming commits must exist")
    ancestry = classify_branch_ancestry(repository, local_oid, incoming_oid)
    if ancestry not in (Ancestry.DIVERGED, Ancestry.UNRELATED):
        raise DivergenceError("resolution worktree requires divergent histories")
    incoming_ref = write_incoming_ref(repository, peer, short, incoming_oid)
    slug = _resolution_slug(repository, short, incoming_oid)
    base_resolution_branch = "mhadi/chore/chatmesh-resolve-%s" % slug
    if base_dir is None:
        worktrees = list_worktrees(repository)
        primary = next((item.path for item in worktrees if not item.bare), repository.real_path)
        parent = os.path.join(primary, ".worktrees")
    else:
        parent = os.path.abspath(os.fspath(base_dir))
    os.makedirs(parent, exist_ok=True)
    path = os.path.join(parent, "chore-chatmesh-resolve-%s" % slug)
    if os.path.lexists(path):
        existing = next(
            (
                item
                for item in list_worktrees(repository)
                if os.path.realpath(item.path) == os.path.realpath(path)
            ),
            None,
        )
        if existing is not None and existing.branch == base_resolution_branch:
            return DivergenceWorktree(
                path=os.path.realpath(path),
                branch=base_resolution_branch,
                local_oid=local_oid,
                incoming_oid=incoming_oid,
                incoming_ref=incoming_ref,
            )
        raise DivergenceError("resolution worktree path already exists: %s" % path)
    resolution_branch = base_resolution_branch
    if _resolve_commit(repository, "refs/heads/%s" % resolution_branch) is not None:
        resolution_branch += "-%s" % uuid.uuid4().hex[:6]
    proc = subprocess.run(
        [
            "git",
            "-C",
            repository.real_path,
            "worktree",
            "add",
            "-b",
            resolution_branch,
            path,
            local_oid,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ, LC_ALL="C"),
    )
    if proc.returncode != 0:
        raise DivergenceError("worktree creation failed: %s" % _display(proc.stderr))
    path = os.path.realpath(path)
    merge_args = [
        "git", "-C", path, "merge", "--no-commit", "--no-ff", "--no-edit",
    ]
    if ancestry == Ancestry.UNRELATED:
        merge_args.append("--allow-unrelated-histories")
    merge_args.append(incoming_ref)
    merge = subprocess.run(
        merge_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(
            os.environ,
            LC_ALL="C",
            GIT_MERGE_AUTOEDIT="no",
            GIT_AUTHOR_NAME=os.environ.get("GIT_AUTHOR_NAME", "Chatmesh"),
            GIT_AUTHOR_EMAIL=os.environ.get(
                "GIT_AUTHOR_EMAIL", "chatmesh@localhost"
            ),
            GIT_COMMITTER_NAME=os.environ.get("GIT_COMMITTER_NAME", "Chatmesh"),
            GIT_COMMITTER_EMAIL=os.environ.get(
                "GIT_COMMITTER_EMAIL", "chatmesh@localhost"
            ),
        ),
    )
    # Exit 1 is the expected, reviewable conflict state. Originals are still
    # untouched because the merge runs only in this dedicated worktree.
    if merge.returncode not in (0, 1):
        raise DivergenceError(
            "isolated merge preparation failed: %s" % _display(merge.stderr)
        )
    return DivergenceWorktree(
        path=path,
        branch=resolution_branch,
        local_oid=local_oid,
        incoming_oid=incoming_oid,
        incoming_ref=incoming_ref,
    )


create_divergence_worktree = prepare_divergence_worktree


def accept_resolution(
    repo: Union[GitRepository, str, os.PathLike],
    target_branch: str,
    resolution_branch: str,
    *,
    expected_old: Optional[str] = None,
    peer: str = "resolution",
) -> FastForwardResult:
    """Accept a resolved branch only when the target can fast-forward to it."""
    repository = _coerce_repo(repo)
    target_short, target_ref = _branch_ref(target_branch)
    resolution_short, resolution_ref = _branch_ref(resolution_branch)
    target_oid = _resolve_commit(repository, target_ref)
    resolution_oid = _resolve_commit(repository, resolution_ref)
    if target_oid is None or resolution_oid is None:
        raise DivergenceError("target and resolution branches must exist")
    resolution_worktrees = [
        item.path for item in list_worktrees(repository)
        if item.branch == resolution_short
    ]
    if not resolution_worktrees:
        raise DivergenceError("resolution branch has no review worktree")
    for path in resolution_worktrees:
        if not _raw_worktree_clean(path):
            raise DivergenceError("resolution worktree is not clean and committed")
        resolution_repo = open_repository(path)
        blocker = mutation_blocker(resolution_repo)
        if blocker:
            raise DivergenceError(
                "resolution worktree still has active %s state" % blocker
            )
    incoming_refs = _git(
        repository,
        ["for-each-ref", "--format=%(objectname)", "refs/chatmesh/incoming"],
    ).stdout.splitlines()
    includes_incoming = False
    for raw_oid in incoming_refs:
        incoming_oid = _decode(raw_oid).strip()
        if not incoming_oid or incoming_oid == target_oid:
            continue
        incoming_in_resolution = _git(
            repository,
            ["merge-base", "--is-ancestor", incoming_oid, resolution_oid],
            check=False,
        ).returncode == 0
        incoming_already_in_target = _git(
            repository,
            ["merge-base", "--is-ancestor", incoming_oid, target_oid],
            check=False,
        ).returncode == 0
        if incoming_in_resolution and not incoming_already_in_target:
            includes_incoming = True
            break
    if not includes_incoming:
        raise DivergenceError(
            "resolution does not contain an imported incoming history"
        )
    return fast_forward_branch(
        repository,
        target_short,
        resolution_short,
        expected_old=expected_old,
        peer=peer,
        create_backup=True,
    )


__all__ = [
    "Ancestry",
    "BranchMetadata",
    "DivergenceError",
    "DivergenceWorktree",
    "FastForwardError",
    "FastForwardResult",
    "GitHubRepository",
    "GitRepoError",
    "GitRepository",
    "RepositoryMetadata",
    "SnapshotError",
    "TagMetadata",
    "UnsafeSnapshotError",
    "WipSnapshot",
    "WorktreeMetadata",
    "accept_resolution",
    "apply_snapshot",
    "apply_wip_snapshot",
    "backup_ref_name",
    "classify_ancestry",
    "classify_branch_ancestry",
    "collect_metadata",
    "create_divergence_worktree",
    "create_wip_snapshot",
    "derive_stable_identity",
    "discover",
    "discover_repositories",
    "fast_forward_branch",
    "incoming_ref_name",
    "list_worktrees",
    "normalize_github_origin",
    "normalize_github_remote",
    "open_repository",
    "origin_url",
    "parse_github_origin",
    "prepare_divergence_worktree",
    "read_snapshot",
    "read_wip_snapshot",
    "recover_snapshot",
    "recover_wip_journal",
    "repo_identity",
    "repository_metadata",
    "resolve_github_repository",
    "snapshot_wip",
    "status_fingerprint",
    "write_incoming_ref",
]
