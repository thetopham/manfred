package com.thetopham.manfred_companion.eyevue

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiNetworkSpecifier
import android.os.Build
import android.os.Handler
import android.os.Looper
import androidx.core.content.ContextCompat
import java.io.IOException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.withTimeout

/** TK8 access-point ownership; never changes the application's default network. */
internal class EyevueApConnection(
    private val context: Context,
    private val onLost: () -> Unit,
) {
    private val connectivity = context.getSystemService(ConnectivityManager::class.java)
    private var callback: ConnectivityManager.NetworkCallback? = null
    private var network: Network? = null

    suspend fun connect(ssid: String): Network {
        check(callback == null) { "An EyeVue Wi-Fi connection is already owned" }
        val permission = if (Build.VERSION.SDK_INT >= 33) {
            Manifest.permission.NEARBY_WIFI_DEVICES
        } else {
            Manifest.permission.ACCESS_FINE_LOCATION
        }
        check(ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED) {
            "Grant nearby Wi-Fi permission (location on Android 12 or earlier)"
        }
        require(ssid.isNotBlank()) { "The glasses returned an empty Wi-Fi name" }
        val available = CompletableDeferred<Network>()
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .setNetworkSpecifier(
                WifiNetworkSpecifier.Builder()
                    .setSsid(ssid)
                    .setWpa2Passphrase("12345678")
                    .build(),
            )
            .build()
        val owned = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(connected: Network) {
                if (callback !== this) return
                network = connected
                available.complete(connected)
            }

            override fun onUnavailable() {
                if (callback !== this) return
                available.completeExceptionally(IOException("EyeVue Wi-Fi join was declined or unavailable"))
            }

            override fun onLost(lost: Network) {
                if (callback !== this || network != lost) return
                network = null
                if (!available.isCompleted) {
                    available.completeExceptionally(IOException("EyeVue Wi-Fi disappeared while connecting"))
                }
                onLost()
            }
        }
        callback = owned
        try {
            connectivity.requestNetwork(request, owned, Handler(Looper.getMainLooper()), 60_000)
            return withTimeout(65_000L) { available.await() }
        } catch (error: Throwable) {
            close()
            throw error
        }
    }

    fun close() {
        val owned = callback
        callback = null
        network = null
        if (owned != null) {
            try {
                connectivity.unregisterNetworkCallback(owned)
            } catch (_: IllegalArgumentException) {
                // Android can already have removed an unavailable request.
            }
        }
    }
}
