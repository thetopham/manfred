package com.thetopham.manfred_companion.eyevue

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class EyevuePhotoValidationTest {
    @Test fun rejectsTruncatedResponseDespiteValidJpegDelimiters() = withFile(byteArrayOf(0xff.toByte(), 0xd8.toByte(), 1, 2, 0xff.toByte(), 0xd9.toByte())) {
        assertFalse(hasCompleteJpegEnvelope(it, 20))
        assertTrue(hasCompleteJpegEnvelope(it, 6))
        assertTrue(hasCompleteJpegEnvelope(it, -1))
    }

    @Test fun acceptsObservedZeroAlignmentAfterJpegEndWithoutChangingNetworkLength() {
        val jpeg = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 1, 2, 0xff.toByte(), 0xd9.toByte())
        for (padding in 1..3) {
            val original = jpeg + ByteArray(padding)
            withFile(original) {
                assertTrue(hasCompleteJpegEnvelope(it, original.size.toLong()))
                assertFalse(hasCompleteJpegEnvelope(it, jpeg.size.toLong()))
                assertEquals(original.size.toLong(), it.length())
            }
        }
    }

    @Test fun refusesNonzeroTrailingDataAndExcessPadding() {
        val jpeg = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 1, 2, 0xff.toByte(), 0xd9.toByte())
        withFile(jpeg + byteArrayOf(1, 0, 0)) { assertFalse(hasCompleteJpegEnvelope(it, -1)) }
        withFile(jpeg + ByteArray(4)) { assertFalse(hasCompleteJpegEnvelope(it, -1)) }
    }

    @Test fun rejectsMissingEndMarkerEvenWhenHttpLengthMatches() = withFile(byteArrayOf(0xff.toByte(), 0xd8.toByte(), 1, 2)) {
        assertFalse(hasCompleteJpegEnvelope(it, 4))
        assertFalse(hasCompleteJpegEnvelope(it, -1))
    }

    @Test fun rejectsNonJpegAndEmptyResponses() {
        withFile(byteArrayOf()) { assertFalse(hasCompleteJpegEnvelope(it, 0)) }
        withFile("HTML error".toByteArray()) { assertFalse(hasCompleteJpegEnvelope(it, -1)) }
    }

    @Test fun normalizesVendorWindowsPathsWithoutChangingOriginalName() {
        assertEquals("DCIM/IMG_001.JPG", normalizeEyevuePhotoPath("C:\\DCIM\\IMG_001.JPG"))
        assertEquals("DCIM/IMG_002.jpeg", normalizeEyevuePhotoPath("/DCIM/IMG_002.jpeg"))
    }

    @Test fun refusesTraversalAndNonPhotoManifestEntries() {
        assertNull(normalizeEyevuePhotoPath("/DCIM/../private.jpg"))
        assertNull(normalizeEyevuePhotoPath("/DCIM/./photo.jpg"))
        assertNull(normalizeEyevuePhotoPath("/DCIM/movie.mp4"))
        assertNull(normalizeEyevuePhotoPath(""))
    }

    private fun withFile(bytes: ByteArray, block: (File) -> Unit) {
        val file = File.createTempFile("eyevue-validation", ".jpg")
        try {
            file.writeBytes(bytes)
            block(file)
        } finally {
            file.delete()
        }
    }
}
