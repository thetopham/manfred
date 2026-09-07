const test = require("node:test");
const assert = require("node:assert/strict");
const flow = require("./eyevue-worker-flow.js");
const queue = require("./eyevue-queue.js");

const id = "12345678-1234-4234-8234-123456789abc";
const owner = "worker_original";
function ui(changes = {}) {
    return JSON.stringify({ version: 1, stage: "prepare", status: "prepared", id, owner,
        selectionAttempted: false, sendAttempted: false, ...changes });
}
function confirmed(changes = {}) {
    return { stage:"attach_send", status:"send_confirmed", selectionAttempted:true, sendAttempted:true,
        selectionActionCompleted:true, sendActionCompleted:true, submissionObserved:true,
        newImageObserved:true, networkValidated:true, confirmationEnabled:true,
        errorUiObserved:false, uploadInProgress:false, focusModeToggleAttempted:false,
        focusModeToggleCompleted:false,
        presentationVerifiedBeforeSend:true, confirmationViewChanged:false, ...changes };
}
test("only matching preparation without content submission permits begin_send", () => {
    assert.equal(flow.afterPrepare(ui(), id, owner).op, "begin_send");
    for (const raw of ["%mq_ui_result", "", "{}", ui({id:"wrong"}), ui({owner:"wrong"}),
        ui({stage:"attach_send"}), ui({version:2}), ui({selectionAttempted:true}),
        ui({sendAttempted:true}), ui({status:"error"})]) {
        assert.equal(flow.afterPrepare(raw, id, owner).op, "hold");
    }
});
test("confirmed send requires stage identity and actual selection/send attempt flags", () => {
    const good = confirmed();
    assert.equal(flow.afterAttach(ui(good), id, owner).op, "complete");
    for (const change of [{id:"wrong"},{owner:"wrong"},{stage:"prepare"},{version:0},
        {selectionAttempted:false},{sendAttempted:false},{status:"ambiguous"},
        {status:"error"},{status:"no_op"}]) {
        assert.equal(flow.afterAttach(ui({...good,...change}), id, owner).op, "hold");
    }
});
test("voice needing resume does not authorize resending an already accepted photo", () => {
    const decision = flow.afterAttach(ui(confirmed({voiceNeedsResume:true,voiceActiveAfter:false})), id, owner);
    assert.equal(decision.op, "complete");
    assert.equal(decision.voiceNeedsResume, true);
    assert.equal(decision.voiceActiveAfter, false);
});
test("held attachment preserves observed live voice without completing or resending", () => {
    const decision = flow.afterAttach(ui({stage:"attach_send",status:"ambiguous",
        selectionAttempted:true,sendAttempted:true,voiceNeedsResume:false,voiceActiveAfter:true}), id, owner);
    assert.equal(decision.op, "hold");
    assert.equal(decision.confirmed, false);
    assert.equal(decision.voiceNeedsResume, false);
    assert.equal(decision.voiceActiveAfter, true);
});
test("missing malformed or identity-invalid voice evidence stays unknown", () => {
    for (const raw of ["", ui({stage:"attach_send",status:"ambiguous"}),
        ui({stage:"attach_send",status:"ambiguous",voiceActiveAfter:"true",voiceNeedsResume:0}),
        ui({stage:"attach_send",status:"ambiguous",id:"other",voiceActiveAfter:true,voiceNeedsResume:false})]) {
        const decision = flow.afterAttach(raw, id, owner);
        assert.equal(decision.op, "hold");
        assert.equal(decision.voiceActiveAfter, null);
        assert.equal(decision.voiceNeedsResume, null);
    }
});
test("diagnostic reason cannot echo unrelated window text", () => {
    assert.equal(flow.afterPrepare(ui({status:"error",reason:"private conversation text\nsecret"}),id,owner).reason,
        "preparation_unconfirmed");
});
test("receipt kicks only useful queued work and never a held item", () => {
    assert.equal(flow.shouldKickReceipt({ok:true,status:"queued",counts:{queued:1,activeId:""}}),true);
    assert.equal(flow.shouldKickReceipt({ok:true,status:"duplicate",counts:{queued:1,activeId:""}}),true);
    assert.equal(flow.shouldKickReceipt({ok:true,status:"duplicate",counts:{queued:0,activeId:""}}),false);
    assert.equal(flow.shouldKickReceipt({ok:true,status:"queued",counts:{queued:1,activeId:id}}),false);
    assert.equal(flow.shouldKickReceipt({ok:false,status:"error"}),false);
});
test("dispatch refuses another active task and blocked or corrupt queue", () => {
    assert.equal(flow.shouldDispatch({ok:true,status:"ready"},false),true);
    for (const state of [{ok:true,status:"blocked"},{ok:true,status:"empty"},{ok:false,status:"error"}]) {
        assert.equal(flow.shouldDispatch(state,false),false);
    }
    assert.equal(flow.shouldDispatch({ok:true,status:"ready"},true),false);
});

function receipt(n) {
    const prefix = n.toString(16).padStart(8,"0");
    return {id:prefix+"-1234-4234-8234-123456789abc",sessionId:id,
        uri:"content://media/external/images/media/"+n,fileName:"EyeVue_1788652800000_"+prefix+".jpg",
        width:3200,height:2400,bytes:266148,sha256:"a".repeat(64),detectedAt:1788652800000,receivedAt:1788652801000};
}
test("lower-priority final dispatcher sees a receipt arriving after the worker's final kick", () => {
    let raw = "";
    const db = {global:()=>raw,setGlobal:(_name,value)=>{raw=value;}};
    const run = command => queue.transact(db,command,1788652800000);
    run({op:"receipt",image:receipt(1)});
    const first = run({op:"claim",owner});
    run({op:"begin_send",id:first.image.id,owner});
    run({op:"complete",id:first.image.id,owner,confirmed:true});
    // Worker schedules dispatcher priority4 unconditionally, then a receipt arrives
    // before the worker-priority5 task is marked finished. Direct same-task kick is rejected.
    let workerRunning = true;
    const second = run({op:"receipt",image:receipt(2)});
    assert.equal(flow.shouldKickReceipt(second),true);
    assert.equal(flow.shouldDispatch(run({op:"inspect"}),workerRunning),false);
    workerRunning = false; // Deferred dispatcher executes only after higher-priority worker ends.
    assert.equal(flow.shouldDispatch(run({op:"inspect"}),workerRunning),true);
    assert.equal(run({op:"claim",owner:"worker_next"}).image.id,receipt(2).id);
});
test("ambiguous first image keeps later arrivals durable without dispatching them", () => {
    let raw = "";
    const db = {global:()=>raw,setGlobal:(_name,value)=>{raw=value;}};
    const run = command => queue.transact(db,command,1788652800000);
    run({op:"receipt",image:receipt(1)});
    const first = run({op:"claim",owner});
    run({op:"begin_send",id:first.image.id,owner});
    run({op:"hold",id:first.image.id,owner});
    run({op:"receipt",image:receipt(2)});
    assert.equal(flow.shouldDispatch(run({op:"inspect"}),false),false);
    assert.equal(JSON.parse(raw).items[1].state,"queued");
});
