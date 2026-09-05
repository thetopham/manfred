from __future__ import annotations

import sys
import unittest
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
COMPANION = ROOT / "apps/manfred-companion"
ANDROID = COMPANION / "platform/android"
ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


class ChatMirrorAndroidOverlayTests(unittest.TestCase):
    def test_manifest_declares_only_bounded_accessibility_service(self) -> None:
        root = ElementTree.parse(ANDROID / "AndroidManifest.xml").getroot()
        application = root.find("application")
        self.assertIsNotNone(application)
        assert application is not None
        self.assertEqual(application.attrib.get(f"{ANDROID_NS}allowBackup"), "false")
        self.assertEqual(application.attrib.get(f"{ANDROID_NS}fullBackupContent"), "false")
        services = {
            service.attrib.get(f"{ANDROID_NS}name"): service
            for service in application.findall("service")
        }
        service = services.get(".ChatMirrorAccessibilityService")
        self.assertIsNotNone(service)
        assert service is not None
        self.assertEqual(
            service.attrib.get(f"{ANDROID_NS}permission"),
            "android.permission.BIND_ACCESSIBILITY_SERVICE",
        )
        self.assertEqual(service.attrib.get(f"{ANDROID_NS}exported"), "true")
        metadata = service.find("meta-data")
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(
            metadata.attrib.get(f"{ANDROID_NS}resource"),
            "@xml/chat_mirror_accessibility_service",
        )

    def test_accessibility_resource_is_chatgpt_only_and_read_only(self) -> None:
        service = ElementTree.parse(
            ANDROID / "res/xml/chat_mirror_accessibility_service.xml"
        ).getroot()
        self.assertEqual(service.attrib.get(f"{ANDROID_NS}packageNames"), "com.openai.chatgpt")
        events = service.attrib.get(f"{ANDROID_NS}accessibilityEventTypes", "")
        self.assertEqual(
            set(events.split("|")),
            {"typeWindowStateChanged", "typeWindowContentChanged"},
        )
        source = (ANDROID / "kotlin/ChatMirrorAccessibilityService.kt").read_text()
        self.assertIn('private const val TARGET_PACKAGE = "com.openai.chatgpt"', source)
        self.assertIn("event.packageName?.toString() != TARGET_PACKAGE", source)
        self.assertIn("root.packageName?.toString() != TARGET_PACKAGE", source)
        self.assertIn('gapReason = "root-package-mismatch"', source)
        self.assertIn("node.isVisibleToUser", source)
        self.assertIn("node.isPassword", source)
        self.assertIn('gapReason = "password-window-redacted"', source)
        self.assertIn("AtomicFile(file)", source)
        self.assertIn("ChatMirrorQuota.current(this, directory)", source)
        self.assertNotIn("directory.listFiles().orEmpty().filter(File::isFile)", source)
        quota = (ANDROID / "kotlin/ChatMirrorQuota.kt").read_text()
        self.assertIn("fun reconcile(context: Context, directory: File)", quota)
        self.assertIn("fun recordReplacement(", quota)
        self.assertIn("fun recordDeletion(", quota)
        self.assertIn("MAX_TEXT_UTF8_BYTES", source)
        self.assertIn("MAX_PENDING_RECORDS", source)
        self.assertIn("MAX_PENDING_BYTES", source)
        self.assertIn('"native-spool-capacity-reached"', source)
        self.assertIn('"first_dropped_sequence_number"', source)
        self.assertIn('"last_dropped_sequence_number"', source)
        self.assertIn('"dropped_count"', source)
        self.assertIn('prior.optString("mirror_session_id") == sessionId', source)
        self.assertIn('return File(mirrorDirectory(), "native-overflow-${safeSession}.json")', source)
        self.assertIn("OVERFLOW_RESERVED_RECORDS", source)
        self.assertIn("captureHandler.postDelayed(task, MIN_CAPTURE_INTERVAL_MS)", source)
        self.assertIn("flushPendingCapture()", source)
        self.assertNotIn("SystemClock.elapsedRealtime()", source)
        self.assertIn("truncateUtf8(normalized, remaining)", source)
        self.assertIn("builder.appendCodePoint(codePoint)", source)
        self.assertIn("SnapshotTextAccumulator", source)
        self.assertIn("while (queue.isNotEmpty() && visited < MAX_NODES)", source)
        self.assertIn("if (raw == null || isFull)", source)
        self.assertIn("val remaining = MAX_TEXT_UTF8_BYTES - usedBytes - separatorBytes", source)
        self.assertNotIn("LinkedHashSet", source)
        activity = (ANDROID / "kotlin/MainActivity.kt").read_text()
        self.assertIn("AtomicFile(file).readFully()", activity)
        self.assertIn('"listPendingNames" -> runIo(result) { listPendingNames() }', activity)
        self.assertIn("Executors.newSingleThreadExecutor()", activity)
        self.assertIn(".take(MAX_RECORD_BODIES_PER_CALL)", activity)
        self.assertIn("private fun allPendingNames()", activity)
        self.assertIn(".dropWhile { name -> afterName != null && name <= afterName }", activity)
        self.assertIn("runCatching { AtomicFile(file).readFully() }", activity)
        self.assertIn('"body" to body', activity)
        self.assertNotIn("body.toString(Charsets.UTF_8)", activity)
        self.assertIn("AtomicFile(file).delete()", activity)
        self.assertIn("expectedSha256", activity)
        self.assertIn("ChatMirrorQuota.locked", activity)
        self.assertIn('"fsyncSpoolDirectory"', activity)
        self.assertIn("Os.fsync(descriptor)", activity)
        for forbidden in (
            "performAction(",
            "dispatchGesture(",
            "performGlobalAction(",
            "ACTION_SET_TEXT",
            "ACTION_CLICK",
        ):
            self.assertNotIn(forbidden, source)

    def test_materializer_copies_native_sources_and_resources(self) -> None:
        script = (COMPANION / "tool/materialize_android.sh").read_text()
        self.assertIn('platform/android/kotlin/"*.kt', script)
        self.assertIn('platform/android/res/.', script)
        self.assertIn("com/thetopham/manfred_companion", script)


if __name__ == "__main__":
    unittest.main()
