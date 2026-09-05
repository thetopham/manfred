# Preserved Ears implementation guide

This guide is extracted from the current Brain source. Historical host setup examples describe the previous deployment; use the new repository README and installer plan for configuration. Extraction does not apply deployment commands.

# Manfred Ears v0

Evidence-first wearable audio ingestion for Demerzel.

This experiment implements one narrow vertical slice:

```text
Omi Dev Kit 2
  → Manfred Companion on Samsung Galaxy S25
  → BLE Opus/PCM decode + durable phone spool
  → Tailscale direct transport
  → Manfred Ears `/audio`
  → immutable PCM16 chunks + SHA-256 provenance
  → cheap deterministic energy prefilter + packaged Silero neural VAD
  → incremental faster-whisper on confirmed speech only
  → provisional searchable transcript segments in ~5–15 seconds
  → periodic/final composition into versioned canonical transcripts
  → local daily Wiki-distillation inbox
```

The companion source lives at `../apps/manfred-companion/`. A bounded Frame bootstrap accepts tap-triggered Noa custom-server JPEGs into a separate vision receiver. Each accepted image now creates a durable image-centered multimodal episode that waits for its future context window, then references the original JPEG, overlapping Omi chunks, the latest overlapping transcript segments, and explicit Frame trigger events. It deliberately does **not** yet implement periodic/continuous Frame capture in the companion, adaptive episode boundaries, visual model inference, custom Omi firmware, automatic durable-memory promotion, or a production Supabase migration.

## ChatGPT Conversation Mirror MVP

ChatGPT Android Live remains the initial realtime cognition interface because it carries the user's existing ChatGPT account context. Manfred Companion adds a deliberately narrow Accessibility service restricted to package `com.openai.chatgpt`; it observes rendered window text but performs no UI actions and never accesses ChatGPT authentication or private APIs.

Each bounded Accessibility observation is committed to app-private native storage, imported into an atomic Flutter spool, hash-checked immediately before upload, and sent to a fourth receiver-only capability:

```text
ChatGPT Android UI/history
→ ChatGPT-only Accessibility snapshot
→ native app-private commit
→ Flutter Chat Mirror spool
→ POST /chat-mirror on tailnet port 8790
→ exact JSON evidence + sidecar + SQLite/FTS index
→ operator search/session retrieval/delete
```

The receiver requires a dedicated `MANFRED_CHAT_MIRROR_TOKEN`, `Idempotency-Key`, and `X-Manfred-Content-SHA256`. It rejects changed-body key reuse and changed evidence for an existing `(mirror_session_id, sequence_number)`. Accessibility text is indexed as a versioned `android-accessibility` observation with explicit `partial`/`gap`/`complete` and `provisional`/`final` metadata; it is not mislabeled as an official ChatGPT transcript.

This software slice does not yet prove that the real ChatGPT app exposes complete dialogue through Android Accessibility, that Live session boundaries can be reconstructed exactly, or that EyeVue custom-BLE Opus can coexist with HFP audio. Those remain physical acceptance gates.

## Frame bootstrap: Noa custom-server compatibility

Noa already captures one Frame JPEG plus short Frame-microphone audio after a tap interaction and can POST both to a custom endpoint. Manfred uses that as a disposable physical bootstrap while the durable Frame connector is added to Manfred Companion:

```text
Frame tap → Noa multipart POST /noa → receiver-only vision app on 7090
                                      → exact JPEG + sidecar + SHA-256
                                      → visual_events timeline row
                                      → Noa-compatible capture receipt on Frame
```

The compatibility receiver ignores Noa's conversation history and location fields. It retains only the exact JPEG, server receive/capture estimate, Noa's unzoned client time string, source/trigger, image hash, and the Frame-audio byte count/hash. Frame audio itself is not archived because Omi remains the canonical continuous ears. The capture time is honestly labeled `noa-request-receive-time`; Noa does not supply the exposure-time interval required by the eventual synchronized Companion path.

Noa does not provide Manfred's durable offline spool or periodic capture, so this endpoint is a hardware/transport slice rather than the final lifelog architecture. The permanent path remains Frame → Manfred Companion → durable JPEG spool → visual archive → synchronized Omi/Frame episodes.

## Fixed synchronized episodes

The first episode contract is intentionally simple and observable. A Frame image estimated at time `t` creates a pending interval from `t - 15 seconds` through `t + 15 seconds`. It becomes ready only after the future half of that interval plus a small finalization grace has elapsed. The receiver worker then records references to:

- the anchor JPEG and nearby Frame JPEG IDs inside the interval;
- overlapping Omi chunk IDs, using direct S25 capture epochs when present and an explicitly lower-quality receive-time estimate only for legacy audio;
- deterministic segment references from the latest canonical transcript version for each overlapping audio session;
- Frame tap/query event IDs;
- clock basis, timing quality, version, and a hash of the exact reference set.

JPEG and PCM bytes are never copied into the episode. SQLite source triggers atomically mark overlapping episodes dirty in the same commit that inserts/deletes audio, transcripts, or visual evidence, so an app crash cannot lose the late-evidence signal. Startup/worker reconciliation backfills pre-existing visuals and removes pre-trigger orphan anchors. Idempotent audio retries re-mark their existing chunk as a second recovery path. The five-second receiver worker rematerializes dirty/due episodes and increments the episode version only when its references change. Noa timing remains honestly labeled `noa-request-receive-time` with low quality; the later owned Companion path must add capture-request/readiness/receipt bounds on the shared S25 monotonic clock.

The v0 implementation uses one image-centered episode per anchor and includes nearby frames. Coalescing overlapping anchors into larger adaptive scenes remains later work.

## Decision: the archive is deeper than the Wiki

The evidence hierarchy is:

```text
raw audio chunks                 canonical observed evidence
raw Frame JPEGs                  canonical visual evidence
  ↓
versioned ASR / visual context   machine interpretation of evidence
  ↓
synchronized episodes / search  exact and semantic recall surface
  ↓
daily distillation              selected understanding
  ↓
LLM Wiki / memories / tasks      durable compressed state
```

Raw audio is retained indefinitely in v0. Nothing deletes it automatically. The only deletion path is an explicit authenticated API call or CLI command. This is intentional until real Omi recordings establish whether the microphone, transport, ASR model, names, technical vocabulary, noise handling, and multi-speaker behavior are good enough.

The full transcript archive is local and searchable with SQLite FTS5. The Wiki receives a local scratch inbox for daily distillation rather than becoming the transcript database. A future migration to production Supabase/Postgres plus pgvector can add hybrid FTS/vector retrieval after this file/SQLite contract proves useful; v0 does not modify the existing production Supabase service.

## Omi paths: direct is primary, cloud webhook is compatibility only

Omi documents raw-audio streaming as a stock integration-app capability intended for custom speech recognition and audio analysis.[1][2]

The developer webhook payload is PCM16 little-endian mono with `sample_rate` and `uid` query parameters.[2][3]

DevKit 2 supports standalone storage, but the stock transcription workflow still reconnects to the app for processing.[4]

The current open-source app stores the configured audio URL and delay through Omi's user-webhook API.[12]

The current backend pusher accumulates decoded PCM, queues the developer webhook, and the backend webhook client performs the outbound HTTP POST.[13][14]

The webhook client adds an `Idempotency-Key`, retries failures, preserves existing URL query parameters, and currently splits delivery into one-second PCM chunks.[13]

### Stock webhook correction and retirement

**The stock webhook POST originates from Omi's backend, not directly from the S25.** It would require public HTTPS ingress and would still route decoded audio through Omi. That route remains supported by the receiver contract for compatibility, but it is no longer the planned Manfred path. Funnel is not required or planned for the direct bridge.

Manfred Companion now implements the direct S25 bridge using Omi's published BLE audio UUIDs, codec IDs, three-byte packet framing, and 16 kHz mono PCM contract.[3][15] It preserves phone-derived capture/session/sequence metadata and spools raw PCM before uploading through Tailscale. Interrupted phone-spool commits are reconciled by content hash, and chunks that arrive while ASR is running remain pending for an automatic next transcript version rather than being marked processed by an earlier snapshot.

### Physical validation receipt — 2026-08-22

The first real Omi Dev Kit 2 → Galaxy S25 → Demerzel run succeeded over the tailnet receiver at `http://100.126.233.3:8787/audio`:

- backend session: `s25-20260822T055801Z-515cc7c8`;
- phone capture session: `486fe5a8-c4b6-477e-9b31-3bd319d9d567`;
- duration: 322.48 seconds at 16 kHz mono PCM16LE;
- source sequence: 0 through 322, with zero missing chunks;
- transport: `s25-direct-ble-tailscale`;
- capture time basis: phone-derived S25 PCM sample clock;
- faster-whisper `base.en` on Demerzel CPU: approximately 1.85 seconds for the 5:22 recording.

The source was physically confirmed as the Omi microphone: the companion requests no Android `RECORD_AUDIO` permission, `Start ears` connects the selected Omi BLE audio characteristic, the Omi LED changes for streaming, and BLE packet flow follows the Omi connection state. This receipt is established evidence; the direct transport is not redesigned by the rolling-ASR slice.

### Stronger-model promotion receipt — 2026-08-22

The deployable Demerzel default is now faster-whisper `large-v3-turbo` on CPU/int8 rather than `base.en`. The identical retained 2,930.44-second (48:50) physical Omi session was replayed through six local configurations before promotion. `base.en` completed in 78.13 seconds (37.51x realtime); `large-v3-turbo` completed in 644.09 seconds (4.55x realtime) and produced the best Whisper/event-recall result, recovering exact “Good morning,” “High Altar,” and “Come on,” plus an approximate final utterance. No tested model recovered the restaurant order or salsa request. The event list is not a verbatim reference transcript, so this receipt establishes useful keyphrase recall and sufficient CPU throughput, not whole-session WER.

NVIDIA `parakeet-unified-en-0.6b` remains the conservative candidate for a future streaming first pass: its CPU INT8 run completed in 276.90 seconds (10.58x realtime) with fewer hallucinations but less quiet-speech recall. Until Manfred has separate first- and second-pass model configuration, `large-v3-turbo` is the stronger single live backend and the recommended session-close model. No GPU was reassigned for this promotion.

## Archive contract

Default state root: `~/.hermes/manfred-ears/` (override with `MANFRED_STATE_DIR`).

```text
archive.sqlite3                 session/chunk/canonical transcript index + rolling state + FTS5
raw/YYYY-MM-DD/<session>/       original received PCM16 webhook bodies
  chunk-<uuid>.pcm
vision/raw/YYYY-MM-DD/          exact Frame JPEGs + crash-recoverable sidecars
  frame-<digest>.jpg
  frame-<digest>.json
chat-mirror/raw/YYYY-MM-DD/     exact Accessibility observation JSON + sidecars
  chat-<digest>.json
  chat-<digest>.meta.json
events/YYYY-MM-DD/              immutable rolling-window events and canonical transcript versions
wiki-inbox/YYYY-MM-DD.md        local scratch for nightly distillation
audit/events.jsonl              bounded operational events without raw uid/text
tmp/                            temporary bounded-window/full-manual WAVs; removed after ASR
```

The original webhook bodies are canonical. A temporary WAV is assembled only for ASR and then removed; this avoids permanently doubling storage. Every chunk records SHA-256, byte count, sample rate, duration, receive time, session ID, idempotency key, and receiver-computed PCM peak/RMS/full-scale/DC-offset metrics. Direct S25 chunks additionally retain the source capture-session ID, sequence, phone-derived start/end, codec, transport, and decoder generation. The user-supplied Omi `uid` is SHA-256 hashed before persistence and is not written to the audit log.

A transcript event retains:

- event/session/transcript-version IDs;
- device, phone bridge, transport, relay, and hashed source identity;
- first/last receive times and an explicitly approximate observed interval;
- capture-time quality (`phone-derived-capture-time` or `relay-arrival-estimate`) and timestamp basis;
- evidence chunk IDs/paths and whole-session audio SHA-256;
- model, language, latency, segment timestamps, Whisper log-probability metrics, and honest `confidence: null` when no calibrated probability exists;
- speaker and diarization status (currently unknown/not run);
- `derived_from` links;
- explicit-observation vs inference status;
- pending Wiki/memory promotion state.

Direct S25 sessions use the companion's PCM sample clock and preserve sequence gaps. Because the published Omi packet header does not expose a documented hardware timestamp, this is honestly labeled phone-derived rather than device-clock evidence. Legacy Omi-cloud sessions retain the bounded `last_received_at - total PCM duration` estimate.

## Session-close transcription and optional rolling preview

The production default archives continuously but defers transcription until the capture has been idle for the configured interval. It then runs the complete retained session once through faster-whisper's packaged Silero VAD and Whisper decoder. This uses the mature upstream batch pipeline as the canonical authority and avoids resetting VAD state every ten seconds. Full-session failures remain durable and retry automatically with bounded exponential backoff; the idle cutoff is revalidated inside the archive write lock so late audio cannot be finalized into a stale snapshot. Backends without integrated full-session speech filtering are rejected in this mode rather than being allowed to decode environmental noise. The older rolling path remains available behind `MANFRED_ROLLING_TRANSCRIPTION_ENABLED=true` for deliberate preview experiments, but is disabled by default and never substitutes for the session-close full rerun.

When rolling preview is explicitly enabled, every worker poll snapshots only chunks that do not yet have a durable rolling decision:

1. The dependency-free energy gate is retained only as a cheap prefilter that decides which immutable one-second chunks can form rolling windows. It is not treated as proof of human speech: movement, wind, traffic, clothing noise, and animal sounds can all cross an energy threshold. Raw silence and rejected audio remain untouched in the archive.
2. Candidate chunks are grouped into bounded windows, normally at most 10 seconds and closed by one second of trailing silence. Incoming chunks have a hard 15-second duration ceiling, so even an indivisible compatibility chunk remains within the initial latency envelope. A source-sequence gap always splits the window rather than collapsing missing time.
3. A window is durably claimed before ASR. Its exact chunk list survives restart; failed or stale claims retry the same evidence range.
4. Faster-whisper runs only on that new window with its packaged Silero neural VAD enabled. Silero performs the authoritative human-voice selection before Whisper decoding, while previous-text conditioning remains disabled for rolling windows to prevent cross-window repetition. A candidate window containing only environmental noise therefore commits an empty result instead of forcing Whisper to invent words. Committed non-empty provisional segments immediately enter a unified canonical/rolling FTS5 index and local Wiki scratch export.
5. Hourly provisional reconciliation may still compose committed rolling segments for searchable near-live context. When capture becomes idle, however, the worker assembles the exact retained session once and runs faster-whisper's complete packaged VAD+ASR pipeline over it. That full-session result supersedes provisional rolling output and becomes the canonical Wiki/search transcript; rolling fragments are never accepted as the final transcript merely because they exist.

With rolling preview enabled, this yields the initial 5–15 second latency target from a five-second worker poll plus a roughly ten-second maximum speech window. With the safer production default, canonical text appears after capture stops plus the idle interval and one bounded full-session VAD+ASR pass.

Each rolling segment records its backend and source capture session, source sequence range, exact chunk IDs/paths and window SHA-256, sample interval relative to the concatenated window PCM, phone/sample-clock time estimates, model and rolling schema revision, and `provisional`/`finalized`/`superseded` state with reconciliation linkage. Rolling output is durable derived evidence, not canonical raw evidence and not unquestioned memory.

Configuration knobs:

```text
MANFRED_WORKER_POLL_SECONDS=5
MANFRED_MAX_CHUNK_DURATION_SECONDS=15
MANFRED_ROLLING_WINDOW_SECONDS=10
MANFRED_ROLLING_TRAILING_SILENCE_SECONDS=1
MANFRED_ROLLING_RECOVERY_SECONDS=60
MANFRED_ROLLING_RECONCILE_SECONDS=3600
MANFRED_ROLLING_TRANSCRIPTION_ENABLED=false
MANFRED_VAD_THRESHOLD_DBFS=-45
MANFRED_VAD_MINIMUM_ACTIVE_RATIO=0.08
MANFRED_SILERO_VAD_THRESHOLD=0.10
MANFRED_SILERO_VAD_NEG_THRESHOLD=0.05
MANFRED_SILERO_VAD_ROLLING_THRESHOLD=0.50
MANFRED_SILERO_VAD_ROLLING_NEG_THRESHOLD=0.35
MANFRED_SILERO_VAD_MIN_SPEECH_DURATION_MS=50
MANFRED_SILERO_VAD_MIN_SILENCE_DURATION_MS=300
MANFRED_SILERO_VAD_SPEECH_PAD_MS=300
```

Silero tuning remains backend-owned and appears on the loopback operator `/health` response. The full-session profile favors recall on low-signal wearable audio; the optional rolling profile is stricter because short-window VAD resets are more prone to noise false positives. The phone app intentionally has no threshold slider: it should capture and transport evidence consistently, while server-side profiles can be calibrated against retained labeled recordings without coupling ASR policy to a particular client build.

The rolling implementation was also exercised against a temporary copy of the established 323-chunk physical session. It retained all 323 raw files with the same SHA-256 multiset, produced 13 bounded ASR windows and 13 ASR calls, kept the longest model segment to 7.0 seconds, recovered both spoken passages, returned a canonical FTS hit, and finalized with `full_session_asr_rerun: false`.

## Storage reality

Uncompressed 16 kHz mono PCM16 is 2,764,800,000 bytes/day: about **2.575 GiB/day**, **77.25 GiB/30 days**, or **0.918 TiB/year** at continuous 24/7 capture. The first trial is intentionally bounded. Do not turn on unlimited all-day collection before measuring speech duty cycle and deciding whether lossless FLAC consolidation, an encrypted rolling tier, or larger storage is appropriate. Raw evidence still remains undeleted during the validation phase.

## API isolation and authentication

The service is split into four independent FastAPI applications and ports.

**Receiver app — tailnet target for Manfred Companion:**

- `POST /audio?sample_rate=16000&uid=...&token=...` — Omi PCM16 webhook receiver.
- No `/health`, `/v1/*`, docs, OpenAPI, search, flush, status, or delete route exists on this app.

Direct companion requests add a complete all-or-nothing header set: capture session, sequence, capture start/end, decoded codec, and transport, plus a decoder generation and explicit content digest on new clients. The receiver rejects partial metadata or timestamps that disagree materially with the PCM duration. It recomputes every PCM digest before the first database write and validates both `X-Manfred-Content-SHA256` and a 64-hex digest suffix embedded in new direct-bridge idempotency keys. Older cloud-webhook and pre-upgrade direct requests remain compatible when those optional integrity fields are absent. Source-session identity groups retries/network gaps without relying on receive-time proximity.

**Operator app — loopback/tailnet only:**

- `GET /health` — non-sensitive liveness and ASR identity.
- `GET /v1/status` — audio, visual, Chat Mirror, and episode counts.
- `POST /v1/sessions/{id}/flush` — create or reuse the session's final full-session Silero-VAD + ASR canonical transcript; `force=true` deliberately bypasses reuse and creates a new full-session transcript version for operator reprocessing.
- `GET /v1/search?q=...` — SQLite FTS5 retrieval over the latest canonical transcript plus unreconciled near-live segments, each with state and provenance.
- `POST /v1/episodes/refresh` — bounded operator-triggered materialization of due/dirty image-centered episodes.
- `GET /v1/episodes` and `GET /v1/episodes/{id}` — retrieve episode reference sets, timing quality, and versions without copying raw evidence.
- `GET /v1/chat-mirror/search?q=...` — FTS retrieval over mirrored Accessibility observations.
- `GET /v1/chat-mirror/sessions/{id}` — ordered raw-observation metadata and exact-evidence references for one mirrored session.
- `DELETE /v1/chat-mirror/sessions/{id}` — explicit mirrored-session evidence and index deletion.
- `DELETE /v1/sessions/{id}` — explicit evidence/transcript deletion; affected episodes are rematerialized.
- `DELETE /v1/vision/{id}` — explicit JPEG, sidecar, and visual timeline deletion.
- No `/audio` route exists on this app.

**Vision receiver — tailnet-only Noa compatibility surface:**

- `POST /noa` — bounded multipart request containing a Frame JPEG and optional Frame WAV.
- Requires `X-Manfred-Vision-Token` or the standard Bearer authorization header; query tokens are unsupported.
- Returns the exact JSON fields Noa expects: `user_prompt`, `message`, optional `image`/`audio`, and `debug.topic_changed`.
- No `/audio`, `/health`, `/v1/*`, docs, OpenAPI, search, status, or delete route exists on this app.
- Uses a third high-entropy token so Noa cannot authorize audio ingestion or operator actions.

**Chat Mirror receiver — tailnet-only Accessibility observation surface:**

- `POST /chat-mirror` — bounded UTF-8 JSON observation body retained byte-for-byte before indexing.
- Requires `X-Manfred-Chat-Token` or Bearer authorization plus `Idempotency-Key` and `X-Manfred-Content-SHA256`; query tokens are unsupported.
- Accepts only the versioned ChatGPT-only Accessibility schema and exposes no audio, vision, operator, docs, OpenAPI, status, search, or delete route.
- Uses a fourth high-entropy token that cannot authorize audio, vision, or operator actions.

The audio receiver refuses startup unless `MANFRED_AUTH_TOKEN` is configured. The operator independently refuses startup unless `MANFRED_OPERATOR_TOKEN` is configured. The vision receiver independently refuses startup unless `MANFRED_VISION_TOKEN` is configured. The Chat Mirror receiver independently refuses startup unless `MANFRED_CHAT_MIRROR_TOKEN` is configured. Audio, vision, and Chat Mirror clients receive ingestion-only capabilities and cannot authorize status, search, re-transcription, or deletion. Operator calls use the standard Bearer authorization header or `X-Manfred-Operator-Token`; query-string operator tokens are intentionally unsupported.

The receiver rejects a declared oversized `Content-Length` before reading and also streams the body through a hard byte counter, so missing or dishonest length headers cannot cause the application to accumulate an unbounded body. `token=` remains compatible with Omi's stock webhook. Manfred Companion instead uses `X-Manfred-Token`, keeping the receiver secret out of URLs/access logs, and stores it in Android secure storage.

## Run locally

The Demerzel control plane already has compatible FastAPI, Uvicorn, and faster-whisper packages. From this directory:

```bash
export PYTHONPATH="$PWD"
export MANFRED_STATE_DIR=/tmp/manfred-ears-validation
export HF_HOME="$MANFRED_STATE_DIR/models/huggingface"
export MANFRED_AUTH_TOKEN='<high-entropy-receiver-token>'
export MANFRED_OPERATOR_TOKEN='<different-high-entropy-operator-token>'
export MANFRED_VISION_TOKEN='<third-high-entropy-vision-token>'
export MANFRED_CHAT_MIRROR_TOKEN='<fourth-high-entropy-chat-ingest-token>'
export MANFRED_ASR_MODEL=large-v3-turbo
export MANFRED_ASR_DEVICE=cpu
export MANFRED_ASR_COMPUTE_TYPE=int8
# Receiver: bind to Demerzel's Tailscale address for the S25.
python3 -m manfred_ears serve --host 100.x.y.z --port 8787

# Terminal 2: keep loopback/tailnet-only.
python3 -m manfred_ears serve-operator --host 127.0.0.1 --port 8788

# Terminal 3: bind only to the S25-reachable tailnet address.
python3 -m manfred_ears serve-vision --host 100.x.y.z --port 8789

# Terminal 4: separate ChatGPT Accessibility observation capability.
python3 -m manfred_ears serve-chat-mirror --host 100.x.y.z --port 8790
```

No Funnel is needed. Keep all ports tailnet/local-only, with audio, vision, Chat Mirror, and operator tokens scoped independently.

In Noa's **Tune → Server → Custom Server** fields use:

```text
API Endpoint:     http://100.x.y.z:8789/noa
API Header Value: the MANFRED_VISION_TOKEN value
API Header Key:   X-Manfred-Vision-Token
```

Noa captures after the first tap and submits after the finishing tap. A successful request displays `Saved this Frame image to Manfred's visual timeline.` on Frame.

## Always-on 7090 deployment and Demerzel link

`7090` is the canonical Manfred evidence data plane. It owns raw audio, Frame JPEGs, ChatGPT Accessibility observations, the shared SQLite archive, transcript versions, event records, Hugging Face model cache, receiver apps, operator API, and rolling CPU ASR. The audio receiver binds only `7090`'s Tailscale address (`100.112.32.64:8787`), vision uses tailnet port `8789`, Chat Mirror uses tailnet port `8790`, and the authenticated operator API remains loopback-only on `127.0.0.1:8788`.

Demerzel remains the control and knowledge plane through two deliberately narrow links:

1. `manfred-ears-forward.socket` preserves the old `100.126.233.3:8787` companion endpoint and forwards TCP to `7090`, so the S25 does not need an immediate endpoint change.
2. `manfred-ears-wiki-sync.timer` pulls only validated `wiki-inbox/YYYY-MM-DD.md` scratch exports over the dedicated fleet SSH key. It uses the validated local mirror as rsync's link destination, so unchanged historical exports are not retransferred, then publishes files atomically into Demerzel's existing `~/.hermes/manfred-ears/wiki-inbox/` path. The exact 23:52 run leaves bounded headroom before the 23:55 Hermes Daily Conversation Wiki Ingest; the collector therefore needs no remote filesystem mount and no raw archive access.

The sync rejects symlinks, non-date filenames, oversized/non-UTF-8 files, bad headings, missing scratch-policy markers, and an unexpectedly empty source. A failed pull preserves the last good mirror. Raw audio, SQLite, event JSON, models, and full searchable transcripts are never mirrored by this job.

Tracked deployment artifacts live under `deploy/`:

```text
systemd/manfred-ears-receiver.service   7090 receiver + rolling ASR worker; host comes from env
systemd/manfred-ears-operator.service   7090 loopback authenticated operator API
systemd/manfred-vision-receiver.service 7090 receiver-only Noa compatibility surface
systemd/manfred-chat-mirror-receiver.service 7090 receiver-only ChatGPT observation surface
systemd/manfred-ears.env.example        non-secret 7090 environment template
systemd/manfred-ears-forward.socket     Demerzel legacy endpoint listener
systemd/manfred-ears-forward.service    Demerzel TCP proxy to 7090
systemd/manfred-ears-wiki-sync.service  Demerzel validated/atomic Wiki mirror
systemd/manfred-ears-wiki-sync.timer    five-minute mirror plus exact 23:52 pre-ingest refresh
manfred_wiki_sync.py                    fail-closed rsync/validation/publish implementation
```

The live per-user units are installed under `~/.config/systemd/user/`. `loginctl` linger must be enabled on both hosts so they survive logout and start without an interactive session. The real environment file is `~/.config/manfred-ears/manfred-ears.env`, mode `0600`; never commit it. Audio, vision, Chat Mirror, and operator capabilities use four different high-entropy tokens. The faster-whisper adapter explicitly pins English for multilingual Whisper models. `HF_HOME` stays under the canonical state root so model restore/migration remains self-contained.

On `7090`, run from `ingestion/manfred-ears/` after copying the code, state, and secret environment file:

```bash
set -euo pipefail
systemd-analyze --user verify deploy/systemd/manfred-ears-receiver.service \
  deploy/systemd/manfred-ears-operator.service \
  deploy/systemd/manfred-vision-receiver.service \
  deploy/systemd/manfred-chat-mirror-receiver.service
install -d -m 0700 "$HOME/.config/systemd/user" "$HOME/.config/manfred-ears"
install -m 0644 deploy/systemd/manfred-ears-receiver.service \
  deploy/systemd/manfred-ears-operator.service \
  deploy/systemd/manfred-vision-receiver.service \
  deploy/systemd/manfred-chat-mirror-receiver.service "$HOME/.config/systemd/user/"
test -s "$HOME/.config/manfred-ears/manfred-ears.env"
chmod 0600 "$HOME/.config/manfred-ears/manfred-ears.env"
systemctl --user daemon-reload
systemctl --user enable --now manfred-ears-receiver.service manfred-ears-operator.service
systemctl --user is-active manfred-ears-receiver.service manfred-ears-operator.service
curl http://127.0.0.1:8788/health

# Enable only after MANFRED_VISION_TOKEN is set and a Noa physical test is ready.
systemctl --user enable --now manfred-vision-receiver.service
systemctl --user is-active manfred-vision-receiver.service

# Enable only after MANFRED_CHAT_MIRROR_TOKEN is set and the private APK is ready.
systemctl --user enable --now manfred-chat-mirror-receiver.service
systemctl --user is-active manfred-chat-mirror-receiver.service
```

On Demerzel, disable the former receiver/operator only after the stopped-state final rsync succeeds, then install the forwarder and Wiki mirror:

```bash
set -euo pipefail
systemctl --user disable --now manfred-ears-receiver.service manfred-ears-operator.service
install -m 0644 deploy/systemd/manfred-ears-forward.socket \
  deploy/systemd/manfred-ears-forward.service \
  deploy/systemd/manfred-ears-wiki-sync.service \
  deploy/systemd/manfred-ears-wiki-sync.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now manfred-ears-forward.socket manfred-ears-wiki-sync.timer
systemctl --user start manfred-ears-wiki-sync.service
```

No public ingress, filesystem mount, firewall rule, Tailscale ACL change, or shared SQLite database is part of this architecture. Rollback is explicit: stop the forwarder and 7090 units, rsync the stopped canonical state back to Demerzel, restore the Demerzel environment/state path, and re-enable the original receiver/operator.

Replay a mono PCM16 WAV through the same chunk/session/archive path:

```bash
python3 -m manfred_ears --state-dir /tmp/manfred-ears-validation \
  ingest-wav sample.wav --uid local-validation --chunk-seconds 1
python3 -m manfred_ears --state-dir /tmp/manfred-ears-validation \
  flush SESSION_ID --backend faster-whisper --model large-v3-turbo --device cpu --compute-type int8
python3 -m manfred_ears --state-dir /tmp/manfred-ears-validation \
  search 'Manfred hardware'
```

Benchmark ASR against a reference transcript:

```bash
python3 -m manfred_ears benchmark sample.wav \
  --reference 'exact expected words' \
  --backend faster-whisper --model large-v3-turbo --device cpu --compute-type int8

# In a separate approved NeMo/CUDA environment:
python3 -m manfred_ears benchmark sample.wav \
  --reference 'exact expected words' \
  --backend parakeet --model nvidia/parakeet-unified-en-0.6b --device cuda
```

The report includes duration, peak/RMS/clipping, ASR text and segment metrics, wall latency, real-time factor, WER, and continuous PCM storage projection.

## S25/Omi direct setup

1. Build/install the debug Manfred Companion APK on the S25.
2. Enter `7090`'s tailnet `/audio` endpoint and receiver token. The former Demerzel endpoint remains a compatibility forwarder during migration.
3. Grant Bluetooth and foreground-notification permissions.
4. Select Dev Kit 2 and tap **Start ears**.
5. Speak the fixed 60-second validation script, then stop.
6. Verify sequence continuity, phone spool drain, chunk hashes, capture-vs-receive timestamps, raw files, transcript version, FTS result, and Wiki inbox.

The stock cloud path may still emit one-second requests.[13][14] The direct companion also emits one-second chunks, but adds source session, sequence, and phone sample-clock metadata.

## Fleet placement

Read-only fleet inspection found:

- `3660` is healthy and its single RTX 3060 is the dedicated ComfyUI worker. WSL and Virtual Machine Platform are disabled, so the supported NeMo Linux/CUDA route is not currently available without an approved OS-feature install/reboot and GPU-passthrough validation.
- ComfyUI was idle during inspection, but the installed Comfy Python environment did not contain FastAPI, Uvicorn, faster-whisper, CTranslate2, or Silero VAD.
- A Parakeet ASR service on `3660` requires a separate Python environment, model download, durable launcher, and approved Comfy coexistence/controller behavior. It must not reuse or mutate the Comfy environment.
- `comfy` (dual RTX 3060) was busy with Qwen Builder; `ai` holds Qwen Senior; neither should be reassigned.
- The validated Demerzel CPU environment, model cache, evidence archive, and service units were transferred intact to `7090`, which is now the canonical ingestion/storage/CPU-ASR plane. Demerzel retains only the authenticated transport forwarder and validated daily Wiki scratch mirror.

The backend now includes a lazy NeMo `parakeet` adapter defaulting to `nvidia/parakeet-unified-en-0.6b` on CUDA, but no GPU environment/model was installed. The least disruptive sequence remains: record once through the direct bridge, benchmark the exact archived WAV with CPU faster-whisper and Parakeet in an isolated test environment, then approve placement from evidence.

## Acceptance test

Record the same approximately 30–60 second script in each condition:

1. quiet room at normal pendant distance;
2. workstation fans/keyboard;
3. walking outdoors;
4. classroom/lecture distance;
5. two speakers with turn-taking;
6. proper nouns and technical vocabulary: Demerzel, Manfred, Omi, Brilliant Labs Frame, CSCI 3104, induction invariant.

For every run capture:

- received bytes vs expected duration (`sample_rate × 2 × seconds`);
- missing/duplicate sequence numbers, phone spool retries, and idempotency behavior;
- peak, RMS dBFS, clipping, audible discontinuities;
- ASR model/settings, latency, real-time factor, WER against a hand transcript;
- name/number/technical-term errors;
- speaker boundaries and diarization label stability;
- raw storage growth and derived transcript/event sizes;
- retrieval success for an exact phrase and a paraphrased episodic query.

The v0 gate is not “the API returned 202.” It is: a real Omi recording arrives without manual file movement, raw audio remains verifiably preserved, a versioned local transcript is accurate enough to search, and the daily scratch context points back to the evidence.

## What remains deliberately blocked

- Any public Tailscale Funnel or internet-facing route; the planned path is tailnet-only.
- Installing ASR dependencies/models or a durable service on `3660`.
- Changing Windows firewall rules or ComfyUI controllers.
- Writing to production Supabase/pgvector.
- Automatic raw-audio retention/deletion policy.
- Periodic Frame capture in Manfred Companion, adaptive/coalesced episode boundaries, visual model inference, or automatic Frame output.
- Automatic Wiki promotion, embeddings, or speaker identity.

Those are consequential follow-on steps after the local receiver and real Omi audio quality are verified.

## Sources

[1] https://docs.omi.me/doc/developer/apps/Introduction — Omi Apps Introduction
[2] https://docs.omi.me/doc/developer/apps/AudioStreaming — Omi Real-Time Audio Streaming
[3] https://docs.omi.me/doc/developer/Protocol — Omi App-Device Protocol
[4] https://docs.omi.me/doc/hardware/DevKit2 — Omi DevKit 2
[12] https://github.com/BasedHardware/omi/blob/main/app/lib/providers/developer_mode_provider.dart — Omi developer-mode provider source
[13] https://github.com/BasedHardware/omi/blob/main/backend/utils/webhooks.py — Omi webhook delivery source
[14] https://github.com/BasedHardware/omi/blob/main/backend/routers/pusher.py — Omi audio pusher source
[15] https://github.com/BasedHardware/omi/blob/main/sdks/device/PROTOCOL.md — Omi device protocol source
