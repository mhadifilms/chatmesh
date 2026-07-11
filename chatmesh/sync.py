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
    Add them to CHATMESH_PROCESS_GATE_APPS for strict behavior.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Optional

from . import VERSION, cursordb, filetrees
from .config import Config, REMOTE_REPO, STATE_DIR
from .rewrite import HomeRewriter
from .util import (app_running_local, app_running_remote, log, remote_chatmesh_cmd,
                   remote_home, run, ssh_argv, ssh_out)

STATE_PATH = os.path.join(STATE_DIR, "state.json")
TREE_APP = {"claude-projects": "claude", "codex-sessions": "codex",
            "cursor-cli": "cursor-cli"}
HISTORY_APP = {"claude-history": "claude", "codex-history": "codex"}


def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_PATH)


def mark(state: dict, peer: str, unit: str, direction: str, **detail) -> None:
    state.setdefault(peer, {}).setdefault(unit, {})[direction] = dict(
        ts=int(time.time()), **detail)


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def backup_dir() -> str:
    d = os.path.join(STATE_DIR, "backups", time.strftime("%Y%m%d"))
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
# Peer deployment (chatmesh must exist on the peer for export/apply)
# --------------------------------------------------------------------------- #
def ensure_deployed(peer: str, force: bool = False) -> None:
    try:
        rv = ssh_out(
            peer,
            'PYTHONPATH="$HOME/%s" python3 -c '
            '"import chatmesh,sys;sys.stdout.write(chatmesh.VERSION)" '
            '2>/dev/null || true' % REMOTE_REPO).strip()
    except RuntimeError:
        rv = ""
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
                 "--backup", os.path.join(backup_dir(), "cursor-rows-%s.jsonl" % peer)],
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
                 "--src-home", rhome, "--backup", backup_dir(),
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
        log.warning("no peers configured (CHATMESH_PEERS) — nothing to do")
        return
    state = load_state()
    for peer in cfg.peers:
        if only_peer and peer != only_peer:
            continue
        try:
            rhome = remote_home(peer)
        except Exception as e:
            log.error("peer %s unreachable: %s", peer, e)
            continue
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
        save_state(state)
    log.info("sync complete")
