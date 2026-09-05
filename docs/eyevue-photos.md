# EyeVue photos in Manfred Companion

Manfred 0.5 adds a focused Android EyeVue photo component alongside the existing Omi audio bridge. It targets the tested TK8 glasses profile. It does not bring over CyanBridge's other device integrations, assistant, microphone capture, models, or video player.

## First supervised session

1. Disconnect EyeVue from CyanBridge so only Manfred owns its BLE connection.
2. Open Manfred, choose **Find EyeVue**, select the glasses, and **Connect**. Allow nearby-device permissions.
3. Leave **Photo session** selected, tap **Start photo session**, and accept Android's glasses Wi-Fi prompt if it appears.
4. Wait for Ready. Take a photo with the glasses button or **Take photo** in Manfred.
5. New photographs are saved in **DCIM/Manfred** and the latest image appears with its measured dimensions. Existing photographs are baselined and skipped when the session starts; nothing is deleted from the glasses.
6. **Stop photo session** ends the glasses Wi-Fi session. Stopping Eyes does not stop an active Ears session, and stopping Ears does not stop Eyes.

The phone retains its normal internet route for Omi uploads; only EyeVue HTTP sockets use the glasses Wi-Fi network. Actual Tailscale and concurrent-audio behavior still requires device validation.

## Hardware acceptance remains explicit

The earlier CyanBridge build downloaded originals at 3200x2400. Repeated bulk sync downloaded the whole album again. Its BLE shutter preview was 320x180; neither tested opaque image-pull value returned a larger image.

This implementation holds one AP session open, polls for new photos, and waits for a stable manifest entry before importing it. Whether TK8 firmware will capture and expose new originals while the AP remains active must be measured on the glasses. The acceptance test is two new full-resolution photos imported once each without reconnecting Wi-Fi, plus a measured shutter-to-save delay.

**Alternate startup (experimental)** starts the vendor live AP mode and then attempts the same new-photo retrieval. It sends one live command and waits for the asynchronous SSID report, without the duplicate media-start requests found in CyanBridge. It does not start or decode video. A previous live-preview test reached RTSP but CyanBridge's Media3 parser rejected the vendor SDP line `a=decode_buf=300`; no stream dimensions were measured.

## Tasker handoff

After a successful complete local JPEG save, the native component emits an `imageReady` event to Flutter and a package-scoped `com.thetopham.manfred_companion.EYEVUE_IMAGE_READY` broadcast to `net.dinglisch.android.taskerm`. The event carries a gallery URI and integrity/dimension/session metadata. Only Tasker receives the granted URI access. A missing Tasker installation does not discard a saved image.

Configure a Tasker **Intent Received** profile for that action to perform a later handoff. This app does not automate the ChatGPT interface, start a voice conversation, or upload photographs itself. Attaching the image to the user's already-open ChatGPT Live conversation remains a separate integration and has not been verified by these changes.

## Implementation boundaries

- Flutter MethodChannel `manfred/eyevue` and EventChannel `manfred/eyevue/events`.
- Kotlin source and pure tests are checked-in Android overlays, copied by the materializer.
- The source validator runs Flutter analysis/tests, native unit tests, an arm64 APK build, and pinned stable-signature verification.
- No receiver/server schema, archive, fleet scheduling, or microphone settings change.
- Chat Mirror remains a separate draft PR. Its later integration should register its channel additively in MainActivity.

See native `SOURCE_PROVENANCE.md` for protocol source provenance. The tested app version and physical results must be recorded separately from software test results.
