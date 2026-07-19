import unittest

from chatmesh.gittransport import incoming_ref, ssh_repo_url


class GitTransportTests(unittest.TestCase):
    def test_ssh_url_quotes_spaces_and_preserves_absolute_path(self):
        self.assertEqual(
            ssh_repo_url("mhadi-mini", "/Volumes/Muhammad Hadi/GitHub/repo"),
            "ssh://mhadi-mini/Volumes/Muhammad%20Hadi/GitHub/repo",
        )

    def test_rejects_shell_metacharacters_in_peer(self):
        with self.assertRaises(ValueError):
            ssh_repo_url("host;touch /tmp/no", "/repo")

    def test_incoming_refs_are_oid_immutable_and_branch_safe(self):
        oid = "a" * 40
        first = incoming_ref("mini", "feature/a", oid)
        second = incoming_ref("mini", "feature/b", oid)
        self.assertTrue(first.endswith("/" + oid))
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
