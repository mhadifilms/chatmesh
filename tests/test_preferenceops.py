import json
import os
import tempfile
import unittest
from unittest import mock

from chatmesh.config import Config, CustomPreferencePath, PreferencesProfile
from chatmesh import preferenceops


class PreferenceOpsTests(unittest.TestCase):
    def config(self, state, custom=None):
        return Config(
            state_dir=state,
            preferences=PreferencesProfile(
                enabled=True,
                cursor=True,
                claude=False,
                codex=False,
                custom_paths=list(custom or []),
            ),
        )

    def test_custom_paths_are_bounded_to_home_and_honor_size_limit(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as state:
            safe = os.path.join(home, ".tool", "preferences.txt")
            os.makedirs(os.path.dirname(safe))
            with open(safe, "w") as stream:
                stream.write("too large")
            cfg = self.config(state, [
                CustomPreferencePath(
                    name="tool", path=safe, max_file_bytes=3
                ),
                CustomPreferencePath(
                    name="outside", path=os.path.join(state, "outside")
                ),
            ])
            adapters = preferenceops.adapters_for_config(cfg, home)
            names = {adapter.name for adapter in adapters}
            self.assertIn("custom-tool", names)
            self.assertNotIn("custom-outside", names)
            with mock.patch.object(
                preferenceops, "app_running_local", return_value=False
            ):
                snapshot = preferenceops.snapshot_preferences(cfg, home)
            self.assertNotIn("custom/tool", snapshot["manifest"]["entries"])
            self.assertTrue(any(
                item["reason"] == "size_limit"
                for item in snapshot["manifest"]["blocked"]
            ))

    def test_converged_baseline_keeps_old_base_for_conflict(self):
        home = "/Users/example"
        old = preferenceops.empty_snapshot(home)
        entry = {
            "adapter": "cursor-cli-settings",
            "destination": ".cursor/settings.json",
            "kind": "file",
            "format": "json",
            "rewrite_text": True,
            "mode": 0o600,
        }

        def snap(content):
            data = content.encode()
            item = dict(entry, sha256=__import__("hashlib").sha256(data).hexdigest(),
                        size=len(data))
            return {
                "version": 1,
                "home": home,
                "manifest": {
                    "version": 1,
                    "entries": {"cursor/cli/settings.json": item},
                    "blocked": [],
                    "total_size": len(data),
                },
                "payloads": {
                    "cursor/cli/settings.json":
                        __import__("base64").b64encode(data).decode()
                },
            }

        old = snap('{"value":"base"}\n')
        baseline = preferenceops.converged_baseline(
            snap('{"value":"local"}\n'),
            snap('{"value":"remote"}\n'),
            old,
        )
        _, payloads, _ = preferenceops.decode_snapshot(baseline)
        self.assertEqual(
            json.loads(payloads["cursor/cli/settings.json"]),
            {"value": "base"},
        )

    def test_process_gate_turns_apply_into_keep(self):
        cfg = Config(process_gate_apps=["cursor"])
        plan = {
            "version": 1,
            "actions": [{
                "key": "cursor/cli/settings.json",
                "op": "apply",
                "entry": {"adapter": "cursor-cli-settings"},
                "payload": "",
            }],
            "counts": {"apply": 1, "keep": 0, "conflict": 0},
        }
        with mock.patch.object(
            preferenceops, "app_running_local",
            side_effect=lambda app: app == "cursor",
        ):
            gated = preferenceops.gate_running_tools(cfg, plan)
        self.assertEqual(gated["actions"][0]["op"], "keep")
        self.assertEqual(gated["actions"][0]["key"], "cursor/cli/settings.json")

    def test_custom_destination_is_rebound_to_local_profile_path(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as state:
            local_path = os.path.join(home, ".machine", "tool.conf")
            cfg = self.config(state, [
                CustomPreferencePath(name="tool", path=local_path)
            ])
            plan = {
                "version": 1,
                "actions": [{
                    "key": "custom/tool",
                    "op": "apply",
                    "entry": {
                        "adapter": "custom-tool",
                        "destination": ".different/tool.conf",
                    },
                    "payload": "",
                }],
                "counts": {"apply": 1, "keep": 0, "conflict": 0},
            }
            rebound = preferenceops.rebind_plan_destinations(cfg, plan, home)
            self.assertEqual(
                rebound["actions"][0]["entry"]["destination"],
                ".machine/tool.conf",
            )


if __name__ == "__main__":
    unittest.main()
