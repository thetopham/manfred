#!/usr/bin/env python3
"""Generate a manual-only, exact-item Tasker replay resolver using observed JSlet schema."""
from __future__ import annotations

import argparse
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from generate_worker_project import add, js_action, normalize_attribute_order

HERE = Path(__file__).resolve().parent


def maintenance_body(library, policy):
    return library.rstrip() + "\n" + policy.rstrip() + r"""
var mr_result = (function () {
    var request;
    try {
        var input = typeof par1 !== "undefined" ? String(par1) :
            typeof par !== "undefined" && Array.isArray(par) ? String(par[0] || "") : "";
        if (input.length > 16384) throw new Error("maintenance_input_too_large");
        request = JSON.parse(input);
    } catch (_) {
        return {ok:false,status:"error",error:"explicit_request_json_required"};
    }
    try {
        return EyevueReplayMaintenance.resolve({
            global:function(name){return global(name);},
            setGlobal:function(name,value){setGlobal(name,value);}
        },request,taskRunning("Manfred Photo Worker"),taskRunning("Manfred Photo Dispatch"),Date.now());
    } catch (_) {
        return {ok:false,status:"error",error:"maintenance_runtime_error"};
    }
}());
var mr_status = mr_result.status || "error";
var mr_error = mr_result.error || "";
var mr_id = mr_result.ok && mr_result.image ? mr_result.image.id : "";
// Consume this invocation's proof input, including on failure. Never dispatch automatically.
var par1 = "";
var par = [];
writeFile("Tasker/manfred-replay-resolution.json",JSON.stringify({
    status:mr_status,error:mr_error,id:mr_id
}),false);
flash("Manfred Replay Resolve: " + mr_status + (mr_error ? " | " + mr_error : "") + " | " + mr_id);
"""


def build_maintenance(template, library, policy, now_ms):
    source = ET.fromstring(template)
    if source.tag != "TaskerData":
        raise ValueError("Expected approved authored TaskerData export")
    root = ET.Element("TaskerData",{key:source.attrib[key] for key in ("sr","dvi","tv") if key in source.attrib})
    project = ET.SubElement(root,"Project",{"sr":"proj0","ve":"2"})
    add(project,"cdate",now_ms)
    add(project,"name","Manfred_Replay_Resolve")
    add(project,"tids","52")
    task = ET.SubElement(root,"Task",{"sr":"task52"})
    for tag,value in (("cdate",now_ms),("edate",now_ms),("id","52"),("nme","Manfred Replay Resolve"),("pri","5")):
        add(task,tag,value)
    task.append(js_action(maintenance_body(library,policy)))
    normalize_attribute_order(root)
    ET.indent(root,space="\t")
    return ET.tostring(root,encoding="utf-8",xml_declaration=False)+b"\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args = parser.parse_args()
    xml = build_maintenance(args.template.read_bytes(),(HERE/"eyevue-queue.js").read_text(),
                            (HERE/"eyevue-replay-maintenance.js").read_text(),int(time.time()*1000))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_bytes(xml)
    print("Generated:",args.output)


if __name__ == "__main__":
    main()
