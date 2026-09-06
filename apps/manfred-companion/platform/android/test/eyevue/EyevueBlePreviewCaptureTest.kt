package com.thetopham.manfred_companion.eyevue

import java.io.IOException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class EyevueBlePreviewCaptureTest {
    private val jpeg = byteArrayOf(0xff.toByte(), 0xd8.toByte(), 0xff.toByte(), 0xd9.toByte())

    @Test(timeout = 5_000)
    fun subscribesBeforeTheProvenAiPhotoWriteAndReturnsItsOneResult() = runBlocking {
        val results = MutableSharedFlow<Result<ByteArray>>()
        val packets = mutableListOf<ByteArray>()
        var resets = 0
        val capture = EyevueBlePreviewCapture(results, { packet ->
            assertEquals(1, results.subscriptionCount.value)
            packets.add(packet)
            results.emit(Result.success(jpeg))
            Result.success(Unit)
        }, { resets++ })
        assertArrayEquals(jpeg, capture.capture())
        assertEquals(1, packets.size)
        val command = EyevueProtocol.parseDatagram(packets.single())
        assertEquals(0x22, command.commandId)
        assertArrayEquals(byteArrayOf(0x31), command.payload)
        assertEquals(2, resets)
        assertEquals(0, results.subscriptionCount.value)
    }

    @Test(timeout = 5_000)
    fun aRejectedWriteDoesNotWaitForOrRetryAnImage() = runBlocking {
        val results = MutableSharedFlow<Result<ByteArray>>()
        var writes = 0
        var resets = 0
        val capture = EyevueBlePreviewCapture(results, {
            writes++
            Result.failure(IOException("write rejected"))
        }, { resets++ })
        val failure = runCatching { capture.capture() }.exceptionOrNull()
        assertTrue(failure is IOException)
        assertEquals("write rejected", failure?.message)
        assertEquals(1, writes)
        assertEquals(2, resets)
        assertEquals(0, results.subscriptionCount.value)
    }

    @Test(timeout = 5_000)
    fun incompleteAa15TransferIsAnErrorWithoutAnotherShutter() = runBlocking {
        val results = MutableSharedFlow<Result<ByteArray>>()
        var writes = 0
        val capture = EyevueBlePreviewCapture(results, {
            writes++
            results.emit(Result.failure(IOException("missing image chunk")))
            Result.success(Unit)
        }, {})
        val failure = runCatching { capture.capture() }.exceptionOrNull()
        assertTrue(failure is IOException)
        assertEquals("missing image chunk", failure?.message)
        assertEquals(1, writes)
        assertEquals(0, results.subscriptionCount.value)
    }

    @Test(timeout = 5_000)
    fun transferTimeoutReleasesListenerAndNeverRetriesAutomatically() = runBlocking {
        val results = MutableSharedFlow<Result<ByteArray>>()
        var writes = 0
        var resets = 0
        val capture = EyevueBlePreviewCapture(results, {
            writes++
            Result.success(Unit)
        }, { resets++ }, timeoutMs = 250L)
        assertNull(withTimeoutOrNull(2000L) {
            try {
                capture.capture()
                error("Unexpected image")
            } catch (_: kotlinx.coroutines.TimeoutCancellationException) {
                null
            }
        })
        assertEquals(1, writes)
        assertEquals(2, resets)
        assertEquals(0, results.subscriptionCount.value)
    }

    @Test(timeout = 5_000)
    fun aConcurrentRequestCannotSendOrResetTheOwnedTransfer() = runBlocking {
        val results = MutableSharedFlow<Result<ByteArray>>()
        val releaseWrite = CompletableDeferred<Unit>()
        var writes = 0
        var resets = 0
        val capture = EyevueBlePreviewCapture(results, {
            writes++
            releaseWrite.await()
            results.emit(Result.success(jpeg))
            Result.success(Unit)
        }, { resets++ })
        val first = async(start = CoroutineStart.UNDISPATCHED) { capture.capture() }
        val failure = runCatching { capture.capture() }.exceptionOrNull()
        assertTrue(failure is IllegalStateException)
        assertEquals(1, writes)
        assertEquals(1, resets)
        releaseWrite.complete(Unit)
        assertArrayEquals(jpeg, first.await())
        assertEquals(2, resets)
    }

    @Test(timeout = 5_000)
    fun stopDrainsAnAcceptedWriteBeforeReleasingCaptureOwnership() = runBlocking {
        val results = MutableSharedFlow<Result<ByteArray>>()
        val acknowledgement = CompletableDeferred<Unit>()
        var returnedImage = false
        var resets = 0
        val capture = EyevueBlePreviewCapture(results, {
            acknowledgement.await()
            Result.success(Unit)
        }, { resets++ })
        val pending = launch(start = CoroutineStart.UNDISPATCHED) {
            capture.capture()
            returnedImage = true
        }
        pending.cancel()
        assertFalse(pending.isCompleted)
        assertEquals(1, resets)
        acknowledgement.complete(Unit)
        pending.join()
        assertFalse(returnedImage)
        assertEquals(2, resets)
        assertEquals(0, results.subscriptionCount.value)
    }

    @Test(timeout = 5_000)
    fun stoppingWhileAwaitingPhotoRemovesTheListenerAndDoesNotPublishLateBytes() = runBlocking {
        val results = MutableSharedFlow<Result<ByteArray>>()
        var returnedImage = false
        var writes = 0
        val capture = EyevueBlePreviewCapture(results, {
            writes++
            Result.success(Unit)
        }, {})
        val pending = launch(start = CoroutineStart.UNDISPATCHED) {
            capture.capture()
            returnedImage = true
        }
        assertEquals(1, results.subscriptionCount.value)
        pending.cancelAndJoin()
        results.emit(Result.success(jpeg))
        assertFalse(returnedImage)
        assertEquals(1, writes)
        assertEquals(0, results.subscriptionCount.value)
    }

    @Test(timeout = 5_000)
    fun sequentialExplicitRequestsEachUseANewSingleCapture() = runBlocking {
        val results = MutableSharedFlow<Result<ByteArray>>()
        var writes = 0
        val capture = EyevueBlePreviewCapture(results, {
            writes++
            results.emit(Result.success(jpeg + byteArrayOf(writes.toByte())))
            Result.success(Unit)
        }, {})
        assertArrayEquals(jpeg + byteArrayOf(1), capture.capture())
        assertArrayEquals(jpeg + byteArrayOf(2), capture.capture())
        assertEquals(2, writes)
        assertEquals(0, results.subscriptionCount.value)
    }
}
