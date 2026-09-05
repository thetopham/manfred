from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import Settings


@dataclass(frozen=True)
class EpisodeRefreshResult:
    episode_id: str
    version: int
    changed: bool
    state: str
    audio_chunk_count: int
    transcript_segment_count: int


class EpisodeArchive:
    """Versioned image-centered joins over immutable visual and audio evidence.

    Episodes store source identifiers and bounded transcript-segment references;
    they never copy JPEG or PCM payloads. The initial clock model is deliberately
    conservative: Noa visual events use request receive time, while direct Omi
    chunks use the S25 PCM sample clock.
    """

    schema_version = 1

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.state_dir
        self.db_path = self.root / "archive.sqlite3"
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=20000")
        conn.execute("PRAGMA foreign_keys=ON")
        for attempt in range(20):
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 19:
                    conn.close()
                    raise
                time.sleep(0.05 * (attempt + 1))
        return conn

    def _init_db(self) -> None:
        for attempt in range(20):
            try:
                with self._connect() as conn:
                    conn.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS multimodal_episodes (
                            episode_id TEXT PRIMARY KEY,
                            anchor_visual_id TEXT NOT NULL UNIQUE,
                            schema_version INTEGER NOT NULL,
                            start_at TEXT NOT NULL,
                            end_at TEXT NOT NULL,
                            start_epoch REAL NOT NULL,
                            end_epoch REAL NOT NULL,
                            clock_basis TEXT NOT NULL,
                            timing_quality TEXT NOT NULL,
                            capture_uncertainty_seconds REAL,
                            state TEXT NOT NULL CHECK(state IN ('pending','ready')),
                            dirty INTEGER NOT NULL DEFAULT 1 CHECK(dirty IN (0,1)),
                            version INTEGER NOT NULL DEFAULT 1,
                            image_ids_json TEXT NOT NULL,
                            audio_chunk_ids_json TEXT NOT NULL,
                            source_session_ids_json TEXT NOT NULL,
                            transcript_segment_refs_json TEXT NOT NULL,
                            event_ids_json TEXT NOT NULL,
                            reference_sha256 TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );
                        CREATE INDEX IF NOT EXISTS multimodal_episodes_due
                            ON multimodal_episodes(state,dirty,end_epoch,episode_id);
                        CREATE INDEX IF NOT EXISTS multimodal_episodes_time
                            ON multimodal_episodes(start_epoch,end_epoch,episode_id);
                        """
                    )
                    self._install_source_triggers(conn)
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 19:
                    raise
                time.sleep(0.05 * (attempt + 1))
        os.chmod(self.db_path, 0o600)
        self.reconcile_visuals()

    def _install_source_triggers(self, conn: sqlite3.Connection) -> None:
        installed = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        visual_triggers = {"multimodal_visual_insert_v1", "multimodal_visual_delete_v1"}
        if self._table_exists(conn, "visual_events") and not visual_triggers.issubset(installed):
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS multimodal_visual_insert_v1
                AFTER INSERT ON visual_events BEGIN
                    UPDATE multimodal_episodes SET dirty=1
                    WHERE start_epoch<=new.received_epoch AND end_epoch>=new.received_epoch;
                END;
                CREATE TRIGGER IF NOT EXISTS multimodal_visual_delete_v1
                AFTER DELETE ON visual_events BEGIN
                    UPDATE multimodal_episodes SET dirty=1
                    WHERE start_epoch<=old.received_epoch AND end_epoch>=old.received_epoch;
                    DELETE FROM multimodal_episodes WHERE anchor_visual_id=old.visual_id;
                END;
                """
            )
        chunk_columns: set[str] = set()
        chunk_triggers = {"multimodal_chunk_insert_v1", "multimodal_chunk_delete_v1"}
        if self._table_exists(conn, "chunks"):
            chunk_columns = {row["name"] for row in conn.execute("PRAGMA table_info(chunks)")}
            if {
                "capture_started_epoch",
                "capture_ended_epoch",
                "received_epoch",
                "duration_seconds",
            }.issubset(chunk_columns) and not chunk_triggers.issubset(installed):
                conn.executescript(
                    """
                    CREATE TRIGGER IF NOT EXISTS multimodal_chunk_insert_v1
                    AFTER INSERT ON chunks BEGIN
                        UPDATE multimodal_episodes SET dirty=1
                        WHERE (
                            new.capture_started_epoch IS NOT NULL
                            AND new.capture_ended_epoch IS NOT NULL
                            AND start_epoch<new.capture_ended_epoch
                            AND end_epoch>new.capture_started_epoch
                        ) OR (
                            new.capture_started_epoch IS NULL
                            AND start_epoch<new.received_epoch
                            AND end_epoch>(new.received_epoch-new.duration_seconds)
                        );
                    END;
                    CREATE TRIGGER IF NOT EXISTS multimodal_chunk_delete_v1
                    AFTER DELETE ON chunks BEGIN
                        UPDATE multimodal_episodes SET dirty=1
                        WHERE (
                            old.capture_started_epoch IS NOT NULL
                            AND old.capture_ended_epoch IS NOT NULL
                            AND start_epoch<old.capture_ended_epoch
                            AND end_epoch>old.capture_started_epoch
                        ) OR (
                            old.capture_started_epoch IS NULL
                            AND start_epoch<old.received_epoch
                            AND end_epoch>(old.received_epoch-old.duration_seconds)
                        );
                    END;
                    """
                )
        transcript_triggers = {
            "multimodal_transcript_insert_v1",
            "multimodal_transcript_delete_v1",
        }
        if (
            self._table_exists(conn, "transcripts")
            and {"capture_started_epoch", "capture_ended_epoch"}.issubset(chunk_columns)
            and not transcript_triggers.issubset(installed)
        ):
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS multimodal_transcript_insert_v1
                AFTER INSERT ON transcripts BEGIN
                    UPDATE multimodal_episodes SET dirty=1
                    WHERE EXISTS (
                        SELECT 1 FROM chunks c
                        WHERE c.session_id=new.session_id AND (
                            (c.capture_started_epoch IS NOT NULL
                             AND c.capture_ended_epoch IS NOT NULL
                             AND c.capture_started_epoch<multimodal_episodes.end_epoch
                             AND c.capture_ended_epoch>multimodal_episodes.start_epoch)
                            OR
                            (c.capture_started_epoch IS NULL
                             AND c.received_epoch>multimodal_episodes.start_epoch
                             AND (c.received_epoch-c.duration_seconds)<multimodal_episodes.end_epoch)
                        )
                    );
                END;
                CREATE TRIGGER IF NOT EXISTS multimodal_transcript_delete_v1
                AFTER DELETE ON transcripts BEGIN
                    UPDATE multimodal_episodes SET dirty=1
                    WHERE EXISTS (
                        SELECT 1 FROM chunks c
                        WHERE c.session_id=old.session_id AND (
                            (c.capture_started_epoch IS NOT NULL
                             AND c.capture_ended_epoch IS NOT NULL
                             AND c.capture_started_epoch<multimodal_episodes.end_epoch
                             AND c.capture_ended_epoch>multimodal_episodes.start_epoch)
                            OR
                            (c.capture_started_epoch IS NULL
                             AND c.received_epoch>multimodal_episodes.start_epoch
                             AND (c.received_epoch-c.duration_seconds)<multimodal_episodes.end_epoch)
                        )
                    );
                END;
                """
            )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _parse_time(value: str) -> datetime:
        return EpisodeArchive._utc(datetime.fromisoformat(value.replace("Z", "+00:00")))

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _hash(value: Any) -> str:
        return hashlib.sha256(EpisodeArchive._json(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _episode_id(visual_id: str) -> str:
        digest = hashlib.sha256(f"image-centered-v1\0{visual_id}".encode("utf-8")).hexdigest()
        return f"episode-{digest[:32]}"

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            is not None
        )

    def reconcile_visuals(self, limit: int = 1000) -> dict[str, int]:
        bounded_limit = max(1, min(limit, 10_000))
        with self._connect() as conn:
            self._install_source_triggers(conn)
            if not self._table_exists(conn, "visual_events"):
                return {"created": 0, "deleted": 0}
            missing = [
                str(row["visual_id"])
                for row in conn.execute(
                    """SELECT v.visual_id FROM visual_events v
                       LEFT JOIN multimodal_episodes e ON e.anchor_visual_id=v.visual_id
                       WHERE e.episode_id IS NULL
                       ORDER BY v.received_epoch,v.visual_id LIMIT ?""",
                    (bounded_limit,),
                )
            ]
            orphaned = [
                str(row["anchor_visual_id"])
                for row in conn.execute(
                    """SELECT e.anchor_visual_id FROM multimodal_episodes e
                       LEFT JOIN visual_events v ON v.visual_id=e.anchor_visual_id
                       WHERE v.visual_id IS NULL
                       ORDER BY e.end_epoch,e.episode_id LIMIT ?""",
                    (bounded_limit,),
                )
            ]
            if orphaned:
                conn.executemany(
                    "DELETE FROM multimodal_episodes WHERE anchor_visual_id=?",
                    ((visual_id,) for visual_id in orphaned),
                )
        created = 0
        for visual_id in missing:
            try:
                self.ensure_visual(visual_id)
                created += 1
            except KeyError:
                continue
        return {"created": created, "deleted": len(orphaned)}

    def ensure_visual(self, visual_id: str) -> str:
        with self._connect() as conn:
            if not self._table_exists(conn, "visual_events"):
                raise KeyError(visual_id)
            visual = conn.execute(
                """SELECT visual_id,capture_estimate,capture_time_basis,received_epoch,trigger
                   FROM visual_events WHERE visual_id=?""",
                (visual_id,),
            ).fetchone()
            if visual is None:
                raise KeyError(visual_id)
            capture = self._parse_time(str(visual["capture_estimate"]))
            context = timedelta(seconds=self.settings.episode_context_seconds)
            started = capture - context
            ended = capture + context
            episode_id = self._episode_id(visual_id)
            now = datetime.now(timezone.utc).isoformat()
            initial_references = {
                "image_ids": [visual_id],
                "audio_chunk_ids": [],
                "source_session_ids": [],
                "transcript_segment_refs": [],
                "event_ids": [f"{visual['trigger']}:{visual_id}"],
            }
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT OR IGNORE INTO multimodal_episodes(
                       episode_id,anchor_visual_id,schema_version,start_at,end_at,start_epoch,end_epoch,
                       clock_basis,timing_quality,capture_uncertainty_seconds,state,dirty,version,
                       image_ids_json,audio_chunk_ids_json,source_session_ids_json,
                       transcript_segment_refs_json,event_ids_json,reference_sha256,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?, 'pending',1,1,?,?,?,?,?,?,?,?)""",
                (
                    episode_id,
                    visual_id,
                    self.schema_version,
                    started.isoformat(),
                    ended.isoformat(),
                    started.timestamp(),
                    ended.timestamp(),
                    str(visual["capture_time_basis"]),
                    "low" if visual["capture_time_basis"] == "noa-request-receive-time" else "bounded",
                    None,
                    self._json(initial_references["image_ids"]),
                    self._json(initial_references["audio_chunk_ids"]),
                    self._json(initial_references["source_session_ids"]),
                    self._json(initial_references["transcript_segment_refs"]),
                    self._json(initial_references["event_ids"]),
                    self._hash(initial_references),
                    now,
                    now,
                ),
            )
            conn.execute(
                """UPDATE multimodal_episodes SET dirty=1
                   WHERE start_epoch<? AND end_epoch>?""",
                (ended.timestamp(), started.timestamp()),
            )
            conn.commit()
        return episode_id

    def mark_visual_changed(self, visual_id: str) -> int:
        with self._connect() as conn:
            if not self._table_exists(conn, "visual_events"):
                return 0
            row = conn.execute(
                "SELECT received_epoch FROM visual_events WHERE visual_id=?",
                (visual_id,),
            ).fetchone()
            if row is None:
                return 0
            epoch = float(row["received_epoch"])
            cursor = conn.execute(
                """UPDATE multimodal_episodes SET dirty=1
                   WHERE start_epoch<=? AND end_epoch>=?""",
                (epoch, epoch),
            )
            return int(cursor.rowcount)

    def delete_for_visual(self, visual_id: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM multimodal_episodes WHERE anchor_visual_id=?",
                (visual_id,),
            )
            return int(cursor.rowcount)

    def mark_chunk_changed(self, chunk_id: str) -> int:
        with self._connect() as conn:
            if not self._table_exists(conn, "chunks"):
                return 0
            row = conn.execute(
                """SELECT capture_started_epoch,capture_ended_epoch,received_epoch,duration_seconds
                   FROM chunks WHERE chunk_id=?""",
                (chunk_id,),
            ).fetchone()
            if row is None:
                return 0
            if row["capture_started_epoch"] is not None and row["capture_ended_epoch"] is not None:
                started = float(row["capture_started_epoch"])
                ended = float(row["capture_ended_epoch"])
            else:
                ended = float(row["received_epoch"])
                started = ended - float(row["duration_seconds"])
            cursor = conn.execute(
                """UPDATE multimodal_episodes SET dirty=1
                   WHERE start_epoch<? AND end_epoch>?""",
                (ended, started),
            )
            return int(cursor.rowcount)

    def mark_session_changed(self, session_id: str) -> int:
        with self._connect() as conn:
            if not self._table_exists(conn, "chunks"):
                return 0
            cursor = conn.execute(
                """UPDATE multimodal_episodes SET dirty=1
                   WHERE EXISTS (
                       SELECT 1 FROM chunks c
                       WHERE c.session_id=? AND (
                           (c.capture_started_epoch IS NOT NULL AND c.capture_ended_epoch IS NOT NULL
                            AND c.capture_started_epoch<multimodal_episodes.end_epoch
                            AND c.capture_ended_epoch>multimodal_episodes.start_epoch)
                           OR
                           (c.capture_started_epoch IS NULL
                            AND c.received_epoch>multimodal_episodes.start_epoch
                            AND (c.received_epoch-c.duration_seconds)<multimodal_episodes.end_epoch)
                       )
                   )""",
                (session_id,),
            )
            return int(cursor.rowcount)

    def _overlapping_images(
        self, conn: sqlite3.Connection, start_epoch: float, end_epoch: float
    ) -> tuple[list[str], list[str]]:
        if not self._table_exists(conn, "visual_events"):
            return [], []
        rows = conn.execute(
            """SELECT visual_id,trigger FROM visual_events
               WHERE received_epoch>=? AND received_epoch<=?
               ORDER BY received_epoch,visual_id""",
            (start_epoch, end_epoch),
        ).fetchall()
        image_ids = [str(row["visual_id"]) for row in rows]
        event_ids = [f"{row['trigger']}:{row['visual_id']}" for row in rows]
        return image_ids, event_ids

    def _overlapping_chunks(
        self, conn: sqlite3.Connection, start_epoch: float, end_epoch: float
    ) -> list[sqlite3.Row]:
        if not self._table_exists(conn, "chunks"):
            return []
        return conn.execute(
            """SELECT chunk_id,session_id,capture_started_epoch,capture_ended_epoch,
                      received_epoch,duration_seconds
               FROM chunks
               WHERE (
                   capture_started_epoch IS NOT NULL AND capture_ended_epoch IS NOT NULL
                   AND capture_started_epoch<? AND capture_ended_epoch>?
               ) OR (
                   capture_started_epoch IS NULL
                   AND received_epoch>? AND (received_epoch-duration_seconds)<?
               )
               ORDER BY COALESCE(capture_started_epoch,received_epoch-duration_seconds),chunk_id""",
            (end_epoch, start_epoch, start_epoch, end_epoch),
        ).fetchall()

    def _latest_transcripts(
        self, conn: sqlite3.Connection, session_ids: Iterable[str]
    ) -> list[sqlite3.Row]:
        bounded = sorted(set(session_ids))
        if not bounded or not self._table_exists(conn, "transcripts"):
            return []
        placeholders = ",".join("?" for _ in bounded)
        return conn.execute(
            f"""SELECT t.transcript_id,t.session_id,t.version,t.model,t.event_json
                FROM transcripts t
                JOIN (
                    SELECT session_id,MAX(version) AS version
                    FROM transcripts WHERE session_id IN ({placeholders})
                    GROUP BY session_id
                ) latest ON latest.session_id=t.session_id AND latest.version=t.version
                ORDER BY t.session_id,t.version""",
            bounded,
        ).fetchall()

    @staticmethod
    def _offset_to_observed_time(
        event: dict[str, Any],
        offset_seconds: float,
        fallback_start: datetime,
        *,
        prefer_previous_boundary: bool,
    ) -> datetime:
        remaining = max(0.0, offset_seconds)
        captures = event.get("evidence", {}).get("chunk_capture", [])
        parsed: list[tuple[datetime, datetime]] = []
        for capture in captures:
            started_value = capture.get("capture_started_at")
            ended_value = capture.get("capture_ended_at")
            if not started_value or not ended_value:
                parsed = []
                break
            try:
                started = EpisodeArchive._parse_time(str(started_value))
                ended = EpisodeArchive._parse_time(str(ended_value))
            except ValueError:
                parsed = []
                break
            if ended <= started:
                parsed = []
                break
            parsed.append((started, ended))
        for started, ended in parsed:
            duration = (ended - started).total_seconds()
            if remaining < duration or (prefer_previous_boundary and remaining <= duration):
                return started + timedelta(seconds=min(remaining, duration))
            remaining -= duration
        if parsed:
            return parsed[-1][1]
        return fallback_start + timedelta(seconds=remaining)

    def _transcript_refs(
        self,
        conn: sqlite3.Connection,
        session_ids: Iterable[str],
        start_epoch: float,
        end_epoch: float,
    ) -> list[dict[str, Any]]:
        references: list[dict[str, Any]] = []
        for row in self._latest_transcripts(conn, session_ids):
            event = json.loads(row["event_json"])
            transcript = event.get("transcript", {})
            segments = transcript.get("segments", [])
            observed_start = self._parse_time(event["timestamps"]["observed_start_estimate"])
            if not segments and str(transcript.get("text", "")).strip():
                observed_end = self._parse_time(event["timestamps"]["observed_end_estimate"])
                segments = [
                    {
                        "start": 0.0,
                        "end": max(0.0, (observed_end - observed_start).total_seconds()),
                        "text": str(transcript["text"]).strip(),
                    }
                ]
            reconciliation_strategy = event.get("reconciliation", {}).get("strategy")
            offsets_are_concatenated_pcm = reconciliation_strategy == "full_session_asr"
            for index, segment in enumerate(segments):
                start_offset = max(0.0, float(segment.get("start", 0.0)))
                end_offset = max(start_offset, float(segment.get("end", 0.0)))
                if offsets_are_concatenated_pcm:
                    segment_start = self._offset_to_observed_time(
                        event,
                        start_offset,
                        observed_start,
                        prefer_previous_boundary=False,
                    )
                    segment_end = self._offset_to_observed_time(
                        event,
                        end_offset,
                        observed_start,
                        prefer_previous_boundary=True,
                    )
                else:
                    segment_start = observed_start + timedelta(seconds=start_offset)
                    segment_end = observed_start + timedelta(seconds=end_offset)
                point_inside = (
                    segment_start == segment_end
                    and start_epoch <= segment_start.timestamp() <= end_epoch
                )
                overlaps = segment_start.timestamp() < end_epoch and segment_end.timestamp() > start_epoch
                if not point_inside and not overlaps:
                    continue
                references.append(
                    {
                        "segment_id": f"{row['transcript_id']}:segment:{index}",
                        "transcript_id": str(row["transcript_id"]),
                        "session_id": str(row["session_id"]),
                        "transcript_version": int(row["version"]),
                        "model": str(row["model"]),
                        "observed_start": segment_start.isoformat(),
                        "observed_end": segment_end.isoformat(),
                        "timestamp_basis": event["timestamps"].get("timestamp_basis"),
                        "capture_time_quality": event["timestamps"].get("capture_time_quality"),
                        "text": str(segment.get("text", "")).strip(),
                    }
                )
        return sorted(references, key=lambda value: (value["observed_start"], value["segment_id"]))

    def refresh_episode(
        self,
        episode_id: str,
        *,
        now_epoch: float | None = None,
        force: bool = False,
    ) -> EpisodeRefreshResult:
        effective_now = time.time() if now_epoch is None else now_epoch
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM multimodal_episodes WHERE episode_id=?",
                (episode_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(episode_id)
            due_epoch = float(row["end_epoch"]) + self.settings.episode_finalize_grace_seconds
            if not force and row["state"] == "pending" and effective_now < due_epoch:
                conn.rollback()
                return EpisodeRefreshResult(
                    episode_id=episode_id,
                    version=int(row["version"]),
                    changed=False,
                    state="pending",
                    audio_chunk_count=len(json.loads(row["audio_chunk_ids_json"])),
                    transcript_segment_count=len(json.loads(row["transcript_segment_refs_json"])),
                )

            image_ids, event_ids = self._overlapping_images(
                conn, float(row["start_epoch"]), float(row["end_epoch"])
            )
            chunks = self._overlapping_chunks(
                conn, float(row["start_epoch"]), float(row["end_epoch"])
            )
            audio_chunk_ids = [str(chunk["chunk_id"]) for chunk in chunks]
            source_session_ids = sorted({str(chunk["session_id"]) for chunk in chunks})
            transcript_refs = self._transcript_refs(
                conn,
                source_session_ids,
                float(row["start_epoch"]),
                float(row["end_epoch"]),
            )
            references = {
                "image_ids": image_ids,
                "audio_chunk_ids": audio_chunk_ids,
                "source_session_ids": source_session_ids,
                "transcript_segment_refs": transcript_refs,
                "event_ids": event_ids,
            }
            reference_sha256 = self._hash(references)
            changed = reference_sha256 != row["reference_sha256"]
            version = int(row["version"]) + (1 if changed else 0)
            conn.execute(
                """UPDATE multimodal_episodes
                   SET state='ready',dirty=0,version=?,image_ids_json=?,audio_chunk_ids_json=?,
                       source_session_ids_json=?,transcript_segment_refs_json=?,event_ids_json=?,
                       reference_sha256=?,updated_at=?
                   WHERE episode_id=?""",
                (
                    version,
                    self._json(image_ids),
                    self._json(audio_chunk_ids),
                    self._json(source_session_ids),
                    self._json(transcript_refs),
                    self._json(event_ids),
                    reference_sha256,
                    datetime.now(timezone.utc).isoformat(),
                    episode_id,
                ),
            )
            conn.commit()
        return EpisodeRefreshResult(
            episode_id=episode_id,
            version=version,
            changed=changed,
            state="ready",
            audio_chunk_count=len(audio_chunk_ids),
            transcript_segment_count=len(transcript_refs),
        )

    def refresh_due(self, *, now_epoch: float | None = None, limit: int = 100) -> list[EpisodeRefreshResult]:
        effective_now = time.time() if now_epoch is None else now_epoch
        bounded_limit = max(1, min(limit, 1000))
        self.reconcile_visuals(limit=bounded_limit)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT episode_id FROM multimodal_episodes
                   WHERE (state='pending' AND end_epoch+?<=?)
                      OR (state='ready' AND dirty=1)
                   ORDER BY end_epoch,episode_id LIMIT ?""",
                (self.settings.episode_finalize_grace_seconds, effective_now, bounded_limit),
            ).fetchall()
        results: list[EpisodeRefreshResult] = []
        for row in rows:
            results.append(
                self.refresh_episode(
                    str(row["episode_id"]),
                    now_epoch=effective_now,
                )
            )
        return results

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        transcript_refs = json.loads(row["transcript_segment_refs_json"])
        return {
            "episode_id": row["episode_id"],
            "anchor_visual_id": row["anchor_visual_id"],
            "schema_version": int(row["schema_version"]),
            "start": row["start_at"],
            "end": row["end_at"],
            "clock_basis": row["clock_basis"],
            "timing_quality": row["timing_quality"],
            "capture_uncertainty_seconds": row["capture_uncertainty_seconds"],
            "state": row["state"],
            "dirty": bool(row["dirty"]),
            "version": int(row["version"]),
            "image_ids": json.loads(row["image_ids_json"]),
            "audio_chunk_ids": json.loads(row["audio_chunk_ids_json"]),
            "source_session_ids": json.loads(row["source_session_ids_json"]),
            "transcript_segment_ids": [value["segment_id"] for value in transcript_refs],
            "transcript_segments": transcript_refs,
            "event_ids": json.loads(row["event_ids_json"]),
            "reference_sha256": row["reference_sha256"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get(self, episode_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM multimodal_episodes WHERE episode_id=?",
                (episode_id,),
            ).fetchone()
        if row is None:
            raise KeyError(episode_id)
        return self._record(row)

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 100))
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM multimodal_episodes
                   ORDER BY start_epoch DESC,episode_id DESC LIMIT ?""",
                (bounded_limit,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def status(self) -> dict[str, int]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END) AS pending,
                          SUM(CASE WHEN state='ready' THEN 1 ELSE 0 END) AS ready,
                          SUM(CASE WHEN dirty=1 THEN 1 ELSE 0 END) AS dirty
                   FROM multimodal_episodes"""
            ).fetchone()
        return {
            "episodes": int(row["total"] or 0),
            "pending_episodes": int(row["pending"] or 0),
            "ready_episodes": int(row["ready"] or 0),
            "dirty_episodes": int(row["dirty"] or 0),
        }
