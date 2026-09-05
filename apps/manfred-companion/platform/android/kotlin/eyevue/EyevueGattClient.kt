// Ported from thetopham/Alternative-HeyCyan-App-and-SDK; see SOURCE_PROVENANCE.md.
package com.thetopham.manfred_companion.eyevue

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothProfile
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CancellableContinuation
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeout
import java.io.IOException
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

enum class EyevueGattState {
    DISCONNECTED,
    CONNECTING,
    CONNECTED,
    ERROR,
}

/** Native GATT transport for the Eyevue AA12/AA13/AA14 service. */
class EyevueGattClient(
    private val context: Context,
) {
    companion object {
        private const val TAG = "EyevueGatt"
        private const val OPERATION_TIMEOUT_MS = 10_000L
        private const val RAW_TRACE_BYTE_LIMIT = 32
        private val ENABLE_NOTIFICATION_VALUE = byteArrayOf(0x01, 0x00)
    }

    @Volatile
    private var suppressProbeWakeEvents = false

    fun setPhotoProbeWakeSuppression(enabled: Boolean) {
        suppressProbeWakeEvents = enabled
    }

    private val operationMutex = Mutex()
    private val decoder = EyevueFrameDecoder()
    private val _state = MutableStateFlow(EyevueGattState.DISCONNECTED)
    private val _frames = MutableSharedFlow<EyevueFrame>(extraBufferCapacity = 64)
    private val _photos = MutableSharedFlow<ByteArray>(extraBufferCapacity = 1)
    private val _photoResults = MutableSharedFlow<Result<ByteArray>>(extraBufferCapacity = 1)
    private val photoAssembler = EyevuePhotoAssembler { reason ->
        Log.w(TAG, "Photo transfer rejected: $reason")
        if (!_photoResults.tryEmit(Result.failure(IOException("Eyevue photo was incomplete: $reason")))) {
            Log.w(TAG, "Photo rejection could not be delivered to the capture listener")
        }
    }

    val state: StateFlow<EyevueGattState> = _state.asStateFlow()
    val frames: SharedFlow<EyevueFrame> = _frames.asSharedFlow()
    val photos: SharedFlow<ByteArray> = _photos.asSharedFlow()
    val photoResults: SharedFlow<Result<ByteArray>> = _photoResults.asSharedFlow()

    @Volatile
    private var gatt: BluetoothGatt? = null
    private var writeCharacteristic: BluetoothGattCharacteristic? = null
    private var notifyCharacteristic: BluetoothGattCharacteristic? = null
    private var photoNotifyCharacteristic: BluetoothGattCharacteristic? = null
    private var connectContinuation: CancellableContinuation<Unit>? = null
    private var serviceContinuation: CancellableContinuation<Unit>? = null
    private var descriptorContinuation: CancellableContinuation<Unit>? = null
    private var pendingWrite: CompletableDeferred<Boolean>? = null

    @SuppressLint("MissingPermission")
    private val callback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(
            callbackGatt: BluetoothGatt,
            status: Int,
            newState: Int,
        ) {
            if (callbackGatt !== gatt) return
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                Log.i(TAG, "GATT connected: ${callbackGatt.device.address} status=$status")
                connectContinuation?.let { continuation ->
                    connectContinuation = null
                    if (continuation.isActive) continuation.resume(Unit)
                }
                return
            }

            if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                Log.i(TAG, "GATT disconnected: status=$status")
                connectContinuation?.let { continuation ->
                    connectContinuation = null
                    if (continuation.isActive) {
                        continuation.resumeWithException(
                            IOException("Eyevue GATT disconnected during connect (status=$status)"),
                        )
                    }
                }
                pendingWrite?.complete(false)
                pendingWrite = null
                serviceContinuation?.let { continuation ->
                    serviceContinuation = null
                    if (continuation.isActive) {
                        continuation.resumeWithException(IOException("Eyevue GATT disconnected"))
                    }
                }
                descriptorContinuation?.let { continuation ->
                    descriptorContinuation = null
                    if (continuation.isActive) {
                        continuation.resumeWithException(IOException("Eyevue GATT disconnected"))
                    }
                }
                closeGatt()
                _state.value = EyevueGattState.DISCONNECTED
            }
        }

        override fun onServicesDiscovered(callbackGatt: BluetoothGatt, status: Int) {
            if (callbackGatt !== gatt) return
            if (status == BluetoothGatt.GATT_SUCCESS) {
                Log.i(TAG, "Eyevue services discovered: ${callbackGatt.services.size}")
                resolveCharacteristics(callbackGatt)
                serviceContinuation?.let { continuation ->
                    serviceContinuation = null
                    if (continuation.isActive) continuation.resume(Unit)
                }
            } else {
                serviceContinuation?.let { continuation ->
                    serviceContinuation = null
                    if (continuation.isActive) {
                        continuation.resumeWithException(
                            IOException("Eyevue service discovery failed (status=$status)"),
                        )
                    }
                }
            }
        }

        override fun onDescriptorWrite(
            callbackGatt: BluetoothGatt,
            descriptor: BluetoothGattDescriptor,
            status: Int,
        ) {
            if (callbackGatt !== gatt) return
            if (status == BluetoothGatt.GATT_SUCCESS) {
                descriptorContinuation?.let { continuation ->
                    descriptorContinuation = null
                    if (continuation.isActive) continuation.resume(Unit)
                }
            } else {
                descriptorContinuation?.let { continuation ->
                    descriptorContinuation = null
                    if (continuation.isActive) {
                        continuation.resumeWithException(
                            IOException("Eyevue notification setup failed (status=$status)"),
                        )
                    }
                }
            }
        }

        override fun onCharacteristicWrite(
            callbackGatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            status: Int,
        ) {
            if (callbackGatt !== gatt) return
            val write = pendingWrite ?: return
            pendingWrite = null
            write.complete(status == BluetoothGatt.GATT_SUCCESS)
        }

        @Suppress("DEPRECATION")
        override fun onCharacteristicChanged(
            callbackGatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
        ) {
            if (callbackGatt !== gatt) return
            handleNotification(characteristic, characteristic.value.copyOf())
        }

        override fun onCharacteristicChanged(
            callbackGatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray,
        ) {
            if (callbackGatt !== gatt) return
            handleNotification(characteristic, value.copyOf())
        }

        private fun handleNotification(
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray,
        ) {
            when (characteristic.uuid) {
                EyevueProtocol.COMMAND_NOTIFY_UUID -> {
                    Log.i(
                        "EyevueRaw",
                        "AA14 len=${value.size} preview=${value.toHexPreview()}",
                    )
                    decoder.append(value).forEach { frame ->
                        if (suppressProbeWakeEvents && frame.commandId == EyevueProtocol.CMD_RECEIVE_VOICE_DATA_START) {
                            // Fence at notification receipt, before async collectors can delay
                            // handling until after the image-pull probe has completed.
                            Log.d(TAG, "AA14 0x97 retained in raw trace; probe wake routing suppressed")
                        } else {
                            _frames.tryEmit(frame)
                        }
                    }
                }

                EyevueProtocol.PHOTO_NOTIFY_UUID -> {
                    // Keep high-volume AA15 image payloads on a separate tag and cap the
                    // preview. Full JPEG bytes belong in an explicit evidence file, not logcat.
                    Log.d(
                        "EyevueRawPhoto",
                        "AA15 len=${value.size}",
                    )
                    photoAssembler.append(value)?.let { image ->
                        Log.i(TAG, "Photo assembled: bytes=${image.size}")
                        _photos.tryEmit(image)
                        if (!_photoResults.tryEmit(Result.success(image))) {
                            Log.w(TAG, "Completed photo could not be delivered to the capture listener")
                        }
                    }
                }

                else -> {
                    Log.i(
                        "EyevueRaw",
                        "unknown uuid=${characteristic.uuid} len=${value.size} preview=${value.toHexPreview()}",
                    )
                }
            }
        }
    }

    @SuppressLint("MissingPermission")
    suspend fun connect(address: String): Result<Unit> {
        if (!hasConnectPermission()) {
            return Result.failure(SecurityException("BLUETOOTH_CONNECT permission is required"))
        }
        val normalizedAddress = address.trim()
        if (normalizedAddress.isBlank()) {
            return Result.failure(IllegalArgumentException("Eyevue Bluetooth address is empty"))
        }

        return try {
            operationMutex.withLock {
                if (_state.value == EyevueGattState.CONNECTED) return@withLock
                disconnect()
                _state.value = EyevueGattState.CONNECTING

                val bluetoothManager = context.getSystemService(Context.BLUETOOTH_SERVICE)
                    as? android.bluetooth.BluetoothManager
                    ?: throw IOException("BluetoothManager is unavailable")
                val adapter = bluetoothManager.adapter
                    ?: throw IOException("BluetoothAdapter is unavailable")
                val device = adapter.getRemoteDevice(normalizedAddress)

                withTimeout(OPERATION_TIMEOUT_MS) {
                    suspendCancellableCoroutine<Unit> { continuation ->
                        connectContinuation = continuation
                        continuation.invokeOnCancellation {
                            connectContinuation = null
                            closeGatt()
                        }
                        val opened = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                            device.connectGatt(context, false, callback, BluetoothDevice.TRANSPORT_LE)
                        } else {
                            @Suppress("DEPRECATION")
                            device.connectGatt(context, false, callback)
                        }
                        gatt = opened
                        if (opened == null) {
                            connectContinuation = null
                            continuation.resumeWithException(IOException("connectGatt returned null"))
                        }
                    }
                }

                val connectedGatt = gatt ?: throw IOException("Eyevue GATT object is missing")
                withTimeout(OPERATION_TIMEOUT_MS) {
                    suspendCancellableCoroutine<Unit> { continuation ->
                        serviceContinuation = continuation
                        continuation.invokeOnCancellation {
                            serviceContinuation = null
                            closeGatt()
                        }
                        if (!connectedGatt.discoverServices()) {
                            serviceContinuation = null
                            continuation.resumeWithException(IOException("discoverServices returned false"))
                        }
                    }
                }

                val commandWrite = writeCharacteristic
                    ?: throw IOException("Eyevue AA13 write characteristic was not found")
                val commandNotify = notifyCharacteristic
                    ?: throw IOException("Eyevue AA14 notify characteristic was not found")
                val photoNotify = photoNotifyCharacteristic
                    ?: throw IOException("Eyevue AA15 photo characteristic was not found")

                enableNotifications(connectedGatt, commandNotify, "AA14")
                enableNotifications(connectedGatt, photoNotify, "AA15")
                connectedGatt.requestMtu(512)

                commandWrite.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                _state.value = EyevueGattState.CONNECTED
            }
            Result.success(Unit)
        } catch (timeout: TimeoutCancellationException) {
            closeGatt()
            _state.value = EyevueGattState.ERROR
            Result.failure(IOException("Eyevue GATT operation timed out", timeout))
        } catch (error: Throwable) {
            closeGatt()
            _state.value = EyevueGattState.ERROR
            Result.failure(error)
        }
    }

    @SuppressLint("MissingPermission")
    suspend fun write(packet: ByteArray): Result<Unit> {
        if (!hasConnectPermission()) {
            return Result.failure(SecurityException("BLUETOOTH_CONNECT permission is required"))
        }
        if (_state.value != EyevueGattState.CONNECTED) {
            return Result.failure(IOException("Eyevue GATT is not connected"))
        }
        return operationMutex.withLock {
            val currentGatt = gatt ?: return@withLock Result.failure(IOException("Eyevue GATT is closed"))
            val characteristic = writeCharacteristic
                ?: return@withLock Result.failure(IOException("Eyevue AA13 characteristic is unavailable"))
            try {
                characteristic.value = packet
                characteristic.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                val result = CompletableDeferred<Boolean>()
                pendingWrite = result
                if (!currentGatt.writeCharacteristic(characteristic)) {
                    pendingWrite = null
                    return@withLock Result.failure(IOException("Eyevue writeCharacteristic returned false"))
                }
                if (withTimeout(OPERATION_TIMEOUT_MS) { result.await() }) {
                    Result.success(Unit)
                } else {
                    Result.failure(IOException("Eyevue characteristic write failed"))
                }
            } catch (timeout: TimeoutCancellationException) {
                pendingWrite = null
                Result.failure(IOException("Eyevue characteristic write timed out", timeout))
            } catch (error: Throwable) {
                pendingWrite = null
                Result.failure(error)
            }
        }
    }

    @SuppressLint("MissingPermission")
    fun disconnect() {
        pendingWrite?.complete(false)
        pendingWrite = null
        closeGatt()
        _state.value = EyevueGattState.DISCONNECTED
    }

    fun isConnected(): Boolean = _state.value == EyevueGattState.CONNECTED

    @SuppressLint("MissingPermission")
    private fun closeGatt() {
        val currentGatt = gatt
        gatt = null
        writeCharacteristic = null
        notifyCharacteristic = null
        photoNotifyCharacteristic = null
        try {
            currentGatt?.disconnect()
            currentGatt?.close()
        } catch (_: Throwable) {
            // Permission loss during lifecycle cleanup should not escape.
        } finally {
            decoder.reset()
            photoAssembler.reset()
        }
    }

    private fun resolveCharacteristics(currentGatt: BluetoothGatt) {
        val service = currentGatt.getService(EyevueProtocol.SERVICE_UUID)
            ?: run {
                writeCharacteristic = null
                notifyCharacteristic = null
                photoNotifyCharacteristic = null
                return
            }
        writeCharacteristic = service.getCharacteristic(EyevueProtocol.COMMAND_WRITE_UUID)
        notifyCharacteristic = service.getCharacteristic(EyevueProtocol.COMMAND_NOTIFY_UUID)
        photoNotifyCharacteristic = service.getCharacteristic(EyevueProtocol.PHOTO_NOTIFY_UUID)
    }

    @SuppressLint("MissingPermission")
    private suspend fun enableNotifications(
        currentGatt: BluetoothGatt,
        characteristic: BluetoothGattCharacteristic,
        label: String,
    ) {
        val cccd = characteristic.getDescriptor(EyevueProtocol.CCCD_UUID)
            ?: throw IOException("Eyevue $label CCCD was not found")
        withTimeout(OPERATION_TIMEOUT_MS) {
            suspendCancellableCoroutine<Unit> { continuation ->
                descriptorContinuation = continuation
                continuation.invokeOnCancellation {
                    descriptorContinuation = null
                    closeGatt()
                }
                currentGatt.setCharacteristicNotification(characteristic, true)
                cccd.value = ENABLE_NOTIFICATION_VALUE
                if (!currentGatt.writeDescriptor(cccd)) {
                    descriptorContinuation = null
                    continuation.resumeWithException(IOException("Eyevue $label descriptor write returned false"))
                }
            }
        }
    }

    private fun ByteArray.toHexPreview(): String {
        val visible = take(RAW_TRACE_BYTE_LIMIT)
            .joinToString(" ") { byte -> "%02X".format(byte.toInt() and 0xFF) }
        val omitted = size - visibleByteCount()
        return if (omitted > 0) "$visible … (+$omitted bytes)" else visible
    }

    private fun ByteArray.visibleByteCount(): Int = minOf(size, RAW_TRACE_BYTE_LIMIT)

    private fun hasConnectPermission(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.S ||
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.BLUETOOTH_CONNECT,
            ) == PackageManager.PERMISSION_GRANTED
}
