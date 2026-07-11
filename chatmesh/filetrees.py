"""File-tree chat stores: Claude Code projects, Codex sessions, Cursor CLI
chats, plus line-union history files.

Transfer model (hub-driven, symmetric): manifests are exchanged, only files
that are missing-or-older on the destination move, as a tar stream piped over
ssh. Per-file rules:
  * newer mtime wins; equal/older is skipped
  * files modified within the guard window on EITHER side are skipped
    (an actively-running session is never touched)
  * an overwritten file is copied into the backup dir first
  * nothing is ever deleted

Per-tree quirks:
  * claude-projects dir names encode the project path with dashes
    (-Users-<user>-...) — the first path segment is renamed between homes.
  * cursor-cli stores chats at ~/.cursor/chats/<md5(cwd)>/<session>/store.db.
    The md5 is recomputed from the rewritten cwd (extracted from store.db).
    store.db contents are content-addressed (sha256 blob ids), so file bytes
    are copied verbatim — only the outer dir name is remapped. Live DBs are
    snapshotted with sqlite's online backup before transfer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tarfile
import tempfile
import time
from typing import Dict, List, Optional, Tuple

from .config import STATE_DIR
from .rewrite import HomeRewriter
from .util import home as _home, log

TREES = {
    "claude-projects": {"root": "~/.claude/projects", "rewrite": True, "rename": "claude"},
    "codex-sessions": {"root": "~/.codex/sessions", "rewrite": True, "rename": None},
    "cursor-cli": {"root": "~/.cursor/chats", "rewrite": False, "rename": "md5cwd", "sqlite": True},
}
HISTORY_FILES = {
    "claude-history": "~/.claude/history.jsonl",
    "codex-history": "~/.codex/history.jsonl",
}
SKIP_SUFFIXES = ("-wal", "-shm", "-journal")
SKIP_NAMES = {".DS_Store"}
MTIME_SLOP = 2  # seconds; filesystems differ in mtime precision

_CWD_RE = re.compile(rb'Workspace Path: ([^\\"\n]+)')
_CWD_CACHE_PATH = os.path.join(STATE_DIR, "cwdcache.json")


def tree_root(tree: str, home: Optional[str] = None) -> str:
    rel = TREES[tree]["root"].replace("~", home or _home(), 1)
    return rel


# --------------------------------------------------------------------------- #
# cursor-cli cwd extraction (md5 dir remap)
# --------------------------------------------------------------------------- #
def _load_cwd_cache() -> dict:
    try:
        with open(_CWD_CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cwd_cache(cache: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    if len(cache) > 5000:
        cache = dict(list(cache.items())[-4000:])
    tmp = _CWD_CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, _CWD_CACHE_PATH)


def extract_cwd(store_db: str) -> Optional[str]:
    """Find the session's workspace path inside a cursor-cli store.db."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % store_db.replace(" ", "%20"),
                              uri=True, timeout=15)
        try:
            for (data,) in con.execute("SELECT data FROM blobs"):
                b = data if isinstance(data, (bytes, bytearray)) else str(data).encode()
                m = _CWD_RE.search(bytes(b))
                if m:
                    return m.group(1).decode("utf-8", "replace").strip()
        finally:
            con.close()
    except Exception:
        return None
    return None


def cwd_for_sessions(root: str, rels: List[str]) -> Dict[str, str]:
    """hashdir -> cwd for the given store.db rels, with an mtime cache."""
    cache = _load_cwd_cache()
    out, dirty = {}, False
    for rel in rels:
        hashdir = rel.split("/", 1)[0]
        if hashdir in out:
            continue
        p = os.path.join(root, rel)
        try:
            key = "%s:%d" % (p, int(os.stat(p).st_mtime))
        except OSError:
            continue
        if key in cache:
            if cache[key]:
                out[hashdir] = cache[key]
            continue
        cwd = extract_cwd(p)
        cache[key] = cwd or ""
        dirty = True
        if cwd:
            out[hashdir] = cwd
    if dirty:
        _save_cwd_cache(cache)
    return out


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #
def manifest(tree: str) -> dict:
    root = tree_root(tree)
    files: Dict[str, List[float]] = {}
    if os.path.isdir(root):
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if fn in SKIP_NAMES or fn.endswith(SKIP_SUFFIXES):
                    continue
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, root)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                files[rel] = [st.st_mtime, st.st_size]
    out = {"v": 1, "tree": tree, "home": _home(), "files": files}
    if TREES[tree].get("rename") == "md5cwd":
        out["cwds"] = cwd_for_sessions(root, list(files))
    return out


def cmd_manifest(tree: str) -> None:
    json.dump(manifest(tree), sys.stdout)


# --------------------------------------------------------------------------- #
# Rel-path normalization between homes
# --------------------------------------------------------------------------- #
def normalize_rel(tree: str, rel: str, rw: HomeRewriter,
                  cwds: Dict[str, str]) -> str:
    mode = TREES[tree].get("rename")
    if mode is None or rw.identity:
        return rel
    parts = rel.split("/")
    if mode == "claude":
        parts[0] = rw.encoded_name(parts[0])
    elif mode == "md5cwd":
        cwd = cwds.get(parts[0])
        if cwd:
            new_cwd, _ = rw.text(cwd)
            parts[0] = hashlib.md5(new_cwd.encode()).hexdigest()
    return "/".join(parts)


def plan_transfers(tree: str, src_man: dict, dst_man: dict,
                   src_home: str, dst_home: str, guard_sec: int) -> List[str]:
    """Rels (in src naming) that should move src -> dst."""
    rw = HomeRewriter(src_home, dst_home)
    cwds = src_man.get("cwds", {})
    now = time.time()
    todo = []
    for rel, (mt, _size) in sorted(src_man["files"].items()):
        if now - mt < guard_sec:
            continue  # actively-written on source
        drel = normalize_rel(tree, rel, rw, cwds)
        dst = dst_man["files"].get(drel)
        if dst is not None:
            if mt <= dst[0] + MTIME_SLOP:
                continue
            if now - dst[0] < guard_sec:
                continue  # actively-written on destination
        todo.append(rel)
    return todo


# --------------------------------------------------------------------------- #
# Send (source side): rels on stdin -> tar on stdout
# --------------------------------------------------------------------------- #
def cmd_send(tree: str) -> None:
    root = tree_root(tree)
    rels = json.load(sys.stdin)
    use_backup = TREES[tree].get("sqlite", False)
    tf = tarfile.open(fileobj=sys.stdout.buffer, mode="w|")
    tmpdir = tempfile.mkdtemp(prefix="chatmesh-send-")
    try:
        for rel in rels:
            p = os.path.join(root, rel)
            if not os.path.isfile(p):
                continue
            src = p
            if use_backup and rel.endswith(".db"):
                snap = os.path.join(tmpdir, hashlib.md5(rel.encode()).hexdigest())
                try:
                    con = sqlite3.connect(p, timeout=30)
                    dst = sqlite3.connect(snap)
                    con.backup(dst)
                    dst.close()
                    con.close()
                    os.utime(snap, (os.stat(p).st_mtime, os.stat(p).st_mtime))
                    src = snap
                except sqlite3.Error:
                    continue
            tf.add(src, arcname=rel, recursive=False)
    finally:
        tf.close()
        for fn in os.listdir(tmpdir):
            os.unlink(os.path.join(tmpdir, fn))
        os.rmdir(tmpdir)


# --------------------------------------------------------------------------- #
# Receive (destination side): tar on stdin
# --------------------------------------------------------------------------- #
def cmd_recv(tree: str, src_home: str, backup_dir: str, guard_sec: int) -> None:
    root = tree_root(tree)
    rw = HomeRewriter(src_home, _home())
    do_rewrite = TREES[tree].get("rewrite", False)
    is_md5 = TREES[tree].get("rename") == "md5cwd"
    now = time.time()
    written = skipped = 0
    tf = tarfile.open(fileobj=sys.stdin.buffer, mode="r|")
    tmp_sessions: Dict[str, str] = {}
    for member in tf:
        if not member.isfile():
            continue
        f = tf.extractfile(member)
        if f is None:
            continue
        data = f.read()
        rel = member.name
        if is_md5:
            # need the cwd from this store.db to compute the local hash dir
            hashdir = rel.split("/", 1)[0]
            if hashdir not in tmp_sessions:
                with tempfile.NamedTemporaryFile(delete=False,
                                                 prefix="chatmesh-cwd-") as t:
                    t.write(data)
                cwd = extract_cwd(t.name)
                os.unlink(t.name)
                if cwd:
                    ncwd, _ = rw.text(cwd)
                    tmp_sessions[hashdir] = hashlib.md5(ncwd.encode()).hexdigest()
                else:
                    tmp_sessions[hashdir] = hashdir
            parts = rel.split("/")
            parts[0] = tmp_sessions[hashdir]
            drel = "/".join(parts)
        else:
            drel = normalize_rel(tree, rel, rw, {})
        if do_rewrite:
            try:
                text = data.decode("utf-8")
                ntext, n = rw.text(text)
                if n:
                    data = ntext.encode("utf-8")
            except UnicodeDecodeError:
                pass
        dst = os.path.join(root, drel)
        if os.path.exists(dst):
            st = os.stat(dst)
            if member.mtime <= st.st_mtime + MTIME_SLOP or now - st.st_mtime < guard_sec:
                skipped += 1
                continue
            bpath = os.path.join(backup_dir, tree, drel)
            os.makedirs(os.path.dirname(bpath), exist_ok=True)
            with open(dst, "rb") as a, open(bpath, "wb") as b:
                b.write(a.read())
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".chatmesh-tmp"
        with open(tmp, "wb") as out:
            out.write(data)
        os.utime(tmp, (member.mtime, member.mtime))
        os.replace(tmp, dst)
        written += 1
    tf.close()
    print(json.dumps({"ok": True, "written": written, "skipped": skipped}))


# --------------------------------------------------------------------------- #
# History files: line-union merge
# --------------------------------------------------------------------------- #
def history_path(name: str, home: Optional[str] = None) -> str:
    return HISTORY_FILES[name].replace("~", home or _home(), 1)


def merge_history_lines(existing: List[str], incoming: List[str]) -> Tuple[List[str], int]:
    seen = set(existing)
    added = [ln for ln in incoming if ln and ln not in seen]
    if not added:
        return existing, 0
    merged = existing + added

    def ts(line):
        try:
            return json.loads(line).get("timestamp") or json.loads(line).get("ts") or 0
        except Exception:
            return 0

    try:
        merged.sort(key=ts)
    except Exception:
        pass
    return merged, len(added)


def cmd_merge_history(name: str, src_home: str) -> None:
    """Union lines from stdin (source naming) into the local history file."""
    rw = HomeRewriter(src_home, _home())
    incoming = []
    for ln in sys.stdin.read().splitlines():
        if ln.strip():
            out, _ = rw.text(ln)
            incoming.append(out)
    path = history_path(name)
    existing = []
    if os.path.exists(path):
        with open(path) as f:
            existing = [ln for ln in f.read().splitlines() if ln.strip()]
    merged, added = merge_history_lines(existing, incoming)
    if added:
        tmp = path + ".chatmesh-tmp"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as f:
            f.write("\n".join(merged) + "\n")
        os.replace(tmp, path)
    print(json.dumps({"ok": True, "added": added}))
