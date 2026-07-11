"""chatmesh CLI. User-facing: sync, status, doctor, install, uninstall,
deploy, init. Internal (invoked over ssh between machines): export-cursor-index,
export-cursor-rows, apply-cursor, files-manifest, files-send, files-recv,
merge-history."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import VERSION, cursordb, filetrees
from .config import CONFIG_PATH, Config, STATE_DIR
from .util import Lock, app_running_local, log, setup_logging

EXAMPLE_ENV = """\
# chatmesh configuration — machine names are ssh host aliases (~/.ssh/config)
CHATMESH_PEERS=my-mini
CHATMESH_APPS=cursor,cursor-cli,claude,codex
CHATMESH_DIRECTIONS=pull,push
CHATMESH_INTERVAL=3600
CHATMESH_FILE_GUARD_MINUTES=15
CHATMESH_PROCESS_GATE_APPS=cursor,cursor-cli
CHATMESH_SYNC_CHECKPOINTS=0
CHATMESH_MAX_COMPOSERS_PER_RUN=0
"""


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
    sub.add_parser("init", help="write example config to ~/.config/chatmesh/env")
    p = sub.add_parser("deploy", help="push chatmesh itself to a peer")
    p.add_argument("--peer", required=True)

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

    args = ap.parse_args(argv)
    cfg = Config.load()
    setup_logging(cfg.log_level)

    if args.cmd == "sync":
        from .sync import sync_all
        lock = Lock()
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
        state = load_state()
        print("chatmesh %s" % VERSION)
        print("peers: %s | apps: %s" % (",".join(cfg.peers), ",".join(cfg.apps)))
        for app in ("cursor", "cursor-cli", "claude", "codex"):
            print("  %-10s running locally: %s" % (app, app_running_local(app)))
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
        launchd.install(cfg.interval)
        return 0

    if args.cmd == "uninstall":
        from . import launchd
        launchd.uninstall()
        return 0

    if args.cmd == "init":
        if os.path.exists(CONFIG_PATH):
            print("config exists: %s" % CONFIG_PATH)
            return 1
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            f.write(EXAMPLE_ENV)
        print("wrote %s — edit peers, then: chatmesh doctor" % CONFIG_PATH)
        return 0

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
    return 1


def doctor(cfg: Config) -> int:
    from .util import remote_home, ssh_out
    from .config import REMOTE_REPO
    ok = True
    print("chatmesh %s on %s" % (VERSION, os.uname().nodename))
    db = cursordb.global_db_path()
    real = os.path.realpath(db)
    print("cursor DB: %s%s -> %s" % (
        db, " (symlink)" if os.path.islink(db) else "", real))
    print("  exists: %s" % os.path.exists(real))
    for tree in filetrees.TREES:
        r = filetrees.tree_root(tree)
        print("%s: %s (exists: %s)" % (tree, r, os.path.isdir(r)))
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
            rdb = ssh_out(
                peer,
                'python3 -c "import os;p=os.path.expanduser(\'~/%s\');'
                'print(os.path.exists(os.path.realpath(p)))"'
                % cursordb.GLOBAL_DB_REL).strip()
            print("  peer cursor DB reachable: %s" % rdb)
        except Exception as e:
            ok = False
            print("peer %s: ERROR %s" % (peer, e))
    return 0 if ok else 1
