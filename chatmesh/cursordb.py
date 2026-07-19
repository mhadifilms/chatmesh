"""Row-level merge of Cursor IDE chats via the global state.vscdb.

Model (verified against Cursor 2.x):
  * ``composerHeaders`` is the authoritative chat index (per-workspace chat
    lists come from it; workspace state.vscdb no longer holds composer lists).
  * Conversation content lives in ``cursorDiskKV`` under per-composer keys:
    composerData:<cid>, bubbleId:<cid>:<bid>, messageRequestContext:<cid>:<bid>,
    codeBlockDiff:<cid>:<id>, codeBlockPartialInlineDiffFates:<cid>:<id>,
    ofsContent:<cid>:file://<path>   (path inside the KEY — keys get rewritten).
  * Checkpoint/restore state (checkpointId:*, agentKv:*) is a content-addressed
    store measured in tens of GB; excluded unless mesh.sync_checkpoints=true
    (v0.1 excludes the agentKv blob CAS entirely).
  * Cursor matches workspaceStorage dirs to folders by the URI in
    workspace.json (it does not re-hash paths), so foreign workspace ids work
    on the destination once a stub workspace.json exists.

Sync direction is decided by ``composerHeaders.lastUpdatedAt`` — newer side
wins a whole composer. Rows are only ever inserted/replaced, never deleted;
every replaced row is appended to a base64 JSONL backup first.
"""

from __future__ import annotations

import base64
import glob
import gzip
import json
import os
import sqlite3
import sys
from typing import Dict, List, Optional, Tuple

from .rewrite import HomeRewriter
from .util import app_running_local, log
from .util import home as _home

GLOBAL_DB_REL = "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
WS_DIR_REL = "Library/Application Support/Cursor/User/workspaceStorage"

BASE_FAMILIES = [
    "bubbleId:{cid}:",
    "messageRequestContext:{cid}:",
    "codeBlockDiff:{cid}:",
    "codeBlockPartialInlineDiffFates:{cid}:",
    "ofsContent:{cid}:",
]
CHECKPOINT_FAMILIES = [
    "checkpointId:{cid}:",
    "agentKv:bubbleCheckpoint:{cid}:",
]
HEADER_COLS = ["composerId", "workspaceId", "createdAt", "lastUpdatedAt",
               "isArchived", "isSubagent", "recency", "checkpointAt", "value"]
SKIP_IDS = {"empty-state-draft"}


def global_db_path(home: Optional[str] = None) -> str:
    return os.path.join(home or _home(), GLOBAL_DB_REL)


def ws_dir_path(home: Optional[str] = None) -> str:
    return os.path.join(home or _home(), WS_DIR_REL)


def connect(db: str, readonly: bool = False) -> sqlite3.Connection:
    real = os.path.realpath(db)  # mini symlinks the DB onto an external volume
    if readonly:
        con = sqlite3.connect("file:%s?mode=ro" % real.replace(" ", "%20"),
                              uri=True, timeout=60)
    else:
        con = sqlite3.connect(real, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    return con


def workspace_map(home: Optional[str] = None) -> Dict[str, str]:
    """workspaceId -> folder URI (from workspace.json files)."""
    out = {}
    for wj in glob.glob(os.path.join(ws_dir_path(home), "*", "workspace.json")):
        try:
            with open(wj) as f:
                d = json.load(f)
        except Exception:
            continue
        folder = d.get("folder")
        if isinstance(folder, str):
            out[os.path.basename(os.path.dirname(wj))] = folder
    return out


def read_index(db: Optional[str] = None) -> Dict[str, dict]:
    """composerId -> {lastUpdatedAt, workspaceId}. Empty dict if no DB."""
    path = db or global_db_path()
    if not os.path.exists(os.path.realpath(path)):
        return {}
    con = connect(path, readonly=True)
    try:
        rows = con.execute(
            "SELECT composerId, workspaceId, lastUpdatedAt FROM composerHeaders"
        ).fetchall()
    finally:
        con.close()
    return {r[0]: {"ws": r[1], "u": r[2] or 0} for r in rows
            if r[0] not in SKIP_IDS}


# --------------------------------------------------------------------------- #
# Export (runs on the SOURCE machine; stdout = gzip JSONL stream)
# --------------------------------------------------------------------------- #
def cmd_export_index() -> None:
    h = _home()
    idx = read_index()
    json.dump({
        "v": 1,
        "home": h,
        "cursorRunning": app_running_local("cursor"),
        "ws": workspace_map(),
        "composers": idx,
    }, sys.stdout)


def _families(include_checkpoints: bool) -> List[str]:
    fams = list(BASE_FAMILIES)
    if include_checkpoints:
        fams += CHECKPOINT_FAMILIES
    return fams


def cmd_export_rows(include_checkpoints: bool) -> None:
    """Read composer ids (JSON list) on stdin, stream their rows to stdout."""
    ids = json.load(sys.stdin)
    con = connect(global_db_path(), readonly=True)
    out = gzip.GzipFile(fileobj=sys.stdout.buffer, mode="wb", compresslevel=6)

    def emit(obj):
        out.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())

    emit({"t": "meta", "home": _home(), "ws": workspace_map(),
          "ckpt": bool(include_checkpoints)})
    try:
        for cid in ids:
            if cid in SKIP_IDS:
                continue
            con.execute("BEGIN DEFERRED")
            try:
                hdr = con.execute(
                    "SELECT %s FROM composerHeaders WHERE composerId=?"
                    % ",".join(HEADER_COLS), (cid,)).fetchone()
                if hdr is None:
                    continue
                keys = [("composerData:%s" % cid, None)]
                for fam in _families(include_checkpoints):
                    p = fam.format(cid=cid)
                    keys.append((p, p + "￿"))
                if include_checkpoints:
                    keys.append(("agentKv:checkpoint:%s" % cid, None))
                for lo, hi in keys:
                    if hi is None:
                        rows = con.execute(
                            "SELECT key, value FROM cursorDiskKV WHERE key=?",
                            (lo,)).fetchall()
                    else:
                        rows = con.execute(
                            "SELECT key, value FROM cursorDiskKV "
                            "WHERE key>=? AND key<?", (lo, hi)).fetchall()
                    for k, v in rows:
                        if v is None:
                            continue
                        if isinstance(v, str):
                            y, b = "t", v.encode("utf-8", "surrogatepass")
                        else:
                            y, b = "b", bytes(v)
                        emit({"t": "row", "k": k, "y": y,
                              "d": base64.b64encode(b).decode()})
                hcols = dict(zip(HEADER_COLS, hdr))
                emit({"t": "end", "id": cid, "hdr": hcols})
            finally:
                con.execute("COMMIT")
    finally:
        out.close()
        con.close()


# --------------------------------------------------------------------------- #
# Apply (runs on the DESTINATION machine; stdin = gzip JSONL stream)
# --------------------------------------------------------------------------- #
def _load_ws_remap(src_ws: Dict[str, str], rw: HomeRewriter) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Return (ws_id_map src->dst, stubs_needed src_id->rewritten_folder_uri)."""
    local_by_folder = {v: k for k, v in workspace_map().items()}
    id_map, stubs = {}, {}
    for sid, folder in src_ws.items():
        rew, _ = rw.text(folder)
        lid = local_by_folder.get(rew)
        if lid:
            id_map[sid] = lid
        else:
            id_map[sid] = sid
            stubs[sid] = rew
    return id_map, stubs


def _ensure_ws_stub(sid: str, folder_uri: str) -> None:
    d = os.path.join(ws_dir_path(), sid)
    wj = os.path.join(d, "workspace.json")
    if os.path.exists(wj):
        return
    os.makedirs(d, exist_ok=True)
    with open(wj, "w") as f:
        json.dump({"folder": folder_uri}, f)
    log.info("created workspace stub %s -> %s", sid, folder_uri)


def cmd_apply(src_home_hint: Optional[str], backup_path: str) -> None:
    if app_running_local("cursor"):
        print(json.dumps({"ok": False, "err": "cursor-running"}))
        return
    dst_home = _home()
    stream = gzip.GzipFile(fileobj=sys.stdin.buffer, mode="rb")
    meta = json.loads(stream.readline())
    assert meta.get("t") == "meta"
    src_home = meta.get("home") or src_home_hint
    rw = HomeRewriter(src_home, dst_home)
    id_map, stubs = _load_ws_remap(meta.get("ws", {}), rw)
    ws_subs = [(s, d) for s, d in id_map.items() if s != d]

    def remap_ws_ids(text: str) -> str:
        for s, d in ws_subs:
            if s in text:
                text = text.replace(s, d)
        return text

    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    bak = open(backup_path, "a")
    db = global_db_path()
    con = connect(db)
    applied = rows_written = 0
    pending: List[Tuple[str, str, bytes]] = []
    try:
        for raw in stream:
            rec = json.loads(raw)
            if rec["t"] == "row":
                pending.append((rec["k"], rec["y"],
                                base64.b64decode(rec["d"])))
            elif rec["t"] == "end":
                cid = rec["id"]
                hdr = rec["hdr"]
                con.execute("BEGIN IMMEDIATE")
                try:
                    for k, y, b in pending:
                        nk, _ = rw.text(k)
                        if y == "t":
                            txt = b.decode("utf-8", "surrogatepass")
                            nt, _ = rw.text(txt)
                            nv = remap_ws_ids(nt)
                        else:
                            rv, _ = rw.value(b)
                            v2 = rv if rv is not None else b
                            if isinstance(v2, (bytes, bytearray)):
                                try:
                                    t2 = bytes(v2).decode("utf-8")
                                    nv = sqlite3.Binary(
                                        remap_ws_ids(t2).encode("utf-8"))
                                except UnicodeDecodeError:
                                    nv = sqlite3.Binary(bytes(v2))
                            else:
                                nv = sqlite3.Binary(remap_ws_ids(v2).encode())
                        old = con.execute(
                            "SELECT value, typeof(value) FROM cursorDiskKV "
                            "WHERE key=?", (nk,)).fetchone()
                        if old is not None and old[0] is not None:
                            ob = old[0] if isinstance(old[0], (bytes, bytearray)) \
                                else str(old[0]).encode("utf-8", "surrogatepass")
                            bak.write(json.dumps({
                                "tbl": "cursorDiskKV", "key": nk,
                                "y": "b" if isinstance(old[0], (bytes, bytearray)) else "t",
                                "b64": base64.b64encode(bytes(ob)).decode()}) + "\n")
                        else:
                            bak.write(json.dumps({
                                "tbl": "cursorDiskKV", "key": nk,
                                "inserted": True}) + "\n")
                        con.execute(
                            "INSERT OR REPLACE INTO cursorDiskKV(key,value) "
                            "VALUES(?,?)", (nk, nv))
                        rows_written += 1
                    # header last: acts as the resume/commit marker
                    sid = hdr.get("workspaceId") or ""
                    did = id_map.get(sid, sid)
                    if sid in stubs:
                        _ensure_ws_stub(sid, stubs[sid])
                    hval = hdr.get("value")
                    if isinstance(hval, str):
                        hval, _ = rw.text(hval)
                        hval = remap_ws_ids(hval)
                    oldh = con.execute(
                        "SELECT %s FROM composerHeaders WHERE composerId=?"
                        % ",".join(HEADER_COLS), (cid,)).fetchone()
                    if oldh is not None:
                        bak.write(json.dumps({
                            "tbl": "composerHeaders",
                            "row": dict(zip(HEADER_COLS, oldh))}) + "\n")
                    else:
                        bak.write(json.dumps({
                            "tbl": "composerHeaders", "key": cid,
                            "inserted": True}) + "\n")
                    con.execute(
                        "INSERT OR REPLACE INTO composerHeaders(%s) "
                        "VALUES(%s)" % (",".join(HEADER_COLS),
                                        ",".join("?" * len(HEADER_COLS))),
                        (cid, did, hdr.get("createdAt"), hdr.get("lastUpdatedAt"),
                         hdr.get("isArchived"), hdr.get("isSubagent"),
                         hdr.get("recency"), hdr.get("checkpointAt"), hval))
                    con.execute("COMMIT")
                    applied += 1
                except Exception:
                    con.execute("ROLLBACK")
                    raise
                pending = []
                if applied % 50 == 0:
                    log.info("applied %d composers (%d rows)", applied, rows_written)
    finally:
        bak.close()
        con.close()
    print(json.dumps({"ok": True, "composers": applied, "rows": rows_written}))


def diff(local: Dict[str, dict], remote: Dict[str, dict]) -> Tuple[List[str], List[str]]:
    """Return (pull_ids, push_ids), newest-first."""
    pull = [c for c, m in remote.items()
            if m["u"] > local.get(c, {"u": 0})["u"]]
    push = [c for c, m in local.items()
            if m["u"] > remote.get(c, {"u": 0})["u"]]
    pull.sort(key=lambda c: remote[c]["u"], reverse=True)
    push.sort(key=lambda c: local[c]["u"], reverse=True)
    return pull, push
