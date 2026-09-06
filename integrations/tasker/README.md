# EyeVue photos to ChatGPT with Tasker

This experimental integration receives photos saved by Manfred, queues each receipt once, and uses Tasker to attach the exact image in the current ChatGPT conversation. The optional UI worker can enter voice, open the file picker, submit the image once, and attempt to resume voice after confirmed submission. It uses the installed ChatGPT interface; it does not use an API backend.

Select the intended conversation before running it. The worker does not bind to a conversation ID, and opening Files can leave voice mode. **Automatic attachment and queue completion are verified for supervised replays; fresh glasses-capture-to-ChatGPT delivery and continuous audio remain unverified.** For the glasses capture/transfer setup and its separate Wi-Fi reliability limits, see [EyeVue photos in Manfred](../../docs/eyevue-photos.md). Detailed selectors, gates and result fields are in the [Java UI worker guide](eyevue-ui-worker.md).

## Current device acceptance

Supervised checks on **2026-09-05**, using a Galaxy S25, Android 16, Tasker 6.6.20 and ChatGPT 1.2026.244:

| Stage | Observed result |
| --- | --- |
| Receipt and queue | A receipt queued with the correct checksum through `sha[255]`. Repeating the final completed receipt returned `duplicate`; the saved UI-worker result was unchanged, with no additional worker execution recorded. |
| Tasker import | The corrected XML attribute order made worker actions 1–10 visible. The generated worker contains 12 actions; confirmation of all 12 by a fresh device export remains pending. Java `source()` execution works on the phone. |
| Automated preparation | Start voice → Files → Upload files → exact filename completed in **2.408 s** in the separate preparation check. |
| Automatic attachment, first final replay | Exact-file ACTION_CLICK and native Send completed. Submission/new-image/network checks passed, error/upload flags were false, and Live End remained visible. A fresh independent UI snapshot confirmed the image, a new reply and no draft. The attachment stage took **3.312 s**. |
| Automatic completion, second final replay | The same worker returned `send_confirmed` and the queue became **SENT automatically**. The attachment stage took **3.777 s**; Live End stayed visible and the existing transcript was left open without a focus toggle or manual UI actions. |
| Network-dependent manual recovery | An earlier exact attachment failed on validated cellular internet with Wi-Fi disconnected. After home Wi-Fi reconnected and auto reconnect was enabled, an image-specific manual Retry produced a new detailed reply with Live End present. |

The **3.312–3.777 s** measurements cover the Tasker attachment stage, from selection through UI confirmation. They exclude receipt/preparation, shutter, glasses AP discovery and file transfer; they are not broadcast-to-image or full capture latency. These were controlled replays of saved originals, not fresh native captures. Visible Live state does not prove uninterrupted audio.

After the first final replay was independently verified, the completion switch was enabled **on this test phone only**, using the exact result and held-item ownership guard. New installations must leave it unset until their own supervised acceptance check. The final duplicate test verifies suppression after automatic queue completion; it does not establish burst delivery from multiple new physical captures.

Host tests cover queue transitions, generation and simulated Java execution. They cannot establish installed UI behavior, Android grants, server acceptance, background reliability or uninterrupted voice.

## Setup

Use the tested app versions initially. Tasker needs its accessibility service enabled, access to the deployed scripts and diagnostic directory, and the image URI grant from Manfred. Keep the phone unlocked and the intended ChatGPT conversation selected for supervised tests. The worker does not unlock the phone.

The complete sanitized Tasker schema is checked in as `tasker-schema-template.prj.xml`; no personal export is needed to regenerate the portable test bundle:

```sh
python3 integrations/tasker/generate_tasker_bundle.py \
  --expected-worker-sha256 <frozen-source-sha256> \
  --output /path/to/Tasker-Manfred-0.5.3-test.zip
```

Supply the SHA256 of the reviewed worker source being packaged. The default schema is pinned and contains only generic authored actions; an optional `--template` accepts the pinned legacy schema export. The archive's own README covers deployment, and SHA256SUMS covers every supplied file. It contains no personal queue, image or recovery artifact. The separate generators below support staged receipt-only setup using the same checked-in schema.

Start with the receipt-only project:

```sh
python3 integrations/tasker/generate_receipt_project.py \
  --template integrations/tasker/tasker-schema-template.prj.xml \
  --output /path/to/Manfred_Photo_Queue.prj.xml
```

Import it in Tasker. It creates **Manfred Photo Receipt**, profile/task 40, with one JavaScriptlet action, Auto Exit enabled and a 45-second script timeout. Enable the event profile, then take a new photo after Manfred reports Ready. This isolated project writes a receipt diagnostic and shows its status. Playing the task manually supplies no event extras.

Once receipt handling works, generate the worker and its receipt loader:

```sh
python3 integrations/tasker/generate_worker_project.py \
  --template integrations/tasker/tasker-schema-template.prj.xml \
  --output /path/to/Manfred_ChatGPT_Worker.prj.xml \
  --receipt /path/to/receipt.js \
  --loader /path/to/receipt-loader.js
```

Import **Manfred Photo Worker** (task 50) and **Manfred Photo Dispatch** (task 51). Deploy `receipt.js` to `/sdcard/Tasker/Manfred/receipt.js`, then replace the existing receipt action body with the generated `receipt-loader.js`; keep Auto Exit enabled. Use `--device-receipt-path` when deploying elsewhere. The production receipt bundle queues silently and starts the worker when work is ready.

The loader must keep its explicit inline event-variable references. Replacing it with a bare `eval(readFile(...))` loses required locals on the tested Tasker version. Built-ins inside the external bundle use `tk.*`. The generated worker embeds its Java source; an installation that instead uses `source()` must also update the referenced device file when the worker changes.

Verify the installed action list after import. Preserve the generator's `sr`-before-`ve` XML attribute order, which is required by the observed Tasker import behavior. Configure the receipt task to allow simultaneous instances and the worker/dispatcher to reject duplicate instances through Tasker's collision settings. The generators do not override those settings. See Tasker's [JavaScript](https://tasker.joaoapps.com/userguide/en/javascript.html) and [task scheduling](https://tasker.joaoapps.com/userguide/en/tasks.html) references.

Leave **%ManfredEyevueUiConfirmed unset** for the first supervised submission: the result remains held for inspection. Set it to **verified_on_device_v1** only after checking the installed worker's exact image, submission evidence and destination. This enables a UI-confirmed result, not a server receipt guarantee. A UI change or uncertain result still holds the item; the switch never enables automatic retries.

## Event and image identity

Use an **Intent Received** event for **com.thetopham.manfred_companion.EYEVUE_IMAGE_READY**. Manfred targets `net.dinglisch.android.taskerm` and grants read access through the MediaStore URI and ClipData. Do not add a MIME/data filter: the current broadcast carries the URI in extras/ClipData, not `Intent.data`.

[EyevuePhotoSession](../../apps/manfred-companion/platform/android/kotlin/eyevue/EyevuePhotoSession.kt) commits the gallery JPEG before [broadcastImageReady](../../apps/manfred-companion/platform/android/kotlin/eyevue/EyevuePlugin.kt) publishes it. Tasker maps its extras as follows:

| Native extra | Tasker local | JavaScript input |
| --- | --- | --- |
| `id` | `%var_id` | `var_id` |
| `sessionId` | `%sessionid` | `sessionid` |
| `uri` | `%uri` | `uri` |
| `fileName` | `%filename` | `filename` |
| `width`, `height`, `bytes` | same names with `%` | same names |
| `sha256` | `%sha256` | `sha[255]` |
| `detectedAt`, `receivedAt` | `%detectedat`, `%receivedat` | lowercase names |

Tasker interprets numeric suffixes as array indices; its JavaScript arrays are zero-based. The adapter similarly exports `mq_sha[255]` for the following Java action's `%mq_sha256`. See Tasker's [Intent](https://tasker.joaoapps.com/userguide/en/intents.html) and [variable](https://tasker.joaoapps.com/userguide/en/variables.html) references. `capturedAt` may be absent. The private `cachePath` is ignored because Manfred can evict preview files; the handoff uses the gallery URI.

Receipts require the native UUID/filename relationship (`EyeVue_<milliseconds>_<first-eight-ID-characters>.jpg`), an exact `content://media/{external|external_primary}/images/media/<positive-ID>` URI, positive integral dimensions/bytes/timestamps, and lowercase SHA256. The Java worker additionally opens that exact URI and verifies MIME, display name, byte count, checksum and image dimensions. Missing or changed content fails without selecting a different recent photo. Metadata validation alone does not authenticate the event or guarantee URI-grant lifetime.

## Queue and worker contract

`eyevue-queue.js` owns **%ManfredEyevueQueue**. Every transition is one synchronous JavaScript action with readback before granting a send permit. `eyevue-tasker.js` uses `global()`/`setGlobal()` for persistent state and direct lowercase variables for action inputs/outputs. Keep all queue writers on this adapter.

| `%mq_op` | Inputs | Result |
| --- | --- | --- |
| `receipt` | Event extras | `queued` or `duplicate`, without requeueing |
| `claim` | None; creates an owner token | `claimed`, `busy` or `empty` |
| `begin_send` | `%mq_id`, `%mq_owner` from claim | `send_permitted` once |
| `complete` | Same ID/owner, `%mq_confirm=yes` | `sent` |
| `hold` | Same ID/owner after uncertainty | `held` |
| `inspect` | None | `ready`, `blocked` or `empty` |
| `resolve` | Explicit manual recovery | `resolved_sent`, `resolved_discard` or `resolved_retry` |

Each call sets `%mq_ok`, `%mq_status` and `%mq_error`. Successful item outputs include ID, owner, URI, filename, dimensions, bytes and checksum; summary outputs are `%mq_queued`, `%mq_activeid` and `%mq_activestate`. Failed/busy operations clear item outputs, and confirmation is consumed. Do not insert `inspect` into a worker and reuse the item locals it overwrites.

The worker claims one item, verifies content and prepares the picker without selecting. A matching prepare result permits `begin_send`. The local permit and UI ledger are consumed **before the first possible selection/upload**. Only a matching `send_confirmed` result with both attempt flags completes the queue item. Uncertainty holds it. A stop/crash can leave claimed or sending state; all three active states block another worker. There is no expiry, takeover or automatic resend.

The final worker action schedules the dispatcher at priority 4; it starts worker priority 5 only for a fresh ready queue and no running worker. Burst delivery and final-action scheduling still require phone acceptance. Tasker globals are not a transactional database or an exactly-once guarantee across every OS failure.

All **256 receipt IDs**, including sent/discarded tombstones, are retained. At capacity, `queue_full` refuses new entries rather than forgetting deduplication history. Back up and review the queue before an explicit archive/reset; a reset also resets that history. Corrupt JSON fails as `queue_corrupt` and must not be automatically cleared. No queue operation deletes gallery photos.

## Recovery

Stop **Manfred Photo Worker** and **Manfred Photo Dispatch**, inspect the destination and the exact queued image, then resolve the held item explicitly. Generate the separate manual task:

```sh
python3 integrations/tasker/generate_maintenance_project.py \
  --template integrations/tasker/tasker-schema-template.prj.xml \
  --output /path/to/Manfred_Replay_Resolve.prj.xml
```

**Manfred Replay Resolve** (task 52) has no profile or automatic trigger. Supply Parameter 1 as JSON with the exact held ID and owner:

```json
{"id":"<held-UUID>","owner":"<held-owner>","resolution":"sent","confirmation":"reviewed_and_worker_stopped"}
```

Use `sent` only after manually verifying acceptance; use `discard` to stop processing without asserting submission. The helper rejects a different owner, non-held item, running/unknown task state, malformed queue or changed snapshot. It preserves other items, receipt tombstones, the UI ledger and gallery files. It clears its invocation's parameter but cannot erase a caller's separately copied value. It never dispatches work or enables the UI-confirmation switch.

A narrowly supported `retry` requires a reviewed **prepare-stage failure before selection**: add `retryConfirmation: "reviewed_prepare_no_selection"` and the actual structured Java `prepareResult`. That result must have version 1, stage `prepare`, status `error`, matching ID/owner, and boolean `selectionAttempted=false` and `sendAttempted=false`. Do not use this exception for an ambiguous attachment or send. Inputs are capped at 16 KiB. Retry clears the old owner and requeues that item; the next claim gets a new owner. Start the dispatcher explicitly only when ready for the next supervised attempt.

The optional `generate_debug_retry.py` creates a manually invoked, fixed-ID development artifact that reads the actual saved result and applies the same checks. It does not dispatch. It is not part of normal receipt processing.

## Handoff limits and diagnostics

The native image event means **the gallery save completed**, not that glasses Wi-Fi has closed. On the tested S25, local-only Wi-Fi uses the primary interface. The worker waits up to 20 seconds for validated internet before preparation, checks it again before selection and Send, and stops on a locked screen. The observed cellular-path attachment failure confirms that validated internet does not prove a usable ChatGPT upload path. Check the actual Wi-Fi connection and the saved network's auto-reconnect setting; unreliable glasses AP joins remain a separate capture problem. See the [measured home-network finding](../../docs/hardware/eyevue-e09.md#why-home-wi-fi-disconnects).

The worker acts in the current ChatGPT screen and cannot prove a server conversation ID. Keep that conversation selected during a supervised run. Files can end voice mode; the latest worker attempts one resumption after gated UI confirmation, recording any failure separately without resending a confirmed image. Continuous audio, unattended operation and queue draining after multiple real captures remain unverified.

Diagnostics overwrite files under `Tasker/`: `manfred-receipt.json`, `manfred-worker.json`, `manfred-ui-worker.json`, `manfred-worker-dispatch.json`, `manfred-dispatch.json` and `manfred-replay-resolution.json`. They retain IDs, bounded reason codes and UI/voice metadata rather than unrelated message text. The UI ledger is stored separately in **%ManfredEyevueUiLedger**. Inspect the full Java result before recovery; a summary status alone is insufficient proof for retry.

## Tests

Run from the repository root:

```sh
node --test integrations/tasker/eyevue-queue.test.js \
  integrations/tasker/eyevue-worker-flow.test.js \
  integrations/tasker/eyevue-replay-maintenance.test.js
python3 -m unittest discover -s integrations/tasker -p 'test_*project.py' -v
python3 -m unittest discover -s integrations/tasker -p 'test_debug_retry.py' -v
```

The [worker guide](eyevue-ui-worker.md#verification) covers the task-local BeanShell runtime and Java tests. Re-run supervised receipt, exact-file selection, submission, voice resumption and second-photo checks after changing the installed Tasker or ChatGPT UI.
