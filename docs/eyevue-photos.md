# EyeVue photos in Manfred Companion

Manfred Companion adds a focused Android EyeVue photo component alongside the existing Omi audio bridge, targeting the tested TK8 glasses profile. Installed **0.5.3+11** defaults to **Capture and fetch (experimental)** and adds optional **Improve Wi-Fi discovery**. Source [`d513944`](https://github.com/thetopham/manfred/commit/d513944fabe8cfcdfe2c1f4db3417b0c122c8adb) passed **58 Flutter and 99 native tests**; one supervised baseline join improved to **7.712 seconds**, but the following photo rejoin failed. Reliable faster photo delivery remains **unverified**.

The measured captures below used **0.5.2+10**, source [`212f98b`](https://github.com/thetopham/manfred/commit/212f98ba4f5f4d8ae110c034f2dca6f7965f9d73), which passed 49 Flutter and 84 native tests with stable signing verified in [Manfred run 33992980630](https://github.com/thetopham/manfred/actions/runs/33992980630). Two supervised cycles saved new **3200x2400** originals without historical duplicates and returned to Ready. Capture-event-to-save times were **36.650 seconds** and **29.781 seconds**, dominated by Wi-Fi discovery; immediate delivery and background operation remain unverified.

Hardware identity, measured resolutions, firmware boundaries, and protocol evidence are recorded in the [EyeVue E09-family / TK8 hardware profile](hardware/eyevue-e09.md).

## First supervised session

1. Charge the glasses above 30% for the supervised test. The vendor app blocks Wi-Fi import and live preview below 20%; Manfred 0.5.2 follows that startup policy when battery is known. This is not a verified firmware threshold for taking an offline photo. Disconnect EyeVue from CyanBridge so only Manfred owns its BLE connection.
2. Open Manfred, choose **Find EyeVue**, select the glasses, and **Connect**. Allow nearby-device permissions.
3. Leave **Capture and fetch (experimental)** selected, tap **Start photo session**, and accept Android's glasses Wi-Fi prompt if it appears.
4. Manfred first joins the glasses AP to baseline the existing album, then ends transfer mode and releases that Wi-Fi connection. Wait for **Ready**: the camera must report idle and a fresh media count must arrive before a new capture can be tracked.
5. Press the glasses shutter or **Take photo**. Manfred waits for photo-busy then idle plus a new count before rejoining Wi-Fi to retrieve new originals. It then finishes without clearing the glasses and closes Wi-Fi again before returning to Ready. Accept another Android Wi-Fi prompt if requested.
6. Verify that one new photograph appears in **DCIM/Manfred**, with its measured dimensions in the panel. Existing album entries are skipped; nothing is deleted from the glasses.
7. Repeat once, recording shutter-to-save delay and checking for duplicates. **Stop photo session** ends the logical capture session and cleans up any active transfer. Stopping Eyes does not stop an active Ears session, and stopping Ears does not stop Eyes.

Only EyeVue HTTP sockets use the glasses Wi-Fi Network; the app does not bind all traffic to it. The tested S25 uses its primary Wi-Fi interface for the glasses; earlier controlled cycles returned to home Wi-Fi after release. That return is not guaranteed when saved home-network auto reconnect is off, and socket routing alone does not preserve the home connection. A later ChatGPT attachment failed on validated cellular internet, then succeeded after home Wi-Fi was restored and the exact image was retried manually. See the [network finding and limits](hardware/eyevue-e09.md#why-home-wi-fi-disconnects). This does not explain glasses AP invisibility; automatic end-to-end delivery and concurrent-audio behavior still require device validation.

After BLE connection, optional read-only queries display firmware versions and (from 0.5.2) battery percentage/charging state. Missing replies remain unavailable and do not invalidate a working Bluetooth connection. Version 0.5.1 read **BT 1.1.9 / ISP 3.3.7 / device 2** from this TK8/0201 unit. The vendor catalog offered neither a BLE nor Wi-Fi package for the actual BT version; this does not independently establish the latest ISP version. No firmware was installed or modified.

## Hardware acceptance remains explicit

The earlier CyanBridge build downloaded originals at 3200x2400. The independently decoded reference original contains a JPEG end marker followed by three zero alignment bytes. Validation accepts up to three zero bytes after the end marker, retains the original bytes, and still checks HTTP completion and Android decoding. Repeated bulk sync downloaded the whole album again. Its BLE shutter preview was 320x180; neither tested opaque image-pull value returned a larger image.

The earlier **0.5.0** primary media-AP test was at **27% battery**. Stop was issued only **3.673 seconds after the shutter request**, confounding the lack of a new image; that run cannot establish a capture restriction. An offline shutter did produce a count increase of one in about **2.4 seconds**.

The later **0.5.1 Capture and fetch** session joined Wi-Fi, read the baseline, finished transfer, then received photo-idle status and a fresh media count before Ready. The user reported an error sound on Take photo; no photo-busy/count response or saved JPEG followed, and the 15-second completion deadline expired. Battery push frames reported a decline from 16% to **13%**, not charging. The 13% reading belongs to this later test. Its logs omit outbound write acknowledgements, so AA14 silence alone cannot establish whether the shutter command reached firmware. Low power remains a plausible cause of that failure.

### Verified 0.5.2 capture-and-fetch cycles

Two new full-resolution JPEGs were saved and independently verified at **3200x2400**. Each cycle returned to **Ready**, and neither imported historical album duplicates. Device-log times on 2026-09-05 were:

| Trigger | Capture event to saved | Total delay | Wi-Fi rejoin | Download/save | JPEG bytes |
| --- | --- | --- | --- | --- | --- |
| App shutter request | 15:45:49.997 to 15:46:26.647 | 36.650 s | 32.708 s | 498 ms | 354704 |
| Received glasses capture event | 15:48:00.325 to 15:48:30.106 | 29.781 s | 26.209 s | 257 ms | 263400 |

The glasses timing begins at its received capture event. Android bypassed the remembered approval dialog. Retained scans now show two scans separated by **10.016-17.518 seconds** before connection initiation; availability followed initiation in **0.948-1.779 seconds**. Download/save took under half a second. Later tests distinguish missing AP discovery from one authentication failure after an immediately reused cached result; see the [reconnect latency investigation](hardware/eyevue-e09.md#reconnect-latency-investigation-052-2026-09-05).

These measurements verify the two supervised capture/reconnect/save cycles. Later Tasker receipt, automatic controlled-replay attachment and completed-receipt deduplication checks are recorded in the [integration acceptance table](../integrations/tasker/README.md#current-device-acceptance). Background capture, VPN and concurrent-audio behavior, and fully automatic native-capture delivery remain unverified.

### Charged keep-Wi-Fi-open test

**Keep Wi-Fi open (experimental)** retains one media AP session and polls for new photos after baselining. With the glasses **41% charged and unplugged**, an app shutter request at **16:00:19.709** received Android GATT write completion with **status 0 at 16:00:19.730**. No `0x22` response, photo-busy event, new media count, or JPEG followed for **at least 47 seconds**. The user confirmed another error beep during this test. GATT write completion does not establish that firmware accepted the requested camera operation.

This charged result supports a capture restriction while the tested firmware is in **media-import mode**. It does not establish a universal inability to use the camera and Wi-Fi together; live-AP/video mode remains a separate capability question. Concurrent still capture in the media AP mode is not an accepted capability on this unit. The test session was stopped successfully, and the temporary Android keep-awake setting was restored to **0**.

**Alternate startup (experimental)** starts the vendor live AP mode and then attempts the same new-photo retrieval. It sends one live command and waits for the asynchronous SSID report, without the duplicate media-start requests found in CyanBridge. It does not start or decode video. A previous live-preview test reached RTSP but CyanBridge's Media3 parser rejected the vendor SDP line `a=decode_buf=300`; no stream dimensions were measured.

## Optional discovery experiment in 0.5.3

**Improve Wi-Fi discovery** enables app-visible fresh scans when Precise location permission and Android Location services are available. At most two scans run within 16 seconds; a fresh matching result supplies the observed AP address/frequency. Missing permission, rejected scans or no timely match fall back to standard transfer within the shared connection deadline. One supervised baseline retry joined in **7.712 seconds from AP-phase request** (7.122 seconds on the narrower discovery timer), but the initial baseline, subsequent photo retrieval and three later baselines through 18:25 failed. The later failures persisted at reported battery levels up to 90%. A final baseline again took **31.600 s on the discovery/Android-join timer**, and the subsequent physical capture was blocked by another post-capture discovery failure. The logs do not establish a firmware cause. **Reliable faster photo delivery remains unverified.** See the [measured results and acceptance limits](hardware/eyevue-e09.md#installed-053-discovery-experiment).

## While ChatGPT is visible

Start the Manfred photo session before switching to ChatGPT. Android 16 accepts a Wi-Fi network request from an app with an active foreground service; Eyes keeps its connected-device service lease across the capture/fetch cycle. An exact SSID/security request can reuse remembered approval, but Android can still show its connection dialog when approval is missing or a second Wi-Fi interface is unavailable. This is platform eligibility, not a verified background-capture result. See [Android approval behavior](https://developer.android.com/develop/connectivity/wifi/wifi-bootstrap#bypassing-user-approval) and the [Android 16 Wi-Fi request policy](https://android.googlesource.com/platform/packages/modules/Wifi/+/14c1216a43e60884e189a66d93d7c179a86ca4f5/service/java/com/android/server/wifi/WifiNetworkFactory.java).

Hardware acceptance must include two physical-shutter captures with ChatGPT foreground, return to Ready, readable full-resolution saves, and any connection prompts or voice/internet interruption. Repeat with the intended home Wi-Fi/VPN setup. Ordinary app switching does not explicitly stop the session; destruction of Manfred's Activity/Flutter engine currently disposes EyeVue, so process/activity recreation is not a durable recovery mechanism yet.

## Tasker handoff

After a successful complete local JPEG save, the native component emits an `imageReady` event to Flutter and a package-scoped `com.thetopham.manfred_companion.EYEVUE_IMAGE_READY` broadcast to `net.dinglisch.android.taskerm`. The event carries a gallery URI and integrity/dimension/session metadata. Only Tasker receives the granted URI access. A missing Tasker installation does not discard a saved image.

Create **Profiles > + > Event > System > Intent Received** with:

- **Action:** `com.thetopham.manfred_companion.EYEVUE_IMAGE_READY`
- Both **Cat** fields: **None**
- **Scheme** and **MIME Type**: blank
- **Priority:** Normal; **Stop Event:** off

Tasker lowercases extra names and prefixes names shorter than three characters with `var_`. These are task-local variables; camelCase does not become snake_case. See the [official Tasker intent documentation](https://tasker.joaoapps.com/userguide/en/intents.html).

| Native extra | Tasker variable |
| --- | --- |
| `id` | `%var_id` |
| `sessionId` | `%sessionid` |
| `uri` | `%uri` |
| `fileName` | `%filename` |
| `width`, `height`, `bytes`, `sha256` | `%width`, `%height`, `%bytes`, `%sha256` |
| `detectedAt`, `receivedAt` | `%detectedat`, `%receivedat` |
| `captureTimestampSource` | `%capturetimestampsource` |

Use **`%uri`** for the image. The supplied `cachePath` maps to `%cachepath`, but that file is private to Manfred and is for its own thumbnail. The broadcast does not set Intent data, so `%intent_data` is absent. Unknown `capturedAt` is null and omitted from the broadcast; `%capturedat` is therefore absent. Detection and receipt timestamps are epoch milliseconds, not verified physical shutter times.

For an initial receipt-only test, attach an **Alert > Flash** task displaying `%filename | %width x %height | %bytes bytes | %var_id`. Enable the profile before taking a new photo after Manfred reports Ready. A manual task run has no broadcast extras. A separate local check using Android's `ContentResolver.openInputStream(Uri.parse(%uri))` can verify URI readability without opening another app or uploading the photo; receiving metadata alone does not prove file access.

The separate [Tasker integration](../integrations/tasker/README.md) has now passed two supervised automatic replays of saved originals: exact-file selection and native Send produced the image and a new reply while Live End remained visible. Their **3.312 s and 3.777 s** timings cover only the Tasker attachment stage through UI confirmation, excluding preparation, receipt, shutter, AP discovery and transfer. The second returned `send_confirmed` and advanced its queue item to **SENT automatically**. Repeating that completed receipt returned `duplicate` with an unchanged saved UI-worker result and no additional execution recorded.

The completion switch was enabled only on the verified test phone; it stays unset by default on another installation. These results verify controlled-replay UI delivery and queue completion. In the final physical attempt, the glasses shutter completed in **2.449 s**, but two fresh scans found no AP match and the fallback failed before transfer: **no new original was saved and no native Tasker receipt arrived**. The latest receipt remained the earlier controlled duplicate. **The fresh capture → receipt → ChatGPT chain was attempted but remains blocked at Wi-Fi discovery; repeated physical delivery and uninterrupted voice remain unverified.** The Manfred APK remains 0.5.3+11. See the [final physical-test timeline](hardware/eyevue-e09.md#final-physical-capture-attempt-053).

## Implementation boundaries

- Flutter MethodChannel `manfred/eyevue` and EventChannel `manfred/eyevue/events`.
- Kotlin source and pure tests are checked-in Android overlays, copied by the materializer.
- The source validator runs Flutter analysis/tests, native unit tests, an arm64 APK build, and pinned stable-signature verification.
- No receiver/server schema, archive, fleet scheduling, or microphone settings change.
- Chat Mirror remains a separate draft PR. Its later integration should register its channel additively in MainActivity.

See native `SOURCE_PROVENANCE.md` for protocol source provenance. The tested app version and physical results must be recorded separately from software test results.
