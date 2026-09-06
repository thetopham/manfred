#!/usr/bin/env python3
"""Generate a receipt-only Tasker project from an explicitly supplied authored export."""
from __future__ import annotations

import argparse
import copy
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACTION = "com.thetopham.manfred_companion.EYEVUE_IMAGE_READY"
NAME = "Manfred Photo Receipt"
PROJECT = "Manfred_Photo_Queue"
IDENTIFIER = "40"


def child(parent: ET.Element, tag: str, text: str) -> ET.Element:
    value = ET.SubElement(parent, tag)
    value.text = text
    return value


def build_project(template: bytes, library: str, adapter: str, now_ms: int) -> bytes:
    source = ET.fromstring(template)
    if source.tag != "TaskerData":
        raise ValueError("Expected an authored TaskerData project export")
    project_source = source.find("Project")
    profile_source = next(
        (item for item in source.findall("Profile") if item.findtext("Event/code") == "599"), None
    )
    action_source = next(
        (item for item in source.findall("Task/Action") if item.findtext("code") == "129"), None
    )
    if project_source is None or profile_source is None or action_source is None:
        raise ValueError("Template must contain an Intent Received profile and JavaScriptlet action")
    for tag, name in (("Str", "arg0"), ("Str", "arg1"), ("Int", "arg2"), ("Int", "arg3")):
        if action_source.find(tag + "[@sr='" + name + "']") is None:
            raise ValueError("Unrecognized JavaScriptlet argument schema")
    event_source = profile_source.find("Event")
    assert event_source is not None
    for tag, name in (("Str", "arg0"), ("Int", "arg1"), ("Int", "arg2"), ("Str", "arg3"), ("Str", "arg4")):
        if event_source.find(tag + "[@sr='" + name + "']") is None:
            raise ValueError("Unrecognized Intent Received argument schema")
    stamp = str(now_ms)
    root = ET.Element("TaskerData", {key: source.attrib[key] for key in ("sr", "dvi", "tv") if key in source.attrib})

    profile = ET.SubElement(root, "Profile", {"sr": "prof" + IDENTIFIER, "ve": profile_source.get("ve", "2")})
    child(profile, "cdate", stamp)
    if profile_source.find("clp") is not None:
        child(profile, "clp", profile_source.findtext("clp", "true"))
    child(profile, "edate", stamp)
    child(profile, "id", IDENTIFIER)
    child(profile, "mid0", IDENTIFIER)
    child(profile, "nme", NAME)
    event = copy.deepcopy(event_source)
    event.set("sr", "con0")
    event.find("Str[@sr='arg0']").text = ACTION
    for name in ("arg1", "arg2"):
        event.find("Int[@sr='" + name + "']").set("val", "0")
    for name in ("arg3", "arg4"):
        event.find("Str[@sr='" + name + "']").text = None
    profile.append(event)

    project = ET.SubElement(root, "Project", {"sr": "proj0", "ve": project_source.get("ve", "2")})
    child(project, "cdate", stamp)
    child(project, "name", PROJECT)
    child(project, "pids", IDENTIFIER)
    child(project, "tids", IDENTIFIER)

    task = ET.SubElement(root, "Task", {"sr": "task" + IDENTIFIER})
    child(task, "cdate", stamp)
    child(task, "edate", stamp)
    child(task, "id", IDENTIFIER)
    child(task, "nme", NAME)
    # Deliberately omit task collision settings: their enum has not been verified.
    action = copy.deepcopy(action_source)
    action.set("sr", "act0")
    # Do not retain arbitrary fields from another JavaScriptlet action.
    for element in list(action):
        if element.tag != "code" and element.get("sr") not in ("arg0", "arg1", "arg2", "arg3"):
            action.remove(element)
    body = (
        'var mq_op = "receipt";\n'
        + library.rstrip() + "\n\n" + adapter.rstrip() + "\n\n"
        + "writeFile('Tasker/manfred-receipt.json', JSON.stringify({status:mq_status,error:mq_error,"
        + "id:mq_id,filename:mq_filename,width:mq_width,height:mq_height}),false);\n"
        + 'flash("Manfred Photo Receipt: " + mq_status + (mq_error ? " | " + mq_error : "")'
        + ' + " | " + mq_filename + " | " + mq_width + " x " + mq_height);\n'
    )
    action.find("Str[@sr='arg0']").text = body
    action.find("Str[@sr='arg1']").text = None
    action.find("Int[@sr='arg2']").set("val", "1")
    action.find("Int[@sr='arg3']").set("val", "45")
    task.append(action)
    ET.indent(root, space="\t")
    return ET.tostring(root, encoding="utf-8", xml_declaration=False) + b"\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True, help="Explicitly approved authored Tasker export")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_project(
        args.template.read_bytes(),
        (HERE / "eyevue-queue.js").read_text(encoding="utf-8"),
        (HERE / "eyevue-tasker.js").read_text(encoding="utf-8"),
        int(time.time() * 1000),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(result)
    print("Generated receipt-only project:", args.output)


if __name__ == "__main__":
    main()
