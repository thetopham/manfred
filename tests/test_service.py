from __future__ import annotations

import asyncio
import hashlib
import math
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from manfred_ears.archive import AudioArchive, CaptureMetadata
from manfred_ears.asr import StubASR
from manfred_ears.config import Settings
from manfred_ears.episodes import EpisodeArchive
from manfred_ears.service import _run_transcription_cycle, create_operator_app, create_receiver_app
from manfred_ears.vad import EnergySpeechGate
from manfred_ears.vision import VisionArchive


def pcm() -> bytes:
    values = [int(3000 * math.sin(2 * math.pi * 220 * index / 16000)) for index in range(16000)]
    return struct.pack(f"<{len(values)}h", *values)


class BlockingStubASR:
    name = "blocking-stub-asr"

    def __init__(self, started: threading.Event, release: threading.Event, text: str) -> None:
        self.started = started
        self.release = release
        self.delegate = StubASR(text)

    def transcribe(self, audio_path: Path):  # type: ignore[no-untyped-def]
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release idle finalization ASR")
        return self.delegate.transcribe(audio_path)


class FlakyStubASR:
    name = "flaky-stub-asr"

    def __init__(self, text: str) -> None:
        self.calls = 0
        self.delegate = StubASR(text)

    def transcribe(self, audio_path: Path):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient model load failure")
        return self.delegate.transcribe(audio_path)


class UngatedStubASR(StubASR):
    supports_full_session_vad = False


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            state_dir=Path(self.temp.name) / "state",
            auth_token="receiver-secret",
            operator_token="operator-secret",
            asr_backend="stub",
            rolling_transcription_enabled=True,
            idle_flush_seconds=9999,
            max_body_bytes=64_000,
        )
        self.archive = AudioArchive(self.settings)
        asr = StubASR("searchable observed statement")
        self.receiver = TestClient(
            create_receiver_app(
                self.settings,
                archive=self.archive,
                asr=asr,
                start_worker=False,
            )
        )
        self.operator = TestClient(
            create_operator_app(
                self.settings,
                archive=self.archive,
                asr=asr,
            )
        )

    def tearDown(self) -> None:
        self.receiver.close()
        self.operator.close()
        self.temp.cleanup()

    def operator_headers(self, token: str = "operator-secret") -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def test_missing_tokens_refuse_app_creation(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "MANFRED_AUTH_TOKEN"):
            create_receiver_app(replace(self.settings, auth_token=None), start_worker=False)
        with self.assertRaisesRegex(RuntimeError, "MANFRED_OPERATOR_TOKEN"):
            create_operator_app(replace(self.settings, operator_token=None))

    def test_ungated_backend_refuses_full_session_production_mode(self) -> None:
        for rolling_enabled in (False, True):
            with self.subTest(rolling_enabled=rolling_enabled):
                unsafe_settings = replace(
                    self.settings,
                    rolling_transcription_enabled=rolling_enabled,
                )
                with self.assertRaisesRegex(RuntimeError, "full-session speech filtering"):
                    create_receiver_app(
                        unsafe_settings,
                        archive=self.archive,
                        asr=UngatedStubASR("unsafe"),
                        start_worker=False,
                    )

    def test_receiver_exposes_only_audio(self) -> None:
        for path in ("/health", "/v1/status", "/v1/search?q=x", "/docs", "/openapi.json"):
            self.assertEqual(self.receiver.get(path).status_code, 404, path)
        self.assertEqual(
            self.receiver.post(
                "/audio?sample_rate=16000&uid=u&token=receiver-secret",
                content=pcm(),
                headers={"Content-Type": "application/octet-stream"},
            ).status_code,
            202,
        )

    def test_operator_exposes_no_audio_and_uses_distinct_token(self) -> None:
        self.assertEqual(
            self.operator.post(
                "/audio?sample_rate=16000&uid=u&token=receiver-secret",
                content=pcm(),
                headers={"Content-Type": "application/octet-stream"},
            ).status_code,
            404,
        )
        health = self.operator.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(
            health.json()["speech_filter"],
            {
                "backend": "silero-vad",
                "rolling_enabled": self.settings.rolling_transcription_enabled,
                "full_session_parameters": self.settings.silero_vad_parameters(),
                "rolling_parameters": self.settings.rolling_silero_vad_parameters(),
            },
        )
        self.assertEqual(self.operator.get("/v1/status").status_code, 401)
        self.assertEqual(self.operator.get("/v1/status?token=operator-secret").status_code, 401)
        self.assertEqual(self.operator.get("/v1/status", headers=self.operator_headers("receiver-secret")).status_code, 401)
        self.assertEqual(self.operator.get("/v1/status", headers=self.operator_headers()).status_code, 200)

    def test_auth_content_type_dedupe_flush_search_delete(self) -> None:
        url = "/audio?sample_rate=16000&uid=cloud-user"
        self.assertEqual(
            self.receiver.post(url, content=pcm(), headers={"Content-Type": "application/octet-stream"}).status_code,
            401,
        )
        self.assertEqual(
            self.receiver.post(
                url + "&token=receiver-secret",
                content=pcm(),
                headers={"Content-Type": "audio/wav"},
            ).status_code,
            415,
        )
        headers = {"Content-Type": "application/octet-stream", "Idempotency-Key": "omi-delivery-1"}
        first = self.receiver.post(url + "&token=receiver-secret", content=pcm(), headers=headers)
        second = self.receiver.post(url + "&token=receiver-secret", content=pcm(), headers=headers)
        self.assertEqual(first.status_code, 202)
        self.assertFalse(first.json()["duplicate"])
        self.assertTrue(second.json()["duplicate"])
        session_id = first.json()["session_id"]
        flushed = self.operator.post(f"/v1/sessions/{session_id}/flush", headers=self.operator_headers())
        self.assertEqual(flushed.status_code, 200)
        self.assertEqual(flushed.json()["transcript"]["text"], "searchable observed statement")
        self.assertEqual(flushed.json()["reconciliation"]["strategy"], "full_session_asr")
        self.assertEqual(flushed.json()["transcript_version"], 1)

        repeated = self.operator.post(
            f"/v1/sessions/{session_id}/flush", headers=self.operator_headers()
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json()["transcript_version"], 1)

        forced = self.operator.post(
            f"/v1/sessions/{session_id}/flush?force=true", headers=self.operator_headers()
        )
        self.assertEqual(forced.status_code, 200)
        self.assertEqual(forced.json()["transcript_version"], 2)
        self.assertTrue(forced.json()["reconciliation"]["full_session_asr_rerun"])
        found = self.operator.get("/v1/search?q=observed+statement", headers=self.operator_headers())
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()["count"], 1)
        deleted = self.operator.delete(f"/v1/sessions/{session_id}", headers=self.operator_headers())
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["deleted"])

    def test_ordinary_flush_replaces_legacy_rolling_canonical_transcript(self) -> None:
        ingested = self.archive.ingest_pcm(
            uid="legacy-rolling-flush",
            sample_rate=16000,
            body=pcm(),
        )
        legacy = self.archive.finalize_incremental_session(
            ingested.session_id,
            StubASR("legacy rolling transcript"),
            EnergySpeechGate(),
        )
        self.assertIsNotNone(legacy)
        assert legacy is not None
        self.assertEqual(legacy["transcript_version"], 1)
        self.assertEqual(
            legacy["reconciliation"]["strategy"],
            "compose_committed_rolling_segments",
        )

        flushed = self.operator.post(
            f"/v1/sessions/{ingested.session_id}/flush",
            headers=self.operator_headers(),
        )
        self.assertEqual(flushed.status_code, 200)
        self.assertEqual(flushed.json()["transcript_version"], 2)
        self.assertEqual(flushed.json()["reconciliation"]["strategy"], "full_session_asr")
        self.assertEqual(flushed.json()["transcript"]["text"], "searchable observed statement")

    def test_direct_bridge_headers_reach_provenance_event(self) -> None:
        started = "2026-08-21T15:00:00+00:00"
        ended = "2026-08-21T15:00:01+00:00"
        body = pcm()
        body_sha256 = hashlib.sha256(body).hexdigest()
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Manfred-Token": "receiver-secret",
            "Idempotency-Key": f"s25-session:0:{body_sha256}",
            "X-Manfred-Content-SHA256": body_sha256,
            "X-Manfred-Capture-Session": "s25-session",
            "X-Manfred-Sequence": "0",
            "X-Manfred-Capture-Started-At": started,
            "X-Manfred-Capture-Ended-At": ended,
            "X-Manfred-Codec": "pcm16le",
            "X-Manfred-Transport": "s25-direct-ble-tailscale",
            "X-Manfred-Decoder-Generation": "3",
        }
        received = self.receiver.post(
            "/audio?sample_rate=16000&uid=omi-device",
            content=body,
            headers=headers,
        )
        self.assertEqual(received.status_code, 202)
        event = self.operator.post(
            f"/v1/sessions/{received.json()['session_id']}/flush",
            headers=self.operator_headers(),
        ).json()
        self.assertEqual(event["timestamps"]["observed_start_estimate"], started)
        self.assertEqual(event["timestamps"]["observed_end_estimate"], ended)
        self.assertEqual(event["source"]["transport"], "s25-direct-ble-tailscale")
        capture = event["evidence"]["chunk_capture"][0]
        self.assertEqual(capture["decoder_generation"], 3)
        self.assertEqual(capture["pcm_quality"]["peak_abs"], 3000)
        self.assertEqual(capture["pcm_quality"]["clipped_samples"], 0)
        self.assertAlmostEqual(capture["pcm_quality"]["dc_offset"], 0.0, places=6)
        self.assertGreater(capture["pcm_quality"]["rms"], 0.0)

        incomplete = dict(headers)
        incomplete.pop("X-Manfred-Capture-Ended-At")
        rejected = self.receiver.post(
            "/audio?sample_rate=16000&uid=omi-device",
            content=pcm(),
            headers=incomplete,
        )
        self.assertEqual(rejected.status_code, 422)

        generation_only = self.receiver.post(
            "/audio?sample_rate=16000&uid=omi-device",
            content=pcm(),
            headers={
                "Content-Type": "application/octet-stream",
                "X-Manfred-Token": "receiver-secret",
                "X-Manfred-Decoder-Generation": "3",
            },
        )
        self.assertEqual(generation_only.status_code, 422)

    def test_duplicate_audio_retry_repairs_missed_episode_dirty_mark(self) -> None:
        started = datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc)
        ended = started + timedelta(seconds=1)
        vision = VisionArchive(self.settings)
        visual = vision.ingest_noa_jpeg(
            image=b"\xff\xd8\xff\xe0retry-frame\xff\xd9",
            received_at=started,
            idempotency_key="retry-frame",
        )
        episodes = getattr(self.receiver.app, "state").episodes
        episode_id = episodes.ensure_visual(visual.visual_id)
        episodes.refresh_episode(
            episode_id,
            now_epoch=(started + timedelta(seconds=20)).timestamp(),
        )
        with sqlite3.connect(self.archive.db_path) as connection:
            connection.execute("DROP TRIGGER multimodal_chunk_insert_v1")
        body = pcm()
        direct = self.archive.ingest_pcm(
            uid="retry-device",
            sample_rate=16000,
            body=body,
            idempotency_key="retry-session:0",
            received_at=started + timedelta(minutes=1),
            capture=CaptureMetadata(
                source_session_id="retry-session",
                sequence_number=0,
                capture_started_at=started,
                capture_ended_at=ended,
                codec="pcm16le",
                transport="s25-direct-ble-tailscale",
            ),
        )
        self.assertFalse(episodes.get(episode_id)["dirty"])
        retry = self.receiver.post(
            "/audio?sample_rate=16000&uid=retry-device",
            content=body,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Manfred-Token": "receiver-secret",
                "Idempotency-Key": "retry-session:0",
                "X-Manfred-Capture-Session": "retry-session",
                "X-Manfred-Sequence": "0",
                "X-Manfred-Capture-Started-At": started.isoformat(),
                "X-Manfred-Capture-Ended-At": ended.isoformat(),
                "X-Manfred-Codec": "pcm16le",
                "X-Manfred-Transport": "s25-direct-ble-tailscale",
            },
        )
        self.assertEqual(retry.status_code, 202)
        self.assertTrue(retry.json()["duplicate"])
        self.assertEqual(retry.json()["chunk_id"], direct.chunk_id)
        self.assertTrue(episodes.get(episode_id)["dirty"])

    def test_committed_transcript_dirties_episode_even_if_export_step_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(
                self.settings,
                state_dir=Path(tmp) / "state",
                rolling_transcription_enabled=False,
                idle_flush_seconds=0,
                episode_finalize_grace_seconds=0,
            )
            archive = AudioArchive(settings)
            vision = VisionArchive(settings)
            episodes = EpisodeArchive(settings)
            started = datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc)
            ingested = archive.ingest_pcm(
                uid="transcript-crash-device",
                sample_rate=16000,
                body=pcm(),
                idempotency_key="transcript-crash:0",
                received_at=started + timedelta(seconds=2),
                capture=CaptureMetadata(
                    source_session_id="transcript-crash",
                    sequence_number=0,
                    capture_started_at=started,
                    capture_ended_at=started + timedelta(seconds=1),
                    codec="pcm16le",
                    transport="s25-direct-ble-tailscale",
                ),
            )
            visual = vision.ingest_noa_jpeg(
                image=b"\xff\xd8\xff\xe0transcript-crash-frame\xff\xd9",
                received_at=started,
                idempotency_key="transcript-crash-frame",
            )
            episode_id = episodes.ensure_visual(visual.visual_id)
            episodes.refresh_episode(
                episode_id,
                now_epoch=(started + timedelta(seconds=20)).timestamp(),
            )
            self.assertEqual(episodes.get(episode_id)["transcript_segment_ids"], [])
            with mock.patch(
                "manfred_ears.service._export_session_days",
                new=mock.AsyncMock(side_effect=RuntimeError("simulated export failure")),
            ):
                asyncio.run(
                    _run_transcription_cycle(
                        settings,
                        archive,
                        StubASR("committed despite export failure"),
                        EnergySpeechGate(),
                        episodes,
                        now_epoch=(started + timedelta(minutes=1)).timestamp(),
                    )
                )
            with sqlite3.connect(archive.db_path) as connection:
                transcript_count = connection.execute(
                    "SELECT COUNT(*) FROM transcripts WHERE session_id=?",
                    (ingested.session_id,),
                ).fetchone()[0]
            self.assertEqual(transcript_count, 1)
            record = episodes.get(episode_id)
            self.assertFalse(record["dirty"])
            self.assertEqual(len(record["transcript_segments"]), 1)
            self.assertEqual(
                record["transcript_segments"][0]["text"],
                "committed despite export failure",
            )

    def test_direct_bridge_validates_declared_and_idempotency_hashes_on_first_ingest(self) -> None:
        body = pcm()
        digest = hashlib.sha256(body).hexdigest()
        base_headers = {
            "Content-Type": "application/octet-stream",
            "X-Manfred-Token": "receiver-secret",
            "X-Manfred-Capture-Session": "hash-session",
            "X-Manfred-Sequence": "0",
            "X-Manfred-Capture-Started-At": "2026-08-21T15:00:00+00:00",
            "X-Manfred-Capture-Ended-At": "2026-08-21T15:00:01+00:00",
            "X-Manfred-Codec": "pcm16le",
            "X-Manfred-Transport": "s25-direct-ble-tailscale",
        }

        wrong_declared = self.receiver.post(
            "/audio?sample_rate=16000&uid=omi-device",
            content=body,
            headers={
                **base_headers,
                "Idempotency-Key": f"hash-session:0:{digest}",
                "X-Manfred-Content-SHA256": "0" * 64,
            },
        )
        self.assertEqual(wrong_declared.status_code, 422)
        self.assertEqual(self.archive.status()["chunks"], 0)

        wrong_embedded = self.receiver.post(
            "/audio?sample_rate=16000&uid=omi-device",
            content=body,
            headers={
                **base_headers,
                "Idempotency-Key": f"hash-session:0:{'f' * 64}",
                "X-Manfred-Content-SHA256": digest,
            },
        )
        self.assertEqual(wrong_embedded.status_code, 422)
        self.assertEqual(self.archive.status()["chunks"], 0)

        accepted = self.receiver.post(
            "/audio?sample_rate=16000&uid=omi-device",
            content=body,
            headers={
                **base_headers,
                "Idempotency-Key": f"hash-session:0:{digest}",
                "X-Manfred-Content-SHA256": digest,
            },
        )
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(self.archive.status()["chunks"], 1)

        legacy_headers = {
            **base_headers,
            "Idempotency-Key": "legacy-hash-session:0",
            "X-Manfred-Capture-Session": "legacy-hash-session",
        }
        legacy = self.receiver.post(
            "/audio?sample_rate=16000&uid=omi-device",
            content=body,
            headers=legacy_headers,
        )
        self.assertEqual(legacy.status_code, 202)
        self.assertEqual(self.archive.status()["chunks"], 2)

    def test_streaming_body_limit_rejects_before_archive_write(self) -> None:
        oversized = b"\x00\x00" * 40_000
        url = "/audio?sample_rate=16000&uid=u&token=receiver-secret"
        response = self.receiver.post(
            url,
            content=oversized,
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.archive.status()["chunks"], 0)

        # Content-Length is only an early rejection hint; streaming still
        # enforces the limit when a sender lies about it.
        response = self.receiver.post(
            url,
            content=oversized,
            headers={"Content-Type": "application/octet-stream", "Content-Length": "2"},
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.archive.status()["chunks"], 0)

    def test_worker_transcribes_new_audio_while_session_is_still_active(self) -> None:
        live_settings = replace(
            self.settings,
            state_dir=Path(self.temp.name) / "live-state",
            rolling_window_seconds=1,
            idle_flush_seconds=9999,
        )
        archive = AudioArchive(live_settings)
        ingested = archive.ingest_pcm(uid="live-omi", sample_rate=16000, body=pcm())
        asyncio.run(
            _run_transcription_cycle(
                live_settings,
                archive,
                StubASR("near live searchable phrase"),
                EnergySpeechGate(),
                now_epoch=time.time(),
            )
        )
        hits = archive.search("near live searchable")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "rolling_segment")
        self.assertIn(ingested.session_id, archive.open_sessions())

    def test_worker_rechecks_idleness_before_finalizing(self) -> None:
        final_settings = replace(
            self.settings,
            state_dir=Path(self.temp.name) / "idle-race-state",
            rolling_window_seconds=10,
            idle_flush_seconds=20,
        )
        archive = AudioArchive(final_settings)
        base_time = datetime.now(timezone.utc)
        first = archive.ingest_pcm(
            uid="idle-race-omi",
            sample_rate=16000,
            body=pcm(),
            received_at=base_time,
        )
        original_due = archive.sessions_due_reconciliation
        late_session_id = ""

        def revive_before_finalize(now_epoch=None):  # type: ignore[no-untyped-def]
            nonlocal late_session_id
            late = archive.ingest_pcm(
                uid="idle-race-omi",
                sample_rate=16000,
                body=pcm(),
                received_at=base_time + timedelta(seconds=20.5),
            )
            late_session_id = late.session_id
            return original_due(now_epoch=now_epoch)

        archive.sessions_due_reconciliation = revive_before_finalize  # type: ignore[method-assign]
        asyncio.run(
            _run_transcription_cycle(
                final_settings,
                archive,
                StubASR("must not finalize stale idle snapshot"),
                EnergySpeechGate(),
                now_epoch=base_time.timestamp() + 21,
            )
        )
        archive.sessions_due_reconciliation = original_due  # type: ignore[method-assign]

        self.assertEqual(late_session_id, first.session_id)
        self.assertIn(first.session_id, archive.open_sessions())
        self.assertEqual(archive.search("must not finalize"), [])
        self.assertEqual(len(list((final_settings.state_dir / "raw").rglob("*.pcm"))), 2)

    def test_full_session_finalizer_rechecks_idle_cutoff_inside_archive_lock(self) -> None:
        final_settings = replace(
            self.settings,
            state_dir=Path(self.temp.name) / "idle-cutoff-state",
            rolling_transcription_enabled=False,
            idle_flush_seconds=20,
        )
        archive = AudioArchive(final_settings)
        base_time = datetime.now(timezone.utc)
        first = archive.ingest_pcm(
            uid="idle-cutoff-omi",
            sample_rate=16000,
            body=pcm(),
            received_at=base_time,
        )
        original_transcribe = archive.transcribe_session

        def revive_inside_finalizer(session_id, asr, **kwargs):  # type: ignore[no-untyped-def]
            late = archive.ingest_pcm(
                uid="idle-cutoff-omi",
                sample_rate=16000,
                body=pcm(),
                received_at=base_time + timedelta(seconds=20.5),
            )
            self.assertEqual(late.session_id, first.session_id)
            return original_transcribe(session_id, asr, **kwargs)

        archive.transcribe_session = revive_inside_finalizer  # type: ignore[method-assign]
        asyncio.run(
            _run_transcription_cycle(
                final_settings,
                archive,
                StubASR("must not finalize revived full session"),
                EnergySpeechGate(),
                now_epoch=base_time.timestamp() + 21,
            )
        )

        self.assertIn(first.session_id, archive.open_sessions())
        self.assertEqual(archive.search("must not finalize revived"), [])

    def test_worker_idle_finalization_is_atomic_with_revival(self) -> None:
        final_settings = replace(
            self.settings,
            state_dir=Path(self.temp.name) / "idle-atomic-state",
            rolling_window_seconds=10,
            idle_flush_seconds=20,
        )
        archive = AudioArchive(final_settings)
        base_time = datetime.now(timezone.utc)
        first = archive.ingest_pcm(
            uid="idle-atomic-omi",
            sample_rate=16000,
            body=pcm(),
            received_at=base_time,
        )
        started = threading.Event()
        release = threading.Event()
        asr = BlockingStubASR(started, release, "atomic idle snapshot phrase")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                lambda: asyncio.run(
                    _run_transcription_cycle(
                        final_settings,
                        archive,
                        asr,
                        EnergySpeechGate(),
                        now_epoch=base_time.timestamp() + 21,
                    )
                )
            )
            self.assertTrue(started.wait(timeout=5))
            late = archive.ingest_pcm(
                uid="idle-atomic-omi",
                sample_rate=16000,
                body=pcm(),
                received_at=base_time + timedelta(seconds=20.5),
            )
            release.set()
            future.result(timeout=10)

        self.assertEqual(late.session_id, first.session_id)
        self.assertIn(first.session_id, archive.open_sessions())
        hits = archive.search("atomic idle snapshot")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "canonical_transcript")
        self.assertEqual(hits[0]["state"], "provisional")
        self.assertTrue(hits[0]["event"]["reconciliation"]["full_session_asr_rerun"])
        self.assertFalse(hits[0]["event"]["reconciliation"]["final"])
        next_chunk = archive.ingest_pcm(
            uid="idle-atomic-omi",
            sample_rate=16000,
            body=pcm(),
            received_at=base_time + timedelta(seconds=21),
        )
        self.assertEqual(next_chunk.session_id, first.session_id)
        self.assertEqual(len(list((final_settings.state_dir / "raw").rglob("*.pcm"))), 3)

    def test_failed_full_session_asr_retries_after_bounded_backoff(self) -> None:
        final_settings = replace(
            self.settings,
            state_dir=Path(self.temp.name) / "retry-state",
            rolling_transcription_enabled=False,
            idle_flush_seconds=1,
            rolling_recovery_seconds=2,
        )
        archive = AudioArchive(final_settings)
        base_time = datetime.now(timezone.utc)
        ingested = archive.ingest_pcm(
            uid="retry-omi",
            sample_rate=16000,
            body=pcm(),
            received_at=base_time,
        )
        asr = FlakyStubASR("recovered full-session transcript")

        asyncio.run(
            _run_transcription_cycle(
                final_settings,
                archive,
                asr,
                EnergySpeechGate(),
                now_epoch=base_time.timestamp() + 2,
            )
        )
        self.assertEqual(asr.calls, 1)
        self.assertEqual(archive.search("recovered full session"), [])

        asyncio.run(
            _run_transcription_cycle(
                final_settings,
                archive,
                asr,
                EnergySpeechGate(),
                now_epoch=time.time() + 1,
            )
        )
        self.assertEqual(asr.calls, 1)

        asyncio.run(
            _run_transcription_cycle(
                final_settings,
                archive,
                asr,
                EnergySpeechGate(),
                now_epoch=time.time() + 3,
            )
        )
        self.assertEqual(asr.calls, 2)
        hits = archive.search("recovered full session")
        self.assertEqual(len(hits), 1)
        self.assertNotIn(ingested.session_id, archive.open_sessions())

    def test_worker_defers_asr_until_idle_when_rolling_is_disabled(self) -> None:
        final_settings = replace(
            self.settings,
            state_dir=Path(self.temp.name) / "deferred-state",
            rolling_transcription_enabled=False,
            idle_flush_seconds=20,
        )
        archive = AudioArchive(final_settings)
        base_time = datetime.now(timezone.utc)
        ingested = archive.ingest_pcm(
            uid="deferred-omi",
            sample_rate=16000,
            body=pcm(),
            received_at=base_time,
        )
        asr = StubASR("deferred full-session transcript")

        asyncio.run(
            _run_transcription_cycle(
                final_settings,
                archive,
                asr,
                EnergySpeechGate(),
                now_epoch=base_time.timestamp() + 10,
            )
        )
        self.assertEqual(archive.search("deferred full session"), [])
        self.assertIn(ingested.session_id, archive.open_sessions())

        asyncio.run(
            _run_transcription_cycle(
                final_settings,
                archive,
                asr,
                EnergySpeechGate(),
                now_epoch=base_time.timestamp() + 21,
            )
        )
        hits = archive.search("deferred full session")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0]["event"]["reconciliation"]["full_session_asr_rerun"])
        self.assertNotIn(ingested.session_id, archive.open_sessions())

    def test_worker_finalizes_the_tail_after_capture_becomes_idle(self) -> None:
        final_settings = replace(
            self.settings,
            state_dir=Path(self.temp.name) / "final-state",
            rolling_window_seconds=10,
            idle_flush_seconds=20,
        )
        archive = AudioArchive(final_settings)
        ingested = archive.ingest_pcm(uid="idle-omi", sample_rate=16000, body=pcm())
        asyncio.run(
            _run_transcription_cycle(
                final_settings,
                archive,
                StubASR("idle tail finalized phrase"),
                EnergySpeechGate(),
                now_epoch=time.time() + 21,
            )
        )
        hits = archive.search("idle tail finalized")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "canonical_transcript")
        self.assertTrue(hits[0]["event"]["reconciliation"]["full_session_asr_rerun"])
        self.assertNotIn(ingested.session_id, archive.open_sessions())
        self.assertEqual(len(list((final_settings.state_dir / "raw").rglob("*.pcm"))), 1)

    def test_rejects_odd_pcm(self) -> None:
        response = self.receiver.post(
            "/audio?sample_rate=16000&uid=u&token=receiver-secret",
            content=b"\x00",
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
