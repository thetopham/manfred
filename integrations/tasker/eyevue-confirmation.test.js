const test = require("node:test");
const assert = require("node:assert/strict");
const flow = require("./eyevue-worker-flow.js");
const id = "12345678-1234-4234-8234-123456789abc";
const owner = "worker_original";
function evidence(changes = {}) {
    return {version:1, stage:"attach_send", id, owner, status:"send_confirmed",
        selectionAttempted:true, sendAttempted:true,
        selectionActionCompleted:true, sendActionCompleted:true,
        submissionObserved:true, newImageObserved:true, networkValidated:true,
        confirmationEnabled:true, errorUiObserved:false, uploadInProgress:false,
        focusModeToggleAttempted:false, focusModeToggleCompleted:false,
        voiceNeedsResume:false, voiceActiveAfter:true, ...changes};
}
function decide(value) { return flow.afterAttach(JSON.stringify(value), id, owner); }
test("complete requires all positive evidence in an unchanged presentation", () => {
    assert.deepEqual(decide(evidence()), {op:"complete", confirmed:true,
        reason:"submission_observed", voiceNeedsResume:false, voiceActiveAfter:true});
});
for (const field of ["selectionAttempted", "sendAttempted", "selectionActionCompleted",
    "sendActionCompleted", "submissionObserved", "newImageObserved", "networkValidated",
    "confirmationEnabled"]) {
    test("holds missing, false, or non-boolean positive evidence: " + field, () => {
        for (const invalid of [undefined, null, false, 0, 1, "true", {}, []]) {
            const decision = decide(evidence({[field]:invalid}));
            assert.equal(decision.op, "hold");
            assert.equal(decision.confirmed, false);
        }
    });
}
for (const field of ["errorUiObserved", "uploadInProgress", "focusModeToggleAttempted",
    "focusModeToggleCompleted"]) {
    test("holds missing, true, or non-boolean negative evidence: " + field, () => {
        for (const invalid of [undefined, null, true, 0, 1, "false", {}, []]) {
            assert.equal(decide(evidence({[field]:invalid})).op, "hold");
        }
    });
}
test("old images revealed by a focus toggle cannot confirm this receipt", () => {
    const decision = decide(evidence({focusModeToggleAttempted:true,
        focusModeToggleCompleted:true, imagesBeforeSend:[],
        imagesAfterSend:[{uniqueId:"old-photo"}], newImageObserved:true}));
    assert.equal(decision.op, "hold");
    assert.equal(decision.reason, "confirmation_view_changed");
});
test("an attempted but unconfirmed toggle is equally ambiguous", () => {
    assert.equal(decide(evidence({focusModeToggleAttempted:true})).reason,
        "confirmation_view_changed");
});
test("status text without the supporting evidence cannot complete", () => {
    const decision = decide({version:1, stage:"attach_send", id, owner,
        status:"send_confirmed", selectionAttempted:true, sendAttempted:true});
    assert.equal(decision.op, "hold");
    assert.equal(decision.reason, "confirmation_evidence_incomplete");
});
test("valid upload does not require uninterrupted voice or cause another send", () => {
    const decision = decide(evidence({voiceNeedsResume:true, voiceActiveAfter:false}));
    assert.equal(decision.op, "complete");
    assert.equal(decision.voiceNeedsResume, true);
});
test("ambiguous sends remain held even with otherwise positive fields", () => {
    for (const status of ["ambiguous", "error", "no_op", "prepared", ""]) {
        assert.equal(decide(evidence({status})).op, "hold");
    }
});
test("identity-invalid results cannot supply confirmation or voice observations", () => {
    for (const change of [{id:"other"}, {owner:"other"}, {stage:"prepare"}, {version:2}]) {
        const decision = decide(evidence(change));
        assert.equal(decision.op, "hold");
        assert.equal(decision.voiceActiveAfter, null);
        assert.equal(decision.voiceNeedsResume, null);
    }
});
test("malformed result bodies fail closed", () => {
    for (const raw of ["", "%mq_ui_result", "{", "null", "[]", "42", "true"]) {
        assert.equal(flow.afterAttach(raw, id, owner).op, "hold");
    }
});
test("decision parsing has no mutation, dispatch, retry, or send side effects", () => {
    const raw = JSON.stringify(evidence({focusModeToggleAttempted:true}));
    const copy = raw;
    for (let i=0; i<20; i++) assert.equal(flow.afterAttach(raw,id,owner).op, "hold");
    assert.equal(raw, copy);
});
