package com.thetopham.manfred_companion.eyevue

import java.io.File
import java.io.RandomAccessFile

/** Reject path traversal and only accept original JPEG entries from the device manifest. */
internal fun normalizeEyevuePhotoPath(name: String): String? {
    val path = name.trim().replace('\\', '/').replace(Regex("^[A-Za-z]:"), "").trimStart('/')
    val parts = path.split('/').filter { it.isNotEmpty() }
    if (parts.isEmpty() || parts.any { it == "." || it == ".." }) return null
    val normalized = parts.joinToString("/")
    if (!normalized.endsWith(".jpg", true) && !normalized.endsWith(".jpeg", true)) return null
    return normalized
}

/** Network completion and JPEG delimiters are checked before Android's decoder validation. */
internal fun hasCompleteJpegEnvelope(file: File, expectedBytes: Long): Boolean {
    val size = file.length()
    if (size < 4 || (expectedBytes >= 0 && size != expectedBytes)) return false
    return RandomAccessFile(file, "r").use { input ->
        if (input.readUnsignedShort() != 0xffd8) return@use false
        // The verified TK8 original includes up to three zero alignment bytes
        // after EOI. Preserve those original bytes; reject other trailing data.
        var end = size
        var padding = 0
        while (padding < 3 && end > 4) {
            input.seek(end - 1)
            if (input.readUnsignedByte() != 0) break
            end--
            padding++
        }
        input.seek(end - 2)
        input.readUnsignedShort() == 0xffd9
    }
}
