# chatmesh

Your conversations should belong to you—not to whichever AI tool or Mac
happened to start them.

Chatmesh is a local-first conversation fabric for agentic development. It
meshes chats across Claude Code, Codex, Cursor IDE, and Cursor CLI; carries the
mesh between Macs; and keeps the skills, rules, instructions, repositories, and
environment needed to continue the work attached to it.

The machine running `chatmesh sync` is the hub. It drives both SSH directions,
so peers never need SSH access back to the hub and no hosted Chatmesh service
sees your conversations.

## The mesh

Chatmesh has two complementary dimensions:

1. **Across tools on one machine.** When the optional agent mesh is enabled,
   human-visible Claude Code messages appear as a resumable Codex session and
   Codex messages appear as a resumable Claude Code session. Skills, rules, and
   instruction files are routed into each configured agent's native layout.
2. **Across machines.** Native and projected sessions, canonical skills, Git
   work, preferences, and safe environment declarations converge through the
   existing hub-and-peer SSH mesh.

That means a conversation can start in Claude on one Mac, become visible in
Codex, move to another Mac, and retain the repository and agent resources
needed to continue it.

### What meshes

- **Claude ↔ Codex messages:** user and assistant text is projected into a
  separate native session for the other tool.
- **Cursor IDE composers:** rows are merged in `state.vscdb`; workspace IDs and
  home paths are remapped without replacing the database.
- **Cursor CLI, Claude, and Codex histories:** guarded native stores converge
  across computers.
- **Skills with companion files:** one canonical `~/.agents/skills` tree is
  linked into Claude, Codex, Cursor, OpenCode, Gemini, Antigravity, Copilot, or
  Windsurf layouts selected in configuration.
- **Rules and instructions:** a `chatmesh.json` or existing `vibelink.json`
  manifest can route repo-level skills, Claude/Cursor rules, and
  `CLAUDE.md`/`AGENTS.md`/`GEMINI.md`/`.cursorrules`.
- **Git context:** repositories, branches, tags, worktrees, unpushed commits,
  and exact staged, unstaged, and untracked work.
- **Curated preferences:** safe Cursor, Claude, Codex, and custom user paths
  use content-aware three-way merge.
- **Development environment:** Homebrew declarations, user pip packages,
  pipx/uv tools, Python compatibility, and safely restorable project venvs.

Project `.cursor`, `.claude`, `.codex`, `.agents`, `AGENTS.md`, and
`CLAUDE.md` files remain part of the Git WIP snapshot. Preference adapters own
only user-level paths.

## Cross-agent message safety

Claude and Codex session files are private implementation formats, so Chatmesh
does not splice records into a live conversation. It creates a distinct,
deterministically identified projection:

- only human-visible user and assistant text crosses the tool boundary;
- reasoning, thinking, tool calls, tool output, attachments, and token metadata
  are omitted;
- sessions modified inside `mesh.file_guard_minutes` are not read or written;
- every projected record carries a Chatmesh origin marker, preventing
  Claude → Codex → Claude echo loops;
- an unchanged projection can be refreshed as its source conversation grows;
- once a user continues or edits the imported session, Chatmesh preserves it
  and quarantines later source changes instead of overwriting them;
- session size is bounded by `agent_mesh.max_session_bytes`.

The original native session is always retained.

## Skills, rules, and instructions

The resource mesh incorporates the useful parts of the formerly standalone
Vibelink tool into Chatmesh:

- a curated registry of agent resource layouts;
- one canonical source with relative links into agent-specific locations;
- deterministic planning and drift checks;
- repo manifests;
- agent filtering;
- compatibility with existing `vibelink.json` files.

Chatmesh additionally meshes complete skill directories—including
`references/`, `assets/`, and scripts—and carries the canonical skill tree
between computers. Different live resources with the same name are never
silently replaced: they are recorded as conflicts under the Chatmesh state
directory.

Example repo manifest:

```json
{
  "$schema": 1,
  "skills": [
    "./.claude/skills/review"
  ],
  "rules": [
    "./.cursor/rules/api.mdc"
  ],
  "instructions": "./AGENTS.md",
  "agents": ["claude", "codex", "cursor"]
}
```

Name it `chatmesh.json`. Existing `vibelink.json` manifests work unchanged.
Paths must stay inside the manifest's repository.

Useful commands:

```sh
bin/chatmesh mesh agents
bin/chatmesh mesh run --dry-run
bin/chatmesh mesh run
bin/chatmesh mesh doctor
```

The installed LaunchAgent invokes the same mesh automatically on Chatmesh's
configured interval, so a separate file watcher is unnecessary.

## Configuration

`~/.config/chatmesh/config.toml` is the only user configuration source.
`CHATMESH_HOME` and `CHATMESH_ASSUME_CLOSED` exist only for fixtures. Python
3.9 and 3.10 use Chatmesh's bundled TOML fallback.

The cross-agent mesh is opt-in:

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

[agent_mesh]
enabled = true
messages = true
skills = true
rules = true
instructions = true
resource_agents = ["claude", "codex", "cursor"]
roots = ["~/Documents/GitHub"]
max_session_bytes = 52428800
conflict_policy = "quarantine"

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

[preferences]
enabled = true
cursor = true
claude = true
codex = true
conflict_policy = "quarantine"
max_file_bytes = 10485760
max_total_bytes = 104857600
exclude = []

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

Repository overrides and custom preference paths remain available; run
`bin/chatmesh config show` for the complete normalized document.

## Git safety model

- Git objects arrive through Git's smart protocol under
  `refs/chatmesh/incoming/...`; `.git`, indexes, refs, and worktree metadata are
  never copied as files.
- Clean checkouts advance only through `git merge --ff-only`; unmounted
  branches use compare-and-swap `git update-ref`.
- Diverged histories do not move either original branch. Chatmesh creates a
  `mhadi/chore/chatmesh-resolve-*` branch and isolated worktree for review.
- WIP archives preserve staged binary patches, unstaged and untracked bytes,
  deletions, executable modes, and safe relative symlinks.
- Apply requires the same repository identity, branch, HEAD, and a clean
  destination or an exact previously accepted Chatmesh snapshot.
- Active Git operations, locks, unmerged indexes, oversized payloads, symlink
  escapes, path traversal, concurrent edits, and ambiguous duplicate clones
  fail closed.
- Every accepted WIP apply is backed up and journaled.
- Chatmesh never pushes to GitHub and never force-updates a live branch or tag.

## Preference and environment safety

- Preference inventory excludes credentials, auth stores, keychains, caches,
  downloaded runtimes, vendor trees, managed skills, project trust/state, and
  literal secret values. MCP secrets must use environment references.
- Cursor's allow-listed global user-rule value is read or written only while
  Cursor is closed; the containing settings row is never copied wholesale.
- Environment sync never copies Homebrew prefixes, Python interpreters,
  `site-packages`, or venv directories.
- It never uninstalls, force-upgrades, or downgrades packages.
- Existing venvs are never replaced. A missing venv can be created only from
  an unchanged, flat, fully pinned requirements file.

## Setup

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

A dry run does not deploy code, update state, rewrite conversations, create
resource links, cache GitHub resolution, or change repositories, preferences,
packages, tools, or venvs. Deploy the same version to each peer first.

The explicit one-time old-config migration archives the legacy env file:

```sh
bin/chatmesh config migrate --from ~/.config/chatmesh/env
```

## Operations

```sh
bin/chatmesh status
bin/chatmesh sync --app agent-mesh --peer mhadi-mini --dry-run
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
bin/chatmesh environment pending
```

Logs, backups, inboxes, baselines, projections, and conflict records live below
`~/.local/state/chatmesh/` unless `mesh.state_dir` changes it.

## Development

Chatmesh is a dependency-free Python CLI:

```sh
python3 -m unittest discover -s tests -v
bin/chatmesh --help
```
