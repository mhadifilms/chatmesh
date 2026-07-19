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


if __name__ == "__main__":
    unittest.main()
