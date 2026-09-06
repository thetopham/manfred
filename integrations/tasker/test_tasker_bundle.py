import hashlib
import io
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from generate_tasker_bundle import archive_bytes, build_entries, JAVA_PATH

HERE=Path(__file__).resolve().parent


class TaskerBundleTest(unittest.TestCase):
    def setUp(self):
        template=ET.fromstring((HERE/"receipt-template.fixture.xml").read_bytes())
        template.find("Task/Action/App/appPkg").text="com.openai.chatgpt"
        wait=ET.SubElement(template.find("Task"),"Action",{"sr":"act4","ve":"7"})
        ET.SubElement(wait,"code").text="30"
        for number,value in enumerate(["0","2","0","0","0"]):
            ET.SubElement(wait,"Int",{"sr":"arg"+str(number),"val":value})
        self.template=ET.tostring(template)
        self.java='return "test_fixture_not_a_runtime_worker";\n'
        self.sha=hashlib.sha256(self.java.encode()).hexdigest()
        self.args=[self.template,(HERE/"eyevue-queue.js").read_text(),(HERE/"eyevue-tasker.js").read_text(),
                   (HERE/"eyevue-worker-flow.js").read_text(),self.java,self.sha,1788652800000]
        self.entries=build_entries(*self.args)

    def test_portable_whitelist_contains_no_queue_or_recovery_export(self):
        self.assertEqual(set(self.entries),{
            "Manfred_ChatGPT_Worker.prj.xml","Manfred_Photo_Queue.prj.xml",
            "Tasker/Manfred/eyevue-ui-worker.java.txt","Tasker/Manfred/receipt.js",
            "Tasker/Manfred/receipt-loader.js","README.md","SHA256SUMS"})
        for name in self.entries:
            self.assertFalse(name.startswith("/") or ".." in name.split("/"))
        for name in ("Manfred_ChatGPT_Worker.prj.xml","Manfred_Photo_Queue.prj.xml"):
            root=ET.fromstring(self.entries[name])
            self.assertEqual(root.findall("Variable"),[])
            self.assertEqual(root.findall("Scene"),[])

    def test_exact_source_loader_stages_keep_full_worker_order_and_receipt_local_imports(self):
        root=ET.fromstring(self.entries["Manfred_ChatGPT_Worker.prj.xml"])
        worker,dispatcher=root.findall("Task")
        self.assertEqual([a.findtext("code") for a in worker.findall("Action")],
                         ["129","37","20","30","474","129","37","474","129","38","38","129"])
        self.assertEqual(len(dispatcher.findall("Action")),1)
        for action in worker.findall("Action"):
            if action.findtext("code")=="474":
                self.assertEqual(action.findtext("Str[@sr='arg0']"),'return source("'+JAVA_PATH+'");')
                self.assertEqual(action.findtext("Str[@sr='arg1']"),"%mq_ui_result")
                self.assertEqual(action.findtext("se"),"false")
        receipt=ET.fromstring(self.entries["Manfred_Photo_Queue.prj.xml"])
        body=receipt.findtext("Task/Action/Str[@sr='arg0']")
        self.assertEqual(body.encode(),self.entries["Tasker/Manfred/receipt-loader.js"])
        self.assertIn("typeof sha",body)
        self.assertIn("typeof var_id",body)
        self.assertEqual(receipt.findtext("Profile/Event/Str[@sr='arg0']"),
                         "com.thetopham.manfred_companion.EYEVUE_IMAGE_READY")
        self.assertNotIn(b"tk.flash(",self.entries["Tasker/Manfred/receipt.js"])

    def test_every_project_node_retains_sr_before_ve(self):
        for name,content in self.entries.items():
            if name.endswith(".xml"):
                for element in ET.fromstring(content).iter():
                    keys=list(element.attrib)
                    if "sr" in keys:self.assertEqual(keys[0],"sr")
                    if "ve" in keys:self.assertEqual(keys[1 if "sr" in keys else 0],"ve")

    def test_hash_manifest_covers_exact_delivered_files_and_archive_is_reproducible(self):
        lines=self.entries["SHA256SUMS"].decode().splitlines()
        self.assertEqual(len(lines),len(self.entries)-1)
        for line in lines:
            digest,name=line.split("  ",1)
            self.assertEqual(hashlib.sha256(self.entries[name]).hexdigest(),digest)
        archive=archive_bytes(self.entries)
        self.assertEqual(archive,archive_bytes(self.entries))
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            self.assertIsNone(zipped.testzip())
            self.assertEqual(set(zipped.namelist()),set(self.entries))
            for name in zipped.namelist():self.assertEqual(zipped.read(name),self.entries[name])

    def test_repository_default_template_regenerates_bundle_without_phone_export(self):
        schema=ET.parse(HERE/"tasker-schema-template.prj.xml").getroot()
        self.assertEqual(schema.findall(".//Bundle"),[])
        self.assertEqual(schema.findall("Variable"),[])
        self.assertEqual(schema.findall("Scene"),[])
        self.assertEqual({a.findtext("code") for a in schema.findall("Task/Action")},
                         {"20","30","129","474","37","38"})
        for element in schema.iter():
            if element.tag in ("cdate","edate","id","mid0"):
                self.assertEqual(element.text,"1")
        java=(HERE/"eyevue-ui-worker.java.txt").read_text()
        digest=hashlib.sha256(java.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/"Tasker-Manfred-0.5.3-test.zip"
            subprocess.run([sys.executable,str(HERE/"generate_tasker_bundle.py"),
                            "--expected-worker-sha256",digest,"--output",str(output)],
                           cwd=temp,text=True,capture_output=True,check=True)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(archive.read("Tasker/Manfred/eyevue-ui-worker.java.txt"),java.encode())
                worker=ET.fromstring(archive.read("Manfred_ChatGPT_Worker.prj.xml"))
                self.assertEqual([len(task.findall("Action")) for task in worker.findall("Task")],[12,1])

    def test_unfrozen_or_changed_java_source_is_rejected(self):
        args=list(self.args)
        args[4]+="// changed after release\n"
        with self.assertRaises(ValueError):build_entries(*args)


if __name__=="__main__":
    unittest.main()
