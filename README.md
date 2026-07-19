# chatmesh

Chatmesh synchronizes agent chats, Git repositories and exact work-in-progress,
curated AI-tool preferences, and declarative machine environments between Macs
with different home paths. The machine running `chatmesh sync` is the hub and
drives both SSH directions; a peer never needs SSH access back to the hub.

## What syncs

- Cursor IDE composers are merged row-by-row in `state.vscdb`; workspace IDs
  and home paths are remapped without replacing the database.
- Cursor CLI, Claude Code, and Codex sessions retain the existing guarded
  file-tree/history merge behavior.
- Every Git repository below the configured roots is discovered recursively,
  including nested repositories, submodules, bare repositories, linked
  worktrees, and owner-directory symlinks.
- Local branches, tags, upstream metadata, worktree branches, unpushed commits,
  and configured WIP categories are synchronized with Git plumbing.
- Curated Cursor, Claude, Codex, and custom user preference paths use hash-based
  three-way merge rather than mtime.
- Homebrew declarations, user-level pip packages, pipx/uv tools, Python runtime
  compatibility, and restorable project venv declarations can be inventoried
  and synchronized additively.

Project `.cursor`, `.claude`, `.codex`, `.agents`, `AGENTS.md`, and
`CLAUDE.md` files belong to the Git WIP snapshot. Preference adapters only own
user-level paths.

## Safety model

- Git objects arrive through Git's smart protocol under
  `refs/chatmesh/incoming/...`; `.git`, indexes, refs, and worktree metadata are
  never copied as files.
- A clean checkout advances only through `git merge --ff-only`; an unmounted
  branch uses compare-and-swap `git update-ref`.
- Diverged histories do not move either original branch. Chatmesh creates a
  `mhadi/chore/chatmesh-resolve-*` branch and worktree under `.worktrees/` with
  a reviewable merge or normal conflict state. `chatmesh git accept` verifies
  that the committed resolution includes the incoming history.
- WIP archives preserve staged binary patches, unstaged and untracked bytes,
  deletions, executable modes, and safe relative symlinks. Apply requires the
  same repository identity, branch, HEAD, and a clean destination or an exact
  previously accepted Chatmesh snapshot.
- Active Git operations, locks, unmerged indexes, oversized payloads, symlink
  escapes, path traversal, and concurrent edits are quarantined. Unrecognized
  live content is never overwritten.
- Every accepted WIP apply is backed up and journaled; preference writes are
  backed up and atomically replaced. Recovery commands are fail-closed.
- Preference inventory excludes credentials, token/auth stores, keychains,
  caches, downloaded plugins/runtimes, vendor trees, managed skills, project
  trust/state, and literal secret values. MCP secrets must be environment
  references.
- Cursor's narrowly allow-listed global user-rule value is read or written only
  while Cursor is closed; no whole settings row is copied.
- Environment sync never copies Homebrew prefixes, Python interpreters,
  `site-packages`, or venv directories. It never uninstalls, force-upgrades, or
  downgrades packages. Existing venvs are never replaced; a missing venv may be
  created only from an unchanged flat, fully pinned requirements file. `uv`
  tool installations sync additively; project `uv.lock` recreation remains
  manual because local/workspace dependency closure cannot be proven safely.

## Configuration

`~/.config/chatmesh/config.toml` is the only user configuration source.
`CHATMESH_HOME` and `CHATMESH_ASSUME_CLOSED` exist only for fixtures. Python
3.9 and 3.10 use Chatmesh's bundled TOML fallback.

```toml
version = 1

[mesh]
peers = ["mhadi-mini"]
apps = ["cursor", "cursor-cli", "claude", "codex"]
directions = ["pull", "push"]
interval = 3600
file_guard_minutes = 15
process_gate_apps = ["cursor", "cursor-cli"]
sync_checkpoints = false
max_composers_per_run = 0
log_level = "INFO"
state_dir = "~/.local/state/chatmesh"

[git]
enabled = true
roots = ["~/Documents/GitHub"]
branches = true
tags = true
worktrees = true
clone_missing = true
relocate = true
staged = true
unstaged = true
untracked = true
ignored = false
auto_apply = true
max_file_bytes = 52428800
max_snapshot_bytes = 1073741824
conflict_policy = "quarantine"

[[git.repositories]]
identity = "github-id:R_example"
auto_apply = false

[preferences]
enabled = true
cursor = true
claude = true
codex = true
conflict_policy = "quarantine"
max_file_bytes = 10485760
max_total_bytes = 104857600
exclude = []

[[preferences.custom_paths]]
name = "future-tool"
path = "~/.future-tool/preferences"
kind = "tree"
rewrite_home = true
exclude = ["cache/**"]

[environment]
enabled = false
homebrew = true
brewfile = "~/Brewfile"
python = true
pip = true
pipx = true
uv = true
venvs = true
auto_apply = false
roots = ["~/Documents/GitHub"]
exclude = []
max_lock_file_bytes = 10485760
conflict_policy = "quarantine"
```

Repository overrides are matched by the stable identity shown by
`chatmesh git list --json`. GitHub's current owner/name controls canonical
`<root>/<owner>/<repo>` paths. Symlinked owner roots remain symlinked.

## Setup and rollout

Peers are SSH aliases from `~/.ssh/config`. Initialize and validate both
machines before enabling scheduled writes:

```sh
bin/chatmesh init
bin/chatmesh config validate
bin/chatmesh deploy --peer mhadi-mini
bin/chatmesh doctor
bin/chatmesh sync --dry-run
bin/chatmesh install
```

The explicit one-time old-config migration archives the env file:

```sh
bin/chatmesh config migrate --from ~/.config/chatmesh/env
```

A dry run does not deploy code, update sync state, cache GitHub resolution, or
write repositories, preferences, packages, tools, or venvs. Deploy the same
version first.

## Operations

```sh
bin/chatmesh status
bin/chatmesh sync --app git --peer mhadi-mini --dry-run
bin/chatmesh git list
bin/chatmesh git status
bin/chatmesh git show --snapshot /path/to/snapshot.zip
bin/chatmesh git accept --repo /path/to/repo --branch dev \
  --resolution mhadi/chore/chatmesh-resolve-example
bin/chatmesh git recover --repo /path/to/repo --journal /path/to/journal.json
bin/chatmesh preferences list
bin/chatmesh preferences conflicts
bin/chatmesh environment list
bin/chatmesh environment plan --peer mhadi-mini
bin/chatmesh sync --app environment --peer mhadi-mini --dry-run
bin/chatmesh environment apply --peer mhadi-mini --direction pull
bin/chatmesh environment pending
```

Logs, backups, inboxes, baselines, and conflict records live below
`~/.local/state/chatmesh/` unless `mesh.state_dir` changes it. Chatmesh never
pushes to GitHub and never force-updates a live branch or tag.
