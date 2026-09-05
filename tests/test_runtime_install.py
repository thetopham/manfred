#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install_runtime.py"
spec = importlib.util.spec_from_file_location("install_manfred_runtime", SCRIPT)
assert spec and spec.loader
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class ManfredRuntimeInstallTests(unittest.TestCase):
    def args(self, root, role="manfred-data-plane"):
        args = installer.build_parser().parse_args(["plan", "--role", role, "--skip-venv"])
        args.runtime_root = root / "runtime"
        args.state_dir = root / "archive"
        args.env_file = root / "private/config.env"
        args.bin_dir = root / "bin"
        args.user_unit_dir = root / "units"
        args.brain_inbox_dir = root / "brain-input/wearable"
        args.export_ssh_key = root / "private/id_manfred"
        args.export_source = "capture:/private/archive/wiki-inbox/"
        return args

    def test_plan_is_read_only_and_keeps_existing_data_and_environment_outside_copy(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = installer.plan(self.args(root))
            self.assertFalse(result["environment_file_modified"])
            self.assertFalse(result["data_moved"])
            self.assertFalse(result["services_activated"])
            self.assertEqual(list(root.iterdir()), [])
            self.assertTrue(all(not path.startswith(("apps/", "tests/")) for path in result["source_files"]))

    def test_each_host_role_installs_only_its_units_without_reading_or_changing_secrets(self):
        for role in ("demerzel", "manfred-data-plane", "none"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as raw:
                args = self.args(Path(raw), role)
                args.env_file.parent.mkdir()
                args.env_file.write_text("TOKEN=synthetic-private-test\nPYTHONPATH=/old/runtime\n")
                before = args.env_file.read_bytes()
                with mock.patch.object(installer.subprocess, "run", side_effect=AssertionError("no commands")):
                    installer.apply(args)
                    installer.check(args)
                self.assertEqual(args.env_file.read_bytes(), before)
                self.assertFalse(args.state_dir.exists())
                self.assertFalse(args.brain_inbox_dir.exists())
                installed = {path.name for path in args.user_unit_dir.glob("*")}
                self.assertEqual(installed, set(installer.ROLE_UNITS[role]))
                for name in installed:
                    text = (args.user_unit_dir / name).read_text()
                    self.assertNotIn("@MANFRED_", text)
                    self.assertNotIn("llm-brain", text)
                    if "WorkingDirectory=" in text:
                        self.assertIn(f"WorkingDirectory={args.runtime_root}", text)
                        self.assertIn(f"PYTHONPATH={args.runtime_root}", text)
                self.assertTrue((args.runtime_root / "manfred_ears/archive.py").is_file())
                self.assertTrue((args.bin_dir / "manfred").stat().st_mode & 0o100)

    def test_check_rejects_stale_runtime_source(self):
        with tempfile.TemporaryDirectory() as raw:
            args = self.args(Path(raw))
            installer.apply(args)
            (args.runtime_root / "manfred_ears/config.py").write_text("stale")
            with self.assertRaisesRegex(RuntimeError, "source differs"):
                installer.check(args)

    def test_check_rejects_launcher_with_execute_permission_removed(self):
        with tempfile.TemporaryDirectory() as raw:
            args = self.args(Path(raw))
            installer.apply(args)
            (args.bin_dir / "manfred").chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "launcher is not executable"):
                installer.check(args)

    def test_symlinked_runtime_ancestor_fails_before_copy(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            outside = root / "outside"
            outside.mkdir()
            (root / "link").symlink_to(outside, target_is_directory=True)
            args = self.args(root)
            args.runtime_root = root / "link/runtime"
            with self.assertRaisesRegex(RuntimeError, "symlinks"):
                installer.apply(args)
            self.assertEqual(list(outside.iterdir()), [])

    def test_chat_mirror_unit_is_rendered_for_data_plane_with_distinct_ingest_capability(self):
        with tempfile.TemporaryDirectory() as raw:
            args = self.args(Path(raw), "manfred-data-plane")
            args.chat_mirror_host = "127.0.0.1"
            self.assertIn("manfred-chat-mirror-receiver.service", installer.ROLE_UNITS[args.role])
            self.assertNotIn("manfred-chat-mirror-receiver.service", installer.ROLE_UNITS["demerzel"])
            unit = installer.render_unit("manfred-chat-mirror-receiver.service", args).decode()
            self.assertIn("Environment=MANFRED_CHAT_MIRROR_HOST=127.0.0.1", unit)
            self.assertIn("serve-chat-mirror --host ${MANFRED_CHAT_MIRROR_HOST} --port 8790", unit)
            self.assertIn(f"EnvironmentFile={args.env_file}", unit)
            self.assertIn(f"PYTHONPATH={args.runtime_root}", unit)
            self.assertNotIn("@MANFRED_", unit)

    def test_environment_selects_independent_runtime_and_export_inbox(self):
        with mock.patch.dict(os.environ, {
            "MANFRED_RUNTIME_ROOT": "/configured/manfred",
            "MANFRED_BRAIN_INBOX_DIR": "/configured/brain-inbox",
            "MANFRED_EXPORT_SOURCE": "capture:/configured/exports/",
        }):
            args = installer.build_parser().parse_args(["plan"])
        self.assertEqual(str(args.runtime_root), "/configured/manfred")
        self.assertEqual(str(args.brain_inbox_dir), "/configured/brain-inbox")
        self.assertEqual(args.export_source, "capture:/configured/exports/")


if __name__ == "__main__":
    unittest.main(verbosity=2)
