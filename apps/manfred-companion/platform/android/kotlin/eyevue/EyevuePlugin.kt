package com.thetopham.manfred_companion.eyevue

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.ClipData
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.core.content.ContextCompat
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.io.IOException
import java.util.UUID
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Native EyeVue photo sessions. No microphone, audio-route, upload, or ChatGPT operations.
 * The Flutter owner keeps its connected-device foreground lease until sessionActive is false.
 */
class EyevuePlugin(
    activity: Activity,
    messenger: BinaryMessenger,
) : MethodChannel.MethodCallHandler, EventChannel.StreamHandler {
    private val context = activity.applicationContext
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val mainHandler = Handler(Looper.getMainLooper())
    private val methods = MethodChannel(messenger, "manfred/eyevue")
    private val events = EventChannel(messenger, "manfred/eyevue/events")
    private val gatt = EyevueGattClient(context)
    private val preferences = context.getSharedPreferences("manfred_eyevue", Context.MODE_PRIVATE)
    private var sink: EventChannel.EventSink? = null
    private var disposed = false
    private var connecting = false
    private var disconnecting = false
    private var sessionActive = false
    private var ready = false
    private var status = "Disconnected"
    private var error: String? = null
    private var address: String? = preferences.getString("address", null)
    private var customer: EyevueCustomer? = null
    private var latestImage: Map<String, Any?>? = null
    private var connectionJob: Job? = null
    private var sessionJob: Job? = null
    private var captureJob: Job? = null
    private var sessionId: String? = null
    private var sessionFailure: String? = null
    private var captureMode = false
    private var captureGate: EyevueCaptureCycleGate? = null
    private var captureReceipt: kotlinx.coroutines.CompletableDeferred<Unit>? = null
    private var captureDeadlineJob: Job? = null
    private var captureCommandJob: Job? = null
    private var scanCallback: ScanCallback? = null
    private var scanTimeout: Job? = null
    private val devices = linkedMapOf<String, Map<String, Any?>>()

    init {
        methods.setMethodCallHandler(this)
        events.setStreamHandler(this)
        scope.launch {
            gatt.state.collect { state ->
                if (state == EyevueGattState.DISCONNECTED || state == EyevueGattState.ERROR) {
                    if (sessionActive && !disconnecting) failSession("The EyeVue Bluetooth connection was lost")
                    if (!connecting && !sessionActive && !disconnecting) status = "Disconnected"
                }
                emitState()
            }
        }
    }

    override fun onListen(arguments: Any?, eventSink: EventChannel.EventSink) {
        sink = eventSink
        emitState()
    }

    override fun onCancel(arguments: Any?) {
        sink = null
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        if (disposed) {
            result.error("disposed", "The EyeVue component is closed", null)
            return
        }
        try {
            when (call.method) {
                "getState" -> {
                    result.success(state())
                    return
                }
                "scan" -> startScan()
                "stopScan" -> stopScan()
                "connect" -> connect(call.argument<String>("address") ?: "")
                "disconnect" -> disconnect()
                "startSession" -> startSession(call.argument<String>("startup") ?: "media")
                "stopSession" -> stopSession()
                "capture" -> capture()
                else -> {
                    result.notImplemented()
                    return
                }
            }
            result.success(null)
        } catch (failure: Exception) {
            error = failure.message ?: "EyeVue operation failed"
            emitState()
            result.error("eyevue", error, null)
        }
    }

    private fun state(): Map<String, Any?> = linkedMapOf(
        "type" to "state",
        "androidSdkInt" to Build.VERSION.SDK_INT,
        "connected" to gatt.isConnected(),
        "connecting" to connecting,
        "sessionActive" to sessionActive,
        "ready" to ready,
        "status" to status,
        "error" to error,
        "address" to address,
        "devices" to devices.values.toList(),
        "latestImage" to latestImage,
        "sessionId" to sessionId,
    )

    private fun emitState() {
        if (!disposed) sink?.success(state())
    }

    private fun requireBluetoothPermissions(scanning: Boolean) {
        if (Build.VERSION.SDK_INT >= 31) {
            check(hasPermission(Manifest.permission.BLUETOOTH_CONNECT)) { "Grant nearby Bluetooth permission" }
            if (scanning) check(hasPermission(Manifest.permission.BLUETOOTH_SCAN)) { "Grant Bluetooth scan permission" }
        } else if (scanning) {
            check(hasPermission(Manifest.permission.ACCESS_FINE_LOCATION)) { "Grant location permission for Bluetooth scanning" }
        }
    }

    private fun hasPermission(permission: String): Boolean =
        ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED

    @SuppressLint("MissingPermission")
    private fun startScan() {
        requireBluetoothPermissions(scanning = true)
        check(!connecting && !disconnecting && !sessionActive) { "Stop the current operation before scanning" }
        stopScan()
        val adapter = context.getSystemService(BluetoothManager::class.java).adapter
            ?: throw IOException("Bluetooth is unavailable")
        check(adapter.isEnabled) { "Turn on Bluetooth first" }
        val scanner = adapter.bluetoothLeScanner ?: throw IOException("Bluetooth scanning is unavailable")
        devices.clear()
        error = null
        status = "Scanning for nearby glasses"
        val callback = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                mainHandler.post {
                    if (scanCallback !== this || disposed) return@post
                    val name = result.scanRecord?.deviceName ?: try {
                        result.device.name
                    } catch (_: SecurityException) { null }
                    val deviceAddress = result.device.address
                    if (devices.size < 100 || devices.containsKey(deviceAddress)) {
                        devices[deviceAddress] = linkedMapOf(
                            "address" to deviceAddress,
                            "name" to (name ?: "Unnamed Bluetooth device"),
                            "rssi" to result.rssi,
                        )
                        emitState()
                    }
                }
            }

            override fun onScanFailed(errorCode: Int) {
                mainHandler.post {
                    if (scanCallback !== this) return@post
                    stopScan()
                    error = "Bluetooth scan failed ($errorCode)"
                    status = error!!
                    emitState()
                }
            }
        }
        scanCallback = callback
        try {
            scanner.startScan(null, ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(), callback)
            scanTimeout = scope.launch {
                delay(15_000)
                stopScan()
            }
        } catch (failure: Throwable) {
            scanCallback = null
            throw failure
        }
        emitState()
    }

    @SuppressLint("MissingPermission")
    private fun stopScan() {
        val owned = scanCallback
        scanCallback = null
        scanTimeout?.cancel()
        scanTimeout = null
        if (owned != null) {
            try {
                context.getSystemService(BluetoothManager::class.java).adapter?.bluetoothLeScanner?.stopScan(owned)
            } catch (_: SecurityException) {
                // Permission can be revoked during a scan.
            }
            status = if (gatt.isConnected()) "Connected" else "Scan finished"
            emitState()
        }
    }

    private fun connect(target: String) {
        requireBluetoothPermissions(scanning = false)
        val normalized = target.trim().uppercase()
        require(BluetoothAdapter.checkBluetoothAddress(normalized)) { "Select a valid Bluetooth address" }
        check(!connecting && !disconnecting && !sessionActive) { "Stop the current operation before connecting" }
        stopScan()
        gatt.disconnect()
        customer = null
        address = normalized
        error = null
        connecting = true
        status = "Connecting to EyeVue"
        emitState()
        val job = scope.launch(start = CoroutineStart.LAZY) {
            try {
                gatt.connect(normalized).getOrThrow()
                currentCoroutineContext().ensureActive()
                customer = awaitCustomer()
                currentCoroutineContext().ensureActive()
                preferences.edit().putString("address", normalized).apply()
                status = "Connected - ${customer?.project ?: "EyeVue"}"
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (failure: Exception) {
                error = failure.message ?: "EyeVue connection failed"
                status = error!!
                gatt.disconnect()
            } finally {
                connecting = false
                connectionJob = null
                emitState()
            }
        }
        connectionJob = job
        job.start()
    }

    private suspend fun awaitCustomer(): EyevueCustomer = coroutineScope {
        val reply = async(start = CoroutineStart.UNDISPATCHED) {
            withTimeout(10_000L) {
                EyevueProtocol.parseCustomer(
                    gatt.frames.first { EyevueProtocol.parseCustomer(it) != null },
                )!!
            }
        }
        try {
            gatt.write(EyevueProtocol.buildGetCustomerPacket()).getOrThrow()
            currentCoroutineContext().ensureActive()
            reply.await()
        } finally {
            reply.cancel()
        }
    }

    private fun startSession(startup: String) {
        require(startup == "media" || startup == "live" || startup == "capture") { "Startup must be media, live, or capture" }
        check(gatt.isConnected() && !connecting && !disconnecting) { "Connect the glasses first" }
        check(!sessionActive && sessionJob == null) { "A photo session is already active" }
        check(customer?.project?.uppercase()?.startsWith("TK8") == true) {
            "This photo session currently supports verified TK8 EyeVue glasses only"
        }
        stopScan()
        val id = UUID.randomUUID().toString()
        sessionId = id
        captureMode = startup == "capture"
        sessionActive = true
        ready = false
        sessionFailure = null
        error = null
        status = if (startup == "live") "Opening experimental live-mode Wi-Fi" else "Opening EyeVue photo Wi-Fi"
        emitState()
        val job = scope.launch(start = CoroutineStart.LAZY) {
            if (startup == "capture") {
                runCaptureSession(id)
                return@launch
            }
            val ap = EyevueApConnection(context) {
                failSession("The EyeVue Wi-Fi connection was lost")
            }
            var media: EyevuePhotoSession? = null
            var modeRequested = false
            try {
                modeRequested = true
                val ssid = awaitSsid(startup)
                status = "Join the glasses Wi-Fi when Android asks"
                emitState()
                val network = ap.connect(ssid)
                status = "Reading existing photos"
                emitState()
                media = EyevuePhotoSession(context, network)
                media.watch(
                    sessionId = id,
                    onStatus = { message, isReady ->
                        status = message
                        ready = isReady
                        emitState()
                    },
                    onImage = { image ->
                        latestImage = image
                        broadcastImageReady(image)
                        sink?.success(linkedMapOf<String, Any?>("type" to "imageReady").apply { putAll(image) })
                        emitState()
                    },
                )
            } catch (cancelled: CancellationException) {
                // User Stop and owned network loss both pass through the same cleanup.
                throw cancelled
            } catch (failure: Exception) {
                sessionFailure = failure.message ?: "EyeVue photo session failed"
                Log.w("EyevuePhotoSession", "Session failed: ${failure.javaClass.simpleName}")
            } finally {
                withContext(NonCancellable) {
                    ready = false
                    status = "Closing EyeVue photo session"
                    emitState()
                    media?.close()
                    try {
                        if (modeRequested && gatt.isConnected()) {
                            withTimeoutOrNull(5_000L) {
                                gatt.write(EyevueProtocol.buildFinishTransferPacket())
                            }
                        }
                    } finally {
                        ap.close()
                        sessionActive = false
                        sessionJob = null
                        error = sessionFailure
                        status = sessionFailure ?: if (gatt.isConnected()) "Connected - photo session stopped" else "Disconnected"
                        emitState()
                    }
                }
            }
        }
        sessionJob = job
        job.start()
    }


    /** Keep one logical session while closing Wi-Fi around every shutter. */
    private suspend fun runCaptureSession(id: String) {
        val completions = kotlinx.coroutines.channels.Channel<Unit>(kotlinx.coroutines.channels.Channel.CONFLATED)
        val gate = EyevueCaptureCycleGate()
        var ap: EyevueApConnection? = null
        var media: EyevuePhotoSession? = null
        var modeRequested = false
        var closingAp = false
        captureGate = gate
        val observer = scope.launch(start = CoroutineStart.UNDISPATCHED) {
            gatt.frames.collect { frame ->
                if (!captureMode || sessionId != id) return@collect
                var completed = false
                when (frame.commandId) {
                    EyevueProtocol.CMD_GET_MEDIA_COUNT, EyevueProtocol.CMD_RECEIVE_THUMBNAIL_COUNT -> {
                        decodeMediaCount(frame)?.let {
                            completed = gate.onMediaCount(it)
                            if (!completed && gate.isArmed && gate.hasFreshCount) {
                                ready = false
                                status = "New media reported - waiting for photo completion"
                                beginCaptureDeadline(id)
                                emitState()
                            }
                        }
                    }
                    0x45 -> {
                        // TK8 has nine status fields; photo-busy is byte zero.
                        // Its optional tenth isImport field must not be invented.
                        if (frame.payload.size >= 9) {
                            val value = frame.payload[0].toInt() and 0xff
                            if (value == 0 || value == 1) {
                                if (value == 1 && gate.isArmed) {
                                    ready = false
                                    status = "Capturing photo - waiting for the glasses"
                                    beginCaptureDeadline(id)
                                    emitState()
                                }
                                completed = gate.onPhotoBusy(value == 1)
                            }
                        }
                    }
                }
                if (completed) {
                    captureReceipt?.complete(Unit)
                    completions.trySend(Unit)
                }
            }
        }

        suspend fun openMedia(): EyevuePhotoSession {
            closingAp = false
            val owned = EyevueApConnection(context) {
                if (!closingAp) failSession("The EyeVue Wi-Fi connection was lost")
            }
            ap = owned
            modeRequested = true
            status = "Joining EyeVue Wi-Fi for new photos"
            emitState()
            val ssid = awaitSsid("media")
            val network = owned.connect(ssid)
            return EyevuePhotoSession(context, network).also { media = it }
        }

        suspend fun closeMedia(requireReceipt: Boolean) {
            closingAp = true
            val ownedAp = ap
            val ownedMedia = media
            ap = null
            media = null
            val finishNeeded = modeRequested
            modeRequested = false
            withContext(NonCancellable) {
                try {
                    ownedMedia?.close()
                    if (finishNeeded && gatt.isConnected()) {
                        if (requireReceipt) {
                            awaitFinishReceipt()
                        } else {
                            withTimeoutOrNull(5_000L) {
                                gatt.write(EyevueProtocol.buildFinishTransferPacket())
                            }
                        }
                    }
                } finally {
                    ownedAp?.close()
                }
            }
        }

        try {
            val initial = openMedia().snapshot()
            val tracker = EyevueNewPhotoTracker(initial)
            closeMedia(requireReceipt = true)
            while (true) {
                currentCoroutineContext().ensureActive()
                // AP exit can finish a previously busy camera asynchronously.
                status = "Waiting for the glasses camera to become idle"
                emitState()
                awaitPhotoIdle()
                // Query after AP closure so an old count notification cannot arm the cycle.
                gate.arm(awaitMediaCount())
                ready = true
                status = "Ready - press the shutter to capture and fetch."
                emitState()
                completions.receive()
                ready = false
                gate.disarm()
                captureDeadlineJob?.cancelAndJoin()
                captureDeadlineJob = null
                captureReceipt = null
                captureCommandJob?.join()
                captureCommandJob = null
                status = "Photo captured - reconnecting to fetch it"
                emitState()
                val attached = openMedia()
                attached.fetchNewPhotos(
                    sessionId = id,
                    tracker = tracker,
                    onStatus = { message, _ ->
                        ready = false
                        status = message
                        emitState()
                    },
                    onImage = { image ->
                        latestImage = image
                        broadcastImageReady(image)
                        sink?.success(linkedMapOf<String, Any?>("type" to "imageReady").apply { putAll(image) })
                        emitState()
                    },
                )
                closeMedia(requireReceipt = true)
            }
        } catch (_: kotlinx.coroutines.TimeoutCancellationException) {
            sessionFailure = "Capture and fetch timed out: " + status
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (failure: Exception) {
            sessionFailure = failure.message ?: "EyeVue capture and fetch failed"
            Log.w("EyevuePhotoSession", "Capture cycle failed: " + failure.javaClass.simpleName)
        } finally {
            withContext(NonCancellable) {
                ready = false
                status = "Closing EyeVue capture session"
                gate.disarm()
                emitState()
                captureDeadlineJob?.cancelAndJoin()
                captureDeadlineJob = null
                captureReceipt?.cancel()
                captureReceipt = null
                captureCommandJob?.cancelAndJoin()
                captureCommandJob = null
                observer.cancelAndJoin()
                completions.close()
                try {
                    closeMedia(requireReceipt = false)
                } finally {
                    captureGate = null
                    captureMode = false
                    sessionActive = false
                    sessionJob = null
                    error = sessionFailure
                    status = sessionFailure ?: if (gatt.isConnected()) "Connected - capture session stopped" else "Disconnected"
                    emitState()
                }
            }
        }
    }

    private fun decodeMediaCount(frame: EyevueFrame): Int? =
        if (frame.payload.size < 2) null else
            ((frame.payload[0].toInt() and 0xff) shl 8) or (frame.payload[1].toInt() and 0xff)


    private suspend fun awaitPhotoIdle(): Unit = coroutineScope {
        val reply = async(start = CoroutineStart.UNDISPATCHED) {
            withTimeout(10_000L) {
                gatt.frames.first {
                    it.commandId == 0x45 && it.payload.size >= 9 &&
                        it.payload[0].toInt() == 0
                }
            }
        }
        try {
            gatt.write(EyevueProtocol.buildGetDeviceStatusPacket()).getOrThrow()
            reply.await()
            Unit
        } finally {
            reply.cancel()
        }
    }

    private suspend fun awaitMediaCount(): Int = coroutineScope {
        val reply = async(start = CoroutineStart.UNDISPATCHED) {
            withTimeout(10_000L) {
                decodeMediaCount(gatt.frames.first {
                    (it.commandId == EyevueProtocol.CMD_GET_MEDIA_COUNT ||
                        it.commandId == EyevueProtocol.CMD_RECEIVE_THUMBNAIL_COUNT) &&
                        decodeMediaCount(it) != null
                })!!
            }
        }
        try {
            gatt.write(EyevueProtocol.valuePacket(EyevueProtocol.CMD_GET_MEDIA_COUNT, 0)).getOrThrow()
            reply.await()
        } finally {
            reply.cancel()
        }
    }

    private suspend fun awaitFinishReceipt(): Unit = coroutineScope {
        val reply = async(start = CoroutineStart.UNDISPATCHED) {
            withTimeout(5_000L) {
                gatt.frames.first {
                    it.commandId == EyevueProtocol.CMD_FILE_DOWNLOAD_FINISH &&
                        it.payload.contentEquals(byteArrayOf(0x30, 0x01))
                }
            }
        }
        try {
            gatt.write(EyevueProtocol.buildFinishTransferPacket()).getOrThrow()
            reply.await()
            // An observed sequencing response, not proof of AP shutdown.
            Unit
        } finally {
            reply.cancel()
        }
    }

    private fun beginCaptureDeadline(id: String) {
        if (captureReceipt != null) return
        val receipt = kotlinx.coroutines.CompletableDeferred<Unit>()
        captureReceipt = receipt
        lateinit var owned: Job
        owned = scope.launch(start = CoroutineStart.LAZY) {
            try {
                withTimeout(15_000L) { receipt.await() }
            } catch (_: kotlinx.coroutines.TimeoutCancellationException) {
                if (sessionId == id && sessionActive && captureMode) {
                    failSession("The glasses did not confirm a completed photo within 15 seconds")
                }
            } finally {
                if (captureDeadlineJob === owned) captureDeadlineJob = null
            }
        }
        captureDeadlineJob = owned
        owned.start()
    }

    private fun captureForCycle() {
        check(sessionActive && ready && captureGate?.isArmed == true) {
            "Wait until the capture session is ready with glasses Wi-Fi off"
        }
        val id = sessionId ?: throw IOException("The capture session is unavailable")
        ready = false
        status = "Sending shutter command"
        error = null
        beginCaptureDeadline(id)
        emitState()
        val job = scope.launch(start = CoroutineStart.LAZY) {
            try {
                gatt.write(EyevueProtocol.buildPhotoPacket(highQuality = false)).getOrThrow()
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (failure: Exception) {
                failSession(failure.message ?: "The shutter command failed")
            }
        }
        captureCommandJob = job
        job.start()
    }

    private suspend fun awaitSsid(startup: String): String = coroutineScope {
        val reply = async(start = CoroutineStart.UNDISPATCHED) {
            withTimeout(30_000L) {
                EyevueProtocol.parseWifiSsid(
                    gatt.frames.first { !EyevueProtocol.parseWifiSsid(it).isNullOrBlank() },
                )!!
            }
        }
        try {
            // The vendor live path emits SSID asynchronously after 0x67 alone.
            val packet = if (startup == "live") {
                EyevueProtocol.buildStartLiveApPacket()
            } else {
                EyevueProtocol.buildGetWifiInfoPacket(p2p = false)
            }
            gatt.write(packet).getOrThrow()
            currentCoroutineContext().ensureActive()
            reply.await()
        } finally {
            reply.cancel()
        }
    }

    private fun failSession(reason: String) {
        if (!sessionActive) return
        sessionFailure = reason
        error = reason
        ready = false
        status = reason
        emitState()
        sessionJob?.cancel(CancellationException(reason))
    }

    private fun stopSession() {
        if (!sessionActive) return
        ready = false
        status = "Stopping EyeVue photo session"
        emitState()
        sessionJob?.cancel(CancellationException("User stopped the photo session"))
    }

    private fun capture() {
        check(gatt.isConnected() && !connecting && !disconnecting) { "Connect the glasses first" }
        if (captureMode) {
            captureForCycle()
            return
        }
        check(captureJob == null) { "A shutter command is already pending" }
        error = null
        val job = scope.launch(start = CoroutineStart.LAZY) {
            try {
                // Known vendor 0x22/0x30 command. No BLE image-pull probes or camera setting writes.
                gatt.write(EyevueProtocol.buildPhotoPacket(highQuality = false)).getOrThrow()
                currentCoroutineContext().ensureActive()
                status = if (sessionActive) {
                    "Shutter command sent - waiting for a new photo (experimental with Wi-Fi active)"
                } else {
                    "Shutter command sent - start a session before the next capture to receive it"
                }
                emitState()
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (failure: Exception) {
                error = failure.message ?: "The shutter command failed"
                emitState()
            } finally {
                captureJob = null
            }
        }
        captureJob = job
        job.start()
    }

    private fun disconnect() {
        if (disconnecting) return
        disconnecting = true
        stopScan()
        stopSession()
        scope.launch {
            try {
                connectionJob?.cancelAndJoin()
                captureJob?.cancelAndJoin()
                sessionJob?.cancelAndJoin()
                gatt.disconnect()
                customer = null
                status = "Disconnected"
            } finally {
                disconnecting = false
                emitState()
            }
        }
    }

    private fun broadcastImageReady(image: Map<String, Any?>) {
        val uri = Uri.parse(image.getValue("uri") as String)
        try {
            // No broad broadcast: only an explicitly configured local Tasker receiver can see this.
            @Suppress("DEPRECATION")
            context.packageManager.getPackageInfo(TASKER_PACKAGE, 0)
            context.grantUriPermission(TASKER_PACKAGE, uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
            val intent = Intent(IMAGE_READY_ACTION)
                .setPackage(TASKER_PACKAGE)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            intent.clipData = ClipData.newRawUri("EyeVue photo", uri)
            for ((key, value) in image) {
                when (value) {
                    is String -> intent.putExtra(key, value)
                    is Int -> intent.putExtra(key, value)
                    is Long -> intent.putExtra(key, value)
                    is Boolean -> intent.putExtra(key, value)
                }
            }
            intent.putExtra(Intent.EXTRA_STREAM, uri)
            context.sendBroadcast(intent)
        } catch (_: PackageManager.NameNotFoundException) {
            // Tasker is optional; the saved image remains available in Manfred and the gallery.
        } catch (failure: Exception) {
            Log.w("EyevuePhotoSession", "Optional Tasker ready event unavailable: ${failure.javaClass.simpleName}")
        }
    }

    fun dispose() {
        if (disposed) return
        stopScan()
        disposed = true
        methods.setMethodCallHandler(null)
        events.setStreamHandler(null)
        sink = null
        scope.launch {
            try {
                connectionJob?.cancelAndJoin()
                captureJob?.cancelAndJoin()
                sessionJob?.cancelAndJoin()
            } finally {
                gatt.disconnect()
                scope.cancel()
            }
        }
    }

    companion object {
        const val IMAGE_READY_ACTION = "com.thetopham.manfred_companion.EYEVUE_IMAGE_READY"
        private const val TASKER_PACKAGE = "net.dinglisch.android.taskerm"
    }
}
