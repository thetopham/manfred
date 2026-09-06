/* Manual-only resolver. Retry needs matching pre-selection error proof; never resets or dispatches. */
(function (root, factory) {
    if (typeof module === "object" && module.exports) module.exports = factory(require("./eyevue-queue.js"));
    else root.EyevueReplayMaintenance = factory(root.EyevueQueue);
}(this, function (queue) {
    "use strict";
    function fail(code) { throw new Error(code); }
    function resolve(adapter, request, workerRunning, dispatcherRunning, now) {
        try {
            if (!request || typeof request !== "object") fail("explicit_request_required");
            if (request.confirmation !== "reviewed_and_worker_stopped") fail("manual_review_required");
            if (workerRunning !== false || dispatcherRunning !== false) fail("worker_or_dispatcher_running");
            if (typeof request.id !== "string" ||
                !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(request.id)) fail("invalid_id");
            if (typeof request.owner !== "string" || !/^[A-Za-z0-9_-]{8,96}$/.test(request.owner)) fail("invalid_owner");
            if (request.resolution === "retry") {
                if (request.retryConfirmation !== "reviewed_prepare_no_selection") fail("prepare_retry_review_required");
                var proof = request.prepareResult;
                if (!proof || typeof proof !== "object" || proof.version !== 1 ||
                    proof.stage !== "prepare" || proof.status !== "error" ||
                    proof.id !== request.id || proof.owner !== request.owner ||
                    proof.selectionAttempted !== false || proof.sendAttempted !== false) {
                    fail("matching_prepare_no_selection_proof_required");
                }
            } else if (request.resolution !== "sent" && request.resolution !== "discard") fail("resolution_not_allowed");
            var raw = adapter.global(queue.key);
            // Use the queue's validator, then inspect only its normalized snapshot.
            var snapshot = JSON.parse(queue.apply(raw, {op:"inspect"}, now).json);
            var item = snapshot.items.filter(function (entry) { return entry.image.id === request.id; })[0];
            if (!item || item.state !== "held") fail("exact_held_item_required");
            if (item.owner !== request.owner) fail("owner_mismatch");
            var firstRead = true;
            return queue.transact({
                global:function (name) {
                    var value = adapter.global(name);
                    if (firstRead) {
                        firstRead = false;
                        if (value !== raw) fail("queue_changed_during_review");
                    }
                    return value;
                },
                setGlobal:function (name,value) { adapter.setGlobal(name,value); }
            }, {op:"resolve",id:request.id,resolution:request.resolution,confirmed:true}, now);
        } catch (error) {
            return {ok:false,status:"error",error:error && error.message || "maintenance_error"};
        }
    }
    return {resolve:resolve};
}));
