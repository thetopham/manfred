package com.thetopham.manfred_companion

import android.content.ComponentName
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.system.Os
import android.system.OsConstants
import android.util.AtomicFile
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.Executors

class MainActivity : FlutterActivity() {
    companion object {
        private const val CHANNEL = "com.thetopham.manfred_companion/chat_mirror"
        private const val MAX_RECORD_BYTES = 262_144L
        private const val MAX_RECORDS_PER_CALL = 1_000
        private const val MAX_RECORD_BODIES_PER_CALL = 50
        private val SAFE_NAME = Regex("^[A-Za-z0-9_-]+\\.json$")
        private val SHA256 = Regex("^[0-9a-f]{64}$")
    }

    private val ioExecutor = Executors.newSingleThreadExecutor()
    private val mainHandler = Handler(Looper.getMainLooper())

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL).setMethodCallHandler {
                call, result ->
            try {
                when (call.method) {
                    "isEnabled" -> result.success(isMirrorServiceEnabled())
                    "openAccessibilitySettings" -> {
                        startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS).apply {
                            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                        })
                        result.success(null)
                    }
                    "listPending" -> {
                        val afterName = call.argument<String>("afterName")
                        if (afterName != null &&
                            (!SAFE_NAME.matches(afterName) || afterName.contains(".."))
                        ) {
                            throw IllegalArgumentException("Unsafe Chat Mirror cursor")
                        }
                        runIo(result) { listPending(afterName) }
                    }
                    "listPendingNames" -> runIo(result) { listPendingNames() }
                    "deletePending" -> {
                        val name = call.argument<String>("name")
                            ?: throw IllegalArgumentException("Chat Mirror filename is required")
                        val expectedSha256 = call.argument<String>("expectedSha256")
                        if (expectedSha256 != null && !SHA256.matches(expectedSha256)) {
                            throw IllegalArgumentException("Chat Mirror expected SHA-256 is invalid")
                        }
                        runIo(result) {
                            deletePending(name, expectedSha256)
                        }
                    }
                    "fsyncSpoolDirectory" -> {
                        val path = call.argument<String>("path")
                            ?: throw IllegalArgumentException("Chat Mirror spool path is required")
                        runIo(result) {
                            fsyncSpoolDirectory(path)
                            null
                        }
                    }
                    else -> result.notImplemented()
                }
            } catch (error: Throwable) {
                result.error("chat_mirror_native", error.message, null)
            }
        }
    }

    private fun runIo(result: MethodChannel.Result, block: () -> Any?) {
        ioExecutor.execute {
            try {
                val value = block()
                mainHandler.post { result.success(value) }
            } catch (error: Throwable) {
                mainHandler.post {
                    result.error("chat_mirror_native", error.message, null)
                }
            }
        }
    }

    override fun onDestroy() {
        ioExecutor.shutdownNow()
        super.onDestroy()
    }

    private fun mirrorDirectory(): File = File(filesDir, "chat-mirror-native").apply {
        mkdirs()
    }

    private fun isMirrorServiceEnabled(): Boolean {
        val expected = ComponentName(this, ChatMirrorAccessibilityService::class.java)
        val enabled = Settings.Secure.getString(
            contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
        ).orEmpty()
        return enabled.split(':')
            .mapNotNull { value -> ComponentName.unflattenFromString(value) }
            .any { it == expected }
    }

    private fun allPendingNames(): List<String> {
        return mirrorDirectory()
            .listFiles()
            .orEmpty()
            .mapNotNull { file ->
                when {
                    file.isFile && SAFE_NAME.matches(file.name) -> file.name
                    file.isFile &&
                        file.name.endsWith(".json.bak") &&
                        SAFE_NAME.matches(file.name.removeSuffix(".bak")) ->
                        file.name.removeSuffix(".bak")
                    else -> null
                }
            }
            .toSortedSet()
            .toList()
    }

    private fun listPendingNames(): List<String> =
        allPendingNames().take(MAX_RECORDS_PER_CALL)

    private fun listPending(afterName: String?): List<Map<String, Any>> {
        val directory = mirrorDirectory()
        return allPendingNames()
            .asSequence()
            .dropWhile { name -> afterName != null && name <= afterName }
            .mapNotNull { name ->
                val file = File(directory, name)
                val body = runCatching { AtomicFile(file).readFully() }.getOrNull()
                    ?: return@mapNotNull null
                if (body.size.toLong() !in 1..MAX_RECORD_BYTES) {
                    null
                } else {
                    mapOf(
                        "name" to name,
                        "body" to body,
                    )
                }
            }
            .take(MAX_RECORD_BODIES_PER_CALL)
            .toList()
    }

    private fun fsyncSpoolDirectory(path: String) {
        val directory = File(path).canonicalFile
        val appFiles = filesDir.canonicalFile
        if (!directory.isDirectory || !directory.toPath().startsWith(appFiles.toPath())) {
            throw IllegalArgumentException("Chat Mirror spool directory escaped app storage")
        }
        val descriptor = Os.open(
            directory.path,
            OsConstants.O_RDONLY or OsConstants.O_DIRECTORY,
            0,
        )
        try {
            Os.fsync(descriptor)
        } finally {
            Os.close(descriptor)
        }
    }

    private fun deletePending(name: String, expectedSha256: String?): Boolean {
        if (!SAFE_NAME.matches(name) || name.contains("..")) {
            throw IllegalArgumentException("Unsafe Chat Mirror filename")
        }
        val directory = mirrorDirectory().canonicalFile
        val file = File(directory, name).canonicalFile
        if (file.parentFile != directory) {
            throw IllegalArgumentException("Chat Mirror file escaped its directory")
        }
        return ChatMirrorQuota.locked {
            if (expectedSha256 != null) {
                val current = runCatching { AtomicFile(file).readFully() }.getOrNull()
                    ?: return@locked false
                if (sha256(current) != expectedSha256) {
                    return@locked false
                }
            }
            val backup = File("${file.path}.bak")
            val existed = file.exists() || backup.exists()
            val deletedBytes = (if (file.exists()) file.length() else 0L) +
                (if (backup.exists()) backup.length() else 0L)
            AtomicFile(file).delete()
            if (file.exists() || backup.exists()) {
                throw IllegalStateException("Could not delete imported Chat Mirror observation")
            }
            ChatMirrorQuota.recordDeletion(
                this,
                directory,
                existed = existed,
                deletedBytes = deletedBytes,
            )
            true
        }
    }

    private fun sha256(body: ByteArray): String {
        return MessageDigest.getInstance("SHA-256")
            .digest(body)
            .joinToString("") { byte -> "%02x".format(byte) }
    }
}
