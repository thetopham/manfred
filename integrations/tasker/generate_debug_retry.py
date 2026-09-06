#!/usr/bin/env python3
"""Generate a manual debugger retry pinned to one controlled ID and matching saved prepare result."""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path

HERE=Path(__file__).resolve().parent


def debug_retry_bundle(library, policy, item_id):
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",item_id):
        raise ValueError("Exact controlled item UUID required")
    return r"""/* MANUAL DEBUGGING ONLY. Fixed controlled photo ID; no profile or dispatch.
 * The owner comes only from the saved full result and must match the current held owner.
 */
(function () {
""" + library.rstrip() + "\n" + policy.rstrip() + "\nvar controlledId = " + json.dumps(item_id) + r""";
var outcome;
try {
    if (tk.taskRunning("Manfred Photo Worker") !== false ||
        tk.taskRunning("Manfred Photo Dispatch") !== false) throw new Error("worker_or_dispatcher_running");
    var rawResult = tk.readFile("/sdcard/Tasker/manfred-ui-worker.json");
    if (typeof rawResult !== "string" || rawResult.length > 16384) throw new Error("bounded_worker_result_required");
    var proof;
    try { proof=JSON.parse(rawResult); } catch (_) { throw new Error("saved_worker_result_invalid_json"); }
    if (!proof || proof.id !== controlledId) throw new Error("controlled_result_id_mismatch");
    outcome=EyevueReplayMaintenance.resolve({
        global:function(name){return tk.global(name);},
        setGlobal:function(name,value){tk.setGlobal(name,value);}
    },{
        id:controlledId,owner:proof.owner,resolution:"retry",
        confirmation:"reviewed_and_worker_stopped",
        retryConfirmation:"reviewed_prepare_no_selection",prepareResult:proof
    },tk.taskRunning("Manfred Photo Worker"),tk.taskRunning("Manfred Photo Dispatch"),Date.now());
} catch(error) {
    outcome={ok:false,status:"error",error:error && error.message || "manual_debug_retry_error"};
}
tk.writeFile("Tasker/manfred-replay-resolution.json",JSON.stringify({
    status:outcome.status,error:outcome.error || "",id:controlledId,
    evidenceSource:"saved_full_prepare_result_manual_debug",
    resolution:"retry",automaticDispatch:false
}),false);
tk.flash("Manfred manual debug retry: " + outcome.status + (outcome.error ? " | " + outcome.error : ""));
}).call(this);
"""


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id",required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    bundle=debug_retry_bundle((HERE/"eyevue-queue.js").read_text(),
                              (HERE/"eyevue-replay-maintenance.js").read_text(),args.id)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(bundle)
    print("Generated:",args.output)


if __name__=="__main__":
    main()
