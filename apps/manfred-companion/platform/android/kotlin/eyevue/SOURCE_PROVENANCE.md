# EyeVue source provenance
Protocol, GATT, HTTP cancellation/routing helper and tracker were ported from:
https://github.com/thetopham/Alternative-HeyCyan-App-and-SDK
Source checkout: .worktrees/eyevue-ble-resolution-probe
Base revision: c6c3ed3d23ed59cfadb69e4ba42149a2f6213f7b
The source checkout additionally contained local HTTP cancellation/routing and photo tracker changes; exact input hashes follow. No license declaration was present in the inspected source root, so no license or authorship claim is invented here. Preserve this provenance alongside the project-level licensing review.

EyevueProtocol.kt source SHA256 28280f2e938243eb4b20dbee7e652827a4cf77a6f273f70220c924283d16f475
EyevueGattClient.kt source SHA256 4be58f598980711d0240d42bd316770004e77d7b23398730f02c50c5f1cacd54
EyevueMediaHttpClient.kt source SHA256 8b9997685a2a8ef6affdf1820a582bc54f1c6b79a418e4b8ccccefeaa8320836
EyevueNewPhotoTracker.kt source SHA256 bd6611bfb5a70f22519815ec042227b320dc93f01b8b484a7170f1e628cae8ed

Manfred adaptations: package rename; stale GATT callback guards; no image byte previews in logs; bounded HTTP call lifetime. The plugin, AP-only transport and photo session integration are new Manfred code.

Ported test inputs:
EyevueProtocolTest.kt source SHA256 127a8b56b1738686eb27bf2a65c4ad0971f11944eb98ebabefbef0861d1f6c5a
EyevueNewPhotoTrackerTest.kt source SHA256 3ba00dea4efc40732713c3fbe61c6f54e87ccdc3a5c02666df16bcebd51186cc
EyevueMediaHttpClientTest.kt source SHA256 9a8a0047dbb76db284d7fe8f7a947015220739cb7267dd920ca729cc91da8f37

## BLE preview session adaptation (0.5.4)

The app-button BLE capture sequence is independently pinned to clean CyanBridge revision
`66584218b3462c80fe42cb5d9cc45213aa6fe1ce`, file
`android/CyanBridge/app/src/main/java/com/fersaiyan/cyanbridge/devices/eyevue/EyevueManager.kt`,
`capturePhotoForAi` (lines 139–151): subscribe to `photoResults` with an undispatched
coroutine before sending `buildPhotoPacket(highQuality = true)`, then await one result
within twelve seconds. That misleading protocol boolean emits 0x22 with payload 0x31;
it takes a new AI-photo exposure, measured 320x180, rather than returning a stored original.
[Repair PR4](https://github.com/thetopham/Alternative-HeyCyan-App-and-SDK/pull/4)
records three successful previews at 3399/3514/3576 ms. This is CyanBridge evidence;
Manfred's new BLE mode and combined Tasker latency require their own device test.

Manfred reuses its already-ported AA15 assembler unchanged. The new capture helper adds
single-flight ownership, resets incomplete transfer state at owned boundaries, and drains
an accepted bounded GATT write before Stop releases ownership. It does not change Bluetooth
audio routing. Both transports share the former photo-session validation/MediaStore/cache
commit code through `EyevuePhotoStore`; the filename, actual dimensions, hash, URI and ready
intent remain the same, with additive `imageSource` metadata.

The ordinary physical shutter does not emit an AA15 image on the observed firmware.
BLE mode therefore listens for app capture requests only and never turns its own 0x22
acknowledgement or photo-completion event into another exposure. Physical-shutter original
retrieval remains part of the Wi-Fi session. No opaque image-pull, firmware, or camera-setting
commands are introduced.
