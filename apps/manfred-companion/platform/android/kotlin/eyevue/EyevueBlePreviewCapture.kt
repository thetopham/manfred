package com.thetopham.manfred_companion.eyevue

import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout

/** Proven CyanBridge AI-photo sequence; the firmware emits a 320x180 AA15 JPEG. */
internal class EyevueBlePreviewCapture(
    private val results: Flow<Result<ByteArray>>,
    private val write: suspend (ByteArray) -> Result<Unit>,
    private val resetTransfer: () -> Unit,
    private val timeoutMs: Long = 12_000L,
) {
    private val captureMutex = Mutex()

    suspend fun capture(): ByteArray {
        check(captureMutex.tryLock()) { "A BLE preview is already being captured" }
        try {
            resetTransfer()
            return withTimeout(timeoutMs) {
                coroutineScope {
                    // Subscribe before the write, including an immediate/rejected AA15 result.
                    val reply = async(start = CoroutineStart.UNDISPATCHED) {
                        results.first().getOrThrow()
                    }
                    try {
                        currentCoroutineContext().ensureActive()
                        // An accepted GATT write is already bounded by its ten-second
                        // transport timeout. Drain its acknowledgement before Stop can
                        // tear down the session or issue another command.
                        withContext(NonCancellable) {
                            write(EyevueProtocol.buildPhotoPacket(highQuality = true)).getOrThrow()
                        }
                        currentCoroutineContext().ensureActive()
                        reply.await()
                    } finally {
                        reply.cancel()
                    }
                }
            }
        } finally {
            resetTransfer()
            captureMutex.unlock()
        }
    }
}
