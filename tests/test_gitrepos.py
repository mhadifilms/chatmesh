from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chatmesh import gitrepos


GIT_ENV = dict(
    os.environ,
    GIT_AUTHOR_NAME="Chatmesh Test",
    GIT_AUTHOR_EMAIL="chatmesh@example.invalid",
    GIT_COMMITTER_NAME="Chatmesh Test",
    GIT_COMMITTER_EMAIL="chatmesh@example.invalid",
    LC_ALL="C",
)


class GitReposTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(
        self,
        repo: Path,
        *args: str,
        input_data: bytes = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=GIT_ENV,
        )
        if check and proc.returncode:
            self.fail(
                "git %s failed (%d): %s"
                % (" ".join(args), proc.returncode, proc.stderr.decode(errors="replace"))
            )
        return proc

    def init_repo(self, path: Path, filename: str = "tracked.txt") -> Path:
        path.mkdir(parents=True)
        self.git(path, "init", "-b", "main")
        (path / filename).write_text("base\n")
        self.git(path, "add", filename)
        self.git(path, "commit", "-m", "initial")
        return path

    def commit_file(self, repo: Path, path: str, content: str, message: str) -> str:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.git(repo, "add", path)
        self.git(repo, "commit", "-m", message)
        return self.git(repo, "rev-parse", "HEAD").stdout.decode().strip()

    def test_cycle_safe_discovery_preserves_logical_and_real_paths(self) -> None:
        physical_owner = self.root / "physical" / "acme"
        main = self.init_repo(physical_owner / "main")
        nested = self.init_repo(main / "vendor" / "nested")
        archived_metadata = main / ".git.old"
        archived_metadata.mkdir()
        self.git(archived_metadata, "init", "--bare")
        bare = physical_owner / "archive.git"
        bare.mkdir()
        self.git(bare, "init", "--bare")

        linked = physical_owner / "linked"
        self.git(main, "worktree", "add", "-b", "linked", str(linked))

        module_source = self.init_repo(self.root / "module-source")
        self.git(
            main,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(module_source),
            "deps/module",
        )

        # A directory symlink is the configured logical owner namespace.
        logical_root = self.root / "logical"
        logical_root.mkdir()
        os.symlink(physical_owner, logical_root / "acme")
        # This cycle must terminate without suppressing the intended alias.
        os.symlink(physical_owner, physical_owner / "cycle")

        discovered = gitrepos.discover_repositories(logical_root)
        by_logical = {Path(repo.logical_path): repo for repo in discovered}
        logical_main = logical_root / "acme" / "main"
        logical_nested = logical_main / "vendor" / "nested"
        logical_module = logical_main / "deps" / "module"

        self.assertIn(logical_main, by_logical)
        self.assertIn(logical_nested, by_logical)
        self.assertIn(logical_module, by_logical)
        self.assertIn(logical_root / "acme" / "linked", by_logical)
        self.assertIn(logical_root / "acme" / "archive.git", by_logical)
        self.assertNotIn(logical_main / ".git.old", by_logical)
        self.assertEqual(by_logical[logical_main].real_path, str(main.resolve()))
        self.assertEqual(by_logical[logical_nested].real_path, str(nested.resolve()))
        self.assertEqual(by_logical[logical_module].kind, "submodule")
        self.assertEqual(
            by_logical[logical_root / "acme" / "linked"].kind, "linked-worktree"
        )
        self.assertTrue(by_logical[logical_root / "acme" / "archive.git"].is_bare)
        self.assertLess(len(discovered), 10)

    def test_github_identity_and_repository_metadata(self) -> None:
        urls = (
            "git@github.com:Owner/Repo.git",
            "ssh://git@github.com/Owner/Repo.git",
            "https://token@github.com/Owner/Repo.git?secret=ignored",
        )
        for url in urls:
            self.assertEqual(
                gitrepos.normalize_github_origin(url), "github.com/Owner/Repo"
            )
        self.assertIsNone(
            gitrepos.normalize_github_origin("git@gitlab.com:Owner/Repo.git")
        )

        repo = self.init_repo(self.root / "metadata")
        self.git(
            repo, "remote", "add", "origin", "git@github.com:Owner/Repo.git"
        )
        self.git(repo, "tag", "-a", "v1", "-m", "version one")
        linked = self.root / "metadata-linked"
        self.git(repo, "worktree", "add", "-b", "topic", str(linked))

        metadata = gitrepos.repository_metadata(repo)
        self.assertEqual(metadata.identity, "github:owner/repo")
        self.assertEqual(metadata.origin, "github.com/Owner/Repo")
        self.assertEqual(metadata.branch, "main")
        self.assertEqual({branch.name for branch in metadata.branches}, {"main", "topic"})
        self.assertEqual({tag.name for tag in metadata.tags}, {"v1"})
        topic = next(branch for branch in metadata.branches if branch.name == "topic")
        self.assertEqual(topic.worktrees, (os.path.realpath(str(linked)),))

    def test_github_resolution_uses_stable_id_and_caches_current_slug(self) -> None:
        cache = self.root / "github-cache.json"
        response = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=b'{"nameWithOwner":"NewOwner/NewName","id":"R_123"}',
            stderr=b"",
        )
        with mock.patch.object(gitrepos.shutil, "which", return_value="/usr/bin/gh"), \
                mock.patch.object(gitrepos.subprocess, "run", return_value=response) as run:
            resolved = gitrepos.resolve_github_repository(
                "https://github.com/OldOwner/OldName.git",
                cache,
                allow_gh=True,
            )
            cached = gitrepos.resolve_github_repository(
                "https://github.com/OldOwner/OldName.git",
                cache,
                allow_gh=True,
            )
        self.assertEqual(resolved.slug, "NewOwner/NewName")
        self.assertEqual(resolved.identity, "github-id:R_123")
        self.assertEqual(cached.identity, "github-id:R_123")
        self.assertEqual(run.call_count, 1)

    def _make_wip_source(self) -> Path:
        source = self.init_repo(self.root / "source", filename="mixed.bin")
        (source / "mixed.bin").write_bytes(b"base\x00bytes\n")
        (source / "unstaged.txt").write_text("base unstaged\n")
        (source / "delete.txt").write_text("delete me\n")
        (source / "staged-only.txt").write_text("base staged\n")
        self.git(source, "add", ".")
        self.git(source, "commit", "-m", "fixture files")

        # The index and worktree intentionally hold different binary versions.
        (source / "mixed.bin").write_bytes(b"staged\x00binary\n")
        self.git(source, "add", "mixed.bin")
        (source / "mixed.bin").write_bytes(b"working\x00binary\xff\n")

        (source / "staged-only.txt").write_text("staged version\n")
        self.git(source, "add", "staged-only.txt")
        (source / "unstaged.txt").write_text("unstaged version\n")
        (source / "delete.txt").unlink()
        (source / "untracked.bin").write_bytes(b"\x00\xffuntracked")
        os.symlink("unstaged.txt", source / "untracked-link")

        nested = self.init_repo(source / "nested")
        (nested / "private-wip.txt").write_text("must belong only to nested\n")
        return source

    def test_deterministic_wip_snapshot_captures_partial_staging(self) -> None:
        source = self._make_wip_source()
        first = self.root / "first.zip"
        second = self.root / "second.zip"

        created = gitrepos.create_wip_snapshot(source, first)
        loaded = gitrepos.read_wip_snapshot(first)
        gitrepos.create_wip_snapshot(source, second)

        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(created.snapshot_id, loaded.snapshot_id)
        entries = {entry["path"]: entry for entry in loaded.manifest["entries"]}
        self.assertNotIn("nested", entries)
        self.assertFalse(any(path.startswith("nested/") for path in entries))
        self.assertTrue(entries["mixed.bin"]["staged"])
        self.assertTrue(entries["mixed.bin"]["unstaged"])
        index_payload = loaded.payloads[entries["mixed.bin"]["index"]["sha256"]]
        worktree_payload = loaded.payloads[
            entries["mixed.bin"]["worktree"]["sha256"]
        ]
        self.assertEqual(index_payload, b"staged\x00binary\n")
        self.assertEqual(worktree_payload, b"working\x00binary\xff\n")
        self.assertFalse(entries["delete.txt"]["worktree"]["present"])
        self.assertEqual(entries["untracked-link"]["worktree"]["type"], "symlink")
        self.assertNotIn(str(source), first.read_bytes().decode("latin1"))

    def test_snapshot_limits_and_unsafe_symlink_fail_closed(self) -> None:
        source = self.init_repo(self.root / "limits")
        (source / "large.bin").write_bytes(b"x" * 32)
        with self.assertRaises(gitrepos.SnapshotError):
            gitrepos.create_wip_snapshot(
                source, self.root / "too-large.zip", max_file_bytes=8
            )

        (source / "large.bin").unlink()
        os.symlink("/tmp/outside-chatmesh", source / "escape-link")
        snapshot = gitrepos.create_wip_snapshot(
            source, self.root / "unsafe-link.zip"
        )
        destination = self.root / "unsafe-destination"
        self.git(self.root, "clone", str(source), str(destination))
        with self.assertRaises(gitrepos.UnsafeSnapshotError):
            gitrepos.apply_wip_snapshot(destination, snapshot)

    def test_wip_category_toggles_project_only_selected_state(self) -> None:
        source = self.init_repo(self.root / "category-source")
        (source / ".gitignore").write_text("ignored.txt\n")
        (source / "tracked.txt").write_text("staged\n")
        self.git(source, "add", ".gitignore", "tracked.txt")
        self.git(source, "commit", "-m", "ignore and staged base")
        (source / "tracked.txt").write_text("index version\n")
        self.git(source, "add", "tracked.txt")
        (source / "tracked.txt").write_text("working version\n")
        (source / "ignored.txt").write_text("explicit ignored WIP\n")

        unstaged_only = gitrepos.create_wip_snapshot(
            source,
            self.root / "unstaged-only.zip",
            include_staged=False,
            include_unstaged=True,
            include_untracked=False,
            include_ignored=False,
        )
        destination = self.root / "category-destination"
        self.git(self.root, "clone", str(source), str(destination))
        gitrepos.apply_wip_snapshot(destination, unstaged_only)
        self.assertEqual((destination / "tracked.txt").read_text(), "working version\n")
        self.assertFalse(
            self.git(destination, "diff", "--cached", "--quiet", check=False).returncode
        )
        self.assertFalse((destination / "ignored.txt").exists())

        # A clean second clone can opt in to ignored payloads explicitly.
        ignored_only = gitrepos.create_wip_snapshot(
            source,
            self.root / "ignored-only.zip",
            include_staged=False,
            include_unstaged=False,
            include_untracked=False,
            include_ignored=True,
        )
        ignored_destination = self.root / "ignored-destination"
        self.git(self.root, "clone", str(source), str(ignored_destination))
        gitrepos.apply_wip_snapshot(ignored_destination, ignored_only)
        self.assertEqual(
            (ignored_destination / "ignored.txt").read_text(),
            "explicit ignored WIP\n",
        )

    def test_active_git_lock_blocks_snapshot_apply(self) -> None:
        source = self._make_wip_source()
        snapshot = gitrepos.create_wip_snapshot(source, self.root / "locked.zip")
        destination = self.root / "locked-destination"
        self.git(self.root, "clone", str(source), str(destination))
        # Clone from a dirty source copies the same HEAD but starts clean.
        index_lock = destination / ".git" / "index.lock"
        index_lock.write_text("busy")
        try:
            with self.assertRaises(gitrepos.UnsafeSnapshotError):
                gitrepos.apply_wip_snapshot(destination, snapshot)
        finally:
            index_lock.unlink()

    def test_apply_snapshot_restores_index_and_worktree_and_can_recover(self) -> None:
        source = self._make_wip_source()
        archive = self.root / "wip.zip"
        snapshot = gitrepos.create_wip_snapshot(source, archive)
        target = self.root / "target"
        self.git(self.root, "clone", str(source), str(target))

        result = gitrepos.apply_wip_snapshot(target, archive)
        self.assertTrue(result["ok"])
        self.assertEqual(
            gitrepos.status_fingerprint(target), snapshot.status_fingerprint
        )
        self.assertEqual((target / "mixed.bin").read_bytes(), b"working\x00binary\xff\n")
        self.assertEqual((target / "untracked.bin").read_bytes(), b"\x00\xffuntracked")
        self.assertTrue((target / "untracked-link").is_symlink())
        self.assertFalse((target / "delete.txt").exists())
        staged_blob = self.git(target, "show", ":mixed.bin").stdout
        self.assertEqual(staged_blob, b"staged\x00binary\n")
        self.assertEqual(
            self.git(source, "diff", "--cached", "--binary", "--full-index").stdout,
            self.git(target, "diff", "--cached", "--binary", "--full-index").stdout,
        )

        recovered = gitrepos.recover_wip_journal(target, result["journal"])
        self.assertTrue(recovered["ok"])
        self.assertEqual(
            self.git(target, "status", "--porcelain", "-z").stdout, b""
        )

    def test_apply_can_replace_exactly_matching_prior_snapshot(self) -> None:
        source = self.init_repo(self.root / "prior-source")
        (source / "tracked.txt").write_text("first worktree state\n")
        (source / "first-untracked.txt").write_text("first\n")
        first_path = self.root / "prior-first.zip"
        first = gitrepos.create_wip_snapshot(source, first_path)
        target = self.root / "prior-target"
        self.git(self.root, "clone", str(source), str(target))
        gitrepos.apply_wip_snapshot(target, first)

        (source / "tracked.txt").write_text("second worktree state\n")
        (source / "first-untracked.txt").unlink()
        (source / "second-untracked.txt").write_text("second\n")
        second_path = self.root / "prior-second.zip"
        second = gitrepos.create_wip_snapshot(source, second_path)

        result = gitrepos.apply_wip_snapshot(
            target, second, prior_snapshot=first_path
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            gitrepos.status_fingerprint(target), second.status_fingerprint
        )
        self.assertFalse((target / "first-untracked.txt").exists())
        self.assertEqual(
            (target / "second-untracked.txt").read_text(), "second\n"
        )

    def test_apply_rejects_mismatched_head_without_changes(self) -> None:
        source = self._make_wip_source()
        archive = self.root / "wip.zip"
        gitrepos.create_wip_snapshot(source, archive)
        target = self.root / "mismatched"
        self.git(self.root, "clone", str(source), str(target))
        self.commit_file(target, "other.txt", "different head\n", "different head")
        before = self.git(target, "status", "--porcelain", "-z").stdout

        with self.assertRaises(gitrepos.UnsafeSnapshotError):
            gitrepos.apply_wip_snapshot(target, archive)

        self.assertEqual(
            self.git(target, "status", "--porcelain", "-z").stdout, before
        )

    def test_fast_forward_and_divergence_worktree(self) -> None:
        repo = self.init_repo(self.root / "branches")
        initial = self.git(repo, "rev-parse", "HEAD").stdout.decode().strip()

        self.git(repo, "checkout", "-b", "future")
        future = self.commit_file(repo, "future.txt", "future\n", "future")
        self.git(repo, "checkout", "main")
        self.assertEqual(
            gitrepos.classify_branch_ancestry(repo, "main", "future"),
            gitrepos.Ancestry.FAST_FORWARD,
        )
        ff = gitrepos.fast_forward_branch(repo, "main", future, peer="test")
        self.assertEqual(ff.method, "merge-ff-only")
        self.assertEqual(self.git(repo, "rev-parse", "main").stdout.decode().strip(), future)
        self.assertTrue(ff.backup_ref.startswith("refs/chatmesh/backups/test/main/"))

        self.git(repo, "branch", "dormant", initial)
        update_ref = gitrepos.fast_forward_branch(repo, "dormant", future)
        self.assertEqual(update_ref.method, "update-ref")

        self.git(repo, "checkout", "-b", "left", initial)
        left = self.commit_file(repo, "left.txt", "left\n", "left")
        self.git(repo, "checkout", "-b", "right", initial)
        right = self.commit_file(repo, "right.txt", "right\n", "right")
        self.assertEqual(
            gitrepos.classify_branch_ancestry(repo, left, right),
            gitrepos.Ancestry.DIVERGED,
        )
        resolution = gitrepos.prepare_divergence_worktree(
            repo,
            "left",
            right,
            peer="peer-a",
            base_dir=self.root / "resolution-worktrees",
        )
        self.assertTrue(
            resolution.branch.startswith("mhadi/chore/chatmesh-resolve-")
        )
        self.assertTrue(Path(resolution.path).is_dir())
        self.assertEqual(
            self.git(repo, "rev-parse", resolution.incoming_ref).stdout.decode().strip(),
            right,
        )
        mapped = {
            worktree.branch: worktree.path for worktree in gitrepos.list_worktrees(repo)
        }
        self.assertEqual(
            os.path.realpath(mapped[resolution.branch]),
            os.path.realpath(resolution.path),
        )
        repeated = gitrepos.prepare_divergence_worktree(
            repo,
            "left",
            right,
            peer="peer-a",
            base_dir=self.root / "resolution-worktrees",
        )
        self.assertEqual(repeated, resolution)
        with self.assertRaises(gitrepos.DivergenceError):
            gitrepos.accept_resolution(repo, "left", resolution.branch)
        self.git(Path(resolution.path), "commit", "-m", "resolve histories")
        accepted = gitrepos.accept_resolution(
            repo, "left", resolution.branch, expected_old=left
        )
        self.assertEqual(accepted.new_oid, self.git(
            repo, "rev-parse", resolution.branch
        ).stdout.decode().strip())
        self.assertEqual(
            self.git(repo, "merge-base", "--is-ancestor", right, "left").returncode,
            0,
        )


if __name__ == "__main__":
    unittest.main()
