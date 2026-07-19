from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import is_dataclass
from pathlib import Path
from unittest import mock

from chatmesh import tomlutil
from chatmesh.config import (
    CONFIG_PATH,
    EXAMPLE_TOML,
    Config,
    ConfigError,
    CustomPreferencePath,
    EnvironmentProfile,
    GitProfile,
    MeshConfig,
    PreferencesProfile,
    RepositoryOverride,
    default_config_path,
    example_toml,
    write_example,
)


class ConfigDefaultsTests(unittest.TestCase):
    def test_defaults_keep_existing_chat_behavior_and_disable_new_profiles(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(
                os.environ, {"CHATMESH_HOME": temp}, clear=False
            ):
                cfg = Config.load()

        self.assertEqual(cfg.peers, [])
        self.assertEqual(
            cfg.apps, ["cursor", "cursor-cli", "claude", "codex"]
        )
        self.assertEqual(cfg.directions, ["pull", "push"])
        self.assertEqual(cfg.interval, 3600)
        self.assertEqual(cfg.file_guard_sec, 900)
        self.assertFalse(cfg.sync_checkpoints)
        self.assertEqual(cfg.max_composers_per_run, 0)
        self.assertEqual(cfg.process_gate_apps, ["cursor", "cursor-cli"])
        self.assertEqual(cfg.log_level, "INFO")
        self.assertEqual(
            cfg.state_dir, os.path.join(temp, ".local", "state", "chatmesh")
        )
        self.assertFalse(cfg.git.enabled)
        self.assertFalse(cfg.git.auto_apply)
        self.assertFalse(cfg.preferences.enabled)
        self.assertFalse(cfg.preferences.cursor)
        self.assertFalse(cfg.preferences.claude)
        self.assertFalse(cfg.preferences.codex)
        self.assertFalse(cfg.environment.enabled)
        self.assertFalse(cfg.environment.auto_apply)

    def test_config_path_is_toml_and_fixture_home_is_resolved_at_load_time(self):
        self.assertTrue(CONFIG_PATH.endswith(".config/chatmesh/config.toml"))
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(
                os.environ, {"CHATMESH_HOME": temp}, clear=False
            ):
                self.assertEqual(
                    default_config_path(),
                    os.path.join(temp, ".config", "chatmesh", "config.toml"),
                )

    def test_env_file_and_user_facing_environment_are_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            old_path = Path(temp) / ".config" / "chatmesh" / "env"
            old_path.parent.mkdir(parents=True)
            old_path.write_text(
                "CHATMESH_PEERS=old-peer\nCHATMESH_INTERVAL=7\n",
                encoding="utf-8",
            )
            env = {
                "CHATMESH_HOME": temp,
                "CHATMESH_PEERS": "environment-peer",
                "CHATMESH_INTERVAL": "8",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                cfg = Config.load()
        self.assertEqual(cfg.peers, [])
        self.assertEqual(cfg.interval, 3600)

    def test_public_configuration_types_are_dataclasses(self):
        for value in (
            MeshConfig,
            GitProfile,
            RepositoryOverride,
            PreferencesProfile,
            CustomPreferencePath,
            EnvironmentProfile,
        ):
            self.assertTrue(is_dataclass(value))
        self.assertIs(Config, MeshConfig)

    def test_repository_override_produces_effective_profile(self):
        profile = GitProfile(
            enabled=True,
            auto_apply=False,
            repositories=[
                RepositoryOverride(
                    identity="github:owner/repo",
                    auto_apply=True,
                    ignored=True,
                    sync_tags=False,
                )
            ],
        )
        effective = profile.for_repository("github:owner/repo")
        self.assertTrue(effective.auto_apply)
        self.assertTrue(effective.ignored)
        self.assertFalse(effective.sync_tags)
        self.assertFalse(profile.auto_apply)


class ConfigLoadingTests(unittest.TestCase):
    DOCUMENT = """\
version = 1

[mesh]
peers = ["mini", "studio"]
apps = ["cursor", "codex"]
directions = ["pull"]
interval_seconds = 120
file_guard_minutes = 2
sync_checkpoints = true
max_composers_per_run = 9
process_gate_apps = ["cursor"]
log_level = "debug"
state_dir = "~/.state/chatmesh"

[git]
enabled = true
roots = ["~/Documents/GitHub", "/Volumes/Code"]
branches = true
tags = false
worktrees = true
clone_missing = true
relocate = true
staged = true
unstaged = true
untracked = true
ignored = false
auto_apply = false
max_file_bytes = 123
max_snapshot_bytes = 456
conflict_policy = "quarantine"

[[git.repositories]]
identity = "github:1234"
enabled = true
untracked = false
max_file_bytes = 12
conflict_policy = "manual"

[preferences]
enabled = true
cursor = true
claude = true
codex = false
conflict_policy = "keep-both"
max_file_bytes = 100
max_total_bytes = 200
exclude = ["**/cache/**"]

[[preferences.custom_paths]]
name = "zed"
path = "~/.config/zed/settings.json"
kind = "file"
enabled = true
rewrite_home = true
max_file_bytes = 80
conflict_policy = "skip"
exclude = []
"""

    def test_loads_all_profiles_and_preserves_legacy_attributes(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / ".config" / "chatmesh" / "config.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(self.DOCUMENT, encoding="utf-8")
            with mock.patch.dict(
                os.environ, {"CHATMESH_HOME": temp}, clear=False
            ):
                cfg = Config.load()

        self.assertEqual(cfg.peers, ["mini", "studio"])
        self.assertEqual(cfg.apps, ["cursor", "codex"])
        self.assertEqual(cfg.directions, ["pull"])
        self.assertEqual(cfg.interval, 120)
        self.assertEqual(cfg.file_guard_sec, 120)
        self.assertTrue(cfg.sync_checkpoints)
        self.assertEqual(cfg.max_composers_per_run, 9)
        self.assertEqual(cfg.process_gate_apps, ["cursor"])
        self.assertEqual(cfg.log_level, "DEBUG")
        self.assertEqual(cfg.state_dir, os.path.join(temp, ".state/chatmesh"))

        self.assertTrue(cfg.git.enabled)
        self.assertEqual(
            cfg.git.roots,
            [os.path.join(temp, "Documents/GitHub"), "/Volumes/Code"],
        )
        self.assertFalse(cfg.git.tags)
        self.assertTrue(cfg.git.clone_missing)
        self.assertTrue(cfg.git.relocate_repositories)
        self.assertEqual(len(cfg.git.repositories), 1)
        override = cfg.git.repositories[0]
        self.assertEqual(override.identity, "github:1234")
        self.assertFalse(override.untracked)
        self.assertEqual(override.max_file_bytes, 12)
        self.assertEqual(override.conflict_policy, "manual")

        self.assertTrue(cfg.preferences.enabled)
        self.assertTrue(cfg.preferences.cursor)
        self.assertTrue(cfg.preferences.claude)
        self.assertFalse(cfg.preferences.codex)
        custom = cfg.preferences.custom_paths[0]
        self.assertEqual(custom.name, "zed")
        self.assertEqual(
            custom.path, os.path.join(temp, ".config/zed/settings.json")
        )
        self.assertTrue(custom.rewrite_home)

    def test_missing_file_returns_defaults_but_invalid_toml_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = os.path.join(temp, "missing.toml")
            self.assertIsInstance(Config.load(missing), MeshConfig)
            invalid = os.path.join(temp, "invalid.toml")
            Path(invalid).write_text("[mesh\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "cannot read"):
                Config.load(invalid)


class ConfigValidationTests(unittest.TestCase):
    def assert_invalid(self, document, message):
        with self.assertRaisesRegex(ConfigError, message):
            Config.from_dict(document)

    def test_rejects_wrong_types_unknown_keys_and_invalid_directions(self):
        cases = (
            ({"mesh": {"interval": "10"}}, "integer"),
            ({"mesh": {"sync_checkpoints": 1}}, "boolean"),
            ({"mesh": {"apps": "cursor"}}, "array of strings"),
            ({"mesh": {"directions": ["sideways"]}}, "invalid direction"),
            ({"mesh": {"directions": ["pull", "pull"]}}, "duplicates"),
            ({"mesh": {"typo": True}}, "unknown key"),
            ({"git": {"enabled": "yes"}}, "boolean"),
            ({"preferences": {"cursor": 1}}, "boolean"),
        )
        for document, message in cases:
            with self.subTest(document=document):
                self.assert_invalid(document, message)

    def test_rejects_negative_limits_bad_policies_and_unsafe_roots(self):
        cases = (
            ({"mesh": {"interval": -1}}, "at least 0"),
            ({"mesh": {"file_guard_seconds": -1}}, "at least 0"),
            ({"mesh": {"max_composers_per_run": -1}}, "at least 0"),
            ({"git": {"max_file_bytes": -1}}, "at least 0"),
            ({"git": {"max_snapshot_bytes": -1}}, "at least 0"),
            ({"preferences": {"max_total_bytes": -1}}, "at least 0"),
            ({"git": {"conflict_policy": "overwrite"}}, "must be one of"),
            (
                {"preferences": {"conflict_policy": "newest-wins"}},
                "must be one of",
            ),
            (
                {"git": {"enabled": True, "roots": ["relative/path"]}},
                "must be absolute",
            ),
            (
                {"git": {"enabled": True, "roots": []}},
                "must not be empty",
            ),
            ({"git": {"roots": ["/"]}}, "filesystem root"),
        )
        for document, message in cases:
            with self.subTest(document=document):
                self.assert_invalid(document, message)

    def test_rejects_duplicate_overrides_and_custom_names(self):
        self.assert_invalid(
            {
                "git": {
                    "repositories": [
                        {"identity": "same"},
                        {"identity": "same"},
                    ]
                }
            },
            "identities must be unique",
        )
        self.assert_invalid(
            {
                "preferences": {
                    "custom_paths": [
                        {"name": "same", "path": "~/.one"},
                        {"name": "same", "path": "~/.two"},
                    ]
                }
            },
            "names must be unique",
        )

    def test_environment_profile_is_typed_bounded_and_round_trips(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(
                os.environ, {"CHATMESH_HOME": temp}, clear=False
            ):
                cfg = Config.from_dict({
                    "environment": {
                        "enabled": True,
                        "homebrew": False,
                        "brewfile": "~/Config/Brewfile",
                        "python": True,
                        "pip": False,
                        "pipx": True,
                        "uv": True,
                        "venvs": True,
                        "auto_apply": False,
                        "roots": ["~/Code"],
                        "exclude": ["brew:docker"],
                        "max_lock_file_bytes": 1234,
                        "conflict_policy": "manual",
                    }
                })
                reparsed = Config.from_dict(
                    tomlutil.loads(cfg.to_toml(), force_fallback=True)
                )
        self.assertTrue(cfg.environment.enabled)
        self.assertFalse(cfg.environment.homebrew)
        self.assertEqual(cfg.environment.roots, [os.path.join(temp, "Code")])
        self.assertEqual(cfg.environment.max_lock_file_bytes, 1234)
        self.assertEqual(reparsed, cfg)
        for document, message in (
            ({"environment": {"typo": True}}, "unknown key"),
            ({"environment": {"pip": "yes"}}, "boolean"),
            (
                {"environment": {"max_lock_file_bytes": -1}},
                "at least 0",
            ),
            (
                {
                    "environment": {
                        "enabled": True,
                        "venvs": True,
                        "roots": [],
                    }
                },
                "must not be empty",
            ),
        ):
            with self.subTest(document=document):
                self.assert_invalid(document, message)


class TomlCompatibilityTests(unittest.TestCase):
    def test_fallback_supports_comments_multiline_arrays_and_table_arrays(self):
        parsed = tomlutil.loads(
            """\
title = "value # retained"
[mesh]
peers = [
  "mini", # comment
  'studio',
]
quoted."dotted.key" = { enabled = true, count = 2 }
[[git.repositories]]
identity = "github:1"
branches = false
""",
            force_fallback=True,
        )
        self.assertEqual(parsed["title"], "value # retained")
        self.assertEqual(parsed["mesh"]["peers"], ["mini", "studio"])
        self.assertEqual(
            parsed["mesh"]["quoted"]["dotted.key"],
            {"enabled": True, "count": 2},
        )
        self.assertEqual(
            parsed["git"]["repositories"][0]["identity"], "github:1"
        )

    def test_deterministic_writer_round_trips_with_fallback(self):
        document = {
            "z": 2,
            "a": {"items": ["x", "y"], "enabled": True},
            "profiles": [
                {"name": "one", "limit": 1},
                {"name": "two", "limit": 2},
            ],
        }
        first = tomlutil.dumps(document)
        second = tomlutil.dumps(document)
        self.assertEqual(first, second)
        self.assertEqual(tomlutil.loads_fallback(first), document)

    def test_example_and_full_config_round_trip_through_fallback(self):
        for text in (EXAMPLE_TOML, example_toml()):
            parsed = tomlutil.loads(text, force_fallback=True)
            cfg = Config.from_dict(parsed)
            reparsed = tomlutil.loads(cfg.to_toml(), force_fallback=True)
            self.assertEqual(Config.from_dict(reparsed), cfg)

    def test_write_helpers_are_deterministic_and_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "config.toml")
            write_example(path)
            first = Path(path).read_text(encoding="utf-8")
            self.assertEqual(first, EXAMPLE_TOML)
            with self.assertRaises(FileExistsError):
                write_example(path)

            cfg_path = os.path.join(temp, "nested", "custom.toml")
            cfg = MeshConfig(peers=["mini"])
            cfg.write(cfg_path)
            self.assertEqual(
                Path(cfg_path).read_text(encoding="utf-8"), cfg.to_toml()
            )
            with self.assertRaises(FileExistsError):
                cfg.write(cfg_path)
            cfg.write(cfg_path, overwrite=True)


if __name__ == "__main__":
    unittest.main()
