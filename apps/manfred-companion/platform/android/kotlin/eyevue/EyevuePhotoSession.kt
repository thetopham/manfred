package com.thetopham.manfred_companion.eyevue

import android.content.ContentValues
import android.content.Context
import android.graphics.BitmapFactory
import android.net.Network
import android.provider.MediaStore
import android.util.Log
import java.io.File
import java.io.IOException
import java.net.URLEncoder
import java.security.MessageDigest
import java.util.UUID
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import okhttp3.Dns
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject

internal data class EyevueRemotePhoto(
    val path: String,
    val fingerprint: EyevuePhotoFingerprint,
)

/** Only complete original JPEGs from the TK8 AP are persisted or published. */
internal class EyevuePhotoSession(
    private val context: Context,
    network: Network,
) {
    private val client: OkHttpClient = createEyevueMediaHttpClient(
        network.socketFactory,
        object : Dns {
            override fun lookup(hostname: String) = network.getAllByName(hostname).toList()
        },
    )

    suspend fun watch(
        sessionId: String,
        onStatus: (String, Boolean) -> Unit,
        onImage: (Map<String, Any?>) -> Unit,
    ) {
        val baseline = fetchManifest()
        val tracker = EyevueNewPhotoTracker(baseline.associate { it.path to it.fingerprint })
        val detected = mutableMapOf<Pair<String, EyevuePhotoFingerprint>, Long>()
        val retries = mutableMapOf<Pair<String, EyevuePhotoFingerprint>, Int>()
        var imported = 0
        onStatus("Ready - press the glasses shutter. Capture with Wi-Fi active is experimental.", true)
        while (true) {
            delay(750)
            currentCoroutineContext().ensureActive()
            val photos = fetchManifest()
            val byPath = photos.associateBy { it.path }
            val snapshot = photos.associate { it.path to it.fingerprint }
            val seenAt = System.currentTimeMillis()
            for (photo in photos) {
                detected.putIfAbsent(photo.path to photo.fingerprint, seenAt)
            }
            for ((path, fingerprint) in tracker.observe(snapshot)) {
                val photo = byPath.getValue(path)
                val key = path to fingerprint
                onStatus("Downloading new photo", true)
                try {
                    downloadAndSave(photo, sessionId, detected.getValue(key)) { image ->
                        tracker.markImported(path, fingerprint)
                        retries.remove(key)
                        imported++
                        onImage(image)
                    }
                } catch (error: IncompleteEyevuePhoto) {
                    currentCoroutineContext().ensureActive()
                    val attempt = (retries[key] ?: 0) + 1
                    retries[key] = attempt
                    if (attempt >= 5) {
                        throw IOException("A new photo remained incomplete after five attempts", error)
                    }
                    onStatus("Waiting for the glasses to finish the photo ($attempt/5)", true)
                    continue
                }
                onStatus("Ready - $imported new photo(s) saved", true)
            }
        }
    }

    private suspend fun fetchManifest(): List<EyevueRemotePhoto> = withContext(Dispatchers.IO) {
        val call = client.newCall(Request.Builder().url("http://192.168.169.1/app/getfilelist").build())
        withEyevueHttpCall(call) {
            call.execute().use { response ->
                if (!response.isSuccessful) throw IOException("EyeVue manifest HTTP ${response.code}")
                val body = response.body ?: throw IOException("EyeVue manifest was empty")
                val text = body.byteStream().use { input ->
                    val output = java.io.ByteArrayOutputStream()
                    val buffer = ByteArray(8192)
                    while (true) {
                        currentCoroutineContext().ensureActive()
                        val count = input.read(buffer)
                        if (count < 0) break
                        if (output.size() + count > 4 * 1024 * 1024) throw IOException("EyeVue manifest exceeded limit")
                        output.write(buffer, 0, count)
                    }
                    output.toString("UTF-8")
                }
                val root = JSONObject(text)
                val groups = root.optJSONArray("info") ?: throw IOException("EyeVue manifest has no info list")
                val photos = linkedMapOf<String, EyevueRemotePhoto>()
                for (groupIndex in 0 until groups.length()) {
                    val files = groups.optJSONObject(groupIndex)?.optJSONArray("files") ?: continue
                    for (fileIndex in 0 until files.length()) {
                        val item = files.optJSONObject(fileIndex) ?: continue
                        val path = normalizeEyevuePhotoPath(item.optString("name")) ?: continue
                        photos[path] = EyevueRemotePhoto(
                            path,
                            EyevuePhotoFingerprint(
                                item.optLong("size", -1),
                                item.optString("createtimestr").toLongOrNull() ?: 0L,
                            ),
                        )
                    }
                }
                photos.values.toList()
            }
        }
    }

    private suspend fun downloadAndSave(
        photo: EyevueRemotePhoto,
        sessionId: String,
        detectedAt: Long,
        onCommitted: (Map<String, Any?>) -> Unit,
    ): Unit = withContext(Dispatchers.IO) {
        val id = UUID.randomUUID().toString()
        val cache = File(context.cacheDir, "eyevue").apply {
            if (!isDirectory && !mkdirs()) throw IOException("Could not create the EyeVue photo cache")
        }
        val pendingFile = File(cache, "$id.part")
        val completedFile = File(cache, "$id.jpg")
        val started = android.os.SystemClock.elapsedRealtime()
        var keepCompleted = false
        try {
            val encodedPath = photo.path.split('/').joinToString("/") {
                URLEncoder.encode(it, "UTF-8").replace("+", "%20")
            }
            val call = client.newCall(Request.Builder().url("http://192.168.169.1/$encodedPath").build())
            var expectedBytes = -1L
            try {
                withEyevueHttpCall(call) {
                    call.execute().use { response ->
                        if (response.code == 404) throw IncompleteEyevuePhoto("The original is not available yet")
                        if (!response.isSuccessful) throw IOException("EyeVue original HTTP ${response.code}")
                        val body = response.body ?: throw IncompleteEyevuePhoto("The original was empty")
                        expectedBytes = body.contentLength()
                        if (expectedBytes > MAX_IMAGE_BYTES) throw IOException("EyeVue photo exceeded the size limit")
                        body.byteStream().use { input ->
                            pendingFile.outputStream().use { output ->
                                val buffer = ByteArray(64 * 1024)
                                var received = 0L
                                while (true) {
                                    currentCoroutineContext().ensureActive()
                                    val count = input.read(buffer)
                                    if (count < 0) break
                                    received += count
                                    if (received > MAX_IMAGE_BYTES) throw IOException("EyeVue photo exceeded the size limit")
                                    output.write(buffer, 0, count)
                                }
                            }
                        }
                    }
                }
            } catch (error: java.net.ProtocolException) {
                throw IncompleteEyevuePhoto("The original response ended before completion", error)
            }
            currentCoroutineContext().ensureActive()
            if (!hasCompleteJpegEnvelope(pendingFile, expectedBytes)) {
                throw IncompleteEyevuePhoto("The original JPEG is not complete yet")
            }
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeFile(pendingFile.absolutePath, bounds)
            if (bounds.outWidth <= 0 || bounds.outHeight <= 0 || bounds.outMimeType != "image/jpeg") {
                throw IncompleteEyevuePhoto("The original has invalid JPEG dimensions")
            }
            var sample = 1
            while (bounds.outWidth / sample > 1024 || bounds.outHeight / sample > 1024) sample *= 2
            val decoded = BitmapFactory.decodeFile(
                pendingFile.absolutePath,
                BitmapFactory.Options().apply { inSampleSize = sample },
            ) ?: throw IncompleteEyevuePhoto("The original JPEG could not be decoded")
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

    fun close() {
        client.dispatcher.cancelAll()
        client.connectionPool.evictAll()
        client.dispatcher.executorService.shutdown()
    }

    private class IncompleteEyevuePhoto(message: String, cause: Throwable? = null) : IOException(message, cause)

    companion object {
        private const val MAX_IMAGE_BYTES = 128L * 1024 * 1024
    }
}
