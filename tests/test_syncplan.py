import unittest

from chatmesh.syncplan import (
    branch_import_plan,
    match_repositories,
    wip_transfer_plan,
)


class SyncPlanTests(unittest.TestCase):
    def test_matches_repository_after_folder_move_by_unique_identity(self):
        local = [{"identity": "github:R1", "relative_path": "owner/old"}]
        remote = [{"identity": "github:R1", "relative_path": "owner/new"}]
        pairs, local_only, remote_only, ambiguous = match_repositories(local, remote)
        self.assertEqual(len(pairs), 1)
        self.assertFalse(local_only or remote_only or ambiguous)

    def test_does_not_guess_duplicate_clone_mapping(self):
        local = [
            {"identity": "github:R1", "relative_path": "a", "branch": "main"},
            {"identity": "github:R1", "relative_path": "b", "branch": "main"},
        ]
        remote = [
            {"identity": "github:R1", "relative_path": "c", "branch": "main"},
            {"identity": "github:R1", "relative_path": "d", "branch": "main"},
        ]
        pairs, _, _, ambiguous = match_repositories(local, remote)
        self.assertFalse(pairs)
        self.assertEqual(len(ambiguous), 1)

    def test_matches_unique_origin_fallback_when_only_one_peer_has_github_id(self):
        local = [{
            "identity": "github-id:R_123",
            "repository_id": "R_123",
            "origin_identity": "github:owner/repo",
        }]
        remote = [{
            "identity": "github:owner/repo",
            "repository_id": None,
            "origin_identity": "github:owner/repo",
        }]
        pairs, local_only, remote_only, ambiguous = match_repositories(
            local, remote
        )
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].identity, "github-id:R_123")
        self.assertFalse(local_only or remote_only or ambiguous)

    def test_origin_fallback_matches_main_and_worktree_without_guessing(self):
        local = [
            {
                "identity": "github-id:R_123",
                "repository_id": "R_123",
                "origin_identity": "github:owner/repo",
                "relative_path": "owner/repo",
                "branch": "main",
            },
            {
                "identity": "github-id:R_123",
                "repository_id": "R_123",
                "origin_identity": "github:owner/repo",
                "relative_path": "owner/repo/.worktrees/fix",
                "branch": "mhadi/fix/example",
            },
        ]
        remote = [
            {
                "identity": "github:owner/repo",
                "repository_id": None,
                "origin_identity": "github:owner/repo",
                "relative_path": "owner/repo",
                "branch": "main",
            },
            {
                "identity": "github:owner/repo",
                "repository_id": None,
                "origin_identity": "github:owner/repo",
                "relative_path": "owner/repo/.worktrees/fix",
                "branch": "mhadi/fix/example",
            },
        ]

        pairs, local_only, remote_only, ambiguous = match_repositories(
            local, remote
        )

        self.assertEqual(len(pairs), 2)
        self.assertTrue(all(pair.identity == "github-id:R_123" for pair in pairs))
        self.assertFalse(local_only or remote_only or ambiguous)

    def test_imports_changed_refs_in_both_directions(self):
        actions = branch_import_plan(
            {"dev": "a" * 40}, {"dev": "b" * 40}, ["pull", "push"]
        )
        self.assertEqual({item["action"] for item in actions}, {"pull-ref", "push-ref"})

    def test_concurrent_wip_is_transferred_but_never_applied(self):
        actions = wip_transfer_plan(
            {"dirty": True, "wip_id": "local"},
            {"dirty": True, "wip_id": "remote"},
            ["pull", "push"],
        )
        self.assertEqual(len(actions), 2)
        self.assertTrue(all(not item["apply"] for item in actions))
        self.assertTrue(all(item["reason"] == "concurrent-wip" for item in actions))

    def test_wip_auto_apply_requires_identical_head_and_branch(self):
        local = {
            "dirty": True,
            "wip_id": "local",
            "head": "a" * 40,
            "branch": "topic",
        }
        matching = {
            "dirty": False,
            "wip_id": "clean",
            "head": "a" * 40,
            "branch": "topic",
        }
        mismatched = {
            **matching,
            "head": "b" * 40,
            "branch": "main",
        }

        safe = wip_transfer_plan(local, matching, ["push"])
        quarantined = wip_transfer_plan(local, mismatched, ["push"])

        self.assertTrue(safe[0]["apply"])
        self.assertEqual(safe[0]["reason"], "destination-clean")
        self.assertFalse(quarantined[0]["apply"])
        self.assertEqual(quarantined[0]["reason"], "base-mismatch")


if __name__ == "__main__":
    unittest.main()
