// Ported regression coverage; source hashes in kotlin/eyevue/SOURCE_PROVENANCE.md.
package com.thetopham.manfred_companion.eyevue

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class EyevueNewPhotoTrackerTest {
    private val first = EyevuePhotoFingerprint(reportedSize = 463, timecode = 100)
    private val changed = EyevuePhotoFingerprint(reportedSize = 464, timecode = 101)

    @Test
    fun baselineIsNeverQueuedAndConstructorCopiesIt() {
        val baseline = linkedMapOf("old.jpg" to first)
        val tracker = EyevueNewPhotoTracker(baseline)
        baseline.clear()
        repeat(3) { assertTrue(tracker.observe(mapOf("old.jpg" to first)).isEmpty()) }
    }

    @Test
    fun newPhotosRequireTwoConsecutivePollsAndPreserveManifestOrder() {
        val tracker = EyevueNewPhotoTracker(mapOf("old.jpg" to first))
        val snapshot = linkedMapOf("second.jpg" to changed, "old.jpg" to first, "first.jpg" to first)
        assertTrue(tracker.observe(snapshot).isEmpty())
        assertEquals(listOf("second.jpg", "first.jpg"), tracker.observe(snapshot).keys.toList())
    }

    @Test
    fun changingMetadataRestartsStabilityWithoutTreatingReportedSizeAsBytes() {
        val tracker = EyevueNewPhotoTracker(emptyMap())
        assertTrue(tracker.observe(mapOf("new.jpg" to first)).isEmpty())
        assertTrue(tracker.observe(mapOf("new.jpg" to changed)).isEmpty())
        assertEquals(mapOf("new.jpg" to changed), tracker.observe(mapOf("new.jpg" to changed)))
    }

    @Test
    fun disappearanceRestartsStability() {
        val tracker = EyevueNewPhotoTracker(emptyMap())
        tracker.observe(mapOf("new.jpg" to first))
        tracker.observe(emptyMap())
        assertTrue(tracker.observe(mapOf("new.jpg" to first)).isEmpty())
        assertEquals(mapOf("new.jpg" to first), tracker.observe(mapOf("new.jpg" to first)))
    }

    @Test
    fun partialFailureOrCancellationRetriesOnlyUnconfirmedImports() {
        val tracker = EyevueNewPhotoTracker(emptyMap())
        val snapshot = linkedMapOf("saved.jpg" to first, "unfinished.jpg" to changed)
        tracker.observe(snapshot)
        assertEquals(snapshot, tracker.observe(snapshot))
        tracker.markImported("saved.jpg", first)
        // Failure or cancellation does not call markImported for the second file.
        repeat(2) {
            assertEquals(mapOf("unfinished.jpg" to changed), tracker.observe(snapshot))
        }
        tracker.markImported("unfinished.jpg", changed)
        assertTrue(tracker.observe(snapshot).isEmpty())
    }

    @Test
    fun successfullyImportedPathWithNewFingerprintIsEvaluatedAgain() {
        val tracker = EyevueNewPhotoTracker(emptyMap())
        tracker.observe(mapOf("photo.jpg" to first))
        tracker.observe(mapOf("photo.jpg" to first))
        tracker.markImported("photo.jpg", first)
        assertTrue(tracker.observe(mapOf("photo.jpg" to changed)).isEmpty())
        assertEquals(mapOf("photo.jpg" to changed), tracker.observe(mapOf("photo.jpg" to changed)))
    }

    @Test
    fun baselineAndAlreadyImportedFingerprintsRemainIgnoredAfterReappearance() {
        val tracker = EyevueNewPhotoTracker(mapOf("photo.jpg" to first))
        tracker.observe(mapOf("photo.jpg" to changed))
        tracker.observe(mapOf("photo.jpg" to changed))
        tracker.markImported("photo.jpg", changed)
        tracker.observe(emptyMap())
        repeat(2) { assertTrue(tracker.observe(mapOf("photo.jpg" to changed)).isEmpty()) }
        repeat(2) { assertTrue(tracker.observe(mapOf("photo.jpg" to first)).isEmpty()) }
    }

    @Test
    fun observingCopiesSnapshotSoCallerMutationCannotFakeStability() {
        val tracker = EyevueNewPhotoTracker(emptyMap())
        val snapshot = linkedMapOf("photo.jpg" to first)
        tracker.observe(snapshot)
        snapshot["photo.jpg"] = changed
        assertTrue(tracker.observe(snapshot).isEmpty())
        assertEquals(snapshot, tracker.observe(snapshot))
    }
}
