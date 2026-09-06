/*
 * Tasker JavaScript(let), Auto Exit ON. Load eyevue-queue.js as a LOCAL library.
 * Inputs/outputs are top-level Tasker local variables (without '%' in JavaScript).
 * No local()/setLocal(): Tasker documents those functions for WebView scenes.
 */
var mq_result = (function () {
    var operation = typeof mq_op === "undefined" ? "inspect" : String(mq_op);
    var command = {
        op: operation,
        id: typeof mq_id === "undefined" ? "" : String(mq_id),
        owner: typeof mq_owner === "undefined" ? "" : String(mq_owner),
        confirmed: typeof mq_confirm !== "undefined" && String(mq_confirm) === "yes",
        resolution: typeof mq_resolution === "undefined" ? "" : String(mq_resolution)
    };
    if (operation === "claim") {
        command.owner = "worker_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2);
    }
    if (operation === "receipt") {
        command.image = {
            id: typeof var_id === "undefined" ? null : var_id,
            sessionId: typeof sessionid === "undefined" ? null : sessionid,
            uri: typeof uri === "undefined" ? null : uri,
            fileName: typeof filename === "undefined" ? null : filename,
            width: typeof width === "undefined" ? null : width,
            height: typeof height === "undefined" ? null : height,
            bytes: typeof bytes === "undefined" ? null : bytes,
            // Numeric suffixes are Tasker array elements: %sha256 -> sha[255].
            sha256: typeof sha256 !== "undefined" ? sha256 :
                typeof sha !== "undefined" && Array.isArray(sha) ? sha[255] : null,
            detectedAt: typeof detectedat === "undefined" ? null : detectedat,
            receivedAt: typeof receivedat === "undefined" ? null : receivedat
        };
    }
    try {
        return EyevueQueue.transact({
            global: function (name) { return global(name); },
            setGlobal: function (name, value) { setGlobal(name, value); }
        }, command, Date.now());
    } catch (_) {
        return { ok: false, status: "error", error: "queue_library_unavailable" };
    }
}());
/* Clear every output on each call so failure/busy cannot reuse a previous send permit or URI. */
var mq_ok = mq_result.ok ? "1" : "0";
var mq_status = mq_result.status || "error";
var mq_error = mq_result.error || "";
var mq_owner = mq_result.ok && mq_result.owner || "";
var mq_id = mq_result.ok && mq_result.image ? mq_result.image.id : "";
var mq_uri = mq_result.ok && mq_result.image ? mq_result.image.uri : "";
var mq_filename = mq_result.ok && mq_result.image ? mq_result.image.fileName : "";
var mq_width = mq_result.ok && mq_result.image ? String(mq_result.image.width) : "";
var mq_height = mq_result.ok && mq_result.image ? String(mq_result.image.height) : "";
var mq_bytes = mq_result.ok && mq_result.image ? String(mq_result.image.bytes) : "";
var mq_sha256 = mq_result.ok && mq_result.image ? mq_result.image.sha256 : "";
// Declare an actual JS array so Tasker exports %mq_sha256 for the following Java action.
var mq_sha = [];
if (mq_sha256 !== "") mq_sha[255] = mq_sha256;
var mq_queued = mq_result.ok && mq_result.counts ? String(mq_result.counts.queued) : "";
var mq_activeid = mq_result.ok && mq_result.counts ? mq_result.counts.activeId : "";
var mq_activestate = mq_result.ok && mq_result.counts ? mq_result.counts.activeState : "";
/* Confirmation applies to one operation only, including on an error path. */
var mq_confirm = "";
