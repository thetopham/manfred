/* Local-only, synchronous queue logic. ES5 for Tasker's JavaScript(let) runtime. */
(function (root, factory) {
    if (typeof module === "object" && module.exports) module.exports = factory();
    else root.EyevueQueue = factory();
}(this, function () {
    "use strict";
    var KEY = "ManfredEyevueQueue";
    var MAX_ITEMS = 256; // Retain ID tombstones; refuse overflow rather than silently forgetting IDs.
    var UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
    var OWNER = /^[A-Za-z0-9_-]{8,96}$/;
    var STATES = ["queued", "claimed", "sending", "held", "sent", "discarded"];

    function fail(code) { throw new Error(code); }
    function integer(value, name) {
        if (typeof value === "string" && !/^[1-9][0-9]*$/.test(value)) fail("invalid_" + name);
        var number = Number(value);
        if ((typeof value !== "string" && typeof value !== "number") ||
            !isFinite(number) || Math.floor(number) !== number || number <= 0 ||
            number > 9007199254740991) fail("invalid_" + name);
        return number;
    }
    function uuid(value, name) {
        if (typeof value !== "string" || !UUID.test(value)) fail("invalid_" + name);
        return value;
    }
    function receipt(input) {
        if (!input || typeof input !== "object") fail("invalid_receipt");
        var id = uuid(input.id, "id");
        var file = typeof input.fileName === "string" &&
            /^EyeVue_([1-9][0-9]{0,15})_([0-9a-f]{8})\.jpg$/.exec(input.fileName);
        if (!file || file[2] !== id.slice(0, 8)) fail("invalid_filename");
        integer(file[1], "filename_timestamp");
        if (typeof input.uri !== "string" ||
            !/^content:\/\/media\/(?:external|external_primary)\/images\/media\/[1-9][0-9]*$/.test(input.uri)) {
            fail("invalid_uri");
        }
        if (typeof input.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(input.sha256)) fail("invalid_sha256");
        return {
            id: id,
            sessionId: uuid(input.sessionId, "session_id"),
            uri: input.uri,
            fileName: input.fileName,
            width: integer(input.width, "width"),
            height: integer(input.height, "height"),
            bytes: integer(input.bytes, "bytes"),
            sha256: input.sha256,
            detectedAt: integer(input.detectedAt, "detected_at"),
            receivedAt: integer(input.receivedAt, "received_at")
        };
    }
    function active(item) {
        return item.state === "claimed" || item.state === "sending" || item.state === "held";
    }
    function load(raw) {
        if (raw === undefined || raw === null || raw === "" || raw === "%" + KEY) {
            return { version: 1, items: [] };
        }
        if (typeof raw !== "string" || raw.length > 1000000) fail("queue_corrupt");
        var queue;
        try { queue = JSON.parse(raw); } catch (_) { fail("queue_corrupt"); }
        if (!queue || queue.version !== 1 || !Array.isArray(queue.items) ||
            queue.items.length > MAX_ITEMS) fail("queue_corrupt");
        var ids = [];
        var owners = 0;
        queue.items.forEach(function (item) {
            if (!item || STATES.indexOf(item.state) < 0) fail("queue_corrupt");
            try { item.image = receipt(item.image); } catch (_) { fail("queue_corrupt"); }
            if (ids.indexOf(item.image.id) >= 0) fail("queue_corrupt");
            ids.push(item.image.id);
            if (active(item)) {
                owners += 1;
                if (typeof item.owner !== "string" || !OWNER.test(item.owner)) fail("queue_corrupt");
            } else if (item.owner !== null) fail("queue_corrupt");
            try {
                item.addedAt = integer(item.addedAt, "queue_time");
                item.changedAt = integer(item.changedAt, "queue_time");
            } catch (_) { fail("queue_corrupt"); }
        });
        if (owners > 1) fail("queue_corrupt");
        return queue;
    }
    function summary(queue) {
        var result = { total: queue.items.length, queued: 0, sent: 0, discarded: 0, activeId: "", activeState: "" };
        queue.items.forEach(function (item) {
            if (active(item)) {
                result.activeId = item.image.id;
                result.activeState = item.state;
            } else result[item.state] += 1;
        });
        return result;
    }
    function owned(queue, command) {
        var item = queue.items.filter(active)[0];
        if (!item || item.image.id !== command.id || item.owner !== command.owner) fail("owner_mismatch");
        return item;
    }
    function apply(raw, command, now) {
        var queue = load(raw);
        var time = integer(now, "clock");
        var changed = false;
        var result = { ok: true, status: "", error: "" };
        var item;
        if (!command || typeof command.op !== "string") fail("invalid_operation");
        if (command.op === "receipt") {
            var image = receipt(command.image);
            item = queue.items.filter(function (entry) { return entry.image.id === image.id; })[0];
            if (item) {
                if (JSON.stringify(item.image) !== JSON.stringify(image)) fail("duplicate_conflict");
                result.status = "duplicate";
            } else {
                if (queue.items.length >= MAX_ITEMS) fail("queue_full");
                item = { image: image, state: "queued", owner: null, addedAt: time, changedAt: time };
                queue.items.push(item);
                changed = true;
                result.status = "queued";
            }
        } else if (command.op === "claim") {
            if (typeof command.owner !== "string" || !OWNER.test(command.owner)) fail("invalid_owner");
            if (queue.items.some(active)) result.status = "busy";
            else {
                item = queue.items.filter(function (entry) { return entry.state === "queued"; })[0];
                if (!item) result.status = "empty";
                else {
                    item.state = "claimed";
                    item.owner = command.owner;
                    item.changedAt = time;
                    changed = true;
                    result.status = "claimed";
                }
            }
        } else if (command.op === "begin_send" || command.op === "complete" || command.op === "hold") {
            item = owned(queue, command);
            if (command.op === "begin_send") {
                if (item.state !== "claimed") fail("send_already_attempted_or_held");
                item.state = "sending";
                result.status = "send_permitted";
            } else if (command.op === "complete") {
                if (item.state !== "sending" || command.confirmed !== true) fail("confirmation_required");
                item.state = "sent";
                item.owner = null;
                result.status = "sent";
            } else {
                item.state = "held";
                result.status = "held";
            }
            item.changedAt = time;
            changed = true;
        } else if (command.op === "resolve") {
            // Manual only: stop the old worker and inspect the destination before calling.
            if (command.confirmed !== true) fail("manual_review_required");
            item = queue.items.filter(active)[0];
            if (!item || item.image.id !== command.id) fail("active_id_mismatch");
            if (command.resolution === "retry") item.state = "queued";
            else if (command.resolution === "sent") item.state = "sent";
            else if (command.resolution === "discard") item.state = "discarded";
            else fail("invalid_resolution");
            item.owner = null;
            item.changedAt = time;
            changed = true;
            result.status = "resolved_" + command.resolution;
        } else if (command.op === "inspect") {
            result.status = queue.items.some(active) ? "blocked" :
                queue.items.some(function (entry) { return entry.state === "queued"; }) ? "ready" : "empty";
        } else fail("invalid_operation");
        result.counts = summary(queue);
        // Only the current operation's item is exposed, never another worker's active token.
        if (item) {
            result.image = item.image;
            result.owner = command.op === "receipt" ? "" : item.owner || "";
        }
        return { changed: changed, json: JSON.stringify(queue), result: result };
    }
    function transact(adapter, command, now) {
        try {
            var next = apply(adapter.global(KEY), command, now);
            if (next.changed) {
                adapter.setGlobal(KEY, next.json);
                // Never authorize a UI action unless the state write is observable.
                if (adapter.global(KEY) !== next.json) fail("storage_write_unconfirmed");
            }
            return next.result;
        } catch (error) {
            return { ok: false, status: "error", error: error && error.message || "queue_error" };
        }
    }
    return { key: KEY, maxItems: MAX_ITEMS, validateReceipt: receipt, apply: apply, transact: transact };
}));
