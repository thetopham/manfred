package com.thetopham.manfred_companion.eyevue

import java.io.IOException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.yield
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class EyevueBlePreviewSessionTest {
    @Test(timeout = 5_000)
    fun physicalButtonWaitsForBusyIdleAndNewCountThenSendsExactlyOnePreview() = runBlocking {
        val rig = Rig()
        val job = rig.start(this)
        rig.ready.receive()
        rig.frames.emit(shutter())
        rig.frames.emit(shutter()) // Duplicate button indication.
        assertFalse(rig.session.requestPreview())
        rig.frames.emit(status(true))
        rig.frames.emit(status(true))
        rig.count++
        rig.frames.emit(countFrame(rig.count))
        yield()
        assertEquals(0, rig.previewWrites)
        rig.frames.emit(status(false))
        rig.ready.receive()
        assertEquals(1, rig.previewWrites)
        assertEquals(1, rig.saved.size)
        assertEquals(1000L, rig.saved.single().second)
        assertArrayEquals(rig.jpeg, rig.saved.single().first)

        // Duplicate completion/count events after rearm cannot authorize a new preview.
        rig.frames.emit(status(false))
        rig.frames.emit(countFrame(rig.count))
        yield()
        assertEquals(1, rig.previewWrites)
        rig.completePhysical()
        rig.ready.receive()
        assertEquals(2, rig.previewWrites)
        assertEquals(2, rig.saved.size)
        assertTrue(rig.commands.all { it in listOf(0x40, 0x22) })
        job.cancelAndJoin()
        assertEquals(0, rig.frames.subscriptionCount.value)
    }

    @Test(timeout = 5_000)
    fun ownPreviewShutterAndStatusEchoesDuringTransferAndSaveCannotLoop() = runBlocking {
        val rig = Rig()
        rig.onPublish = {
            rig.frames.emit(shutter())
            rig.frames.emit(status(true))
            rig.frames.emit(countFrame(rig.count))
            rig.frames.emit(status(false))
        }
        val job = rig.start(this)
        rig.ready.receive()
        assertTrue(rig.session.requestPreview())
        assertFalse(rig.session.requestPreview())
        rig.ready.receive()
        yield()
        assertEquals(1, rig.previewWrites)
        assertEquals(1, rig.saved.size)
        assertTrue(rig.session.requestPreview()) // Existing app capture remains available.
        rig.ready.receive()
        assertEquals(2, rig.previewWrites)
        job.cancelAndJoin()
    }

    @Test(timeout = 5_000)
    fun countAndIdleWithoutBusyNeverRequestASecondExposure() = runBlocking {
        val rig = Rig(confirmationTimeoutMs = 80)
        val job = rig.start(this)
        rig.ready.receive()
        rig.frames.emit(shutter())
        rig.count++
        rig.frames.emit(countFrame(rig.count))
        rig.frames.emit(status(false))
        val failure = job.await().exceptionOrNull()
        assertTrue(failure is IOException)
        assertTrue(failure?.message?.contains("physical photo") == true)
        assertEquals(0, rig.previewWrites)
        assertTrue(rig.saved.isEmpty())
        assertEquals(0, rig.frames.subscriptionCount.value)
    }

    @Test(timeout = 5_000)
    fun idleBeforeCountStillCompletesOnePhysicalCapture() = runBlocking {
        val rig = Rig()
        val job = rig.start(this)
        rig.ready.receive()
        rig.frames.emit(shutter())
        rig.frames.emit(status(true))
        rig.frames.emit(status(false))
        yield()
        assertEquals(0, rig.previewWrites)
        rig.count++
        rig.frames.emit(countFrame(rig.count))
        rig.ready.receive()
        assertEquals(1, rig.previewWrites)
        job.cancelAndJoin()
    }

    @Test(timeout = 5_000)
    fun unrelatedAcknowledgementsAndStatusOnlyEventsNeverTrigger() = runBlocking {
        val rig = Rig()
        val job = rig.start(this)
        rig.ready.receive()
        rig.frames.emit(EyevueFrame(0x22, byteArrayOf(0x31)))
        rig.frames.emit(EyevueFrame(0x22, byteArrayOf(0x00)))
        rig.frames.emit(EyevueFrame(0x22, byteArrayOf(0x01, 0x00)))
        rig.frames.emit(status(true))
        rig.frames.emit(status(false))
        rig.frames.emit(countFrame(rig.count))
        yield()
        assertEquals(0, rig.previewWrites)
        assertTrue(rig.session.requestPreview())
        rig.ready.receive()
        assertEquals(1, rig.previewWrites)
        job.cancelAndJoin()
    }

    @Test(timeout = 5_000)
    fun stopDuringPhysicalPhotoRemovesObserverAndDoesNotCaptureItsLateCompletion() = runBlocking {
        val rig = Rig()
        val job = rig.start(this)
        rig.ready.receive()
        rig.frames.emit(shutter())
        rig.frames.emit(status(true))
        job.cancelAndJoin()
        assertFalse(rig.session.requestPreview())
        rig.count++
        rig.frames.emit(countFrame(rig.count))
        rig.frames.emit(status(false))
        assertEquals(0, rig.previewWrites)
        assertEquals(0, rig.frames.subscriptionCount.value)

        // A new run has a newly queried baseline; the old count cannot complete it.
        val next = rig.start(this)
        rig.ready.receive()
        rig.frames.emit(countFrame(rig.count))
        rig.frames.emit(status(false))
        yield()
        assertEquals(0, rig.previewWrites)
        rig.completePhysical()
        rig.ready.receive()
        assertEquals(1, rig.previewWrites)
        next.cancelAndJoin()
    }

    @Test(timeout = 5_000)
    fun stopDuringPreviewDropsLateBytesAndReconnectionRequiresANewTrigger() = runBlocking {
        val rig = Rig()
        rig.autoImage = false
        val job = rig.start(this)
        rig.ready.receive()
        rig.completePhysical()
        rig.previewStarted.receive()
        job.cancelAndJoin()
        assertEquals(0, rig.results.subscriptionCount.value)
        rig.results.emit(Result.success(rig.jpeg))
        assertTrue(rig.saved.isEmpty())

        rig.autoImage = true
        val next = rig.start(this)
        rig.ready.receive()
        assertEquals(1, rig.previewWrites)
        assertTrue(rig.session.requestPreview())
        rig.ready.receive()
        assertEquals(2, rig.previewWrites)
        assertEquals(1, rig.saved.size)
        next.cancelAndJoin()
    }

    @Test(timeout = 5_000)
    fun previewWriteFailureDoesNotSaveOrAutomaticallyRetry() = runBlocking {
        val rig = Rig()
        rig.failPreviewWrite = true
        val job = rig.start(this)
        rig.ready.receive()
        rig.completePhysical()
        assertTrue(job.await().exceptionOrNull() is IOException)
        assertEquals(1, rig.previewWrites)
        assertTrue(rig.saved.isEmpty())
        assertEquals(0, rig.frames.subscriptionCount.value)
        assertEquals(0, rig.results.subscriptionCount.value)
    }

    @Test(timeout = 5_000)
    fun startupWithoutFreshCountDoesNotArmOrTakeAPhoto() = runBlocking {
        val rig = Rig(queryTimeoutMs = 80)
        rig.answerQueries = false
        val job = rig.start(this)
        assertTrue(job.await().exceptionOrNull() is IOException)
        assertTrue(rig.ready.tryReceive().isFailure)
        assertFalse(rig.session.requestPreview())
        assertEquals(0, rig.previewWrites)
        assertEquals(0, rig.frames.subscriptionCount.value)
    }

    @Test(timeout = 5_000)
    fun cancellingAnAcceptedReadinessQueryDrainsItsAckBeforeAnotherRun() = runBlocking {
        val rig = Rig()
        rig.queryAck = CompletableDeferred()
        val job = rig.start(this)
        assertEquals(1, rig.commands.size)
        job.cancel()
        assertFalse(job.isCompleted)
        assertFalse(rig.session.requestPreview())
        rig.queryAck!!.complete(Unit)
        job.join()
        assertEquals(0, rig.frames.subscriptionCount.value)
        assertEquals(0, rig.previewWrites)
    }

    @Test(timeout = 5_000)
    fun initialArmAndCompletedPreviewNeedOnlyActualCountRepliesNotConfigurationQueries() = runBlocking {
        val rig = Rig()
        rig.emitPreviewStatus = false
        val job = rig.start(this)
        rig.ready.receive()
        assertEquals(listOf(0x40), rig.commands)
        assertTrue(rig.session.requestPreview())
        rig.ready.receive()
        assertEquals(listOf(0x40, 0x22, 0x40), rig.commands)
        assertEquals(1, rig.saved.size)
        assertFalse(rig.commands.contains(0x48))
        job.cancelAndJoin()
    }

    @Test(timeout = 5_000)
    fun previewBusyObservedBeforeImageMustBecomeIdleBeforeRearm() = runBlocking {
        val rig = Rig()
        rig.previewRemainsBusy = true
        val job = rig.start(this)
        rig.ready.receive()
        assertTrue(rig.session.requestPreview())
        rig.savedEvents.receive()
        yield()
        assertTrue(rig.ready.tryReceive().isFailure)
        assertFalse(rig.session.requestPreview())
        assertEquals(listOf(0x40, 0x22), rig.commands)
        rig.frames.emit(status(false))
        rig.ready.receive()
        assertEquals(listOf(0x40, 0x22, 0x40), rig.commands)
        assertEquals(1, rig.previewWrites)
        job.cancelAndJoin()
    }

    @Test(timeout = 5_000)
    fun observedBusyTimeoutStopsWithoutSendingAnyStatusQueryOrAnotherPreview() = runBlocking {
        val rig = Rig(queryTimeoutMs = 80)
        rig.previewRemainsBusy = true
        val job = rig.start(this)
        rig.ready.receive()
        assertTrue(rig.session.requestPreview())
        rig.savedEvents.receive()
        assertTrue(job.await().exceptionOrNull() is IOException)
        assertEquals(listOf(0x40, 0x22), rig.commands)
        assertEquals(1, rig.saved.size)
        assertEquals(0, rig.frames.subscriptionCount.value)
    }

    private class Rig(confirmationTimeoutMs: Long = 1000, queryTimeoutMs: Long = 1000) {
        val frames = MutableSharedFlow<EyevueFrame>()
        val results = MutableSharedFlow<Result<ByteArray>>()
        val jpeg = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 0xff.toByte(), 0xd9.toByte())
        val ready = Channel<Unit>(Channel.UNLIMITED)
        val previewStarted = Channel<Unit>(Channel.UNLIMITED)
        val commands = mutableListOf<Int>()
        val saved = mutableListOf<Pair<ByteArray, Long>>()
        val savedEvents = Channel<Unit>(Channel.UNLIMITED)
        var count = 52
        var previewWrites = 0
        var autoImage = true
        var emitPreviewStatus = true
        var previewRemainsBusy = false
        var answerQueries = true
        var failPreviewWrite = false
        var queryAck: CompletableDeferred<Unit>? = null
        var onPublish: suspend () -> Unit = {}
        private var timestamp = 1000L
        private val capture = EyevueBlePreviewCapture(results, ::write, {})
        val session = EyevueBlePreviewSession(
            frames, ::write, capture::capture,
            publish = { bytes, detectedAt ->
                onPublish()
                saved.add(bytes to detectedAt)
                savedEvents.trySend(Unit)
            },
            onStatus = { _, isReady -> if (isReady) ready.trySend(Unit) },
            now = { timestamp++ },
            confirmationTimeoutMs = confirmationTimeoutMs,
            queryTimeoutMs = queryTimeoutMs,
        )

        fun start(scope: CoroutineScope): Deferred<Result<Unit>> =
            scope.async(start = CoroutineStart.UNDISPATCHED) { runCatching { session.run() } }

        suspend fun completePhysical() {
            frames.emit(shutter())
            frames.emit(status(true))
            count++
            frames.emit(countFrame(count))
            frames.emit(status(false))
        }

        private suspend fun write(bytes: ByteArray): Result<Unit> {
            val packet = EyevueProtocol.parseDatagram(bytes)
            commands.add(packet.commandId)
            when (packet.commandId) {
                0x48 -> {
                    // Actual 0.5.5 phone trace: configuration replies only, never 0x45 idle.
                    for (command in listOf(0x01, 0x02, 0x04, 0x06, 0x07, 0x08, 0x09, 0x10, 0x11, 0x61)) {
                        frames.emit(EyevueFrame(command, byteArrayOf(0)))
                    }
                }
                0x40 -> {
                    // Trigger observer and count waiter must be armed before the real 0x42 reply.
                    assertEquals(2, frames.subscriptionCount.value)
                    queryAck?.await()
                    if (answerQueries) frames.emit(countFrame(count))
                }
                0x22 -> {
                    assertArrayEquals(byteArrayOf(0x31), packet.payload)
                    assertEquals(1, results.subscriptionCount.value)
                    previewWrites++
                    previewStarted.trySend(Unit)
                    if (failPreviewWrite) return Result.failure(IOException("rejected shutter"))
                    // Our AI capture may echo the exact event used by the physical shutter.
                    frames.emit(shutter())
                    if (emitPreviewStatus) frames.emit(status(true))
                    count++
                    frames.emit(countFrame(count))
                    if (emitPreviewStatus && !previewRemainsBusy) frames.emit(status(false))
                    if (autoImage) results.emit(Result.success(jpeg))
                }
                else -> error("Unexpected BLE session command")
            }
            return Result.success(Unit)
        }
    }

    private companion object {
        fun shutter() = EyevueFrame(0x22, byteArrayOf(0x01))
        fun status(busy: Boolean) = EyevueFrame(0x45, ByteArray(9).also { it[0] = if (busy) 1 else 0 })
        fun countFrame(value: Int) = EyevueFrame(0x42, byteArrayOf((value ushr 8).toByte(), value.toByte()))
    }
}
