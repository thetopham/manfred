package com.thetopham.manfred_companion.eyevue

import java.io.File
import java.nio.file.Files

private val completedEyevueCacheName = Regex(
    "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\.jpg",
)

/** Best-effort retention after publication: latest preview plus two recent completed JPEGs. */
internal fun trimEyevuePhotoCache(
    cacheDirectory: File,
    latestCompleted: File,
    deleteFile: (File) -> Boolean = { it.delete() },
): Int = try {
    // Android may alias the trusted app-data parent (/data/user/0 -> /data/data).
    // Accept that parent, but never follow a link replacing the cache or a JPEG.
    val directory = cacheDirectory.canonicalFile
    val latest = latestCompleted.canonicalFile
    if (!directory.isDirectory || Files.isSymbolicLink(cacheDirectory.toPath()) ||
        Files.isSymbolicLink(latestCompleted.toPath())
    ) {
        0
    } else {
        val candidates = directory.listFiles().orEmpty().filter { file ->
            completedEyevueCacheName.matches(file.name) && file.isFile &&
                !Files.isSymbolicLink(file.toPath())
        }
        // A missing/invalid latest preview is not permission to evict other previews.
        if (latest !in candidates) {
            0
        } else {
            val retained = candidates.filterNot { it == latest }
                .sortedWith(compareByDescending<File> { it.lastModified() }.thenBy { it.name })
                .take(2).toSet() + latest
            candidates.filterNot { it in retained }.count { file ->
                try { deleteFile(file) } catch (_: Exception) { false }
            }
        }
    }
} catch (_: Exception) {
    // Cleanup must never turn a committed MediaStore import into a failed capture.
    0
}
