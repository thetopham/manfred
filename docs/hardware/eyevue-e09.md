# EyeVue E09-family hardware profile: tested TK8 / 0201

Engineering evidence recorded **2026-09-05**. This profile supports Manfred Companion's photo path; it is not a manufacturer specification sheet. Software reference: Manfred commit `84bf29f944374a81b715d671bc1cb18bcad60483` (integrated capture-and-fetch and read-only firmware support), with Capture and fetch selected by default in 0.5.1+9. The installed 0.5.1 build has now read the firmware versions below; full capture-and-fetch hardware acceptance remains in progress. See the [photo workflow and acceptance guide](../eyevue-photos.md).

## Identity and measured capabilities

| Item | Established evidence |
| --- | --- |
| Family name | The reverse-engineering project calls the family EyeVue / E09 and similar OEM variants. This does not establish the tested unit's exact retail SKU or PCB revision. |
| Device protocol identity | The tested glasses report project **TK8**, customer **0201** through BLE command `0x64`. These are profile identifiers, not a unique serial number or installed firmware version. |
| BLE image | A transferred JPEG decoded at **320 x 180**. This is the measured preview path, not the camera sensor's native resolution. |
| Wi-Fi original | A downloaded JPEG independently decoded at **3200 x 2400** (7.68 million output pixels). This does not prove the sensor model or whether firmware interpolates the image. |
| JPEG framing | The verified original ended with JPEG EOI plus three zero alignment bytes. Manfred preserves the bytes and permits up to three zero bytes after EOI while checking completed HTTP transfer and successful Android decoding. |
| Live video | Vendor software exposes an H.264 RTSP endpoint. Our test reached RTSP but did not decode video; resolution and frame rate remain unknown. |
| Installed firmware | The supervised Manfred **0.5.1** read on **2026-09-05** displayed **BT 1.1.9**, **ISP 3.3.7**, and **device revision 2**, alongside TK8/0201. These are device-reported values, separate from catalog versions. |
| Unestablished hardware | Exact Bluetooth/audio chip, camera/ISP SoC, sensor part number, optics/FOV, native megapixels, RAM, flash/storage capacity, battery capacity, Wi-Fi chipset/band, and board revision. No values should be inferred from the E09 name or an unrelated listing. |
| Companion test target | **Samsung Galaxy S25+ SM-S936U**, Android 16 / **SDK 36**. Samsung identifies SM-S936U as S25+; SDK level was observed on the test phone. |

The family label comes from the [E09 research repository](https://github.com/sctg-development/ai-smart-glasses-e09/tree/c32e065ebbea769184df5abda9e43774631e1eca), not a verified bill of materials. The phone model mapping is supported by [Samsung's SM-S936U page](https://doc.samsungmobile.com/SM-S936U/032308250214/eng.html). Image dimensions and device identity are summarized physical-test observations; no private photographs, MAC addresses, SSID suffixes, serials, or raw diagnostic logs are included here.

The 320 x 180 observation is not a hardcoded pixel limit in Manfred's BLE assembler: its transfer guards are byte/chunk limits, and it accepts a declared payload up to 32 MiB. That establishes that the app does not resize every BLE image to 320 x 180; it does not demonstrate that this firmware can supply a higher-resolution image over BLE. A supported alternate command or compatible firmware would still have to produce those bytes.

## Transport profile used by Manfred

| Path | TK8 evidence and implementation |
| --- | --- |
| BLE service | `0000aa12-0000-1000-8000-00805f9b34fb`; AA13 writes commands, AA14 reports command/status data, AA15 carries the BLE photo path. |
| Command framing | App requests start `AB 55`; this unit's AA14 replies start `AC 55`. The decoder accepts both supported headers and validates length plus the additive command/payload checksum. The earlier AB55-only decoder missed this unit's replies. |
| Normal shutter | `0x22`, payload `30`, matching the vendor home-screen shutter. The vendor calls this parameter THUMBNAIL; that name alone does not establish the size of the separately stored original. |
| Media AP startup | `0x39`, payload `30`, followed by the asynchronous SSID report on `0x25`. |
| Experimental live AP startup | One `0x67`, payload `30`, then wait for SSID. Do not follow it with a media-start request merely to retrieve SSID: that changes the startup sequence. |
| File listing | HTTP `GET http://192.168.169.1/app/getfilelist`; JSON `info[].files[]` includes name, size, and `createtimestr`. |
| Original retrieval | HTTP `GET http://192.168.169.1/<encoded-original-path>`. Manifest size is a vendor metadata value; validate actual HTTP completion and JPEG contents. |
| End transfer | `0x44`, payload `30 01`: finish without clearing originals. |
| Live transport | Vendor URL `rtsp://192.168.169.1/h264`; its player uses LibVLC with RTSP-over-TCP. |

These are the **TK8 AP** routes, not the alternate P2P/XML routes for other EyeVue projects. The current implementation uses the selected Android Wi-Fi `Network.socketFactory` and network-specific DNS for EyeVue HTTP only; it does not bind the entire app process. This was motivated by a physical timeout routed through the phone's VPN and the user's subsequent successful sync after disabling Tailscale. Simultaneous VPN/Omi behavior still needs its own acceptance test.

Source: [native protocol](../../apps/manfred-companion/platform/android/kotlin/eyevue/EyevueProtocol.kt), [plugin startup/capture](../../apps/manfred-companion/platform/android/kotlin/eyevue/EyevuePlugin.kt), [photo session](../../apps/manfred-companion/platform/android/kotlin/eyevue/EyevuePhotoSession.kt), and vendor `EyevueTLiveActivity.java:36,207-211,230-246` under the source root below.

## Capture while Wi-Fi is active: evidence and limits

During the supervised 0.5.0 primary media-AP test, shutter attempts did not import a new image while the AP was active. An offline capture produced a count increase of one in about 2.4 seconds; the user also reported ignored still-photo attempts with Wi-Fi active. Persistent-AP full-resolution capture is therefore **not an accepted capability**. The exact behavior of the media and experimental live startup paths must be recorded separately as controlled tests finish.

The vendor home-screen shutter handler blocks capture while its `isImport` state is true and displays an importing message. This is concrete evidence of a vendor software mode policy. It is not proof that the camera hardware cannot operate with Wi-Fi: the vendor's live activity explicitly opens an AP and plays a camera stream. Nor does live support establish simultaneous full-resolution JPEG capture.

There is an important status-schema mismatch: the vendor's `0x45` parser reads `isImport` from **payload byte 9 only when at least 10 bytes exist**. The measured TK8 reply contains **9 payload bytes** (15 bytes including frame overhead). It has no import byte. A default false value from parsing that shorter frame must not be reported as a measured "not importing" state.

Sources: vendor `view/home/HomeFragment.java:202-217`, `bluetooth/beans/SyncValueBean.java:33-45`, and `view/live/EyevueTLiveActivity.java:230-246`. The import gate is a code observation; whether a different supported command sequence or firmware revision permits concurrent still capture remains unresolved.

The earlier CyanBridge live attempt reached the RTSP server, then Media3 rejected the SDP attribute `a=decode_buf=300`. This is an Android player/parser compatibility failure, not evidence that the stream is absent. The vendor's LibVLC path is a useful implementation reference. A stream-frame fallback would need separate decoding, resolution, latency, and network-routing measurements; it must not be described as a 3200 x 2400 still-photo path. See [Media3 RTSP documentation](https://developer.android.com/media/media3/exoplayer/rtsp) and its [SDP parser source](https://github.com/androidx/media/blob/release/libraries/exoplayer_rtsp/src/main/java/androidx/media3/exoplayer/rtsp/SessionDescriptionParser.java).

## 0.5.1 capture-and-fetch attempt at low battery

The supervised **0.5.1** attempt joined the glasses AP, read the initial album baseline, finished transfer mode, and reached camera Ready. The user's first **Take photo** attempt then produced an apparent error sound, as reported by the user. It timed out after **15 seconds** without a `0x22` shutter acknowledgement, photo-busy/count event, or saved image. The sound has not been decoded as a specific firmware rejection reason.

During this interval, spontaneous `0x53` battery payloads were `00 10`, `00 0F`, `00 0E`, and `00 0D`: **16%, 15%, 14%, then 13%, all not charging**. For this message, byte 0 is the charging flag and byte 1 is the raw unsigned percentage; these are neither progress values nor BCD digits. The separate `0x17` query response has a different encoding. Sources: vendor `bluetooth/beans/ReciveBatteryBean.java:23-27`, `BatteryBean.java:23-27`, and [Manfred's battery parser](../../apps/manfred-companion/platform/android/kotlin/eyevue/EyevueProtocol.kt).

The vendor app explicitly refuses **Wi-Fi import below 20%** (`view/photo/PhotoListFragment.java:705-707`) and **live streaming below 20%** (`view/home/HomeFragment.java:300-301`). Its battery listener stores the reported percentage in `iv6.h` at `HomeFragment.java:402`. The ordinary shutter handler at `HomeFragment.java:202-217` checks connection, recording, and importing state but has no battery check. No low-battery shutter rejection code was established in the reviewed response handling.

Low power is therefore a plausible, **unproven** explanation for this attempt. Charge above the vendor's 20% import/live threshold, verify the reported level, and repeat the capture-and-fetch test before attributing failure to firmware AP/camera concurrency. This attempt does not prove that concurrency is the cause.

## Firmware identification and update architecture

The vendor's read-only device-info request is **command `0x55`, payload `00`**, encoded as `AB 55 00 03 55 00 55`. Manfred's integrated connection flow uses `buildGetDeviceInfoPacket()` for an optional query with a three-second response limit, alongside the separate customer/profile lookup. The installed 0.5.1 build displayed a valid device response: **BT 1.1.9, ISP 3.3.7, device 2**. This was confirmed in the supervised device UI; a retained raw 0x55 diagnostic record is still pending.

For a `0x55` response with at least seven payload bytes, the vendor schema is:

| Payload bytes (unsigned) | Meaning |
| --- | --- |
| 0, 1, 2 | Bluetooth firmware version, dotted decimal |
| 3, 4, 5 | ISP firmware version, dotted decimal |
| 6 | Device revision/version, decimal string |

Reject a shorter response as incomplete rather than inventing values. The similarly named outbound `0x65` operation is a setting/send operation, not this getter. Source: vendor `bluetooth/manager/SendCommandViaBle.java:64-66` and `bluetooth/beans/DeviceValueBean.java:26-32`.

The update metadata model has independent **ble** and **wifi** packages. The settings screen requests metadata using its current **Bluetooth version** plus `project + customer` (for this profile, `TK80201`). Its Retrofit route is `GET https://platform.eyevue-glass.com/api/app/ota/getNewOtaPackage` with `deviceVersion` and `code`. The checks below distinguish a placeholder-version catalog probe from a subsequent query using the actual installed Bluetooth version. Sources: vendor `view/setting/EyevueDeviceSettingActivity.java:326-362`, `com/eyevue/common/bean/ota/OtaBean.java:10-14`, and `defpackage/{hrb,e10,f00,ax4}.java`.

The **TK8/T-series Wi-Fi updater uses raw TCP at 192.168.169.1:5007**, with a binary handshake, file transfer, result strings, and final BLE confirmation. It is not the generic HTTP firmware upload used by some other projects. Its Bluetooth package is handled separately through the Jieli RCSP OTA library. These source-level components do not by themselves prove a particular two-chip layout or identify an exact Jieli/camera part number. Sources: vendor `view/setting/EyevueTOtaActivity.java:70-78,515-540,770-783,1015-1017` and `defpackage/r2b.java:11,71-77`; [Jieli's official Android OTA library](https://github.com/Jieli-Tech/Android-JL_OTA).

No buildable TK8 camera firmware source, verified board mapping, or recovery procedure was established by this bounded audit. Library names and the E09 retail family name are insufficient to select a firmware image. Jieli's [OTA compatibility FAQ](https://doc.zh-jieli.com/Apps/Android/ota/en-us/master/other/qa.html) also distinguishes chip/platform and firmware-layout compatibility. A later public catalog probe downloaded one Bluetooth package for data inspection only, as recorded below. No firmware was installed or modified.

## Public catalog probe and Bluetooth package family

On **2026-09-05**, a public, unauthenticated [catalog request for TK80201](https://platform.eyevue-glass.com/api/app/ota/getNewOtaPackage?deviceVersion=0.0.0&code=TK80201) returned HTTP 200. The request deliberately used **`deviceVersion=0.0.0` as a catalog probe**; this is not a version read from the glasses and does not establish update eligibility.

| Catalog field | Returned value |
| --- | --- |
| Bluetooth package | **1.1.9**, ID **45**, code **TK80201** |
| Package name | `腾星 TK8-G6-G6(TK80201) 蓝牙 1.1.9 (03a312s7)` |
| Creation time | `2026-06-22 14:31:07`, as returned; timezone unspecified |
| Wi-Fi/ISP package | `wifi: null` in this response |
| Changelog | `InternationalData: []`; no release notes returned |
| Download | [Catalog-linked Bluetooth .ufw package](https://aws-sg.oss-ap-southeast-1.aliyuncs.com/other/202606/nz2f4NnPC0wc1qfc.ufw), **1,809,504 bytes** |
| SHA-256 | `43d6a5011e18b192cde698aa90019565298e176d257b355a052eff7b277bd57a` |

The downloaded package was inspected as binary data only. It contains `AC701N` at byte offsets `0x410` and `0x159D`, `BR28_EDR_UPDATE` at `0x180D68`, and `BR28_BLE_UPDATE` at `0x195E2D`. Jieli's [official bootloader mapping](https://github.com/Jieli-Tech/fw-Bootloader#sdk与bootloader系列对应说明) identifies **AC701N as the BR28 family**. Together these identify the offered Bluetooth package family; they do not verify the tested unit's physical chip marking, PCB revision, or camera/ISP SoC. Legacy BR22 updater strings are present too, so isolated strings should not be treated as a full hardware inventory.

The package and a labeled catalog evidence JSON are retained as local artifacts, not in Git. Nothing was executed, flashed, or sent to the glasses. The later device query confirmed installed BT 1.1.9, ISP 3.3.7, and device revision 2. The placeholder-version catalog response provides no evidence that a firmware update fixes still capture during Wi-Fi.

### Check using the installed Bluetooth version

After the real device read, a separate [official request with `deviceVersion=1.1.9&code=TK80201`](https://platform.eyevue-glass.com/api/app/ota/getNewOtaPackage?deviceVersion=1.1.9&code=TK80201) returned **HTTP 200**, `success: true`, and `data: {"wifi": null, "ble": null}` on **2026-09-05**. The vendor endpoint offered no package for this lookup. This is narrower than a claim that all firmware is current: the request follows the vendor app's Bluetooth-version contract and does not independently compare ISP 3.3.7 with a complete ISP release catalog. No update was installed.

## Decisions for the next hardware test

1. Preserve the verified BT 1.1.9 / ISP 3.3.7 / device 2 values and add the read-only 0x55 diagnostic record; retain TK8/0201 as separate profile fields.
2. Give the vendor those versions and the current-version catalog result, then ask whether this firmware supports a JPEG shutter during media AP or live AP. No applicable update or capture-mode fix was offered by this lookup.
3. Repeat the installed 0.5.1 Capture and fetch test after charging above 20% and verifying the reported level: baseline the album, close AP, wait for camera idle and a fresh count, capture offline, then reconnect to fetch new originals. The low-battery attempt timed out; the complete hardware cycle is not yet verified.
4. Keep persistent AP capture and RTSP frame extraction experimental until each has measured image dimensions and repeatable delivery.

## Reproducible source references

Vendor source was inspected in `/home/matt/research/eyevue/repos/e09-main`, commit `c32e065ebbea769184df5abda9e43774631e1eca`. The repository contains a decompiled Android app, not device firmware source. Paths above beginning `view/` or `bluetooth/` are relative to `reverse/com.eyevue.glassapp/sources/com/eyevue/glassapp/`; other vendor paths are relative to `reverse/com.eyevue.glassapp/sources/`.

The app source is reproducible from the [pinned research tree](https://github.com/sctg-development/ai-smart-glasses-e09/tree/c32e065ebbea769184df5abda9e43774631e1eca/reverse/com.eyevue.glassapp/sources). Manfred's port history and source hashes are recorded in [SOURCE_PROVENANCE.md](../../apps/manfred-companion/platform/android/kotlin/eyevue/SOURCE_PROVENANCE.md). The Manfred reference commit identifies inspected software; it does not replace a physical acceptance record.
