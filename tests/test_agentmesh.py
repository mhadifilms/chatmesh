from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from chatmesh.agentmesh import (
    MESH_KEY,
    mesh_global_skills,
    mesh_manifests,
    mesh_messages,
    read_claude_session,
    read_codex_session,
)
from chatmesh.config import AgentMeshProfile, Config


def write_jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    os.utime(path, (1, 1))


class MessageMeshTests(unittest.TestCase):
    def config(self, home: str) -> Config:
        return Config(
            state_dir=os.path.join(home, "state"),
            file_guard_sec=10,
            agent_mesh=AgentMeshProfile(
                enabled=True,
                messages=True,
                skills=False,
                rules=False,
                instructions=False,
                roots=[os.path.join(home, "repos")],
                max_session_bytes=1024 * 1024,
            ),
        )

    def test_claude_projects_into_codex_without_private_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / ".claude/projects/-tmp-repo/session.jsonl"
            write_jsonl(source, [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "hello"},
                    "timestamp": "2026-01-02T03:04:05Z",
                    "cwd": "/tmp/repo",
                    "sessionId": "claude-session",
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "private"},
                            {"type": "text", "text": "world"},
                            {"type": "tool_use", "name": "Bash"},
                        ],
                    },
                    "timestamp": "2026-01-02T03:04:06Z",
                    "cwd": "/tmp/repo",
                    "sessionId": "claude-session",
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "content": "secret"}],
                    },
                    "cwd": "/tmp/repo",
                    "sessionId": "claude-session",
                },
            ])
            result = mesh_messages(
                self.config(temp), user_home=temp, now=1000
            )
            self.assertEqual(len(result["created"]), 1)
            destination = Path(result["created"][0]["destination"])
            parsed = [
                json.loads(line)
                for line in destination.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(parsed[0][MESH_KEY]["source_agent"], "claude")
            rendered = destination.read_text(encoding="utf-8")
            self.assertIn("hello", rendered)
            self.assertIn("world", rendered)
            self.assertNotIn("private", rendered)
            self.assertNotIn("secret", rendered)
            self.assertIsNone(read_codex_session(str(destination), 1024 * 1024))

    def test_codex_projection_is_loop_safe_and_continuation_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            source = (
                Path(temp) / ".codex/sessions/2026/01/02/rollout-native.jsonl"
            )
            records = [
                {
                    "timestamp": "2026-01-02T03:04:05Z",
                    "type": "session_meta",
                    "payload": {"id": "codex-session", "cwd": "/tmp/repo"},
                },
                {
                    "timestamp": "2026-01-02T03:04:06Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "question"}],
                    },
                },
                {
                    "timestamp": "2026-01-02T03:04:07Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "answer"}],
                    },
                },
            ]
            write_jsonl(source, records)
            cfg = self.config(temp)
            first = mesh_messages(cfg, user_home=temp, now=1000)
            destination = Path(first["created"][0]["destination"])
            self.assertIsNone(read_claude_session(str(destination), 1024 * 1024))
            with destination.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "type": "user",
                    "message": {"role": "user", "content": "continued"},
                }) + "\n")
            os.utime(destination, (1, 1))
            records[2]["payload"]["content"][0]["text"] = "new answer"
            write_jsonl(source, records)
            second = mesh_messages(cfg, user_home=temp, now=1000)
            self.assertEqual(len(second["conflicts"]), 1)
            self.assertIn("continued", destination.read_text(encoding="utf-8"))


class ResourceMeshTests(unittest.TestCase):
    def config(self, home: str, roots=None) -> Config:
        return Config(
            state_dir=os.path.join(home, "state"),
            agent_mesh=AgentMeshProfile(
                enabled=True,
                messages=False,
                skills=True,
                rules=True,
                instructions=True,
                resource_agents=["claude", "codex", "cursor"],
                roots=roots or [os.path.join(home, "repos")],
            ),
        )

    def test_global_skill_keeps_companions_and_links_agents(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / ".claude/skills/review"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("# Review\n", encoding="utf-8")
            (source / "references.md").write_text("details\n", encoding="utf-8")
            result = mesh_global_skills(self.config(temp), user_home=temp)
            canonical = Path(temp) / ".agents/skills/review"
            self.assertTrue((canonical / "references.md").is_file())
            self.assertTrue(source.is_symlink())
            self.assertEqual(source.resolve(), canonical.resolve())
            cursor = Path(temp) / ".cursor/skills-cursor/review"
            self.assertTrue(cursor.is_symlink())
            self.assertGreaterEqual(len(result["linked"]), 2)

    def test_different_global_skills_are_quarantined(self):
        with tempfile.TemporaryDirectory() as temp:
            for base, text in (
                (".claude/skills", "claude"),
                (".agents/skills", "codex"),
            ):
                path = Path(temp) / base / "same"
                path.mkdir(parents=True)
                (path / "SKILL.md").write_text(text, encoding="utf-8")
            result = mesh_global_skills(self.config(temp), user_home=temp)
            self.assertEqual(len(result["conflicts"]), 1)
            self.assertFalse((Path(temp) / ".claude/skills/same").is_symlink())

    def test_vibelink_manifest_links_full_resources(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repos/project"
            skill = repo / "source/skill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# Shared\n", encoding="utf-8")
            (skill / "asset.txt").write_text("asset\n", encoding="utf-8")
            rule = repo / "source/rule.md"
            rule.write_text("rule\n", encoding="utf-8")
            instructions = repo / "source/instructions.md"
            instructions.write_text("instructions\n", encoding="utf-8")
            (repo / "vibelink.json").write_text(json.dumps({
                "$schema": 1,
                "skills": ["source/skill"],
                "rules": ["source/rule.md"],
                "instructions": "source/instructions.md",
                "agents": ["claude", "codex", "cursor"],
            }), encoding="utf-8")
            result = mesh_manifests(
                self.config(temp, roots=[str(Path(temp) / "repos")])
            )
            self.assertFalse(result["blocked"])
            self.assertTrue((repo / ".claude/skills/skill").is_symlink())
            self.assertTrue((repo / ".agents/skills/skill").is_symlink())
            self.assertTrue((repo / ".cursor/rules/rule.mdc").is_symlink())
            self.assertTrue((repo / "CLAUDE.md").is_symlink())
            self.assertTrue((repo / "AGENTS.md").is_symlink())
            self.assertTrue((repo / ".cursorrules").is_symlink())


if __name__ == "__main__":
    unittest.main()
