import json
import subprocess
import unittest
from pathlib import Path
from generate_debug_retry import debug_retry_bundle

HERE=Path(__file__).resolve().parent
ID="12345678-1234-4234-8234-123456789abc"
OWNER="worker_current_debug"


class DebugRetryTest(unittest.TestCase):
    def setUp(self):
        self.bundle=debug_retry_bundle((HERE/"eyevue-queue.js").read_text(),
                                      (HERE/"eyevue-replay-maintenance.js").read_text(),ID)

    def execute(self, proof_changes=None, queue_owner=OWNER, worker=False, raw=None, twice=False):
        proof=dict(version=1,stage="prepare",status="error",id=ID,owner=OWNER,
                   selectionAttempted=False,sendAttempted=False,reason="tap_target_occluded")
        proof.update(proof_changes or {})
        script=r"""
const vm=require("node:vm"),fs=require("node:fs"),input=JSON.parse(fs.readFileSync(0,"utf8"));
const image={id:input.id,sessionId:"abcdef01-1234-4234-8234-123456789abc",
 uri:"content://media/external/images/media/1",fileName:"EyeVue_1788652800000_12345678.jpg",
 width:3200,height:2400,bytes:266148,sha256:"a".repeat(64),detectedAt:1788652800000,receivedAt:1788652801000};
const second=Object.assign({},image,{id:"99999999-1234-4234-8234-123456789abc",fileName:"EyeVue_1788652800000_99999999.jpg"});
const values={ManfredEyevueQueue:JSON.stringify({version:1,items:[
 {image,state:"held",owner:input.queueOwner,addedAt:1788652800000,changedAt:1788652801000},
 {image:second,state:"queued",owner:null,addedAt:1788652800000,changedAt:1788652801000}]}),
 ManfredEyevueUiLedger:"untouched"};
const before=JSON.parse(values.ManfredEyevueQueue),writes=[],logs=[],reads=[];
const ctx={tk:{global:n=>values[n],setGlobal:(n,v)=>{writes.push(n);values[n]=v;},
 taskRunning:()=>input.worker,readFile:path=>{reads.push(path);return input.raw;},
 writeFile:(path,text)=>logs.push(JSON.parse(text)),flash:()=>{}}};
vm.createContext(ctx);vm.runInContext(input.bundle,ctx);
if(input.twice)vm.runInContext(input.bundle,ctx);
process.stdout.write(JSON.stringify({before,after:JSON.parse(values.ManfredEyevueQueue),writes,logs,reads,ledger:values.ManfredEyevueUiLedger}));
"""
        payload=dict(id=ID,queueOwner=queue_owner,worker=worker,
                     raw=json.dumps(proof) if raw is None else raw,twice=twice,bundle=self.bundle)
        out=subprocess.run(["node","-e",script],input=json.dumps(payload),text=True,capture_output=True,check=True)
        return json.loads(out.stdout)

    def test_current_saved_prepare_owner_retries_only_controlled_item_once(self):
        result=self.execute(twice=True)
        self.assertEqual(result["logs"][0]["status"],"resolved_retry")
        self.assertEqual(result["logs"][1]["error"],"exact_held_item_required")
        self.assertEqual(result["after"]["items"][0]["state"],"queued")
        self.assertEqual(result["after"]["items"][1],result["before"]["items"][1])
        self.assertEqual(result["ledger"],"untouched")
        self.assertEqual(result["writes"],["ManfredEyevueQueue"])
        self.assertTrue(all(path=="/sdcard/Tasker/manfred-ui-worker.json" for path in result["reads"]))

    def test_new_owner_is_allowed_only_when_both_full_result_and_current_held_item_match(self):
        result=self.execute(proof_changes={"owner":"worker_next_debug"},queue_owner="worker_next_debug")
        self.assertEqual(result["logs"][0]["status"],"resolved_retry")
        stale=self.execute(queue_owner="worker_next_debug")
        self.assertEqual(stale["logs"][0]["error"],"owner_mismatch")
        self.assertEqual(stale["writes"],[])

    def test_nonmatching_or_unsafe_proof_and_running_tasks_do_not_mutate(self):
        cases=[
            ({"proof_changes":{"id":"99999999-1234-4234-8234-123456789abc"}},"controlled_result_id_mismatch"),
            ({"proof_changes":{"stage":"attach_send"}},"matching_prepare_no_selection_proof_required"),
            ({"proof_changes":{"status":"ambiguous"}},"matching_prepare_no_selection_proof_required"),
            ({"proof_changes":{"selectionAttempted":True}},"matching_prepare_no_selection_proof_required"),
            ({"proof_changes":{"sendAttempted":True}},"matching_prepare_no_selection_proof_required"),
            ({"raw":"broken"},"saved_worker_result_invalid_json"),
            ({"raw":"x"*16385},"bounded_worker_result_required"),
            ({"worker":True},"worker_or_dispatcher_running")
        ]
        for kwargs,expected in cases:
            with self.subTest(kwargs=kwargs):
                result=self.execute(**kwargs)
                self.assertEqual(result["logs"][0]["error"],expected)
                self.assertEqual(result["writes"],[])
                self.assertEqual(result["before"],result["after"])

    def test_only_fixed_file_is_read_and_no_dispatch_or_reset_is_available(self):
        self.assertNotIn("performTask(",self.bundle)
        self.assertNotIn("deleteFile(",self.bundle)
        result=self.execute()
        self.assertFalse(result["logs"][0]["automaticDispatch"])
        self.assertEqual(result["logs"][0]["evidenceSource"],"saved_full_prepare_result_manual_debug")


if __name__=="__main__":
    unittest.main()
