/* Pure result/dispatch policy. No UI actions and no queue writes. */
(function (root, factory) {
    if (typeof module === "object" && module.exports) module.exports = factory();
    else root.EyevueWorkerFlow = factory();
}(this, function () {
    "use strict";
    function result(raw, stage, id, owner) {
        var value;
        try { value = JSON.parse(raw); } catch (_) { return null; }
        if (!value || value.version !== 1 || value.stage !== stage ||
            value.id !== id || value.owner !== owner) return null;
        return value;
    }
    function reason(value, fallback) {
        return value && typeof value.reason === "string" && /^[a-z0-9_]{1,64}$/.test(value.reason) ?
            value.reason : fallback;
    }
    function afterPrepare(raw, id, owner) {
        var value = result(raw, "prepare", id, owner);
        if (value && value.status === "prepared" &&
            value.selectionAttempted === false && value.sendAttempted === false) {
            return { op: "begin_send", reason: "prepared" };
        }
        return { op: "hold", reason: reason(value, "preparation_unconfirmed") };
    }
    // Recheck the worker's positive evidence at the queue boundary. A status
    // string alone is not an acknowledgement, and missing flags are unknown.
    function stableSubmission(value) {
        return value && value.status === "send_confirmed" &&
            value.selectionAttempted === true && value.sendAttempted === true &&
            value.selectionActionCompleted === true && value.sendActionCompleted === true &&
            value.submissionObserved === true && value.newImageObserved === true &&
            value.networkValidated === true && value.confirmationEnabled === true &&
            value.errorUiObserved === false && value.uploadInProgress === false &&
            value.focusModeToggleAttempted === false && value.focusModeToggleCompleted === false;
    }
    function afterAttach(raw, id, owner) {
        var value = result(raw, "attach_send", id, owner);
        var decision = { op: "hold", confirmed: false,
            reason: reason(value, "attachment_unconfirmed") };
        if (stableSubmission(value)) {
            decision = { op: "complete", confirmed: true, reason: "submission_observed" };
        } else if (value && value.status === "send_confirmed") {
            // Revealing a transcript after Send can expose OLD Image nodes.
            // Until both snapshots share a verified presentation, fail closed;
            // never turn this ambiguity into another selection or Send attempt.
            decision.reason = value.focusModeToggleAttempted === true ||
                value.focusModeToggleCompleted === true ?
                "confirmation_view_changed" : "confirmation_evidence_incomplete";
        }
        // Observed voice state is independent of whether image confirmation is
        // enabled. Missing or identity-invalid evidence is unknown, not false.
        decision.voiceNeedsResume = value && typeof value.voiceNeedsResume === "boolean" ?
            value.voiceNeedsResume : null;
        decision.voiceActiveAfter = value && typeof value.voiceActiveAfter === "boolean" ?
            value.voiceActiveAfter : null;
        return decision;
    }
    function shouldKickReceipt(value) {
        return value && value.ok === true && (value.status === "queued" || value.status === "duplicate") &&
            value.counts && value.counts.queued > 0 && value.counts.activeId === "";
    }
    function shouldDispatch(value, workerRunning) {
        return value && value.ok === true && value.status === "ready" && workerRunning === false;
    }
    return { afterPrepare: afterPrepare, afterAttach: afterAttach,
        shouldKickReceipt: shouldKickReceipt, shouldDispatch: shouldDispatch };
}));
