#!/usr/bin/env python3
"""Assemble the observed Tasker schemas with staged, fail-closed queue/Java UI gates."""
from __future__ import annotations

import argparse
import copy
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = "Manfred Photo Worker"
DISPATCHER = "Manfred Photo Dispatch"
CALL_ARGS = ',"","", "",false,false,"",false)'


def add(parent, tag, text):
    element = ET.SubElement(parent, tag)
    element.text = str(text)
    return element


def normalize_attribute_order(root):
    # Tasker's own exports put sr before ve. Preserve that exact convention:
    # an installed project stopped before its first ve-before-sr Action.
    # This is import compatibility, not an XML semantic requirement.
    for element in root.iter():
        attributes = element.attrib.copy()
        element.attrib.clear()
        for key in ("sr","ve"):
            if key in attributes:
                element.set(key,attributes.pop(key))
        element.attrib.update(attributes)


def js_action(body, index=0):
    action = ET.Element("Action", {"sr": "act" + str(index), "ve": "7"})
    add(action, "code", "129")
    ET.SubElement(action, "Str", {"sr": "arg0", "ve": "3"}).text = body
    ET.SubElement(action, "Str", {"sr": "arg1", "ve": "3"})
    ET.SubElement(action, "Int", {"sr": "arg2", "val": "1"})
    ET.SubElement(action, "Int", {"sr": "arg3", "val": "45"})
    return action


def java_action(code):
    action = ET.Element("Action", {"ve": "7"})
    add(action, "code", "474")
    add(action, "se", "false")  # Verified: Continue Task After Error checked in authored export.
    ET.SubElement(action, "Str", {"sr": "arg0", "ve": "3"}).text = code
    ET.SubElement(action, "Str", {"sr": "arg1", "ve": "3"}).text = "%mq_ui_result"
    ET.SubElement(action, "Int", {"sr": "arg2", "val": "1"})
    return action


def if_action():
    action = ET.Element("Action", {"ve": "7"})
    add(action, "code", "37")
    conditions = ET.SubElement(action, "ConditionList", {"sr": "if"})
    condition = ET.SubElement(conditions, "Condition", {"sr": "c0", "ve": "3"})
    add(condition, "lhs", "%mq_run")
    add(condition, "op", "2")
    add(condition, "rhs", "1")
    return action


def end_if():
    action = ET.Element("Action", {"ve": "7"})
    add(action, "code", "38")
    return action


def queue_prefix(library, flow=""):
    return library.rstrip() + "\n" + flow.rstrip() + "\n"


def body_claim(library, adapter):
    return queue_prefix(library) + 'var mq_op = "claim";\n' + adapter + r"""
var mq_run = mq_ok === "1" && mq_status === "claimed" ? "1" : "0";
var mq_stage = "prepare";
var mq_ui_result = "";
writeFile("Tasker/manfred-worker.json", JSON.stringify({stage:"claim",status:mq_status,error:mq_error,id:mq_id}),false);
"""


def body_after_prepare(library, adapter, flow):
    return queue_prefix(library, flow) + r"""
var mq_ui_decision = EyevueWorkerFlow.afterPrepare(
    typeof mq_ui_result === "undefined" ? "" : String(mq_ui_result), String(mq_id), String(mq_owner));
var mq_op = mq_ui_decision.op;
""" + adapter + r"""
var mq_run = mq_ok === "1" && mq_status === "send_permitted" ? "1" : "0";
var mq_stage = "attach_send";
var mq_ui_result = "";
writeFile("Tasker/manfred-worker.json", JSON.stringify({
    stage:"prepare",status:mq_status,error:mq_error,id:mq_id,reason:mq_ui_decision.reason
}),false);
"""


def body_after_attach(library, adapter, flow):
    return queue_prefix(library, flow) + r"""
var mq_ui_decision = EyevueWorkerFlow.afterAttach(
    typeof mq_ui_result === "undefined" ? "" : String(mq_ui_result), String(mq_id), String(mq_owner));
var mq_op = mq_ui_decision.op;
var mq_confirm = mq_ui_decision.confirmed === true ? "yes" : "";
""" + adapter + r"""
var mq_run = "0";
writeFile("Tasker/manfred-worker.json", JSON.stringify({
    stage:"attach_send",status:mq_status,error:mq_error,id:mq_id,reason:mq_ui_decision.reason,
    voiceNeedsResume:mq_ui_decision.voiceNeedsResume,
    voiceActiveAfter:mq_ui_decision.voiceActiveAfter,
    voiceContinuity:"unverified"
}),false);
"""


def body_dispatch(library, flow):
    return queue_prefix(library, flow) + r"""
var mq_dispatch_state = EyevueQueue.transact({
    global:function(name){return global(name);},
    setGlobal:function(name,value){setGlobal(name,value);}
},{op:"inspect"},Date.now());
var mq_worker_running = taskRunning("Manfred Photo Worker");
var mq_dispatch_requested = false;
if (EyevueWorkerFlow.shouldDispatch(mq_dispatch_state,mq_worker_running)) {
    mq_dispatch_requested = performTask("Manfred Photo Worker",5,"","", "",false,false,"",false);
}
writeFile("Tasker/manfred-dispatch.json",JSON.stringify({
    status:mq_dispatch_state.status,error:mq_dispatch_state.error || "",
    workerRunning:mq_worker_running,requested:mq_dispatch_requested
}),false);
"""


def body_final_kick():
    return r"""
// Always defer one fresh queue check until this worker has exited, even if it looked empty.
// A direct self-kick could be discarded by Tasker's abort-new collision rule.
var mq_dispatch_scheduled = performTask("Manfred Photo Dispatch",4,"","", "",false,false,"",false);
writeFile("Tasker/manfred-worker-dispatch.json",JSON.stringify({scheduled:mq_dispatch_scheduled}),false);
"""


def receipt_bundle(library, adapter, flow):
    return r"""/* Deployed as eval(readFile(...)); Tasker built-ins must use tk.* in eval. */
(function () {
var global = function(name) { return tk.global(name); };
var setGlobal = function(name,value) { tk.setGlobal(name,value); };
var mq_op = "receipt";
""" + queue_prefix(library, flow) + adapter + r"""
tk.writeFile('Tasker/manfred-receipt.json', JSON.stringify({
    status:mq_status,error:mq_error,id:mq_id,filename:mq_filename,width:mq_width,height:mq_height
}),false);
// Keep successful receipt handling silent so a toast cannot cover the UI worker.
if (EyevueWorkerFlow.shouldKickReceipt(mq_result)) {
    tk.performTask("Manfred Photo Worker",5,"","", "",false,false,"",false);
}
}).call(this);
"""



def receipt_loader(path="/sdcard/Tasker/Manfred/receipt.js"):
    """Inline references make the external bundle's event inputs visible to Tasker's scanner."""
    import json
    names = ("var_id","sessionid","uri","filename","width","height","bytes","detectedat","receivedat")
    return ("// Keep every intent-local reference inline; eval alone may not import event locals.\n" +
            "\n".join("var " + name + " = typeof " + name + ' === "undefined" ? "" : ' + name + ";"
                      for name in names) +
            '\nvar sha = typeof sha === "undefined" ? [] : sha;\n' +
            '\nvar sha256 = typeof sha256 === "undefined" ? undefined : sha256;\n' +
            "\neval(readFile(" + json.dumps(path) + "));\n")


def build_worker(template, library, adapter, flow, java_code, now_ms):
    source = ET.fromstring(template)
    if source.tag != "TaskerData":
        raise ValueError("Expected approved authored TaskerData export")
    launch = next((item for item in source.findall("Task/Action") if item.findtext("code") == "20"),None)
    wait = next((item for item in source.findall("Task/Action") if item.findtext("code") == "30"),None)
    if launch is None or wait is None or launch.findtext("App/appPkg") != "com.openai.chatgpt":
        raise ValueError("Approved template must contain the observed ChatGPT Launch App and Wait actions")
    if not java_code.strip():
        raise ValueError("Reviewed Java worker source is required")
    root = ET.Element("TaskerData",{key:source.attrib[key] for key in ("sr","dvi","tv") if key in source.attrib})
    project = ET.SubElement(root,"Project",{"sr":"proj0","ve":"2"})
    add(project,"cdate",now_ms)
    add(project,"name","Manfred_ChatGPT_Worker")
    add(project,"tids","50,51")
    worker = ET.SubElement(root,"Task",{"sr":"task50"})
    for tag,value in (("cdate",now_ms),("edate",now_ms),("id","50"),("nme",WORKER),("pri","5")):
        add(worker,tag,value)
    actions = [
        js_action(body_claim(library,adapter)),
        if_action(),
        copy.deepcopy(launch),
        copy.deepcopy(wait),
        java_action(java_code),
        js_action(body_after_prepare(library,adapter,flow)),
        if_action(),
        java_action(java_code),
        js_action(body_after_attach(library,adapter,flow)),
        end_if(),
        end_if(),
        js_action(body_final_kick()),
    ]
    for index,action in enumerate(actions):
        action.set("sr","act"+str(index))
        worker.append(action)
    dispatcher = ET.SubElement(root,"Task",{"sr":"task51"})
    for tag,value in (("cdate",now_ms),("edate",now_ms),("id","51"),("nme",DISPATCHER),("pri","4")):
        add(dispatcher,tag,value)
    dispatcher.append(js_action(body_dispatch(library,flow)))
    normalize_attribute_order(root)
    ET.indent(root,space="\t")
    return ET.tostring(root,encoding="utf-8",xml_declaration=False)+b"\n"


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template",type=Path,required=True)
    parser.add_argument("--worker",type=Path,default=HERE/"eyevue-ui-worker.java.txt")
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--receipt",type=Path,required=True)
    parser.add_argument("--loader",type=Path,help="Optional inline receipt action body for the deployed receipt path")
    parser.add_argument("--device-receipt-path",default="/sdcard/Tasker/Manfred/receipt.js")
    args=parser.parse_args()
    library=(HERE/"eyevue-queue.js").read_text()
    adapter=(HERE/"eyevue-tasker.js").read_text()
    flow=(HERE/"eyevue-worker-flow.js").read_text()
    xml=build_worker(args.template.read_bytes(),library,adapter,flow,args.worker.read_text(),int(time.time()*1000))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_bytes(xml)
    args.receipt.parent.mkdir(parents=True,exist_ok=True)
    args.receipt.write_text(receipt_bundle(library,adapter,flow))
    if args.loader:
        args.loader.parent.mkdir(parents=True,exist_ok=True)
        args.loader.write_text(receipt_loader(args.device_receipt_path))
    print("Generated:",args.output,args.receipt,args.loader or "")


if __name__=="__main__":
    main()
