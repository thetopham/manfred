# Manfred Companion — direct S25 wearable bridge

Manfred Companion is the mobile connector between **Omi Dev Kit 2**, the Samsung Galaxy S25, and Demerzel. It continues the user-owned data-spine idea from [`thetopham/exocortex`](https://github.com/thetopham/exocortex), and implementation now belongs to the private `thetopham/manfred` repository alongside Ears and existing vision services. Eyes through CyanBridge are planned; this extraction adds no new device functionality.

The predecessor's durable ideas are preserved:

- one local-first timeline/evidence spine;
- stable, boring connector contracts;
- source time separated from ingestion time;
- cloud systems as optional inputs rather than data owners;
- AI/search layers derived from evidence rather than replacing it.

## Planned architecture

```text
Omi Dev Kit 2
    ↓ BLE notifications (Opus or PCM + 3-byte framing)
Manfred Companion on Galaxy S25
    ↓ decode to PCM16LE / 16 kHz / mono
    ↓ one-second evidence chunks + phone sample-clock timestamps
    ↓ durable on-phone spool and retry
Tailscale (no Omi cloud, no Funnel)
    ↓
Manfred Ears receiver on Demerzel
    ↓ raw archive + versioned transcript + FTS + daily Wiki context
    ↓
Parakeet Unified EN 0.6B or faster-whisper
```

The older Brilliant Labs Frame remains a later output peripheral. Flutter is intentional because the S25 companion owns durable wearable evidence and can add EyeVue/Frame adapters without moving canonical memory into a vendor app.

## ChatGPT Conversation Mirror MVP

The initial realtime cognition interface remains the actual ChatGPT Android Live app. Manfred Companion does not inspect authentication or call private ChatGPT APIs. Its Android overlay declares one Accessibility service whose resource-level `packageNames` filter and runtime guard both restrict observation to `com.openai.chatgpt`; the service performs no clicks, typing, gestures, or generic phone automation.

The first mirror slice is:

```text
visible ChatGPT UI/history text
→ bounded native Accessibility snapshot
→ AtomicFile commit under app-private storage
→ Flutter import into a payload+metadata spool
→ SHA-256 recheck immediately before upload
→ tailnet-only POST /chat-mirror with a distinct ingest token
```

Each snapshot says exactly what it is: an `android-accessibility` observation with `partial`, `complete`, or `gap` completeness and `provisional` or `final` state. Password-bearing windows are not captured; they produce a gap marker. The event package and active root package must both be ChatGPT. Identical consecutive snapshots are deduplicated locally, Android app backup is disabled, and native files are deleted only after the Flutter spool commits them. The server still enforces hash-sensitive idempotency and unique `(mirror session, sequence)` evidence.

The app exposes Chat Mirror endpoint/token settings, explicit saved-credential clearing, Accessibility settings, manual sync, queued-event count, and explicit native-plus-spool chat deletion. A 15-second foreground polling loop imports and drains while the Flutter process is alive. Process-kill wakeup and exact ChatGPT conversation-ID extraction remain later reliability work.

No physical claim is made yet about ChatGPT's real Accessibility tree, Live transcript completeness, session-boundary quality, EyeVue image attachment, or simultaneous custom-BLE Opus plus HFP audio.

## Implemented vertical slice

The app source currently provides:

- Omi GATT UUIDs and codec IDs from the published protocol;
- scan, select, connect, service discovery, codec read, audio notification subscription, and reconnect attempts using `flutter_blue_plus`; concurrent setup callers share one attempt, and setup failure explicitly disconnects before retry;
- generation-tagged BLE callbacks and a single serialized packet/decoder queue: reconnect drains already-queued old-generation work before decoder replacement and rejects any later stale callback instead of decoding it against fresh Opus state;
- invalidatable setup leases checked after every asynchronous BLE setup boundary, generation-specific recovery, single-flight connect/setup operations, and `onValueReceived` capture so stale setup work or a cached characteristic value cannot install a new stream;
- strict three-byte Omi packet-header removal for decoding, with an optional supervised-validation archive that retains every complete notification, the opaque header bytes, phone packet counter, hashes, receive time, disposition, codec, and decoder-generation events as flushed JSONL on the phone; runtime diagnostics-write failures are counted and never abort decoder transitions or PCM spooling;
- PCM16 pass-through and Opus decoding through Android `opus_codec_android` plus `opus_codec_dart`;
- explicit refusal to guess PCM8 signedness;
- one-second PCM16 chunking at 16 kHz mono without mixing decoder generations, including peak, RMS, exact full-scale sample count, and DC-offset metadata in the durable phone spool;
- capture session UUID, sequence number, and sample-clock-derived start/end times;
- atomic on-phone PCM + JSON spool before network upload, with one-time startup-only JSON-as-commit-marker reconciliation after a process kill between PCM and metadata rename; normal capture/drain scans never reconcile active temporary files;
- non-blocking spool drain plus ten-second request timeouts, with concurrent drain callers joining the same future; start and stop never wait for network delivery, stop commits the final partial chunk locally before scheduling a fresh tail pass, and opening the app or tapping **Upload queued audio** retries retained evidence without starting capture;
- retry without deleting evidence until HTTP 202 is received;
- direct upload metadata:
  - `Idempotency-Key`
  - `X-Manfred-Content-SHA256`
  - `X-Manfred-Capture-Session`
  - `X-Manfred-Sequence`
  - `X-Manfred-Capture-Started-At`
  - `X-Manfred-Capture-Ended-At`
  - `X-Manfred-Codec: pcm16le`
  - `X-Manfred-Transport: s25-direct-ble-tailscale`
  - `X-Manfred-Decoder-Generation`
- immediate pre-upload SHA-256 recomputation; changed spool bytes fail closed locally, while Manfred Ears validates both the declared digest and the digest embedded in new idempotency keys on first ingest;
- endpoint policy that permits HTTP only to Tailscale `100.64.0.0/10`, or HTTPS to a tailnet IP/`.ts.net` name;
- receiver token in Android secure storage and `X-Manfred-Token` upload headers rather than URL query parameters;
- SHA-256 pseudonymization of the BLE device ID before it enters request URLs/access logs;
- Android `connectedDevice` foreground-service declaration;
- explicit start, stop, delete-queued-audio, and delete-validation-packets controls;
- visible BLE/codec/packet/PCM/queue/upload/error state;
- ChatGPT-package-only Accessibility observation with password-window gap markers and no UI actions;
- native AtomicFile retention, Flutter payload+metadata Chat Mirror spool, and import-before-delete ordering;
- a separate tailnet `/chat-mirror` endpoint/token policy with pre-upload SHA-256 validation;
- visible Chat Mirror Accessibility, receiver, queue, sync, and delete controls.

Stop is an evidence-finalization boundary, not a network boundary. It disconnects BLE, drains already accepted packet work, commits the final PCM fragment, and ends the foreground capture service before returning. Upload retries continue independently from the durable spool, so an unreachable receiver cannot pin the Stop button behind one or more ten-second request timeouts. Live queue counts are revision-fenced and no longer rescan the complete spool after every BLE packet, preventing a large offline backlog from turning packet shutdown into quadratic directory work.

The foreground service keeps the process elevated while the main Flutter isolate owns BLE and upload state. Android documents either a `connectedDevice` foreground service or Companion Device APIs for long-lived BLE notification listening; Companion Device presence/restart support is a later reliability layer, not required for the first supervised recording.

## Omi protocol contract

Current Omi source documents:

| Item | Value |
|---|---|
| Service | `19b10000-e8f2-537e-4f6c-d104768a1214` |
| Audio notify | `19b10001-e8f2-537e-4f6c-d104768a1214` |
| Codec read | `19b10002-e8f2-537e-4f6c-d104768a1214` |
| PCM16 codec ID | `0` |
| PCM8 codec ID | `1` |
| Dev Kit Opus 10 ms | `20` / `0x14` |
| Opus FS320 20 ms | `21` / `0x15` |
| Wire framing | three-byte header, then codec payload |
| Decoded output | PCM16 little-endian, mono, 16 kHz |

The three header bytes are deliberately treated as opaque framing because the published shared protocol only guarantees their size. Sequence and capture metadata are minted by the S25 bridge rather than inferred from undocumented bits. Supervised validation preserves the bytes so documented sequence/checksum semantics can be evaluated later without inventing them now.

## Capture-time honesty

The direct bridge is substantially better than an Omi-cloud relay, but the Dev Kit notification contract does not publish a hardware capture timestamp. Manfred Companion therefore anchors the capture session to the S25 receipt time of its first decoded samples and advances every later boundary from the total emitted PCM sample count. It never re-anchors to later BLE arrival times, including when a one-second emission leaves the PCM buffer exactly empty.

Manfred Ears records this as:

```text
capture_time_quality = phone-derived-capture-time
timestamp_basis = s25_pcm_sample_clock
```

It is not labeled as an Omi hardware clock. Future firmware/header evidence can strengthen this without changing the upload schema.

## Build and test

This host does not have a system Flutter/Android toolchain. The repository therefore keeps portable Flutter source plus Android policy overlays. On a Flutter 3.44+ development machine with an Android SDK:

```bash
python scripts/validate_android_local.py \
  --output ~/artifacts/manfred-companion/app-debug-ephemeral.apk
```

That command performs materialization, analysis, tests, an arm64 debug APK build, and copies the artifact out of its temporary build tree. An ephemeral artifact is **not** safe to install over the stable-signed S25 build.

For an installable update, supply `MANFRED_ANDROID_KEYSTORE_BASE64`, `MANFRED_ANDROID_KEYSTORE_PASSWORD`, `MANFRED_ANDROID_KEY_PASSWORD`, and `MANFRED_ANDROID_KEY_ALIAS`, then require stable signing explicitly:

```bash
python scripts/validate_android_local.py \
  --require-stable-signing \
  --output ~/artifacts/manfred-companion/app-debug.apk
```

The runner re-signs with Manfred Companion's stable private testing signature and verifies the pinned signer certificate before retaining the APK. This is not Play/release signing, but stable artifacts update in place without clearing the saved endpoint, token, device selection, or spool data.

> **One-time transition:** APKs produced before the stable signer was introduced used disposable GitHub-runner debug keys. Android cannot update those builds in place. Uninstall the old build once before installing the first stable-signed artifact; this clears its app data, so recover the receiver endpoint/token before uninstalling. Every stable-signed artifact after that can update in place and retain settings.

## First-device procedure

After the app PR is merged and its APK is installed on the S25:

1. Start the Manfred Ears receiver on Demerzel's tailnet address, with a high-entropy receiver token.
2. Enter an endpoint such as `http://100.x.y.z:8787/audio` and save the token.
3. Grant Bluetooth scan/connect and notification permissions.
4. Select the Dev Kit 2 through **Find Omi**.
5. For an acoustic/BLE validation run, enable **Supervised raw-packet validation** in settings. Leave it disabled for ordinary capture because every BLE notification is synchronously retained.
6. Tap **Start ears** and read the fixed 60-second script.
7. Tap **Stop**; any failed uploads remain in the local spool and validation packets remain until explicit deletion.
8. Verify server sequence continuity, PCM hashes, decoder generations, per-chunk waveform metrics, capture-vs-receive timestamps, transcript, FTS retrieval, and Wiki scratch context.
9. Run the exact archived WAV through faster-whisper and Parakeet; compare WER, proper nouns, latency, and timestamps before selecting the default ASR or GPU placement.

## Parakeet boundary

Manfred Ears now has a lazy `parakeet` backend using NVIDIA NeMo and defaults that backend to `nvidia/parakeet-unified-en-0.6b` on CUDA. The adapter preserves returned segment timestamps while leaving confidence honestly null because decoder scores are not calibrated probability.

`requirements-parakeet.txt` is intentionally separate from the small receiver environment. This change does **not** install NeMo, download the model, mutate ComfyUI, start a service, or reserve the single RTX 3060. Read-only inspection found `3660` healthy/idle but Windows Subsystem for Linux and Virtual Machine Platform are both disabled. Because NeMo's supported path is Linux/CUDA, deploying Unified Parakeet there would first require an approved WSL2/GPU-passthrough installation and likely reboot, or a separately validated native runtime. Do not force NeMo into the Comfy Python environment. Faster-whisper remains the comparison/fallback backend.

## Physical validation

The direct Omi Dev Kit 2 → Galaxy S25 → Demerzel path completed a 322.48-second hardware run on 2026-08-22 with 323 consecutive one-second chunks (sequence 0–322), zero missing chunks, Opus 10 ms BLE input, PCM16LE output, durable phone spooling, Tailscale upload, raw archival, and faster-whisper transcription. The Omi LED/packet-flow behavior and absence of `RECORD_AUDIO` confirm the source is the Omi microphone rather than the S25 microphone.

The generation fence, optional raw-notification archive, quality metadata, and stronger hash handshake were added after that receipt. Their software contracts are unit/CI verified, but their physical BLE-disconnect acceptance remains a required supervised rerun; the earlier capture is not retroactively claimed to contain packet-level evidence it did not retain.

The UI counters are scoped to the active capture session; queued-local remains an all-capture spool value so offline backlog stays visible.

## Known gaps before all-day capture

- Android process-kill recovery via `CompanionDeviceService` is not implemented.
- Offline Omi onboard-storage recovery is not implemented.
- The phone spool has no retention/size cap yet; deletion is explicit during validation.
- Raw notification validation archives are phone-local, intentionally opt-in, and have no automatic export or retention policy yet. Their per-packet durable flush can perturb timing, so they remain a bounded supervised-test feature rather than an all-day mode.
- CI APKs use a stable private testing signature, not a production Play/release-signing setup.
- Device timestamps are phone-derived sample-clock estimates, not firmware timestamps.
- No Parakeet model or dependency has been installed on a GPU node.
- No Frame output path is included in this milestone.

These gaps do not block one supervised 60-second direct-tailnet recording. They do block calling the app an all-day reliable recorder.

## Sources

- https://github.com/thetopham/exocortex
- https://github.com/BasedHardware/omi/blob/main/sdks/device/PROTOCOL.md
- https://github.com/BasedHardware/omi/blob/main/app/lib/services/devices/connectors/omi_connection.dart
- https://developer.android.com/develop/connectivity/bluetooth/ble/background
- https://developer.android.com/about/versions/14/changes/fgs-types-required
- https://pub.dev/packages/flutter_blue_plus
- https://pub.dev/packages/flutter_foreground_task
- https://pub.dev/packages/opus_codec
- https://huggingface.co/nvidia/parakeet-unified-en-0.6b
- https://docs.nvidia.com/nemo-framework/user-guide/latest/nemotoolkit/asr/results.html
