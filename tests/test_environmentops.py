from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from chatmesh import environment, environmentops
from chatmesh.config import Config, EnvironmentProfile


class EnvironmentOpsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = self.temporary.name
        self.cfg = Config(
            state_dir=os.path.join(self.home, "state"),
            environment=EnvironmentProfile(
                enabled=True,
                auto_apply=False,
                homebrew=True,
                python=False,
                pip=False,
                pipx=False,
                uv=False,
                venvs=False,
                roots=[],
            ),
        )
        self.source = {
            "version": 1,
            "brew": {
                "formulae": ["ripgrep"],
                "casks": [],
                "taps": [],
                "installed_formulae": ["ripgrep"],
                "installed_casks": [],
                "installed_taps": [],
            },
            "python": {},
            "pip": [],
            "pipx": [],
            "uv": [],
            "venvs": {},
            "blocked": [],
            "complete": {
                "brew": True,
                "pip": True,
                "pipx": True,
                "uv": True,
                "venvs": True,
            },
        }
        self.source["snapshot_id"] = environment.manifest_snapshot_id(
            self.source
        )
        self.destination = {
            **self.source,
            "brew": {
                "formulae": [],
                "casks": [],
                "taps": [],
                "installed_formulae": [],
                "installed_casks": [],
                "installed_taps": [],
            },
        }
        self.destination["snapshot_id"] = environment.manifest_snapshot_id(
            self.destination
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_auto_apply_off_quarantines_without_running_install(self):
        with mock.patch.object(
            environmentops,
            "snapshot_environment",
            return_value=self.destination,
        ), mock.patch.object(
            environmentops.environment,
            "apply_environment_plan",
        ) as apply:
            result = environmentops.apply_environment_snapshot(
                self.cfg, "peer", self.source
            )

        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["applied"], [])
        apply.assert_not_called()
        self.assertTrue(os.path.isfile(
            os.path.join(result["inbox"], "snapshot.json")
        ))
        self.assertEqual(
            len([
                name
                for name in os.listdir(result["inbox"])
                if name.startswith("plan-")
            ]),
            1,
        )

    def test_force_apply_runs_only_planned_additions(self):
        applied = {
            "ok": True,
            "applied": [{"kind": "brew-formula", "name": "ripgrep"}],
            "failed": [],
            "conflicts": [],
            "blocked": [],
        }
        with mock.patch.object(
            environmentops,
            "snapshot_environment",
            return_value=self.destination,
        ), mock.patch.object(
            environmentops.environment,
            "apply_environment_plan",
            return_value=applied,
        ) as apply:
            result = environmentops.apply_environment_snapshot(
                self.cfg, "peer", self.source, force=True
            )

        self.assertEqual(len(result["applied"]), 1)
        apply.assert_called_once()

    def test_dry_run_is_read_only_and_disabled_profile_rejects_import(self):
        with mock.patch.object(
            environmentops,
            "snapshot_environment",
            return_value=self.destination,
        ):
            result = environmentops.apply_environment_snapshot(
                self.cfg, "peer", self.source, dry_run=True
            )
        self.assertTrue(result["dry_run"])
        self.assertIsNone(result["inbox"])
        self.assertFalse(os.path.exists(self.cfg.state_dir))

        self.cfg.environment.enabled = False
        with self.assertRaises(environment.EnvironmentSyncError):
            environmentops.apply_environment_snapshot(
                self.cfg, "peer", self.source
            )

    def test_existing_quarantine_is_immutable(self):
        with mock.patch.object(
            environmentops,
            "snapshot_environment",
            return_value=self.destination,
        ):
            first = environmentops.apply_environment_snapshot(
                self.cfg, "peer", self.source
            )
            with open(
                os.path.join(first["inbox"], "snapshot.json"),
                "w",
                encoding="utf-8",
            ) as stream:
                stream.write("{}\n")
            with self.assertRaises(environment.EnvironmentSyncError):
                environmentops.apply_environment_snapshot(
                    self.cfg, "peer", self.source
                )

    def test_same_snapshot_with_new_timestamp_is_idempotent(self):
        with mock.patch.object(
            environmentops,
            "snapshot_environment",
            return_value=self.destination,
        ):
            first = environmentops.apply_environment_snapshot(
                self.cfg, "peer", self.source
            )
            repeated = dict(self.source, generated_at=999)
            self.assertEqual(
                environment.manifest_snapshot_id(repeated),
                self.source["snapshot_id"],
            )
            second = environmentops.apply_environment_snapshot(
                self.cfg, "peer", repeated
            )
        self.assertEqual(first["inbox"], second["inbox"])


if __name__ == "__main__":
    unittest.main()
