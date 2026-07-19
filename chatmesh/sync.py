"""Orchestrator. Runs on the hub; every transfer is between the local machine
and one peer, in either direction, so a single always-on hub keeps an N-machine
mesh converged (chats propagate transitively through it).

Gating (the "don't sync while the app is open" rule):
  * cursor (IDE DB): the destination machine must not have Cursor running —
    hard requirement for DB integrity; checked here and again inside apply.
    The source may be open (sqlite WAL snapshot reads are safe).
  * cursor-cli: process gate on cursor-agent, plus per-file guard.
  * claude / codex: per-file guard only — any session file touched within the
    guard window (default 15 min) on either side is left alone. A process gate
    would permanently block machines where a CLI session is always open.
    Add them to mesh.process_gate_apps in config.toml for strict behavior.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Optional

from . import VERSION, cursordb, filetrees
from .config import Config, REMOTE_REPO, default_state_dir
from .rewrite import HomeRewriter
from .util import (app_running_local, app_running_remote, log, remote_chatmesh_cmd,
                   remote_chatmesh_args, remote_home, run, ssh_argv, ssh_out)

TREE_APP = {"claude-projects": "claude", "codex-sessions": "codex",
            "cursor-cli": "cursor-cli"}
HISTORY_APP = {"claude-history": "claude", "codex-history": "codex"}


def state_path(state_dir: str = "") -> str:
    return os.path.join(state_dir or default_state_dir(), "state.json")


def load_state(state_dir: str = "") -> dict:
    try:
        with open(state_path(state_dir)) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict, state_dir: str = "") -> None:
    path = state_path(state_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def mark(state: dict, peer: str, unit: str, direction: str, **detail) -> None:
    state.setdefault(peer, {}).setdefault(unit, {})[direction] = dict(
        ts=int(time.time()), **detail)


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def backup_dir(state_dir: str = "") -> str:
    d = os.path.join(state_dir or default_state_dir(), "backups",
                     time.strftime("%Y%m%d"))
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
# Peer deployment (chatmesh must exist on the peer for export/apply)
# --------------------------------------------------------------------------- #
def deployed_version(peer: str) -> str:
    try:
        return ssh_out(
            peer,
            'PYTHONPATH="$HOME/%s" python3 -c '
            '"import chatmesh,sys;sys.stdout.write(chatmesh.VERSION)" '
            '2>/dev/null || true' % REMOTE_REPO).strip()
    except RuntimeError:
        return ""


def ensure_deployed(peer: str, force: bool = False) -> None:
    rv = deployed_version(peer)
    if rv == VERSION and not force:
        return
    log.info("deploying chatmesh %s to %s (had: %s)", VERSION, peer, rv or "none")
    tar = subprocess.Popen(
        ["tar", "-c", "--exclude", ".git", "--exclude", "__pycache__",
         "--exclude", "*.pyc", "-C", repo_root(), "."],
        stdout=subprocess.PIPE)
    run(ssh_argv(peer, 'mkdir -p "$HOME/%s" && tar -x -C "$HOME/%s"'
                 % (REMOTE_REPO, REMOTE_REPO)), stdin=tar.stdout)
    tar.wait()
    if tar.returncode != 0:
        raise RuntimeError("local tar failed during deploy")


# --------------------------------------------------------------------------- #
# Cursor IDE DB
# --------------------------------------------------------------------------- #
def _pipe_json(proc: subprocess.Popen, payload) -> None:
    proc.stdin.write(json.dumps(payload).encode())
    proc.stdin.close()


def sync_cursor(cfg: Config, peer: str, rhome: str, state: dict,
                dry_run: bool) -> None:
    try:
        ridx_raw = ssh_out(peer, remote_chatmesh_cmd("export-cursor-index"),
                           timeout=300)
        ridx = json.loads(ridx_raw)
    except Exception as e:
        log.error("cursor[%s]: index fetch failed: %s", peer, e)
        return
    lidx = cursordb.read_index()
    local_running = app_running_local("cursor")
    remote_running = bool(ridx.get("cursorRunning"))
    pull, push = cursordb.diff(lidx, ridx.get("composers", {}))
    cap = cfg.max_composers_per_run
    if cap:
        pull, push = pull[:cap], push[:cap]
    log.info("cursor[%s]: pull=%d push=%d (cursor open: local=%s remote=%s)",
             peer, len(pull), len(push), local_running, remote_running)
    if dry_run:
        return
    ckpt_flag = " --checkpoints" if cfg.sync_checkpoints else ""

    if "pull" in cfg.directions and pull:
        if local_running:
            log.info("cursor[%s]: pull skipped — Cursor is open locally", peer)
        else:
            exp = subprocess.Popen(
                ssh_argv(peer, remote_chatmesh_cmd("export-cursor-rows" + ckpt_flag)),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            app = subprocess.Popen(
                [sys.executable, "-m", "chatmesh", "apply-cursor",
                 "--backup", os.path.join(backup_dir(cfg.state_dir),
                                          "cursor-rows-%s.jsonl" % peer)],
                stdin=exp.stdout, stdout=subprocess.PIPE,
                env=dict(os.environ, PYTHONPATH=repo_root()))
            _pipe_json(exp, pull)
            out, _ = app.communicate()
            exp.wait()
            res = _last_json(out)
            log.info("cursor[%s]: pull result %s", peer, res)
            mark(state, peer, "cursor", "pull", **(res or {"ok": False}))

    if "push" in cfg.directions and push:
        if remote_running:
            log.info("cursor[%s]: push skipped — Cursor is open on %s", peer, peer)
        else:
            exp = subprocess.Popen(
                [sys.executable, "-m", "chatmesh", "export-cursor-rows"] +
                (["--checkpoints"] if cfg.sync_checkpoints else []),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                env=dict(os.environ, PYTHONPATH=repo_root()))
            app = subprocess.Popen(
                ssh_argv(peer, remote_chatmesh_cmd(
                    'apply-cursor --backup "$HOME/.local/state/chatmesh/backups/cursor-rows-hub.jsonl"')),
                stdin=exp.stdout, stdout=subprocess.PIPE)
            _pipe_json(exp, push)
            out, _ = app.communicate()
            exp.wait()
            res = _last_json(out)
            log.info("cursor[%s]: push result %s", peer, res)
            mark(state, peer, "cursor", "push", **(res or {"ok": False}))


def _last_json(out: bytes) -> Optional[dict]:
    for line in reversed(out.decode(errors="replace").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                pass
    return None


# --------------------------------------------------------------------------- #
# File trees
# --------------------------------------------------------------------------- #
def sync_tree(cfg: Config, peer: str, rhome: str, tree: str, state: dict,
              dry_run: bool) -> None:
    app = TREE_APP[tree]
    gated = app in cfg.process_gate_apps
    try:
        rman = json.loads(ssh_out(peer, remote_chatmesh_cmd(
            "files-manifest --tree %s" % tree), timeout=600))
    except Exception as e:
        log.error("%s[%s]: manifest fetch failed: %s", tree, peer, e)
        return
    lman = filetrees.manifest(tree)
    lhome = os.path.expanduser("~")
    pull = filetrees.plan_transfers(tree, rman, lman, rhome, lhome,
                                    cfg.file_guard_sec)
    push = filetrees.plan_transfers(tree, lman, rman, lhome, rhome,
                                    cfg.file_guard_sec)
    log.info("%s[%s]: pull=%d push=%d files", tree, peer, len(pull), len(push))
    if dry_run:
        return

    if "pull" in cfg.directions and pull:
        if gated and app_running_local(app):
            log.info("%s[%s]: pull skipped — %s running locally", tree, peer, app)
        else:
            send = subprocess.Popen(
                ssh_argv(peer, remote_chatmesh_cmd("files-send --tree %s" % tree)),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            recv = subprocess.Popen(
                [sys.executable, "-m", "chatmesh", "files-recv", "--tree", tree,
                 "--src-home", rhome, "--backup", backup_dir(cfg.state_dir),
                 "--guard", str(cfg.file_guard_sec)],
                stdin=send.stdout, stdout=subprocess.PIPE,
                env=dict(os.environ, PYTHONPATH=repo_root()))
            _pipe_json(send, pull)
            out, _ = recv.communicate()
            send.wait()
            res = _last_json(out)
            log.info("%s[%s]: pull result %s", tree, peer, res)
            mark(state, peer, tree, "pull", **(res or {"ok": False}))

    if "push" in cfg.directions and push:
        if gated and app_running_remote(peer, app):
            log.info("%s[%s]: push skipped — %s running on peer", tree, peer, app)
        else:
            send = subprocess.Popen(
                [sys.executable, "-m", "chatmesh", "files-send", "--tree", tree],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                env=dict(os.environ, PYTHONPATH=repo_root()))
            recv = subprocess.Popen(
                ssh_argv(peer, remote_chatmesh_cmd(
                    'files-recv --tree %s --src-home "%s" '
                    '--backup "$HOME/.local/state/chatmesh/backups" --guard %d'
                    % (tree, lhome, cfg.file_guard_sec))),
                stdin=send.stdout, stdout=subprocess.PIPE)
            _pipe_json(send, push)
            out, _ = recv.communicate()
            send.wait()
            res = _last_json(out)
            log.info("%s[%s]: push result %s", tree, peer, res)
            mark(state, peer, tree, "push", **(res or {"ok": False}))


# --------------------------------------------------------------------------- #
# History files
# --------------------------------------------------------------------------- #
def sync_history(cfg: Config, peer: str, rhome: str, name: str, state: dict,
                 dry_run: bool) -> None:
    lpath = filetrees.history_path(name)
    rpath = filetrees.history_path(name, rhome)
    if dry_run:
        return
    if "pull" in cfg.directions:
        try:
            remote_text = ssh_out(peer, 'cat "%s" 2>/dev/null || true' % rpath,
                                  timeout=120)
        except RuntimeError as e:
            log.error("%s[%s]: pull read failed: %s", name, peer, e)
            remote_text = ""
        if remote_text.strip():
            rw = HomeRewriter(rhome, os.path.expanduser("~"))
            incoming = [rw.text(ln)[0] for ln in remote_text.splitlines() if ln.strip()]
            existing = []
            if os.path.exists(lpath):
                with open(lpath) as f:
                    existing = [ln for ln in f.read().splitlines() if ln.strip()]
            merged, added = filetrees.merge_history_lines(existing, incoming)
            if added:
                tmp = lpath + ".chatmesh-tmp"
                os.makedirs(os.path.dirname(lpath), exist_ok=True)
                with open(tmp, "w") as f:
                    f.write("\n".join(merged) + "\n")
                os.replace(tmp, lpath)
            log.info("%s[%s]: pull merged %d lines", name, peer, added)
            mark(state, peer, name, "pull", added=added)
    if "push" in cfg.directions and os.path.exists(lpath):
        with open(lpath, "rb") as f:
            data = f.read()
        if data.strip():
            p = subprocess.Popen(
                ssh_argv(peer, remote_chatmesh_cmd(
                    'merge-history --name %s --src-home "%s"'
                    % (name, os.path.expanduser("~")))),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            out, _ = p.communicate(data)
            res = _last_json(out)
            log.info("%s[%s]: push result %s", name, peer, res)
            mark(state, peer, name, "push", **(res or {"ok": False}))


# --------------------------------------------------------------------------- #
# Git repositories, refs, worktrees, and WIP
# --------------------------------------------------------------------------- #
def _remote_json(peer: str, args, timeout: int = 600) -> dict:
    text = ssh_out(peer, remote_chatmesh_args(list(args)), timeout=timeout)
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError("remote command returned no JSON")


def _local_git_manifest(cfg: Config, dry_run: bool = False) -> dict:
    from .repoops import inventory
    inventory_errors = []
    return {
        "version": 1,
        "roots": cfg.git.roots,
        "repositories": inventory(
            cfg.git,
            github_cache=(
                None if dry_run else
                os.path.join(cfg.state_dir, "github-repositories.json")
            ),
            resolve_github=True,
            errors=inventory_errors,
        ),
        "errors": inventory_errors,
    }


def _sync_repo_layout(cfg: Config, peer: str, local_manifest: dict,
                      remote_manifest: dict, dry_run: bool) -> bool:
    """Canonicalize origins/paths; return whether manifests need refreshing."""
    from . import gitrepos
    from .repoops import (
        relocation_target,
        relocate_repository,
        update_canonical_origin,
    )

    moved = False
    cursor_running = {
        "local": app_running_local("cursor"),
        "remote": app_running_remote(peer, "cursor"),
    }
    for side, records, roots in (
        ("local", local_manifest["repositories"], cfg.git.roots),
        ("remote", remote_manifest["repositories"], remote_manifest.get("roots", [])),
    ):
        targets = {}
        for candidate in records:
            target = relocation_target(candidate, roots)
            if target:
                targets.setdefault(os.path.normpath(target), []).append(candidate)
        for record in records:
            profile = cfg.git.for_repository(record.get("identity", ""))
            if not profile.enabled:
                continue
            origin = record.get("origin")
            path = record.get("logical_path")
            if origin and path:
                if side == "local":
                    changed = update_canonical_origin(path, origin, dry_run=dry_run)
                else:
                    changed = _remote_json(
                        peer,
                        ["git-set-origin", "--repo", path, "--origin", origin]
                        + (["--dry-run"] if dry_run else []),
                    ).get("changed", False)
                if changed:
                    log.info("git[%s]: canonicalized %s origin for %s",
                             peer, side, path)
            if not profile.relocate:
                continue
            if (
                record.get("kind") == "linked-worktree"
                and cursor_running[side]
            ):
                log.info(
                    "git[%s]: %s worktree relocation gated while Cursor "
                    "is running: %s",
                    peer, side, path,
                )
                continue
            target = relocation_target(record, roots)
            if not target or os.path.normpath(path) == os.path.normpath(target):
                continue
            if len(targets.get(os.path.normpath(target), [])) > 1:
                log.error(
                    "git[%s]: %s relocation quarantined; multiple checkouts "
                    "claim %s",
                    peer, side, target,
                )
                continue
            log.info("git[%s]: %s relocation %s -> %s",
                     peer, side, path, target)
            try:
                if side == "local":
                    relocate_repository(
                        gitrepos.open_repository(path), target, dry_run=dry_run
                    )
                else:
                    _remote_json(
                        peer,
                        ["git-relocate", "--repo", path, "--target", target]
                        + (["--dry-run"] if dry_run else []),
                    )
                moved = moved or not dry_run
            except Exception as exc:
                log.error("git[%s]: %s relocation blocked: %s", peer, side, exc)
    return moved


def _bootstrap_missing_repositories(cfg: Config, peer: str,
                                    local_manifest: dict,
                                    remote_manifest: dict,
                                    local_only, remote_only,
                                    dry_run: bool) -> bool:
    from .gittransport import push_all_refs
    from .repoops import (
        canonical_main_path,
        clone_from_peer,
        update_canonical_origin,
    )

    changed = False
    local_identities = {item["identity"] for item in local_manifest["repositories"]}
    remote_identities = {item["identity"] for item in remote_manifest["repositories"]}

    def destination_claims(records, roots):
        claims = {}
        for item in records:
            item_profile = cfg.git.for_repository(item.get("identity", ""))
            if not item_profile.enabled or not item_profile.clone_missing:
                continue
            if (
                item.get("nested")
                or item.get("kind") in ("submodule", "linked-worktree")
            ):
                continue
            target = canonical_main_path(item, roots)
            if target:
                claims.setdefault(os.path.normpath(target), []).append(item)
        return claims

    local_claims = destination_claims(remote_only, cfg.git.roots)
    remote_claims = destination_claims(
        local_only, remote_manifest.get("roots", [])
    )

    for record in remote_only:
        profile = cfg.git.for_repository(record.get("identity", ""))
        if not profile.enabled or not profile.clone_missing:
            continue
        if record.get("identity") in local_identities:
            continue
        if (
            record.get("nested")
            or record.get("kind") in ("submodule", "linked-worktree")
        ):
            log.info("git[%s]: missing nested/worktree checkout requires parent mapping: %s",
                     peer, record.get("logical_path"))
            continue
        target = canonical_main_path(record, cfg.git.roots)
        if not target:
            continue
        if len(local_claims.get(os.path.normpath(target), [])) > 1:
            log.error(
                "git[%s]: local clone quarantined; multiple peer checkouts "
                "claim %s",
                peer, target,
            )
            continue
        log.info("git[%s]: clone peer %s -> %s",
                 peer, record["logical_path"], target)
        try:
            clone_from_peer(
                peer, record["real_path"], target,
                branch=record.get("branch"), dry_run=dry_run,
            )
            if not dry_run and record.get("origin"):
                update_canonical_origin(target, record["origin"])
            changed = changed or not dry_run
        except Exception as exc:
            log.error("git[%s]: local clone blocked: %s", peer, exc)

    for record in local_only:
        profile = cfg.git.for_repository(record.get("identity", ""))
        if not profile.enabled or not profile.clone_missing:
            continue
        if record.get("identity") in remote_identities:
            continue
        if (
            record.get("nested")
            or record.get("kind") in ("submodule", "linked-worktree")
        ):
            log.info("git[%s]: missing nested/worktree checkout requires parent mapping: %s",
                     peer, record.get("logical_path"))
            continue
        target = canonical_main_path(record, remote_manifest.get("roots", []))
        origin = record.get("origin")
        if not target or not origin:
            continue
        if len(remote_claims.get(os.path.normpath(target), [])) > 1:
            log.error(
                "git[%s]: peer bootstrap quarantined; multiple local "
                "checkouts claim %s",
                peer, target,
            )
            continue
        log.info("git[%s]: initialize peer checkout %s", peer, target)
        try:
            _remote_json(
                peer,
                ["git-init-checkout", "--target", target, "--origin", origin]
                + (["--dry-run"] if dry_run else []),
            )
            if not dry_run:
                push_all_refs(record["real_path"], peer, target)
                branch = record.get("branch")
                if branch:
                    _remote_json(
                        peer,
                        ["git-checkout-branch", "--repo", target,
                         "--branch", branch],
                    )
                changed = True
        except Exception as exc:
            log.error("git[%s]: peer bootstrap blocked: %s", peer, exc)
    return changed


def _sync_git_refs(cfg: Config, peer: str, pairs, dry_run: bool) -> dict:
    from .gittransport import fetch_branch, fetch_tag, push_branch
    from .repoops import converge_branch, converge_tag
    from .syncplan import branch_import_plan

    result = {
        "branches_equal": 0,
        "fast_forwards": 0,
        "branches_created": 0,
        "diverged": 0,
        "tags_created": 0,
        "tag_conflicts": 0,
        "errors": 0,
    }
    processed = set()
    for pair in pairs:
        profile = cfg.git.for_repository(pair.identity)
        if not profile.enabled:
            continue
        local = pair.local
        remote = pair.remote
        ref_key = (
            pair.identity,
            local.get("common_dir"),
            remote.get("common_dir"),
        )
        if ref_key in processed:
            continue
        processed.add(ref_key)
        local_repo = local["real_path"]
        remote_repo = remote["real_path"]
        actions = (
            branch_import_plan(
                local.get("branches", {}),
                remote.get("branches", {}),
                cfg.directions,
            )
            if profile.sync_branches
            else []
        )
        branch_conflicts = set()
        for action in actions:
            branch = action["branch"]
            source_oid = action["source_oid"]
            log.info("git[%s]: %s %s %s", peer, action["action"],
                     pair.identity, branch)
            if dry_run:
                continue
            try:
                if action["action"] == "pull-ref":
                    incoming = fetch_branch(
                        local_repo, peer, remote_repo, branch, source_oid
                    )
                    detail = converge_branch(
                        local_repo, branch, incoming, peer,
                        create_resolution=True,
                    )
                    if detail["action"] == "resolution-worktree":
                        branch_conflicts.add(branch)
                else:
                    incoming = push_branch(
                        local_repo, peer, remote_repo, branch, source_oid,
                        os.uname().nodename,
                    )
                    detail = _remote_json(
                        peer,
                        ["git-advance", "--repo", remote_repo,
                         "--branch", branch, "--incoming-ref", incoming,
                         "--peer", os.uname().nodename]
                        + (["--no-resolution"] if branch in branch_conflicts else []),
                    )
                action_name = detail.get("action")
                if action_name == "fast-forward":
                    result["fast_forwards"] += 1
                elif action_name == "created-branch":
                    result["branches_created"] += 1
                elif action_name in ("resolution-worktree", "diverged"):
                    result["diverged"] += 1
                elif action_name in ("equal", "local-ahead"):
                    result["branches_equal"] += 1
            except Exception as exc:
                result["errors"] += 1
                log.error("git[%s]: branch %s failed: %s", peer, branch, exc)

        if not profile.sync_tags:
            continue
        local_tags = local.get("tags", {})
        remote_tags = remote.get("tags", {})
        for tag in sorted(set(local_tags) | set(remote_tags)):
            loid, roid = local_tags.get(tag), remote_tags.get(tag)
            if loid == roid:
                continue
            if roid and "pull" in cfg.directions:
                if not dry_run:
                    try:
                        incoming = fetch_tag(local_repo, peer, remote_repo, tag, roid)
                        detail = converge_tag(local_repo, tag, incoming)
                        if detail["action"] == "created-tag":
                            result["tags_created"] += 1
                        elif detail["action"] == "tag-conflict":
                            result["tag_conflicts"] += 1
                    except Exception as exc:
                        result["errors"] += 1
                        log.error("git[%s]: pull tag %s failed: %s", peer, tag, exc)
            if loid and "push" in cfg.directions:
                if not dry_run:
                    try:
                        incoming = push_branch(
                            local_repo, peer, remote_repo, "tag-" + tag, loid,
                            os.uname().nodename,
                        )
                        detail = _remote_json(
                            peer,
                            ["git-update-tag", "--repo", remote_repo,
                             "--tag", tag, "--incoming-ref", incoming],
                        )
                        if detail["action"] == "created-tag":
                            result["tags_created"] += 1
                        elif detail["action"] == "tag-conflict":
                            result["tag_conflicts"] += 1
                    except Exception as exc:
                        result["errors"] += 1
                        log.error("git[%s]: push tag %s failed: %s", peer, tag, exc)
    return result


def _sync_worktree_topology(cfg: Config, peer: str, local_manifest: dict,
                            remote_manifest: dict, local_only, remote_only,
                            dry_run: bool) -> dict:
    from .repoops import ensure_branch_worktree

    result = {"created": 0, "skipped": 0, "errors": 0}

    def main_record(records, identity):
        return next((
            item for item in records
            if item.get("identity") == identity
            and item.get("kind") not in ("linked-worktree", "submodule")
            and not item.get("bare")
        ), None)

    for source, destination_records, side in (
        (local_only, remote_manifest["repositories"], "remote"),
        (remote_only, local_manifest["repositories"], "local"),
    ):
        for record in source:
            profile = cfg.git.for_repository(record.get("identity", ""))
            if not profile.enabled or not profile.sync_worktrees:
                continue
            if record.get("kind") != "linked-worktree" or not record.get("branch"):
                continue
            destination = main_record(destination_records, record["identity"])
            if destination is None:
                result["skipped"] += 1
                continue
            log.info(
                "git[%s]: ensure %s worktree %s for %s",
                peer, side, record["branch"], record["identity"],
            )
            try:
                if side == "local":
                    detail = ensure_branch_worktree(
                        destination["real_path"], record["branch"], dry_run=dry_run
                    )
                else:
                    detail = _remote_json(
                        peer,
                        ["git-ensure-worktree", "--repo", destination["real_path"],
                         "--branch", record["branch"]]
                        + (["--dry-run"] if dry_run else []),
                    )
                if detail.get("created"):
                    result["created"] += 1
            except Exception as exc:
                result["errors"] += 1
                log.error("git[%s]: worktree creation blocked: %s", peer, exc)
    return result


def _transfer_wip(cfg: Config, peer: str, pair, action: dict,
                  profile) -> dict:
    local_repo = pair.local["real_path"]
    remote_repo = pair.remote["real_path"]
    apply_flag = bool(action["apply"] and profile.auto_apply)
    if action["action"] == "pull-wip":
        send_args = ssh_argv(peer, remote_chatmesh_args(
            ["git-wip-export", "--repo", remote_repo]
        ))
        send_env = None
        receive_args = [
            sys.executable, "-m", "chatmesh", "git-wip-import",
            "--repo", local_repo, "--peer", peer,
        ] + (["--apply"] if apply_flag else [])
        receive_env = dict(os.environ, PYTHONPATH=repo_root())
    else:
        send_args = [
            sys.executable, "-m", "chatmesh", "git-wip-export",
            "--repo", local_repo,
        ]
        send_env = dict(os.environ, PYTHONPATH=repo_root())
        receive_args = ssh_argv(peer, remote_chatmesh_args(
            ["git-wip-import", "--repo", remote_repo,
             "--peer", os.uname().nodename]
            + (["--apply"] if apply_flag else [])
        ))
        receive_env = None

    # Export and import are deliberately sequential. A streaming shell pipeline
    # can let an importer consume a partial/non-archive payload when snapshot
    # creation fails (for example, for an unmerged index).
    with tempfile.TemporaryFile() as archive:
        send = subprocess.run(
            send_args,
            stdout=archive,
            env=send_env,
        )
        if send.returncode:
            return {
                "ok": False,
                "reason": "snapshot-export-failed",
                "send_returncode": send.returncode,
                "recv_returncode": None,
            }
        archive.seek(0)
        recv = subprocess.run(
            receive_args,
            stdin=archive,
            stdout=subprocess.PIPE,
            env=receive_env,
        )
    parsed = _last_json(recv.stdout)
    if recv.returncode or parsed is None:
        return {
            "ok": False,
            "reason": "snapshot-import-failed",
            "send_returncode": send.returncode,
            "recv_returncode": recv.returncode,
        }
    return parsed


def sync_git(cfg: Config, peer: str, state: dict, dry_run: bool) -> None:
    from .syncplan import match_repositories, wip_transfer_plan

    try:
        remote_manifest = _remote_json(
            peer, ["git-manifest"] + (["--no-cache"] if dry_run else []),
            timeout=1800,
        )
        local_manifest = _local_git_manifest(cfg, dry_run)
    except Exception as exc:
        log.error("git[%s]: manifest failed: %s", peer, exc)
        mark(state, peer, "git", "sync", ok=False, error=str(exc))
        return

    for side, manifest in (
        ("local", local_manifest),
        ("remote", remote_manifest),
    ):
        for error in manifest.get("errors", []):
            log.error(
                "git[%s]: %s repository unavailable: %s: %s",
                peer,
                side,
                error.get("logical_path", "(unknown)"),
                error.get("error", "inventory failed"),
            )

    if _sync_repo_layout(
        cfg, peer, local_manifest, remote_manifest, dry_run
    ) and not dry_run:
        remote_manifest = _remote_json(peer, ["git-manifest"], timeout=1800)
        local_manifest = _local_git_manifest(cfg)

    pairs, local_only, remote_only, ambiguities = match_repositories(
        local_manifest["repositories"], remote_manifest["repositories"]
    )
    log.info(
        "git[%s]: matched=%d local-only=%d remote-only=%d ambiguous=%d",
        peer, len(pairs), len(local_only), len(remote_only), len(ambiguities),
    )
    for ambiguity in ambiguities:
        log.error(
            "git[%s]: ambiguous %s (%s): local=%s remote=%s",
            peer,
            ambiguity.identity,
            ambiguity.reason,
            [item.get("logical_path") for item in ambiguity.local],
            [item.get("logical_path") for item in ambiguity.remote],
        )
    if _bootstrap_missing_repositories(
        cfg, peer, local_manifest, remote_manifest,
        local_only, remote_only, dry_run,
    ) and not dry_run:
        remote_manifest = _remote_json(peer, ["git-manifest"], timeout=1800)
        local_manifest = _local_git_manifest(cfg)
        pairs, local_only, remote_only, ambiguities = match_repositories(
            local_manifest["repositories"], remote_manifest["repositories"]
        )

    ref_result = _sync_git_refs(cfg, peer, pairs, dry_run)
    worktree_result = _sync_worktree_topology(
        cfg, peer, local_manifest, remote_manifest,
        local_only, remote_only, dry_run,
    )
    if worktree_result["created"] and not dry_run:
        remote_manifest = _remote_json(peer, ["git-manifest"], timeout=1800)
        local_manifest = _local_git_manifest(cfg)
        pairs, local_only, remote_only, ambiguities = match_repositories(
            local_manifest["repositories"], remote_manifest["repositories"]
        )
    wip_counts = {"transferred": 0, "applied": 0, "quarantined": 0, "errors": 0}
    for pair in pairs:
        profile = cfg.git.for_repository(pair.identity)
        if not profile.enabled:
            continue
        if pair.local.get("bare") or pair.remote.get("bare"):
            continue
        for action in wip_transfer_plan(pair.local, pair.remote, cfg.directions):
            log.info(
                "git[%s]: %s %s apply=%s reason=%s",
                peer, action["action"], pair.identity,
                action["apply"] and profile.auto_apply, action["reason"],
            )
            if dry_run:
                continue
            detail = _transfer_wip(cfg, peer, pair, action, profile)
            if detail.get("ok"):
                wip_counts["transferred"] += 1
                if detail.get("quarantined"):
                    wip_counts["quarantined"] += 1
                else:
                    wip_counts["applied"] += 1
            else:
                wip_counts["errors"] += 1
                log.error(
                    "git[%s]: %s %s failed: %s",
                    peer, action["action"], pair.identity,
                    detail.get("reason", "snapshot transfer failed"),
                )
    detail = {
        "ok": (
            ref_result["errors"] == 0
            and wip_counts["errors"] == 0
            and not local_manifest.get("errors")
            and not remote_manifest.get("errors")
        ),
        "matched": len(pairs),
        "local_only": len(local_only),
        "remote_only": len(remote_only),
        "ambiguous": len(ambiguities),
        "inventory_errors": (
            len(local_manifest.get("errors", []))
            + len(remote_manifest.get("errors", []))
        ),
        **ref_result,
        **{"worktrees_" + key: value for key, value in worktree_result.items()},
        **{"wip_" + key: value for key, value in wip_counts.items()},
        "dry_run": dry_run,
    }
    mark(state, peer, "git", "sync", **detail)


# --------------------------------------------------------------------------- #
# User-level preferences
# --------------------------------------------------------------------------- #
def _remote_apply_preferences(peer: str, plan: dict) -> dict:
    proc = subprocess.run(
        ssh_argv(peer, remote_chatmesh_args(["preferences-apply"])),
        input=json.dumps(plan, sort_keys=True).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=900,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "remote preference apply failed: %s"
            % proc.stderr.decode("utf-8", "replace").strip()[:1000]
        )
    parsed = _last_json(proc.stdout)
    if parsed is None:
        raise RuntimeError("remote preference apply returned no result")
    return parsed


def sync_preferences(cfg: Config, peer: str, state: dict,
                     dry_run: bool) -> None:
    from .preferenceops import (
        apply_preference_plan,
        converged_baseline,
        load_baseline,
        plan_snapshots,
        rewrite_snapshot_home,
        snapshot_preferences,
        write_baseline,
    )
    from .util import home

    try:
        local = snapshot_preferences(cfg)
        remote = _remote_json(peer, ["preferences-export"], timeout=900)
        local_home = home()
        remote_home_value = str(remote["home"])
        remote_local = rewrite_snapshot_home(remote, local_home)
        base_local = load_baseline(cfg, peer, local_home)

        pull_plan = plan_snapshots(base_local, local, remote_local)
        base_remote = rewrite_snapshot_home(base_local, remote_home_value)
        local_remote = rewrite_snapshot_home(local, remote_home_value)
        push_plan = plan_snapshots(base_remote, remote, local_remote)
        log.info(
            "preferences[%s]: pull=%s push=%s blocked(local=%d remote=%d)",
            peer,
            pull_plan["counts"],
            push_plan["counts"],
            len(local["manifest"].get("blocked", [])),
            len(remote["manifest"].get("blocked", [])),
        )
        if dry_run:
            mark(
                state, peer, "preferences", "sync",
                ok=True, dry_run=True,
                pull=pull_plan["counts"], push=push_plan["counts"],
            )
            return

        local_result = {"applied": [], "kept": [], "conflicts": []}
        remote_result = {"applied": [], "kept": [], "conflicts": []}
        if "pull" in cfg.directions:
            local_result = apply_preference_plan(cfg, pull_plan)
        if "push" in cfg.directions:
            remote_result = _remote_apply_preferences(peer, push_plan)

        # Re-scan after writes and retain old bases for unresolved conflicts.
        final_local = snapshot_preferences(cfg)
        final_remote = _remote_json(peer, ["preferences-export"], timeout=900)
        final_remote_local = rewrite_snapshot_home(final_remote, local_home)
        baseline = converged_baseline(
            final_local, final_remote_local, base_local
        )
        write_baseline(cfg, peer, baseline)
        mark(
            state, peer, "preferences", "sync",
            ok=True,
            local_applied=len(local_result.get("applied", [])),
            local_conflicts=len(local_result.get("conflicts", [])),
            remote_applied=len(remote_result.get("applied", [])),
            remote_conflicts=len(remote_result.get("conflicts", [])),
            blocked_local=len(local["manifest"].get("blocked", [])),
            blocked_remote=len(remote["manifest"].get("blocked", [])),
        )
    except Exception as exc:
        log.error("preferences[%s]: %s", peer, exc)
        mark(state, peer, "preferences", "sync", ok=False, error=str(exc))


# --------------------------------------------------------------------------- #
# Additive machine environments
# --------------------------------------------------------------------------- #
def _remote_plan_environment(peer: str, snapshot: dict) -> dict:
    proc = subprocess.run(
        ssh_argv(peer, remote_chatmesh_args(["environment-plan"])),
        input=json.dumps(snapshot, sort_keys=True).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=900,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "remote environment plan failed: %s"
            % proc.stderr.decode("utf-8", "replace").strip()[:1000]
        )
    parsed = _last_json(proc.stdout)
    if parsed is None:
        raise RuntimeError("remote environment plan returned no result")
    return parsed


def _remote_apply_environment(
    peer: str,
    snapshot: dict,
    *,
    force: bool = False,
    action_count: Optional[int] = None,
) -> dict:
    if action_count is None:
        remote_plan = _remote_plan_environment(peer, snapshot)
        action_count = len(remote_plan.get("actions", []))
    args = [
        "environment-apply",
        "--peer",
        os.uname().nodename,
    ] + (["--force"] if force else [])
    proc = subprocess.run(
        ssh_argv(peer, remote_chatmesh_args(args)),
        input=json.dumps(snapshot, sort_keys=True).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=max(3600, 1800 * (action_count + 1)),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "remote environment apply failed: %s"
            % proc.stderr.decode("utf-8", "replace").strip()[:1000]
        )
    parsed = _last_json(proc.stdout)
    if parsed is None:
        raise RuntimeError("remote environment apply returned no result")
    return parsed


def sync_environment(
    cfg: Config, peer: str, state: dict, dry_run: bool
) -> None:
    from .environmentops import (
        apply_environment_snapshot,
        plan_snapshots,
        snapshot_environment,
    )

    try:
        local = snapshot_environment(cfg)
        remote = _remote_json(peer, ["environment-export"], timeout=900)
        empty_plan = {
            "actions": [],
            "conflicts": [],
            "blocked": [],
            "counts": {
                "install": 0,
                "keep": 0,
                "conflict": 0,
                "blocked": 0,
            },
        }
        pull_plan = (
            plan_snapshots(cfg, remote, local)
            if "pull" in cfg.directions
            else empty_plan
        )
        push_plan = (
            _remote_plan_environment(peer, local)
            if "push" in cfg.directions
            else empty_plan
        )
        log.info(
            "environment[%s]: pull=%s push=%s blocked(local=%d remote=%d)",
            peer,
            pull_plan["counts"],
            push_plan["counts"],
            len(local.get("blocked", [])),
            len(remote.get("blocked", [])),
        )
        if dry_run:
            mark(
                state,
                peer,
                "environment",
                "sync",
                ok=True,
                dry_run=True,
                pull=pull_plan["counts"],
                push=push_plan["counts"],
            )
            return
        local_result = {
            "ok": True,
            "applied": [],
            "failed": [],
            "pending": 0,
            "conflicts": [],
        }
        remote_result = dict(local_result)
        if "pull" in cfg.directions:
            local_result = apply_environment_snapshot(cfg, peer, remote)
        if "push" in cfg.directions:
            remote_result = _remote_apply_environment(
                peer,
                local,
                action_count=len(push_plan.get("actions", [])),
            )
        mark(
            state,
            peer,
            "environment",
            "sync",
            ok=bool(local_result.get("ok") and remote_result.get("ok")),
            local_applied=len(local_result.get("applied", [])),
            local_failed=len(local_result.get("failed", [])),
            local_pending=int(local_result.get("pending", 0)),
            local_conflicts=len(local_result.get("conflicts", [])),
            remote_applied=len(remote_result.get("applied", [])),
            remote_failed=len(remote_result.get("failed", [])),
            remote_pending=int(remote_result.get("pending", 0)),
            remote_conflicts=len(remote_result.get("conflicts", [])),
        )
    except Exception as exc:
        log.error("environment[%s]: %s", peer, exc)
        mark(state, peer, "environment", "sync", ok=False, error=str(exc))


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #
APP_UNITS = {
    "cursor": [("db", None)],
    "cursor-cli": [("tree", "cursor-cli")],
    "claude": [("tree", "claude-projects"), ("history", "claude-history")],
    "codex": [("tree", "codex-sessions"), ("history", "codex-history")],
}


def sync_all(cfg: Config, only_peer: Optional[str] = None,
             only_app: Optional[str] = None, dry_run: bool = False) -> None:
    if not cfg.peers:
        log.warning("no peers configured in config.toml — nothing to do")
        return
    state = load_state(cfg.state_dir)
    for peer in cfg.peers:
        if only_peer and peer != only_peer:
            continue
        try:
            rhome = remote_home(peer)
        except Exception as e:
            log.error("peer %s unreachable: %s", peer, e)
            continue
        if dry_run:
            rv = deployed_version(peer)
            if rv != VERSION:
                log.error(
                    "peer %s: dry-run cannot deploy; peer has %s, need %s",
                    peer, rv or "none", VERSION,
                )
                continue
        else:
            try:
                ensure_deployed(peer)
            except Exception as e:
                log.error("peer %s: deploy failed: %s", peer, e)
                continue
        for app in cfg.apps:
            if only_app and app != only_app:
                continue
            for kind, unit in APP_UNITS.get(app, []):
                try:
                    if kind == "db":
                        sync_cursor(cfg, peer, rhome, state, dry_run)
                    elif kind == "tree":
                        sync_tree(cfg, peer, rhome, unit, state, dry_run)
                    elif kind == "history":
                        sync_history(cfg, peer, rhome, unit, state, dry_run)
                except Exception as e:
                    log.exception("%s/%s[%s] failed: %s", app, unit, peer, e)
        if cfg.git.enabled and (only_app is None or only_app == "git"):
            try:
                sync_git(cfg, peer, state, dry_run)
            except Exception as e:
                log.exception("git[%s] failed: %s", peer, e)
        if (
            cfg.preferences.enabled
            and (only_app is None or only_app == "preferences")
        ):
            try:
                sync_preferences(cfg, peer, state, dry_run)
            except Exception as e:
                log.exception("preferences[%s] failed: %s", peer, e)
        if (
            cfg.environment.enabled
            and (only_app is None or only_app == "environment")
        ):
            try:
                sync_environment(cfg, peer, state, dry_run)
            except Exception as e:
                log.exception("environment[%s] failed: %s", peer, e)
        if not dry_run:
            save_state(state, cfg.state_dir)
    log.info("sync complete")
