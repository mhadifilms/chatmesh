"""chatmesh CLI. User-facing: sync, status, doctor, install, uninstall,
deploy, init. Internal (invoked over ssh between machines): export-cursor-index,
export-cursor-rows, apply-cursor, files-manifest, files-send, files-recv,
merge-history."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

from . import VERSION, cursordb, filetrees
from .config import Config, ConfigError, default_config_path, write_example
from .util import Lock, app_running_local, log, setup_logging


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="chatmesh")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sync", help="sync with all configured peers")
    p.add_argument("--peer")
    p.add_argument("--app")
    p.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="last sync results and gate states")
    sub.add_parser("doctor", help="check peers, DBs, versions")
    sub.add_parser("install", help="install login/interval LaunchAgent")
    sub.add_parser("uninstall", help="remove LaunchAgent")
    sub.add_parser("init", help="write example config.toml")
    p = sub.add_parser("deploy", help="push chatmesh itself to a peer")
    p.add_argument("--peer", required=True)
    p = sub.add_parser("config", help="validate, print, or migrate configuration")
    config_sub = p.add_subparsers(dest="config_cmd", required=True)
    config_sub.add_parser("validate", help="validate config.toml")
    config_sub.add_parser("show", help="print normalized config.toml")
    p = config_sub.add_parser("migrate", help="one-time migration from the old env file")
    p.add_argument("--from", dest="from_path",
                   default=os.path.expanduser("~/.config/chatmesh/env"))
    p = sub.add_parser("git", help="inspect and resolve repository synchronization")
    git_sub = p.add_subparsers(dest="git_cmd", required=True)
    p = git_sub.add_parser("list", help="list discovered repositories")
    p.add_argument("--json", action="store_true")
    git_sub.add_parser("status", help="list Git inboxes, conflicts, and journals")
    p = git_sub.add_parser("show", help="show a WIP snapshot manifest")
    p.add_argument("--snapshot", required=True)
    p = git_sub.add_parser("snapshot", help="export exact WIP")
    p.add_argument("--repo", required=True)
    p.add_argument("--output", required=True)
    p = git_sub.add_parser("apply", help="apply a verified WIP snapshot")
    p.add_argument("--repo", required=True)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--prior")
    p = git_sub.add_parser("accept", help="accept an isolated resolution branch")
    p.add_argument("--repo", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--resolution", required=True)
    p.add_argument("--expected-old")
    p = git_sub.add_parser("recover", help="recover a journaled WIP apply")
    p.add_argument("--repo", required=True)
    p.add_argument("--journal", required=True)
    p = sub.add_parser(
        "preferences", help="inspect curated user preference synchronization"
    )
    preference_sub = p.add_subparsers(dest="preference_cmd", required=True)
    p = preference_sub.add_parser("list", help="list safe preference inventory")
    p.add_argument("--json", action="store_true")
    preference_sub.add_parser("conflicts", help="list preference conflict inbox")

    sub.add_parser("export-cursor-index")
    p = sub.add_parser("export-cursor-rows")
    p.add_argument("--checkpoints", action="store_true")
    p = sub.add_parser("apply-cursor")
    p.add_argument("--backup", required=True)
    p.add_argument("--src-home")
    p = sub.add_parser("files-manifest")
    p.add_argument("--tree", required=True)
    p = sub.add_parser("files-send")
    p.add_argument("--tree", required=True)
    p = sub.add_parser("files-recv")
    p.add_argument("--tree", required=True)
    p.add_argument("--src-home", required=True)
    p.add_argument("--backup", required=True)
    p.add_argument("--guard", type=int, default=900)
    p = sub.add_parser("merge-history")
    p.add_argument("--name", required=True)
    p.add_argument("--src-home", required=True)
    p = sub.add_parser("git-manifest")
    p.add_argument("--no-cache", action="store_true")
    p = sub.add_parser("git-advance")
    p.add_argument("--repo", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--incoming-ref", required=True)
    p.add_argument("--peer", required=True)
    p.add_argument("--no-resolution", action="store_true")
    p = sub.add_parser("git-update-tag")
    p.add_argument("--repo", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--incoming-ref", required=True)
    p = sub.add_parser("git-wip-export")
    p.add_argument("--repo", required=True)
    p = sub.add_parser("git-wip-import")
    p.add_argument("--repo", required=True)
    p.add_argument("--peer", required=True)
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("git-init-checkout")
    p.add_argument("--target", required=True)
    p.add_argument("--origin", required=True)
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("git-ensure-worktree")
    p.add_argument("--repo", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("git-checkout-branch")
    p.add_argument("--repo", required=True)
    p.add_argument("--branch", required=True)
    p = sub.add_parser("git-relocate")
    p.add_argument("--repo", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("git-set-origin")
    p.add_argument("--repo", required=True)
    p.add_argument("--origin", required=True)
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("preferences-export")
    sub.add_parser("preferences-apply")

    args = ap.parse_args(argv)
    try:
        cfg = Config.load()
    except ConfigError as exc:
        print("configuration error: %s" % exc, file=sys.stderr)
        return 2
    is_dry_run = args.cmd == "sync" and bool(getattr(args, "dry_run", False))
    setup_logging(
        cfg.log_level, cfg.state_dir, file_logging=not is_dry_run
    )

    if args.cmd == "sync":
        from .sync import sync_all
        lock = Lock(
            name="chatmesh-dry-run.lock" if args.dry_run else "sync.lock",
            state_dir=tempfile.gettempdir() if args.dry_run else cfg.state_dir,
        )
        if not lock.acquire():
            log.info("another chatmesh run holds the lock; exiting")
            return 0
        try:
            sync_all(cfg, only_peer=args.peer, only_app=args.app,
                     dry_run=args.dry_run)
        finally:
            lock.release()
        return 0

    if args.cmd == "status":
        from .sync import load_state
        state = load_state(cfg.state_dir)
        print("chatmesh %s" % VERSION)
        print("peers: %s | apps: %s" % (",".join(cfg.peers), ",".join(cfg.apps)))
        for app in cfg.apps:
            print("  %-10s running locally: %s" % (app, app_running_local(app)))
        print("  git sync: %s | preferences: %s" % (
            "enabled" if cfg.git.enabled else "disabled",
            "enabled" if cfg.preferences.enabled else "disabled",
        ))
        for label, relative in (
            ("inbox", "inbox"),
            ("conflicts", "conflicts"),
            ("transactions", "transactions"),
        ):
            root = os.path.join(cfg.state_dir, relative)
            count = (
                sum(len(files) for _path, _dirs, files in os.walk(root))
                if os.path.isdir(root)
                else 0
            )
            print("  %-12s %d" % (label + ":", count))
        for peer, units in state.items():
            print("peer %s:" % peer)
            for unit, dirs in sorted(units.items()):
                for d, info in sorted(dirs.items()):
                    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(info.get("ts", 0)))
                    extra = {k: v for k, v in info.items() if k != "ts"}
                    print("  %-16s %-4s %s %s" % (unit, d, ts, extra))
        return 0

    if args.cmd == "doctor":
        return doctor(cfg)

    if args.cmd == "install":
        from . import launchd
        launchd.install(cfg.interval, cfg.state_dir)
        return 0

    if args.cmd == "uninstall":
        from . import launchd
        launchd.uninstall()
        return 0

    if args.cmd == "init":
        destination = default_config_path()
        try:
            write_example(destination)
        except FileExistsError:
            print("config exists: %s" % destination)
            return 1
        print("wrote %s — edit peers and profiles, then: chatmesh doctor"
              % destination)
        return 0

    if args.cmd == "config":
        if args.config_cmd == "validate":
            print("valid: %s" % default_config_path())
            return 0
        if args.config_cmd == "show":
            sys.stdout.write(cfg.to_toml())
            return 0
        if args.config_cmd == "migrate":
            return migrate_env_config(args.from_path)

    if args.cmd == "git":
        return run_git_command(cfg, args)
    if args.cmd == "preferences":
        return run_preferences_command(cfg, args)

    if args.cmd == "deploy":
        from .sync import ensure_deployed
        ensure_deployed(args.peer, force=True)
        return 0

    # ---- internal commands ----
    if args.cmd == "export-cursor-index":
        cursordb.cmd_export_index()
        return 0
    if args.cmd == "export-cursor-rows":
        cursordb.cmd_export_rows(args.checkpoints)
        return 0
    if args.cmd == "apply-cursor":
        cursordb.cmd_apply(args.src_home, args.backup)
        return 0
    if args.cmd == "files-manifest":
        filetrees.cmd_manifest(args.tree)
        return 0
    if args.cmd == "files-send":
        filetrees.cmd_send(args.tree)
        return 0
    if args.cmd == "files-recv":
        filetrees.cmd_recv(args.tree, args.src_home, args.backup, args.guard)
        return 0
    if args.cmd == "merge-history":
        filetrees.cmd_merge_history(args.name, args.src_home)
        return 0
    if args.cmd == "git-manifest":
        from .repoops import inventory
        inventory_errors = []
        json.dump({
            "version": 1,
            "roots": cfg.git.roots,
            "repositories": inventory(
                cfg.git,
                github_cache=None if args.no_cache else os.path.join(
                    cfg.state_dir, "github-repositories.json"),
                resolve_github=True,
                errors=inventory_errors,
            ),
            "errors": inventory_errors,
        }, sys.stdout)
        return 0
    if args.cmd == "git-advance":
        from .repoops import converge_branch
        result = converge_branch(
            args.repo, args.branch, args.incoming_ref, args.peer,
            create_resolution=not args.no_resolution,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.cmd == "git-update-tag":
        from .repoops import converge_tag
        print(json.dumps(
            converge_tag(args.repo, args.tag, args.incoming_ref),
            sort_keys=True,
        ))
        return 0
    if args.cmd == "git-wip-export":
        return export_git_wip(cfg, args.repo)
    if args.cmd == "git-wip-import":
        return import_git_wip(cfg, args.repo, args.peer, args.apply)
    if args.cmd == "git-init-checkout":
        from .repoops import initialize_checkout
        print(json.dumps(initialize_checkout(
            args.target, args.origin, dry_run=args.dry_run
        ), sort_keys=True))
        return 0
    if args.cmd == "git-ensure-worktree":
        from .repoops import ensure_branch_worktree
        print(json.dumps(ensure_branch_worktree(
            args.repo, args.branch, dry_run=args.dry_run
        ), sort_keys=True))
        return 0
    if args.cmd == "git-checkout-branch":
        from .repoops import checkout_initialized_branch
        print(json.dumps(
            checkout_initialized_branch(args.repo, args.branch),
            sort_keys=True,
        ))
        return 0
    if args.cmd == "git-relocate":
        from .gitrepos import open_repository
        from .repoops import relocate_repository
        print(json.dumps(relocate_repository(
            open_repository(args.repo), args.target, dry_run=args.dry_run
        ), sort_keys=True))
        return 0
    if args.cmd == "git-set-origin":
        from .repoops import update_canonical_origin
        print(json.dumps({
            "ok": True,
            "changed": update_canonical_origin(
                args.repo, args.origin, dry_run=args.dry_run
            ),
        }, sort_keys=True))
        return 0
    if args.cmd == "preferences-export":
        from .preferenceops import snapshot_preferences
        json.dump(snapshot_preferences(cfg), sys.stdout, sort_keys=True)
        return 0
    if args.cmd == "preferences-apply":
        from .preferenceops import apply_preference_plan
        limit = max(cfg.preferences.max_total_bytes * 3, 1024 * 1024)
        raw = sys.stdin.buffer.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("preference plan exceeds configured limit")
        plan = json.loads(raw.decode("utf-8"))
        print(json.dumps(apply_preference_plan(cfg, plan), sort_keys=True))
        return 0
    return 1


def migrate_env_config(source: str) -> int:
    """One-time, explicit migration; the runtime never reads legacy env."""
    destination = default_config_path()
    if os.path.exists(destination):
        print("config exists: %s" % destination, file=sys.stderr)
        return 1
    if not os.path.isfile(source):
        print("legacy config not found: %s" % source, file=sys.stderr)
        return 1
    values = {}
    with open(source, "r", encoding="utf-8") as stream:
        for raw in stream:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")

    def csv(name, default=""):
        return [item.strip() for item in values.get(name, default).split(",")
                if item.strip()]

    cfg = Config(
        peers=csv("CHATMESH_PEERS"),
        apps=csv("CHATMESH_APPS", "cursor,cursor-cli,claude,codex"),
        directions=csv("CHATMESH_DIRECTIONS", "pull,push"),
        interval=int(values.get("CHATMESH_INTERVAL", "3600")),
        file_guard_sec=int(values.get("CHATMESH_FILE_GUARD_MINUTES", "15")) * 60,
        sync_checkpoints=values.get("CHATMESH_SYNC_CHECKPOINTS", "0") == "1",
        max_composers_per_run=int(
            values.get("CHATMESH_MAX_COMPOSERS_PER_RUN", "0")
        ),
        process_gate_apps=csv(
            "CHATMESH_PROCESS_GATE_APPS", "cursor,cursor-cli"
        ),
        log_level=values.get("CHATMESH_LOG_LEVEL", "INFO").upper(),
    )
    try:
        cfg.write(destination)
    except (ConfigError, OSError, ValueError) as exc:
        print("migration failed: %s" % exc, file=sys.stderr)
        return 1
    archive = "%s.pre-toml-%s" % (source, time.strftime("%Y%m%d-%H%M%S"))
    shutil.move(source, archive)
    print("wrote %s" % destination)
    print("archived %s" % archive)
    return 0


def run_git_command(cfg: Config, args) -> int:
    from dataclasses import asdict
    from . import gitrepos
    from .repoops import inventory

    if args.git_cmd == "list":
        inventory_errors = []
        records = inventory(
            cfg.git,
            github_cache=os.path.join(cfg.state_dir, "github-repositories.json"),
            resolve_github=True,
            errors=inventory_errors,
        )
        if args.json:
            json.dump({
                "version": 1,
                "repositories": records,
                "errors": inventory_errors,
            }, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            for record in records:
                marker = "*" if record.get("dirty") else " "
                print("%s %-36s %-24s %s" % (
                    marker,
                    record.get("relative_path") or record["logical_path"],
                    record.get("branch") or "(detached)",
                    record["identity"],
                ))
            for error in inventory_errors:
                print("! %-36s %s" % (
                    error.get("logical_path", "(unknown)"),
                    error.get("error", "inventory failed"),
                ), file=sys.stderr)
        return 0
    if args.git_cmd == "status":
        roots = (
            ("inbox", os.path.join(cfg.state_dir, "inbox", "git")),
            ("conflicts", os.path.join(cfg.state_dir, "conflicts", "git")),
            ("accepted", os.path.join(cfg.state_dir, "git", "accepted")),
        )
        for label, root in roots:
            count = 0
            if os.path.isdir(root):
                count = sum(len(files) for _path, _dirs, files in os.walk(root))
            print("%-10s %d  %s" % (label + ":", count, root))
        return 0
    if args.git_cmd == "show":
        snapshot = gitrepos.read_wip_snapshot(args.snapshot)
        print(json.dumps(snapshot.manifest, sort_keys=True, indent=2))
        return 0
    if args.git_cmd == "snapshot":
        profile = cfg.git.for_repository(
            gitrepos.derive_stable_identity(args.repo)
        )
        snapshot = gitrepos.create_wip_snapshot(
            args.repo,
            args.output,
            max_file_bytes=profile.max_file_bytes,
            max_snapshot_bytes=profile.max_snapshot_bytes,
            include_staged=profile.staged,
            include_unstaged=profile.unstaged,
            include_untracked=profile.untracked,
            include_ignored=profile.ignored,
        )
        print(json.dumps({
            "ok": True,
            "snapshot_id": snapshot.snapshot_id,
            "output": os.path.abspath(args.output),
        }, sort_keys=True))
        return 0
    if args.git_cmd == "apply":
        result = gitrepos.apply_wip_snapshot(
            args.repo, args.snapshot, prior_snapshot=args.prior
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.git_cmd == "accept":
        result = gitrepos.accept_resolution(
            args.repo,
            args.branch,
            args.resolution,
            expected_old=args.expected_old,
        )
        print(json.dumps({"ok": True, **asdict(result)}, sort_keys=True))
        return 0
    if args.git_cmd == "recover":
        result = gitrepos.recover_wip_journal(args.repo, args.journal)
        print(json.dumps(result, sort_keys=True))
        return 0
    return 1


def export_git_wip(cfg: Config, repo_path: str) -> int:
    from . import gitrepos

    os.makedirs(os.path.join(cfg.state_dir, "tmp"), exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix="git-wip-", suffix=".zip", dir=os.path.join(cfg.state_dir, "tmp")
    )
    os.close(fd)
    try:
        profile = cfg.git.for_repository(
            gitrepos.derive_stable_identity(repo_path)
        )
        gitrepos.create_wip_snapshot(
            repo_path,
            temporary,
            max_file_bytes=profile.max_file_bytes,
            max_snapshot_bytes=profile.max_snapshot_bytes,
            include_staged=profile.staged,
            include_unstaged=profile.unstaged,
            include_untracked=profile.untracked,
            include_ignored=profile.ignored,
        )
        with open(temporary, "rb") as stream:
            shutil.copyfileobj(stream, sys.stdout.buffer)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return 0


def import_git_wip(cfg: Config, repo_path: str, peer: str,
                   should_apply: bool) -> int:
    from . import gitrepos, store

    os.makedirs(os.path.join(cfg.state_dir, "tmp"), exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix="git-wip-incoming-", suffix=".zip",
        dir=os.path.join(cfg.state_dir, "tmp"),
    )
    total = 0
    receive_limit = max(
        [cfg.git.max_snapshot_bytes]
        + [
            item.max_snapshot_bytes
            for item in cfg.git.repositories
            if item.max_snapshot_bytes is not None
        ]
    )
    try:
        with os.fdopen(fd, "wb") as output:
            while True:
                chunk = sys.stdin.buffer.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > receive_limit:
                    raise gitrepos.SnapshotError("incoming WIP exceeds configured limit")
                output.write(chunk)
        snapshot = gitrepos.read_wip_snapshot(temporary)
        repository = snapshot.manifest["repository"]
        identity = str(repository["identity"])
        profile = cfg.git.for_repository(identity)
        should_apply = bool(should_apply and profile.enabled and profile.auto_apply)
        branch = str(repository.get("branch") or "detached")
        directory = store.inbox_path(
            "git", peer, identity + ":" + branch, snapshot.snapshot_id,
            state_dir=cfg.state_dir,
        )
        os.makedirs(directory, exist_ok=True)
        archive = os.path.join(directory, "snapshot.zip")
        if os.path.exists(archive):
            os.unlink(temporary)
        else:
            os.replace(temporary, archive)

        accepted_dir = os.path.join(
            cfg.state_dir, "git", "accepted", store.slug(peer),
        )
        accepted_path = os.path.join(
            accepted_dir, store.slug(identity + ":" + branch) + ".json"
        )
        accepted = store.read_json(accepted_path, default={})
        prior_path = accepted.get("snapshot") if isinstance(accepted, dict) else None
        result = {
            "ok": True,
            "quarantined": not should_apply,
            "snapshot_id": snapshot.snapshot_id,
            "archive": archive,
        }
        if should_apply:
            try:
                applied = gitrepos.apply_wip_snapshot(
                    repo_path,
                    archive,
                    prior_snapshot=prior_path if prior_path and os.path.exists(prior_path)
                    else None,
                )
                result.update(applied)
                result["quarantined"] = False
                store.write_json(accepted_path, {
                    "snapshot_id": snapshot.snapshot_id,
                    "snapshot": archive,
                    "peer": peer,
                    "identity": identity,
                    "branch": branch,
                    "accepted_at": int(time.time()),
                })
            except gitrepos.UnsafeSnapshotError as exc:
                conflict = store.record_conflict(
                    "git", peer, identity + ":" + branch,
                    {
                        "reason": str(exc),
                        "snapshot": archive,
                        "repository": repo_path,
                    },
                    state_dir=cfg.state_dir,
                )
                result.update({
                    "ok": False,
                    "quarantined": True,
                    "reason": str(exc),
                    "conflict": conflict,
                })
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_preferences_command(cfg: Config, args) -> int:
    from .preferenceops import snapshot_preferences

    if args.preference_cmd == "list":
        snapshot = snapshot_preferences(cfg)
        if args.json:
            json.dump(snapshot, sys.stdout, sort_keys=True, indent=2)
            sys.stdout.write("\n")
        else:
            entries = snapshot["manifest"]["entries"]
            for key, entry in sorted(entries.items()):
                print("%-48s %8d  %s" % (
                    key, entry["size"], entry["adapter"]
                ))
            for blocked in snapshot["manifest"]["blocked"]:
                print("BLOCKED %-40s %s" % (
                    blocked.get("path", ""), blocked.get("reason", "")
                ))
        return 0
    if args.preference_cmd == "conflicts":
        root = os.path.join(cfg.state_dir, "inbox", "preferences")
        found = []
        if os.path.isdir(root):
            for directory, _dirs, files in os.walk(root):
                for filename in files:
                    if filename.endswith(".incoming"):
                        found.append(os.path.join(directory, filename))
        for path in sorted(found):
            print(path)
        return 0
    return 1


def doctor(cfg: Config) -> int:
    from .util import remote_chatmesh_args, remote_home, ssh_out
    from .config import REMOTE_REPO
    ok = True
    print("chatmesh %s on %s" % (VERSION, os.uname().nodename))
    print("config: %s" % default_config_path())
    print("state:  %s" % cfg.state_dir)
    db = cursordb.global_db_path()
    real = os.path.realpath(db)
    print("cursor DB: %s%s -> %s" % (
        db, " (symlink)" if os.path.islink(db) else "", real))
    print("  exists: %s" % os.path.exists(real))
    for tree in filetrees.TREES:
        r = filetrees.tree_root(tree)
        print("%s: %s (exists: %s)" % (tree, r, os.path.isdir(r)))
    local_repositories = []
    local_inventory_errors = []
    if cfg.git.enabled:
        from .repoops import inventory
        for root in cfg.git.roots:
            print("git root: %s%s -> %s (exists: %s)" % (
                root,
                " (symlink)" if os.path.islink(root) else "",
                os.path.realpath(root),
                os.path.isdir(root),
            ))
        try:
            local_repositories = inventory(
                cfg.git,
                github_cache=os.path.join(
                    cfg.state_dir, "github-repositories.json"
                ),
                resolve_github=True,
                errors=local_inventory_errors,
            )
            print("git repositories: %d (%d dirty)" % (
                len(local_repositories),
                sum(1 for item in local_repositories if item["dirty"]),
            ))
            for error in local_inventory_errors:
                ok = False
                print("!! Git repository unavailable: %s: %s" % (
                    error.get("logical_path", "(unknown)"),
                    error.get("error", "inventory failed"),
                ))
        except Exception as exc:
            ok = False
            print("!! local Git inventory failed: %s" % exc)
    if cfg.preferences.enabled:
        from .preferenceops import snapshot_preferences
        try:
            preference_snapshot = snapshot_preferences(cfg)
            print("preferences: %d safe, %d blocked" % (
                len(preference_snapshot["manifest"]["entries"]),
                len(preference_snapshot["manifest"]["blocked"]),
            ))
        except Exception as exc:
            ok = False
            print("!! preference inventory failed: %s" % exc)
    if not cfg.peers:
        print("!! no peers configured — run: chatmesh init")
        return 1
    for peer in cfg.peers:
        try:
            rh = remote_home(peer)
            print("peer %s: home=%s" % (peer, rh))
            rv = ssh_out(
                peer,
                'PYTHONPATH="$HOME/%s" python3 -c '
                '"import chatmesh,sys;sys.stdout.write(chatmesh.VERSION)" '
                '2>/dev/null || echo none' % REMOTE_REPO).strip()
            print("  chatmesh on peer: %s" % rv)
            validation = ssh_out(
                peer, remote_chatmesh_args(["config", "validate"])
            ).strip()
            print("  peer config: %s" % validation)
            rdb = ssh_out(
                peer,
                'python3 -c "import os;p=os.path.expanduser(\'~/%s\');'
                'print(os.path.exists(os.path.realpath(p)))"'
                % cursordb.GLOBAL_DB_REL).strip()
            print("  peer cursor DB reachable: %s" % rdb)
            if cfg.git.enabled:
                from .syncplan import match_repositories
                remote_manifest = json.loads(ssh_out(
                    peer, remote_chatmesh_args(["git-manifest"]), timeout=1800
                ))
                for error in remote_manifest.get("errors", []):
                    ok = False
                    print("  !! peer Git repository unavailable: %s: %s" % (
                        error.get("logical_path", "(unknown)"),
                        error.get("error", "inventory failed"),
                    ))
                pairs, local_only, remote_only, ambiguous = match_repositories(
                    local_repositories,
                    remote_manifest.get("repositories", []),
                )
                print(
                    "  Git topology: matched=%d local-only=%d "
                    "peer-only=%d ambiguous=%d"
                    % (
                        len(pairs), len(local_only), len(remote_only),
                        len(ambiguous),
                    )
                )
                if ambiguous:
                    ok = False
            if cfg.preferences.enabled:
                remote_preferences = json.loads(ssh_out(
                    peer, remote_chatmesh_args(["preferences-export"]),
                    timeout=900,
                ))
                print("  peer preferences: %d safe, %d blocked" % (
                    len(remote_preferences["manifest"]["entries"]),
                    len(remote_preferences["manifest"]["blocked"]),
                ))
        except Exception as e:
            ok = False
            print("peer %s: ERROR %s" % (peer, e))
    return 0 if ok else 1
