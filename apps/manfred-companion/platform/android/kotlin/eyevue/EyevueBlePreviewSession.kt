package com.thetopham.manfred_companion.eyevue

import java.io.IOException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout

/**
 * Owns one BLE preview at a time. The physical shutter first stores its ordinary photo;
 * only after that capture completes do we request a SECOND exposure over AA15.
 * Call run/requestPreview on the same serialized dispatcher as the frame observer.
 */
internal class EyevueBlePreviewSession(
    private val frames: Flow<EyevueFrame>,
    private val write: suspend (ByteArray) -> Result<Unit>,
    private val capture: suspend () -> ByteArray,
    private val publish: suspend (ByteArray, Long) -> Unit,
    private val onStatus: (String, Boolean) -> Unit,
    private val onPhase: (String) -> Unit = {},
    private val now: () -> Long = System::currentTimeMillis,
    private val confirmationTimeoutMs: Long = 15_000L,
    private val queryTimeoutMs: Long = 10_000L,
) {
    private enum class State { STOPPED, REARMING, READY, PHYSICAL, PREVIEW }
    private data class Request(val detectedAt: Long, val completed: CompletableDeferred<Unit>? = null)

    private var state = State.STOPPED
    private var requests: Channel<Request>? = null
    private var physical: Request? = null
    private val gate = EyevueCaptureCycleGate()

    /** Reserve ownership before enqueueing: neither an app click nor a BLE echo can race it. */
    fun requestPreview(): Boolean {
        if (state != State.READY) return false
        state = State.PREVIEW
        gate.disarm()
        onStatus("Requesting a BLE preview", false)
        return requests?.trySend(Request(now()))?.isSuccess == true
    }

    suspend fun run(): Unit = coroutineScope {
        check(state == State.STOPPED && requests == null) { "The BLE preview session is already running" }
        val queue = Channel<Request>(1)
        requests = queue
        state = State.REARMING
        // Subscribe before any query or capture can generate status/shutter notifications.
        val observer = launch(start = CoroutineStart.UNDISPATCHED) {
            frames.collect { frame -> observe(frame, queue) }
        }
        try {
            rearm()
            for (request in queue) {
                if (request.completed != null) {
                    try {
                        withTimeout(confirmationTimeoutMs) { request.completed.await() }
                    } catch (timeout: TimeoutCancellationException) {
                        throw IOException("The glasses did not finish the physical photo; no BLE preview was requested", timeout)
                    }
                }
                currentCoroutineContext().ensureActive()
                state = State.PREVIEW
                gate.disarm()
                physical = null
                onStatus("Taking and receiving the BLE preview", false)
                onPhase("ble_preview_requested")
                val bytes = capture()
                currentCoroutineContext().ensureActive()
                onPhase("ble_preview_received")
                onStatus("Saving the BLE preview", false)
                publish(bytes, request.detectedAt)
                onPhase("ble_preview_saved")
                // Keep self-trigger suppression through saving and fresh camera/count queries.
                // A repeated old count cannot authorize the next physical capture.
                rearm()
            }
        } finally {
            state = State.STOPPED
            gate.disarm()
            physical?.completed?.cancel()
            physical = null
            requests = null
            queue.cancel()
            withContext(NonCancellable) { observer.cancelAndJoin() }
        }
    }

    private fun observe(frame: EyevueFrame, queue: Channel<Request>) {
        if (state == State.READY && frame.commandId == EyevueProtocol.CMD_TAKE_PHOTO &&
            frame.payload.contentEquals(byteArrayOf(0x01))) {
            val request = Request(now(), CompletableDeferred())
            state = State.PHYSICAL
            physical = request
            onPhase("physical_shutter_received")
            onStatus("Waiting for the glasses photo, then taking a fresh BLE preview", false)
            check(queue.trySend(request).isSuccess) { "A BLE preview is already pending" }
            return
        }
        if (state != State.PHYSICAL) return
        val completed = when (frame.commandId) {
            EyevueProtocol.CMD_GET_MEDIA_COUNT, EyevueProtocol.CMD_RECEIVE_THUMBNAIL_COUNT ->
                mediaCount(frame)?.let(gate::onMediaCount) ?: false
            0x45 -> if (frame.payload.size >= 9 && frame.payload[0].toInt() in 0..1) {
                gate.onPhotoBusy(frame.payload[0].toInt() == 1)
            } else false
            else -> false
        }
        if (completed) {
            state = State.PREVIEW
            onPhase("physical_photo_complete")
            physical?.completed?.complete(Unit)
        }
    }

    private suspend fun rearm() {
        state = State.REARMING
        gate.disarm()
        onStatus("Checking the glasses camera before the next BLE preview", false)
        query(EyevueProtocol.buildGetDeviceStatusPacket()) {
            it.commandId == 0x45 && it.payload.size >= 9 && it.payload[0].toInt() == 0
        }
        val count = query(EyevueProtocol.valuePacket(EyevueProtocol.CMD_GET_MEDIA_COUNT, 0)) {
            (it.commandId == EyevueProtocol.CMD_GET_MEDIA_COUNT ||
                it.commandId == EyevueProtocol.CMD_RECEIVE_THUMBNAIL_COUNT) && mediaCount(it) != null
        }
        currentCoroutineContext().ensureActive()
        gate.arm(mediaCount(count)!!)
        state = State.READY
        onPhase("ble_preview_armed")
        onStatus("Ready - press the glasses shutter or use Take preview", true)
    }

    private suspend fun query(packet: ByteArray, accepts: (EyevueFrame) -> Boolean): EyevueFrame =
        coroutineScope {
            val reply = async(start = CoroutineStart.UNDISPATCHED) {
                withTimeout(queryTimeoutMs) { frames.first { accepts(it) } }
            }
            try {
                currentCoroutineContext().ensureActive()
                // Like preview writes, an accepted query must drain its GATT acknowledgment.
                withContext(NonCancellable) { write(packet).getOrThrow() }
                currentCoroutineContext().ensureActive()
                reply.await()
            } catch (timeout: TimeoutCancellationException) {
                throw IOException("The glasses did not confirm camera readiness; restart the BLE preview session", timeout)
            } finally {
                reply.cancel()
            }
        }

    private fun mediaCount(frame: EyevueFrame): Int? =
        if (frame.payload.size < 2) null else
            ((frame.payload[0].toInt() and 0xff) shl 8) or (frame.payload[1].toInt() and 0xff)
}
