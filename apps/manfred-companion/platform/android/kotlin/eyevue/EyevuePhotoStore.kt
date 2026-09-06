package com.thetopham.manfred_companion.eyevue

import android.content.ContentValues
import android.content.Context
import android.graphics.BitmapFactory
import android.provider.MediaStore
import android.util.Log
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.util.UUID
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext

/** Shared original-byte validation, durable save, and receipt boundary for both transports. */
internal class EyevuePhotoStore(private val context: Context) {
    suspend fun saveBlePreview(
        bytes: ByteArray,
        sessionId: String,
        detectedAt: Long,
        onCommitted: (Map<String, Any?>) -> Unit,
    ): Unit = withContext(Dispatchers.IO) {
        require(bytes.size in 4..(32 * 1024 * 1024)) { "EyeVue BLE preview exceeded the size limit" }
        val id = UUID.randomUUID().toString()
        val cache = File(context.cacheDir, "eyevue").apply {
            if (!isDirectory && !mkdirs()) throw IOException("Could not create the EyeVue photo cache")
        }
        val pending = File(cache, "$id.part")
        try {
            currentCoroutineContext().ensureActive()
            pending.outputStream().use { it.write(bytes) }
            currentCoroutineContext().ensureActive()
            publishPending(pending, id, sessionId, detectedAt, "ble_preview", bytes.size.toLong(),
                android.os.SystemClock.elapsedRealtime(), onCommitted)
        } finally {
            pending.delete()
        }
    }

    suspend fun publishPending(
        pendingFile: File,
        id: String,
        sessionId: String,
        detectedAt: Long,
        imageSource: String,
        expectedBytes: Long,
        started: Long,
        onCommitted: (Map<String, Any?>) -> Unit,
    ): Unit = withContext(Dispatchers.IO) {
        require(imageSource == "ble_preview" || imageSource == "wifi_original")
        val cache = pendingFile.parentFile ?: throw IOException("EyeVue cache directory missing")
        val completedFile = File(cache, "$id.jpg")
        var keepCompleted = false
        try {
            currentCoroutineContext().ensureActive()
            if (!hasCompleteJpegEnvelope(pendingFile, expectedBytes)) {
                throw IncompleteEyevuePhoto("The EyeVue JPEG is not complete yet")
            }
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeFile(pendingFile.absolutePath, bounds)
            if (bounds.outWidth <= 0 || bounds.outHeight <= 0 || bounds.outMimeType != "image/jpeg") {
                throw IncompleteEyevuePhoto("The EyeVue photo has invalid JPEG dimensions")
            }
            var sample = 1
            while (bounds.outWidth / sample > 1024 || bounds.outHeight / sample > 1024) sample *= 2
            val decoded = BitmapFactory.decodeFile(
                pendingFile.absolutePath,
                BitmapFactory.Options().apply { inSampleSize = sample },
            ) ?: throw IncompleteEyevuePhoto("The EyeVue JPEG could not be decoded")
            decoded.recycle()
            val digest = MessageDigest.getInstance("SHA-256")
            pendingFile.inputStream().use { input ->
                val buffer = ByteArray(64 * 1024)
                while (true) {
                    currentCoroutineContext().ensureActive()
                    val count = input.read(buffer)
                    if (count < 0) break
                    digest.update(buffer, 0, count)
                }
            }
            val sha256 = digest.digest().joinToString("") { "%02x".format(it.toInt() and 0xff) }
            if (!pendingFile.renameTo(completedFile)) throw IOException("Could not complete the EyeVue cache file")
            val fileName = "EyeVue_${System.currentTimeMillis()}_${id.take(8)}.jpg"
            currentCoroutineContext().ensureActive()
            // Network work is complete. Once local commit begins, finish both durable
            // save and its Main-thread receipt before honoring Stop. Otherwise the
            // dispatch back from IO can lose a successfully committed image.
            withContext(NonCancellable + Dispatchers.Main.immediate) {
                val image = withContext(Dispatchers.IO) {
                    val uri = saveMediaStore(completedFile, fileName)
                    keepCompleted = true
                    val receivedAt = System.currentTimeMillis()
                    Log.i("EyevuePhotoSession", "Saved JPEG ${bounds.outWidth}x${bounds.outHeight} bytes=${completedFile.length()} elapsedMs=${android.os.SystemClock.elapsedRealtime() - started}")
                    linkedMapOf<String, Any?>(
                        "imageSource" to imageSource,
                        "id" to id,
                        "sessionId" to sessionId,
                        "uri" to uri.toString(),
                        "cachePath" to completedFile.absolutePath,
                        "fileName" to fileName,
                        "width" to bounds.outWidth,
                        "height" to bounds.outHeight,
                        "bytes" to completedFile.length(),
                        "sha256" to sha256,
                        // Physical shutters do not provide a verified wall-clock timestamp.
                        "capturedAt" to null,
                        "captureTimestampSource" to "unavailable",
                        "detectedAt" to detectedAt,
                        "receivedAt" to receivedAt,
                    )
                }
                onCommitted(image)
                withContext<Unit>(Dispatchers.IO) {
                    trimEyevuePhotoCache(cache, completedFile)
                }
            }
        } finally {
            pendingFile.delete()
            if (!keepCompleted) completedFile.delete()
        }
    }

    private suspend fun saveMediaStore(file: File, fileName: String): android.net.Uri {
        val resolver = context.contentResolver
        val values = ContentValues().apply {
            put(MediaStore.Images.Media.DISPLAY_NAME, fileName)
            put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
            put(MediaStore.Images.Media.RELATIVE_PATH, "DCIM/Manfred")
            put(MediaStore.Images.Media.IS_PENDING, 1)
        }
        val uri = resolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values)
            ?: throw IOException("Could not create a MediaStore photo")
        var committed = false
        try {
            resolver.openOutputStream(uri, "w")?.use { output ->
                file.inputStream().use { input ->
                    val buffer = ByteArray(64 * 1024)
                    while (true) {
                        currentCoroutineContext().ensureActive()
                        val count = input.read(buffer)
                        if (count < 0) break
                        output.write(buffer, 0, count)
                    }
                }
            } ?: throw IOException("Could not write the MediaStore photo")
            currentCoroutineContext().ensureActive()
            val updated = resolver.update(
                uri,
                ContentValues().apply { put(MediaStore.Images.Media.IS_PENDING, 0) },
                null,
                null,
            )
            if (updated != 1) throw IOException("Could not commit the MediaStore photo")
            committed = true
            return uri
        } finally {
            if (!committed) resolver.delete(uri, null, null)
        }
    }

}

internal class IncompleteEyevuePhoto(message: String, cause: Throwable? = null) : IOException(message, cause)
