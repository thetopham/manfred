package com.thetopham.manfred_companion.eyevue

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class EyevueCaptureCycleGateTest {
    @Test
    fun staleInitialCountAndIdleCannotTrigger() {
        val gate = EyevueCaptureCycleGate()
        assertNull(gate.latestMediaCount)
        assertFalse(gate.onMediaCount(20))
        gate.arm(20)

        repeat(3) {
            assertFalse(gate.onPhotoBusy(false))
            assertFalse(gate.onMediaCount(20))
        }
        assertTrue(gate.isArmed)
    }

    @Test
    fun countBeforeBusyAndIdleTriggersOnlyAtCompletion() {
        val gate = armedGate()
        assertFalse(gate.onMediaCount(21))
        assertFalse(gate.onPhotoBusy(false))
        assertFalse(gate.onPhotoBusy(true))
        assertTrue(gate.onPhotoBusy(false))
        assertFalse(gate.isArmed)
        assertEquals(21, gate.latestMediaCount)
    }

    @Test
    fun countDuringBusyWaitsForIdle() {
        val gate = armedGate()
        assertFalse(gate.onPhotoBusy(true))
        assertFalse(gate.onMediaCount(21))
        assertFalse(gate.onPhotoBusy(true))
        assertTrue(gate.onPhotoBusy(false))
    }

    @Test
    fun countAfterIdleTriggersOnce() {
        val gate = armedGate()
        assertFalse(gate.onPhotoBusy(true))
        assertFalse(gate.onPhotoBusy(false))
        assertFalse(gate.onMediaCount(20))
        assertTrue(gate.onMediaCount(21))

        repeat(3) {
            assertFalse(gate.onPhotoBusy(false))
            assertFalse(gate.onMediaCount(21))
            assertFalse(gate.onPhotoBusy(true))
        }
        assertFalse(gate.onMediaCount(22))
        assertEquals(22, gate.latestMediaCount)
        assertFalse(gate.isArmed)
    }

    @Test
    fun countAndIdleWithoutObservedBusyStayArmed() {
        val gate = armedGate()
        assertFalse(gate.onMediaCount(21))
        repeat(3) { assertFalse(gate.onPhotoBusy(false)) }
        assertTrue(gate.isArmed)
    }

    @Test
    fun disarmedCaptureUpdatesCountButCannotLeakIntoNextArm() {
        val gate = armedGate()
        assertFalse(gate.onPhotoBusy(true))
        gate.disarm()

        assertFalse(gate.onMediaCount(21))
        assertFalse(gate.onPhotoBusy(false))
        assertFalse(gate.onPhotoBusy(true))
        assertFalse(gate.onMediaCount(22))
        assertFalse(gate.onPhotoBusy(false))
        assertEquals(22, gate.latestMediaCount)

        gate.arm(22)
        assertFalse(gate.onMediaCount(23))
        assertFalse(gate.onPhotoBusy(false))
        assertFalse(gate.onPhotoBusy(true))
        assertTrue(gate.onPhotoBusy(false))
    }

    @Test
    fun rearmAtLatestCountRequiresAnotherCountAdvance() {
        val gate = armedGate()
        gate.onPhotoBusy(true)
        gate.onMediaCount(21)
        assertTrue(gate.onPhotoBusy(false))

        gate.arm(21)
        assertFalse(gate.onMediaCount(21))
        assertFalse(gate.onPhotoBusy(true))
        assertFalse(gate.onPhotoBusy(false))
        assertFalse(gate.onMediaCount(21))
        assertTrue(gate.onMediaCount(22))
    }

    @Test
    fun rearmingClearsBothBusyAndCompletedEvidence() {
        val busyGate = armedGate()
        busyGate.onPhotoBusy(true)
        busyGate.arm(20)
        assertFalse(busyGate.onMediaCount(21))
        assertFalse(busyGate.onPhotoBusy(false))
        assertTrue(busyGate.isArmed)

        val completedGate = armedGate()
        completedGate.onPhotoBusy(true)
        completedGate.onPhotoBusy(false)
        completedGate.arm(20)
        assertFalse(completedGate.onMediaCount(21))
        assertFalse(completedGate.onPhotoBusy(false))
        assertFalse(completedGate.onPhotoBusy(true))
        assertTrue(completedGate.onPhotoBusy(false))
    }

    @Test
    fun anotherBusyBeforeCountResumesWaitingForIdle() {
        val gate = armedGate()
        gate.onPhotoBusy(true)
        gate.onPhotoBusy(false)
        assertFalse(gate.onPhotoBusy(true))
        assertFalse(gate.onMediaCount(21))
        assertTrue(gate.onPhotoBusy(false))
    }

    @Test
    fun olderAndAmbiguousCountsDoNotSatisfyAnArmedCycle() {
        val gate = armedGate()
        gate.onPhotoBusy(true)
        gate.onPhotoBusy(false)

        assertFalse(gate.onMediaCount(19))
        assertFalse(gate.onMediaCount(20 + 0x8000))
        assertFalse(gate.onMediaCount(20))
        assertTrue(gate.onMediaCount(21))
    }

    @Test
    fun unsignedCountWrapCanCompleteACycle() {
        val gate = EyevueCaptureCycleGate()
        gate.arm(0xffff)
        gate.onPhotoBusy(true)
        gate.onPhotoBusy(false)
        assertTrue(gate.onMediaCount(0))

        gate.arm(0)
        gate.onPhotoBusy(true)
        gate.onPhotoBusy(false)
        assertFalse(gate.onMediaCount(0xffff))
        assertTrue(gate.onMediaCount(1))
    }

    @Test
    fun invalidCountsCannotReplaceTheLatestObservationOrTrigger() {
        val gate = armedGate()
        gate.onPhotoBusy(true)
        gate.onPhotoBusy(false)
        assertFalse(gate.onMediaCount(-1))
        assertFalse(gate.onMediaCount(0x10000))
        assertEquals(20, gate.latestMediaCount)
        assertTrue(gate.onMediaCount(21))
        assertFalse(gate.onMediaCount(Int.MAX_VALUE))
        assertEquals(21, gate.latestMediaCount)
    }

    @Test(expected = IllegalArgumentException::class)
    fun negativeBaselineIsRejected() {
        EyevueCaptureCycleGate().arm(-1)
    }

    @Test(expected = IllegalArgumentException::class)
    fun oversizedBaselineIsRejected() {
        EyevueCaptureCycleGate().arm(0x10000)
    }

    @Test
    fun staleCountAfterFreshCountCannotEraseCaptureEvidence() {
        val gate = armedGate()
        assertFalse(gate.onMediaCount(21))
        assertTrue(gate.hasFreshCount)

        assertFalse(gate.onMediaCount(20))
        assertFalse(gate.onMediaCount(19))
        assertFalse(gate.onMediaCount(20 + 0x8000))
        assertEquals(21, gate.latestMediaCount)
        assertTrue(gate.hasFreshCount)
        assertFalse(gate.onPhotoBusy(true))
        assertTrue(gate.onPhotoBusy(false))
        assertFalse(gate.hasFreshCount)
    }

    @Test
    fun freshCountAloneIsObservableWithoutAuthorizingAFetch() {
        val gate = armedGate()
        assertFalse(gate.hasFreshCount)
        assertFalse(gate.onMediaCount(21))
        assertTrue(gate.hasFreshCount)
        assertTrue(gate.isArmed)
        assertFalse(gate.onPhotoBusy(false))
        assertTrue(gate.hasFreshCount)

        gate.disarm()
        assertFalse(gate.hasFreshCount)
        assertFalse(gate.onMediaCount(22))
        assertEquals(22, gate.latestMediaCount)
        assertFalse(gate.hasFreshCount)
        gate.arm(22)
        assertFalse(gate.hasFreshCount)
    }

    @Test
    fun armedCountAdvancesRemainMonotonicAcrossUnsignedWrap() {
        val gate = EyevueCaptureCycleGate()
        gate.arm(0xfffe)
        assertFalse(gate.onMediaCount(1))
        assertFalse(gate.onMediaCount(0))
        assertFalse(gate.onMediaCount(0xffff))
        assertEquals(1, gate.latestMediaCount)
        assertTrue(gate.hasFreshCount)
        assertFalse(gate.onPhotoBusy(true))
        assertTrue(gate.onPhotoBusy(false))
    }

    @Test
    fun retainingInitialBaselineAcrossApRejoinFindsTheCapturedPhoto() {
        val oldPhoto = EyevuePhotoFingerprint(400, 100)
        val capturedPhoto = EyevuePhotoFingerprint(463, 101)
        val initialManifest = mapOf("old.jpg" to oldPhoto)
        val tracker = EyevueNewPhotoTracker(initialManifest)
        val gate = armedGate()

        // The initial AP attachment establishes this baseline before closing for capture.
        assertTrue(tracker.observe(initialManifest).isEmpty())
        completeCapture(gate, 21)

        // Rejoin with the same tracker: using this manifest as a new baseline would hide new.jpg.
        val rejoinedManifest = initialManifest + ("new.jpg" to capturedPhoto)
        assertTrue(tracker.observe(rejoinedManifest).isEmpty())
        assertEquals(mapOf("new.jpg" to capturedPhoto), tracker.observe(rejoinedManifest))
    }

    @Test
    fun successfulImportsRemainSuppressedAcrossTheNextCaptureCycle() {
        val firstPhoto = EyevuePhotoFingerprint(463, 101)
        val secondPhoto = EyevuePhotoFingerprint(480, 102)
        val tracker = EyevueNewPhotoTracker(emptyMap())
        val gate = armedGate()
        completeCapture(gate, 21)

        val firstManifest = mapOf("first.jpg" to firstPhoto)
        assertTrue(tracker.observe(firstManifest).isEmpty())
        assertEquals(firstManifest, tracker.observe(firstManifest))
        tracker.markImported("first.jpg", firstPhoto)

        // Detach for another capture, retaining the import receipt and original baseline.
        tracker.observe(emptyMap())
        gate.arm(21)
        completeCapture(gate, 22)
        val secondManifest = firstManifest + ("second.jpg" to secondPhoto)
        assertTrue(tracker.observe(secondManifest).isEmpty())
        assertEquals(mapOf("second.jpg" to secondPhoto), tracker.observe(secondManifest))
        assertEquals(mapOf("second.jpg" to secondPhoto), tracker.observe(secondManifest))
    }

    @Test
    fun unconfirmedImportRemainsEligibleAfterSnapshotResetAndRearm() {
        val unfinishedPhoto = EyevuePhotoFingerprint(463, 101)
        val nextPhoto = EyevuePhotoFingerprint(480, 102)
        val tracker = EyevueNewPhotoTracker(emptyMap())
        val gate = armedGate()
        completeCapture(gate, 21)

        val firstManifest = mapOf("unfinished.jpg" to unfinishedPhoto)
        assertTrue(tracker.observe(firstManifest).isEmpty())
        assertEquals(firstManifest, tracker.observe(firstManifest))
        // A failed or cancelled import never calls markImported.
        assertTrue(tracker.observe(emptyMap()).isEmpty())

        gate.arm(21)
        completeCapture(gate, 22)
        val rejoinedManifest = firstManifest + ("next.jpg" to nextPhoto)
        assertTrue(tracker.observe(rejoinedManifest).isEmpty())
        assertEquals(rejoinedManifest, tracker.observe(rejoinedManifest))
    }

    private fun completeCapture(gate: EyevueCaptureCycleGate, count: Int) {
        assertFalse(gate.onPhotoBusy(true))
        assertFalse(gate.onMediaCount(count))
        assertTrue(gate.onPhotoBusy(false))
        assertFalse(gate.isArmed)
    }

    private fun armedGate() = EyevueCaptureCycleGate().apply { arm(20) }
}
