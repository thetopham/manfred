package com.thetopham.manfred_companion.eyevue

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.location.LocationManager
import android.net.ConnectivityManager
import android.net.MacAddress
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiManager
import android.net.wifi.WifiNetworkSpecifier
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import androidx.core.content.ContextCompat
import java.io.IOException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull

/** TK8 access-point ownership; never changes the application's default network. */
internal class EyevueApConnection(
    private val context: Context,
    private val onLost: () -> Unit,
) {
    private val connectivity = context.getSystemService(ConnectivityManager::class.java)
    private val wifi = context.applicationContext.getSystemService(WifiManager::class.java)
    private var callback: ConnectivityManager.NetworkCallback? = null
    private var network: Network? = null
    private var attempt: CompletableDeferred<Network>? = null
    private var scanReceiver: BroadcastReceiver? = null
    private var scanSignals: Channel<Boolean>? = null
    private var startedMs = 0L

    suspend fun connect(ssid: String): Network {
        check(attempt == null && callback == null) { "An EyeVue Wi-Fi connection is already owned" }
        val permission = if (Build.VERSION.SDK_INT >= 33) {
            Manifest.permission.NEARBY_WIFI_DEVICES
        } else {
            Manifest.permission.ACCESS_FINE_LOCATION
        }
        check(hasPermission(permission)) {
            "Grant nearby Wi-Fi permission (location on Android 12 or earlier)"
        }
        require(ssid.isNotBlank()) { "The glasses returned an empty Wi-Fi name" }
        startedMs = SystemClock.elapsedRealtime()
        val connectStartedUs = SystemClock.elapsedRealtimeNanos() / 1_000L
        val available = CompletableDeferred<Network>()
        attempt = available
        try {
            return withTimeout(EyevueApDiscoveryPolicy.remainingConnectionMs(startedMs, SystemClock.elapsedRealtime())) {
                val target = discoverFreshTarget(ssid, connectStartedUs, available)
                // close() can invalidate an attempt while discovery is suspended.
                check(attempt === available) { "EyeVue Wi-Fi connection was closed" }
                val specifier = WifiNetworkSpecifier.Builder()
                    .setSsid(ssid)
                    .setWpa2Passphrase("12345678")
                if (target != null) {
                    specifier.setBssid(MacAddress.fromString(target.bssid))
                    if (Build.VERSION.SDK_INT >= 34) {
                        try {
                            specifier.setPreferredChannelsFrequenciesMhz(intArrayOf(target.frequencyMhz))
                        } catch (_: IllegalArgumentException) {
                            // Keep the freshly observed BSSID if an OEM rejects a frequency hint.
                            phase("frequency_hint_unavailable")
                        }
                    }
                }
                val request = NetworkRequest.Builder()
                    .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                    .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                    .setNetworkSpecifier(specifier.build())
                    .build()
                val owned = object : ConnectivityManager.NetworkCallback() {
                    override fun onAvailable(connected: Network) {
                        if (callback !== this || attempt !== available) return
                        network = connected
                        phase("available")
                        available.complete(connected)
                    }

                    override fun onUnavailable() {
                        if (callback !== this || attempt !== available) return
                        phase("unavailable")
                        available.completeExceptionally(IOException("EyeVue Wi-Fi join was declined or unavailable"))
                    }

                    override fun onLost(lost: Network) {
                        if (callback !== this || attempt !== available || network != lost) return
                        network = null
                        phase("lost")
                        if (!available.isCompleted) {
                            available.completeExceptionally(IOException("EyeVue Wi-Fi disappeared while connecting"))
                        }
                        onLost()
                    }
                }
                val timeoutMs = EyevueApDiscoveryPolicy.networkRequestTimeoutMs(startedMs, SystemClock.elapsedRealtime())
                if (timeoutMs <= 0) throw IOException("EyeVue Wi-Fi connection deadline expired")
                callback = owned
                connectivity.requestNetwork(request, owned, Handler(Looper.getMainLooper()), timeoutMs)
                phase("request_submitted", "verifiedTarget=${target != null} timeoutMs=$timeoutMs")
                available.await()
            }
        } catch (error: Throwable) {
            if (attempt === available) close()
            throw error
        }
    }

    /**
     * Scan broadcasts are device-wide, so selection additionally requires an observation timestamp
     * after this connection began. Never trust a previously cached SSID/BSSID from another AP cycle.
     */
    private suspend fun discoverFreshTarget(
        ssid: String,
        connectStartedUs: Long,
        owner: CompletableDeferred<Network>,
    ): EyevueApDiscoveryPolicy.Target? {
        if (!hasPermission(Manifest.permission.ACCESS_FINE_LOCATION)) {
            phase("fallback", "reason=fine_permission_absent")
            return null
        }
        val manager = wifi
        val locationEnabled = try {
            context.getSystemService(LocationManager::class.java)?.isLocationEnabled == true
        } catch (_: SecurityException) {
            false
        }
        if (manager == null || !locationEnabled) {
            phase("fallback", "reason=scanning_unavailable")
            return null
        }
        if (Build.VERSION.SDK_INT >= 31) {
            val concurrent = runCatching { manager.isStaConcurrencyForLocalOnlyConnectionsSupported }.getOrNull()
            phase("capabilities", "localOnlyStaConcurrency=$concurrent")
        }
        val signals = Channel<Boolean>(Channel.CONFLATED)
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context, intent: Intent) {
                if (scanReceiver !== this || attempt !== owner ||
                    intent.action != WifiManager.SCAN_RESULTS_AVAILABLE_ACTION
                ) return
                signals.trySend(intent.getBooleanExtra(WifiManager.EXTRA_RESULTS_UPDATED, false))
            }
        }
        var fallbackReason = "preflight_deadline"
        try {
            // Install before startScan so a fast completion cannot race the await.
            ContextCompat.registerReceiver(
                context, receiver, IntentFilter(WifiManager.SCAN_RESULTS_AVAILABLE_ACTION),
                ContextCompat.RECEIVER_NOT_EXPORTED,
            )
            scanReceiver = receiver
            scanSignals = signals
            phase("preflight_start")
            val target = withTimeoutOrNull(
                minOf(
                    EyevueApDiscoveryPolicy.PREFLIGHT_TIMEOUT_MS,
                    EyevueApDiscoveryPolicy.remainingConnectionMs(startedMs, SystemClock.elapsedRealtime()),
                ),
            ) {
                repeat(EyevueApDiscoveryPolicy.MAX_COMPLETED_SCANS) { index ->
                    // Drop queued completions from an earlier scan before issuing our next request.
                    signals.tryReceive()
                    if (!manager.startScan()) {
                        fallbackReason = "scan_rejected"
                        return@withTimeoutOrNull null
                    }
                    phase("scan_accepted", "scan=${index + 1}")
                    if (!signals.receive()) {
                        fallbackReason = "scan_failed"
                        return@withTimeoutOrNull null
                    }
                    phase("scan_completed", "scan=${index + 1}")
                    val nowUs = SystemClock.elapsedRealtimeNanos() / 1_000L
                    val observations = manager.scanResults.map {
                        EyevueApDiscoveryPolicy.Observation(
                            it.SSID, it.BSSID, it.frequency, it.level, it.timestamp, it.capabilities,
                        )
                    }
                    val selected = EyevueApDiscoveryPolicy.select(ssid, observations, connectStartedUs, nowUs)
                    if (selected != null) {
                        phase("matched", "scan=${index + 1} frequencyMhz=${selected.frequencyMhz}")
                        return@withTimeoutOrNull selected
                    }
                }
                fallbackReason = "no_fresh_match"
                null
            }
            if (target == null) phase("fallback", "reason=$fallbackReason")
            return target
        } catch (error: CancellationException) {
            throw error
        } catch (_: SecurityException) {
            phase("fallback", "reason=scan_permission_denied")
            return null
        } catch (_: RuntimeException) {
            phase("fallback", "reason=scan_unavailable")
            return null
        } finally {
            unregisterScanReceiver(receiver)
            if (scanSignals === signals) scanSignals = null
            signals.cancel()
        }
    }

    fun close() {
        val owned = callback
        val pending = attempt
        callback = null
        attempt = null
        network = null
        scanReceiver?.let { unregisterScanReceiver(it) }
        scanSignals?.cancel()
        scanSignals = null
        pending?.cancel()
        if (owned != null) {
            try {
                connectivity.unregisterNetworkCallback(owned)
            } catch (_: IllegalArgumentException) {
                // Android can already have removed an unavailable request.
            }
        }
        if (pending != null || owned != null) phase("closed")
    }

    private fun unregisterScanReceiver(owned: BroadcastReceiver) {
        if (scanReceiver !== owned) return
        scanReceiver = null
        try {
            context.unregisterReceiver(owned)
        } catch (_: IllegalArgumentException) {
            // close() and cancellation may both have reached cleanup.
        }
    }

    private fun hasPermission(permission: String): Boolean =
        ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED

    private fun phase(name: String, detail: String = "") {
        Log.i("EyevueWifiDiscovery", "phase=$name elapsedMs=${SystemClock.elapsedRealtime() - startedMs} $detail")
    }
}
