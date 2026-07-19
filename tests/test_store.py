import os
import tempfile
import unittest

from chatmesh import store


class StoreTests(unittest.TestCase):
    def test_atomic_json_and_conflict_record(self):
        with tempfile.TemporaryDirectory() as state:
            path = store.record_conflict(
                "git", "mini", "github:repo", {"reason": "diverged"},
                state_dir=state,
            )
            self.assertTrue(os.path.isfile(path))
            value = store.read_json(path)
            self.assertEqual(value["detail"]["reason"], "diverged")
            self.assertEqual(value["peer"], "mini")

    def test_slug_is_stable_and_path_safe(self):
        value = store.slug("../../Owner/Repo With Spaces")
        self.assertNotIn("/", value)
        self.assertEqual(value, store.slug("../../Owner/Repo With Spaces"))


if __name__ == "__main__":
    unittest.main()
