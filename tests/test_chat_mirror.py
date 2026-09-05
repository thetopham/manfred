from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from manfred_ears.archive import AudioArchive
from manfred_ears.asr import StubASR
from manfred_ears.chat_mirror import ChatMirrorArchive
from manfred_ears.config import Settings
from manfred_ears.service import (
    create_chat_mirror_receiver_app,
    create_operator_app,
)


def observation(
    *,
    session_id: str = "chat-session-1",
    sequence: int = 0,
    text: str = "User: explain the episode contract\nAssistant: Raw evidence remains canonical.",
    observed_at: str = "2026-08-25T20:00:00+00:00",
    event_kind: str = "ui_snapshot",
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "mirror_session_id": session_id,
            "sequence_number": sequence,
            "observed_at": observed_at,
            "clock_basis": "android-system-clock",
            "source_package": "com.openai.chatgpt",
            "event_kind": event_kind,
            "capture_method": "android-accessibility",
            "finality": "provisional",
            "completeness": "partial",
            "observed_text": text,
            "observable_conversation_id": None,
            "interruptions": [],
            "image_refs": [],
            "audio_refs": [],
            "automation_events": [],
            "gaps": [],
            "metadata": {"event_type": 2048, "window_id": 7},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class ChatMirrorArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            state_dir=Path(self.temp.name) / "state",
            auth_token="audio-secret",
            operator_token="operator-secret",
            vision_token="vision-secret",
            chat_mirror_token="chat-secret",
            asr_backend="stub",
        )
        self.archive = ChatMirrorArchive(self.settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_ingest_preserves_exact_payload_and_indexes_observation(self) -> None:
        body = observation()
        received = datetime(2026, 8, 25, 20, 0, 1, tzinfo=timezone.utc)
        result = self.archive.ingest(
            body=body,
            idempotency_key="chat-session-1:0",
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            received_at=received,
        )

        self.assertFalse(result.duplicate)
        record = self.archive.get_event(result.event_id)
        self.assertEqual(record["mirror_session_id"], "chat-session-1")
        self.assertEqual(record["sequence_number"], 0)
        self.assertEqual(record["observed_at"], "2026-08-25T20:00:00+00:00")
        self.assertEqual(record["received_at"], received.isoformat())
        self.assertEqual(record["capture_method"], "android-accessibility")
        self.assertEqual(record["completeness"], "partial")
        self.assertEqual((self.settings.state_dir / record["path"]).read_bytes(), body)
        self.assertEqual(record["payload_sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(self.archive.status(), {"events": 1, "sessions": 1, "raw_bytes": len(body)})

        session = self.archive.get_session("chat-session-1")
        self.assertEqual(session["event_count"], 1)
        self.assertEqual(session["events"][0]["event_id"], result.event_id)
        self.assertIn("Raw evidence remains canonical", session["events"][0]["observed_text"])

        hits = self.archive.search("episode contract")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["mirror_session_id"], "chat-session-1")
        self.assertEqual(hits[0]["sequence_number"], 0)

    def test_retry_dedupes_and_conflicting_key_or_sequence_fails_closed(self) -> None:
        body = observation()
        first = self.archive.ingest(body=body, idempotency_key="chat-key")
        second = self.archive.ingest(body=body, idempotency_key="chat-key")
        self.assertEqual(first.event_id, second.event_id)
        self.assertTrue(second.duplicate)

        with self.assertRaisesRegex(ValueError, "Idempotency-Key"):
            self.archive.ingest(
                body=observation(text="changed body"),
                idempotency_key="chat-key",
            )
        with self.assertRaisesRegex(ValueError, "session sequence"):
            self.archive.ingest(
                body=observation(text="changed body"),
                idempotency_key="different-key",
            )

    def test_claimed_hash_and_schema_validation_fail_closed(self) -> None:
        body = observation()
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.archive.ingest(body=body, idempotency_key="bad-hash", claimed_sha256="0" * 64)
        invalid_package = json.loads(body)
        invalid_package["source_package"] = "com.example.other"
        with self.assertRaisesRegex(ValueError, "source package"):
            self.archive.ingest(
                body=json.dumps(invalid_package).encode(),
                idempotency_key="bad-package",
            )
        invalid_event_kind = json.loads(body)
        invalid_event_kind["event_kind"] = []
        with self.assertRaisesRegex(ValueError, "event kind"):
            self.archive.ingest(
                body=json.dumps(invalid_event_kind).encode(),
                idempotency_key="bad-event-kind-type",
            )
        invalid_field = json.loads(body)
        invalid_field["unexpected"] = "not accepted"
        with self.assertRaisesRegex(ValueError, "unexpected field"):
            self.archive.ingest(
                body=json.dumps(invalid_field).encode(),
                idempotency_key="bad-field",
            )

    def test_concurrent_identical_retries_commit_one_payload(self) -> None:
        body = observation()
        other = ChatMirrorArchive(self.settings)

        def ingest(archive: ChatMirrorArchive):  # type: ignore[no-untyped-def]
            return archive.ingest(body=body, idempotency_key="concurrent-chat")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                future.result()
                for future in (pool.submit(ingest, self.archive), pool.submit(ingest, other))
            ]
        self.assertEqual({result.event_id for result in results}, {results[0].event_id})
        self.assertEqual(sorted(result.duplicate for result in results), [False, True])
        self.assertEqual(self.archive.status()["events"], 1)

    def test_committed_payload_reindexes_after_database_loss(self) -> None:
        body = observation()
        result = self.archive.ingest(body=body, idempotency_key="reindex-chat")
        with sqlite3.connect(self.settings.state_dir / "archive.sqlite3") as connection:
            connection.execute("DELETE FROM chat_mirror_events WHERE event_id=?", (result.event_id,))
            connection.execute("DELETE FROM chat_mirror_fts WHERE event_id=?", (result.event_id,))

        recovered = ChatMirrorArchive(self.settings)
        record = recovered.get_event(result.event_id)
        self.assertEqual(record["payload_sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(recovered.search("canonical")[0]["event_id"], result.event_id)

    def test_startup_reconciles_missing_fts_row_without_duplication(self) -> None:
        result = self.archive.ingest(
            body=observation(),
            idempotency_key="repair-chat-fts",
        )
        with sqlite3.connect(self.settings.state_dir / "archive.sqlite3") as connection:
            connection.execute("DELETE FROM chat_mirror_fts WHERE event_id=?", (result.event_id,))
        recovered = ChatMirrorArchive(self.settings)
        hits = recovered.search("canonical")
        self.assertEqual([hit["event_id"] for hit in hits], [result.event_id])
        recovered_again = ChatMirrorArchive(self.settings)
        hits_again = recovered_again.search("canonical")
        self.assertEqual([hit["event_id"] for hit in hits_again], [result.event_id])

    def test_normal_ingest_uses_constant_time_sequence_reservations(self) -> None:
        self.archive.ingest(
            body=observation(sequence=0),
            idempotency_key="reservation-zero",
        )
        with (
            mock.patch.object(
                self.archive,
                "_committed_sidecar_metadata",
                return_value=None,
            ),
            mock.patch.object(
                Path,
                "glob",
                side_effect=AssertionError("normal ingest scanned sidecar history"),
            ),
        ):
            self.archive.ingest(
                body=observation(sequence=1),
                idempotency_key="reservation-one",
            )
        with sqlite3.connect(self.settings.state_dir / "archive.sqlite3") as connection:
            reservations = connection.execute(
                "SELECT COUNT(*) FROM chat_mirror_sequence_reservations"
            ).fetchone()[0]
        self.assertEqual(reservations, 2)
        self.assertTrue(self.archive.delete_session("chat-session-1"))
        with sqlite3.connect(self.settings.state_dir / "archive.sqlite3") as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM chat_mirror_sequence_reservations"
            ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_unindexed_sidecar_fences_changed_sequence_retry(self) -> None:
        original = observation(text="PRIVATE RESURRECTION SENTINEL")
        with mock.patch.object(
            self.archive,
            "_insert_record",
            side_effect=sqlite3.OperationalError("simulated index failure"),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "simulated index failure"):
                self.archive.ingest(body=original, idempotency_key="crash-key-a")
        self.assertEqual(len(list(self.archive.chat_root.glob("*/chat-*.meta.json"))), 1)

        with self.assertRaisesRegex(ValueError, "session sequence"):
            self.archive.ingest(
                body=observation(text="changed after crash"),
                idempotency_key="crash-key-b",
            )
        recovered_retry = self.archive.ingest(
            body=original,
            idempotency_key="crash-key-b",
        )
        self.assertTrue(recovered_retry.duplicate)
        self.assertEqual(len(list(self.archive.chat_root.glob("*/chat-*.meta.json"))), 1)
        self.assertEqual(self.archive.search("RESURRECTION")[0]["event_id"], recovered_retry.event_id)

    def test_delete_session_removes_unindexed_sidecars_before_reconciliation(self) -> None:
        orphan = observation(sequence=1, text="ORPHAN PRIVATE SENTINEL")
        with mock.patch.object(
            self.archive,
            "_insert_record",
            side_effect=sqlite3.OperationalError("simulated index failure"),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "simulated index failure"):
                self.archive.ingest(body=orphan, idempotency_key="orphan-key")
        indexed = self.archive.ingest(
            body=observation(sequence=0),
            idempotency_key="indexed-key",
        )
        self.assertEqual(self.archive.get_event(indexed.event_id)["mirror_session_id"], "chat-session-1")
        self.assertTrue(self.archive.delete_session("chat-session-1"))
        self.assertEqual(list(self.archive.chat_root.glob("*/chat-*.meta.json")), [])
        recovered = ChatMirrorArchive(self.settings)
        self.assertEqual(recovered.status()["events"], 0)
        self.assertEqual(recovered.search("SENTINEL"), [])

    def test_delete_session_removes_interrupted_temporary_evidence(self) -> None:
        def interrupted_commit(
            payload_path: Path,
            metadata_path: Path,
            body: bytes,
            metadata: dict[str, object],
        ) -> dict[str, object]:
            self.archive._write_bytes(
                metadata_path.with_suffix(".json.tmp"),
                json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )
            self.archive._write_bytes(payload_path.with_suffix(".json.tmp"), body)
            raise RuntimeError("simulated interrupted evidence commit")

        with mock.patch.object(
            self.archive,
            "_commit_evidence",
            side_effect=interrupted_commit,
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated interrupted evidence commit"):
                self.archive.ingest(
                    body=observation(text="TEMPORARY EVIDENCE SENTINEL"),
                    idempotency_key="temporary-evidence",
                )
        self.assertTrue(list(self.archive.chat_root.glob("*/*.json.tmp")))
        self.assertTrue(self.archive.delete_session("chat-session-1"))
        self.assertEqual(list(self.archive.chat_root.glob("*/*.json.tmp")), [])
        recovered = ChatMirrorArchive(self.settings)
        self.assertEqual(recovered.status()["events"], 0)
        self.assertEqual(recovered.search("TEMPORARY"), [])

    def test_delete_session_removes_reservation_left_before_evidence_commit(self) -> None:
        with mock.patch.object(
            self.archive,
            "_commit_evidence",
            side_effect=RuntimeError("simulated crash before evidence commit"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated crash before evidence commit"):
                self.archive.ingest(
                    body=observation(text="RESERVED ONLY SENTINEL"),
                    idempotency_key="reserved-only",
                )
        self.assertEqual(list(self.archive.chat_root.glob("*/chat-*.meta.json")), [])
        self.assertTrue(self.archive.delete_session("chat-session-1"))
        with sqlite3.connect(self.settings.state_dir / "archive.sqlite3") as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM chat_mirror_sequence_reservations"
            ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_tombstone_survives_crash_after_durable_deletion_audit(self) -> None:
        self.archive.ingest(
            body=observation(text="AUDIT ORDER SENTINEL"),
            idempotency_key="audit-order",
        )
        original_audit = self.archive._audit

        def audit_then_crash(action: str, **fields: object) -> None:
            original_audit(action, **fields)
            if action == "chat_mirror_session_deleted":
                raise RuntimeError("simulated crash after audit")

        with mock.patch.object(self.archive, "_audit", side_effect=audit_then_crash):
            with self.assertRaisesRegex(RuntimeError, "simulated crash after audit"):
                self.archive.delete_session("chat-session-1")
        tombstones = list(self.archive.deletion_root.glob("session-*.json"))
        self.assertEqual(len(tombstones), 1)
        audit_rows = [
            json.loads(line)
            for line in (self.settings.state_dir / "audit/events.jsonl").read_text().splitlines()
        ]
        self.assertEqual(audit_rows[-1]["action"], "chat_mirror_session_deleted")

        recovered = ChatMirrorArchive(self.settings)
        self.assertEqual(recovered.status()["events"], 0)
        self.assertEqual(list(recovered.deletion_root.glob("session-*.json")), [])

    def test_tamper_signal_survives_crash_after_evidence_unlink(self) -> None:
        result = self.archive.ingest(
            body=observation(text="PERSISTED TAMPER SENTINEL"),
            idempotency_key="persisted-tamper",
        )
        record = self.archive.get_event(result.event_id)
        payload_path = self.settings.state_dir / record["path"]
        payload_path.write_bytes(b"tampered before delete")

        with mock.patch.object(
            self.archive,
            "_audit",
            side_effect=RuntimeError("simulated crash before deletion audit"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated crash before deletion audit"):
                self.archive.delete_session("chat-session-1")
        self.assertFalse(payload_path.exists())
        tombstone_path = next(self.archive.deletion_root.glob("session-*.json"))
        tombstone = json.loads(tombstone_path.read_text(encoding="utf-8"))
        self.assertEqual(tombstone["tampered_event_ids"], [result.event_id])

        self.assertTrue(self.archive.delete_session("chat-session-1"))
        audit_rows = [
            json.loads(line)
            for line in (self.settings.state_dir / "audit/events.jsonl").read_text().splitlines()
        ]
        self.assertEqual(audit_rows[-1]["action"], "chat_mirror_session_deleted")
        self.assertEqual(audit_rows[-1]["tampered_event_count"], 1)
        recovered = ChatMirrorArchive(self.settings)
        self.assertEqual(recovered.status()["events"], 0)
        self.assertEqual(list(recovered.deletion_root.glob("session-*.json")), [])

    def test_delete_session_removes_tampered_unindexed_sidecar(self) -> None:
        body = observation(sequence=1, text="TAMPERED UNINDEXED SENTINEL")
        with mock.patch.object(
            self.archive,
            "_insert_record",
            side_effect=sqlite3.OperationalError("simulated index failure"),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "simulated index failure"):
                self.archive.ingest(body=body, idempotency_key="tampered-orphan-key")
        metadata_path = next(self.archive.chat_root.glob("*/chat-*.meta.json"))
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload_path = self.settings.state_dir / metadata["path"]
        payload_path.write_bytes(b"tampered after durable commit")

        self.assertTrue(self.archive.delete_session("chat-session-1"))
        self.assertFalse(payload_path.exists())
        self.assertFalse(metadata_path.exists())
        recovered = ChatMirrorArchive(self.settings)
        self.assertEqual(recovered.status()["events"], 0)
        audit_rows = [
            json.loads(line)
            for line in (self.settings.state_dir / "audit/events.jsonl").read_text().splitlines()
        ]
        self.assertEqual(audit_rows[-1]["tampered_event_count"], 1)

    def test_explicit_delete_removes_tampered_evidence_and_audits_it(self) -> None:
        result = self.archive.ingest(
            body=observation(text="TAMPERED PRIVATE SENTINEL"),
            idempotency_key="tampered-delete",
        )
        record = self.archive.get_event(result.event_id)
        payload = self.settings.state_dir / record["path"]
        payload.write_bytes(b"tampered")

        self.assertTrue(self.archive.delete_session("chat-session-1"))
        self.assertFalse(payload.exists())
        self.assertEqual(self.archive.status()["events"], 0)
        audit_rows = [
            json.loads(line)
            for line in (self.settings.state_dir / "audit/events.jsonl").read_text().splitlines()
        ]
        self.assertEqual(audit_rows[-1]["action"], "chat_mirror_session_deleted")
        self.assertEqual(audit_rows[-1]["tampered_event_count"], 1)

    def test_trusted_index_digest_wins_over_consistently_forged_sidecar(self) -> None:
        result = self.archive.ingest(
            body=observation(text="ORIGINAL TRUSTED SENTINEL"),
            idempotency_key="forged-sidecar",
        )
        record = self.archive.get_event(result.event_id)
        payload_path = self.settings.state_dir / record["path"]
        metadata_path = self.settings.state_dir / record["metadata_path"]
        forged_payload = observation(text="FORGED CONSISTENT SENTINEL")
        forged_digest = hashlib.sha256(forged_payload).hexdigest()
        payload_path.write_bytes(forged_payload)
        forged_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        forged_metadata["payload_sha256"] = forged_digest
        forged_metadata["num_bytes"] = len(forged_payload)
        forged_metadata["observed_text"] = "FORGED CONSISTENT SENTINEL"
        metadata_path.write_text(json.dumps(forged_metadata), encoding="utf-8")

        self.assertTrue(self.archive.delete_session("chat-session-1"))
        audit_rows = [
            json.loads(line)
            for line in (self.settings.state_dir / "audit/events.jsonl").read_text().splitlines()
        ]
        self.assertEqual(audit_rows[-1]["tampered_event_count"], 1)
        self.assertFalse(payload_path.exists())
        self.assertFalse(metadata_path.exists())

    def test_metadata_only_sidecar_tampering_is_audited_on_delete(self) -> None:
        result = self.archive.ingest(
            body=observation(text="TRUSTED METADATA SENTINEL"),
            idempotency_key="metadata-only-tamper",
        )
        record = self.archive.get_event(result.event_id)
        metadata_path = self.settings.state_dir / record["metadata_path"]
        forged_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        forged_metadata["observed_text"] = "FORGED METADATA ONLY"
        metadata_path.write_text(json.dumps(forged_metadata), encoding="utf-8")

        self.assertTrue(self.archive.delete_session("chat-session-1"))
        audit_rows = [
            json.loads(line)
            for line in (self.settings.state_dir / "audit/events.jsonl").read_text().splitlines()
        ]
        self.assertEqual(audit_rows[-1]["tampered_event_count"], 1)
        self.assertFalse(metadata_path.exists())

    def test_delete_session_removes_every_duplicate_evidence_path(self) -> None:
        result = self.archive.ingest(
            body=observation(text="DUPLICATE PATH SENTINEL"),
            idempotency_key="duplicate-path",
        )
        record = self.archive.get_event(result.event_id)
        canonical_payload = self.settings.state_dir / record["path"]
        canonical_metadata = self.settings.state_dir / record["metadata_path"]
        duplicate_directory = self.archive.chat_root / "2026-08-26"
        duplicate_directory.mkdir(parents=True, exist_ok=True)
        duplicate_payload = duplicate_directory / f"{result.event_id}.json"
        duplicate_metadata = duplicate_directory / f"{result.event_id}.meta.json"
        duplicate_payload.write_bytes(canonical_payload.read_bytes())
        metadata = json.loads(canonical_metadata.read_text(encoding="utf-8"))
        metadata["path"] = str(duplicate_payload.relative_to(self.settings.state_dir))
        metadata["metadata_path"] = str(duplicate_metadata.relative_to(self.settings.state_dir))
        duplicate_metadata.write_text(json.dumps(metadata), encoding="utf-8")

        self.assertTrue(self.archive.delete_session("chat-session-1"))
        for path in (
            canonical_payload,
            canonical_metadata,
            duplicate_payload,
            duplicate_metadata,
        ):
            self.assertFalse(path.exists())
        recovered = ChatMirrorArchive(self.settings)
        self.assertEqual(recovered.status()["events"], 0)

    def test_delete_session_removes_index_and_exact_raw_payload(self) -> None:
        body = observation()
        result = self.archive.ingest(body=body, idempotency_key="delete-chat")
        record = self.archive.get_event(result.event_id)
        payload = self.settings.state_dir / record["path"]
        self.assertTrue(payload.exists())
        self.assertTrue(self.archive.delete_session("chat-session-1"))
        self.assertFalse(payload.exists())
        self.assertEqual(self.archive.search("canonical"), [])
        self.assertEqual(self.archive.status()["events"], 0)

    def test_new_chat_hierarchy_fsyncs_before_database_index(self) -> None:
        events: list[tuple[str, Path | None]] = []
        original_fsync = self.archive._fsync_directory
        original_insert = self.archive._insert_record
        original_replace = os.replace

        def fsync_directory(path: Path) -> None:
            original_fsync(path)
            events.append(("fsync", path.resolve()))

        def replace_path(source: Path, destination: Path) -> None:
            original_replace(source, destination)
            events.append(("replace", Path(destination).resolve()))

        def insert_record(record):  # type: ignore[no-untyped-def]
            events.append(("insert", None))
            return original_insert(record)

        with mock.patch.object(self.archive, "_fsync_directory", side_effect=fsync_directory):
            with mock.patch("manfred_ears.chat_mirror.os.replace", side_effect=replace_path):
                with mock.patch.object(self.archive, "_insert_record", side_effect=insert_record):
                    self.archive.ingest(body=observation(), idempotency_key="ordered-chat")

        insert_index = events.index(("insert", None))
        payload_replace = next(i for i, event in enumerate(events) if event[0] == "replace")
        fsync_after = next(
            i
            for i, event in enumerate(events)
            if i > payload_replace and event[0] == "fsync"
        )
        self.assertLess(payload_replace, fsync_after)
        self.assertLess(fsync_after, insert_index)


class ChatMirrorReceiverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            state_dir=Path(self.temp.name) / "state",
            auth_token="audio-secret",
            operator_token="operator-secret",
            vision_token="vision-secret",
            chat_mirror_token="chat-secret",
            max_chat_mirror_body_bytes=4096,
            asr_backend="stub",
        )
        self.archive = ChatMirrorArchive(self.settings)
        self.client = TestClient(
            create_chat_mirror_receiver_app(self.settings, archive=self.archive)
        )
        self.operator = TestClient(
            create_operator_app(
                self.settings,
                archive=AudioArchive(self.settings),
                asr=StubASR("unused"),
            )
        )

    def tearDown(self) -> None:
        self.client.close()
        self.operator.close()
        self.temp.cleanup()

    @staticmethod
    def headers(body: bytes, *, token: str = "chat-secret", key: str = "chat-http-1") -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Manfred-Chat-Token": token,
            "Idempotency-Key": key,
            "X-Manfred-Content-SHA256": hashlib.sha256(body).hexdigest(),
        }

    def test_settings_load_chat_token_and_limit_from_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "MANFRED_STATE_DIR": str(self.settings.state_dir),
                "MANFRED_CHAT_MIRROR_TOKEN": "environment-chat-token",
                "MANFRED_MAX_CHAT_MIRROR_BODY_BYTES": "123456",
            },
        ):
            loaded = Settings.from_env()
        self.assertEqual(loaded.chat_mirror_token, "environment-chat-token")
        self.assertEqual(loaded.max_chat_mirror_body_bytes, 123456)

    def test_chat_mirror_unit_has_configured_host_and_isolated_port(self) -> None:
        unit = (
            Path(__file__).resolve().parents[1]
            / "deploy/systemd/manfred-chat-mirror-receiver.service"
        ).read_text()
        default = "Environment=MANFRED_CHAT_MIRROR_HOST=@MANFRED_CHAT_MIRROR_HOST@"
        environment_file = "EnvironmentFile=@MANFRED_ENV_FILE@"
        self.assertIn(default, unit)
        self.assertIn("serve-chat-mirror", unit)
        self.assertIn("--host ${MANFRED_CHAT_MIRROR_HOST} --port 8790", unit)
        self.assertLess(unit.index(default), unit.index(environment_file))
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("ProtectSystem=strict", unit)

    def test_missing_token_refuses_creation_and_surface_is_receiver_only(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "MANFRED_CHAT_MIRROR_TOKEN"):
            create_chat_mirror_receiver_app(
                replace(self.settings, chat_mirror_token=None),
                archive=self.archive,
            )
        for path in ("/health", "/audio", "/noa", "/v1/status", "/docs", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        body = observation()
        self.assertEqual(
            self.client.post("/chat-mirror", content=body, headers=self.headers(body)).status_code,
            202,
        )

    def test_auth_content_type_size_and_hash_validation(self) -> None:
        body = observation()
        self.assertEqual(
            self.client.post(
                "/chat-mirror",
                content=body,
                headers=self.headers(body, token="wrong"),
            ).status_code,
            401,
        )
        wrong_type = self.headers(body)
        wrong_type["Content-Type"] = "text/plain"
        self.assertEqual(
            self.client.post("/chat-mirror", content=body, headers=wrong_type).status_code,
            415,
        )
        wrong_hash = self.headers(body)
        wrong_hash["X-Manfred-Content-SHA256"] = "0" * 64
        self.assertEqual(
            self.client.post("/chat-mirror", content=body, headers=wrong_hash).status_code,
            422,
        )
        invalid_type_payload = json.loads(body)
        invalid_type_payload["event_kind"] = []
        invalid_type = json.dumps(invalid_type_payload).encode()
        self.assertEqual(
            self.client.post(
                "/chat-mirror",
                content=invalid_type,
                headers=self.headers(invalid_type),
            ).status_code,
            422,
        )
        oversized = b"{" + (b"x" * 5000) + b"}"
        self.assertEqual(
            self.client.post(
                "/chat-mirror",
                content=oversized,
                headers={
                    "Content-Type": "application/json",
                    "X-Manfred-Chat-Token": "chat-secret",
                    "Idempotency-Key": "oversized",
                    "X-Manfred-Content-SHA256": hashlib.sha256(oversized).hexdigest(),
                },
            ).status_code,
            413,
        )
        query_only = self.headers(body)
        query_only.pop("X-Manfred-Chat-Token")
        self.assertEqual(
            self.client.post(
                "/chat-mirror?token=chat-secret",
                content=body,
                headers=query_only,
            ).status_code,
            401,
        )

    def test_operator_retrieves_searches_and_deletes_mirrored_session(self) -> None:
        body = observation()
        received = self.client.post(
            "/chat-mirror",
            content=body,
            headers=self.headers(body),
        )
        self.assertEqual(received.status_code, 202)
        operator_headers = {"Authorization": "Bearer operator-secret"}
        status = self.operator.get("/v1/status", headers=operator_headers)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["chat_mirror"]["events"], 1)
        found = self.operator.get(
            "/v1/chat-mirror/search?q=episode+contract",
            headers=operator_headers,
        )
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()["count"], 1)
        malformed = self.operator.get(
            "/v1/chat-mirror/search",
            params={"q": '"'},
            headers=operator_headers,
        )
        self.assertEqual(malformed.status_code, 422)
        session = self.operator.get(
            "/v1/chat-mirror/sessions/chat-session-1",
            headers=operator_headers,
        )
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.json()["events"][0]["observed_text"], json.loads(body)["observed_text"])
        deleted = self.operator.delete(
            "/v1/chat-mirror/sessions/chat-session-1",
            headers=operator_headers,
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["deleted"])


if __name__ == "__main__":
    unittest.main()
