from __future__ import annotations

import argparse
import hashlib
import json
import time
import wave
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .archive import AudioArchive
from .asr import build_asr, require_full_session_vad
from .config import Settings
from .metrics import wav_metrics, word_error_rate


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if getattr(args, "state_dir", None):
        settings = replace(settings, state_dir=Path(args.state_dir).expanduser())
    return settings


def _asr(settings: Settings, args: argparse.Namespace):
    return build_asr(
        getattr(args, "backend", None) or settings.asr_backend,
        getattr(args, "model", None) or settings.asr_model,
        getattr(args, "device", None) or settings.asr_device,
        getattr(args, "compute_type", None) or settings.asr_compute_type,
        vad_parameters=settings.silero_vad_parameters(),
        rolling_vad_parameters=settings.rolling_silero_vad_parameters(),
    )


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .service import create_receiver_app

    settings = _settings(args)
    uvicorn.run(create_receiver_app(settings), host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_serve_operator(args: argparse.Namespace) -> int:
    import uvicorn

    from .service import create_operator_app

    settings = _settings(args)
    uvicorn.run(create_operator_app(settings), host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_serve_vision(args: argparse.Namespace) -> int:
    import uvicorn

    from .service import create_vision_receiver_app

    settings = _settings(args)
    uvicorn.run(
        create_vision_receiver_app(settings),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


def cmd_ingest_wav(args: argparse.Namespace) -> int:
    archive = AudioArchive(_settings(args))
    source = Path(args.audio)
    with wave.open(str(source), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise SystemExit("ingest-wav expects mono PCM16 WAV")
        sample_rate = handle.getframerate()
        frames_per_chunk = max(1, int(sample_rate * args.chunk_seconds))
        chunks: list[bytes] = []
        while True:
            data = handle.readframes(frames_per_chunk)
            if not data:
                break
            chunks.append(data)
    base = datetime.now(timezone.utc)
    file_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    results = []
    elapsed = 0.0
    for index, body in enumerate(chunks):
        duration = len(body) / (sample_rate * 2)
        elapsed += duration
        result = archive.ingest_pcm(
            uid=args.uid,
            sample_rate=sample_rate,
            body=body,
            idempotency_key=f"fixture:{file_hash}:{index}",
            received_at=base + timedelta(seconds=elapsed),
        )
        results.append(result.__dict__)
    print(json.dumps({"chunks": results, "session_id": results[-1]["session_id"] if results else None}, indent=2))
    return 0


def cmd_flush(args: argparse.Namespace) -> int:
    settings = _settings(args)
    archive = AudioArchive(settings)
    asr = _asr(settings, args)
    require_full_session_vad(asr)
    event = archive.transcribe_session(args.session_id, asr, force=args.force)
    for observed_day in archive.transcript_observed_dates(args.session_id):
        archive.export_day(observed_day)
    print(json.dumps(event, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(json.dumps(AudioArchive(_settings(args)).status(), indent=2))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    hits = AudioArchive(_settings(args)).search(args.query, args.limit)
    print(json.dumps({"query": args.query, "count": len(hits), "hits": hits}, indent=2))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    path = AudioArchive(_settings(args)).export_day(date.fromisoformat(args.date))
    print(path)
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    deleted = AudioArchive(_settings(args)).delete_session(args.session_id)
    print(json.dumps({"session_id": args.session_id, "deleted": deleted}))
    return 0 if deleted else 1


def cmd_benchmark(args: argparse.Namespace) -> int:
    settings = _settings(args)
    audio = Path(args.audio)
    metrics = wav_metrics(audio)
    asr = _asr(settings, args)
    started = time.perf_counter()
    result = asr.transcribe(audio)
    wall = time.perf_counter() - started
    report = {
        "audio": metrics,
        "asr": result.to_dict(),
        "wall_latency_seconds": wall,
        "real_time_factor": wall / metrics["duration_seconds"] if metrics["duration_seconds"] else None,
        "storage": {
            "pcm16_mono_bytes_per_day": metrics["sample_rate"] * 2 * 86400,
            "pcm16_mono_gib_per_day": metrics["sample_rate"] * 2 * 86400 / (1024**3),
        },
    }
    if args.reference:
        report["accuracy"] = word_error_rate(args.reference, result.text)
    print(json.dumps(report, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manfred Ears v0 evidence-first audio archive")
    parser.add_argument("--state-dir", help="Override MANFRED_STATE_DIR")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the receiver-only POST /audio surface")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=cmd_serve)

    operator = sub.add_parser("serve-operator", help="Run local/tailnet status/search/flush/delete surface")
    operator.add_argument("--host", default="127.0.0.1")
    operator.add_argument("--port", type=int, default=8788)
    operator.add_argument("--log-level", default="info")
    operator.set_defaults(func=cmd_serve_operator)

    vision = sub.add_parser("serve-vision", help="Run receiver-only POST /noa Frame compatibility surface")
    vision.add_argument("--host", default="127.0.0.1")
    vision.add_argument("--port", type=int, default=8789)
    vision.add_argument("--log-level", default="info")
    vision.set_defaults(func=cmd_serve_vision)

    ingest = sub.add_parser("ingest-wav", help="Replay a mono PCM16 WAV through the chunk archive")
    ingest.add_argument("audio")
    ingest.add_argument("--uid", default="local-validation")
    ingest.add_argument("--chunk-seconds", type=float, default=1.0)
    ingest.set_defaults(func=cmd_ingest_wav)

    flush = sub.add_parser("flush")
    flush.add_argument("session_id")
    flush.add_argument("--force", action="store_true", help="Create a new transcript version")
    flush.add_argument("--backend", choices=("stub", "faster-whisper", "parakeet"))
    flush.add_argument("--model")
    flush.add_argument("--device")
    flush.add_argument("--compute-type")
    flush.set_defaults(func=cmd_flush)

    status_parser = sub.add_parser("status")
    status_parser.set_defaults(func=cmd_status)

    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.set_defaults(func=cmd_search)

    export = sub.add_parser("export-day")
    export.add_argument("date")
    export.set_defaults(func=cmd_export)

    delete = sub.add_parser("delete-session")
    delete.add_argument("session_id")
    delete.set_defaults(func=cmd_delete)

    benchmark = sub.add_parser("benchmark")
    benchmark.add_argument("audio")
    benchmark.add_argument("--reference")
    benchmark.add_argument("--backend", choices=("stub", "faster-whisper", "parakeet"))
    benchmark.add_argument("--model")
    benchmark.add_argument("--device")
    benchmark.add_argument("--compute-type")
    benchmark.set_defaults(func=cmd_benchmark)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
