from __future__ import annotations

import math
import os
import sqlite3
import struct
import sys
import tempfile
import threading
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manfred_ears.archive import AudioArchive, CaptureMetadata, WIKI_EXPORT_MAX_BYTES
from manfred_ears.asr import StubASR
from manfred_ears.config import Settings
from manfred_ears.metrics import wav_metrics, word_error_rate


def pcm(seconds: float = 1.0, sample_rate: int = 16000, frequency: float = 440.0) -> bytes:
    values = [int(4000 * math.sin(2 * math.pi * frequency * index / sample_rate)) for index in range(int(seconds * sample_rate))]
    return struct.pack(f"<{len(values)}h", *values)


class BlockingStubASR(StubASR):
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        super().__init__("snapshot transcript")
        self.started = started
        self.release = release

    def transcribe(self, audio_path: Path):  # type: ignore[no-untyped-def]
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release ASR")
        return super().transcribe(audio_path)


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "state"
        self.settings = Settings(
            state_dir=self.root,
            session_gap_seconds=3,
            idle_flush_seconds=0,
            asr_backend="stub",
        )
        self.archive = AudioArchive(self.settings)
        self.now = datetime(2026, 8, 21, 15, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_ingest_fsyncs_published_chunk_directory(self) -> None:
        with mock.patch.object(
            self.archive,
            "_fsync_directory",
            wraps=self.archive._fsync_directory,
        ) as fsync_directory:
            result = self.archive.ingest_pcm(
                uid="durable-audio",
                sample_rate=16000,
                body=pcm(0.1),
                received_at=self.now,
            )
        with sqlite3.connect(self.archive.db_path) as connection:
            relative = Path(
                connection.execute(
                    "SELECT path FROM chunks WHERE chunk_id=?", (result.chunk_id,)
                ).fetchone()[0]
            )
        self.assertIn(mock.call((self.root / relative).parent), fsync_directory.call_args_list)

    def test_day_export_fsyncs_the_wiki_inbox_generation(self) -> None:
        with mock.patch.object(
            self.archive,
            "_fsync_directory",
            wraps=self.archive._fsync_directory,
        ) as fsync_directory:
            path = self.archive.export_day(self.now.date())
        self.assertIn(mock.call(path.parent), fsync_directory.call_args_list)

    def test_session_deletion_fsyncs_evidence_unlinks_and_directory_removal(self) -> None:
        ingested = self.archive.ingest_pcm(
            uid="delete-durable",
            sample_rate=16000,
            body=pcm(0.1),
            received_at=self.now,
        )
        with sqlite3.connect(self.archive.db_path) as connection:
            relative = Path(
                connection.execute(
                    "SELECT path FROM chunks WHERE chunk_id=?", (ingested.chunk_id,)
                ).fetchone()[0]
            )
        evidence_parent = (self.root / relative).parent
        with mock.patch.object(
            self.archive,
            "_fsync_directory",
            wraps=self.archive._fsync_directory,
        ) as fsync_directory:
            self.assertTrue(self.archive.delete_session(ingested.session_id))
        self.assertIn(mock.call(evidence_parent), fsync_directory.call_args_list)
        self.assertIn(mock.call(evidence_parent.parent), fsync_directory.call_args_list)

    def test_archive_mutation_lock_serializes_ingest_and_delete(self) -> None:
        ingest_started = threading.Event()
        release_ingest = threading.Event()
        delete_finished = threading.Event()

        def blocked_ingest(**_kwargs):  # type: ignore[no-untyped-def]
            ingest_started.set()
            self.assertTrue(release_ingest.wait(timeout=5))
            return mock.sentinel.ingested

        def delete_worker() -> None:
            self.archive.delete_session("missing")
            delete_finished.set()

        with mock.patch.object(self.archive, "_ingest_pcm_unlocked", side_effect=blocked_ingest), mock.patch.object(
            self.archive, "_delete_session_unlocked", return_value=False
        ):
            ingest_thread = threading.Thread(
                target=lambda: self.archive.ingest_pcm(uid="u", sample_rate=16000, body=b"\x00\x00")
            )
            ingest_thread.start()
            self.assertTrue(ingest_started.wait(timeout=5))
            delete_thread = threading.Thread(target=delete_worker)
            delete_thread.start()
            self.assertFalse(delete_finished.wait(timeout=0.1))
            release_ingest.set()
            ingest_thread.join(timeout=5)
            delete_thread.join(timeout=5)
        self.assertTrue(delete_finished.is_set())

    def test_day_export_lock_serializes_same_day_publishers(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        second_finished = threading.Event()

        def blocked_export(_day):  # type: ignore[no-untyped-def]
            first_started.set()
            self.assertTrue(release_first.wait(timeout=5))
            return self.root / "first.md"

        with mock.patch.object(self.archive, "_export_day_unlocked", side_effect=blocked_export):
            first = threading.Thread(target=lambda: self.archive.export_day(self.now.date()))
            first.start()
            self.assertTrue(first_started.wait(timeout=5))

            def second_worker() -> None:
                self.archive.export_day(self.now.date())
                second_finished.set()

            second = threading.Thread(target=second_worker)
            second.start()
            self.assertFalse(second_finished.wait(timeout=0.1))
            release_first.set()
            first.join(timeout=5)
            second.join(timeout=5)
        self.assertTrue(second_finished.is_set())

    def test_wav_assembly_failure_uses_backoff_and_removes_partial_file(self) -> None:
        ingested = self.archive.ingest_pcm(
            uid="corrupt-audio",
            sample_rate=16000,
            body=pcm(),
            received_at=self.now,
        )
        with sqlite3.connect(self.archive.db_path) as connection:
            relative = connection.execute(
                "SELECT path FROM chunks WHERE chunk_id=?", (ingested.chunk_id,)
            ).fetchone()[0]
        raw_path = self.root / relative
        raw_path.write_bytes(raw_path.read_bytes() + b"corrupt")

        with self.assertRaisesRegex(ValueError, "raw chunk hash mismatch"):
            self.archive.transcribe_session(ingested.session_id, StubASR("unused"), force=True)

        with sqlite3.connect(self.archive.db_path) as connection:
            status, failures, next_epoch = connection.execute(
                """SELECT status,transcription_failures,next_transcription_epoch
                   FROM sessions WHERE session_id=?""",
                (ingested.session_id,),
            ).fetchone()
        self.assertEqual(status, "error")
        self.assertEqual(failures, 1)
        self.assertGreater(next_epoch, 0)
        self.assertEqual(list((self.root / "tmp").glob("*.wav")), [])

    def test_idempotency_and_raw_evidence(self) -> None:
        body = pcm()
        first = self.archive.ingest_pcm(
            uid="private-user-id",
            sample_rate=16000,
            body=body,
            idempotency_key="delivery-1",
            received_at=self.now,
        )
        duplicate = self.archive.ingest_pcm(
            uid="private-user-id",
            sample_rate=16000,
            body=body,
            idempotency_key="delivery-1",
            received_at=self.now,
        )
        self.assertFalse(first.duplicate)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(first.chunk_id, duplicate.chunk_id)
        with self.assertRaisesRegex(ValueError, "reused with different audio"):
            self.archive.ingest_pcm(
                uid="private-user-id",
                sample_rate=16000,
                body=b"\x00\x00" * 16000,
                idempotency_key="delivery-1",
                received_at=self.now,
            )
        status = self.archive.status()
        self.assertEqual(status["chunks"], 1)
        self.assertEqual(status["raw_bytes"], len(body))
        raw_files = list((self.root / "raw").rglob("*.pcm"))
        self.assertEqual(len(raw_files), 1)
        self.assertEqual(raw_files[0].read_bytes(), body)
        self.assertNotIn("private-user-id", (self.root / "audit" / "events.jsonl").read_text())

    def test_duplicate_retry_fails_closed_when_indexed_audio_is_missing(self) -> None:
        body = pcm(0.1)
        first = self.archive.ingest_pcm(
            uid="missing-evidence",
            sample_rate=16000,
            body=body,
            idempotency_key="delivery-missing",
            received_at=self.now,
        )
        with sqlite3.connect(self.archive.db_path) as connection:
            relative = connection.execute(
                "SELECT path FROM chunks WHERE chunk_id=?", (first.chunk_id,)
            ).fetchone()[0]
        (self.root / relative).unlink()
        with self.assertRaisesRegex(RuntimeError, "missing"):
            self.archive.ingest_pcm(
                uid="missing-evidence",
                sample_rate=16000,
                body=body,
                idempotency_key="delivery-missing",
                received_at=self.now,
            )

    def test_existing_v0_database_is_migrated_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "legacy"
            root.mkdir(parents=True)
            database = root / "archive.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE sessions (
                        session_id TEXT PRIMARY KEY, uid_hash TEXT NOT NULL, opened_at TEXT NOT NULL,
                        first_receive_epoch REAL NOT NULL, last_receive_epoch REAL NOT NULL,
                        status TEXT NOT NULL, last_error TEXT
                    );
                    CREATE TABLE chunks (
                        chunk_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, uid_hash TEXT NOT NULL,
                        session_id TEXT NOT NULL, received_at TEXT NOT NULL, received_epoch REAL NOT NULL,
                        sample_rate INTEGER NOT NULL, num_bytes INTEGER NOT NULL, duration_seconds REAL NOT NULL,
                        sha256 TEXT NOT NULL, path TEXT NOT NULL, status TEXT NOT NULL
                    );
                    """
                )
            AudioArchive(Settings(state_dir=root, asr_backend="stub"))
            with sqlite3.connect(database) as connection:
                session_columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
                chunk_columns = {row[1] for row in connection.execute("PRAGMA table_info(chunks)")}
                window_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(rolling_windows)")
                }
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("source_session_id", session_columns)
            self.assertIn("transcription_failures", session_columns)
            self.assertIn("next_transcription_epoch", session_columns)
            self.assertIn("capture_started_at", chunk_columns)
            self.assertIn("capture_started_epoch", chunk_columns)
            self.assertIn("capture_ended_epoch", chunk_columns)
            self.assertIn("sequence_number", chunk_columns)
            self.assertIn("decoder_generation", chunk_columns)
            self.assertIn("pcm_peak_abs", chunk_columns)
            self.assertIn("pcm_rms", chunk_columns)
            self.assertIn("pcm_clipped_samples", chunk_columns)
            self.assertIn("pcm_dc_offset", chunk_columns)
            self.assertIn("retired", window_columns)
            self.assertIn("rolling_windows", tables)
            self.assertIn("rolling_chunk_state", tables)
            self.assertIn("rolling_segments", tables)
            self.assertIn("search_documents", tables)
            self.assertIn("search_documents_fts", tables)

    def test_existing_direct_capture_times_are_backfilled_to_indexed_epochs(self) -> None:
        capture = CaptureMetadata(
            source_session_id="legacy-direct-session",
            sequence_number=0,
            capture_started_at=self.now,
            capture_ended_at=self.now + timedelta(seconds=1),
            codec="pcm16le",
            transport="s25-direct-ble-tailscale",
        )
        ingested = self.archive.ingest_pcm(
            uid="legacy-direct-device",
            sample_rate=16000,
            body=pcm(),
            idempotency_key="legacy-direct-session:0",
            received_at=self.now + timedelta(minutes=5),
            capture=capture,
        )
        with sqlite3.connect(self.archive.db_path) as connection:
            connection.execute(
                """UPDATE chunks SET capture_started_epoch=NULL,capture_ended_epoch=NULL
                   WHERE chunk_id=?""",
                (ingested.chunk_id,),
            )
        AudioArchive(self.settings)
        with sqlite3.connect(self.archive.db_path) as connection:
            started, ended = connection.execute(
                """SELECT capture_started_epoch,capture_ended_epoch FROM chunks
                   WHERE chunk_id=?""",
                (ingested.chunk_id,),
            ).fetchone()
        self.assertEqual(started, self.now.timestamp())
        self.assertEqual(ended, (self.now + timedelta(seconds=1)).timestamp())

    def test_receiver_and_operator_can_migrate_the_same_archive_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "legacy"
            root.mkdir(parents=True)
            with sqlite3.connect(root / "archive.sqlite3") as connection:
                connection.executescript(
                    """
                    CREATE TABLE sessions (
                        session_id TEXT PRIMARY KEY, uid_hash TEXT NOT NULL, opened_at TEXT NOT NULL,
                        first_receive_epoch REAL NOT NULL, last_receive_epoch REAL NOT NULL,
                        status TEXT NOT NULL, last_error TEXT
                    );
                    CREATE TABLE chunks (
                        chunk_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, uid_hash TEXT NOT NULL,
                        session_id TEXT NOT NULL, received_at TEXT NOT NULL, received_epoch REAL NOT NULL,
                        sample_rate INTEGER NOT NULL, num_bytes INTEGER NOT NULL, duration_seconds REAL NOT NULL,
                        sha256 TEXT NOT NULL, path TEXT NOT NULL, status TEXT NOT NULL
                    );
                    """
                )
            barrier = threading.Barrier(2)

            def initialize() -> AudioArchive:
                barrier.wait()
                return AudioArchive(Settings(state_dir=root, asr_backend="stub"))

            with ThreadPoolExecutor(max_workers=2) as executor:
                archives = list(executor.map(lambda _: initialize(), range(2)))
            self.assertEqual(len(archives), 2)

    def test_session_gap_grouping(self) -> None:
        one = self.archive.ingest_pcm(uid="u", sample_rate=16000, body=pcm(), received_at=self.now)
        two = self.archive.ingest_pcm(
            uid="u", sample_rate=16000, body=pcm(), received_at=self.now + timedelta(seconds=2)
        )
        three = self.archive.ingest_pcm(
            uid="u", sample_rate=16000, body=pcm(), received_at=self.now + timedelta(seconds=10)
        )
        self.assertEqual(one.session_id, two.session_id)
        self.assertNotEqual(one.session_id, three.session_id)

    def test_direct_bridge_uses_source_session_sequence_and_capture_clock(self) -> None:
        first_meta = CaptureMetadata(
            source_session_id="s25-session-1",
            sequence_number=0,
            capture_started_at=self.now,
            capture_ended_at=self.now + timedelta(seconds=1),
            codec="pcm16le",
            transport="s25-direct-ble-tailscale",
        )
        second_meta = CaptureMetadata(
            source_session_id="s25-session-1",
            sequence_number=1,
            capture_started_at=self.now + timedelta(seconds=1),
            capture_ended_at=self.now + timedelta(seconds=2),
            codec="pcm16le",
            transport="s25-direct-ble-tailscale",
        )
        first = self.archive.ingest_pcm(
            uid="s25-omi-device",
            sample_rate=16000,
            body=pcm(),
            idempotency_key="s25-session-1:0",
            received_at=self.now + timedelta(seconds=10),
            capture=first_meta,
        )
        second = self.archive.ingest_pcm(
            uid="s25-omi-device",
            sample_rate=16000,
            body=pcm(),
            idempotency_key="s25-session-1:1",
            received_at=self.now + timedelta(seconds=30),
            capture=second_meta,
        )
        self.assertEqual(first.session_id, second.session_id)
        with self.assertRaisesRegex(ValueError, "capture sequence was reused"):
            self.archive.ingest_pcm(
                uid="s25-omi-device",
                sample_rate=16000,
                body=b"\x00\x00" * 16000,
                idempotency_key="different-key",
                received_at=self.now + timedelta(seconds=31),
                capture=second_meta,
            )
        event = self.archive.transcribe_session(first.session_id, StubASR("Direct bridge evidence"))
        self.assertEqual(event["timestamps"]["observed_start_estimate"], self.now.isoformat())
        self.assertEqual(
            event["timestamps"]["observed_end_estimate"],
            (self.now + timedelta(seconds=2)).isoformat(),
        )
        self.assertEqual(event["timestamps"]["capture_time_quality"], "phone-derived-capture-time")
        self.assertEqual(event["timestamps"]["timestamp_basis"], "s25_pcm_sample_clock")
        self.assertEqual(event["source"]["transport"], "s25-direct-ble-tailscale")
        self.assertEqual(event["source"]["source_session_id"], "s25-session-1")
        self.assertEqual(
            event["evidence"]["sequence"],
            {"first": 0, "last": 1, "missing": [], "missing_count": 0, "missing_truncated": False},
        )

    def test_direct_bridge_rejects_partial_or_inconsistent_capture_metadata(self) -> None:
        with self.assertRaises(ValueError):
            CaptureMetadata(
                source_session_id="session",
                sequence_number=0,
                capture_started_at=self.now,
                capture_ended_at=self.now + timedelta(seconds=2),
                codec="pcm16le",
                transport="s25-direct-ble-tailscale",
            ).validate(audio_duration_seconds=1.0)

    def test_large_sequence_gap_is_counted_without_unbounded_event_growth(self) -> None:
        session_id = None
        for sequence, second in ((0, 0), (1_000_000, 1)):
            result = self.archive.ingest_pcm(
                uid="s25",
                sample_rate=16000,
                body=pcm(),
                capture=CaptureMetadata(
                    source_session_id="bounded-gap",
                    sequence_number=sequence,
                    capture_started_at=self.now + timedelta(seconds=second),
                    capture_ended_at=self.now + timedelta(seconds=second + 1),
                    codec="pcm16le",
                    transport="s25-direct-ble-tailscale",
                ),
            )
            session_id = result.session_id
        assert session_id is not None
        event = self.archive.transcribe_session(session_id, StubASR("gap"))
        sequence = event["evidence"]["sequence"]
        self.assertEqual(sequence["missing_count"], 999_999)
        self.assertEqual(len(sequence["missing"]), 1000)
        self.assertTrue(sequence["missing_truncated"])

    def test_late_direct_chunk_creates_a_new_transcript_version_without_force(self) -> None:
        first = self.archive.ingest_pcm(
            uid="s25",
            sample_rate=16000,
            body=pcm(),
            capture=CaptureMetadata(
                source_session_id="late-session",
                sequence_number=0,
                capture_started_at=self.now,
                capture_ended_at=self.now + timedelta(seconds=1),
                codec="pcm16le",
                transport="s25-direct-ble-tailscale",
            ),
        )
        version_one = self.archive.transcribe_session(first.session_id, StubASR("first"))
        self.assertEqual(version_one["transcript_version"], 1)
        late = self.archive.ingest_pcm(
            uid="s25",
            sample_rate=16000,
            body=pcm(frequency=880),
            capture=CaptureMetadata(
                source_session_id="late-session",
                sequence_number=1,
                capture_started_at=self.now + timedelta(seconds=1),
                capture_ended_at=self.now + timedelta(seconds=2),
                codec="pcm16le",
                transport="s25-direct-ble-tailscale",
            ),
        )
        self.assertEqual(first.session_id, late.session_id)
        version_two = self.archive.transcribe_session(first.session_id, StubASR("first plus late"))
        self.assertEqual(version_two["transcript_version"], 2)
        self.assertEqual(version_two["evidence"]["sequence"]["last"], 1)

    def test_chunk_arriving_during_asr_remains_pending_for_the_next_version(self) -> None:
        first = self.archive.ingest_pcm(
            uid="s25",
            sample_rate=16000,
            body=pcm(),
            capture=CaptureMetadata(
                source_session_id="during-asr",
                sequence_number=0,
                capture_started_at=self.now,
                capture_ended_at=self.now + timedelta(seconds=1),
                codec="pcm16le",
                transport="s25-direct-ble-tailscale",
            ),
        )
        started = threading.Event()
        release = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self.archive.transcribe_session,
                first.session_id,
                BlockingStubASR(started, release),
            )
            self.assertTrue(started.wait(timeout=5))
            late = self.archive.ingest_pcm(
                uid="s25",
                sample_rate=16000,
                body=pcm(frequency=880),
                capture=CaptureMetadata(
                    source_session_id="during-asr",
                    sequence_number=1,
                    capture_started_at=self.now + timedelta(seconds=1),
                    capture_ended_at=self.now + timedelta(seconds=2),
                    codec="pcm16le",
                    transport="s25-direct-ble-tailscale",
                ),
            )
            release.set()
            version_one = future.result(timeout=5)
        self.assertEqual(first.session_id, late.session_id)
        self.assertEqual(version_one["evidence"]["chunk_ids"], [first.chunk_id])
        with sqlite3.connect(self.archive.db_path) as connection:
            statuses = dict(
                connection.execute(
                    "SELECT chunk_id,status FROM chunks WHERE session_id=?",
                    (first.session_id,),
                )
            )
            session_status = connection.execute(
                "SELECT status FROM sessions WHERE session_id=?",
                (first.session_id,),
            ).fetchone()[0]
        self.assertEqual(statuses[first.chunk_id], "processed")
        self.assertEqual(statuses[late.chunk_id], "pending")
        self.assertEqual(session_status, "open")

        version_two = self.archive.transcribe_session(first.session_id, StubASR("complete transcript"))
        self.assertEqual(version_two["transcript_version"], 2)
        self.assertEqual(version_two["evidence"]["chunk_ids"], [first.chunk_id, late.chunk_id])
        with sqlite3.connect(self.archive.db_path) as connection:
            pending_count = connection.execute(
                "SELECT COUNT(*) FROM chunks WHERE session_id=? AND status='pending'",
                (first.session_id,),
            ).fetchone()[0]
            final_session_status = connection.execute(
                "SELECT status FROM sessions WHERE session_id=?",
                (first.session_id,),
            ).fetchone()[0]
        self.assertEqual(pending_count, 0)
        self.assertEqual(final_session_status, "transcribed")

    def test_versioned_transcript_search_export_and_delete(self) -> None:
        first = self.archive.ingest_pcm(
            uid="u",
            sample_rate=16000,
            body=pcm(),
            idempotency_key="one",
            received_at=self.now,
        )
        self.archive.ingest_pcm(
            uid="u",
            sample_rate=16000,
            body=pcm(),
            idempotency_key="two",
            received_at=self.now + timedelta(seconds=1),
        )
        event = self.archive.transcribe_session(
            first.session_id,
            StubASR("Demerzel heard the Manfred hardware discussion"),
        )
        self.assertEqual(event["schema_version"], 1)
        self.assertEqual(event["event_type"], "observation.audio.transcript")
        self.assertEqual(event["source"]["device"], "omi-devkit-2")
        self.assertEqual(event["evidence"]["canonical"], "raw_audio_chunks")
        self.assertEqual(event["timestamps"]["observed_start_estimate"], (self.now - timedelta(seconds=1)).isoformat())
        self.assertEqual(
            event["timestamps"]["timestamp_basis"],
            "last_relay_receive_time_minus_total_pcm_duration",
        )
        self.assertEqual(event["transcript"]["explicit_or_inferred"], "explicit_observation")
        self.assertIsNone(event["transcript"]["confidence"])
        self.assertEqual(len(event["evidence"]["chunk_ids"]), 2)
        hits = self.archive.search("Manfred hardware")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["session_id"], first.session_id)
        export = self.archive.export_day(self.now.date())
        text = export.read_text()
        self.assertIn("Demerzel heard the Manfred hardware discussion", text)
        self.assertIn("Local scratch input", text)
        event2 = self.archive.transcribe_session(
            first.session_id,
            StubASR("Second improved transcript", name="stub-v2"),
            force=True,
        )
        self.assertEqual(event2["transcript_version"], 2)
        self.assertEqual(self.archive.status()["transcripts"], 2)
        self.assertEqual(len(self.archive.search("Second improved")), 1)
        self.assertEqual(self.archive.search("Manfred hardware"), [])
        self.assertTrue(self.archive.delete_session(first.session_id))
        self.assertEqual(self.archive.search("Manfred hardware"), [])
        self.assertEqual(list((self.root / "raw").rglob("*.pcm")), [])
        self.assertFalse(self.archive.delete_session(first.session_id))

    def test_transcript_daily_bucket_uses_configured_iana_timezone(self) -> None:
        received = datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)
        ingested = self.archive.ingest_pcm(
            uid="local-day",
            sample_rate=16000,
            body=pcm(),
            idempotency_key="local-day-one",
            received_at=received,
        )
        self.archive.transcribe_session(
            ingested.session_id,
            StubASR("Denver evening transcript", name="stub-local-day"),
        )
        with self.archive._connect() as connection:
            observed_date = connection.execute(
                "SELECT observed_date FROM transcripts WHERE session_id=?",
                (ingested.session_id,),
            ).fetchone()[0]
        self.assertEqual(observed_date, "2026-09-01")

    def test_session_deletion_tombstone_recovers_after_index_drop(self) -> None:
        ingested = self.archive.ingest_pcm(
            uid="delete-recovery",
            sample_rate=16000,
            body=pcm(),
            idempotency_key="delete-recovery-one",
            received_at=self.now,
        )
        self.archive.transcribe_session(
            ingested.session_id,
            StubASR("private deletion recovery transcript", name="stub-delete"),
        )
        with mock.patch.object(
            self.archive,
            "_finish_deletion_tombstone",
            side_effect=RuntimeError("simulated crash after index deletion"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.archive.delete_session(ingested.session_id)
        with self.archive._connect() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT session_id FROM sessions WHERE session_id=?",
                    (ingested.session_id,),
                ).fetchone()
            )
        self.assertTrue(list((self.root / "deletions").glob("session-*.json")))
        recovered = AudioArchive(self.settings)
        self.assertFalse(list((self.root / "deletions").glob("session-*.json")))
        self.assertEqual(recovered.search("private deletion recovery"), [])
        self.assertEqual(list((self.root / "raw").rglob("*.pcm")), [])

    def test_day_export_replaces_only_after_complete_temp_write(self) -> None:
        target = self.root / "wiki-inbox" / f"{self.now.date().isoformat()}.md"
        target.write_text("last good mirror\n", encoding="utf-8")
        real_replace = os.replace
        observed = {}

        def guarded_replace(source, destination):  # type: ignore[no-untyped-def]
            self.assertEqual(Path(destination), target)
            self.assertEqual(target.read_text(encoding="utf-8"), "last good mirror\n")
            self.assertEqual(Path(source).parent, self.root / "tmp" / "wiki-exports")
            self.assertNotEqual(Path(source).parent, target.parent)
            temporary_text = Path(source).read_text(encoding="utf-8")
            self.assertTrue(
                temporary_text.startswith(
                    f"# Wearable transcript context — {self.now.date().isoformat()}\n"
                )
            )
            self.assertIn("Local scratch input for daily LLM Wiki distillation", temporary_text)
            self.assertTrue(temporary_text.endswith("\n"))
            observed["temporary"] = Path(source)
            return real_replace(source, destination)

        with mock.patch("manfred_ears.archive.os.replace", side_effect=guarded_replace):
            exported = self.archive.export_day(self.now.date())

        self.assertEqual(exported, target)
        self.assertNotEqual(target.read_text(encoding="utf-8"), "last good mirror\n")
        self.assertFalse(observed["temporary"].exists())

    def test_day_export_is_bounded_to_transfer_and_collector_contract(self) -> None:
        ingested = self.archive.ingest_pcm(
            uid="u",
            sample_rate=16000,
            body=pcm(),
            idempotency_key="bounded-export",
            received_at=self.now,
        )
        self.archive.transcribe_session(
            ingested.session_id,
            StubASR("EARLY-CONTEXT " + "word " * 40_000),
        )
        latest = self.archive.ingest_pcm(
            uid="u",
            sample_rate=16000,
            body=pcm(),
            idempotency_key="bounded-export-latest",
            received_at=self.now + timedelta(seconds=10),
        )
        self.archive.transcribe_session(latest.session_id, StubASR("LATEST-CONTEXT"))

        exported = self.archive.export_day(self.now.date())
        payload = exported.read_bytes()

        self.assertLessEqual(len(payload), WIKI_EXPORT_MAX_BYTES)
        self.assertTrue(
            payload.startswith(
                f"# Wearable transcript context — {self.now.date().isoformat()}\n".encode()
            )
        )
        self.assertIn(b"[TRUNCATED MIDDLE: daily Wiki scratch exceeded", payload)
        self.assertIn(b"EARLY-CONTEXT", payload)
        self.assertIn(b"LATEST-CONTEXT", payload)
        self.assertIn(b"canonical transcript archive on 7090", payload)

    def test_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.archive.ingest_pcm(uid="u", sample_rate=16000, body=b"\x00")
        with self.assertRaises(ValueError):
            self.archive.ingest_pcm(uid="u", sample_rate=1, body=b"\x00\x00")
        with self.assertRaises(ValueError):
            self.archive.ingest_pcm(uid="", sample_rate=16000, body=b"\x00\x00")
        with self.assertRaisesRegex(ValueError, "duration exceeds"):
            self.archive.ingest_pcm(uid="u", sample_rate=16000, body=pcm(16.0))
        self.assertEqual(self.archive.status()["chunks"], 0)


class MetricsTests(unittest.TestCase):
    def test_wer(self) -> None:
        report = word_error_rate("the quick brown fox", "the quick fox")
        self.assertEqual(report["word_edits"], 1)
        self.assertAlmostEqual(report["wer"], 0.25)

    def test_wav_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.wav"
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(pcm(0.5))
            report = wav_metrics(path)
            self.assertAlmostEqual(report["duration_seconds"], 0.5)
            self.assertEqual(report["sample_rate"], 16000)
            self.assertEqual(report["clipped_samples"], 0)


if __name__ == "__main__":
    unittest.main()
