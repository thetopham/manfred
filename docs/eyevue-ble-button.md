# Physical glasses button to BLE preview

This implementation adds a physical-button trigger to Manfred's BLE preview session. Device acceptance of the complete button-to-ChatGPT path must be recorded separately from automated tests.

## Using it

1. Connect the glasses in Manfred, choose **Instant BLE preview**, and select **Start BLE preview**.
2. Wait for **Ready**. Enable the Manfred image-receipt profile in Tasker and its current-conversation delivery workflow.
3. Open the intended ChatGPT Live conversation and leave the Manfred photo session running.
4. Press the physical glasses shutter once. Keep looking at the scene until the BLE preview is saved.
5. Wait for the preview to finish and Manfred to return to Ready before another press.

Tasker being enabled does not connect the glasses or start Manfred's BLE session. Start that session once before using the button. The existing connected-device foreground lease remains held while ChatGPT is foreground. Stop, disconnect, or destruction of Manfred's Activity/engine ends this session; reconnect and start again rather than assuming automatic recovery.

**The delivered preview is a second exposure.** The physical button takes the glasses' ordinary stored photo. Manfred waits for that capture to finish, then requests a fresh BLE preview, typically 320 × 180 on the tested glasses. It does not retrieve the original stored photo over BLE. The app's **Take preview** button still takes one BLE preview directly.

## Capture ownership and evidence

The session subscribes to command/status notifications before querying idle status and the initial media count. A physical trigger requires the observed normal-shutter indication `0x22 [01]`, followed by photo busy/idle and an advanced media count. Count and idle may arrive in either order after busy. A count or idle status alone does not trigger a preview.

One capture owner reserves the request before queueing it. Further app requests and duplicate trigger/status events cannot create parallel previews. The session suppresses trigger interpretation throughout its own `0x22 [31]` command, AA15 transfer, save, and fresh idle/count rearm. A button press during this busy interval is not queued as another preview; the glasses may still store an additional ordinary photo.

The existing AA15 helper subscribes before sending the preview command, validates its assembled transfer, and drains an accepted GATT write before relinquishing ownership on cancellation. Stopping or losing the BLE connection removes the session observer and pending request. A restarted session obtains a fresh count baseline; it does not replay the cancelled capture.

This path sends no Wi-Fi startup or transfer-finish command. It preserves the existing local JPEG validation, gallery publication, SHA-256, image ID, and Tasker receipt contract. `detectedAt` is the received trigger time; `capturedAt` remains unavailable rather than claiming the second exposure occurred at the original button press.

## Validation

Native regression tests cover repeated physical captures, original-photo completion ordering, duplicate/self events, direct app previews, invalid trigger payloads, cancellation and restart, readiness queries, and write failure.

Physical acceptance still requires two separate button presses with ChatGPT Live foreground, two fresh BLE previews, two exact-image receipts and attachments, and no automatic recapture loop. Measure `physical_shutter_received`, `physical_photo_complete`, `ble_preview_requested`, `ble_preview_received`, the JPEG save, and Tasker confirmation independently. Image display is not proof of uninterrupted voice.
