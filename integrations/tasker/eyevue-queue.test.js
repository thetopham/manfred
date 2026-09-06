/* Run with: node --test integrations/tasker/eyevue-queue.test.js */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const queue = require("./eyevue-queue.js");

const NOW = 1788652800000;
const OWNER = "worker_original";
function image(index = 1) {
    const prefix = index.toString(16).padStart(8, "0");
    return {
        id: prefix + "-1234-4234-8234-123456789abc",
        sessionId: "abcdef01-1234-4234-8234-123456789abc",
        uri: "content://media/external/images/media/" + index,
        fileName: "EyeVue_" + NOW + "_" + prefix + ".jpg",
        width: "3200", height: "2400", bytes: "266148",
        sha256: "a".repeat(64), detectedAt: String(NOW), receivedAt: String(NOW + 1000),
        cachePath: "/private/cache/must-not-be-queued.jpg"
    };
}
function store(initial = "") {
    let raw = initial;
    return {
        global: name => { assert.equal(name, queue.key); return raw; },
        setGlobal: (name, value) => { assert.equal(name, queue.key); raw = value; },
        raw: () => raw
    };
}
function run(db, command, time = NOW) { return queue.transact(db, command, time); }
function enqueue(db, entry = image()) { return run(db, { op: "receipt", image: entry }); }
function claim(db, owner = OWNER) { return run(db, { op: "claim", owner }); }
function begin(db, owner = OWNER, id = image().id) { return run(db, { op: "begin_send", owner, id }); }

test("actual receipt fields normalize numbers and exclude private cache paths", () => {
    const db = store();
    const result = enqueue(db);
    assert.equal(result.status, "queued");
    assert.equal(result.image.width, 3200);
    assert.equal(result.image.bytes, 266148);
    assert.ok(!db.raw().includes("cachePath"));
    assert.equal(result.image.fileName, image().fileName);
});
test("only local MediaStore image item URIs are accepted", () => {
    const bad = [
        "https://example.invalid/photo.jpg", "file:///data/user/0/private.jpg",
        "content://other.provider/external/images/media/1",
        "content://media/external/video/media/1", "content://media/external/images/media/0",
        "content://media/external/images/media/1?redirect=1",
        "content://media/external/images/media/1#fragment",
        "content://media/external/images/media/%31"
    ];
    bad.forEach(uri => {
        const db = store();
        assert.equal(enqueue(db, { ...image(), uri }).error, "invalid_uri");
        assert.equal(db.raw(), "");
    });
    assert.equal(enqueue(store(), { ...image(), uri: "content://media/external_primary/images/media/2" }).ok, true);
});
test("filename must be the exact UUID-derived native JPEG name", () => {
    ["../EyeVue_1788652800000_00000001.jpg", "EyeVue_1788652800000_deadbeef.jpg",
        "EyeVue_1788652800000_00000001 (1).jpg", "thumbnail.jpg",
        "EyeVue_1788652800000_00000001.png"].forEach(fileName => {
        assert.equal(enqueue(store(), { ...image(), fileName }).error, "invalid_filename");
    });
});
test("missing, fractional, nondecimal, unsafe and malformed metadata fail closed", () => {
    for (const value of [null, undefined, false, "", "%width", "0", "-1", "1.2", "0x10", "1e3", 1.5, Infinity, 9007199254740992]) {
        assert.equal(enqueue(store(), { ...image(), width: value }).ok, false);
    }
    assert.equal(enqueue(store(), { ...image(), id: "%var_id" }).error, "invalid_id");
    assert.equal(enqueue(store(), { ...image(), sha256: "a".repeat(63) }).error, "invalid_sha256");
    assert.equal(enqueue(store(), { ...image(), sessionId: "unknown" }).error, "invalid_session_id");
});
test("duplicate receipt does not requeue or overwrite a completed ID", () => {
    const db = store();
    enqueue(db); claim(db); begin(db);
    assert.equal(run(db, { op: "complete", owner: OWNER, id: image().id, confirmed: true }).status, "sent");
    const saved = db.raw();
    assert.equal(enqueue(db).status, "duplicate");
    assert.equal(db.raw(), saved);
    assert.equal(claim(db, "worker_next").status, "empty");
});
test("same ID with conflicting metadata is an error without overwriting history", () => {
    const db = store();
    enqueue(db);
    const saved = db.raw();
    assert.equal(enqueue(db, { ...image(), sha256: "b".repeat(64) }).error, "duplicate_conflict");
    assert.equal(db.raw(), saved);
});
test("two workers cannot own the queue and a different worker cannot advance it", () => {
    const db = store();
    enqueue(db); enqueue(db, image(2));
    assert.equal(claim(db).status, "claimed");
    const loser = claim(db, "worker_second");
    assert.equal(loser.status, "busy");
    assert.equal(loser.owner, undefined);
    assert.equal(loser.image, undefined);
    assert.equal(begin(db, "worker_second").error, "owner_mismatch");
    assert.equal(begin(db).status, "send_permitted");
});
test("restarting or waiting indefinitely never reclaims a claimed or ambiguous send", () => {
    const db = store();
    enqueue(db); claim(db);
    assert.equal(claim(store(db.raw()), "worker_restart").status, "busy");
    assert.equal(begin(db).status, "send_permitted");
    const restarted = store(db.raw());
    assert.equal(run(restarted, { op: "claim", owner: "worker_restart" }, NOW + 86400000).status, "busy");
    assert.equal(begin(restarted).error, "send_already_attempted_or_held");
});
test("send state is written and read back before permission is returned", () => {
    const db = store();
    enqueue(db); claim(db);
    const original = db.raw();
    const noWrite = { global: () => original, setGlobal: () => {} };
    assert.equal(begin(noWrite).error, "storage_write_unconfirmed");
    assert.equal(JSON.parse(original).items[0].state, "claimed");
    const result = begin(db);
    assert.equal(result.status, "send_permitted");
    assert.equal(JSON.parse(db.raw()).items[0].state, "sending");
});
test("write exception after persistence still cannot authorize or repeat a send", () => {
    const db = store();
    enqueue(db); claim(db);
    const faulty = { global: db.global, setGlobal: (name, value) => {
        db.setGlobal(name, value);
        throw new Error("simulated_write_failure");
    }};
    assert.equal(begin(faulty).ok, false);
    assert.equal(JSON.parse(db.raw()).items[0].state, "sending");
    assert.equal(begin(db).error, "send_already_attempted_or_held");
});
test("completion needs positive confirmation; a held item blocks later photos", () => {
    const db = store();
    enqueue(db); enqueue(db, image(2)); claim(db); begin(db);
    assert.equal(run(db, { op: "complete", owner: OWNER, id: image().id }).error, "confirmation_required");
    assert.equal(run(db, { op: "hold", owner: OWNER, id: image().id }).status, "held");
    assert.equal(claim(db, "worker_second").status, "busy");
    assert.equal(begin(db).error, "send_already_attempted_or_held");
});
test("manual retry invalidates the previous owner and keeps FIFO order", () => {
    const db = store();
    enqueue(db); enqueue(db, image(2)); claim(db); begin(db);
    assert.equal(run(db, { op: "resolve", id: image().id, resolution: "retry" }).error, "manual_review_required");
    assert.equal(run(db, { op: "resolve", id: image().id, resolution: "retry", confirmed: true }).status, "resolved_retry");
    assert.equal(claim(db, "worker_recovered").image.id, image().id);
    assert.equal(begin(db, OWNER).error, "owner_mismatch");
    assert.equal(begin(db, "worker_recovered").status, "send_permitted");
});
test("manual sent/discard resolutions keep dedup tombstones", () => {
    for (const resolution of ["sent", "discard"]) {
        const db = store();
        enqueue(db); claim(db); begin(db);
        assert.equal(run(db, { op: "resolve", id: image().id, resolution, confirmed: true }).ok, true);
        assert.equal(claim(db, "worker_next").status, "empty");
        assert.equal(enqueue(db).status, "duplicate");
    }
});
test("corrupt or inconsistent durable state is never reset automatically", () => {
    const db = store("{broken");
    assert.equal(enqueue(db).error, "queue_corrupt");
    assert.equal(db.raw(), "{broken");
    const good = store();
    enqueue(good); claim(good);
    const data = JSON.parse(good.raw());
    data.items.push({ ...data.items[0], image: queue.validateReceipt(image(2)) });
    const inconsistent = store(JSON.stringify(data));
    assert.equal(claim(inconsistent).error, "queue_corrupt");
});
test("ledger limit preserves old IDs and refuses new receipts without dropping them silently", () => {
    const db = store();
    for (let i = 1; i <= queue.maxItems; i++) assert.equal(enqueue(db, image(i)).ok, true);
    const saved = db.raw();
    assert.equal(enqueue(db, image(queue.maxItems + 1)).error, "queue_full");
    assert.equal(db.raw(), saved);
    assert.equal(enqueue(db).status, "duplicate");
});
test("Tasker adapter reads var_id/camelcase-lowered extras and preserves output ownership", () => {
    const db = store();
    const entry = image();
    const context = {
        global: db.global, setGlobal: db.setGlobal, mq_op: "receipt",
        var_id: entry.id, sessionid: entry.sessionId, uri: entry.uri, filename: entry.fileName,
        width: entry.width, height: entry.height, bytes: entry.bytes, sha256: entry.sha256,
        detectedat: entry.detectedAt, receivedat: entry.receivedAt
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, "eyevue-queue.js"), "utf8"), context);
    const adapter = fs.readFileSync(path.join(__dirname, "eyevue-tasker.js"), "utf8");
    vm.runInContext(adapter, context);
    assert.equal(context.mq_status, "queued");
    context.mq_op = "claim";
    vm.runInContext(adapter, context);
    const owner = context.mq_owner;
    assert.match(owner, /^worker_/);
    context.mq_op = "begin_send";
    vm.runInContext(adapter, context);
    assert.equal(context.mq_status, "send_permitted");
    assert.equal(context.mq_owner, owner);
    context.mq_op = "complete";
    context.mq_confirm = "yes";
    vm.runInContext(adapter, context);
    assert.equal(context.mq_status, "sent");
    assert.equal(context.mq_confirm, "");
});
test("adapter error clears stale URI, owner and send permission", () => {
    const context = { mq_op: "begin_send", mq_status: "send_permitted", mq_uri: "stale",
        mq_owner: OWNER, mq_id: image().id, mq_confirm: "yes" };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, "eyevue-tasker.js"), "utf8"), context);
    assert.equal(context.mq_ok, "0");
    assert.equal(context.mq_error, "queue_library_unavailable");
    assert.equal(context.mq_uri, "");
    assert.equal(context.mq_owner, "");
    assert.equal(context.mq_confirm, "");
});

test("duplicate receipt cannot borrow the existing worker ownership token", () => {
    const db = store();
    enqueue(db); claim(db);
    const duplicate = enqueue(db);
    assert.equal(duplicate.status, "duplicate");
    assert.equal(duplicate.owner, "");
    assert.equal(begin(db, duplicate.owner).error, "owner_mismatch");
    assert.equal(begin(db).status, "send_permitted");
});
test("receipt arriving during a worker survives all later worker transitions", () => {
    const db = store();
    enqueue(db); claim(db);
    enqueue(db, image(2));
    begin(db);
    run(db, { op: "complete", owner: OWNER, id: image().id, confirmed: true });
    assert.equal(claim(db, "worker_second").image.id, image(2).id);
    assert.equal(JSON.parse(db.raw()).items.length, 2);
});

test("Tasker numeric-suffix checksum imports from sha[255] and exports mq_sha[255]", () => {
    const vm = require("node:vm");
    const fs = require("node:fs");
    const path = require("node:path");
    const values = {};
    const sha = [];
    sha[255] = "a".repeat(64);
    const ctx = {sha,mq_op:"receipt",var_id:"12345678-1234-4234-8234-123456789abc",
        sessionid:"abcdef01-1234-4234-8234-123456789abc",uri:"content://media/external/images/media/1",
        filename:"EyeVue_1788652800000_12345678.jpg",width:"3200",height:"2400",bytes:"266148",
        detectedat:"1788652800000",receivedat:"1788652801000",
        global:n=>values[n],setGlobal:(n,v)=>{values[n]=v;}};
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(path.join(__dirname,"eyevue-queue.js"),"utf8"),ctx);
    const adapter = fs.readFileSync(path.join(__dirname,"eyevue-tasker.js"),"utf8");
    vm.runInContext(adapter,ctx);
    assert.equal(ctx.mq_status,"queued");
    assert.equal(JSON.parse(values.ManfredEyevueQueue).items[0].image.sha256,"a".repeat(64));
    assert.equal(ctx.mq_sha[255],"a".repeat(64));
    // Tasker serializes JS index 255 as local %mq_sha256; Java reads that exact variable.
    const taskerLocals={};
    ctx.mq_sha.forEach((value,index)=>{taskerLocals["mq_sha"+(index+1)]=value;});
    assert.equal(taskerLocals.mq_sha256,"a".repeat(64));
    ctx.mq_op="claim";vm.runInContext(adapter,ctx);
    assert.equal(ctx.mq_sha[255],"a".repeat(64));
    ctx.mq_op="claim";vm.runInContext(adapter,ctx);
    assert.equal(ctx.mq_status,"busy");
    assert.equal(ctx.mq_sha.length,0);
    assert.equal(ctx.mq_sha256,"");
});
