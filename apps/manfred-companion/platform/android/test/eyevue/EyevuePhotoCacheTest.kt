package com.thetopham.manfred_companion.eyevue

import java.io.File
import java.nio.file.Files
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class EyevuePhotoCacheTest {
    @Test fun keepsThreeNewestCompletedPhotos() = withCache { _, cache ->
        val files = (1..6).map { photo(cache, it, it * 1000L) }
        assertEquals(3, trimEyevuePhotoCache(cache, files.last()))
        files.take(3).forEach { assertFalse(it.exists()) }
        files.takeLast(3).forEach { assertTrue(it.exists()) }
    }

    @Test fun protectsPublishedLatestEvenWhenClockMovesBackward() = withCache { _, cache ->
        val older = (1..4).map { photo(cache, it, it * 1000L) }
        val latest = photo(cache, 5, 1L)
        assertEquals(2, trimEyevuePhotoCache(cache, latest))
        assertTrue(latest.exists())
        older.takeLast(2).forEach { assertTrue(it.exists()) }
        older.take(2).forEach { assertFalse(it.exists()) }
    }

    @Test fun leavesDownloadsUnrelatedFilesAndOtherDirectoriesAlone() = withCache { root, cache ->
        val files = (1..5).map { photo(cache, it, it * 1000L) }
        val pending = File(cache, "00000000-0000-0000-0000-000000000006.part").apply { writeText("partial") }
        val unrelated = File(cache, "unrelated.jpg").apply { writeText("other") }
        val nested = File(cache, "nested").apply { mkdir() }
        val nestedPhoto = photo(nested, 7, 1L)
        val omi = File(root, "omi").apply { mkdir() }
        val omiPhoto = photo(omi, 8, 1L)
        assertEquals(2, trimEyevuePhotoCache(cache, files.last()))
        listOf(pending, unrelated, nestedPhoto, omiPhoto).forEach { assertTrue(it.exists()) }
        assertEquals("partial", pending.readText())
    }

    @Test fun ignoresLinksToFilesOutsideTheCache() = withCache { root, cache ->
        val external = File(root, "gallery-original.jpg").apply { writeText("original") }
        val link = File(cache, name(99))
        Files.createSymbolicLink(link.toPath(), external.toPath())
        val files = (1..4).map { photo(cache, it, it * 1000L) }
        assertEquals(1, trimEyevuePhotoCache(cache, files.last()))
        assertTrue(Files.isSymbolicLink(link.toPath()))
        assertEquals("original", external.readText())
    }

    @Test fun refusesAnAliasedCacheDirectory() = withCache { root, cache ->
        val files = (1..4).map { photo(cache, it, it * 1000L) }
        val alias = File(root, "cache-alias")
        Files.createSymbolicLink(alias.toPath(), cache.toPath())
        assertEquals(0, trimEyevuePhotoCache(alias, File(alias, files.last().name)))
        files.forEach { assertTrue(it.exists()) }
    }

    @Test fun acceptsAndroidStyleAliasOfTrustedCacheParent() = withCache { root, cache ->
        val files = (1..4).map { photo(cache, it, it * 1000L) }
        val parentAlias = File(root, "app-data-parent-alias")
        Files.createSymbolicLink(parentAlias.toPath(), root.toPath())
        try {
            val aliasCache = File(parentAlias, "eyevue")
            assertEquals(1, trimEyevuePhotoCache(aliasCache, File(aliasCache, files.last().name)))
            assertFalse(files.first().exists())
            files.takeLast(3).forEach { assertTrue(it.exists()) }
        } finally {
            Files.deleteIfExists(parentAlias.toPath())
        }
    }

    @Test fun deletionFailuresDoNotThrowOrPreventOtherCleanup() = withCache { _, cache ->
        val files = (1..6).map { photo(cache, it, it * 1000L) }
        val attempted = mutableSetOf<File>()
        assertEquals(1, trimEyevuePhotoCache(cache, files.last()) { file ->
            attempted += file
            when (file) {
                files[0] -> false
                files[1] -> throw SecurityException("simulated cache cleanup failure")
                else -> file.delete()
            }
        })
        assertEquals(files.take(3).toSet(), attempted)
        assertTrue(files[0].exists())
        assertTrue(files[1].exists())
        assertFalse(files[2].exists())
        files.takeLast(3).forEach { assertTrue(it.exists()) }
    }

    @Test fun missingOrExternalLatestDoesNotEvictAnyCachePhoto() = withCache { root, cache ->
        val files = (1..4).map { photo(cache, it, it * 1000L) }
        assertEquals(0, trimEyevuePhotoCache(cache, File(cache, name(99))))
        assertEquals(0, trimEyevuePhotoCache(cache, photo(root, 99, 9999L)))
        files.forEach { assertTrue(it.exists()) }
    }

    private fun name(id: Int) = "00000000-0000-0000-0000-${id.toString().padStart(12, '0')}.jpg"

    private fun photo(directory: File, id: Int, modifiedAt: Long): File =
        File(directory, name(id)).apply {
            writeText("completed cache fixture")
            check(setLastModified(modifiedAt))
        }

    private fun withCache(block: (File, File) -> Unit) {
        val root = Files.createTempDirectory("eyevue-cache-test").toFile()
        val cache = File(root, "eyevue").apply { check(mkdir()) }
        try { block(root, cache) } finally { root.deleteRecursively() }
    }
}
