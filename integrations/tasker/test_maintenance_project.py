import json
import subprocess
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from generate_maintenance_project import build_maintenance

HERE = Path(__file__).resolve().parent


class MaintenanceProjectTest(unittest.TestCase):
    def setUp(self):
        self.xml = build_maintenance(
            (HERE/"receipt-template.fixture.xml").read_bytes(),
            (HERE/"eyevue-queue.js").read_text(),
            (HERE/"eyevue-replay-maintenance.js").read_text(),1788652800000)
        self.root = ET.fromstring(self.xml)

    def test_isolated_manual_task_has_no_profile_dispatch_launch_or_other_action(self):
        self.assertEqual(self.root.findall("Profile"),[])
        self.assertEqual(self.root.findtext("Project/tids"),"52")
        self.assertEqual(self.root.findtext("Task/id"),"52")
        self.assertEqual(self.root.findtext("Task/nme"),"Manfred Replay Resolve")
        self.assertEqual([a.findtext("code") for a in self.root.findall("Task/Action")],["129"])
        body=self.root.findtext("Task/Action/Str[@sr='arg0']")
        self.assertNotIn("performTask(",body)
        self.assertNotIn("readFile(",body)
        self.assertIn('taskRunning("Manfred Photo Worker")',body)
        self.assertIn('taskRunning("Manfred Photo Dispatch")',body)

    def test_missing_or_invalid_parameter_performs_no_queue_write_and_clears_proof_input(self):
        body=self.root.findtext("Task/Action/Str[@sr='arg0']")
        script=r"""
const vm=require("node:vm"),fs=require("node:fs"),body=JSON.parse(fs.readFileSync(0,"utf8"));
const results=[];
for(const input of [undefined,"","not-json",'{"resolution":"retry"}']) {
 const writes=[],logs=[],ctx={par1:input,global:()=>"",setGlobal:(...a)=>writes.push(a),
 taskRunning:()=>false,writeFile:(path,text)=>logs.push(JSON.parse(text)),flash:()=>{}};
 vm.createContext(ctx);vm.runInContext(body,ctx);
 results.push({writes,logs,par1:ctx.par1});
}
process.stdout.write(JSON.stringify(results));
"""
        result=subprocess.run(["node","-e",script],input=json.dumps(body),text=True,capture_output=True,check=True)
        for item in json.loads(result.stdout):
            self.assertEqual(item["writes"],[])
            self.assertEqual(item["par1"],"")
            self.assertEqual(item["logs"][0]["status"],"error")

    def test_task_parameter_resolves_one_held_receipt_with_no_dispatch(self):
        body=self.root.findtext("Task/Action/Str[@sr='arg0']")
        script=r"""
const vm=require("node:vm"),fs=require("node:fs"),body=JSON.parse(fs.readFileSync(0,"utf8"));
const id="12345678-1234-4234-8234-123456789abc",owner="worker_controlled_123";
const image={id,sessionId:"abcdef01-1234-4234-8234-123456789abc",
 uri:"content://media/external/images/media/1",fileName:"EyeVue_1788652800000_12345678.jpg",
 width:3200,height:2400,bytes:266148,sha256:"a".repeat(64),detectedAt:1788652800000,receivedAt:1788652801000};
const values={ManfredEyevueQueue:JSON.stringify({version:1,items:[{image,state:"held",owner,
 addedAt:1788652800000,changedAt:1788652801000}]}),ManfredEyevueUiLedger:"untouched"};
const logs=[],ctx={par:[JSON.stringify({id,owner,resolution:"sent",confirmation:"reviewed_and_worker_stopped"})],
 global:n=>values[n],setGlobal:(n,v)=>{values[n]=v;},taskRunning:()=>false,
 writeFile:(path,text)=>logs.push(JSON.parse(text)),flash:()=>{}};
vm.createContext(ctx);vm.runInContext(body,ctx);
process.stdout.write(JSON.stringify({values,logs,par1:ctx.par1}));
"""
        result=json.loads(subprocess.run(["node","-e",script],input=json.dumps(body),text=True,capture_output=True,check=True).stdout)
        self.assertEqual(json.loads(result["values"]["ManfredEyevueQueue"])["items"][0]["state"],"sent")
        self.assertEqual(result["values"]["ManfredEyevueUiLedger"],"untouched")
        self.assertEqual(result["logs"][0]["status"],"resolved_sent")
        self.assertEqual(result["par1"],"")


if __name__ == "__main__":
    unittest.main()
