# chatmesh

Multi-way sync of **agentic chat history** between Macs with different
usernames: Cursor IDE chats, Cursor CLI (`cursor-agent`) chats, Claude Code
sessions, and Codex sessions — with every absolute path, database reference,
and directory name rewritten for the destination machine's home directory.

Built from (and superseding) the old `cursor-sync` script and the
`repo-reorg` Cursor-DB migration engine. Unlike `cursor-sync`, the Cursor
database is **merged row-by-row** (chats from both machines coexist), not
replaced wholesale.

## What syncs

| App | Data | Method |
|---|---|---|
| Cursor IDE | `globalStorage/state.vscdb` composers | row-level merge, newest `lastUpdatedAt` wins per chat; workspace ids remapped, stub `workspace.json` created for unknown projects |
| Cursor CLI | `~/.cursor/chats/<md5(cwd)>/<session>/store.db` | file copy via sqlite online backup; outer dir renamed to `md5(rewritten cwd)` |
| Claude Code | `~/.claude/projects/**`, `~/.claude/history.jsonl` | per-file newer-wins with path rewrite + project dir rename; history = line union |
| Codex | `~/.codex/sessions/**`, `~/.codex/history.jsonl` | per-file newer-wins with path rewrite; history = line union |

Checkpoint/restore blobs (`agentKv:*`, `checkpointId:*` — tens of GB of file
snapshots) are excluded by default; the full conversation, diffs, and context
still sync. `CHATMESH_SYNC_CHECKPOINTS=1` includes the per-composer checkpoint
rows (the blob CAS itself is not synced in v0.1, so cross-machine "restore
checkpoint" is unsupported).

## Safety rules

- **Never deletes** anything, anywhere. Rows/files are only added or replaced.
- Every replaced DB row / file is backed up first under
  `~/.local/state/chatmesh/backups/`.
- Cursor's DB is written **only when Cursor is closed on the destination**
  (the source may be open — sqlite WAL reads are snapshot-consistent).
- Any session file modified in the last `CHATMESH_FILE_GUARD_MINUTES` (15 by
  default) on either side is left alone, so live sessions are never touched.
- A lock file prevents overlapping runs; interrupted syncs resume cleanly
  (a chat's header row is committed last, so a partial chat is re-fetched).
- Known behavior: deleting a chat on one machine does not delete it elsewhere —
  the next sync resurrects it (there are no deletion tombstones to observe).

## Topology

Hub model: the machine that runs `chatmesh sync` drives **both** directions
(pull *and* push) for each peer over ssh, so only the hub needs ssh access to
peers. With N peers configured, chats propagate transitively through the hub.
Peers get the `chatmesh` code auto-deployed to `~/.local/share/chatmesh/repo`
(re-deployed automatically on version change).

## Setup

```sh
bin/chatmesh init      # writes ~/.config/chatmesh/env — set CHATMESH_PEERS
bin/chatmesh doctor    # verify ssh, peer DBs, deployment
bin/chatmesh sync --dry-run
bin/chatmesh install   # LaunchAgent: runs at login + every CHATMESH_INTERVAL
```

Peers are **ssh host aliases** — usernames/IPs live in `~/.ssh/config`, e.g.:

```
Host my-mini
  HostName 100.x.y.z       # e.g. the peer's Tailscale IP
  User someuser
  IdentityFile ~/.ssh/id_ed25519
```

## Config (`~/.config/chatmesh/env`, overridable via environment)

| Key | Default | Meaning |
|---|---|---|
| `CHATMESH_PEERS` | — | comma-separated ssh hosts |
| `CHATMESH_APPS` | `cursor,cursor-cli,claude,codex` | what to sync |
| `CHATMESH_DIRECTIONS` | `pull,push` | limit to one direction if desired |
| `CHATMESH_INTERVAL` | `3600` | LaunchAgent period (s); syncs are no-ops when nothing changed |
| `CHATMESH_FILE_GUARD_MINUTES` | `15` | active-session guard window |
| `CHATMESH_PROCESS_GATE_APPS` | `cursor,cursor-cli` | apps that skip sync entirely while running (add `claude`/`codex` for strict gating) |
| `CHATMESH_SYNC_CHECKPOINTS` | `0` | include checkpoint rows (see above) |
| `CHATMESH_MAX_COMPOSERS_PER_RUN` | `0` | cap Cursor chats per run (0 = all) |

## Ops

```sh
bin/chatmesh status              # last results per peer/app/direction + gates
tail -f ~/.local/state/chatmesh/logs/chatmesh.log
bin/chatmesh sync --app cursor --peer my-mini
bin/chatmesh deploy --peer my-mini   # force re-push code to peer
```
