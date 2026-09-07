#!/usr/bin/env python3
"""Build the portable Manfred EyeVue Tasker test bundle from approved authored schemas."""
from __future__ import annotations

import argparse
import hashlib
import io
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from generate_receipt_project import build_project
from generate_worker_project import build_worker, normalize_attribute_order, receipt_bundle, receipt_loader

HERE=Path(__file__).resolve().parent
TEMPLATE_SHA256="d5830a2075330123fc272da10698c6bcf446c2f254a3d77959a3bffc9a311ce6"
LEGACY_AUTHORED_TEMPLATE_SHA256="43c68ecca382a498489df3b541ad05135702ed099d005d3aa7ec46a8ca875789"
JAVA_PATH="/sdcard/Tasker/Manfred/eyevue-ui-worker.java.txt"
BUNDLE_NAME="Tasker-Manfred-test.zip"


def xml_bytes(root):
    normalize_attribute_order(root)
    ET.indent(root,space="\t")
    return ET.tostring(root,encoding="utf-8",xml_declaration=False)+b"\n"


def install_readme(worker_sha):
    return f"""# Manfred EyeVue → ChatGPT Tasker test bundle

Experimental integration for Manfred EyeVue image receipts, including BLE previews and Wi-Fi originals, using the Android 16 / Tasker 6.6.20 interface. This revision requires supervised phone acceptance before unattended use. It operates the currently open ChatGPT conversation and does not create a new chat; continuous voice audio is unverified.

1. For an existing installation, disable Manfred Photo Receipt and stop Manfred Photo Worker and Manfred Photo Dispatch before updating. Preserve the queue, UI ledger, completion switch and existing projects; resolve historical held records separately using exact ownership and reviewed evidence. Extract the archive. Copy its **Tasker** folder into the phone's internal-storage root, producing /sdcard/Tasker/Manfred/receipt.js and /sdcard/Tasker/Manfred/eyevue-ui-worker.java.txt. Keep both files together. Give Tasker access to these scripts and its diagnostic files; enable Tasker's accessibility service for the UI worker.
2. Import **Manfred_ChatGPT_Worker.prj.xml**, then **Manfred_Photo_Queue.prj.xml** in Tasker. Keep the receipt profile disabled while checking setup. Inspect the imported tasks: Manfred Photo Worker has **12 actions**, Manfred Photo Dispatch has **1**, and Manfred Photo Receipt has **1**. The generators preserve Tasker 6.6.20's required sr-before-ve export ordering.
3. Set Manfred Photo Receipt collision handling to **Run Both Together**. Keep the worker and dispatcher at the default **Abort New Task**. The projects use profile/task 40, worker task 50 and dispatcher task 51; check names before replacing an existing project. The receipt action already embeds the supplied inline loader; do not replace it with a bare eval-only call.
4. On a new installation, leave **%ManfredEyevueUiConfirmed unset** for the first supervised run. Keep the phone unlocked and the intended existing ChatGPT Live conversation available. Ensure its transcript has a completed response with visible response controls; an empty transcript is unsupported by this revision. Enable the receipt profile and start Manfred's Instant BLE preview session. Use Take preview in the app, or the physical glasses button on a build supporting that trigger. A preview requested after the physical shutter is a separate exposure. Wi-Fi originals use the same image-receipt pipeline. A completed native JPEG event provides the actual URI permission and metadata. Verify the exact image, selection, submission/reply and voice state on the phone. The queue holds uncertain results; do not clear it or resend automatically.
5. After a verified installed-UI acceptance test, the completion switch may be set explicitly to **verified_on_device_v1**. This enables only the worker's full UI-evidence predicate; it is not a server receipt or a promise of voice continuity. A changed UI, connection failure or insufficient evidence still holds the item. To stop future receipts, disable Manfred Photo Receipt; stop an already running worker separately in Tasker.

The Java actions use the tested source("/sdcard/Tasker/Manfred/eyevue-ui-worker.java.txt") path. The receipt loader explicitly imports intent locals, including Tasker's sha[255] checksum array element. Production receipts write a JSON diagnostic without a Flash that could obscure controls.

Local diagnostics are under Tasker/: manfred-receipt.json, manfred-worker.json, manfred-ui-worker.json, manfred-worker-dispatch.json and manfred-dispatch.json. The queue is %ManfredEyevueQueue and its UI ownership ledger is separate. There is no automatic retry after ambiguous selection/send. Stop the old worker and inspect the exact item before manual recovery.

This archive includes only portable project schemas and source code. It contains no queue export, personal test IDs, photo metadata, images, credentials or recovery scripts. Importing it does not clear an existing queue or change the completion switch. Native receipt, background networking, upload and ongoing voice still need end-to-end device acceptance.

The updated worker passed 53 tests executing the Java/BeanShell source against simulated Android and Tasker APIs. Preparation identifies transcript mode from positive response controls and may use the focus toggle once before selection, then verifies the destination. A small orb alone is not transcript evidence. After selection, the transcript window must remain consistent through Send and image confirmation. The worker never toggles focus after Send or treats newly exposed historical images as delivery. Missing transcript evidence stops or holds the receipt without resending.

These host tests do not establish phone acceptance. Earlier supervised replays verified a previous worker source; they are historical evidence, not validation of this revision. Fresh physical-button BLE capture through the updated Tasker flow, repeated images and uninterrupted voice audio require supervised device checks. Deploy both the supplied Java source and matching generated worker project, because this revision also strengthens the embedded queue-completion contract.

Pinned Java source SHA256: {worker_sha}

Verify all supplied file hashes against SHA256SUMS before deployment. Source and setup details: integrations/tasker in the Manfred repository.

To regenerate from the repository, no phone export is needed:

    python3 integrations/tasker/generate_tasker_bundle.py --expected-worker-sha256 {worker_sha} --output Tasker-Manfred-test.zip

The default tasker-schema-template.prj.xml contains only complete, sanitized schemas from our authored test projects, with generic IDs and timestamps. The generator checks that schema and the explicitly frozen Java source hash.
"""


def build_entries(template, library, adapter, flow, java_source, expected_worker_sha, now_ms):
    source_bytes=java_source.encode("utf-8")
    actual=hashlib.sha256(source_bytes).hexdigest()
    if actual != expected_worker_sha:
        raise ValueError("Java source does not match the explicitly frozen worker SHA256")
    worker=ET.fromstring(build_worker(template,library,adapter,flow,java_source,now_ms))
    java_actions=[a for a in worker.findall("Task/Action") if a.findtext("code")=="474"]
    if len(java_actions)!=2:
        raise ValueError("Expected exactly two staged Java actions")
    for action in java_actions:
        action.find("Str[@sr='arg0']").text='return source("'+JAVA_PATH+'");'
    receipt=ET.fromstring(build_project(template,library,adapter,now_ms))
    actions=receipt.findall("Task/Action")
    if len(actions)!=1:
        raise ValueError("Expected exactly one receipt action")
    loader=receipt_loader()
    actions[0].find("Str[@sr='arg0']").text=loader
    entries={
        "Manfred_ChatGPT_Worker.prj.xml":xml_bytes(worker),
        "Manfred_Photo_Queue.prj.xml":xml_bytes(receipt),
        "Tasker/Manfred/eyevue-ui-worker.java.txt":source_bytes,
        "Tasker/Manfred/receipt.js":receipt_bundle(library,adapter,flow).encode("utf-8"),
        "Tasker/Manfred/receipt-loader.js":loader.encode("utf-8"),
        "README.md":install_readme(actual).encode("utf-8"),
    }
    entries["SHA256SUMS"]=("".join(hashlib.sha256(content).hexdigest()+"  "+name+"\n"
                                for name,content in sorted(entries.items()))).encode("ascii")
    return entries


def archive_bytes(entries):
    data=io.BytesIO()
    with zipfile.ZipFile(data,"w",zipfile.ZIP_DEFLATED) as archive:
        for name,content in sorted(entries.items()):
            info=zipfile.ZipInfo(name,date_time=(2026,9,5,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o644<<16
            archive.writestr(info,content)
    return data.getvalue()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template",type=Path,default=HERE/"tasker-schema-template.prj.xml",
                        help="Defaults to the complete sanitized authored schemas included in this repository")
    parser.add_argument("--expected-worker-sha256",required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    template=args.template.read_bytes()
    if hashlib.sha256(template).hexdigest() not in (TEMPLATE_SHA256,LEGACY_AUTHORED_TEMPLATE_SHA256):
        raise ValueError("Template hash differs from the reviewed sanitized or legacy authored schemas")
    entries=build_entries(template,(HERE/"eyevue-queue.js").read_text(),
                          (HERE/"eyevue-tasker.js").read_text(),(HERE/"eyevue-worker-flow.js").read_text(),
                          (HERE/"eyevue-ui-worker.java.txt").read_text(),args.expected_worker_sha256,int(time.time()*1000))
    result=archive_bytes(entries)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_bytes(result)
    print("Generated:",args.output)
    print("Bytes:",len(result))
    print("SHA256:",hashlib.sha256(result).hexdigest())


if __name__=="__main__":
    main()
