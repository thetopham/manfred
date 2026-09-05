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
from manfred_ears.config import Settings
from manfred_ears.service import create_operator_app, create_vision_receiver_app
from manfred_ears.vision import VisionArchive


JPEG_A = b"\xff\xd8\xff\xe0frame-a\xff\xd9"
JPEG_B = b"\xff\xd8\xff\xe0frame-b\xff\xd9"
AUDIO_WAV = b"RIFF" + (b"\x00" * 40)


class VisionArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            state_dir=Path(self.temp.name) / "state",
            auth_token="receiver-secret",
            operator_token="operator-secret",
            vision_token="vision-secret",
            asr_backend="stub",
        )
        self.archive = VisionArchive(self.settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_ingest_preserves_exact_jpeg_and_minimal_provenance(self) -> None:
        received = datetime(2026, 8, 23, 20, 30, tzinfo=timezone.utc)
        result = self.archive.ingest_noa_jpeg(
            image=JPEG_A,
            frame_audio=AUDIO_WAV,
            client_reported_time="2026-08-23 14:30:00.000",
            received_at=received,
        )

        self.assertFalse(result.duplicate)
        record = self.archive.get(result.visual_id)
        self.assertEqual(record["received_at"], received.isoformat())
        self.assertEqual(record["capture_time_basis"], "noa-request-receive-time")
        self.assertEqual(record["client_reported_time"], "2026-08-23 14:30:00.000")
        self.assertEqual(record["trigger"], "noa-tap-query")
        self.assertEqual(record["source"], "brilliant-frame-via-noa")
        self.assertEqual((self.settings.state_dir / record["path"]).read_bytes(), JPEG_A)

        sidecar = json.loads((self.settings.state_dir / record["metadata_path"]).read_text())
        self.assertNotIn("messages", sidecar)
        self.assertNotIn("location", sidecar)
        self.assertEqual(sidecar["frame_audio_num_bytes"], len(AUDIO_WAV))
        self.assertRegex(sidecar["frame_audio_sha256"], r"^[0-9a-f]{64}$")

    def test_ingest_fsyncs_evidence_names_before_index_insert(self) -> None:
        events: list[tuple[str, Path | None]] = []
        original_fsync = self.archive._fsync_directory
        original_insert = self.archive._insert_metadata
        original_replace = os.replace

        def fsync_directory(path: Path) -> None:
            original_fsync(path)
            events.append(("fsync", path.resolve()))

        def replace_path(source: Path, destination: Path) -> None:
            original_replace(source, destination)
            events.append(("replace", Path(destination).resolve()))

        def insert_metadata(metadata):  # type: ignore[no-untyped-def]
            events.append(("insert", None))
            return original_insert(metadata)

        with mock.patch.object(self.archive, "_fsync_directory", side_effect=fsync_directory):
            with mock.patch("manfred_ears.vision.os.replace", side_effect=replace_path):
                with mock.patch.object(self.archive, "_insert_metadata", side_effect=insert_metadata):
                    self.archive.ingest_noa_jpeg(
                        image=JPEG_A,
                        frame_audio=AUDIO_WAV,
                        client_reported_time="2026-08-23 14:30:00.000",
                        received_at=datetime(2026, 8, 23, 20, 30, tzinfo=timezone.utc),
                    )

        insert_index = events.index(("insert", None))
        before_insert = events[:insert_index]
        daily_directory = (self.archive.visual_root / "2026-08-23").resolve()
        self.assertIn(("fsync", self.archive.visual_root.resolve()), before_insert)
        self.assertGreaterEqual(before_insert.count(("fsync", daily_directory)), 3)
        image_replace = next(
            index
            for index, event in enumerate(events)
            if event[0] == "replace" and event[1] is not None and event[1].suffix == ".jpg"
        )
        metadata_replace = next(
            index
            for index, event in enumerate(events)
            if event[0] == "replace" and event[1] is not None and event[1].suffix == ".json"
        )
        fsync_after_image = next(
            index
            for index, event in enumerate(events)
            if index > image_replace and event == ("fsync", daily_directory)
        )
        fsync_after_metadata = next(
            index
            for index, event in enumerate(events)
            if index > metadata_replace and event == ("fsync", daily_directory)
        )
        self.assertLess(image_replace, fsync_after_image)
        self.assertLess(fsync_after_image, metadata_replace)
        self.assertLess(metadata_replace, fsync_after_metadata)
        self.assertLess(fsync_after_metadata, insert_index)

    def test_new_vision_hierarchy_fsyncs_each_parent_directory(self) -> None:
        state_dir = Path(self.temp.name) / "fresh" / "nested" / "state"
        settings = replace(self.settings, state_dir=state_dir)
        fsynced: list[Path] = []
        original_fsync = VisionArchive._fsync_directory

        def fsync_directory(path: Path) -> None:
            original_fsync(path)
            fsynced.append(path.resolve())

        with mock.patch.object(VisionArchive, "_fsync_directory", side_effect=fsync_directory):
            archive = VisionArchive(settings)

        self.assertIn(state_dir.parent.resolve(), fsynced)
        self.assertIn(state_dir.resolve(), fsynced)
        self.assertIn((state_dir / "vision").resolve(), fsynced)
        self.assertTrue(archive.visual_root.is_dir())
        self.assertTrue(archive.deletion_root.is_dir())

    def test_vision_limits_and_token_load_from_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "MANFRED_STATE_DIR": str(self.settings.state_dir),
                "MANFRED_VISION_TOKEN": "environment-vision-token",
                "MANFRED_MAX_VISION_REQUEST_BYTES": "123456",
                "MANFRED_MAX_VISION_IMAGE_BYTES": "65432",
            },
        ):
            loaded = Settings.from_env()
        self.assertEqual(loaded.vision_token, "environment-vision-token")
        self.assertEqual(loaded.max_vision_request_bytes, 123456)
        self.assertEqual(loaded.max_vision_image_bytes, 65432)

    def test_retry_dedupes_and_changed_body_reuse_fails_closed(self) -> None:
        first = self.archive.ingest_noa_jpeg(
            image=JPEG_A,
            frame_audio=AUDIO_WAV,
            client_reported_time="2026-08-23 14:30:00.000",
            idempotency_key="noa-bookmark-1",
        )
        second = self.archive.ingest_noa_jpeg(
            image=JPEG_A,
            frame_audio=AUDIO_WAV,
            client_reported_time="2026-08-23 14:30:00.000",
            idempotency_key="noa-bookmark-1",
        )
        self.assertEqual(first.visual_id, second.visual_id)
        self.assertTrue(second.duplicate)

        with self.assertRaisesRegex(ValueError, "different retained provenance"):
            self.archive.ingest_noa_jpeg(
                image=JPEG_B,
                frame_audio=AUDIO_WAV,
                client_reported_time="2026-08-23 14:30:00.000",
                idempotency_key="noa-bookmark-1",
            )
        with self.assertRaisesRegex(ValueError, "different retained provenance"):
            self.archive.ingest_noa_jpeg(
                image=JPEG_A,
                frame_audio=AUDIO_WAV + b"changed",
                client_reported_time="2026-08-23 14:30:00.000",
                idempotency_key="noa-bookmark-1",
            )
        with self.assertRaisesRegex(ValueError, "different retained provenance"):
            self.archive.ingest_noa_jpeg(
                image=JPEG_A,
                frame_audio=AUDIO_WAV,
                client_reported_time="2026-08-23 14:30:01.000",
                idempotency_key="noa-bookmark-1",
            )

    def test_concurrent_identical_retries_commit_one_exact_image(self) -> None:
        def ingest():  # type: ignore[no-untyped-def]
            return self.archive.ingest_noa_jpeg(
                image=JPEG_A,
                frame_audio=AUDIO_WAV,
                client_reported_time="2026-08-23 14:30:00.000",
                idempotency_key="concurrent-noa-bookmark",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result() for future in (pool.submit(ingest), pool.submit(ingest))]
        self.assertEqual({result.visual_id for result in results}, {results[0].visual_id})
        self.assertEqual(sorted(result.duplicate for result in results), [False, True])
        record = self.archive.get(results[0].visual_id)
        self.assertEqual((self.settings.state_dir / record["path"]).read_bytes(), JPEG_A)
        self.assertEqual(self.archive.status()["visual_events"], 1)

    def test_two_process_archive_instances_serialize_same_idempotency_key(self) -> None:
        other = VisionArchive(self.settings)

        def ingest(archive: VisionArchive):  # type: ignore[no-untyped-def]
            return archive.ingest_noa_jpeg(
                image=JPEG_A,
                frame_audio=AUDIO_WAV,
                client_reported_time="2026-08-23 14:30:00.000",
                idempotency_key="cross-process-noa-bookmark",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                future.result()
                for future in (pool.submit(ingest, self.archive), pool.submit(ingest, other))
            ]
        self.assertEqual(sorted(result.duplicate for result in results), [False, True])
        record = self.archive.get(results[0].visual_id)
        self.assertEqual((self.settings.state_dir / record["path"]).read_bytes(), JPEG_A)
        self.assertEqual(self.archive.status()["visual_events"], 1)

    def test_two_process_changed_body_race_preserves_winning_hash(self) -> None:
        other = VisionArchive(self.settings)

        def ingest(archive: VisionArchive, image: bytes):  # type: ignore[no-untyped-def]
            try:
                return archive.ingest_noa_jpeg(
                    image=image,
                    frame_audio=AUDIO_WAV,
                    client_reported_time="2026-08-23 14:30:00.000",
                    idempotency_key="cross-process-conflict",
                )
            except ValueError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                future.result()
                for future in (
                    pool.submit(ingest, self.archive, JPEG_A),
                    pool.submit(ingest, other, JPEG_B),
                )
            ]
        accepted = [result for result in results if not isinstance(result, ValueError)]
        rejected = [result for result in results if isinstance(result, ValueError)]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)
        self.assertIn("different retained provenance", str(rejected[0]))
        record = self.archive.get(accepted[0].visual_id)
        stored = (self.settings.state_dir / record["path"]).read_bytes()
        self.assertEqual(hashlib.sha256(stored).hexdigest(), record["sha256"])

    def test_committed_sidecar_reindexes_after_database_loss(self) -> None:
        result = self.archive.ingest_noa_jpeg(
            image=JPEG_A,
            frame_audio=AUDIO_WAV,
            client_reported_time="2026-08-23 14:30:00.000",
        )
        with sqlite3.connect(self.settings.state_dir / "archive.sqlite3") as conn:
            conn.execute("DELETE FROM visual_events WHERE visual_id=?", (result.visual_id,))

        recovered = VisionArchive(self.settings)
        self.assertEqual(recovered.get(result.visual_id)["visual_id"], result.visual_id)

    def test_retry_after_index_failure_reuses_original_sidecar_timestamps(self) -> None:
        original_received = datetime(2026, 8, 23, 23, 59, tzinfo=timezone.utc)
        with mock.patch.object(
            self.archive,
            "_insert_metadata",
            side_effect=sqlite3.OperationalError("simulated index failure"),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "simulated index failure"):
                self.archive.ingest_noa_jpeg(
                    image=JPEG_A,
                    frame_audio=AUDIO_WAV,
                    client_reported_time="2026-08-23 17:59:00.000",
                    idempotency_key="index-failure-retry",
                    received_at=original_received,
                )

        retry = self.archive.ingest_noa_jpeg(
            image=JPEG_A,
            frame_audio=AUDIO_WAV,
            client_reported_time="2026-08-23 17:59:00.000",
            idempotency_key="index-failure-retry",
            received_at=datetime(2026, 8, 24, 0, 1, tzinfo=timezone.utc),
        )
        self.assertTrue(retry.duplicate)
        self.assertEqual(retry.received_at, original_received.isoformat())
        record = self.archive.get(retry.visual_id)
        self.assertEqual(record["received_at"], original_received.isoformat())
        self.assertEqual(record["capture_estimate"], original_received.isoformat())
        self.assertEqual(len(list(self.archive.visual_root.glob(f"*/{retry.visual_id}.json"))), 1)

    def test_duplicate_retry_fails_closed_when_indexed_evidence_is_missing(self) -> None:
        result = self.archive.ingest_noa_jpeg(
            image=JPEG_A,
            frame_audio=AUDIO_WAV,
            client_reported_time="2026-08-23 14:30:00.000",
            idempotency_key="missing-indexed-evidence",
        )
        record = self.archive.get(result.visual_id)
        (self.settings.state_dir / record["path"]).unlink()

        with self.assertRaisesRegex(ValueError, "indexed visual evidence"):
            self.archive.ingest_noa_jpeg(
                image=JPEG_A,
                frame_audio=AUDIO_WAV,
                client_reported_time="2026-08-23 14:30:00.000",
                idempotency_key="missing-indexed-evidence",
            )

    def test_interrupted_temp_pair_is_finalized_and_reindexed(self) -> None:
        result = self.archive.ingest_noa_jpeg(
            image=JPEG_A,
            frame_audio=AUDIO_WAV,
            client_reported_time="2026-08-23 14:30:00.000",
        )
        record = self.archive.get(result.visual_id)
        image_path = self.settings.state_dir / record["path"]
        metadata_path = self.settings.state_dir / record["metadata_path"]
        with sqlite3.connect(self.settings.state_dir / "archive.sqlite3") as conn:
            conn.execute("DELETE FROM visual_events WHERE visual_id=?", (result.visual_id,))
        image_path.rename(image_path.with_suffix(".jpg.tmp"))
        metadata_path.rename(metadata_path.with_suffix(".json.tmp"))

        events: list[tuple[str, Path]] = []
        original_fsync = VisionArchive._fsync_directory
        original_replace = os.replace

        def fsync_directory(path: Path) -> None:
            original_fsync(path)
            events.append(("fsync", path.resolve()))

        def replace_path(source: Path, destination: Path) -> None:
            original_replace(source, destination)
            events.append(("replace", Path(destination).resolve()))

        with mock.patch.object(VisionArchive, "_fsync_directory", side_effect=fsync_directory):
            with mock.patch("manfred_ears.vision.os.replace", side_effect=replace_path):
                recovered = VisionArchive(self.settings)
        recovered_record = recovered.get(result.visual_id)
        self.assertEqual((self.settings.state_dir / recovered_record["path"]).read_bytes(), JPEG_A)
        self.assertFalse(image_path.with_suffix(".jpg.tmp").exists())
        self.assertFalse(metadata_path.with_suffix(".json.tmp").exists())

        image_replace = events.index(("replace", image_path.resolve()))
        metadata_replace = events.index(("replace", metadata_path.resolve()))
        fsync_between = next(
            index
            for index, event in enumerate(events)
            if image_replace < index < metadata_replace
            and event == ("fsync", image_path.parent.resolve())
        )
        self.assertLess(image_replace, fsync_between)
        self.assertLess(fsync_between, metadata_replace)

    def test_interrupted_delete_tombstone_finishes_before_sidecar_reindex(self) -> None:
        result = self.archive.ingest_noa_jpeg(
            image=JPEG_A,
            frame_audio=AUDIO_WAV,
            client_reported_time="2026-08-23 14:30:00.000",
            idempotency_key="delete-crash-bookmark",
        )
        record = self.archive.get(result.visual_id)
        image_path = self.settings.state_dir / record["path"]
        metadata_path = self.settings.state_dir / record["metadata_path"]

        def crash_after_first_unlink(tombstone_path: Path):  # type: ignore[no-untyped-def]
            tombstone = json.loads(tombstone_path.read_text(encoding="utf-8"))
            _, retained_metadata = self.archive._validate_deletion_tombstone(
                tombstone,
                tombstone_path,
            )
            retained_metadata.unlink()
            self.archive._fsync_directory(retained_metadata.parent)
            raise OSError("simulated deletion crash")

        with mock.patch.object(
            self.archive,
            "_finalize_deletion_tombstone",
            side_effect=crash_after_first_unlink,
        ):
            with self.assertRaisesRegex(OSError, "simulated deletion crash"):
                self.archive.delete(result.visual_id)

        tombstone_path = self.archive.deletion_root / f"{result.visual_id}.json"
        self.assertTrue(tombstone_path.exists())
        self.assertTrue(image_path.exists())
        self.assertFalse(metadata_path.exists())
        self.assertEqual(self.archive.get(result.visual_id)["visual_id"], result.visual_id)

        recovered = VisionArchive(self.settings)
        with self.assertRaises(KeyError):
            recovered.get(result.visual_id)
        self.assertFalse(image_path.exists())
        self.assertFalse(metadata_path.exists())
        self.assertFalse(tombstone_path.exists())

        replacement = recovered.ingest_noa_jpeg(
            image=JPEG_A,
            frame_audio=AUDIO_WAV,
            client_reported_time="2026-08-23 14:30:00.000",
            idempotency_key="delete-crash-bookmark",
        )
        self.assertFalse(replacement.duplicate)
        self.assertEqual(recovered.status()["visual_events"], 1)

    def test_audio_and_vision_process_initializers_share_archive_safely(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            audio_future = pool.submit(AudioArchive, self.settings)
            vision_future = pool.submit(VisionArchive, self.settings)
            self.assertIsInstance(audio_future.result(), AudioArchive)
            self.assertIsInstance(vision_future.result(), VisionArchive)

    def test_reconciliation_never_follows_sidecar_paths_outside_state_root(self) -> None:
        outside = Path(self.temp.name) / "outside.jpg"
        outside.write_bytes(JPEG_A)
        sidecar_dir = self.settings.state_dir / "vision" / "raw" / "2026-08-23"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        (sidecar_dir / "frame-malicious.json").write_text(
            json.dumps(
                {
                    "visual_id": "frame-" + ("a" * 32),
                    "idempotency_key": "malicious",
                    "received_at": "2026-08-23T20:30:00+00:00",
                    "received_epoch": 0.0,
                    "capture_estimate": "2026-08-23T20:30:00+00:00",
                    "capture_time_basis": "noa-request-receive-time",
                    "client_reported_time": None,
                    "source": "brilliant-frame-via-noa",
                    "trigger": "noa-tap-query",
                    "sha256": VisionArchive._digest(JPEG_A),
                    "num_bytes": len(JPEG_A),
                    "path": "../outside.jpg",
                    "metadata_path": "../outside.json",
                    "frame_audio_sha256": None,
                    "frame_audio_num_bytes": 0,
                }
            )
        )

        recovered = VisionArchive(self.settings)
        self.assertEqual(recovered.status()["visual_events"], 0)
        self.assertEqual(outside.read_bytes(), JPEG_A)


class VisionReceiverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            state_dir=Path(self.temp.name) / "state",
            auth_token="receiver-secret",
            operator_token="operator-secret",
            vision_token="vision-secret",
            asr_backend="stub",
            max_vision_request_bytes=4096,
            max_vision_image_bytes=1024,
        )
        self.archive = VisionArchive(self.settings)
        self.client = TestClient(create_vision_receiver_app(self.settings, archive=self.archive))
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

    def _request(self, *, token: str = "vision-secret", image: bytes = JPEG_A, headers=None):  # type: ignore[no-untyped-def]
        request_headers = {"X-Manfred-Vision-Token": token, **(headers or {})}
        return self.client.post(
            "/noa",
            headers=request_headers,
            files={
                "image": ("image.jpg", image, "image/jpeg"),
                "audio": ("audio.wav", AUDIO_WAV, "audio/wav"),
            },
            data={
                "messages": '[{"message":"private history must not be archived"}]',
                "location": "private location must not be archived",
                "time": "2026-08-23 14:30:00.000",
            },
        )

    def test_missing_vision_token_refuses_app_creation(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "MANFRED_VISION_TOKEN"):
            create_vision_receiver_app(replace(self.settings, vision_token=None))

    def test_surface_exposes_only_noa_compatibility_post(self) -> None:
        for path in ("/health", "/audio", "/v1/status", "/docs", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self._request().status_code, 200)

    def test_auth_and_payload_validation(self) -> None:
        self.assertEqual(self._request(token="wrong").status_code, 401)
        query_token = self.client.post(
            "/noa?token=vision-secret",
            files={"image": ("image.jpg", JPEG_A, "image/jpeg")},
        )
        self.assertEqual(query_token.status_code, 401)
        self.assertEqual(
            self.client.post(
                "/noa",
                content=b"not multipart",
                headers={
                    "X-Manfred-Vision-Token": "vision-secret",
                    "Content-Type": "application/octet-stream",
                },
            ).status_code,
            415,
        )
        self.assertEqual(self._request(image=b"not-jpeg").status_code, 422)
        self.assertEqual(
            self._request(image=b"\xff\xd8" + (b"x" * 2000) + b"\xff\xd9").status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/noa",
                content=b"x" * 5000,
                headers={
                    "X-Manfred-Vision-Token": "vision-secret",
                    "Content-Type": "multipart/form-data; boundary=x",
                },
            ).status_code,
            413,
        )
        truncated = (
            b"--cut\r\n"
            b'Content-Disposition: form-data; name="image"; filename="image.jpg"\r\n'
            b"Content-Type: image/jpeg\r\n\r\n"
            + JPEG_A
            + b"\r\n"
        )
        self.assertEqual(
            self.client.post(
                "/noa",
                content=truncated,
                headers={
                    "X-Manfred-Vision-Token": "vision-secret",
                    "Content-Type": "multipart/form-data; boundary=cut",
                },
            ).status_code,
            422,
        )
        inline_suffix = (
            b"--cut\r\n"
            b'Content-Disposition: form-data; name="image"; filename="image.jpg"\r\n'
            b"Content-Type: image/jpeg\r\n\r\n"
            + JPEG_A
            + b"\r\n--cut\r\n"
            b'Content-Disposition: form-data; name="messages"\r\n\r\n'
            b"truncated-payload--cut--"
        )
        self.assertEqual(
            self.client.post(
                "/noa",
                content=inline_suffix,
                headers={
                    "X-Manfred-Vision-Token": "vision-secret",
                    "Content-Type": "multipart/form-data; boundary=cut",
                },
            ).status_code,
            422,
        )
        self.assertEqual(self.archive.status()["visual_events"], 0)

    def test_noa_response_contract_and_archive_receipt(self) -> None:
        response = self._request(headers={"Idempotency-Key": "noa-request-1"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["user_prompt"], "Visual bookmark")
        self.assertIn("Saved this Frame image", body["message"])
        self.assertIsNone(body["image"])
        self.assertIsNone(body["audio"])
        self.assertFalse(body["debug"]["topic_changed"])
        self.assertFalse(body["debug"]["duplicate"])
        self.assertRegex(body["debug"]["visual_id"], r"^frame-[0-9a-f]{32}$")
        self.assertRegex(body["debug"]["episode_id"], r"^episode-[0-9a-f]{32}$")
        self.assertEqual(self.archive.status()["visual_events"], 1)

        duplicate = self._request(headers={"Idempotency-Key": "noa-request-1"})
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["debug"]["duplicate"])
        self.assertEqual(self.archive.status()["visual_events"], 1)

        operator_headers = {"Authorization": "Bearer operator-secret"}
        status_response = self.operator.get("/v1/status", headers=operator_headers)
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["vision"]["visual_events"], 1)
        self.assertEqual(status_response.json()["multimodal"]["episodes"], 1)
        self.assertEqual(status_response.json()["multimodal"]["pending_episodes"], 1)
        episode_id = body["debug"]["episode_id"]
        episode_response = self.operator.get(
            f"/v1/episodes/{episode_id}", headers=operator_headers
        )
        self.assertEqual(episode_response.status_code, 200)
        self.assertEqual(episode_response.json()["anchor_visual_id"], body["debug"]["visual_id"])
        episode_list = self.operator.get("/v1/episodes", headers=operator_headers)
        self.assertEqual(episode_list.status_code, 200)
        self.assertEqual(episode_list.json()["count"], 1)
        self.assertEqual(self.operator.get("/v1/episodes").status_code, 401)

        visual_id = body["debug"]["visual_id"]
        deleted = self.operator.delete(f"/v1/vision/{visual_id}", headers=operator_headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["deleted"])
        self.assertEqual(self.archive.status()["visual_events"], 0)
        cleared_status = self.operator.get("/v1/status", headers=operator_headers).json()
        self.assertEqual(cleared_status["multimodal"]["episodes"], 0)
        self.assertEqual(
            self.operator.delete(f"/v1/vision/{visual_id}", headers=operator_headers).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
