const test = require("node:test");
const assert = require("node:assert/strict");
const queue = require("./eyevue-queue.js");
const maintenance = require("./eyevue-replay-maintenance.js");
const ID = "12345678-1234-4234-8234-123456789abc";
const OWNER = "worker_controlled_123";
function image(id, index) {
    return {id,sessionId:"abcdef01-1234-4234-8234-123456789abc",
        uri:"content://media/external/images/media/"+index,fileName:"EyeVue_1788652800000_"+id.slice(0,8)+".jpg",
        width:3200,height:2400,bytes:266148,sha256:"a".repeat(64),
        detectedAt:1788652800000,receivedAt:1788652801000};
}
function fixture() {
    let raw = "";
    function apply(command) { raw = queue.apply(raw,command,1788652801000).json; }
    apply({op:"receipt",image:image("87654321-1234-4234-8234-123456789abc",2)});
    apply({op:"claim",owner:"worker_previously_sent"});
    apply({op:"begin_send",id:"87654321-1234-4234-8234-123456789abc",owner:"worker_previously_sent"});
    apply({op:"complete",id:"87654321-1234-4234-8234-123456789abc",owner:"worker_previously_sent",confirmed:true});
    apply({op:"receipt",image:image(ID,1)});
    apply({op:"claim",owner:OWNER});
    apply({op:"hold",id:ID,owner:OWNER});
    apply({op:"receipt",image:image("99999999-1234-4234-8234-123456789abc",3)});
    const values = {[queue.key]:raw,ManfredEyevueUiLedger:"keep-this-ledger"};
    const writes = [];
    return {values,writes,adapter:{
        global:name=>values[name],setGlobal:(name,value)=>{writes.push(name);values[name]=value;}
    }};
}
function request(extra) {
    return Object.assign({id:ID,owner:OWNER,resolution:"sent",confirmation:"reviewed_and_worker_stopped"},extra);
}
function run(f, req=request(), worker=false, dispatcher=false) {
    return maintenance.resolve(f.adapter,req,worker,dispatcher,1788652805000);
}
test("manual proof resolves only exact held owner while preserving other items, ledger, and ID tombstone",()=>{
    const f=fixture(),before=JSON.parse(f.values[queue.key]);
    const result=run(f),after=JSON.parse(f.values[queue.key]);
    assert.equal(result.status,"resolved_sent");
    assert.deepEqual(after.items[0],before.items[0]);
    assert.deepEqual(after.items[2],before.items[2]);
    assert.equal(after.items[1].state,"sent");
    assert.equal(after.items[1].owner,null);
    assert.deepEqual(after.items[1].image,before.items[1].image);
    assert.equal(f.values.ManfredEyevueUiLedger,"keep-this-ledger");
    assert.deepEqual(f.writes,[queue.key]);
    assert.equal(queue.apply(f.values[queue.key],{op:"receipt",image:before.items[1].image},1788652806000).result.status,"duplicate");
});
test("discard is allowed without deleting photo metadata or changing next queued item",()=>{
    const f=fixture(),result=run(f,request({resolution:"discard"}));
    assert.equal(result.status,"resolved_discard");
    assert.equal(JSON.parse(f.values[queue.key]).items[1].state,"discarded");
    assert.equal(result.counts.queued,1);
});
for(const [name,req,worker,dispatcher,code] of [
    ["wrong owner",request({owner:"worker_different_123"}),false,false,"owner_mismatch"],
    ["wrong id",request({id:"99999999-1234-4234-8234-123456789abc"}),false,false,"exact_held_item_required"],
    ["missing proof",request({confirmation:""}),false,false,"manual_review_required"],
    ["retry without additional proof",request({resolution:"retry"}),false,false,"prepare_retry_review_required"],
    ["running worker",request(),true,false,"worker_or_dispatcher_running"],
    ["running dispatcher",request(),false,true,"worker_or_dispatcher_running"],
    ["unknown runtime state",request(),undefined,false,"worker_or_dispatcher_running"]
]) test(name+" refuses without writes",()=>{
    const f=fixture(),before=f.values[queue.key];
    const result=maintenance.resolve(f.adapter,req,worker,dispatcher,1788652805000);
    assert.equal(result.error,code);assert.equal(f.values[queue.key],before);assert.equal(f.writes.length,0);
});
test("claimed or sending items cannot be resolved by the controlled replay task",()=>{
    for(const state of ["claimed","sending"]) {
        const f=fixture(),value=JSON.parse(f.values[queue.key]);value.items[1].state=state;
        f.values[queue.key]=JSON.stringify(value);
        assert.equal(run(f).error,"exact_held_item_required");assert.equal(f.writes.length,0);
    }
});
test("second invocation cannot reuse the reviewed ownership token",()=>{
    const f=fixture();assert.equal(run(f).status,"resolved_sent");
    assert.equal(run(f).error,"exact_held_item_required");assert.equal(f.writes.length,1);
});
test("queue change between verification and transaction refuses instead of resolving another snapshot",()=>{
    const f=fixture(),original=f.adapter.global;let reads=0;
    f.adapter.global=name=>{reads++;return reads===2 ? original(name)+" " : original(name);};
    assert.equal(run(f).error,"queue_changed_during_review");assert.equal(f.writes.length,0);
});
test("corrupt queue and failed storage readback fail closed",()=>{
    const corrupt=fixture();corrupt.values[queue.key]="broken";
    assert.equal(run(corrupt).error,"queue_corrupt");assert.equal(corrupt.writes.length,0);
    const stale=fixture();stale.adapter.setGlobal=()=>{};
    assert.equal(run(stale).error,"storage_write_unconfirmed");
});

function retryRequest(extra) {
    return request(Object.assign({
        resolution:"retry",retryConfirmation:"reviewed_prepare_no_selection",
        prepareResult:{version:1,stage:"prepare",status:"error",id:ID,owner:OWNER,
            selectionAttempted:false,sendAttempted:false,reason:"tap_target_occluded"}
    },extra));
}
test("supervised prepare-only retry invalidates ownership and preserves every unrelated queue item",()=>{
    const f=fixture(),before=JSON.parse(f.values[queue.key]);
    const result=run(f,retryRequest()),after=JSON.parse(f.values[queue.key]);
    assert.equal(result.status,"resolved_retry");
    assert.equal(after.items[1].state,"queued");assert.equal(after.items[1].owner,null);
    assert.deepEqual(after.items[0],before.items[0]);assert.deepEqual(after.items[2],before.items[2]);
    assert.equal(f.values.ManfredEyevueUiLedger,"keep-this-ledger");
    assert.equal(after.items.length,before.items.length);
    assert.equal(run(f,retryRequest()).error,"exact_held_item_required");
    const next=queue.apply(f.values[queue.key],{op:"claim",owner:"worker_new_claim"},1788652806000);
    assert.equal(next.result.image.id,ID);
    assert.equal(next.result.owner,"worker_new_claim");
    assert.throws(()=>queue.apply(next.json,{op:"begin_send",id:ID,owner:OWNER},1788652806000),/owner_mismatch/);
});
for(const [name,changes] of [
    ["selection may have occurred",{selectionAttempted:true}],
    ["send may have occurred",{sendAttempted:true}],
    ["selection field missing",{selectionAttempted:undefined}],
    ["string false is insufficient",{selectionAttempted:"false"}],
    ["attach stage",{stage:"attach_send"}],
    ["ambiguous result",{status:"ambiguous"}],
    ["no-op result",{status:"no_op"}],
    ["another item",{id:"99999999-1234-4234-8234-123456789abc"}],
    ["old owner",{owner:"worker_other_owner"}],
    ["unknown version",{version:2}]
]) test("retry rejects "+name+" without mutation",()=>{
    const f=fixture(),req=retryRequest();Object.assign(req.prepareResult,changes);
    const before=f.values[queue.key];
    assert.equal(run(f,req).error,"matching_prepare_no_selection_proof_required");
    assert.equal(f.values[queue.key],before);assert.equal(f.writes.length,0);
});
test("retry does not weaken stopped-task, held-only, owner or review requirements",()=>{
    for(const [extra,worker,dispatcher,code] of [
        [{confirmation:""},false,false,"manual_review_required"],
        [{retryConfirmation:""},false,false,"prepare_retry_review_required"],
        [{prepareResult:null},false,false,"matching_prepare_no_selection_proof_required"],
        [{},true,false,"worker_or_dispatcher_running"],
        [{},false,true,"worker_or_dispatcher_running"]
    ]) {
        const f=fixture(),before=f.values[queue.key];
        assert.equal(run(f,retryRequest(extra),worker,dispatcher).error,code);
        assert.equal(f.values[queue.key],before);assert.equal(f.writes.length,0);
    }
});
