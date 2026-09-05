// Ported from thetopham/Alternative-HeyCyan-App-and-SDK; see SOURCE_PROVENANCE.md.
package com.thetopham.manfred_companion.eyevue

import java.net.Proxy
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import javax.net.SocketFactory
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import okhttp3.Call
import okhttp3.Dns
import okhttp3.OkHttpClient

/** A fresh pool and explicit routing keep each transfer on its connected glasses network. */
internal fun createEyevueMediaHttpClient(
    socketFactory: SocketFactory,
    dns: Dns,
): OkHttpClient = OkHttpClient.Builder()
    .socketFactory(socketFactory)
    .dns(dns)
    .proxy(Proxy.NO_PROXY)
    .connectTimeout(15, TimeUnit.SECONDS)
    .readTimeout(120, TimeUnit.SECONDS)
    .writeTimeout(30, TimeUnit.SECONDS)
    .callTimeout(120, TimeUnit.SECONDS)
    .build()

/** Keep cancellation attached to the call until its entire response body has been consumed. */
internal suspend fun <T> withEyevueHttpCall(
    call: Call,
    block: suspend () -> T,
): T = coroutineScope {
    val completed = AtomicBoolean(false)
    val cancellationWatcher = launch(Dispatchers.IO, start = CoroutineStart.UNDISPATCHED) {
        try {
            awaitCancellation()
        } finally {
            if (!completed.get()) call.cancel()
        }
    }
    try {
        block()
    } catch (error: Throwable) {
        coroutineContext.ensureActive()
        throw error
    } finally {
        completed.set(true)
        cancellationWatcher.cancel()
    }
}
