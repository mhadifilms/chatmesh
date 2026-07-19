import glob
import os
import tempfile
import unittest
from unittest import mock

from chatmesh.cli import migrate_env_config
from chatmesh.config import Config, default_config_path


class ConfigMigrationTests(unittest.TestCase):
    def test_migrates_and_archives_legacy_env(self):
        with tempfile.TemporaryDirectory() as home:
            legacy = os.path.join(home, ".config", "chatmesh", "env")
            os.makedirs(os.path.dirname(legacy))
            with open(legacy, "w", encoding="utf-8") as stream:
                stream.write(
                    "CHATMESH_PEERS=mhadi-mini\n"
                    "CHATMESH_APPS=cursor,claude\n"
                    "CHATMESH_DIRECTIONS=pull,push\n"
                    "CHATMESH_INTERVAL=120\n"
                )
            with mock.patch.dict(os.environ, {"CHATMESH_HOME": home}):
                self.assertEqual(migrate_env_config(legacy), 0)
                config_path = default_config_path()
                self.assertTrue(os.path.isfile(config_path))
                cfg = Config.load(config_path)
                self.assertEqual(cfg.peers, ["mhadi-mini"])
                self.assertEqual(cfg.interval, 120)
                self.assertFalse(os.path.exists(legacy))
                self.assertEqual(len(glob.glob(legacy + ".pre-toml-*")), 1)


if __name__ == "__main__":
    unittest.main()
