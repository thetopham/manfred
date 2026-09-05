# Chat Mirror source reconciliation

Destination baseline: Manfred `36af66ae5e0cac0949f1994afe0d802b5913bbe8` on `feat/chat-mirror-app`.

Source: Brain draft head `4eccc7e4678c2cd7eafb70a38fcdffe687690639`, limited to the 26-file delta against merge-base `35d72eb49488206e5d71c1bfdd8b7228cc6da3e2`. Only `apps/manfred-companion/` and `ingestion/manfred-ears/` code/docs/tests were imported. No old repository tree, personal Wiki content, raw evidence, or runtime configuration was copied.

## Reconciliation

- Applied the delta using Git three-way patching with original source blobs. Server package/deploy/tests move from `ingestion/manfred-ears/` to the Manfred root; the Ears README maps to `docs/ears.md`. Application paths remain unchanged.
- The sole textual conflict was the Companion README ownership paragraph. Manfred app/Ears/vision ownership and planned Eyes/CyanBridge wording were kept; Chat Mirror documentation was retained.
- Current archive.py, vision.py, episodes.py, archive/deletion tests, foreground_service.dart and foreground_service_result_test.dart remain unchanged from the new Manfred baseline. Three-way application preserves daily_timezone validation/loading and current service-level archive publication/deletion coordination.
- Removed an unused `_drainFuture` member reintroduced by the candidate. Current audio draining continues to use its existing BackgroundDrainCoordinator; Chat Mirror uses its own existing single-flight synchronization.
- Rendered the added receiver unit through MANFRED_RUNTIME_ROOT, MANFRED_ENV_FILE, MANFRED_STATE_DIR, and a configurable MANFRED_CHAT_MIRROR_HOST. It belongs only to the data-plane role. Installer source-only behavior and no-secret-rewrite boundary are unchanged.
- Adjusted the native Android overlay test root for the flattened repository. Retained all source Chat Mirror tests alongside newer baseline regressions.
- Brain export and API remain independent. No Chat Mirror-to-Brain export is invented by this draft; existing wearable scratch exports continue unchanged.

## Validation boundaries

Validation results are recorded alongside this document. Server tests use synthetic temporary archives and an independent dependency environment. Android helper/overlay tests are source checks; they do not prove a compiled APK or physical capture. The inspected remote host has no Flutter executable on PATH and no detected Flutter/Android SDK directory. No phone, Accessibility service, receiver, scheduler, live archive, or secret configuration was changed.

Final baseline merge: `b9460b26c4699b74ae9980aebfd463da4c6d8220`, including the executable-launcher permission check and regression. It merged without conflicts. Final standalone validation passed all 167 Python tests in 28.974 seconds; installed-package and CLI checks passed in the independent environment. See `FINAL_VALIDATION.txt`. Android APK compilation and physical capture are not claimed.
