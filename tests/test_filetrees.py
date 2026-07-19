import io
import os
import tarfile
import tempfile
import time
import unittest
from unittest import mock

from chatmesh import filetrees
from chatmesh.filetrees import safe_path, safe_relpath


class _Input:
    def __init__(self, payload):
        self.buffer = io.BytesIO(payload)


class SafePathTests(unittest.TestCase):
    def test_rejects_archive_traversal_and_git_metadata(self):
        for value in ("../escape", "/absolute", "a/../../b", "a//b",
                      "./a", "repo/.git/config", "a\\b"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    safe_relpath(value)

    def test_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            os.symlink(outside, os.path.join(root, "escape"))
            with self.assertRaises(ValueError):
                safe_path(root, "escape/file")

    def test_allows_safe_relative_path_and_final_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "nested"))
            target = os.path.join(root, "nested", "target")
            with open(target, "w") as stream:
                stream.write("ok")
            os.symlink("target", os.path.join(root, "nested", "link"))
            self.assertEqual(safe_relpath("nested/link"), "nested/link")
            self.assertEqual(
                safe_path(root, "nested/link", allow_final_symlink=True),
                os.path.join(root, "nested", "link"),
            )

    def test_receiver_skips_traversal_and_preserves_executable_mode(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as backup:
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode="w") as archive:
                bad = tarfile.TarInfo("../escape")
                bad_data = b"bad"
                bad.size = len(bad_data)
                bad.mtime = int(time.time()) - 100
                archive.addfile(bad, io.BytesIO(bad_data))
                good = tarfile.TarInfo("hooks/run.sh")
                good_data = b"#!/bin/sh\nexit 0\n"
                good.size = len(good_data)
                good.mode = 0o755
                good.mtime = int(time.time()) - 100
                archive.addfile(good, io.BytesIO(good_data))

            tree = "test-safe-recv"
            filetrees.TREES[tree] = {
                "root": "~/.test-safe-recv",
                "rewrite": False,
                "rename": None,
            }
            try:
                with mock.patch.dict(os.environ, {"CHATMESH_HOME": home}), \
                        mock.patch.object(filetrees.sys, "stdin", _Input(payload.getvalue())), \
                        mock.patch.object(filetrees.sys, "stdout", io.StringIO()):
                    filetrees.cmd_recv(tree, home, backup, 0)
            finally:
                filetrees.TREES.pop(tree, None)

            received = os.path.join(home, ".test-safe-recv", "hooks", "run.sh")
            self.assertTrue(os.path.isfile(received))
            self.assertEqual(os.stat(received).st_mode & 0o777, 0o755)
            self.assertFalse(os.path.exists(os.path.join(home, "escape")))


if __name__ == "__main__":
    unittest.main()
