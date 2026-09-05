# EyeVue photos in Manfred Companion

Manfred Companion adds a focused Android EyeVue photo component alongside the existing Omi audio bridge, targeting the tested TK8 glasses profile. The pending **0.5.1+9** build makes **Capture and fetch (experimental)** the default. Its build, software tests, and new physical workflow acceptance are pending; these instructions describe the implemented flow, not a confirmed hardware result.

Hardware identity, measured resolutions, firmware boundaries, and protocol evidence are recorded in the [EyeVue E09-family / TK8 hardware profile](hardware/eyevue-e09.md).

## First supervised session

1. Disconnect EyeVue from CyanBridge so only Manfred owns its BLE connection.
2. Open Manfred, choose **Find EyeVue**, select the glasses, and **Connect**. Allow nearby-device permissions.
3. Leave **Capture and fetch (experimental)** selected, tap **Start photo session**, and accept Android's glasses Wi-Fi prompt if it appears.
4. Manfred first joins the glasses AP to baseline the existing album, then ends transfer mode and releases that Wi-Fi connection. Wait for **Ready**: the camera must report idle and a fresh media count must arrive before a new capture can be tracked.
5. Press the glasses shutter or **Take photo**. Manfred waits for photo-busy then idle plus a new count before rejoining Wi-Fi to retrieve new originals. It then finishes without clearing the glasses and closes Wi-Fi again before returning to Ready. Accept another Android Wi-Fi prompt if requested.
6. Verify that one new photograph appears in **DCIM/Manfred**, with its measured dimensions in the panel. Existing album entries are skipped; nothing is deleted from the glasses.
7. Repeat once, recording shutter-to-save delay and checking for duplicates. **Stop photo session** ends the logical capture session and cleans up any active transfer. Stopping Eyes does not stop an active Ears session, and stopping Ears does not stop Eyes.

Only EyeVue HTTP sockets use the glasses Wi-Fi network; the app preserves the normal internet route for Omi uploads. Actual Tailscale and concurrent-audio behavior still requires device validation.

After BLE connection, the new build also makes an optional read-only firmware query. If the device replies, the panel displays **BT**, **ISP**, and **device** versions. A missing reply displays unavailable and does not invalidate a working Bluetooth connection. No current firmware values have yet been measured with this build; the query does not check for or install an update.

## Hardware acceptance remains explicit

The earlier CyanBridge build downloaded originals at 3200x2400. The independently decoded reference original contains a JPEG end marker followed by three zero alignment bytes. Validation accepts up to three zero bytes after the end marker, retains the original bytes, and still checks HTTP completion and Android decoding. Repeated bulk sync downloaded the whole album again. Its BLE shutter preview was 320x180; neither tested opaque image-pull value returned a larger image.

The installed **0.5.0** primary media-AP test did not import a new image from shutter attempts while the AP was active. An offline shutter did produce a count increase of one in about **2.4 seconds**. This is the evidence motivating Capture and fetch; it does not prove that the new automatic capture/reconnect/save cycle works.

Acceptance for **0.5.1 Capture and fetch** is two successive offline captures, each followed by automatic AP retrieval of exactly one new full-resolution JPEG, then return to camera Ready. Record measured dimensions, total shutter-to-save latency, absence of historical duplicates, and working Stop. Source tests and an APK build cannot substitute for this test.

**Keep Wi-Fi open (experimental)** retains one media AP session and polls for new photos after baselining. Concurrent still capture is not an accepted capability on the tested firmware.

**Alternate startup (experimental)** starts the vendor live AP mode and then attempts the same new-photo retrieval. It sends one live command and waits for the asynchronous SSID report, without the duplicate media-start requests found in CyanBridge. It does not start or decode video. A previous live-preview test reached RTSP but CyanBridge's Media3 parser rejected the vendor SDP line `a=decode_buf=300`; no stream dimensions were measured.

## While ChatGPT is visible

Start the Manfred photo session before switching to ChatGPT. Android 16 accepts a Wi-Fi network request from an app with an active foreground service; Eyes keeps its connected-device service lease across the capture/fetch cycle. An exact SSID/security request can reuse remembered approval, but Android can still show its connection dialog when approval is missing or a second Wi-Fi interface is unavailable. This is platform eligibility, not a verified background-capture result. See [Android approval behavior](https://developer.android.com/develop/connectivity/wifi/wifi-bootstrap#bypassing-user-approval) and the [Android 16 Wi-Fi request policy](https://android.googlesource.com/platform/packages/modules/Wifi/+/refs/heads/android16-release/service/java/com/android/server/wifi/WifiNetworkFactory.java).

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

Hardware receipt and Tasker's URI readability have not yet been verified. This app does not automate the ChatGPT interface, start a voice conversation, or upload photographs itself. Attaching the image to the user's already-open ChatGPT Live conversation remains a separate, unverified integration.

## Implementation boundaries

- Flutter MethodChannel `manfred/eyevue` and EventChannel `manfred/eyevue/events`.
- Kotlin source and pure tests are checked-in Android overlays, copied by the materializer.
- The source validator runs Flutter analysis/tests, native unit tests, an arm64 APK build, and pinned stable-signature verification.
- No receiver/server schema, archive, fleet scheduling, or microphone settings change.
- Chat Mirror remains a separate draft PR. Its later integration should register its channel additively in MainActivity.

See native `SOURCE_PROVENANCE.md` for protocol source provenance. The tested app version and physical results must be recorded separately from software test results.
