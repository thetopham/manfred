package com.thetopham.manfred_companion.eyevue

import android.content.Context
import android.net.Network
import java.io.File
import java.io.IOException
import java.net.URLEncoder
import java.util.UUID
import kotlinx.coroutines.Dispatchers
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


    /** Baseline belongs to the logical capture session, not this network attachment. */
    suspend fun snapshot(): Map<String, EyevuePhotoFingerprint> =
        fetchManifest().associate { it.path to it.fingerprint }

    /** Retrieve a completed shutter against the baseline retained before Wi-Fi was closed. */
    suspend fun fetchNewPhotos(
        sessionId: String,
        tracker: EyevueNewPhotoTracker,
        onStatus: (String, Boolean) -> Unit,
        onImage: (Map<String, Any?>) -> Unit,
    ): Int = kotlinx.coroutines.withTimeout(60_000L) {
        val detected = mutableMapOf<Pair<String, EyevuePhotoFingerprint>, Long>()
        val retries = mutableMapOf<Pair<String, EyevuePhotoFingerprint>, Int>()
        var imported = 0
        var settled = false
        var previousSnapshot: Map<String, EyevuePhotoFingerprint>? = null
        tracker.observe(emptyMap())
        while (!settled) {
            currentCoroutineContext().ensureActive()
            val photos = fetchManifest()
            val byPath = photos.associateBy { it.path }
            val snapshot = photos.associate { it.path to it.fingerprint }
            val candidates = tracker.observe(snapshot)
            // Do not leave another new photo behind merely because the first import succeeded.
            if (imported > 0 && snapshot == previousSnapshot && candidates.isEmpty()) {
                settled = true
                continue
            }
            val now = System.currentTimeMillis()
            for (photo in photos) detected.putIfAbsent(photo.path to photo.fingerprint, now)
            for ((path, fingerprint) in candidates) {
                val key = path to fingerprint
                onStatus("Fetching the new photo", false)
                try {
                    downloadAndSave(byPath.getValue(path), sessionId, detected.getValue(key)) { image ->
                        tracker.markImported(path, fingerprint)
                        retries.remove(key)
                        imported++
                        onImage(image)
                    }
                } catch (failure: IncompleteEyevuePhoto) {
                    currentCoroutineContext().ensureActive()
                    val attempt = (retries[key] ?: 0) + 1
                    retries[key] = attempt
                    if (attempt >= 5) throw IOException("The new photo remained incomplete after five attempts", failure)
                    onStatus("Waiting for the completed original ($attempt/5)", false)
                }
            }
            previousSnapshot = snapshot
            delay(750)
        }
        imported
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
        val started = android.os.SystemClock.elapsedRealtime()
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
            EyevuePhotoStore(context).publishPending(
                pendingFile, id, sessionId, detectedAt, "wifi_original", expectedBytes,
                started, onCommitted,
            )
        } finally {
            pendingFile.delete()
        }
    }

    fun close() {
        client.dispatcher.cancelAll()
        client.connectionPool.evictAll()
        client.dispatcher.executorService.shutdown()
    }

    companion object {
        private const val MAX_IMAGE_BYTES = 128L * 1024 * 1024
    }
}
