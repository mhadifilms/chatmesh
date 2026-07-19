import os
import subprocess
import tempfile
import unittest
from unittest import mock

from chatmesh import gitrepos, repoops
from chatmesh.config import GitProfile


def git(path, *args):
    subprocess.run(
        ["git", "-C", path] + list(args),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class RepositoryOpsTests(unittest.TestCase):
    def test_inventory_keeps_logical_symlink_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            physical = os.path.join(tmp, "volume")
            logical = os.path.join(tmp, "GitHub")
            repo = os.path.join(physical, "owner", "repo")
            os.makedirs(repo)
            git(repo, "init")
            git(repo, "remote", "add", "origin", "git@github.com:Owner/Repo.git")
            os.symlink(physical, logical)
            profile = type("Profile", (), {"roots": [logical]})()
            records = repoops.inventory(profile)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["relative_path"], "owner/repo")
            self.assertEqual(records[0]["logical_path"], os.path.join(logical, "owner", "repo"))
            self.assertEqual(records[0]["real_path"], os.path.realpath(repo))

    def test_inventory_reports_broken_repository_and_keeps_healthy_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            healthy = os.path.join(tmp, "healthy")
            broken = os.path.join(tmp, "broken")
            os.makedirs(healthy)
            os.makedirs(broken)
            git(healthy, "init")
            git(broken, "init")

            profile = type("Profile", (), {"roots": [tmp]})()
            errors = []
            original = repoops.repository_record

            def record(repository, *args, **kwargs):
                if repository.logical_path == broken:
                    raise gitrepos.GitRepoError("missing object for test ref")
                return original(repository, *args, **kwargs)

            with mock.patch.object(repoops, "repository_record", side_effect=record):
                records = repoops.inventory(profile, errors=errors)

            self.assertEqual(
                [record["logical_path"] for record in records],
                [healthy],
            )
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["logical_path"], broken)
            self.assertIn("missing object", errors[0]["error"])

    def test_inventory_wip_id_respects_disabled_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            clean = os.path.join(tmp, "clean")
            os.makedirs(repo)
            os.makedirs(clean)
            git(repo, "init")
            git(clean, "init")
            with open(os.path.join(repo, "untracked.txt"), "w") as stream:
                stream.write("local work\n")
            profile = GitProfile(
                enabled=True,
                roots=[tmp],
                staged=False,
                unstaged=False,
                untracked=False,
                ignored=False,
            )

            records = {
                record["logical_path"]: record
                for record in repoops.inventory(profile)
            }

            self.assertTrue(records[repo]["dirty"])
            self.assertFalse(records[clean]["dirty"])
            self.assertEqual(
                records[repo]["wip_id"],
                records[clean]["wip_id"],
            )

    def test_nested_repository_is_inventoried_but_not_relocated(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = os.path.join(tmp, "Owner", "Parent")
            nested = os.path.join(parent, ".build", "checkouts", "Nested")
            os.makedirs(parent)
            os.makedirs(nested)
            git(parent, "init")
            git(nested, "init")
            git(nested, "remote", "add", "origin", "https://github.com/Other/Nested")
            profile = GitProfile(enabled=True, roots=[tmp])

            records = {
                record["logical_path"]: record
                for record in repoops.inventory(profile)
            }

            self.assertTrue(records[nested]["nested"])
            self.assertEqual(
                records[nested]["parent_identity"],
                records[parent]["identity"],
            )
            self.assertIsNone(
                repoops.relocation_target(records[nested], [tmp])
            )

    def test_relocation_dry_run_does_not_create_owner_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "GitHub")
            source = os.path.join(root, "OldOwner", "Repo")
            target = os.path.join(root, "NewOwner", "Repo")
            os.makedirs(source)
            git(source, "init")

            result = repoops.relocate_repository(
                gitrepos.open_repository(source), target, dry_run=True
            )

            self.assertTrue(result["dry_run"])
            self.assertFalse(os.path.lexists(os.path.dirname(target)))

    def test_cross_device_relocation_fails_before_creating_owner_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "GitHub")
            source = os.path.join(root, "OldOwner", "Repo")
            target = os.path.join(root, "NewOwner", "Repo")
            os.makedirs(source)
            git(source, "init")

            def device(path):
                return 1 if os.path.realpath(path) == os.path.realpath(source) else 2

            with mock.patch.object(repoops, "_device", side_effect=device):
                with self.assertRaisesRegex(
                    repoops.RepositoryLayoutError, "cross-device"
                ):
                    repoops.relocate_repository(
                        gitrepos.open_repository(source), target
                    )

            self.assertTrue(os.path.isdir(source))
            self.assertFalse(os.path.lexists(os.path.dirname(target)))

    def test_relocation_is_blocked_by_active_git_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "GitHub")
            source = os.path.join(root, "OldOwner", "Repo")
            target = os.path.join(root, "NewOwner", "Repo")
            os.makedirs(source)
            git(source, "init")
            with open(os.path.join(source, ".git", "index.lock"), "w"):
                pass

            with self.assertRaises(gitrepos.UnsafeSnapshotError):
                repoops.relocate_repository(
                    gitrepos.open_repository(source), target
                )

            self.assertTrue(os.path.isdir(source))
            self.assertFalse(os.path.lexists(target))

    def test_relocation_preserves_owner_symlink_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "GitHub")
            storage = os.path.join(tmp, "ExternalGitHub")
            source = os.path.join(root, "OldOwner", "Repo")
            target = os.path.join(root, "NewOwner", "Repo")
            os.makedirs(os.path.join(storage, "OldOwner", "Repo"))
            os.makedirs(root)
            os.symlink(
                os.path.join(storage, "OldOwner"),
                os.path.join(root, "OldOwner"),
            )
            git(source, "init")

            result = repoops.relocate_repository(
                gitrepos.open_repository(source), target
            )

            self.assertTrue(result["moved"])
            self.assertTrue(os.path.islink(os.path.join(root, "NewOwner")))
            self.assertTrue(os.path.isdir(
                os.path.join(storage, "NewOwner", "Repo", ".git")
            ))

    def test_initialize_checkout_accepts_canonical_origin_in_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "GitHub", "Owner", "Repo")
            os.makedirs(os.path.join(tmp, "GitHub"))

            result = repoops.initialize_checkout(
                target, "github.com/Owner/Repo", dry_run=True
            )

            self.assertTrue(result["dry_run"])
            self.assertFalse(os.path.lexists(target))

    def test_initialize_checkout_uses_unborn_bootstrap_branch_and_reuses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "GitHub", "Owner", "Repo")
            os.makedirs(os.path.join(tmp, "GitHub"))

            created = repoops.initialize_checkout(
                target, "github.com/Owner/Repo"
            )
            head = subprocess.run(
                ["git", "-C", target, "symbolic-ref", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            reused = repoops.initialize_checkout(
                target, "https://github.com/Owner/Repo.git"
            )

            self.assertTrue(created["initialized"])
            self.assertEqual(head, "refs/heads/chatmesh-bootstrap")
            self.assertTrue(reused["reused"])
            self.assertFalse(reused["initialized"])

    def test_checkout_materializes_safely_after_received_current_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            os.makedirs(source)
            os.makedirs(target)
            git(source, "init")
            git(source, "branch", "-M", "main")
            git(source, "config", "user.name", "Test")
            git(source, "config", "user.email", "test@example.com")
            with open(os.path.join(source, "tracked.txt"), "w") as stream:
                stream.write("tracked\n")
            git(source, "add", "tracked.txt")
            git(source, "commit", "-m", "initial")

            git(target, "init")
            git(target, "symbolic-ref", "HEAD", "refs/heads/main")
            git(target, "remote", "add", "origin", source)
            git(target, "fetch", "origin", "main")
            git(target, "update-ref", "refs/heads/main", "FETCH_HEAD")

            result = repoops.checkout_initialized_branch(target, "main")

            self.assertGreater(result["materialized"], 0)
            with open(os.path.join(target, "tracked.txt")) as stream:
                self.assertEqual(stream.read(), "tracked\n")
            status = subprocess.run(
                ["git", "-C", target, "status", "--porcelain"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout
            self.assertEqual(status, "")

    def test_canonical_paths_use_owner_repo_and_worktree_convention(self):
        roots = ["/Users/test/Documents/GitHub"]
        record = {
            "origin": "github.com/NewOwner/NewName",
            "kind": "worktree",
        }
        self.assertEqual(
            repoops.relocation_target(record, roots),
            "/Users/test/Documents/GitHub/NewOwner/NewName",
        )
        linked = dict(record, kind="linked-worktree", branch="mhadi/fix/safe-sync")
        self.assertEqual(
            repoops.relocation_target(linked, roots),
            "/Users/test/Documents/GitHub/NewOwner/NewName/.worktrees/fix-safe-sync",
        )
        external = dict(record, relative_path="external/OldName")
        self.assertEqual(
            repoops.relocation_target(external, roots),
            "/Users/test/Documents/GitHub/external/NewName",
        )

    def test_submodule_is_not_relocated_out_of_parent(self):
        record = {
            "origin": "github.com/vendor/dependency",
            "kind": "submodule",
            "branch": None,
        }
        self.assertIsNone(repoops.relocation_target(record, ["/GitHub"]))

    def test_main_checkout_relocation_repairs_embedded_venv_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "GitHub", "OldOwner", "OldName")
            target = os.path.join(tmp, "GitHub", "NewOwner", "NewName")
            os.makedirs(source)
            git(source, "init")
            activate = os.path.join(source, ".venv", "bin", "activate")
            os.makedirs(os.path.dirname(activate))
            with open(activate, "w") as stream:
                stream.write("VIRTUAL_ENV=%s/.venv\n" % source)
            checkout_git = os.path.join(
                source, ".build", "checkouts", "dependency", ".git"
            )
            alternates = os.path.join(
                checkout_git, "objects", "info", "alternates"
            )
            os.makedirs(os.path.dirname(alternates))
            with open(os.path.join(checkout_git, "config"), "w") as stream:
                stream.write("[remote \"origin\"]\n\turl = %s/.build/repositories/dependency\n" % source)
            with open(alternates, "w") as stream:
                stream.write("%s/.build/repositories/dependency/objects\n" % source)
            result = repoops.relocate_repository(
                gitrepos.open_repository(source), target
            )
            self.assertTrue(result["moved"])
            self.assertFalse(os.path.exists(source))
            self.assertTrue(os.path.isdir(target))
            with open(os.path.join(target, ".venv", "bin", "activate")) as stream:
                self.assertIn(target, stream.read())
            moved_checkout = os.path.join(
                target, ".build", "checkouts", "dependency", ".git"
            )
            with open(os.path.join(moved_checkout, "config")) as stream:
                self.assertIn(target, stream.read())
            with open(os.path.join(
                moved_checkout, "objects", "info", "alternates"
            )) as stream:
                self.assertIn(target, stream.read())


if __name__ == "__main__":
    unittest.main()
