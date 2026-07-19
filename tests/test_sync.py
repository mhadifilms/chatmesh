import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from chatmesh import sync


class SyncTests(unittest.TestCase):
    def test_wip_import_never_starts_after_export_failure(self):
        pair = SimpleNamespace(
            local={"real_path": "/local/repo"},
            remote={"real_path": "/remote/repo"},
        )
        action = {"action": "push-wip", "apply": True}
        profile = SimpleNamespace(auto_apply=True)

        with mock.patch.object(
            sync.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1),
        ) as run:
            result = sync._transfer_wip(
                SimpleNamespace(), "peer", pair, action, profile
            )

        self.assertEqual(result["reason"], "snapshot-export-failed")
        self.assertIsNone(result["recv_returncode"])
        run.assert_called_once()

    def test_environment_dry_run_marks_plan_without_applying(self):
        cfg = SimpleNamespace(directions=["pull", "push"])
        local = {"version": 1, "snapshot_id": "local", "blocked": []}
        remote = {"version": 1, "snapshot_id": "remote", "blocked": []}
        plan = {
            "actions": [{"kind": "brew-formula", "name": "ripgrep"}],
            "conflicts": [],
            "blocked": [],
            "counts": {
                "install": 1,
                "keep": 0,
                "conflict": 0,
                "blocked": 0,
            },
        }
        state = {}
        with mock.patch(
            "chatmesh.environmentops.snapshot_environment",
            return_value=local,
        ), mock.patch(
            "chatmesh.environmentops.plan_snapshots",
            return_value=plan,
        ), mock.patch(
            "chatmesh.environmentops.apply_environment_snapshot"
        ) as apply, mock.patch.object(
            sync, "_remote_json", return_value=remote
        ), mock.patch.object(
            sync, "_remote_plan_environment", return_value=plan
        ), mock.patch.object(
            sync, "_remote_apply_environment"
        ) as remote_apply:
            sync.sync_environment(cfg, "peer", state, dry_run=True)

        apply.assert_not_called()
        remote_apply.assert_not_called()
        detail = state["peer"]["environment"]["sync"]
        self.assertTrue(detail["ok"])
        self.assertTrue(detail["dry_run"])
        self.assertEqual(detail["pull"]["install"], 1)


if __name__ == "__main__":
    unittest.main()
