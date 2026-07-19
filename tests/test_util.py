import unittest

from chatmesh.util import remote_chatmesh_args


class RemoteCommandTests(unittest.TestCase):
    def test_quotes_remote_arguments(self):
        command = remote_chatmesh_args(
            ["git-advance", "--repo", "/Volumes/Drive Name/repo; false"]
        )
        self.assertIn("'/Volumes/Drive Name/repo; false'", command)
        self.assertNotIn("repo; false --", command)


if __name__ == "__main__":
    unittest.main()
