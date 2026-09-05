from __future__ import annotations

import asyncio
import contextlib
import hmac
import time
from contextlib import asynccontextmanager
from datetime import datetime
from email import policy
from email.parser import BytesParser
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Query, Request, status

from .archive import AudioArchive, CaptureMetadata
from .asr import ASRBackend, build_asr, require_full_session_vad
from .chat_mirror import ChatMirrorArchive
from .config import Settings
from .episodes import EpisodeArchive
from .vad import EnergySpeechGate, SpeechGate
from .vision import VisionArchive


def _require_configured_token(value: str | None, env_name: str) -> str:
    if value is None or not value.strip():
        raise RuntimeError(f"{env_name} is required; refusing to start without authentication")
    return value.strip()


def _token_ok(expected: str | None, *candidates: str | None) -> bool:
    if expected is None or not expected:
        return False
    return any(value is not None and hmac.compare_digest(value, expected) for value in candidates)


def _bearer_value(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def _read_bounded_body(request: Request, limit: int, *, label: str = "audio body") -> bytes:
    """Read an ASGI request stream without accumulating more than ``limit`` bytes."""
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            declared = int(raw_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
        if declared < 0:
            raise HTTPException(status_code=400, detail="invalid Content-Length")
        if declared > limit:
            raise HTTPException(status_code=413, detail=f"{label} exceeds configured maximum")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail=f"{label} exceeds configured maximum")
        body.extend(chunk)
    return bytes(body)


def _parse_noa_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, bytes]]:
    if (
        len(content_type) > 1024
        or not content_type.isascii()
        or "\r" in content_type
        or "\n" in content_type
    ):
        raise ValueError("multipart Content-Type is invalid")
    envelope = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + body
    )
    message = BytesParser(policy=policy.default).parsebytes(envelope)
    if not message.is_multipart():
        raise ValueError("multipart request boundary is invalid")
    boundary = message.get_boundary()
    if (
        not isinstance(boundary, str)
        or not boundary
        or len(boundary) > 200
        or not boundary.isascii()
    ):
        raise ValueError("multipart request boundary is invalid")
    boundary_bytes = boundary.encode("ascii")
    stripped_body = body[:-2] if body.endswith(b"\r\n") else body
    if not body.startswith(b"--" + boundary_bytes + b"\r\n") or not stripped_body.endswith(
        b"\r\n--" + boundary_bytes + b"--"
    ):
        raise ValueError("multipart request is missing a valid closing boundary")
    if message.defects:
        raise ValueError("multipart request is malformed")
    fields: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for part in message.iter_parts():
        if part.defects:
            raise ValueError("multipart part is malformed")
        if part.is_multipart() or part.get_content_disposition() != "form-data":
            raise ValueError("nested or non-form-data multipart part is unsupported")
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str) or not name or len(name) > 64:
            raise ValueError("multipart part name is invalid")
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            raise ValueError("multipart part payload is invalid")
        if name in fields or name in files:
            raise ValueError(f"duplicate multipart part: {name}")
        if part.get_filename() is not None:
            files[name] = payload
        else:
            field_limit = {
                "messages": 1_048_576,
                "location": 4_096,
                "time": 128,
            }.get(name, 16_384)
            if len(payload) > field_limit:
                raise ValueError(f"multipart field is too large: {name}")
            try:
                fields[name] = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"multipart field is not UTF-8: {name}") from exc
    return fields, files


def _capture_metadata(
    source_session_id: str | None,
    sequence_number: int | None,
    capture_started_at: str | None,
    capture_ended_at: str | None,
    codec: str | None,
    transport: str | None,
    decoder_generation: int | None,
) -> CaptureMetadata | None:
    values = (source_session_id, sequence_number, capture_started_at, capture_ended_at, codec, transport)
    if all(value is None for value in values):
        if decoder_generation is not None:
            raise HTTPException(status_code=422, detail="direct bridge capture headers must be complete")
        return None
    if any(value is None for value in values):
        raise HTTPException(status_code=422, detail="direct bridge capture headers must be complete")
    assert source_session_id is not None
    assert sequence_number is not None
    assert capture_started_at is not None
    assert capture_ended_at is not None
    assert codec is not None
    assert transport is not None
    try:
        started = datetime.fromisoformat(str(capture_started_at).replace("Z", "+00:00"))
        ended = datetime.fromisoformat(str(capture_ended_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="capture timestamps must be ISO8601") from exc
    return CaptureMetadata(
        source_session_id=str(source_session_id),
        sequence_number=int(sequence_number),
        capture_started_at=started,
        capture_ended_at=ended,
        codec=str(codec),
        transport=str(transport),
        decoder_generation=decoder_generation,
    )


def _runtime(
    settings: Settings,
    archive: AudioArchive | None,
    asr: ASRBackend | None,
) -> tuple[AudioArchive, ASRBackend]:
    resolved_archive = archive or AudioArchive(settings)
    resolved_asr = asr or build_asr(
        settings.asr_backend,
        settings.asr_model,
        settings.asr_device,
        settings.asr_compute_type,
        vad_parameters=settings.silero_vad_parameters(),
        rolling_vad_parameters=settings.rolling_silero_vad_parameters(),
    )
    require_full_session_vad(resolved_asr)
    return resolved_archive, resolved_asr


def _speech_gate(settings: Settings, gate: SpeechGate | None) -> SpeechGate:
    return gate or EnergySpeechGate(
        threshold_dbfs=settings.vad_threshold_dbfs,
        minimum_active_ratio=settings.vad_minimum_active_ratio,
    )


async def _export_session_days(archive: AudioArchive, session_id: str) -> None:
    observed_dates = await asyncio.to_thread(archive.transcript_observed_dates, session_id)
    for observed_day in observed_dates:
        await asyncio.to_thread(archive.export_day, observed_day)


async def _run_transcription_cycle(
    settings: Settings,
    archive: AudioArchive,
    asr: ASRBackend,
    gate: SpeechGate,
    episodes: EpisodeArchive | None = None,
    *,
    now_epoch: float | None = None,
) -> None:
    idle_sessions = set(archive.idle_open_sessions(now_epoch=now_epoch))
    if settings.rolling_transcription_enabled:
        for session_id in archive.open_sessions():
            if session_id in idle_sessions:
                continue
            try:
                event = await asyncio.to_thread(
                    archive.process_incremental,
                    session_id,
                    asr,
                    gate,
                    now_epoch=now_epoch,
                )
                if event is not None:
                    await _export_session_days(archive, session_id)
                    if episodes is not None:
                        await asyncio.to_thread(episodes.mark_session_changed, session_id)
            except Exception as exc:
                await asyncio.to_thread(archive.record_rolling_error, session_id, exc)
                continue

        for session_id in archive.sessions_due_reconciliation(now_epoch=now_epoch):
            if session_id in idle_sessions:
                continue
            try:
                await asyncio.to_thread(archive.reconcile_incremental_session, session_id, final=False)
                await _export_session_days(archive, session_id)
                if episodes is not None:
                    await asyncio.to_thread(episodes.mark_session_changed, session_id)
            except Exception as exc:
                await asyncio.to_thread(archive.record_rolling_error, session_id, exc)
                continue

    for session_id in idle_sessions:
        try:
            still_idle = await asyncio.to_thread(
                archive.idle_open_sessions,
                now_epoch=now_epoch,
            )
            if session_id not in still_idle:
                continue
            idle_cutoff_epoch = (
                now_epoch if now_epoch is not None else time.time()
            ) - settings.idle_flush_seconds
            event = await asyncio.to_thread(
                archive.transcribe_session,
                session_id,
                asr,
                force=True,
                idle_cutoff_epoch=idle_cutoff_epoch,
            )
            if event is not None:
                await _export_session_days(archive, session_id)
                if episodes is not None:
                    await asyncio.to_thread(episodes.mark_session_changed, session_id)
        except Exception as exc:
            await asyncio.to_thread(archive.record_rolling_error, session_id, exc)
            continue

    if episodes is not None:
        await asyncio.to_thread(episodes.refresh_due, now_epoch=now_epoch)


def _receiver_lifespan(
    settings: Settings,
    archive: AudioArchive,
    asr: ASRBackend,
    gate: SpeechGate,
    episodes: EpisodeArchive,
    start_worker: bool,
):
    async def worker() -> None:
        while True:
            try:
                await _run_transcription_cycle(settings, archive, asr, gate, episodes)
            except Exception:
                # Archive records bounded failure state. Keep ingestion alive so
                # a failed window can be retried from its durable claim.
                pass
            await asyncio.sleep(settings.worker_poll_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(worker()) if start_worker else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    return lifespan


def create_receiver_app(
    settings: Settings | None = None,
    *,
    archive: AudioArchive | None = None,
    asr: ASRBackend | None = None,
    speech_gate: SpeechGate | None = None,
    start_worker: bool = True,
) -> FastAPI:
    """Create the public receiver surface: exactly ``POST /audio``."""
    settings = settings or Settings.from_env()
    receiver_secret = _require_configured_token(settings.auth_token, "MANFRED_AUTH_TOKEN")
    archive, asr = _runtime(settings, archive, asr)
    episodes = EpisodeArchive(settings)
    gate = _speech_gate(settings, speech_gate)
    app = FastAPI(
        title="Manfred Ears receiver",
        version="0.4.0",
        lifespan=_receiver_lifespan(settings, archive, asr, gate, episodes, start_worker),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.archive = archive
    app.state.asr = asr
    app.state.episodes = episodes
    app.state.speech_gate = gate

    @app.post("/audio", status_code=status.HTTP_202_ACCEPTED)
    async def receive_audio(
        request: Request,
        sample_rate: int = Query(...),
        uid: str = Query(...),
        token: str | None = Query(None),
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
        content_sha256: str | None = Header(None, alias="X-Manfred-Content-SHA256"),
        receiver_token: str | None = Header(None, alias="X-Manfred-Token"),
        authorization: str | None = Header(None, alias="Authorization"),
        capture_session: str | None = Header(None, alias="X-Manfred-Capture-Session"),
        sequence_number: int | None = Header(None, alias="X-Manfred-Sequence"),
        capture_started_at: str | None = Header(None, alias="X-Manfred-Capture-Started-At"),
        capture_ended_at: str | None = Header(None, alias="X-Manfred-Capture-Ended-At"),
        codec: str | None = Header(None, alias="X-Manfred-Codec"),
        transport: str | None = Header(None, alias="X-Manfred-Transport"),
        decoder_generation: int | None = Header(None, alias="X-Manfred-Decoder-Generation"),
    ) -> dict:
        if not _token_ok(receiver_secret, token, receiver_token, _bearer_value(authorization)):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid receiver token")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/octet-stream":
            raise HTTPException(status_code=415, detail="expected application/octet-stream PCM16LE")
        capture = _capture_metadata(
            capture_session,
            sequence_number,
            capture_started_at,
            capture_ended_at,
            codec,
            transport,
            decoder_generation,
        )
        body = await _read_bounded_body(request, settings.max_body_bytes)
        try:
            result = await asyncio.to_thread(
                archive.ingest_pcm,
                uid=uid,
                sample_rate=sample_rate,
                body=body,
                idempotency_key=idempotency_key,
                claimed_sha256=content_sha256,
                capture=capture,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await asyncio.to_thread(episodes.mark_chunk_changed, result.chunk_id)
        return {
            "accepted": True,
            "duplicate": result.duplicate,
            "chunk_id": result.chunk_id,
            "session_id": result.session_id,
            "duration_seconds": result.duration_seconds,
            "sha256": result.sha256,
        }

    return app


def create_vision_receiver_app(
    settings: Settings | None = None,
    *,
    archive: VisionArchive | None = None,
) -> FastAPI:
    """Create a receiver-only Noa custom-server compatibility surface."""
    settings = settings or Settings.from_env()
    vision_secret = _require_configured_token(settings.vision_token, "MANFRED_VISION_TOKEN")
    resolved_archive = archive or VisionArchive(settings)
    episodes = EpisodeArchive(settings)
    app = FastAPI(
        title="Manfred vision receiver",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.vision_archive = resolved_archive
    app.state.episodes = episodes

    @app.post("/noa", status_code=status.HTTP_200_OK)
    async def receive_noa_frame(
        request: Request,
        vision_token: str | None = Header(None, alias="X-Manfred-Vision-Token"),
        authorization: str | None = Header(None, alias="Authorization"),
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict:
        if not _token_ok(vision_secret, vision_token, _bearer_value(authorization)):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid vision token")
        content_type = request.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "multipart/form-data":
            raise HTTPException(status_code=415, detail="expected Noa multipart/form-data")
        body = await _read_bounded_body(
            request,
            settings.max_vision_request_bytes,
            label="vision request",
        )
        try:
            fields, files = _parse_noa_multipart(content_type, body)
            image = files.get("image")
            if image is None:
                raise ValueError("Noa request did not include an image")
            result = await asyncio.to_thread(
                resolved_archive.ingest_noa_jpeg,
                image=image,
                frame_audio=files.get("audio", b""),
                client_reported_time=fields.get("time"),
                idempotency_key=idempotency_key,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        episode_id = await asyncio.to_thread(episodes.ensure_visual, result.visual_id)
        return {
            "user_prompt": "Visual bookmark",
            "message": "Saved this Frame image to Manfred's visual timeline.",
            "image": None,
            "audio": None,
            "debug": {
                "topic_changed": False,
                "visual_id": result.visual_id,
                "episode_id": episode_id,
                "duplicate": result.duplicate,
                "capture_time_basis": "noa-request-receive-time",
            },
        }

    return app


def create_chat_mirror_receiver_app(
    settings: Settings | None = None,
    *,
    archive: ChatMirrorArchive | None = None,
) -> FastAPI:
    """Create a receiver-only ChatGPT Accessibility observation surface."""
    settings = settings or Settings.from_env()
    chat_secret = _require_configured_token(
        settings.chat_mirror_token,
        "MANFRED_CHAT_MIRROR_TOKEN",
    )
    resolved_archive = archive or ChatMirrorArchive(settings)
    app = FastAPI(
        title="Manfred Chat Mirror receiver",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.chat_mirror_archive = resolved_archive

    @app.post("/chat-mirror", status_code=status.HTTP_202_ACCEPTED)
    async def receive_chat_mirror_event(
        request: Request,
        chat_token: str | None = Header(None, alias="X-Manfred-Chat-Token"),
        authorization: str | None = Header(None, alias="Authorization"),
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        content_sha256: str = Header(..., alias="X-Manfred-Content-SHA256"),
    ) -> dict:
        if not _token_ok(chat_secret, chat_token, _bearer_value(authorization)):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid chat mirror token",
            )
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise HTTPException(status_code=415, detail="expected application/json")
        body = await _read_bounded_body(
            request,
            settings.max_chat_mirror_body_bytes,
            label="chat mirror body",
        )
        try:
            result = await asyncio.to_thread(
                resolved_archive.ingest,
                body=body,
                idempotency_key=idempotency_key,
                claimed_sha256=content_sha256,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "accepted": True,
            "duplicate": result.duplicate,
            "event_id": result.event_id,
            "mirror_session_id": result.mirror_session_id,
            "sequence_number": result.sequence_number,
            "sha256": result.sha256,
            "received_at": result.received_at,
        }

    return app


def create_operator_app(
    settings: Settings | None = None,
    *,
    archive: AudioArchive | None = None,
    asr: ASRBackend | None = None,
    speech_gate: SpeechGate | None = None,
) -> FastAPI:
    """Create the local/tailnet operator surface; it never accepts audio."""
    settings = settings or Settings.from_env()
    operator_secret = _require_configured_token(settings.operator_token, "MANFRED_OPERATOR_TOKEN")
    archive, asr = _runtime(settings, archive, asr)
    vision_archive = VisionArchive(settings)
    chat_mirror_archive = ChatMirrorArchive(settings)
    episodes = EpisodeArchive(settings)
    gate = _speech_gate(settings, speech_gate)
    app = FastAPI(
        title="Manfred Ears operator",
        version="0.4.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.archive = archive
    app.state.asr = asr
    app.state.vision_archive = vision_archive
    app.state.chat_mirror_archive = chat_mirror_archive
    app.state.episodes = episodes
    app.state.speech_gate = gate

    def authorize(header_token: str | None, authorization: str | None) -> None:
        if not _token_ok(operator_secret, header_token, _bearer_value(authorization)):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid operator token")

    @app.get("/health")
    def health() -> dict:
        return {
            "ok": True,
            "service": "manfred-ears-operator",
            "asr": getattr(asr, "name", type(asr).__name__),
            "speech_filter": {
                "backend": "silero-vad",
                "rolling_enabled": settings.rolling_transcription_enabled,
                "full_session_parameters": settings.silero_vad_parameters(),
                "rolling_parameters": settings.rolling_silero_vad_parameters(),
            },
            "raw_retention": "indefinite-until-explicit-policy-change",
        }

    @app.get("/v1/status")
    def service_status(
        operator_token: str | None = Header(None, alias="X-Manfred-Operator-Token"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict:
        authorize(operator_token, authorization)
        return {
            **archive.status(),
            "vision": vision_archive.status(),
            "chat_mirror": chat_mirror_archive.status(),
            "multimodal": episodes.status(),
        }

    @app.post("/v1/sessions/{session_id}/flush")
    async def flush_session(
        session_id: str,
        force: bool = Query(False),
        operator_token: str | None = Header(None, alias="X-Manfred-Operator-Token"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict:
        authorize(operator_token, authorization)
        try:
            require_full_session_vad(asr)
            event = await asyncio.to_thread(
                archive.transcribe_session,
                session_id,
                asr,
                force=force,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown session") from exc
        if event is not None:
            await _export_session_days(archive, session_id)
            await asyncio.to_thread(episodes.mark_session_changed, session_id)
        return event

    @app.get("/v1/search")
    def search_transcripts(
        q: str = Query(..., min_length=1, max_length=500),
        limit: int = Query(10, ge=1, le=100),
        operator_token: str | None = Header(None, alias="X-Manfred-Operator-Token"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict:
        authorize(operator_token, authorization)
        hits = archive.search(q, limit)
        return {"query": q, "count": len(hits), "hits": hits}

    @app.get("/v1/chat-mirror/search")
    def search_chat_mirror(
        q: str = Query(..., min_length=1, max_length=500),
        limit: int = Query(10, ge=1, le=100),
        operator_token: str | None = Header(None, alias="X-Manfred-Operator-Token"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict:
        authorize(operator_token, authorization)
        try:
            hits = chat_mirror_archive.search(q, limit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"query": q, "count": len(hits), "hits": hits}

    @app.get("/v1/chat-mirror/sessions/{mirror_session_id}")
    def get_chat_mirror_session(
        mirror_session_id: str,
        operator_token: str | None = Header(None, alias="X-Manfred-Operator-Token"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict:
        authorize(operator_token, authorization)
        try:
            return chat_mirror_archive.get_session(mirror_session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown chat mirror session") from exc

    @app.delete("/v1/chat-mirror/sessions/{mirror_session_id}")
    def delete_chat_mirror_session(
        mirror_session_id: str,
        operator_token: str | None = Header(None, alias="X-Manfred-Operator-Token"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict:
        authorize(operator_token, authorization)
        if not chat_mirror_archive.delete_session(mirror_session_id):
            raise HTTPException(status_code=404, detail="unknown chat mirror session")
        return {"deleted": True, "mirror_session_id": mirror_session_id}

    @app.post("/v1/episodes/refresh")
    async def refresh_episodes(
        limit: int = Query(100, ge=1, le=1000),
        operator_token: str | None = Header(None, alias="X-Manfred-Operator-Token"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict:
        authorize(operator_token, authorization)
        refreshed = await asyncio.to_thread(episodes.refresh_due, limit=limit)
        return {
            "count": len(refreshed),
            "episodes": [result.__dict__ for result in refreshed],
        }

    @app.get("/v1/episodes")
    def list_episodes(
        limit: int = Query(20, ge=1, le=100),
        operator_token: str | None = Header(None, alias="X-Manfred-Operator-Token"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict:
        authorize(operator_token, authorization)
        records = episodes.list(limit)
        return {"count": len(records), "episodes": records}

    @app.get("/v1/episodes/{episode_id}")
    def get_episode(
        episode_id: str,
        operator_token: str | None = Header(None, alias="X-Manfred-Operator-Token"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict:
        authorize(operator_token, authorization)
        try:
            return episodes.get(episode_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown episode") from exc

    @app.delete("/v1/sessions/{session_id}")
    def delete_session(
        session_id: str,
        operator_token: str | None = Header(None, alias="X-Manfred-Operator-Token"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict:
        authorize(operator_token, authorization)
        episodes.mark_session_changed(session_id)
        if not archive.delete_session(session_id):
            raise HTTPException(status_code=404, detail="unknown session")
        return {"deleted": True, "session_id": session_id}

    @app.delete("/v1/vision/{visual_id}")
    def delete_visual_event(
        visual_id: str,
        operator_token: str | None = Header(None, alias="X-Manfred-Operator-Token"),
        authorization: str | None = Header(None, alias="Authorization"),
    ) -> dict:
        authorize(operator_token, authorization)
        episodes.mark_visual_changed(visual_id)
        deleted = vision_archive.delete(visual_id)
        episodes.delete_for_visual(visual_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="unknown visual event")
        return {"deleted": True, "visual_id": visual_id}

    return app


# Backward-compatible factory name now resolves to the safe receiver-only app.
def create_app(
    settings: Settings | None = None,
    *,
    archive: AudioArchive | None = None,
    asr: ASRBackend | None = None,
    speech_gate: SpeechGate | None = None,
    start_worker: bool = True,
) -> FastAPI:
    return create_receiver_app(
        settings,
        archive=archive,
        asr=asr,
        speech_gate=speech_gate,
        start_worker=start_worker,
    )
