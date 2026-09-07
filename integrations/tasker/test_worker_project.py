import json
import subprocess
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from generate_worker_project import build_worker, receipt_bundle, receipt_loader

HERE = Path(__file__).resolve().parent


class WorkerProjectTest(unittest.TestCase):
    def setUp(self):
        self.library = (HERE / "eyevue-queue.js").read_text()
        self.adapter = (HERE / "eyevue-tasker.js").read_text()
        self.flow = (HERE / "eyevue-worker-flow.js").read_text()
        template = ET.fromstring((HERE / "receipt-template.fixture.xml").read_bytes())
        template.find("Task/Action/App/appPkg").text = "com.openai.chatgpt"
        wait = ET.SubElement(template.find("Task"), "Action", {"sr": "act4", "ve": "7"})
        ET.SubElement(wait, "code").text = "30"
        for number, value in enumerate(["0", "2", "0", "0", "0"]):
            ET.SubElement(wait, "Int", {"sr": "arg" + str(number), "val": value})
        self.template = ET.tostring(template)
        self.xml = build_worker(self.template, self.library, self.adapter, self.flow,
                                'return "fixture_only";', 1788652800000)
        self.root = ET.fromstring(self.xml)

    def node(self, program, value):
        result = subprocess.run(["node", "-e", program], input=json.dumps(value), text=True,
                                capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_worker_dispatcher_ids_and_only_verified_stage_action_schemas(self):
        self.assertEqual(self.root.findall("Profile"), [])
        self.assertEqual(self.root.findtext("Project/tids"), "50,51")
        self.assertEqual(self.root.findtext("Project/name"), "Manfred_ChatGPT_Worker")
        worker, dispatcher = self.root.findall("Task")
        self.assertEqual(worker.findtext("id"), "50")
        self.assertEqual(worker.findtext("nme"), "Manfred Photo Worker")
        self.assertEqual(dispatcher.findtext("id"), "51")
        self.assertEqual(dispatcher.findtext("nme"), "Manfred Photo Dispatch")
        actions = worker.findall("Action")
        self.assertEqual([item.findtext("code") for item in actions],
                         ["129", "37", "20", "30", "474", "129", "37", "474", "129", "38", "38", "129"])
        self.assertEqual([item.get("sr") for item in actions], ["act" + str(i) for i in range(12)])
        for index in (4, 7):
            self.assertEqual(actions[index].findtext("se"), "false")
            self.assertEqual(actions[index].findtext("Str[@sr='arg1']"), "%mq_ui_result")
            self.assertEqual(actions[index].find("Int[@sr='arg2']").get("val"), "1")
        for index in (1, 6):
            self.assertEqual(actions[index].findtext("ConditionList/Condition/lhs"), "%mq_run")
            self.assertEqual(actions[index].findtext("ConditionList/Condition/op"), "2")
            self.assertEqual(actions[index].findtext("ConditionList/Condition/rhs"), "1")
        self.assertEqual(self.root.findall(".//Bundle"), [])
        self.assertIsNone(worker.find("rty"))

    def test_every_node_preserves_tasker_export_sr_then_ve_attribute_order(self):
        for element in self.root.iter():
            keys=list(element.attrib)
            if "sr" in keys:
                self.assertEqual(keys[0],"sr")
            if "ve" in keys:
                self.assertEqual(keys[1 if "sr" in keys else 0],"ve")
        self.assertIn(b'<Action sr="act1" ve="7">',self.xml)
        self.assertIn(b'<Action sr="act4" ve="7">',self.xml)
        self.assertIn(b'<Action sr="act9" ve="7">',self.xml)

    def test_eval_receipt_uses_tk_only_and_does_not_export_task_locals(self):
        bundle = receipt_bundle(self.library, self.adapter, self.flow)
        self.assertNotIn("tk.flash(",bundle)
        result = self.node(r"""
const vm=require("node:vm"),fs=require("node:fs");
const bundle=JSON.parse(fs.readFileSync(0,"utf8"));
const values={},writes=[],calls=[];
const context={bundle,tk:{
 global:name=>values[name],setGlobal:(name,value)=>{values[name]=value;},
 writeFile:(path,text,append)=>{writes.push({path,data:JSON.parse(text),append});},
 flash:()=>{},performTask:(...args)=>{calls.push(args);return true;}
},var_id:"12345678-1234-4234-8234-123456789abc",sessionid:"abcdef01-1234-4234-8234-123456789abc",
 uri:"content://media/external/images/media/1",filename:"EyeVue_1788652800000_12345678.jpg",
 width:"3200",height:"2400",bytes:"266148",sha:Array(255).concat(["a".repeat(64)]),detectedat:"1788652800000",receivedat:"1788652801000"};
vm.createContext(context);
vm.runInContext("eval(bundle)",context);
process.stdout.write(JSON.stringify({calls,writes,queue:JSON.parse(values.ManfredEyevueQueue),
 exported:typeof context.mq_id!=="undefined"}));
""", bundle)
        self.assertFalse(result["exported"])
        self.assertEqual(result["writes"][0]["data"]["status"], "queued")
        self.assertEqual(result["queue"]["items"][0]["state"], "queued")
        self.assertEqual(result["calls"], [["Manfred Photo Worker", 5, "", "", "", False, False, "", False]])


    def test_inline_loader_declares_all_event_locals_before_eval_bundle(self):
        loader = receipt_loader()
        result = self.node(r"""
const vm=require("node:vm"),fs=require("node:fs"),input=JSON.parse(fs.readFileSync(0,"utf8"));
const values={},calls=[];
const extras={var_id:"12345678-1234-4234-8234-123456789abc",sessionid:"abcdef01-1234-4234-8234-123456789abc",
 uri:"content://media/external/images/media/1",filename:"EyeVue_1788652800000_12345678.jpg",
 width:"3200",height:"2400",bytes:"266148",sha:Array(255).concat(["a".repeat(64)]),detectedat:"1788652800000",receivedat:"1788652801000"};
// Simulate importing only event names explicitly referenced by the inline source.
const ctx={readFile:path=>{if(path!=="/sdcard/Tasker/Manfred/receipt.js")throw Error("wrong path");return input.bundle;},
 tk:{global:n=>values[n],setGlobal:(n,v)=>{values[n]=v;},writeFile:()=>{},flash:()=>{},
 performTask:(...args)=>{calls.push(args);return true;}}};
for(const [name,value] of Object.entries(extras)) {
 if(new RegExp("\\bvar "+name+" = typeof "+name+"\\b").test(input.loader))ctx[name]=value;
}
vm.createContext(ctx);vm.runInContext(input.loader,ctx);
process.stdout.write(JSON.stringify({imported:Object.keys(extras).filter(n=>ctx[n]===extras[n]),
 queue:JSON.parse(values.ManfredEyevueQueue),calls}));
""", {"loader":loader,"bundle":receipt_bundle(self.library,self.adapter,self.flow)})
        self.assertEqual(len(result["imported"]),10)
        self.assertEqual(result["queue"]["items"][0]["state"],"queued")
        self.assertEqual(len(result["calls"]),1)

    def run_stages(self, status, voice=None):
        actions = self.root.find("Task").findall("Action")
        bodies = {str(i): actions[i].findtext("Str[@sr='arg0']") for i in (0, 5, 8, 11)}
        return self.node(r"""
const vm=require("node:vm"),fs=require("node:fs");
const input=JSON.parse(fs.readFileSync(0,"utf8")),values={},logs=[],calls=[];
const ctx={
 global:name=>values[name],setGlobal:(name,value)=>{values[name]=value;},
 writeFile:(path,text)=>logs.push({path,data:JSON.parse(text)}),
 performTask:(...args)=>{calls.push(args);return true;}
};
vm.createContext(ctx);
vm.runInContext(input.library,ctx);
const image={id:"12345678-1234-4234-8234-123456789abc",sessionId:"abcdef01-1234-4234-8234-123456789abc",
 uri:"content://media/external/images/media/1",fileName:"EyeVue_1788652800000_12345678.jpg",
 width:3200,height:2400,bytes:266148,sha256:"a".repeat(64),detectedAt:1788652800000,receivedAt:1788652801000};
ctx.EyevueQueue.transact({global:ctx.global,setGlobal:ctx.setGlobal},{op:"receipt",image},1788652800000);
vm.runInContext(input.bodies["0"],ctx);
const claimRun=ctx.mq_run,id=ctx.mq_id,owner=ctx.mq_owner;
ctx.mq_ui_result=JSON.stringify({version:1,stage:"prepare",status:"prepared",id,owner,
 selectionAttempted:false,sendAttempted:false});
vm.runInContext(input.bodies["5"],ctx);
const sendRun=ctx.mq_run,clearedBeforeJava=ctx.mq_ui_result==="";
ctx.mq_status="ui_permit_consumed"; // The actual Java worker consumes the single local permit.
ctx.mq_ui_result=JSON.stringify(Object.assign({version:1,stage:"attach_send",status:input.status,id,owner,
 selectionAttempted:true,sendAttempted:true,reason:"submission_observed"},input.voice));
vm.runInContext(input.bodies["8"],ctx);
vm.runInContext(input.bodies["11"],ctx);
process.stdout.write(JSON.stringify({claimRun,sendRun,clearedBeforeJava,queue:JSON.parse(values.ManfredEyevueQueue),calls,logs}));
""", {"library": self.library, "bodies": bodies, "status": status, "voice": voice or {}})

    @staticmethod
    def confirmed_evidence(**overrides):
        evidence = {
            "selectionActionCompleted": True, "sendActionCompleted": True,
            "submissionObserved": True, "newImageObserved": True,
            "networkValidated": True, "confirmationEnabled": True,
            "errorUiObserved": False, "uploadInProgress": False,
            "focusModeToggleAttempted": False, "focusModeToggleCompleted": False,
            "presentationVerifiedBeforeSend": True, "confirmationViewChanged": False,
        }
        evidence.update(overrides)
        return evidence

    def test_ambiguous_java_result_holds_and_only_defers_dispatcher(self):
        result = self.run_stages("ambiguous")
        self.assertEqual(result["claimRun"], "1")
        self.assertEqual(result["sendRun"], "1")
        self.assertTrue(result["clearedBeforeJava"])
        self.assertEqual(result["queue"]["items"][0]["state"], "held")
        self.assertEqual(result["calls"], [["Manfred Photo Dispatch", 4, "", "", "", False, False, "", False]])

    def test_only_positive_confirmed_java_result_marks_sent(self):
        result = self.run_stages("send_confirmed", self.confirmed_evidence())
        self.assertEqual(result["queue"]["items"][0]["state"], "sent")
        self.assertIsNone(result["queue"]["items"][0]["owner"])
        self.assertEqual(result["logs"][-1]["path"], "Tasker/manfred-worker-dispatch.json")

    def test_generated_worker_holds_status_only_or_incomplete_confirmation(self):
        for evidence in ({}, self.confirmed_evidence(newImageObserved=False),
                         self.confirmed_evidence(networkValidated=None)):
            with self.subTest(evidence=evidence):
                result = self.run_stages("send_confirmed", evidence)
                self.assertEqual(result["queue"]["items"][0]["state"], "held")
                self.assertIsNotNone(result["queue"]["items"][0]["owner"])
                self.assertEqual(result["calls"], [["Manfred Photo Dispatch", 4, "", "", "", False, False, "", False]])

    def test_generated_worker_holds_old_images_revealed_after_focus_toggle(self):
        result = self.run_stages("send_confirmed", self.confirmed_evidence(
            focusModeToggleAttempted=True, focusModeToggleCompleted=True,
            imagesBeforeSend=[], imagesAfterSend=[{"uniqueId": "old-photo"}]))
        self.assertEqual(result["queue"]["items"][0]["state"], "held")
        self.assertIsNotNone(result["queue"]["items"][0]["owner"])
        self.assertEqual(result["calls"], [["Manfred Photo Dispatch", 4, "", "", "", False, False, "", False]])

    def test_generated_reporting_preserves_held_live_voice_and_unknown_values(self):
        for voice in ({"voiceActiveAfter": True, "voiceNeedsResume": False}, {}):
            with self.subTest(voice=voice):
                result = self.run_stages("ambiguous", voice)
                report = next(entry["data"] for entry in result["logs"]
                              if entry["data"].get("stage") == "attach_send")
                self.assertEqual(report["voiceActiveAfter"], voice.get("voiceActiveAfter"))
                self.assertEqual(report["voiceNeedsResume"], voice.get("voiceNeedsResume"))
                self.assertEqual(result["queue"]["items"][0]["state"], "held")
                self.assertEqual(len(result["calls"]), 1)

    def test_empty_claim_disables_launch_and_prepare(self):
        body = self.root.find("Task/Action/Str[@sr='arg0']").text
        result = self.node(r"""
const vm=require("node:vm"),fs=require("node:fs"),body=JSON.parse(fs.readFileSync(0,"utf8"));
const ctx={global:()=>"",setGlobal:()=>{},writeFile:()=>{}};
vm.createContext(ctx);vm.runInContext(body,ctx);
process.stdout.write(JSON.stringify({run:ctx.mq_run,status:ctx.mq_status}));
""", body)
        self.assertEqual(result, {"run": "0", "status": "empty"})

    def test_empty_or_wrong_application_templates_are_rejected(self):
        with self.assertRaises(ValueError):
            build_worker(self.template, self.library, self.adapter, self.flow, "", 1)
        wrong = self.template.replace(b"com.openai.chatgpt", b"example.other")
        with self.assertRaises(ValueError):
            build_worker(wrong, self.library, self.adapter, self.flow, "return null;", 1)


if __name__ == "__main__":
    unittest.main()
