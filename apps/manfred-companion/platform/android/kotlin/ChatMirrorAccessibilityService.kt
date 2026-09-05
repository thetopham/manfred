package com.thetopham.manfred_companion

import android.accessibilityservice.AccessibilityService
import android.os.Handler
import android.os.Looper
import android.util.AtomicFile
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.time.Instant
import java.util.ArrayDeque
import java.util.UUID

class ChatMirrorAccessibilityService : AccessibilityService() {
    companion object {
        private const val TAG = "ManfredChatMirror"
        private const val TARGET_PACKAGE = "com.openai.chatgpt"
        private const val MIN_CAPTURE_INTERVAL_MS = 500L
        private const val MAX_NODES = 1_000
        private const val MAX_TEXT_UTF8_BYTES = 90_000
    }

    private var sessionId: String = "accessibility-${UUID.randomUUID()}"
    private var sequenceNumber: Long = 0
    private var lastFingerprint: String? = null
    private val captureHandler = Handler(Looper.getMainLooper())
    private var pendingEvent: AccessibilityEvent? = null
    private var pendingCapture: Runnable? = null

    override fun onServiceConnected() {
        super.onServiceConnected()
        sessionId = "accessibility-${UUID.randomUUID()}"
        sequenceNumber = 0
        cancelPendingCapture()
        lastFingerprint = null
        ChatMirrorQuota.reconcile(this, mirrorDirectory())
        persistObservation(
            eventKind = "session_started",
            observedText = "",
            completeness = "partial",
            finality = "provisional",
            event = null,
        )
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null || event.packageName?.toString() != TARGET_PACKAGE) {
            return
        }
        if (
            event.eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED &&
            event.eventType != AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED
        ) {
            return
        }
        scheduleCapture(event)
    }

    @Suppress("DEPRECATION")
    private fun scheduleCapture(event: AccessibilityEvent) {
        cancelPendingCapture()
        val copied = AccessibilityEvent.obtain(event)
        pendingEvent = copied
        val task = Runnable {
            if (pendingEvent === copied) {
                pendingEvent = null
                pendingCapture = null
            }
            try {
                captureEvent(copied)
            } finally {
                copied.recycle()
            }
        }
        pendingCapture = task
        captureHandler.postDelayed(task, MIN_CAPTURE_INTERVAL_MS)
    }

    @Suppress("DEPRECATION")
    private fun cancelPendingCapture() {
        pendingCapture?.let(captureHandler::removeCallbacks)
        pendingCapture = null
        pendingEvent?.recycle()
        pendingEvent = null
    }

    @Suppress("DEPRECATION")
    private fun flushPendingCapture() {
        val event = pendingEvent ?: return
        pendingCapture?.let(captureHandler::removeCallbacks)
        pendingCapture = null
        pendingEvent = null
        try {
            captureEvent(event)
        } finally {
            event.recycle()
        }
    }

    private fun captureEvent(event: AccessibilityEvent) {
        val root = rootInActiveWindow ?: event.source ?: return
        if (root.packageName?.toString() != TARGET_PACKAGE) {
            persistObservation(
                eventKind = "gap",
                observedText = "",
                completeness = "gap",
                finality = "provisional",
                event = event,
                gapReason = "root-package-mismatch",
            )
            return
        }
        val snapshot = collectSnapshot(root)
        if (snapshot.passwordPresent) {
            val fingerprint = "gap:password-window-redacted"
            if (lastFingerprint != fingerprint) {
                lastFingerprint = fingerprint
                persistObservation(
                    eventKind = "gap",
                    observedText = "",
                    completeness = "gap",
                    finality = "provisional",
                    event = event,
                    gapReason = "password-window-redacted",
                )
            }
            return
        }
        if (snapshot.text.isBlank()) {
            return
        }
        val fingerprint = sha256(snapshot.text)
        if (fingerprint == lastFingerprint) {
            return
        }
        lastFingerprint = fingerprint
        persistObservation(
            eventKind = "ui_snapshot",
            observedText = snapshot.text,
            completeness = "partial",
            finality = "provisional",
            event = event,
        )
    }

    override fun onInterrupt() {
        flushPendingCapture()
        persistObservation(
            eventKind = "gap",
            observedText = "",
            completeness = "gap",
            finality = "provisional",
            event = null,
            gapReason = "accessibility-service-interrupted",
        )
    }

    override fun onDestroy() {
        flushPendingCapture()
        persistObservation(
            eventKind = "session_ended",
            observedText = "",
            completeness = "partial",
            finality = "final",
            event = null,
        )
        super.onDestroy()
    }

    private data class Snapshot(val text: String, val passwordPresent: Boolean)

    private fun collectSnapshot(root: AccessibilityNodeInfo): Snapshot {
        val queue = ArrayDeque<AccessibilityNodeInfo>()
        val text = SnapshotTextAccumulator()
        queue.add(root)
        var visited = 0
        var passwordPresent = false
        while (queue.isNotEmpty() && visited < MAX_NODES) {
            val node = queue.removeFirst()
            visited++
            if (node.isVisibleToUser) {
                if (node.isPassword) {
                    passwordPresent = true
                }
                text.add(node.text?.toString())
                if (node.contentDescription != node.text) {
                    text.add(node.contentDescription?.toString())
                }
            }
            for (index in 0 until node.childCount) {
                node.getChild(index)?.let(queue::addLast)
            }
        }
        return Snapshot(
            text = text.value(),
            passwordPresent = passwordPresent,
        )
    }

    private inner class SnapshotTextAccumulator {
        private val lines = mutableListOf<String>()
        private var usedBytes = 0

        val isFull: Boolean
            get() = usedBytes >= MAX_TEXT_UTF8_BYTES

        fun add(raw: String?) {
            if (raw == null || isFull) {
                return
            }
            val normalized = raw.replace(Regex("\\s+"), " ").trim()
            if (normalized.isEmpty() || lines.lastOrNull() == normalized) {
                return
            }
            val separatorBytes = if (lines.isEmpty()) 0 else 1
            val remaining = MAX_TEXT_UTF8_BYTES - usedBytes - separatorBytes
            if (remaining <= 0) {
                usedBytes = MAX_TEXT_UTF8_BYTES
                return
            }
            val bounded = truncateUtf8(normalized, remaining)
            if (bounded.isEmpty()) {
                usedBytes = MAX_TEXT_UTF8_BYTES
                return
            }
            lines.add(bounded)
            usedBytes += separatorBytes + bounded.toByteArray(Charsets.UTF_8).size
        }

        fun value(): String = lines.joinToString("\n")
    }

    private fun truncateUtf8(value: String, maximumBytes: Int): String {
        if (value.toByteArray(Charsets.UTF_8).size <= maximumBytes) {
            return value
        }
        val builder = StringBuilder()
        var offset = 0
        var used = 0
        while (offset < value.length) {
            val codePoint = value.codePointAt(offset)
            val encoded = String(Character.toChars(codePoint)).toByteArray(Charsets.UTF_8)
            if (used + encoded.size > maximumBytes) {
                break
            }
            builder.appendCodePoint(codePoint)
            used += encoded.size
            offset += Character.charCount(codePoint)
        }
        return builder.toString()
    }

    private fun persistObservation(
        eventKind: String,
        observedText: String,
        completeness: String,
        finality: String,
        event: AccessibilityEvent?,
        gapReason: String? = null,
    ) {
        try {
            val sequence = sequenceNumber++
            val metadata = JSONObject()
                .put("event_type", event?.eventType ?: 0)
                .put("event_time_ms", event?.eventTime ?: 0L)
                .put("window_id", event?.windowId ?: -1)
                .put("class_name", event?.className?.toString() ?: JSONObject.NULL)
            val gaps = JSONArray()
            if (gapReason != null) {
                gaps.put(JSONObject().put("reason", gapReason))
            }
            val body = JSONObject()
                .put("schema_version", 1)
                .put("mirror_session_id", sessionId)
                .put("sequence_number", sequence)
                .put("observed_at", Instant.now().toString())
                .put("clock_basis", "android-system-clock")
                .put("source_package", TARGET_PACKAGE)
                .put("event_kind", eventKind)
                .put("capture_method", "android-accessibility")
                .put("finality", finality)
                .put("completeness", completeness)
                .put("observed_text", observedText)
                .put("observable_conversation_id", JSONObject.NULL)
                .put("interruptions", JSONArray())
                .put("image_refs", JSONArray())
                .put("audio_refs", JSONArray())
                .put("automation_events", JSONArray())
                .put("gaps", gaps)
                .put("metadata", metadata)
                .toString()
            if (!writeAtomically(sequence, body)) {
                val overflowSequence = sequenceNumber++
                val overflowBody = buildOverflowBody(body, sequence, overflowSequence)
                if (writeOverflowAtomically(overflowBody)) {
                    Log.w(TAG, "Chat Mirror native spool is full; retained overflow gap")
                } else {
                    Log.e(TAG, "Chat Mirror native spool is full and overflow reserve is exhausted")
                }
            }
        } catch (error: Throwable) {
            Log.e(TAG, "Could not retain Chat Mirror observation", error)
        }
    }

    private fun buildOverflowBody(
        template: String,
        droppedSequence: Long,
        overflowSequence: Long,
    ): String {
        var firstDropped = droppedSequence
        var droppedCount = 1L
        val overflowFile = overflowFile()
        val overflowBackup = File("${overflowFile.path}.bak")
        if (overflowFile.exists() || overflowBackup.exists()) {
            runCatching {
                val prior = JSONObject(String(AtomicFile(overflowFile).readFully(), Charsets.UTF_8))
                val priorGap = prior.optJSONArray("gaps")?.optJSONObject(0)
                if (
                    prior.optString("mirror_session_id") == sessionId &&
                    priorGap?.optString("reason") == "native-spool-capacity-reached"
                ) {
                    firstDropped = priorGap.optLong("first_dropped_sequence_number", droppedSequence)
                    droppedCount = priorGap.optLong("dropped_count", 0L) + 1L
                }
            }.onFailure { error ->
                Log.w(TAG, "Could not merge prior Chat Mirror overflow gap", error)
            }
        }
        val gap = JSONObject()
            .put("reason", "native-spool-capacity-reached")
            .put("first_dropped_sequence_number", firstDropped)
            .put("last_dropped_sequence_number", droppedSequence)
            .put("dropped_count", droppedCount)
        return JSONObject(template)
            .put("sequence_number", overflowSequence)
            .put("observed_at", Instant.now().toString())
            .put("event_kind", "gap")
            .put("finality", "provisional")
            .put("completeness", "gap")
            .put("observed_text", "")
            .put("gaps", JSONArray().put(gap))
            .toString()
    }

    private fun mirrorDirectory(): File = File(filesDir, "chat-mirror-native").apply { mkdirs() }

    private fun overflowFile(): File {
        val safeSession = sessionId.replace(Regex("[^A-Za-z0-9_-]"), "_")
        return File(mirrorDirectory(), "native-overflow-${safeSession}.json")
    }

    private fun writeAtomically(sequence: Long, body: String): Boolean {
        val directory = mirrorDirectory()
        val bytes = body.toByteArray(Charsets.UTF_8)
        val quota = ChatMirrorQuota.current(this, directory)
        if (
            quota.records >=
                ChatMirrorQuota.MAX_PENDING_RECORDS - ChatMirrorQuota.OVERFLOW_RESERVED_RECORDS ||
            quota.bytes + bytes.size >
                ChatMirrorQuota.MAX_PENDING_BYTES - ChatMirrorQuota.OVERFLOW_RESERVE_BYTES
        ) {
            return false
        }
        val safeSession = sessionId.replace(Regex("[^A-Za-z0-9_-]"), "_")
        val file = File(directory, "native-${safeSession}-${sequence.toString().padStart(12, '0')}.json")
        writeAtomicFile(directory, file, bytes)
        return true
    }

    private fun writeOverflowAtomically(body: String): Boolean {
        val directory = mirrorDirectory()
        val file = overflowFile()
        val backup = File("${file.path}.bak")
        val existed = file.exists() || backup.exists()
        val quota = ChatMirrorQuota.current(this, directory)
        val bodyBytes = body.toByteArray(Charsets.UTF_8)
        if (
            !existed &&
            (quota.records >= ChatMirrorQuota.MAX_PENDING_RECORDS ||
                quota.bytes + bodyBytes.size > ChatMirrorQuota.MAX_PENDING_BYTES)
        ) {
            return false
        }
        writeAtomicFile(directory, file, bodyBytes)
        return true
    }

    private fun writeAtomicFile(directory: File, file: File, body: ByteArray) {
        ChatMirrorQuota.locked {
            val backup = File("${file.path}.bak")
            val existed = file.exists() || backup.exists()
            val oldBytes = (if (file.exists()) file.length() else 0L) +
                (if (backup.exists()) backup.length() else 0L)
            val atomicFile = AtomicFile(file)
            val output = atomicFile.startWrite()
            try {
                output.write(body)
                atomicFile.finishWrite(output)
            } catch (error: Throwable) {
                atomicFile.failWrite(output)
                throw error
            }
            val newBytes = (if (file.exists()) file.length() else 0L) +
                (if (backup.exists()) backup.length() else 0L)
            ChatMirrorQuota.recordReplacement(
                this,
                directory,
                existed = existed,
                oldBytes = oldBytes,
                newBytes = newBytes,
            )
        }
    }

    private fun sha256(value: String): String {
        return MessageDigest.getInstance("SHA-256")
            .digest(value.toByteArray(Charsets.UTF_8))
            .joinToString("") { byte -> "%02x".format(byte) }
    }
}
