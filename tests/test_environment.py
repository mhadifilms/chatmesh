from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chatmesh import environment
from chatmesh.config import EnvironmentProfile


def completed(argv=None, returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(
        argv or [], returncode, stdout=stdout, stderr=stderr
    )


class EnvironmentInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.root = self.home / "Code"
        self.project = self.root / "project"
        self.venv = self.project / ".venv"
        self.venv.mkdir(parents=True)
        (self.venv / "pyvenv.cfg").write_text(
            "version = 3.10.14\n", encoding="utf-8"
        )
        (self.project / "requirements.txt").write_text(
            "requests==2.32.0\n", encoding="utf-8"
        )
        self.brewfile = self.home / "Brewfile"
        self.brewfile.write_text(
            'tap "homebrew/cask"\nbrew "ripgrep"\ncask "iterm2"\n',
            encoding="utf-8",
        )
        self.profile = EnvironmentProfile(
            enabled=True,
            homebrew=True,
            brewfile=str(self.brewfile),
            python=False,
            pip=False,
            pipx=False,
            uv=False,
            venvs=True,
            roots=[str(self.root)],
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_inventory_uses_brewfile_and_restorable_venv(self):
        with mock.patch.object(
            environment, "_command_available", return_value=True
        ):
            first = environment.snapshot_environment(
                self.profile,
                home=str(self.home),
                runner=lambda argv, **kwargs: completed(argv),
            )
            second = environment.snapshot_environment(
                self.profile,
                home=str(self.home),
                runner=lambda argv, **kwargs: completed(argv),
            )

        self.assertEqual(first["brew"]["formulae"], ["ripgrep"])
        self.assertEqual(first["brew"]["casks"], ["iterm2"])
        self.assertEqual(first["brew"]["taps"], ["homebrew/cask"])
        descriptor = first["venvs"]["~/Code/project/.venv"]
        self.assertTrue(descriptor["restorable"])
        self.assertEqual(descriptor["kind"], "requirements")
        self.assertEqual(descriptor["python_major_minor"], "3.10")
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])

    def test_oversized_declaration_is_blocked(self):
        self.profile.max_lock_file_bytes = 4
        self.profile.homebrew = False
        snapshot = environment.snapshot_environment(
            self.profile, home=str(self.home)
        )
        descriptor = snapshot["venvs"]["~/Code/project/.venv"]
        self.assertFalse(descriptor["restorable"])
        self.assertTrue(any(
            "limit" in item["reason"] for item in snapshot["blocked"]
        ))

    def test_transitive_requirements_and_custom_tap_origins_are_blocked(self):
        (self.project / "requirements.txt").write_text(
            "-r more.txt\n", encoding="utf-8"
        )
        self.brewfile.write_text(
            'tap "corp/tools", "ssh://internal/tools.git"\n',
            encoding="utf-8",
        )
        with mock.patch.object(
            environment, "_command_available", return_value=False
        ):
            snapshot = environment.snapshot_environment(
                self.profile, home=str(self.home)
            )
        self.assertFalse(
            snapshot["venvs"]["~/Code/project/.venv"]["restorable"]
        )
        reasons = [item["reason"] for item in snapshot["blocked"]]
        self.assertTrue(any("flat pinned" in reason for reason in reasons))
        self.assertTrue(any("custom tap" in reason for reason in reasons))

    def test_inventory_marks_malformed_tools_and_symlink_escape_incomplete(self):
        outside = self.home / "outside"
        outside.mkdir()
        owner = self.root / "owner"
        owner.mkdir()
        os.symlink(outside, owner / "escaped")
        profile = EnvironmentProfile(
            enabled=True,
            homebrew=False,
            python=False,
            pip=True,
            pipx=True,
            uv=False,
            venvs=True,
            roots=[str(self.root)],
        )

        def runner(argv, **_kwargs):
            if argv[0] == "pipx":
                return completed(
                    argv, stdout=b'{"venvs":{"../invalid":{}}}'
                )
            return completed(argv, stdout=b"[{}]")

        with mock.patch.object(
            environment, "_command_available", return_value=True
        ):
            snapshot = environment.snapshot_environment(
                profile, home=str(self.home), runner=runner
            )
        self.assertFalse(snapshot["complete"]["pip"])
        self.assertFalse(snapshot["complete"]["pipx"])
        self.assertFalse(snapshot["complete"]["venvs"])
        self.assertTrue(any(
            "escapes configured root" in item["reason"]
            for item in snapshot["blocked"]
        ))


class EnvironmentPlanTests(unittest.TestCase):
    def profile(self):
        return EnvironmentProfile(
            enabled=True,
            roots=["/tmp/code"],
        )

    def manifest(self, **updates):
        value = {
            "version": 1,
            "brew": {
                "formulae": [],
                "casks": [],
                "taps": [],
                "installed_formulae": [],
                "installed_casks": [],
                "installed_taps": [],
            },
            "python": {"version": "3.10.1", "major_minor": "3.10"},
            "pip": [],
            "pipx": [],
            "uv": [],
            "venvs": {},
            "blocked": [],
            "complete": {
                "brew": True,
                "pip": True,
                "pipx": True,
                "uv": True,
                "venvs": True,
            },
        }
        value.update(updates)
        value["snapshot_id"] = environment.manifest_snapshot_id(value)
        return value

    def test_additive_plan_installs_missing_and_never_removes_extras(self):
        source = self.manifest(
            brew={
                "formulae": ["ffmpeg"],
                "casks": [],
                "taps": [],
                "installed_formulae": ["ffmpeg"],
                "installed_casks": [],
                "installed_taps": [],
            },
            pip=["ruff"],
            pipx=["poetry"],
            uv=["pre-commit"],
        )
        destination = self.manifest(
            brew={
                "formulae": ["jq"],
                "casks": [],
                "taps": [],
                "installed_formulae": ["jq"],
                "installed_casks": [],
                "installed_taps": [],
            },
            pip=["local-only"],
        )

        plan = environment.plan_environment(
            source, destination, self.profile()
        )

        self.assertEqual(plan["counts"]["install"], 4)
        self.assertEqual(
            {item["kind"] for item in plan["actions"]},
            {"brew-formula", "pip-user", "pipx", "uv-tool"},
        )
        self.assertFalse(any(
            item.get("kind") in ("remove", "uninstall", "upgrade")
            for item in plan["actions"]
        ))

    def test_existing_venv_difference_is_conflict_not_recreate(self):
        source_descriptor = {
            "path": "/tmp/code/project/.venv",
            "project": "/tmp/code/project",
            "kind": "requirements",
            "files": ["requirements.txt"],
            "lock_sha256": "a" * 64,
            "python_major_minor": "3.10",
            "restorable": True,
        }
        destination_descriptor = dict(
            source_descriptor, lock_sha256="b" * 64
        )
        source = self.manifest(
            venvs={source_descriptor["path"]: source_descriptor}
        )
        destination = self.manifest(
            venvs={source_descriptor["path"]: destination_descriptor}
        )

        plan = environment.plan_environment(
            source, destination, self.profile()
        )

        self.assertFalse(plan["actions"])
        self.assertEqual(plan["counts"]["conflict"], 1)

    def test_python_runtime_difference_is_report_only(self):
        source = self.manifest(
            python={"version": "3.12.1", "major_minor": "3.12"}
        )
        destination = self.manifest(
            python={"version": "3.10.1", "major_minor": "3.10"}
        )
        plan = environment.plan_environment(
            source, destination, self.profile()
        )
        self.assertFalse(plan["actions"])
        self.assertEqual(plan["blocked"][0]["kind"], "python-runtime")

    def test_brewfile_desire_is_compared_with_installed_state(self):
        source = self.manifest(brew={
            "formulae": ["ripgrep"],
            "casks": [],
            "taps": [],
            "installed_formulae": ["ripgrep"],
            "installed_casks": [],
            "installed_taps": [],
        })
        destination = self.manifest(brew={
            "formulae": ["ripgrep"],
            "casks": [],
            "taps": [],
            "installed_formulae": [],
            "installed_casks": [],
            "installed_taps": [],
        })
        plan = environment.plan_environment(
            source, destination, self.profile()
        )
        self.assertEqual(
            plan["actions"],
            [{"kind": "brew-formula", "name": "ripgrep"}],
        )

    def test_incomplete_destination_and_malformed_manifest_fail_closed(self):
        source = self.manifest(pip=["ruff"])
        destination = self.manifest()
        destination["complete"]["pip"] = False
        destination["snapshot_id"] = environment.manifest_snapshot_id(
            destination
        )
        plan = environment.plan_environment(
            source, destination, self.profile()
        )
        self.assertFalse(plan["actions"])
        self.assertEqual(plan["blocked"][0]["kind"], "pip-user")

        malformed = dict(source, pip="ruff")
        malformed["snapshot_id"] = environment.manifest_snapshot_id(
            malformed
        )
        with self.assertRaises(environment.EnvironmentSyncError):
            environment.plan_environment(
                malformed, destination, self.profile()
            )


class EnvironmentApplyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.root = self.home / "Code"
        self.project = self.root / "project"
        self.project.mkdir(parents=True)
        self.requirements = self.project / "requirements.txt"
        self.requirements.write_text("requests==2.32.0\n", encoding="utf-8")
        self.profile = EnvironmentProfile(
            enabled=True,
            roots=[str(self.root)],
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_package_apply_uses_argument_vectors_without_removals(self):
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            return completed(argv)

        plan = {
            "actions": [
                {"kind": "brew-formula", "name": "ripgrep"},
                {"kind": "pipx", "name": "poetry"},
                {"kind": "uv-tool", "name": "ruff"},
            ],
            "conflicts": [],
            "blocked": [],
        }
        result = environment.apply_environment_plan(
            self.profile, plan, home=str(self.home), runner=runner
        )

        self.assertTrue(result["ok"])
        self.assertEqual(calls[0], ["brew", "install", "ripgrep"])
        self.assertFalse(any(
            word in ("remove", "uninstall") for call in calls for word in call
        ))

    def test_missing_venv_is_created_from_unchanged_requirements(self):
        target = self.project / ".venv"
        digest = environment._declaration_digest(
            str(self.project), ["requirements.txt"], 1024
        )
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            if argv[:3] == [sys.executable, "-m", "venv"]:
                Path(argv[3], "bin").mkdir(parents=True, exist_ok=True)
                Path(argv[3], "bin", "python").touch()
                Path(argv[3], "bin", "tool").write_text(
                    "#!%s/bin/python\n" % argv[3], encoding="utf-8"
                )
                dist_info = Path(
                    argv[3],
                    "lib",
                    "python%d.%d" % sys.version_info[:2],
                    "site-packages",
                    "tool.dist-info",
                )
                dist_info.mkdir(parents=True)
                Path(dist_info, "RECORD").write_text(
                    "../../../bin/tool,sha256=old,1\n",
                    encoding="utf-8",
                )
            return completed(argv)

        plan = {
            "actions": [{
                "kind": "venv-create",
                "path": "~/Code/project/.venv",
                "venv": {
                    "path": "~/Code/project/.venv",
                    "project": "~/Code/project",
                    "kind": "requirements",
                    "files": ["requirements.txt"],
                    "lock_sha256": digest,
                    "python_major_minor": "%d.%d" % sys.version_info[:2],
                },
            }],
            "conflicts": [],
            "blocked": [],
        }
        result = environment.apply_environment_plan(
            self.profile, plan, home=str(self.home), runner=runner
        )

        self.assertTrue(result["ok"])
        self.assertTrue(target.is_dir())
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(
            os.path.realpath(calls[0][3]), os.path.realpath(target)
        )
        self.assertEqual(
            (target / "bin" / "tool").read_text(encoding="utf-8"),
            "#!%s/bin/python\n" % os.path.realpath(target),
        )
        record = next(target.glob(
            "lib/python*/site-packages/tool.dist-info/RECORD"
        )).read_text(encoding="utf-8")
        self.assertIn("../../../bin/tool,sha256=", record)
        self.assertNotIn("sha256=old", record)

    def test_changed_declaration_fails_before_creating_target(self):
        digest = environment._declaration_digest(
            str(self.project), ["requirements.txt"], 1024
        )
        self.requirements.write_text("different==1\n", encoding="utf-8")
        plan = {
            "actions": [{
                "kind": "venv-create",
                "path": "~/Code/project/.venv",
                "venv": {
                    "path": "~/Code/project/.venv",
                    "project": "~/Code/project",
                    "kind": "requirements",
                    "files": ["requirements.txt"],
                    "lock_sha256": digest,
                    "python_major_minor": "%d.%d" % sys.version_info[:2],
                },
            }],
            "conflicts": [],
            "blocked": [],
        }

        result = environment.apply_environment_plan(
            self.profile,
            plan,
            home=str(self.home),
            runner=lambda argv, **kwargs: completed(argv),
        )

        self.assertFalse(result["ok"])
        self.assertFalse((self.project / ".venv").exists())
        self.assertIn("changed", result["failed"][0]["reason"])

    def test_existing_target_and_nested_symlink_escape_are_preserved(self):
        target = self.project / ".venv"
        target.mkdir()
        sentinel = target / "keep"
        sentinel.write_text("local\n", encoding="utf-8")
        descriptor = {
            "path": "~/Code/project/.venv",
            "project": "~/Code/project",
            "kind": "requirements",
            "files": ["requirements.txt"],
            "lock_sha256": environment._declaration_digest(
                str(self.project), ["requirements.txt"], 1024
            ),
            "python_major_minor": "%d.%d" % sys.version_info[:2],
        }
        plan = {
            "actions": [{
                "kind": "venv-create",
                "path": descriptor["path"],
                "venv": descriptor,
            }],
            "conflicts": [],
            "blocked": [],
        }
        result = environment.apply_environment_plan(
            self.profile,
            plan,
            home=str(self.home),
            runner=lambda argv, **kwargs: completed(argv),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "local\n")

        outside = self.home / "outside"
        outside.mkdir()
        owner = self.root / "owner"
        owner.mkdir()
        os.symlink(outside, owner / "project")
        escaped = dict(
            descriptor,
            path="~/Code/owner/project/.venv",
            project="~/Code/owner/project",
        )
        escape_plan = {
            "actions": [{
                "kind": "venv-create",
                "path": escaped["path"],
                "venv": escaped,
            }],
            "conflicts": [],
            "blocked": [],
        }
        escaped_result = environment.apply_environment_plan(
            self.profile,
            escape_plan,
            home=str(self.home),
            runner=lambda argv, **kwargs: completed(argv),
        )
        self.assertFalse(escaped_result["ok"])
        self.assertFalse((outside / ".venv").exists())

    def test_failed_venv_creation_leaves_visible_recovery_marker(self):
        digest = environment._declaration_digest(
            str(self.project), ["requirements.txt"], 1024
        )
        plan = {
            "actions": [{
                "kind": "venv-create",
                "path": "~/Code/project/.venv",
                "venv": {
                    "path": "~/Code/project/.venv",
                    "project": "~/Code/project",
                    "kind": "requirements",
                    "files": ["requirements.txt"],
                    "lock_sha256": digest,
                    "python_major_minor": "%d.%d" % sys.version_info[:2],
                },
            }],
            "conflicts": [],
            "blocked": [],
        }
        result = environment.apply_environment_plan(
            self.profile,
            plan,
            home=str(self.home),
            runner=lambda argv, **kwargs: completed(
                argv, returncode=1, stderr=b"failed"
            ),
        )
        self.assertFalse(result["ok"])
        recovery = list(self.project.glob(
            ".chatmesh-venv-*/.chatmesh-incomplete.json"
        ))
        self.assertEqual(len(recovery), 1)

        snapshot_profile = EnvironmentProfile(
            enabled=True,
            homebrew=False,
            python=False,
            pip=False,
            pipx=False,
            uv=False,
            venvs=True,
            roots=[str(self.root)],
        )
        snapshot = environment.snapshot_environment(
            snapshot_profile, home=str(self.home)
        )
        self.assertFalse(snapshot["venvs"])
        self.assertTrue(any(
            "recovery directory" in item["reason"]
            for item in snapshot["blocked"]
        ))


if __name__ == "__main__":
    unittest.main()
