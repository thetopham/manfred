from __future__ import annotations

import json
import math
import sqlite3
import struct
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from manfred_ears.archive import AudioArchive, CaptureMetadata
from manfred_ears.asr import TranscriptResult, TranscriptSegment
from manfred_ears.config import Settings
from manfred_ears.episodes import EpisodeArchive
from manfred_ears.vision import VisionArchive


JPEG = b"\xff\xd8\xff\xe0episode-frame\xff\xd9"


def pcm(seconds: float = 1.0, sample_rate: int = 16000) -> bytes:
    values = [
        int(4000 * math.sin(2 * math.pi * 440.0 * index / sample_rate))
        for index in range(int(seconds * sample_rate))
    ]
    return struct.pack(f"<{len(values)}h", *values)


class SegmentASR:
    name = "segment-asr"
    supports_full_session_vad = True

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        del audio_path
        return TranscriptResult(
            text="the slide says gradient descent",
            model=self.name,
            language="en",
            language_probability=1.0,
            confidence=None,
            avg_logprob=-0.1,
            no_speech_probability=0.0,
            latency_seconds=0.01,
            segments=[
                TranscriptSegment(
                    start=4.0,
                    end=6.0,
                    text="the slide says gradient descent",
                    avg_logprob=-0.1,
                    no_speech_prob=0.0,
                )
            ],
        )


class GapASR:
    name = "gap-asr"
    supports_full_session_vad = True

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        del audio_path
        return TranscriptResult(
            text="after the missing interval",
            model=self.name,
            language="en",
            language_probability=1.0,
            confidence=None,
            avg_logprob=-0.1,
            no_speech_probability=0.0,
            latency_seconds=0.01,
            segments=[TranscriptSegment(start=1.0, end=2.0, text="after the missing interval")],
        )


class EpisodeArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "state"
        self.settings = Settings(
            state_dir=self.root,
            asr_backend="stub",
            episode_context_seconds=15,
            episode_finalize_grace_seconds=0,
        )
        self.audio = AudioArchive(self.settings)
        self.vision = VisionArchive(self.settings)
        self.episodes = EpisodeArchive(self.settings)
        self.anchor = datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_visual(self, when: datetime, key: str = "visual-1") -> tuple[str, str]:
        visual = self.vision.ingest_noa_jpeg(
            image=JPEG,
            received_at=when,
            idempotency_key=key,
            client_reported_time=when.replace(tzinfo=None).isoformat(),
        )
        episode_id = self.episodes.ensure_visual(visual.visual_id)
        return visual.visual_id, episode_id

    def add_direct_audio(
        self,
        started: datetime,
        *,
        seconds: float = 1.0,
        sequence: int = 0,
        source_session: str = "s25-episode-session",
        received_offset: float = 300.0,
    ):
        return self.audio.ingest_pcm(
            uid="episode-omi",
            sample_rate=16000,
            body=pcm(seconds),
            idempotency_key=f"{source_session}:{sequence}",
            received_at=started + timedelta(seconds=received_offset),
            capture=CaptureMetadata(
                source_session_id=source_session,
                sequence_number=sequence,
                capture_started_at=started,
                capture_ended_at=started + timedelta(seconds=seconds),
                codec="pcm16le",
                transport="s25-direct-ble-tailscale",
            ),
        )

    def test_pending_episode_becomes_exact_plus_minus_fifteen_second_join(self) -> None:
        visual_id, episode_id = self.add_visual(self.anchor)
        before = self.add_direct_audio(
            self.anchor - timedelta(seconds=16), sequence=0
        )
        inside_before = self.add_direct_audio(
            self.anchor - timedelta(seconds=14), sequence=1
        )
        inside_after = self.add_direct_audio(
            self.anchor + timedelta(seconds=14), sequence=2
        )
        after = self.add_direct_audio(
            self.anchor + timedelta(seconds=15), sequence=3
        )

        pending = self.episodes.refresh_episode(
            episode_id,
            now_epoch=(self.anchor + timedelta(seconds=14)).timestamp(),
        )
        self.assertEqual(pending.state, "pending")
        self.assertEqual(self.episodes.get(episode_id)["audio_chunk_ids"], [])

        ready = self.episodes.refresh_episode(
            episode_id,
            now_epoch=(self.anchor + timedelta(seconds=16)).timestamp(),
        )
        self.assertEqual(ready.state, "ready")
        record = self.episodes.get(episode_id)
        self.assertEqual(record["start"], (self.anchor - timedelta(seconds=15)).isoformat())
        self.assertEqual(record["end"], (self.anchor + timedelta(seconds=15)).isoformat())
        self.assertEqual(record["clock_basis"], "noa-request-receive-time")
        self.assertEqual(record["timing_quality"], "low")
        self.assertIsNone(record["capture_uncertainty_seconds"])
        self.assertEqual(record["image_ids"], [visual_id])
        self.assertEqual(
            record["audio_chunk_ids"],
            [inside_before.chunk_id, inside_after.chunk_id],
        )
        self.assertNotIn(before.chunk_id, record["audio_chunk_ids"])
        self.assertNotIn(after.chunk_id, record["audio_chunk_ids"])
        self.assertEqual(record["event_ids"], [f"noa-tap-query:{visual_id}"])

    def test_late_audio_marks_ready_episode_dirty_and_creates_new_version(self) -> None:
        _, episode_id = self.add_visual(self.anchor)
        self.episodes.refresh_episode(
            episode_id,
            now_epoch=(self.anchor + timedelta(seconds=16)).timestamp(),
        )
        original = self.episodes.get(episode_id)
        self.assertFalse(original["dirty"])

        late = self.add_direct_audio(self.anchor, received_offset=3600)
        dirty = self.episodes.get(episode_id)
        self.assertTrue(dirty["dirty"])

        refreshed = self.episodes.refresh_due(
            now_epoch=(self.anchor + timedelta(hours=2)).timestamp()
        )
        self.assertEqual(len(refreshed), 1)
        current = self.episodes.get(episode_id)
        self.assertEqual(current["version"], original["version"] + 1)
        self.assertEqual(current["audio_chunk_ids"], [late.chunk_id])
        self.assertFalse(current["dirty"])

    def test_late_canonical_transcript_adds_bounded_segment_reference(self) -> None:
        _, episode_id = self.add_visual(self.anchor)
        audio = self.add_direct_audio(
            self.anchor - timedelta(seconds=5),
            seconds=10,
            sequence=0,
        )
        self.episodes.mark_chunk_changed(audio.chunk_id)
        self.episodes.refresh_episode(
            episode_id,
            now_epoch=(self.anchor + timedelta(seconds=16)).timestamp(),
        )
        before = self.episodes.get(episode_id)
        self.assertEqual(before["transcript_segment_ids"], [])

        event = self.audio.transcribe_session(audio.session_id, SegmentASR(), force=True)
        self.assertIsNotNone(event)
        self.assertTrue(self.episodes.get(episode_id)["dirty"])
        self.episodes.refresh_due(now_epoch=(self.anchor + timedelta(hours=1)).timestamp())

        after = self.episodes.get(episode_id)
        self.assertEqual(after["version"], before["version"] + 1)
        self.assertEqual(len(after["transcript_segments"]), 1)
        segment = after["transcript_segments"][0]
        self.assertEqual(segment["text"], "the slide says gradient descent")
        self.assertEqual(segment["session_id"], audio.session_id)
        self.assertEqual(segment["timestamp_basis"], "s25_pcm_sample_clock")
        self.assertEqual(segment["capture_time_quality"], "phone-derived-capture-time")
        self.assertEqual(
            segment["observed_start"],
            (self.anchor - timedelta(seconds=1)).isoformat(),
        )
        self.assertEqual(segment["observed_end"], (self.anchor + timedelta(seconds=1)).isoformat())
        self.assertEqual(after["transcript_segment_ids"], [segment["segment_id"]])

    def test_segment_offsets_follow_chunk_capture_clock_across_a_gap(self) -> None:
        first = self.add_direct_audio(
            self.anchor - timedelta(seconds=10),
            sequence=0,
            source_session="s25-gap-session",
        )
        second = self.add_direct_audio(
            self.anchor + timedelta(seconds=5),
            sequence=1,
            source_session="s25-gap-session",
        )
        _, episode_id = self.add_visual(
            self.anchor + timedelta(seconds=5, milliseconds=500),
            key="gap-visual",
        )
        event = self.audio.transcribe_session(first.session_id, GapASR(), force=True)
        self.assertIsNotNone(event)
        self.assertEqual(first.session_id, second.session_id)
        self.episodes.mark_session_changed(first.session_id)
        self.episodes.refresh_episode(
            episode_id,
            now_epoch=(self.anchor + timedelta(seconds=30)).timestamp(),
        )
        segment = self.episodes.get(episode_id)["transcript_segments"][0]
        self.assertEqual(
            segment["observed_start"],
            (self.anchor + timedelta(seconds=5)).isoformat(),
        )
        self.assertEqual(
            segment["observed_end"],
            (self.anchor + timedelta(seconds=6)).isoformat(),
        )

    def test_preexisting_visual_is_backfilled_when_episode_archive_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            settings = Settings(state_dir=state, asr_backend="stub")
            AudioArchive(settings)
            vision = VisionArchive(settings)
            visual = vision.ingest_noa_jpeg(
                image=JPEG,
                received_at=self.anchor,
                idempotency_key="preexisting-visual",
            )
            episodes = EpisodeArchive(settings)
            record = episodes.get(episodes._episode_id(visual.visual_id))
            self.assertEqual(record["anchor_visual_id"], visual.visual_id)
            self.assertEqual(record["state"], "pending")
            with sqlite3.connect(state / "archive.sqlite3") as connection:
                triggers = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger'"
                    )
                }
            self.assertTrue(
                {
                    "multimodal_visual_insert_v1",
                    "multimodal_visual_delete_v1",
                    "multimodal_chunk_insert_v1",
                    "multimodal_chunk_delete_v1",
                    "multimodal_transcript_insert_v1",
                    "multimodal_transcript_delete_v1",
                }.issubset(triggers)
            )

    def test_visual_delete_trigger_removes_anchor_episode_atomically(self) -> None:
        visual_id, episode_id = self.add_visual(self.anchor, key="delete-trigger")
        self.assertTrue(self.vision.delete(visual_id))
        with self.assertRaises(KeyError):
            self.episodes.get(episode_id)

    def test_startup_reconciliation_removes_pretrigger_orphan(self) -> None:
        visual_id, episode_id = self.add_visual(self.anchor, key="orphan-recovery")
        with sqlite3.connect(self.audio.db_path) as connection:
            connection.execute("DROP TRIGGER multimodal_visual_delete_v1")
            connection.execute("DELETE FROM visual_events WHERE visual_id=?", (visual_id,))
        recovered = EpisodeArchive(self.settings)
        with self.assertRaises(KeyError):
            recovered.get(episode_id)

    def test_reconciled_rolling_segments_keep_wall_clock_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            settings = Settings(
                state_dir=state,
                asr_backend="stub",
                episode_context_seconds=0.25,
                episode_finalize_grace_seconds=0,
            )
            audio = AudioArchive(settings)
            vision = VisionArchive(settings)
            episodes = EpisodeArchive(settings)
            first_started = self.anchor - timedelta(seconds=10)
            second_started = self.anchor + timedelta(seconds=5)
            first = audio.ingest_pcm(
                uid="rolling-gap-device",
                sample_rate=16000,
                body=pcm(),
                idempotency_key="rolling-gap:0",
                received_at=self.anchor + timedelta(minutes=5),
                capture=CaptureMetadata(
                    source_session_id="rolling-gap",
                    sequence_number=0,
                    capture_started_at=first_started,
                    capture_ended_at=first_started + timedelta(seconds=1),
                    codec="pcm16le",
                    transport="s25-direct-ble-tailscale",
                ),
            )
            audio.ingest_pcm(
                uid="rolling-gap-device",
                sample_rate=16000,
                body=pcm(),
                idempotency_key="rolling-gap:1",
                received_at=self.anchor + timedelta(minutes=5, seconds=1),
                capture=CaptureMetadata(
                    source_session_id="rolling-gap",
                    sequence_number=1,
                    capture_started_at=second_started,
                    capture_ended_at=second_started + timedelta(seconds=1),
                    codec="pcm16le",
                    transport="s25-direct-ble-tailscale",
                ),
            )
            visual = vision.ingest_noa_jpeg(
                image=JPEG,
                received_at=second_started + timedelta(milliseconds=500),
                idempotency_key="rolling-gap-visual",
            )
            episode_id = episodes.ensure_visual(visual.visual_id)
            episodes.refresh_episode(
                episode_id,
                now_epoch=(self.anchor + timedelta(seconds=30)).timestamp(),
            )
            transcript_id = f"transcript-{first.session_id}-v1"
            event = {
                "timestamps": {
                    "observed_start_estimate": first_started.isoformat(),
                    "observed_end_estimate": (second_started + timedelta(seconds=1)).isoformat(),
                    "timestamp_basis": "s25_pcm_sample_clock",
                    "capture_time_quality": "phone-derived-capture-time",
                },
                "evidence": {"chunk_capture": []},
                "transcript": {
                    "text": "first window second window",
                    "segments": [
                        {"start": 0.0, "end": 1.0, "text": "first window"},
                        {"start": 15.0, "end": 16.0, "text": "second window"},
                    ],
                },
                "reconciliation": {"strategy": "compose_committed_rolling_segments"},
            }
            with sqlite3.connect(audio.db_path) as connection:
                connection.execute(
                    """INSERT INTO transcripts(
                           transcript_id,session_id,version,observed_date,transcribed_at,model,
                           language,confidence,avg_logprob,no_speech_probability,latency_seconds,
                           duration_seconds,audio_sha256,text,event_path,event_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        transcript_id,
                        first.session_id,
                        1,
                        first_started.date().isoformat(),
                        datetime.now(timezone.utc).isoformat(),
                        "rolling-test",
                        "en",
                        None,
                        None,
                        None,
                        0.01,
                        2.0,
                        "0" * 64,
                        "first window second window",
                        "events/test.json",
                        json.dumps(event),
                    ),
                )
            self.assertTrue(episodes.get(episode_id)["dirty"])
            episodes.refresh_due(now_epoch=(self.anchor + timedelta(minutes=1)).timestamp())
            refs = episodes.get(episode_id)["transcript_segments"]
            self.assertEqual([ref["text"] for ref in refs], ["second window"])
            self.assertEqual(refs[0]["observed_start"], second_started.isoformat())
            self.assertEqual(
                refs[0]["observed_end"],
                (second_started + timedelta(seconds=1)).isoformat(),
            )

    def test_nearby_frame_is_included_without_copying_visual_payload(self) -> None:
        first_visual, first_episode = self.add_visual(self.anchor, key="visual-first")
        second_visual, _ = self.add_visual(
            self.anchor + timedelta(seconds=10), key="visual-second"
        )
        self.episodes.refresh_episode(
            first_episode,
            now_epoch=(self.anchor + timedelta(seconds=16)).timestamp(),
        )
        record = self.episodes.get(first_episode)
        self.assertEqual(record["image_ids"], [first_visual, second_visual])
        self.assertNotIn("path", record)
        self.assertNotIn("image", record)


if __name__ == "__main__":
    unittest.main()
