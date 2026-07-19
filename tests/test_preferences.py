import base64
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from chatmesh import preferences


def write(path, data, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    if mode is not None:
        path.chmod(mode)
    return path


def entry(adapter, destination, data, file_format="text", mode=0o644, kind="file"):
    return {
        "adapter": adapter,
        "destination": destination,
        "kind": kind,
        "format": file_format,
        "rewrite_text": file_format in {"text", "json", "toml"},
        "sha256": preferences._sha256(data),
        "size": len(data),
        "mode": mode,
    }


def manifest(key=None, value=None):
    entries = {}
    if key is not None:
        entries[key] = value
    return {"version": 1, "entries": entries, "blocked": [], "total_size": 0}


class PreferenceInventoryTests(unittest.TestCase):
    def test_curated_inventory_and_machine_local_filtering(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            write(
                home
                / "Library/Application Support/Cursor/User/settings.json",
                json.dumps(
                    {
                        "editor.fontSize": 15,
                        "machine_id": "local-only",
                        "apiKey": "literal-secret-value",
                    }
                ),
            )
            write(
                home / ".claude/settings.json",
                json.dumps(
                    {
                        "enabledPlugins": {"formatter@example": True},
                        "trustedWorkspaces": ["/private/project"],
                    }
                ),
            )
            write(
                home / ".claude.json",
                json.dumps(
                    {
                        "mcpServers": {
                            "safe": {
                                "command": "node",
                                "env": {"API_TOKEN": "${API_TOKEN}"},
                            },
                            "literal": {
                                "command": "node",
                                "env": {"API_TOKEN": "literal-secret-value"},
                            },
                            "argument": {
                                "command": "node",
                                "args": ["--api-key", "literal-secret-value"],
                            },
                        },
                        "projects": {"/private/project": {"trusted": True}},
                    }
                ),
            )
            write(home / ".agents/skills/mine/SKILL.md", "# Mine\n")
            write(home / ".codex/skills/user/SKILL.md", "# User skill\n")
            write(home / ".codex/skills/.system/builtin/SKILL.md", "# Managed\n")
            write(
                home / "Documents/GitHub/repo/.cursor/rules/project.mdc",
                "project only",
            )

            result, payloads = preferences.scan_preferences(str(home))

            self.assertIn("cursor/ide/settings.json", result["entries"])
            cursor_settings = json.loads(payloads["cursor/ide/settings.json"])
            self.assertEqual(cursor_settings["editor.fontSize"], 15)
            self.assertNotIn("machine_id", cursor_settings)
            self.assertNotIn("apiKey", cursor_settings)

            claude_settings = json.loads(payloads["claude/settings.json"])
            self.assertEqual(
                claude_settings["enabledPlugins"], {"formatter@example": True}
            )
            self.assertNotIn("trustedWorkspaces", claude_settings)

            mcp = json.loads(payloads["claude/mcp.json"])
            self.assertEqual(
                mcp["mcpServers"]["safe"]["env"]["API_TOKEN"], "${API_TOKEN}"
            )
            self.assertNotIn(
                "API_TOKEN", mcp["mcpServers"]["literal"].get("env", {})
            )
            self.assertEqual(mcp["mcpServers"]["argument"]["args"], [])
            self.assertNotIn("projects", mcp)

            self.assertIn("agents/skills/mine/SKILL.md", result["entries"])
            self.assertIn("codex/skills/user/SKILL.md", result["entries"])
            self.assertNotIn(
                "codex/skills/.system/builtin/SKILL.md", result["entries"]
            )
            self.assertNotIn("project.mdc", "\n".join(result["entries"]))
            reasons = {item["reason"] for item in result["blocked"]}
            self.assertIn("secret_field", reasons)
            self.assertIn("machine_local_field", reasons)
            self.assertIn("managed_content", reasons)

    def test_secret_paths_and_literal_text_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            write(home / ".cursor/commands/auth-token.txt", "not uploaded")
            write(
                home / ".cursor/commands/deploy.sh",
                "API_KEY=abcdefghijklmnop\n",
                0o755,
            )

            result, payloads = preferences.scan_preferences(str(home))

            self.assertEqual(payloads, {})
            blocked = {
                (item["path"], item["reason"]) for item in result["blocked"]
            }
            self.assertIn(
                ("cursor/commands/auth-token.txt", "secret_path"), blocked
            )
            self.assertIn(
                ("cursor/commands/deploy.sh", "literal_secret"), blocked
            )

    def test_symlink_escape_is_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            outside = write(home / "outside.txt", "outside")
            skill_dir = home / ".cursor/skills/mine"
            skill_dir.mkdir(parents=True)
            os.symlink(str(outside), skill_dir / "escape")

            result, _payloads = preferences.scan_preferences(str(home))

            self.assertNotIn("cursor/skills/mine/escape", result["entries"])
            self.assertTrue(
                any(
                    item["path"] == "cursor/skills/mine/escape"
                    and item["reason"] == "symlink_escape"
                    for item in result["blocked"]
                )
            )

    def test_adapter_root_symlink_escape_is_blocked(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside_temp:
            home = Path(temporary)
            outside = Path(outside_temp)
            write(outside / "settings.json", '{"editor.fontSize": 99}')
            os.symlink(str(outside), home / ".cursor")

            result, payloads = preferences.scan_preferences(str(home))

            self.assertNotIn("cursor/cli/settings.json", payloads)
            self.assertTrue(
                any(
                    item["path"] == "cursor/cli/settings.json"
                    and item["reason"] == "adapter_root_escape"
                    for item in result["blocked"]
                )
            )

    def test_size_limit_reports_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            write(home / ".cursor/commands/large.txt", "x" * 20)
            result, _payloads = preferences.scan_preferences(
                str(home), max_file_size=10
            )
            self.assertTrue(
                any(item["reason"] == "size_limit" for item in result["blocked"])
            )

    def test_home_rewrite_only_touches_declared_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            marker = str(home).encode()
            write(
                home / ".cursor/skills/mine/SKILL.md",
                b"Use " + marker + b"/tools\n",
            )
            write(
                home / ".cursor/skills/mine/asset.bin",
                b"\x00" + marker + b"/raw\xff",
            )
            result, payloads = preferences.scan_preferences(str(home))

            _rewritten_manifest, rewritten = preferences.rewrite_snapshot(
                result, payloads, str(home), "/Users/peer"
            )

            self.assertIn(
                b"/Users/peer/tools",
                rewritten["cursor/skills/mine/SKILL.md"],
            )
            self.assertEqual(
                rewritten["cursor/skills/mine/asset.bin"],
                payloads["cursor/skills/mine/asset.bin"],
            )


class SemanticMergeTests(unittest.TestCase):
    def test_json_disjoint_changes_merge_and_same_field_conflicts(self):
        base = b'{"editor": {"fontSize": 12, "wordWrap": "off"}}'
        local = b'{"editor": {"fontSize": 14, "wordWrap": "off"}}'
        incoming = b'{"editor": {"fontSize": 12, "wordWrap": "on"}}'
        merged, conflicts = preferences.three_way_merge_json(
            base, local, incoming
        )
        self.assertEqual(conflicts, [])
        self.assertEqual(
            json.loads(merged),
            {"editor": {"fontSize": 14, "wordWrap": "on"}},
        )

        incoming_conflict = (
            b'{"editor": {"fontSize": 16, "wordWrap": "off"}}'
        )
        _candidate, conflicts = preferences.three_way_merge_json(
            base, local, incoming_conflict
        )
        self.assertEqual(conflicts, ["editor.fontSize"])

    def test_toml_recursive_conflict_and_machine_local_preservation(self):
        base = b"""
model = "gpt"
[profiles.work]
reasoning = "medium"
[projects."/tmp/repo"]
trust_level = "trusted"
"""
        local = b"""
model = "gpt"
[profiles.work]
reasoning = "high"
[projects."/tmp/repo"]
trust_level = "trusted"
"""
        incoming = b"""
model = "gpt"
[profiles.work]
reasoning = "low"
[projects."/tmp/repo"]
trust_level = "untrusted"
"""
        candidate, conflicts = preferences.three_way_merge_toml(
            base, local, incoming
        )
        self.assertEqual(conflicts, ["profiles.work.reasoning"])
        parsed = preferences.toml_loads_compatible(candidate.decode())
        self.assertEqual(parsed["projects"]["/tmp/repo"]["trust_level"], "trusted")

    def test_plan_conflict_has_inbox_payload(self):
        key = "cursor/hooks.json"
        base_data = b'{"version": 1}\n'
        local_data = b'{"version": 2}\n'
        incoming_data = b'{"version": 3}\n'
        base_entry = entry(
            "cursor-hooks-declaration",
            ".cursor/hooks.json",
            base_data,
            "json",
        )
        local_entry = entry(
            "cursor-hooks-declaration",
            ".cursor/hooks.json",
            local_data,
            "json",
        )
        incoming_entry = entry(
            "cursor-hooks-declaration",
            ".cursor/hooks.json",
            incoming_data,
            "json",
        )

        plan = preferences.plan_preferences(
            manifest(key, base_entry),
            manifest(key, local_entry),
            manifest(key, incoming_entry),
            base_payloads={key: base_data},
            local_payloads={key: local_data},
            incoming_payloads={key: incoming_data},
        )

        action = plan["actions"][0]
        self.assertEqual(action["op"], "conflict")
        self.assertEqual(
            base64.b64decode(action["inbox_payload"]), incoming_data
        )


class PreferenceApplyTests(unittest.TestCase):
    def test_executable_mode_survives_inventory_plan_and_apply(self):
        with tempfile.TemporaryDirectory() as source_temp, tempfile.TemporaryDirectory() as destination_temp:
            source = Path(source_temp)
            destination = Path(destination_temp)
            write(
                source / ".cursor/commands/run.sh",
                "#!/bin/sh\nexit 0\n",
                0o755,
            )
            incoming_manifest, incoming_payloads = preferences.scan_preferences(
                str(source)
            )
            empty = manifest()
            plan = preferences.plan_preferences(
                empty,
                empty,
                incoming_manifest,
                incoming_payloads=incoming_payloads,
            )
            result = preferences.apply_preferences(
                plan,
                str(destination),
                backup_dir=str(destination / "backups"),
                inbox_dir=str(destination / "inbox"),
                batch_id="test",
            )

            applied = destination / ".cursor/commands/run.sh"
            self.assertEqual(result["applied"], ["cursor/commands/run.sh"])
            self.assertEqual(applied.read_text(), "#!/bin/sh\nexit 0\n")
            self.assertEqual(stat.S_IMODE(applied.stat().st_mode), 0o755)

    def test_safe_relative_symlink_survives_plan_and_apply(self):
        with tempfile.TemporaryDirectory() as source_temp, tempfile.TemporaryDirectory() as destination_temp:
            source = Path(source_temp)
            destination = Path(destination_temp)
            write(source / ".cursor/skills/mine/SKILL.md", "# Mine\n")
            os.symlink("SKILL.md", source / ".cursor/skills/mine/current")
            incoming_manifest, incoming_payloads = preferences.scan_preferences(
                str(source)
            )
            empty = manifest()

            plan = preferences.plan_preferences(
                empty,
                empty,
                incoming_manifest,
                incoming_payloads=incoming_payloads,
            )
            preferences.apply_preferences(
                plan,
                str(destination),
                backup_dir=str(destination / "backups"),
                inbox_dir=str(destination / "inbox"),
                batch_id="test",
            )

            link = destination / ".cursor/skills/mine/current"
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), "SKILL.md")

    def test_conflict_does_not_overwrite_and_writes_inbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            live = write(home / ".cursor/hooks.json", '{"version": 2}\n')
            incoming = b'{"version": 3}\n'
            plan = {
                "version": 1,
                "actions": [
                    {
                        "key": "cursor/hooks.json",
                        "op": "conflict",
                        "reason": "concurrent_change",
                        "entry": entry(
                            "cursor-hooks-declaration",
                            ".cursor/hooks.json",
                            incoming,
                            "json",
                        ),
                        "conflict_paths": ["version"],
                        "inbox_payload": base64.b64encode(incoming).decode(),
                    }
                ],
            }
            result = preferences.apply_preferences(
                plan,
                str(home),
                backup_dir=str(home / "backups"),
                inbox_dir=str(home / "inbox"),
            )

            self.assertEqual(live.read_text(), '{"version": 2}\n')
            inbox = Path(result["conflicts"][0]["inbox"])
            self.assertEqual(inbox.read_bytes(), incoming)
            self.assertTrue(Path(str(inbox) + ".json").is_file())

    def test_apply_backs_up_and_preserves_local_excluded_json_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            live = write(
                home
                / "Library/Application Support/Cursor/User/settings.json",
                '{"editor.fontSize": 12, "machine_id": "keep-me"}\n',
            )
            incoming = (
                b'{\n  "editor.fontSize": 16\n}\n'
            )
            incoming_entry = entry(
                "cursor-ide-settings",
                "Library/Application Support/Cursor/User/settings.json",
                incoming,
                "json",
            )
            plan = {
                "version": 1,
                "actions": [
                    {
                        "key": "cursor/ide/settings.json",
                        "op": "apply",
                        "reason": "incoming_only_change",
                        "entry": incoming_entry,
                        "payload": base64.b64encode(incoming).decode(),
                    }
                ],
            }
            result = preferences.apply_preferences(
                plan,
                str(home),
                backup_dir=str(home / "backups"),
                inbox_dir=str(home / "inbox"),
                batch_id="test",
            )

            updated = json.loads(live.read_text())
            self.assertEqual(updated["editor.fontSize"], 16)
            self.assertEqual(updated["machine_id"], "keep-me")
            self.assertEqual(len(result["backups"]), 1)
            self.assertIn('"editor.fontSize": 12', Path(result["backups"][0]).read_text())

    def test_cursor_rule_adapter_is_closed_only_and_updates_one_key(self):
        with tempfile.TemporaryDirectory() as source_temp, tempfile.TemporaryDirectory() as destination_temp:
            source = Path(source_temp)
            destination = Path(destination_temp)
            source_db = source / preferences.CURSOR_STATE_DB
            destination_db = destination / preferences.CURSOR_STATE_DB
            for path, rule in (
                (source_db, "source rule"),
                (destination_db, "destination rule"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(str(path))
                connection.execute(
                    "CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)"
                )
                connection.execute(
                    "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
                    (preferences.CURSOR_USER_RULE_KEY, rule),
                )
                connection.execute(
                    "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
                    ("unrelated", "do not touch"),
                )
                connection.commit()
                connection.close()

            closed_manifest, closed_payloads = preferences.scan_preferences(
                str(source), cursor_closed=True
            )
            open_manifest, _ = preferences.scan_preferences(
                str(source), cursor_closed=False
            )
            self.assertIn("cursor/global-user-rule", closed_manifest["entries"])
            self.assertNotIn("cursor/global-user-rule", open_manifest["entries"])
            self.assertTrue(
                any(
                    item["reason"] == "cursor_must_be_closed"
                    for item in open_manifest["blocked"]
                )
            )

            local_manifest, local_payloads = preferences.scan_preferences(
                str(destination), cursor_closed=True
            )
            plan = preferences.plan_preferences(
                local_manifest,
                local_manifest,
                closed_manifest,
                base_payloads=local_payloads,
                local_payloads=local_payloads,
                incoming_payloads=closed_payloads,
            )
            preferences.apply_preferences(
                plan,
                str(destination),
                backup_dir=str(destination / "backups"),
                inbox_dir=str(destination / "inbox"),
                cursor_closed=True,
                batch_id="test",
            )
            connection = sqlite3.connect(str(destination_db))
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM ItemTable WHERE key = ?",
                        (preferences.CURSOR_USER_RULE_KEY,),
                    ).fetchone()[0],
                    "source rule",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM ItemTable WHERE key = 'unrelated'"
                    ).fetchone()[0],
                    "do not touch",
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
