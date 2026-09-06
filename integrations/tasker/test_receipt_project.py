import json
import subprocess
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from generate_receipt_project import ACTION, build_project

HERE = Path(__file__).resolve().parent


class ReceiptProjectTest(unittest.TestCase):
    def setUp(self):
        self.library = (HERE / "eyevue-queue.js").read_text()
        self.adapter = (HERE / "eyevue-tasker.js").read_text()
        self.template = (HERE / "receipt-template.fixture.xml").read_bytes()
        self.output = build_project(self.template, self.library, self.adapter, 1788652800000)
        self.root = ET.fromstring(self.output)

    def test_project_links_one_receipt_profile_and_task_using_id_40(self):
        self.assertEqual(len(self.root.findall("Profile")), 1)
        self.assertEqual(len(self.root.findall("Task")), 1)
        self.assertEqual(self.root.findtext("Project/pids"), "40")
        self.assertEqual(self.root.findtext("Project/tids"), "40")
        self.assertEqual(self.root.findtext("Profile/id"), "40")
        self.assertEqual(self.root.findtext("Profile/mid0"), "40")
        self.assertEqual(self.root.findtext("Task/id"), "40")
        self.assertEqual(self.root.findtext("Task/nme"), "Manfred Photo Receipt")
        self.assertEqual(self.root.findtext("Profile/nme"), "Manfred Photo Receipt")
        self.assertEqual(self.root.findtext("Profile/Event/code"), "599")
        self.assertEqual(self.root.findtext("Profile/Event/Str[@sr='arg0']"), ACTION)

    def test_only_embedded_javascript_remains_with_known_auto_exit_schema(self):
        actions = self.root.findall("Task/Action")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].findtext("code"), "129")
        self.assertEqual(actions[0].find("Int[@sr='arg2']").get("val"), "1")
        self.assertEqual(actions[0].find("Int[@sr='arg3']").get("val"), "45")
        self.assertFalse(actions[0].findtext("Str[@sr='arg1']"))
        self.assertEqual(self.root.findall(".//App"), [])
        self.assertEqual(self.root.findall(".//Bundle"), [])
        self.assertEqual(
            {element.tag for element in self.root.find("Task")},
            {"cdate", "edate", "id", "nme", "Action"},
        )
        body = actions[0].findtext("Str[@sr='arg0']")
        self.assertIn(self.library.rstrip(), body)
        self.assertIn(self.adapter.rstrip(), body)
        self.assertNotIn("performTask(", body)
        self.assertNotIn("sendIntent(", body)
        self.assertTrue(body.startswith('var mq_op = "receipt";'))

    def test_xml_roundtrip_preserves_javascript_special_characters(self):
        body = self.root.findtext("Task/Action/Str[@sr='arg0']")
        self.assertIn("&&", body)
        self.assertIn("<= 0", body)
        self.assertIn(b"&amp;&amp;", self.output)
        self.assertIn(b"&lt;= 0", self.output)

    def test_generated_action_enqueues_only_and_emits_redacted_receipt_diagnostic(self):
        program = r"""
const vm = require("node:vm");
const fs = require("node:fs");
const body = JSON.parse(fs.readFileSync(0, "utf8"));
const globals = {};
const writes = [];
const flashes = [];
const context = {
  global: name => globals[name],
  setGlobal: (name, value) => { globals[name] = value; },
  writeFile: (name, value, append) => { writes.push({name, data:JSON.parse(value), append}); return true; },
  flash: value => { flashes.push(value); },
  var_id: "12345678-1234-4234-8234-123456789abc",
  sessionid: "abcdef01-1234-4234-8234-123456789abc",
  uri: "content://media/external/images/media/1",
  filename: "EyeVue_1788652800000_12345678.jpg",
  width: "3200", height: "2400", bytes: "266148", sha256: "a".repeat(64),
  detectedat: "1788652800000", receivedat: "1788652801000"
};
vm.createContext(context);
vm.runInContext(body, context);
vm.runInContext(body, context);
process.stdout.write(JSON.stringify({writes, flashes, queue:JSON.parse(globals.ManfredEyevueQueue)}));
"""
        body = self.root.findtext("Task/Action/Str[@sr='arg0']")
        result = subprocess.run(["node", "-e", program], input=json.dumps(body), text=True, capture_output=True, check=True)
        output = json.loads(result.stdout)
        self.assertEqual([item["data"]["status"] for item in output["writes"]], ["queued", "duplicate"])
        self.assertTrue(all(item["name"] == "Tasker/manfred-receipt.json" and item["append"] is False for item in output["writes"]))
        self.assertEqual(set(output["writes"][0]["data"]), {"status", "error", "id", "filename", "width", "height"})
        self.assertIn("queued", output["flashes"][0])
        self.assertEqual(len(output["queue"]["items"]), 1)
        self.assertEqual(output["queue"]["items"][0]["state"], "queued")
        self.assertIsNone(output["queue"]["items"][0]["owner"])

    def test_unrecognized_templates_fail_instead_of_guessing_action_schema(self):
        with self.assertRaises(ValueError):
            build_project(b"<TaskerData/>", self.library, self.adapter, 1788652800000)
        damaged = self.template.replace(b'<Int sr="arg3" val="45"/>', b"")
        with self.assertRaises(ValueError):
            build_project(damaged, self.library, self.adapter, 1788652800000)


if __name__ == "__main__":
    unittest.main()
