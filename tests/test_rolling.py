from __future__ import annotations

import asyncio
import json
import math
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
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manfred_ears.archive import AudioArchive, CaptureMetadata
from manfred_ears.asr import StubASR, TranscriptResult, TranscriptSegment
from manfred_ears.config import Settings
from manfred_ears.service import _export_session_days
from manfred_ears.vad import EnergySpeechGate


def pcm(*, speech: bool, sample_rate: int = 16000) -> bytes:
    if not speech:
        return b"\x00\x00" * sample_rate
    values = [
        int(4000 * math.sin(2 * math.pi * 220 * index / sample_rate))
        for index in range(sample_rate)
    ]
    return struct.pack(f"<{len(values)}h", *values)


class DeterministicGate:
    name = "deterministic-test-gate"

    def is_speech(self, pcm16le: bytes, sample_rate: int) -> bool:
        return any(pcm16le)


class CountingASR:
    name = "counting-asr"

    def __init__(self, prefix: str = "rolling window") -> None:
        self.calls: list[Path] = []
        self.prefix = prefix

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        self.calls.append(audio_path)
        with wave.open(str(audio_path), "rb") as handle:
            duration = handle.getnframes() / handle.getframerate()
        text = f"{self.prefix} {len(self.calls)}"
        return TranscriptResult(
            text=text,
            model=self.name,
            language="en",
            language_probability=1.0,
            confidence=None,
            avg_logprob=-0.1,
            no_speech_probability=0.01,
            latency_seconds=0.01,
            segments=[
                TranscriptSegment(
                    start=0.0,
                    end=duration,
                    text=text,
                    avg_logprob=-0.1,
                    no_speech_prob=0.01,
                )
            ],
        )


class SplitBaseASR:
    name = "split-base-asr"

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        return TranscriptResult(
            text="zero two",
            model=self.name,
            language="en",
            language_probability=1.0,
            confidence=None,
            avg_logprob=-0.1,
            no_speech_probability=0.01,
            latency_seconds=0.01,
            segments=[
                TranscriptSegment(
                    start=0.0,
                    end=1.0,
                    text="zero",
                    avg_logprob=-0.1,
                    no_speech_prob=0.01,
                ),
                TranscriptSegment(
                    start=1.0,
                    end=2.0,
                    text="two",
                    avg_logprob=-0.1,
                    no_speech_prob=0.01,
                ),
            ],
        )


class BlockingASR(CountingASR):
    def __init__(
        self,
        started: threading.Event,
        release: threading.Event,
        prefix: str = "rolling window",
    ) -> None:
        super().__init__(prefix)
        self.started = started
        self.release = release

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release rolling ASR")
        return super().transcribe(audio_path)


class FailingASR(CountingASR):
    def transcribe(self, audio_path: Path) -> TranscriptResult:
        self.calls.append(audio_path)
        raise RuntimeError("transient ASR interruption")


class SelectedSpeechASR(CountingASR):
    def __init__(self) -> None:
        super().__init__("selected speech")
        self.full_session_calls = 0
        self.selected_speech_calls = 0

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        self.full_session_calls += 1
        return super().transcribe(audio_path)

    def transcribe_selected_speech(self, audio_path: Path) -> TranscriptResult:
        self.selected_speech_calls += 1
        return super().transcribe(audio_path)


class RollingTranscriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "state"
        self.settings = Settings(
            state_dir=self.root,
            session_gap_seconds=90,
            idle_flush_seconds=20,
            rolling_window_seconds=2,
            rolling_trailing_silence_seconds=1,
            rolling_recovery_seconds=60,
            rolling_reconcile_seconds=1,
            asr_backend="stub",
        )
        self.archive = AudioArchive(self.settings)
        self.gate = DeterministicGate()
        self.now = datetime(2026, 8, 22, 6, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def ingest(self, sequence: int, *, speech: bool, source_session: str = "continuous"):
        started = self.now + timedelta(seconds=sequence)
        return self.archive.ingest_pcm(
            uid="s25-omi",
            sample_rate=16000,
            body=pcm(speech=speech),
            idempotency_key=f"{source_session}:{sequence}",
            received_at=started + timedelta(seconds=10),
            capture=CaptureMetadata(
                source_session_id=source_session,
                sequence_number=sequence,
                capture_started_at=started,
                capture_ended_at=started + timedelta(seconds=1),
                codec="pcm16le",
                transport="s25-direct-ble-tailscale",
            ),
        )

    def raw_paths(self) -> list[Path]:
        return sorted((self.root / "raw").rglob("*.pcm"))

    def test_rolling_window_uses_selected_speech_backend_path(self) -> None:
        first = self.ingest(0, speech=True)
        self.ingest(1, speech=False)
        asr = SelectedSpeechASR()

        event = self.archive.process_incremental(first.session_id, asr, self.gate)

        self.assertIsNotNone(event)
        self.assertEqual(asr.selected_speech_calls, 1)
        self.assertEqual(asr.full_session_calls, 0)

    def test_continuous_chunks_arriving_during_asr_remain_for_the_next_window(self) -> None:
        first = self.ingest(0, speech=True)
        self.ingest(1, speech=False)
        started = threading.Event()
        release = threading.Event()
        asr = BlockingASR(started, release)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self.archive.process_incremental,
                first.session_id,
                asr,
                self.gate,
            )
            self.assertTrue(started.wait(timeout=5))
            late_speech = self.ingest(2, speech=True)
            late_silence = self.ingest(3, speech=False)
            release.set()
            first_window = future.result(timeout=5)
        assert first_window is not None
        self.assertEqual(first_window["evidence"]["sequence"]["first"], 0)
        self.assertEqual(first_window["evidence"]["sequence"]["last"], 0)

        with sqlite3.connect(self.archive.db_path) as connection:
            classified_late = connection.execute(
                "SELECT COUNT(*) FROM rolling_chunk_state WHERE chunk_id IN (?,?)",
                (late_speech.chunk_id, late_silence.chunk_id),
            ).fetchone()[0]
        self.assertEqual(classified_late, 0)

        second_window = self.archive.process_incremental(first.session_id, asr, self.gate)
        assert second_window is not None
        self.assertEqual(second_window["evidence"]["sequence"]["first"], 2)
        self.assertEqual(len(asr.calls), 2)
        self.assertEqual(len(self.raw_paths()), 4)

    def test_delayed_prebase_chunk_is_reconciled_in_observed_time_order(self) -> None:
        first = self.ingest(1, speech=True, source_session="legacy-out-of-order")
        legacy = self.archive.transcribe_session(
            first.session_id,
            StubASR("later canonical text"),
        )
        delayed = self.ingest(0, speech=True, source_session="legacy-out-of-order")
        asr = CountingASR()
        reconciled = self.archive.finalize_incremental_session(first.session_id, asr, self.gate)

        self.assertEqual(
            reconciled["transcript"]["text"],
            "rolling window 1 later canonical text",
        )
        self.assertEqual(
            reconciled["reconciliation"]["base_transcript_id"],
            legacy["event_id"],
        )
        self.assertEqual(
            reconciled["reconciliation"]["base_transcript_observed_start"],
            legacy["timestamps"]["observed_start_estimate"],
        )
        self.assertEqual(reconciled["evidence"]["chunk_ids"][0], delayed.chunk_id)
        starts = [segment["start"] for segment in reconciled["transcript"]["segments"]]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(asr.calls), 1)
        self.assertEqual(len(self.raw_paths()), 2)

    def test_delayed_chunk_is_interleaved_between_timestamped_base_segments(self) -> None:
        first = self.ingest(0, speech=True, source_session="legacy-interleaved")
        self.ingest(2, speech=True, source_session="legacy-interleaved")
        legacy = self.archive.transcribe_session(first.session_id, SplitBaseASR())
        self.assertEqual(legacy["transcript"]["text"], "zero two")

        delayed = self.ingest(1, speech=True, source_session="legacy-interleaved")
        asr = CountingASR(prefix="one")
        reconciled = self.archive.finalize_incremental_session(first.session_id, asr, self.gate)

        self.assertEqual(reconciled["transcript"]["text"], "zero one 1 two")
        self.assertEqual(
            reconciled["reconciliation"]["base_transcript_id"],
            legacy["event_id"],
        )
        self.assertIn(delayed.chunk_id, reconciled["evidence"]["chunk_ids"])
        starts = [segment["start"] for segment in reconciled["transcript"]["segments"]]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(asr.calls), 1)
        self.assertEqual(len(self.raw_paths()), 3)

    def test_sequence_gap_splits_windows_instead_of_collapsing_missing_time(self) -> None:
        first = self.ingest(0, speech=True, source_session="gap-window")
        self.ingest(2, speech=True, source_session="gap-window")
        self.ingest(3, speech=False, source_session="gap-window")
        asr = CountingASR()
        window_one = self.archive.process_incremental(first.session_id, asr, self.gate)
        window_two = self.archive.process_incremental(first.session_id, asr, self.gate)
        assert window_one is not None and window_two is not None
        self.assertEqual(window_one["evidence"]["sequence"]["first"], 0)
        self.assertEqual(window_one["evidence"]["sequence"]["last"], 0)
        self.assertEqual(window_two["evidence"]["sequence"]["first"], 2)
        self.assertEqual(window_two["evidence"]["sequence"]["last"], 2)
        final = self.archive.finalize_incremental_session(first.session_id, asr, self.gate)
        self.assertEqual(final["evidence"]["sequence"]["missing"], [1])
        self.assertEqual(len(asr.calls), 2)

    def test_variable_duration_chunks_do_not_overfill_a_rolling_window(self) -> None:
        sample = int(4000).to_bytes(2, "little", signed=True)
        first = self.archive.ingest_pcm(
            uid="compatibility-source",
            sample_rate=16000,
            body=sample * (16000 * 9),
            idempotency_key="variable:0",
            received_at=self.now,
        )
        second = self.archive.ingest_pcm(
            uid="compatibility-source",
            sample_rate=16000,
            body=sample * (16000 * 15),
            idempotency_key="variable:1",
            received_at=self.now + timedelta(seconds=9),
        )
        self.assertEqual(first.session_id, second.session_id)
        asr = CountingASR()
        window_one = self.archive.process_incremental(first.session_id, asr, self.gate)
        window_two = self.archive.process_incremental(first.session_id, asr, self.gate)
        assert window_one is not None and window_two is not None
        self.assertEqual(window_one["evidence"]["duration_seconds"], 9.0)
        self.assertEqual(window_two["evidence"]["duration_seconds"], 15.0)
        self.assertNotEqual(
            window_one["evidence"]["chunk_ids"],
            window_two["evidence"]["chunk_ids"],
        )
        self.assertEqual(len(asr.calls), 2)
        self.assertEqual(len(self.raw_paths()), 2)

    def test_incremental_processing_never_duplicates_committed_ranges(self) -> None:
        first = self.ingest(0, speech=True)
        self.ingest(1, speech=False)
        asr = CountingASR()
        committed = self.archive.process_incremental(first.session_id, asr, self.gate)
        self.assertIsNotNone(committed)
        self.assertIsNone(self.archive.process_incremental(first.session_id, asr, self.gate))
        self.assertEqual(len(asr.calls), 1)
        with sqlite3.connect(self.archive.db_path) as connection:
            windows = connection.execute(
                "SELECT COUNT(*) FROM rolling_windows WHERE session_id=? AND status='committed'",
                (first.session_id,),
            ).fetchone()[0]
            assignments = connection.execute(
                "SELECT COUNT(DISTINCT chunk_id) FROM rolling_chunk_state WHERE window_id IS NOT NULL",
            ).fetchone()[0]
        self.assertEqual(windows, 1)
        self.assertEqual(assignments, 1)

    def test_long_silence_creates_no_asr_window_or_misleading_segment(self) -> None:
        first = self.ingest(0, speech=False, source_session="silence")
        for sequence in range(1, 20):
            self.ingest(sequence, speech=False, source_session="silence")
        asr = CountingASR()
        self.assertIsNone(self.archive.process_incremental(first.session_id, asr, self.gate))
        self.assertEqual(asr.calls, [])
        with sqlite3.connect(self.archive.db_path) as connection:
            decisions = connection.execute(
                "SELECT decision,COUNT(*) FROM rolling_chunk_state GROUP BY decision"
            ).fetchall()
            segments = connection.execute("SELECT COUNT(*) FROM rolling_segments").fetchone()[0]
        self.assertEqual(decisions, [("silence", 20)])
        self.assertEqual(segments, 0)
        self.assertEqual(len(self.raw_paths()), 20)

    def test_boundary_split_utterance_is_preserved_across_adjacent_chunks(self) -> None:
        gate = EnergySpeechGate()
        tone = pcm(speech=True)
        active_bytes = int(16000 * 0.06) * 2
        silence_bytes = int(16000 * 0.94) * 2
        first_body = b"\x00" * silence_bytes + tone[-active_bytes:]
        second_body = tone[:active_bytes] + b"\x00" * silence_bytes
        self.assertFalse(gate.is_speech(first_body, 16000))
        self.assertFalse(gate.is_speech(second_body, 16000))
        self.assertTrue(gate.is_speech(first_body + second_body, 16000))

        def ingest_body(sequence: int, body: bytes):
            started = self.now + timedelta(seconds=sequence)
            return self.archive.ingest_pcm(
                uid="s25-omi",
                sample_rate=16000,
                body=body,
                idempotency_key=f"boundary-speech:{sequence}",
                received_at=started + timedelta(seconds=10),
                capture=CaptureMetadata(
                    source_session_id="boundary-speech",
                    sequence_number=sequence,
                    capture_started_at=started,
                    capture_ended_at=started + timedelta(seconds=1),
                    codec="pcm16le",
                    transport="s25-direct-ble-tailscale",
                ),
            )

        first = ingest_body(0, first_body)
        asr = CountingASR()
        self.assertIsNone(self.archive.process_incremental(first.session_id, asr, gate))
        with sqlite3.connect(self.archive.db_path) as connection:
            first_state = connection.execute(
                "SELECT decision,finalized FROM rolling_chunk_state WHERE chunk_id=?",
                (first.chunk_id,),
            ).fetchone()
        self.assertEqual(first_state, ("silence", 0))

        second = ingest_body(1, second_body)
        event = self.archive.process_incremental(first.session_id, asr, gate)
        assert event is not None
        self.assertEqual(event["evidence"]["chunk_ids"], [first.chunk_id, second.chunk_id])
        with sqlite3.connect(self.archive.db_path) as connection:
            decisions = dict(
                connection.execute(
                    "SELECT chunk_id,decision FROM rolling_chunk_state WHERE session_id=?",
                    (first.session_id,),
                )
            )
        self.assertEqual(set(decisions.values()), {"speech"})
        self.assertEqual(len(asr.calls), 1)
        self.assertEqual(len(self.raw_paths()), 2)

    def test_late_chunk_after_legacy_full_transcript_preserves_prior_text(self) -> None:
        first = self.ingest(0, speech=True, source_session="legacy-late")
        legacy = self.archive.transcribe_session(first.session_id, StubASR("legacy canonical text"))
        self.assertEqual(legacy["transcript_version"], 1)
        self.ingest(1, speech=True, source_session="legacy-late")
        self.ingest(2, speech=False, source_session="legacy-late")
        asr = CountingASR()
        window = self.archive.process_incremental(first.session_id, asr, self.gate)
        assert window is not None
        self.assertEqual(window["evidence"]["sequence"]["first"], 1)
        final = self.archive.finalize_incremental_session(first.session_id, asr, self.gate)
        self.assertEqual(final["transcript_version"], 2)
        self.assertEqual(final["transcript"]["text"], "legacy canonical text rolling window 1")
        self.assertEqual(final["reconciliation"]["base_transcript_id"], legacy["event_id"])

        self.ingest(3, speech=True, source_session="legacy-late")
        self.ingest(4, speech=False, source_session="legacy-late")
        second_window = self.archive.process_incremental(first.session_id, asr, self.gate)
        assert second_window is not None
        final_again = self.archive.finalize_incremental_session(first.session_id, asr, self.gate)
        self.assertEqual(final_again["transcript_version"], 3)
        self.assertEqual(
            final_again["transcript"]["text"],
            "legacy canonical text rolling window 1 rolling window 2",
        )
        self.assertEqual(final_again["reconciliation"]["base_transcript_id"], legacy["event_id"])
        self.assertEqual(len(asr.calls), 2)
        self.assertEqual(len(self.raw_paths()), 5)

    def test_forced_full_asr_retires_unclaimed_classified_chunks(self) -> None:
        first = self.ingest(0, speech=True, source_session="force-retire-unclaimed")
        self.assertEqual(self.archive._classify_rolling_chunks(first.session_id, self.gate), 1)
        forced = self.archive.transcribe_session(
            first.session_id,
            StubASR("forced canonical"),
            force=True,
        )
        self.assertTrue(forced["reconciliation"]["final"])
        with sqlite3.connect(self.archive.db_path) as connection:
            finalized = connection.execute(
                "SELECT finalized FROM rolling_chunk_state WHERE chunk_id=?",
                (first.chunk_id,),
            ).fetchone()[0]
        self.assertEqual(finalized, 1)

        late_speech = self.ingest(1, speech=True, source_session="force-retire-unclaimed")
        self.ingest(2, speech=False, source_session="force-retire-unclaimed")
        rolling = self.archive.process_incremental(
            first.session_id,
            CountingASR(),
            self.gate,
        )
        assert rolling is not None
        self.assertEqual(rolling["evidence"]["chunk_ids"], [late_speech.chunk_id])
        self.assertNotIn(first.chunk_id, rolling["evidence"]["chunk_ids"])
        self.assertEqual(len(self.raw_paths()), 3)

    def test_forced_full_asr_retires_failed_rolling_window(self) -> None:
        first = self.ingest(0, speech=True, source_session="force-retire-failed")
        self.ingest(1, speech=False, source_session="force-retire-failed")
        with self.assertRaises(RuntimeError):
            self.archive.process_incremental(first.session_id, FailingASR(), self.gate)
        forced = self.archive.transcribe_session(
            first.session_id,
            StubASR("forced canonical"),
            force=True,
        )
        retired_window_id = forced["reconciliation"]["rolling_window_ids"][0]
        with sqlite3.connect(self.archive.db_path) as connection:
            retired = connection.execute(
                "SELECT retired FROM rolling_windows WHERE window_id=?",
                (retired_window_id,),
            ).fetchone()[0]
        self.assertEqual(retired, 1)

        late_speech = self.ingest(2, speech=True, source_session="force-retire-failed")
        self.ingest(3, speech=False, source_session="force-retire-failed")
        rolling = self.archive.process_incremental(
            first.session_id,
            CountingASR(),
            self.gate,
        )
        assert rolling is not None
        self.assertEqual(rolling["evidence"]["chunk_ids"], [late_speech.chunk_id])
        with sqlite3.connect(self.archive.db_path) as connection:
            active_window_id = connection.execute(
                """SELECT window_id FROM rolling_windows
                   WHERE retired=0 AND status='committed' ORDER BY committed_at DESC LIMIT 1"""
            ).fetchone()[0]
        self.assertNotEqual(active_window_id, retired_window_id)
        reconciled = self.archive.finalize_incremental_session(
            first.session_id,
            CountingASR(),
            self.gate,
        )
        self.assertEqual(reconciled["reconciliation"]["rolling_window_ids"], [active_window_id])
        self.assertNotIn(retired_window_id, reconciled["reconciliation"]["rolling_window_ids"])
        self.assertAlmostEqual(reconciled["transcript"]["latency_seconds"], 0.01)
        self.assertEqual(len(self.raw_paths()), 4)

    def test_restart_retries_the_exact_failed_window_without_losing_evidence(self) -> None:
        first = self.ingest(0, speech=True, source_session="restart")
        self.ingest(1, speech=False, source_session="restart")
        failing = FailingASR()
        with self.assertRaisesRegex(RuntimeError, "transient"):
            self.archive.process_incremental(first.session_id, failing, self.gate)
        with sqlite3.connect(self.archive.db_path) as connection:
            failed = connection.execute(
                "SELECT window_id,chunk_ids_json,status FROM rolling_windows"
            ).fetchone()
            session_error = connection.execute(
                "SELECT last_error FROM sessions WHERE session_id=?",
                (first.session_id,),
            ).fetchone()[0]
        self.assertEqual(failed[2], "failed")
        self.assertIn("transient ASR interruption", session_error)

        restarted = AudioArchive(self.settings)
        recovered_asr = CountingASR()
        event = restarted.process_incremental(first.session_id, recovered_asr, self.gate)
        assert event is not None
        self.assertEqual(event["event_id"], failed[0])
        self.assertEqual(event["evidence"]["chunk_ids"], __import__("json").loads(failed[1]))
        self.assertEqual(len(recovered_asr.calls), 1)
        with sqlite3.connect(self.archive.db_path) as connection:
            recovered_error = connection.execute(
                "SELECT last_error FROM sessions WHERE session_id=?",
                (first.session_id,),
            ).fetchone()[0]
        self.assertIsNone(recovered_error)
        self.assertEqual(len(self.raw_paths()), 2)

    def test_chunks_committed_during_reconciliation_remain_pending_for_next_version(self) -> None:
        first = self.ingest(0, speech=True, source_session="reconcile-race")
        self.ingest(1, speech=False, source_session="reconcile-race")
        asr = CountingASR()
        self.assertIsNotNone(self.archive.process_incremental(first.session_id, asr, self.gate))
        started = threading.Event()
        release = threading.Event()
        original_digest = self.archive._evidence_digest

        def blocking_digest(chunks):  # type: ignore[no-untyped-def]
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release reconciliation")
            return original_digest(chunks)

        self.archive._evidence_digest = blocking_digest  # type: ignore[method-assign]
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self.archive.reconcile_incremental_session,
                first.session_id,
                final=True,
            )
            self.assertTrue(started.wait(timeout=5))
            late_speech = self.ingest(2, speech=True, source_session="reconcile-race")
            late_silence = self.ingest(3, speech=False, source_session="reconcile-race")
            self.assertIsNotNone(
                self.archive._process_incremental_unlocked(first.session_id, asr, self.gate)
            )
            release.set()
            version_one = future.result(timeout=5)
        self.archive._evidence_digest = original_digest  # type: ignore[method-assign]
        self.assertEqual(version_one["transcript_version"], 1)
        self.assertEqual(version_one["transcript"]["text"], "rolling window 1")
        with sqlite3.connect(self.archive.db_path) as connection:
            late_statuses = dict(
                connection.execute(
                    "SELECT chunk_id,status FROM chunks WHERE chunk_id IN (?,?)",
                    (late_speech.chunk_id, late_silence.chunk_id),
                )
            )
            segment_two = connection.execute(
                "SELECT state,reconciled_by FROM rolling_segments WHERE text='rolling window 2'"
            ).fetchone()
            session_status = connection.execute(
                "SELECT status FROM sessions WHERE session_id=?", (first.session_id,)
            ).fetchone()[0]
        self.assertEqual(set(late_statuses.values()), {"pending"})
        self.assertEqual(segment_two, ("provisional", None))
        self.assertEqual(session_status, "open")

        version_two = self.archive.finalize_incremental_session(first.session_id, asr, self.gate)
        self.assertEqual(version_two["transcript_version"], 2)
        self.assertEqual(version_two["transcript"]["text"], "rolling window 1 rolling window 2")
        self.assertEqual(len(asr.calls), 2)
        self.assertEqual(len(self.raw_paths()), 4)

    def test_segments_committed_during_forced_full_asr_are_not_superseded(self) -> None:
        first = self.ingest(0, speech=True, source_session="full-asr-race")
        self.ingest(1, speech=False, source_session="full-asr-race")
        rolling_asr = CountingASR()
        self.assertIsNotNone(self.archive.process_incremental(first.session_id, rolling_asr, self.gate))
        started = threading.Event()
        release = threading.Event()
        full_asr = BlockingASR(started, release, prefix="forced full")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self.archive.transcribe_session,
                first.session_id,
                full_asr,
                force=True,
            )
            self.assertTrue(started.wait(timeout=5))
            late_speech = self.ingest(2, speech=True, source_session="full-asr-race")
            late_silence = self.ingest(3, speech=False, source_session="full-asr-race")
            self.assertIsNotNone(
                self.archive._process_incremental_unlocked(first.session_id, rolling_asr, self.gate)
            )
            release.set()
            forced = future.result(timeout=5)
        self.assertEqual(forced["transcript"]["text"], "forced full 1")
        self.assertFalse(forced["reconciliation"]["final"])
        forced_hits = self.archive.search("forced full")
        self.assertEqual(forced_hits[0]["kind"], "canonical_transcript")
        self.assertEqual(forced_hits[0]["state"], "provisional")
        with sqlite3.connect(self.archive.db_path) as connection:
            segment_states = dict(
                connection.execute("SELECT text,state FROM rolling_segments ORDER BY text")
            )
            late_statuses = dict(
                connection.execute(
                    "SELECT chunk_id,status FROM chunks WHERE chunk_id IN (?,?)",
                    (late_speech.chunk_id, late_silence.chunk_id),
                )
            )
            session_status = connection.execute(
                "SELECT status FROM sessions WHERE session_id=?", (first.session_id,)
            ).fetchone()[0]
        self.assertEqual(segment_states["rolling window 1"], "superseded")
        self.assertEqual(segment_states["rolling window 2"], "provisional")
        self.assertEqual(set(late_statuses.values()), {"pending"})
        self.assertEqual(session_status, "open")
        self.assertEqual(len(self.raw_paths()), 4)

    def test_finalization_waits_for_an_active_rolling_window(self) -> None:
        first = self.ingest(0, speech=True, source_session="final-race")
        self.ingest(1, speech=False, source_session="final-race")
        started = threading.Event()
        release = threading.Event()
        blocking = BlockingASR(started, release)
        with ThreadPoolExecutor(max_workers=2) as executor:
            rolling_future = executor.submit(
                self.archive.process_incremental,
                first.session_id,
                blocking,
                self.gate,
            )
            self.assertTrue(started.wait(timeout=5))
            final_future = executor.submit(
                self.archive.finalize_incremental_session,
                first.session_id,
                CountingASR(),
                self.gate,
            )
            threading.Event().wait(0.05)
            self.assertFalse(final_future.done())
            with sqlite3.connect(self.archive.db_path) as connection:
                transcript_count = connection.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
                session_status = connection.execute(
                    "SELECT status FROM sessions WHERE session_id=?", (first.session_id,)
                ).fetchone()[0]
            self.assertEqual(transcript_count, 0)
            self.assertEqual(session_status, "open")
            release.set()
            self.assertIsNotNone(rolling_future.result(timeout=5))
            finalized = final_future.result(timeout=5)
        self.assertTrue(finalized["reconciliation"]["final"])
        self.assertEqual(finalized["transcript"]["text"], "rolling window 1")

    def test_new_rolling_claim_waits_for_final_reconciliation_lock(self) -> None:
        first = self.ingest(0, speech=True, source_session="final-claim-race")
        self.ingest(1, speech=False, source_session="final-claim-race")
        asr = CountingASR()
        self.assertIsNotNone(self.archive.process_incremental(first.session_id, asr, self.gate))
        started = threading.Event()
        release = threading.Event()
        original_digest = self.archive._evidence_digest

        def blocking_digest(chunks):  # type: ignore[no-untyped-def]
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release final reconciliation")
            return original_digest(chunks)

        self.archive._evidence_digest = blocking_digest  # type: ignore[method-assign]
        with ThreadPoolExecutor(max_workers=2) as executor:
            final_future = executor.submit(
                self.archive.finalize_incremental_session,
                first.session_id,
                asr,
                self.gate,
            )
            self.assertTrue(started.wait(timeout=5))
            late_speech = self.ingest(2, speech=True, source_session="final-claim-race")
            late_silence = self.ingest(3, speech=False, source_session="final-claim-race")
            rolling_future = executor.submit(
                self.archive.process_incremental,
                first.session_id,
                asr,
                self.gate,
            )
            threading.Event().wait(0.05)
            self.assertFalse(rolling_future.done())
            release.set()
            final = final_future.result(timeout=5)
            rolling = rolling_future.result(timeout=5)
        self.archive._evidence_digest = original_digest  # type: ignore[method-assign]
        assert rolling is not None
        self.assertEqual(final["transcript"]["text"], "rolling window 1")
        self.assertEqual(rolling["transcript"]["text"], "rolling window 2")
        with sqlite3.connect(self.archive.db_path) as connection:
            late_statuses = dict(
                connection.execute(
                    "SELECT chunk_id,status FROM chunks WHERE chunk_id IN (?,?)",
                    (late_speech.chunk_id, late_silence.chunk_id),
                )
            )
            late_segment = connection.execute(
                "SELECT state,reconciled_by FROM rolling_segments WHERE text='rolling window 2'"
            ).fetchone()
            session_status = connection.execute(
                "SELECT status FROM sessions WHERE session_id=?", (first.session_id,)
            ).fetchone()[0]
        self.assertEqual(set(late_statuses.values()), {"pending"})
        self.assertEqual(late_segment, ("provisional", None))
        self.assertEqual(session_status, "open")
        self.assertEqual(len(self.raw_paths()), 4)

    def test_upload_after_drain_before_snapshot_stays_pending(self) -> None:
        first = self.ingest(0, speech=True, source_session="drain-snapshot-race")
        self.ingest(1, speech=False, source_session="drain-snapshot-race")
        asr = CountingASR()
        self.assertIsNotNone(self.archive.process_incremental(first.session_id, asr, self.gate))
        started = threading.Event()
        release = threading.Event()
        original_snapshot = self.archive._reconciliation_chunks

        def blocking_snapshot(session_id):  # type: ignore[no-untyped-def]
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release reconciliation snapshot")
            return original_snapshot(session_id)

        self.archive._reconciliation_chunks = blocking_snapshot  # type: ignore[method-assign]
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self.archive.finalize_incremental_session,
                first.session_id,
                asr,
                self.gate,
            )
            self.assertTrue(started.wait(timeout=5))
            late_speech = self.ingest(2, speech=True, source_session="drain-snapshot-race")
            late_silence = self.ingest(3, speech=False, source_session="drain-snapshot-race")
            release.set()
            version_one = future.result(timeout=5)
        self.archive._reconciliation_chunks = original_snapshot  # type: ignore[method-assign]
        self.assertNotIn(late_speech.chunk_id, version_one["evidence"]["chunk_ids"])
        self.assertNotIn(late_silence.chunk_id, version_one["evidence"]["chunk_ids"])
        with sqlite3.connect(self.archive.db_path) as connection:
            late_statuses = dict(
                connection.execute(
                    "SELECT chunk_id,status FROM chunks WHERE chunk_id IN (?,?)",
                    (late_speech.chunk_id, late_silence.chunk_id),
                )
            )
            classified_late = connection.execute(
                "SELECT COUNT(*) FROM rolling_chunk_state WHERE chunk_id IN (?,?)",
                (late_speech.chunk_id, late_silence.chunk_id),
            ).fetchone()[0]
            session_status = connection.execute(
                "SELECT status FROM sessions WHERE session_id=?", (first.session_id,)
            ).fetchone()[0]
        self.assertEqual(set(late_statuses.values()), {"pending"})
        self.assertEqual(classified_late, 0)
        self.assertEqual(session_status, "open")
        self.assertFalse(version_one["reconciliation"]["final"])
        interim_hits = self.archive.search("rolling window")
        self.assertEqual(interim_hits[0]["kind"], "canonical_transcript")
        self.assertEqual(interim_hits[0]["state"], "provisional")

        version_two = self.archive.finalize_incremental_session(first.session_id, asr, self.gate)
        self.assertIn(late_speech.chunk_id, version_two["evidence"]["chunk_ids"])
        self.assertIn("rolling window 2", version_two["transcript"]["text"])
        self.assertEqual(len(asr.calls), 2)
        self.assertEqual(len(self.raw_paths()), 4)

    def test_expired_claim_is_fenced_from_overwriting_retry_result(self) -> None:
        first = self.ingest(0, speech=True, source_session="claim-fence")
        self.ingest(1, speech=False, source_session="claim-fence")
        started = threading.Event()
        release = threading.Event()
        slow = BlockingASR(started, release, prefix="slow result")
        retry = CountingASR(prefix="retry result")
        with ThreadPoolExecutor(max_workers=1) as executor:
            slow_future = executor.submit(
                self.archive._process_incremental_unlocked,
                first.session_id,
                slow,
                self.gate,
                now_epoch=100.0,
            )
            self.assertTrue(started.wait(timeout=5))
            retry_event = self.archive._process_incremental_unlocked(
                first.session_id,
                retry,
                self.gate,
                now_epoch=161.0,
            )
            assert retry_event is not None
            release.set()
            slow_return = slow_future.result(timeout=5)
        assert slow_return is not None
        self.assertEqual(retry_event["transcript"]["text"], "retry result 1")
        self.assertEqual(slow_return["transcript"]["text"], "retry result 1")
        with sqlite3.connect(self.archive.db_path) as connection:
            connection.row_factory = sqlite3.Row
            stored = connection.execute(
                "SELECT text,event_json,event_path FROM rolling_windows WHERE session_id=?",
                (first.session_id,),
            ).fetchone()
            segments = connection.execute(
                "SELECT text FROM rolling_segments WHERE session_id=?",
                (first.session_id,),
            ).fetchall()
        file_event = json.loads((self.root / stored["event_path"]).read_text())
        self.assertEqual(stored["text"], "retry result 1")
        self.assertEqual(json.loads(stored["event_json"])["transcript"]["text"], "retry result 1")
        self.assertEqual(file_event["transcript"]["text"], "retry result 1")
        self.assertEqual([row[0] for row in segments], ["retry result 1"])
        self.assertEqual(list((self.root / "events").rglob("*.tmp")), [])

    def test_final_reconciliation_composes_windows_without_full_session_asr(self) -> None:
        first = self.ingest(0, speech=True, source_session="final")
        self.ingest(1, speech=False, source_session="final")
        asr = CountingASR()
        rolling = self.archive.process_incremental(first.session_id, asr, self.gate)
        assert rolling is not None
        live_hits = self.archive.search("rolling window")
        self.assertEqual(len(live_hits), 1)
        self.assertEqual(live_hits[0]["kind"], "rolling_segment")
        self.assertEqual(live_hits[0]["state"], "provisional")
        live_export = self.archive.export_day(self.now.date()).read_text()
        self.assertIn("Rolling transcript segment evidence without same-day canonical coverage", live_export)
        self.assertIn("rolling window 1", live_export)
        self.assertIn("must not be promoted as unquestioned memory", live_export)

        final = self.archive.finalize_incremental_session(first.session_id, asr, self.gate)
        self.assertTrue(final["reconciliation"]["final"])
        self.assertFalse(final["reconciliation"]["full_session_asr_rerun"])
        self.assertEqual(final["transcript"]["text"], "rolling window 1")
        self.assertEqual(len(asr.calls), 1)
        final_hits = self.archive.search("rolling window")
        self.assertEqual(len(final_hits), 1)
        self.assertEqual(final_hits[0]["kind"], "canonical_transcript")
        with sqlite3.connect(self.archive.db_path) as connection:
            session_status = connection.execute(
                "SELECT status FROM sessions WHERE session_id=?", (first.session_id,)
            ).fetchone()[0]
            segment_state = connection.execute(
                "SELECT state,reconciled_by FROM rolling_segments"
            ).fetchone()
            chunk_statuses = {
                row[0] for row in connection.execute(
                    "SELECT status FROM chunks WHERE session_id=?", (first.session_id,)
                )
            }
            search_documents = connection.execute(
                "SELECT document_id,kind FROM search_documents WHERE session_id=?",
                (first.session_id,),
            ).fetchall()
        self.assertEqual(session_status, "transcribed")
        self.assertEqual(segment_state[0], "finalized")
        self.assertEqual(segment_state[1], final["event_id"])
        self.assertEqual(chunk_statuses, {"processed"})
        self.assertEqual(search_documents, [(final["event_id"], "canonical_transcript")])
        final_export = self.archive.export_day(self.now.date()).read_text()
        self.assertIn("transcript v1", final_export)
        self.assertNotIn(
            "Rolling transcript segment evidence without same-day canonical coverage",
            final_export,
        )
        self.assertEqual(len(self.raw_paths()), 2)

    def test_cross_midnight_segments_remain_in_each_days_wiki_evidence(self) -> None:
        self.now = datetime(2026, 8, 22, 23, 59, 58, tzinfo=ZoneInfo("America/Denver"))
        first = self.ingest(0, speech=True, source_session="cross-midnight")
        self.ingest(1, speech=False, source_session="cross-midnight")
        self.ingest(2, speech=True, source_session="cross-midnight")
        self.ingest(3, speech=False, source_session="cross-midnight")
        asr = CountingASR()
        self.assertIsNotNone(self.archive.process_incremental(first.session_id, asr, self.gate))
        self.assertIsNotNone(self.archive.process_incremental(first.session_id, asr, self.gate))
        final = self.archive.finalize_incremental_session(first.session_id, asr, self.gate)
        assert final is not None
        observed_start = datetime.fromisoformat(final["timestamps"]["observed_start_estimate"])
        self.assertEqual(self.archive._daily_date(observed_start).isoformat(), "2026-08-22")

        asyncio.run(_export_session_days(self.archive, first.session_id))
        first_day_path = self.root / "wiki-inbox" / "2026-08-22.md"
        second_day_path = self.root / "wiki-inbox" / "2026-08-23.md"
        self.assertTrue(first_day_path.is_file())
        self.assertTrue(second_day_path.is_file())
        first_day = first_day_path.read_text()
        second_day = second_day_path.read_text()
        self.assertIn("transcript v1", first_day)
        self.assertIn("transcript v1", second_day)
        self.assertIn("rolling window 2", second_day)
        self.assertEqual(len(self.raw_paths()), 4)

    def test_segment_straddling_midnight_exports_to_both_days(self) -> None:
        self.now = datetime(2026, 8, 22, 23, 59, 59, tzinfo=ZoneInfo("America/Denver"))
        first = self.ingest(0, speech=True, source_session="straddle-midnight")
        self.ingest(1, speech=True, source_session="straddle-midnight")
        self.ingest(2, speech=False, source_session="straddle-midnight")
        asr = CountingASR()
        self.assertIsNotNone(self.archive.process_incremental(first.session_id, asr, self.gate))
        self.archive.finalize_incremental_session(first.session_id, asr, self.gate)
        asyncio.run(_export_session_days(self.archive, first.session_id))

        day_one = (self.root / "wiki-inbox" / "2026-08-22.md").read_text()
        day_two = (self.root / "wiki-inbox" / "2026-08-23.md").read_text()
        self.assertIn("transcript v1", day_one)
        self.assertIn("rolling window 1", day_two)
        self.assertIn("transcript v1", day_two)
        self.assertEqual(len(self.raw_paths()), 3)

        forced = self.archive.transcribe_session(
            first.session_id,
            StubASR("forced canonical replacement"),
            force=True,
        )
        self.assertEqual(forced["transcript"]["text"], "forced canonical replacement")
        asyncio.run(_export_session_days(self.archive, first.session_id))
        refreshed_after_force = (self.root / "wiki-inbox" / "2026-08-23.md").read_text()
        self.assertNotIn("rolling window 1", refreshed_after_force)
        self.assertIn("forced canonical replacement", refreshed_after_force)

        self.assertTrue(self.archive.delete_session(first.session_id))
        refreshed_day_two = (self.root / "wiki-inbox" / "2026-08-23.md").read_text()
        self.assertNotIn("rolling window 1", refreshed_day_two)
        self.assertEqual(len(self.raw_paths()), 0)

    def test_canonical_only_cross_midnight_session_exports_and_deletes_both_days(self) -> None:
        self.now = datetime(2026, 8, 22, 23, 59, 59, tzinfo=ZoneInfo("America/Denver"))
        first = self.ingest(0, speech=True, source_session="canonical-only-midnight")
        self.ingest(1, speech=True, source_session="canonical-only-midnight")
        event = self.archive.transcribe_session(
            first.session_id,
            StubASR("canonical only across midnight"),
        )
        self.assertEqual(event["timestamps"]["observed_end_estimate"][:10], "2026-08-23")
        asyncio.run(_export_session_days(self.archive, first.session_id))

        day_one_path = self.root / "wiki-inbox" / "2026-08-22.md"
        day_two_path = self.root / "wiki-inbox" / "2026-08-23.md"
        self.assertIn("canonical only across midnight", day_one_path.read_text())
        self.assertIn("canonical only across midnight", day_two_path.read_text())

        self.assertTrue(self.archive.delete_session(first.session_id))
        self.assertNotIn("canonical only across midnight", day_one_path.read_text())
        self.assertNotIn("canonical only across midnight", day_two_path.read_text())
        self.assertEqual(len(self.raw_paths()), 0)

    def test_concurrent_reconciliation_writers_share_one_canonical_version(self) -> None:
        first = self.ingest(0, speech=True, source_session="canonical-lock")
        self.ingest(1, speech=False, source_session="canonical-lock")
        self.assertIsNotNone(
            self.archive.process_incremental(first.session_id, CountingASR(), self.gate)
        )
        barrier = threading.Barrier(2)

        def reconcile() -> dict:
            barrier.wait()
            return self.archive.reconcile_incremental_session(first.session_id, final=False)

        with ThreadPoolExecutor(max_workers=2) as executor:
            events = list(executor.map(lambda _: reconcile(), range(2)))
        self.assertEqual(events[0]["event_id"], events[1]["event_id"])
        with sqlite3.connect(self.archive.db_path) as connection:
            connection.row_factory = sqlite3.Row
            transcripts = connection.execute(
                "SELECT transcript_id,event_path,event_json FROM transcripts WHERE session_id=?",
                (first.session_id,),
            ).fetchall()
        self.assertEqual(len(transcripts), 1)
        stored = transcripts[0]
        event_file = json.loads((self.root / stored["event_path"]).read_text())
        self.assertEqual(event_file, json.loads(stored["event_json"]))
        self.assertEqual(event_file["event_id"], stored["transcript_id"])

    def test_search_ranks_canonical_and_rolling_hits_before_applying_limit(self) -> None:
        canonical = self.ingest(0, speech=True, source_session="canonical-rank")
        self.archive.transcribe_session(
            canonical.session_id,
            StubASR("rank " * 20 + "target"),
        )
        for index in range(6):
            filler = self.ingest(
                0,
                speech=True,
                source_session=f"rank-common-filler-{index}",
            )
            self.archive.transcribe_session(filler.session_id, StubASR("rank filler"))
        rolling = self.ingest(0, speech=True, source_session="rolling-rank")
        self.ingest(1, speech=False, source_session="rolling-rank")
        self.assertIsNotNone(
            self.archive.process_incremental(
                rolling.session_id,
                CountingASR(prefix="rank target target"),
                self.gate,
            )
        )

        best = self.archive.search("rank target", limit=1)
        all_hits = self.archive.search("rank target", limit=2)
        self.assertEqual(best[0]["kind"], "rolling_segment")
        self.assertEqual({hit["kind"] for hit in all_hits}, {"canonical_transcript", "rolling_segment"})
        self.assertGreater(all_hits[0]["common_bm25_score"], all_hits[1]["common_bm25_score"])
        self.assertLessEqual(all_hits[0]["relevance_score"], all_hits[1]["relevance_score"])
        self.assertTrue(all("source_bm25" in hit for hit in all_hits))

    def test_search_ranks_every_eligible_hit_before_limit(self) -> None:
        latest_transcript_id = ""
        base_time = self.now
        for index in range(101):
            self.now = base_time + timedelta(seconds=index)
            chunk = self.ingest(
                0,
                speech=True,
                source_session=f"candidate-saturation-{index}",
            )
            event = self.archive.transcribe_session(
                chunk.session_id,
                StubASR("candidate saturation"),
            )
            latest_transcript_id = event["event_id"]
        best = self.archive.search("candidate saturation", limit=1)
        self.assertEqual(best[0]["transcript_id"], latest_transcript_id)
        self.assertEqual(best[0]["kind"], "canonical_transcript")
        self.assertEqual(len(self.raw_paths()), 101)

    def test_periodic_reconciliation_keeps_newer_segments_near_live(self) -> None:
        first = self.ingest(0, speech=True, source_session="periodic")
        self.ingest(1, speech=False, source_session="periodic")
        asr = CountingASR()
        self.archive.process_incremental(first.session_id, asr, self.gate)
        with sqlite3.connect(self.archive.db_path) as connection:
            connection.execute(
                "UPDATE rolling_windows SET committed_at=? WHERE session_id=?",
                (self.now.isoformat(), first.session_id),
            )
        due = self.archive.sessions_due_reconciliation(now_epoch=self.now.timestamp() + 1000)
        self.assertEqual(due, [first.session_id])
        snapshot = self.archive.reconcile_incremental_session(first.session_id, final=False)
        self.assertFalse(snapshot["reconciliation"]["final"])
        snapshot_hits = self.archive.search("rolling window")
        self.assertEqual(len(snapshot_hits), 1)
        self.assertEqual(snapshot_hits[0]["kind"], "canonical_transcript")
        self.assertEqual(snapshot_hits[0]["state"], "provisional")

        self.ingest(2, speech=True, source_session="periodic")
        self.ingest(3, speech=False, source_session="periodic")
        self.archive.process_incremental(first.session_id, asr, self.gate)
        hits = self.archive.search("rolling window")
        self.assertEqual({hit["kind"] for hit in hits}, {"canonical_transcript", "rolling_segment"})
        self.assertEqual(len(asr.calls), 2)
        self.assertEqual(len(self.raw_paths()), 4)


if __name__ == "__main__":
    unittest.main()
