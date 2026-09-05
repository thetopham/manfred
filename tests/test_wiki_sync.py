from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "deploy" / "manfred_wiki_sync.py"
SPEC = importlib.util.spec_from_file_location("manfred_wiki_sync", SCRIPT)
assert SPEC and SPEC.loader
SYNC = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SYNC
SPEC.loader.exec_module(SYNC)


def export_text(day: str, body: str = "Observed test phrase.") -> str:
    return (
        f"# Wearable transcript context — {day}\n\n"
        "Local scratch input for daily LLM Wiki distillation. "
        "The searchable transcript archive and raw audio remain the evidence source.\n\n"
        f"{body}\n"
    )


class ManfredWikiSyncTests(unittest.TestCase):
    def test_receiver_unit_uses_configured_host_before_existing_environment_file(self) -> None:
        unit = (SCRIPT.parent / "systemd" / "manfred-ears-receiver.service").read_text()
        default = "Environment=MANFRED_RECEIVER_HOST=@MANFRED_RECEIVER_HOST@"
        environment_file = "EnvironmentFile=@MANFRED_ENV_FILE@"
        self.assertIn(default, unit)
        self.assertIn("--host ${MANFRED_RECEIVER_HOST}", unit)
        self.assertLess(unit.index(default), unit.index(environment_file))

    def test_forward_socket_can_bind_before_tailscale_assigns_its_address(self) -> None:
        unit = (SCRIPT.parent / "systemd" / "manfred-ears-forward.socket").read_text()
        self.assertIn("ListenStream=@MANFRED_FORWARD_LISTEN@", unit)
        self.assertIn("FreeBind=true", unit)

    def test_final_sync_has_bounded_headroom_before_daily_ingest(self) -> None:
        unit = (SCRIPT.parent / "systemd" / "manfred-ears-wiki-sync.timer").read_text()
        self.assertIn("OnCalendar=*-*-* *:03/5:00", unit)
        self.assertIn("OnCalendar=*-*-* 23:52:00", unit)
        self.assertNotIn("*:00/5:00", unit)
        self.assertIn("AccuracySec=1s", unit)
        self.assertIn("RandomizedDelaySec=0", unit)

    def test_validate_export_accepts_contract_and_returns_hash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "2026-08-23.md"
            path.write_text(export_text("2026-08-23"), encoding="utf-8")
            export = SYNC.validate_export(path)
            self.assertEqual(export.payload, path.read_bytes())
            self.assertEqual(len(export.sha256), 64)
            self.assertEqual(export.mtime_ns, path.stat().st_mtime_ns)

    def test_validate_export_rejects_bad_heading_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bad = root / "2026-08-23.md"
            bad.write_text("# wrong\nLocal scratch input for daily LLM Wiki distillation\n")
            with self.assertRaises(SYNC.ExportValidationError):
                SYNC.validate_export(bad)
            target = root / "target"
            target.write_text(export_text("2026-08-24"), encoding="utf-8")
            link = root / "2026-08-24.md"
            link.symlink_to(target)
            with self.assertRaises(SYNC.ExportValidationError):
                SYNC.validate_export(link)

    def test_empty_stage_preserves_last_good_mirror_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(SYNC.ExportValidationError):
                SYNC.load_validated_exports(Path(raw))

    def test_pull_rejects_rsync_size_or_symlink_skip_diagnostics(self) -> None:
        skipped = CompletedProcess(
            args=["rsync"],
            returncode=0,
            stdout='large.md is over max-size (1048577 > 1048576)\n',
            stderr='',
        )
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            SYNC.subprocess, "run", return_value=skipped
        ) as run:
            with self.assertRaisesRegex(SYNC.ExportValidationError, "rsync skipped"):
                SYNC.pull_exports("7090:/inbox/", Path(raw))
            command = run.call_args.args[0]
            self.assertIn("--info=SKIP2", command)
            self.assertIn("--max-size=1m", command)

    def test_pull_uses_validated_mirror_as_incremental_link_destination(self) -> None:
        completed = CompletedProcess(args=["rsync"], returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            SYNC.subprocess, "run", return_value=completed
        ) as run:
            root = Path(raw)
            mirror = root / "mirror"
            mirror.mkdir()
            SYNC.pull_exports("7090:/inbox/", root / "stage", link_destination=mirror)
            command = run.call_args.args[0]
            self.assertIn(f"--link-dest={mirror.resolve()}", command)
            self.assertIn("--checksum", command)

    def test_incremental_pull_detects_same_size_same_mtime_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            mirror = root / "mirror"
            stage = root / "stage"
            source.mkdir()
            mirror.mkdir()
            stage.mkdir()
            source_path = source / "2026-08-23.md"
            mirror_path = mirror / "2026-08-23.md"
            source_path.write_text(export_text("2026-08-23", "alpha"))
            mirror_path.write_text(export_text("2026-08-23", "bravo"))
            self.assertEqual(source_path.stat().st_size, mirror_path.stat().st_size)
            same_mtime_ns = 1_700_000_000_000_000_000
            os.utime(source_path, ns=(same_mtime_ns, same_mtime_ns))
            os.utime(mirror_path, ns=(same_mtime_ns, same_mtime_ns))

            SYNC.pull_exports(f"{source}/", stage, link_destination=mirror)

            staged = stage / "2026-08-23.md"
            self.assertEqual(staged.read_bytes(), source_path.read_bytes())
            self.assertNotEqual(staged.read_bytes(), mirror_path.read_bytes())

    def test_pull_surfaces_empty_markdown_directories_and_unexpected_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            stage = root / "stage"
            source.mkdir()
            stage.mkdir()
            (source / "2026-08-23.md").write_text(export_text("2026-08-23"))
            (source / "2026-08-22.md").mkdir()
            (source / "unexpected.txt").write_text("unexpected")

            SYNC.pull_exports(f"{source}/", stage)

            self.assertTrue((stage / "2026-08-22.md").is_dir())
            self.assertTrue((stage / "unexpected.txt").is_file())
            with self.assertRaisesRegex(SYNC.ExportValidationError, "unexpected staged entries"):
                SYNC.load_validated_exports(stage)

    def test_publish_exchanges_complete_mirror_and_status_in_one_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            destination = root / "wiki-inbox"
            candidate = root / "candidate"
            destination.mkdir()
            candidate.mkdir()
            old = destination / "2026-08-22.md"
            old.write_text(export_text("2026-08-22", "old"))
            (destination / "stale.txt").write_text("stale")
            new = candidate / "2026-08-23.md"
            new.write_text(export_text("2026-08-23", "new"))
            (candidate / "wiki-sync-status.json").write_text('{"schema": 1}\n')
            real_exchange = SYNC._exchange_directories
            observed = {}

            def guarded_exchange(left, right):  # type: ignore[no-untyped-def]
                self.assertEqual(Path(left), candidate)
                self.assertEqual(Path(right), destination)
                self.assertTrue(old.exists())
                self.assertTrue(new.exists())
                self.assertTrue((candidate / "wiki-sync-status.json").is_file())
                observed["called"] = True
                return real_exchange(left, right)

            with mock.patch.object(SYNC, "_exchange_directories", side_effect=guarded_exchange):
                SYNC.publish_staged_mirror(candidate, destination)

            self.assertTrue(observed["called"])
            self.assertTrue((destination / "2026-08-23.md").is_file())
            self.assertTrue((destination / "wiki-sync-status.json").is_file())
            self.assertFalse((destination / "2026-08-22.md").exists())
            self.assertFalse((destination / "stale.txt").exists())
            self.assertTrue((candidate / "2026-08-22.md").is_file())
            self.assertTrue((candidate / "stale.txt").is_file())

    def test_exchange_failure_preserves_last_good_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            destination = root / "wiki-inbox"
            candidate = root / "candidate"
            destination.mkdir()
            candidate.mkdir()
            old = destination / "2026-08-22.md"
            old_payload = export_text("2026-08-22", "old")
            old.write_text(old_payload)
            new = candidate / "2026-08-23.md"
            new.write_text(export_text("2026-08-23", "new"))
            (candidate / "wiki-sync-status.json").write_text('{"schema": 1}\n')

            with mock.patch.object(SYNC, "_exchange_directories", side_effect=OSError("I/O")):
                with self.assertRaises(OSError):
                    SYNC.publish_staged_mirror(candidate, destination)

            self.assertEqual(old.read_text(), old_payload)
            self.assertFalse((destination / "2026-08-23.md").exists())
            self.assertTrue(new.exists())


if __name__ == "__main__":
    unittest.main()
