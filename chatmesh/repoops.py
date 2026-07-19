"""Repository inventory, canonical layout, relocation, and bootstrapping."""

from __future__ import annotations

import glob
import hashlib
import os
import re
import subprocess
from dataclasses import asdict
from typing import List, Mapping, Optional, Sequence

from . import gitrepos
from .config import GitProfile
from .gittransport import ssh_repo_url


class RepositoryLayoutError(RuntimeError):
    pass


def _run(repo_path: str, args: Sequence[str], check: bool = True):
    proc = subprocess.run(
        ["git", "-C", repo_path] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ, LC_ALL="C", GIT_OPTIONAL_LOCKS="0"),
    )
    if check and proc.returncode != 0:
        raise RepositoryLayoutError(
            proc.stderr.decode("utf-8", "replace").strip()[:1000]
        )
    return proc


def _logical_relative(path: str, roots: Sequence[str]) -> Optional[str]:
    normalized = os.path.abspath(path)
    matches = []
    for root in roots:
        root_abs = os.path.abspath(root)
        try:
            if os.path.commonpath([root_abs, normalized]) == root_abs:
                matches.append((len(root_abs), os.path.relpath(normalized, root_abs)))
        except ValueError:
            continue
    if not matches:
        return None
    return max(matches)[1].replace(os.sep, "/")


def _superproject(repo: gitrepos.GitRepository) -> Optional[str]:
    if repo.is_bare:
        return None
    proc = _run(repo.real_path, ["rev-parse", "--show-superproject-working-tree"],
                check=False)
    value = proc.stdout.decode("utf-8", "surrogateescape").strip()
    if not value:
        return None
    try:
        return gitrepos.derive_stable_identity(gitrepos.open_repository(value))
    except gitrepos.GitRepoError:
        return None


def _quick_wip_id(
    repo: gitrepos.GitRepository,
    *,
    include_staged: bool = True,
    include_unstaged: bool = True,
    include_untracked: bool = True,
    include_ignored: bool = False,
) -> str:
    digest = hashlib.sha256(b"chatmesh-wip-plan-v2\x00")
    if include_staged:
        digest.update(b"staged\x00")
        digest.update(_run(
            repo.real_path,
            ["diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--"],
        ).stdout)
    if include_unstaged:
        digest.update(b"unstaged\x00")
        digest.update(_run(
            repo.real_path,
            ["diff", "--binary", "--full-index", "--no-ext-diff", "--"],
        ).stdout)

    selected = []
    if include_untracked:
        selected.extend(
            (b"untracked", item)
            for item in _run(
                repo.real_path,
                ["ls-files", "--others", "--exclude-standard", "-z", "--"],
            ).stdout.split(b"\x00")
            if item
        )
    if include_ignored:
        selected.extend(
            (b"ignored", item)
            for item in _run(
                repo.real_path,
                ["ls-files", "--others", "--ignored", "--exclude-standard",
                 "-z", "--"],
            ).stdout.split(b"\x00")
            if item
        )
    for category, raw in sorted(selected):
        rel = raw.decode("utf-8", "surrogateescape")
        path = os.path.join(repo.real_path, *rel.split("/"))
        digest.update(category + b"\x00" + raw + b"\x00")
        try:
            info = os.lstat(path)
            digest.update(str(info.st_mode).encode("ascii") + b"\x00")
            if os.path.islink(path):
                digest.update(os.readlink(path).encode("utf-8", "surrogateescape"))
            elif os.path.isfile(path):
                with open(path, "rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
        except OSError:
            digest.update(b"<raced>")
    return digest.hexdigest()


def repository_record(
    repo: gitrepos.GitRepository,
    roots: Sequence[str],
    *,
    github_cache: Optional[str] = None,
    resolve_github: bool = False,
    include_ignored: bool = False,
    profile: Optional[GitProfile] = None,
) -> dict:
    metadata = gitrepos.repository_metadata(
        repo, include_status_fingerprint=False
    )
    branches = {
        item.name: item.oid for item in metadata.branches
    }
    tags = {item.name: item.oid for item in metadata.tags}
    worktrees = [asdict(item) for item in metadata.worktrees]
    resolved = (
        gitrepos.resolve_github_repository(
            metadata.origin or "", github_cache, allow_gh=resolve_github
        )
        if metadata.origin
        else None
    )
    identity = resolved.identity if resolved else metadata.identity
    selected_profile = (
        profile.for_repository(identity)
        if profile is not None and hasattr(profile, "for_repository")
        else profile
    )
    include_staged = bool(getattr(selected_profile, "staged", True))
    include_unstaged = bool(getattr(selected_profile, "unstaged", True))
    include_untracked = bool(getattr(selected_profile, "untracked", True))
    include_ignored = bool(
        getattr(selected_profile, "ignored", include_ignored)
    )
    # Use porcelain for the boolean because the exact fingerprint encoding is
    # deliberately private to gitrepos.
    dirty = False
    status_hash = None
    if not repo.is_bare:
        status_args = [
            "status", "--porcelain=v2", "-z", "--untracked-files=all",
        ]
        if include_ignored:
            status_args.append("--ignored=matching")
        status_bytes = _run(
            repo.real_path,
            status_args,
        ).stdout
        dirty = bool(status_bytes)
        status_hash = _quick_wip_id(
            repo,
            include_staged=include_staged,
            include_unstaged=include_unstaged,
            include_untracked=include_untracked,
            include_ignored=include_ignored,
        )
    return {
        "identity": identity,
        "repository_id": resolved.repository_id if resolved else None,
        "origin_identity": metadata.identity,
        "origin": resolved.canonical if resolved else metadata.origin,
        "logical_path": repo.logical_path,
        "real_path": repo.real_path,
        "relative_path": _logical_relative(repo.logical_path, roots),
        "git_dir": repo.git_dir,
        "common_dir": repo.common_dir,
        "kind": repo.kind,
        "bare": repo.is_bare,
        "head": metadata.head,
        "branch": metadata.branch,
        "detached": metadata.detached,
        "status_fingerprint": status_hash,
        "wip_id": status_hash,
        "dirty": dirty,
        "branches": branches,
        "branch_metadata": [asdict(item) for item in metadata.branches],
        "tags": tags,
        "tag_metadata": [asdict(item) for item in metadata.tags],
        "worktrees": worktrees,
        "superproject_identity": _superproject(repo),
    }


def inventory(
    profile: GitProfile,
    *,
    github_cache: Optional[str] = None,
    resolve_github: bool = False,
    errors: Optional[List[dict]] = None,
) -> List[dict]:
    repositories = gitrepos.discover_repositories(profile.roots)
    nested_parents = {}
    for repo in repositories:
        candidates = [
            parent
            for parent in repositories
            if parent is not repo
            and os.path.commonpath([
                os.path.abspath(parent.logical_path),
                os.path.abspath(repo.logical_path),
            ]) == os.path.abspath(parent.logical_path)
        ]
        if candidates:
            nested_parents[repo.logical_path] = max(
                candidates, key=lambda item: len(item.logical_path)
            )
    records = []
    for repo in repositories:
        try:
            record = repository_record(
                repo,
                profile.roots,
                github_cache=github_cache,
                resolve_github=resolve_github,
                include_ignored=bool(getattr(profile, "ignored", False)),
                profile=profile,
            )
            parent = nested_parents.get(repo.logical_path)
            record["nested"] = parent is not None
            record["parent_logical_path"] = (
                parent.logical_path if parent is not None else None
            )
            records.append(record)
        except (gitrepos.GitRepoError, RepositoryLayoutError, OSError) as exc:
            if errors is not None:
                errors.append({
                    "logical_path": repo.logical_path,
                    "real_path": repo.real_path,
                    "kind": repo.kind,
                    "error": str(exc),
                })
    identities = {
        record["logical_path"]: record["identity"]
        for record in records
    }
    for record in records:
        record["parent_identity"] = identities.get(
            record["parent_logical_path"]
        )
    return records


def canonical_main_path(record: Mapping[str, object], roots: Sequence[str]) -> Optional[str]:
    origin = str(record.get("origin") or "")
    if origin.lower().startswith("github.com/"):
        origin = "https://" + origin
    github = gitrepos.parse_github_origin(origin)
    if github is None or not roots:
        return None
    relative = str(record.get("relative_path") or "").replace(os.sep, "/")
    if relative == "external" or relative.startswith("external/"):
        return os.path.join(os.path.abspath(roots[0]), "external", github.name)
    return os.path.join(os.path.abspath(roots[0]), github.owner, github.name)


def _branch_worktree_slug(branch: str) -> str:
    pieces = branch.split("/")
    if len(pieces) >= 3 and pieces[0] == "mhadi":
        kind = pieces[1]
        description = "-".join(pieces[2:])
        raw = "%s-%s" % (kind, description)
    else:
        raw = "branch-" + branch.replace("/", "-")
    value = re.sub(r"[^a-z0-9-]+", "-", raw.lower())
    return re.sub(r"-+", "-", value).strip("-")[:100]


def canonical_worktree_path(main_path: str, branch: str) -> str:
    return os.path.join(main_path, ".worktrees", _branch_worktree_slug(branch))


def relocation_target(record: Mapping[str, object], roots: Sequence[str]) -> Optional[str]:
    if record.get("kind") == "submodule":
        return None
    if record.get("nested") and record.get("kind") != "linked-worktree":
        return None
    main = canonical_main_path(record, roots)
    if not main:
        return None
    if record.get("kind") == "linked-worktree":
        branch = str(record.get("branch") or "")
        if not branch:
            return None
        return canonical_worktree_path(main, branch)
    return main


def update_canonical_origin(repo_path: str, canonical_url: str,
                            dry_run: bool = False) -> bool:
    if canonical_url.lower().startswith("github.com/"):
        canonical_url = "https://" + canonical_url
    github = gitrepos.parse_github_origin(canonical_url)
    if github is None:
        return False
    current = _run(repo_path, ["remote", "get-url", "origin"], check=False)
    current_value = current.stdout.decode("utf-8", "replace").strip()
    if gitrepos.normalize_github_origin(current_value) == github.canonical:
        return False
    if not dry_run:
        _run(repo_path, ["remote", "set-url", "origin", github.https_url])
    return True


def _rewrite_text_file(path: str, old: str, new: str) -> bool:
    try:
        with open(path, "rb") as stream:
            data = stream.read()
    except OSError:
        return False
    old_bytes = old.encode("utf-8")
    if old_bytes not in data or b"\x00" in data:
        return False
    updated = data.replace(old_bytes, new.encode("utf-8"))
    temporary = path + ".chatmesh-tmp"
    with open(temporary, "wb") as stream:
        stream.write(updated)
    os.chmod(temporary, os.stat(path).st_mode & 0o777)
    os.replace(temporary, path)
    return True


def repair_absolute_paths(new_path: str, old_path: str,
                          home: Optional[str] = None) -> int:
    changed = 0
    candidates = []
    for env_name in (".venv", "venv", "env"):
        env = os.path.join(new_path, env_name)
        candidates.extend(glob.glob(os.path.join(env, "pyvenv.cfg")))
        candidates.extend(glob.glob(os.path.join(env, "bin", "activate*")))
        candidates.extend(glob.glob(os.path.join(env, "bin", "*")))
    # Package-manager checkouts can contain Git alternates and local remotes
    # that become invalid when their parent repository moves.  These are text
    # references only; object databases and Git metadata are never copied.
    candidates.extend(glob.glob(
        os.path.join(new_path, "**", ".git", "config"), recursive=True
    ))
    candidates.extend(glob.glob(
        os.path.join(
            new_path, "**", ".git", "objects", "info", "alternates"
        ),
        recursive=True,
    ))
    candidates.extend(glob.glob(
        os.path.join(new_path, ".build", "**", ".git", "config"),
        recursive=True,
    ))
    candidates.extend(glob.glob(
        os.path.join(
            new_path, ".build", "**", ".git", "objects", "info", "alternates"
        ),
        recursive=True,
    ))
    candidates.extend(glob.glob(
        os.path.join(new_path, ".git", "**", "config"), recursive=True
    ))
    candidates.extend(glob.glob(
        os.path.join(
            new_path, ".git", "**", "objects", "info", "alternates"
        ),
        recursive=True,
    ))
    for candidate in sorted(set(candidates)):
        if os.path.isfile(candidate) and _rewrite_text_file(candidate, old_path, new_path):
            changed += 1

    user_home = home or os.path.expanduser("~")
    workspace_glob = os.path.join(
        user_home, "Library", "Application Support", "Cursor", "User",
        "workspaceStorage", "*", "workspace.json",
    )
    for candidate in glob.glob(workspace_glob):
        if _rewrite_text_file(candidate, old_path, new_path):
            changed += 1
    return changed


def _owner_storage_root(root: str) -> Optional[str]:
    """Infer a shared physical owner root from logical owner symlinks."""
    candidates = set()
    try:
        entries = list(os.scandir(root))
    except OSError:
        return None
    for entry in entries:
        try:
            if not entry.is_symlink() or not entry.is_dir(follow_symlinks=True):
                continue
            resolved = os.path.realpath(entry.path)
            if os.path.basename(resolved) == entry.name:
                candidates.add(os.path.dirname(resolved))
        except OSError:
            continue
    return next(iter(candidates)) if len(candidates) == 1 else None


def _prepare_canonical_parent(target: str, dry_run: bool = False) -> str:
    """Preserve an owner-symlink namespace when creating a new owner."""
    owner_path = os.path.dirname(target)
    root = os.path.dirname(owner_path)
    if not os.path.isdir(root):
        raise RepositoryLayoutError("repository root does not exist: %s" % root)
    storage_root = _owner_storage_root(root)

    if os.path.isdir(owner_path) and not os.path.islink(owner_path):
        if storage_root:
            try:
                empty = not os.listdir(owner_path)
            except OSError:
                empty = False
            if empty:
                if dry_run:
                    return owner_path
                os.rmdir(owner_path)
            else:
                return owner_path
        else:
            return owner_path
    elif os.path.lexists(owner_path):
        if not os.path.isdir(owner_path):
            raise RepositoryLayoutError(
                "repository owner path is not a directory: %s" % owner_path
            )
        return owner_path

    if dry_run:
        return owner_path
    if storage_root:
        physical_owner = os.path.join(storage_root, os.path.basename(owner_path))
        os.makedirs(physical_owner, exist_ok=True)
        os.symlink(physical_owner, owner_path, target_is_directory=True)
    else:
        os.makedirs(owner_path, exist_ok=True)
    return owner_path


def _nearest_existing_directory(path: str) -> str:
    candidate = os.path.abspath(path)
    while not os.path.isdir(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            raise RepositoryLayoutError(
                "no existing destination ancestor for %s" % path
            )
        candidate = parent
    return os.path.realpath(candidate)


def _relocation_destination_anchor(target: str, linked_worktree: bool) -> str:
    """Return an existing directory on the eventual destination filesystem."""
    parent = os.path.dirname(target)
    if linked_worktree:
        return _nearest_existing_directory(parent)
    owner_path = parent
    root = os.path.dirname(owner_path)
    if not os.path.isdir(root):
        raise RepositoryLayoutError(
            "repository root does not exist: %s" % root
        )
    if os.path.isdir(owner_path):
        return os.path.realpath(owner_path)
    storage_root = _owner_storage_root(root)
    return os.path.realpath(storage_root or root)


def _device(path: str) -> int:
    return os.stat(path).st_dev


def relocate_repository(repo: gitrepos.GitRepository, target: str,
                        dry_run: bool = False) -> dict:
    source = repo.logical_path
    target = os.path.abspath(target)
    if os.path.normpath(source) == os.path.normpath(target):
        return {"ok": True, "moved": False, "source": source, "target": target}
    gitrepos.require_mutation_safe(repo)
    same_location = False
    if os.path.lexists(target):
        try:
            same_location = os.path.samefile(source, target)
        except OSError:
            same_location = False
    if os.path.lexists(target) and not same_location:
        raise RepositoryLayoutError("canonical destination already exists: %s" % target)
    try:
        if os.path.commonpath([os.path.abspath(source), target]) == os.path.abspath(source):
            raise RepositoryLayoutError(
                "canonical destination is inside the current checkout"
            )
    except ValueError:
        pass
    parent = os.path.dirname(target)
    destination_anchor = _relocation_destination_anchor(
        target, repo.kind == "linked-worktree"
    )
    if _device(source) != _device(destination_anchor):
        raise RepositoryLayoutError(
            "cross-device repository relocation requires explicit repo-reorg"
        )
    if dry_run:
        return {"ok": True, "moved": True, "dry_run": True,
                "source": source, "target": target}
    if repo.kind != "linked-worktree":
        _prepare_canonical_parent(target)
    os.makedirs(parent, exist_ok=True)

    if repo.kind == "linked-worktree":
        main = gitrepos.list_worktrees(repo)[0].path
        _run(main, ["worktree", "move", source, target])
        _run(main, ["worktree", "repair"])
    elif repo.kind in ("submodule", "bare"):
        raise RepositoryLayoutError("automatic relocation is disabled for %s" % repo.kind)
    else:
        os.rename(source, target)
        _run(target, ["worktree", "repair"])
        repair_absolute_paths(target, source)
    return {"ok": True, "moved": True, "source": source, "target": target}


def clone_from_peer(peer: str, remote_path: str, target: str,
                    branch: Optional[str] = None, dry_run: bool = False) -> dict:
    if os.path.lexists(target):
        raise RepositoryLayoutError("clone destination exists: %s" % target)
    url = ssh_repo_url(peer, remote_path)
    if dry_run:
        return {"ok": True, "cloned": True, "dry_run": True,
                "source": url, "target": target}
    _prepare_canonical_parent(target)
    args = ["git", "clone"]
    if branch:
        args.extend(["--branch", branch])
    args.extend(["--", url, target])
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    if proc.returncode != 0:
        raise RepositoryLayoutError(
            proc.stderr.decode("utf-8", "replace").strip()[:1000]
        )
    return {"ok": True, "cloned": True, "source": url, "target": target}


def initialize_checkout(target: str, origin: str, dry_run: bool = False) -> dict:
    if origin.lower().startswith("github.com/"):
        origin = "https://" + origin
    github = gitrepos.parse_github_origin(origin)
    if github is None:
        raise RepositoryLayoutError("missing checkout requires a GitHub origin")
    if os.path.lexists(target):
        try:
            existing = gitrepos.open_repository(target)
        except gitrepos.GitRepoError:
            existing = None
        reusable = existing is not None and not existing.is_bare
        if reusable:
            current_origin = _run(
                existing.real_path, ["remote", "get-url", "origin"],
                check=False,
            )
            current_url = current_origin.stdout.decode(
                "utf-8", "replace"
            ).strip()
            reusable = (
                gitrepos.normalize_github_origin(current_url)
                == github.canonical
                and _run(
                    existing.real_path,
                    ["rev-parse", "--verify", "HEAD"],
                    check=False,
                ).returncode != 0
                and not _run(
                    existing.real_path, ["for-each-ref", "--format=%(refname)"]
                ).stdout
                and not _run(
                    existing.real_path,
                    ["status", "--porcelain", "-z", "--untracked-files=all"],
                ).stdout
            )
        if not reusable:
            raise RepositoryLayoutError(
                "checkout destination exists: %s" % target
            )
        if not dry_run:
            _run(
                existing.real_path,
                ["symbolic-ref", "HEAD", "refs/heads/chatmesh-bootstrap"],
            )
        return {
            "ok": True,
            "initialized": False,
            "reused": True,
            "dry_run": dry_run,
            "target": target,
        }
    if dry_run:
        return {"ok": True, "initialized": True, "dry_run": True, "target": target}
    _prepare_canonical_parent(target)
    os.makedirs(target)
    _run(target, ["init"])
    _run(target, ["remote", "add", "origin", github.https_url])
    _run(target, ["symbolic-ref", "HEAD", "refs/heads/chatmesh-bootstrap"])
    return {"ok": True, "initialized": True, "target": target}


def checkout_initialized_branch(repo_path: str, branch: str) -> dict:
    repo = gitrepos.open_repository(repo_path)
    if repo.is_bare:
        raise RepositoryLayoutError("cannot checkout a branch in a bare repository")
    status = _run(
        repo.real_path, ["status", "--porcelain=v2", "-z", "--untracked-files=all"]
    ).stdout
    current = _run(
        repo.real_path, ["symbolic-ref", "--short", "HEAD"], check=False
    ).stdout.decode("utf-8", "replace").strip()
    if status and current == branch:
        records = [item for item in status.split(b"\x00") if item]
        deletion_only = bool(records) and all(
            item.startswith(b"1 D. ") for item in records
        )
        tree_paths = _run(
            repo.real_path, ["ls-tree", "-r", "--name-only", "-z", "HEAD"]
        ).stdout.split(b"\x00")
        root = os.path.abspath(repo.real_path)
        destinations = []
        for raw in (item for item in tree_paths if item):
            relative = raw.decode("utf-8", "surrogateescape")
            destination = os.path.abspath(os.path.join(
                root, *relative.split("/")
            ))
            if (
                os.path.commonpath([root, destination]) != root
                or os.path.lexists(destination)
            ):
                deletion_only = False
                break
            destinations.append(destination)
        if deletion_only:
            _run(repo.real_path, ["read-tree", "HEAD"])
            _run(repo.real_path, ["checkout-index", "--all"])
            if _run(
                repo.real_path,
                ["status", "--porcelain=v2", "-z", "--untracked-files=all"],
            ).stdout:
                raise RepositoryLayoutError(
                    "bootstrap worktree materialization did not converge"
                )
            return {
                "ok": True,
                "checked_out": branch,
                "repo": repo.logical_path,
                "materialized": len(destinations),
            }
    if status:
        raise RepositoryLayoutError("new checkout is unexpectedly dirty")
    proc = _run(repo.real_path, ["checkout", branch], check=False)
    if proc.returncode != 0:
        raise RepositoryLayoutError(
            proc.stderr.decode("utf-8", "replace").strip()
        )
    return {"ok": True, "checked_out": branch, "repo": repo.logical_path}


def ensure_branch_worktree(repo_path: str, branch: str,
                           dry_run: bool = False) -> dict:
    repo = gitrepos.open_repository(repo_path)
    existing = [
        item.path for item in gitrepos.list_worktrees(repo)
        if item.branch == branch
    ]
    if existing:
        return {"ok": True, "created": False, "path": existing[0]}
    main = gitrepos.list_worktrees(repo)[0].path
    target = canonical_worktree_path(main, branch)
    if os.path.lexists(target):
        raise RepositoryLayoutError("worktree path exists: %s" % target)
    if dry_run:
        return {"ok": True, "created": True, "dry_run": True, "path": target}
    os.makedirs(os.path.dirname(target), exist_ok=True)
    _run(main, ["worktree", "add", target, branch])
    return {"ok": True, "created": True, "path": target}


def converge_branch(repo_path: str, branch: str, incoming: str, peer: str,
                    create_resolution: bool = True) -> dict:
    repo = gitrepos.open_repository(repo_path)
    ref = "refs/heads/%s" % branch
    old_proc = _run(repo.real_path, ["rev-parse", "--verify", ref], check=False)
    old_oid = old_proc.stdout.decode().strip() if old_proc.returncode == 0 else None
    incoming_proc = _run(
        repo.real_path, ["rev-parse", "--verify", "%s^{commit}" % incoming],
        check=False,
    )
    if incoming_proc.returncode != 0:
        raise RepositoryLayoutError("incoming ref is not a local commit")
    incoming_oid = incoming_proc.stdout.decode().strip()
    if old_oid is None:
        _run(repo.real_path, ["update-ref", ref, incoming_oid, "0" * 40])
        return {
            "ok": True, "action": "created-branch", "branch": branch,
            "old_oid": None, "new_oid": incoming_oid,
        }
    ancestry = gitrepos.classify_branch_ancestry(repo, old_oid, incoming_oid)
    if ancestry == gitrepos.Ancestry.EQUAL:
        return {"ok": True, "action": "equal", "branch": branch,
                "old_oid": old_oid, "new_oid": incoming_oid}
    if ancestry == gitrepos.Ancestry.AHEAD:
        return {"ok": True, "action": "local-ahead", "branch": branch,
                "old_oid": old_oid, "new_oid": old_oid}
    if ancestry == gitrepos.Ancestry.FAST_FORWARD:
        result = gitrepos.fast_forward_branch(
            repo, branch, incoming_oid, expected_old=old_oid, peer=peer
        )
        return {"ok": True, "action": "fast-forward", **asdict(result)}
    if not create_resolution:
        return {
            "ok": False, "action": "diverged", "branch": branch,
            "old_oid": old_oid, "incoming_oid": incoming_oid,
        }
    resolution = gitrepos.prepare_divergence_worktree(
        repo, branch, incoming_oid, peer=peer
    )
    return {
        "ok": False, "action": "resolution-worktree",
        **asdict(resolution),
    }


def converge_tag(repo_path: str, tag: str, incoming: str) -> dict:
    repo = gitrepos.open_repository(repo_path)
    if subprocess.run(
        ["git", "check-ref-format", "refs/tags/%s" % tag],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode != 0:
        raise RepositoryLayoutError("invalid tag name")
    target = _run(repo.real_path, ["rev-parse", "--verify", incoming],
                  check=False)
    if target.returncode != 0:
        raise RepositoryLayoutError("incoming tag object is missing")
    incoming_oid = target.stdout.decode().strip()
    ref = "refs/tags/%s" % tag
    current = _run(repo.real_path, ["rev-parse", "--verify", ref], check=False)
    if current.returncode != 0:
        _run(repo.real_path, ["update-ref", ref, incoming_oid, "0" * 40])
        return {"ok": True, "action": "created-tag", "tag": tag,
                "new_oid": incoming_oid}
    current_oid = current.stdout.decode().strip()
    if current_oid == incoming_oid:
        return {"ok": True, "action": "equal", "tag": tag,
                "new_oid": current_oid}
    return {
        "ok": False,
        "action": "tag-conflict",
        "tag": tag,
        "current_oid": current_oid,
        "incoming_oid": incoming_oid,
    }
