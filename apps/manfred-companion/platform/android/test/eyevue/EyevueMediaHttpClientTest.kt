// Ported regression coverage; source hashes in kotlin/eyevue/SOURCE_PROVENANCE.md.
package com.thetopham.manfred_companion.eyevue

import java.io.Closeable
import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import javax.net.SocketFactory
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.Dns
import okhttp3.OkHttpClient
import okhttp3.Request
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

class EyevueMediaHttpClientTest {
    @Test(timeout = 15_000)
    fun routesHttpThroughProvidedNetworkWithoutSharingSessionConnections() {
        LoopbackHttpServer().use { server ->
            val lookups = CopyOnWriteArrayList<String>()
            val dns = object : Dns {
                override fun lookup(hostname: String): List<InetAddress> {
                    assertEquals("eyeglasses.invalid", hostname)
                    lookups.add(hostname)
                    return listOf(server.address)
                }
            }
            val socketFactory = RecordingSocketFactory()
            // Identical routing dependencies make a shared pool eligible to reuse
            // the first session's socket, so this exercises actual pool isolation.
            val first = createEyevueMediaHttpClient(socketFactory, dns)
            val second = createEyevueMediaHttpClient(socketFactory, dns)
            try {
                for (client in listOf(first, second)) {
                    assertSame(socketFactory, client.socketFactory)
                    assertSame(dns, client.dns)
                    assertSame(Proxy.NO_PROXY, client.proxy)
                    assertEquals(15_000, client.connectTimeoutMillis)
                    assertEquals(120_000, client.readTimeoutMillis)
                    assertEquals(30_000, client.writeTimeoutMillis)
                }
                assertNotSame(first, second)
                assertNotSame(first.connectionPool, second.connectionPool)

                assertEquals("connection-1", getMediaList(first, server.port))
                assertEquals(listOf("eyeglasses.invalid"), lookups.toList())
                assertEquals(1, socketFactory.sockets.size)
                assertEquals(
                    InetSocketAddress(server.address, server.port),
                    socketFactory.sockets.single().remoteSocketAddress,
                )

                // The server keeps connections alive: a second request on the same
                // client must reuse the established socket rather than reconnect.
                assertEquals("connection-1", getMediaList(first, server.port))
                assertEquals(1, socketFactory.sockets.size)

                assertEquals("connection-2", getMediaList(second, server.port))
                assertEquals(2, socketFactory.sockets.size)
                assertEquals(listOf("eyeglasses.invalid", "eyeglasses.invalid"), lookups.toList())
                assertEquals(
                    List(3) { "GET /app/getfilelist HTTP/1.1" },
                    server.requests.toList(),
                )
                assertTrue("Server failures: ${server.failures}", server.failures.isEmpty())
            } finally {
                for (client in listOf(first, second)) {
                    client.connectionPool.evictAll()
                    client.dispatcher.executorService.shutdownNow()
                }
            }
        }
    }

    @Test(timeout = 10_000)
    fun timeoutCancelsStalledBodyAfterSynchronousExecuteReturns() = runBlocking {
        LoopbackHttpServer(stallBody = true).use { server ->
            val client = createEyevueMediaHttpClient(
                RecordingSocketFactory(),
                object : Dns {
                    override fun lookup(hostname: String): List<InetAddress> = listOf(server.address)
                },
            )
            val call = client.newCall(
                Request.Builder().url("http://eyeglasses.invalid:" + server.port + "/app/getfilelist").build(),
            )
            // A broken cancellation helper must fail in seconds, not hang for the
            // production 120-second read timeout.
            call.timeout().timeout(5, TimeUnit.SECONDS)
            try {
                withContext(Dispatchers.IO) {
                    call.execute().use { response ->
                        assertEquals(200, response.code)
                        // OkHttp already removed this synchronous call from the
                        // dispatcher, although its response body is still active.
                        assertEquals(0, client.dispatcher.runningCallsCount())
                        val startedAt = System.nanoTime()
                        val result = withTimeoutOrNull(250L) {
                            withEyevueHttpCall(call) {
                                checkNotNull(response.body).string()
                            }
                        }
                        val elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startedAt)
                        assertNull(result)
                        assertTrue(call.isCanceled())
                        assertTrue("Body cancellation took " + elapsedMs + "ms", elapsedMs < 3_000L)
                    }
                }
            } finally {
                call.cancel()
                client.connectionPool.evictAll()
                client.dispatcher.executorService.shutdownNow()
            }
        }
    }

    private fun getMediaList(client: OkHttpClient, port: Int): String {
        val call = client.newCall(
            Request.Builder()
                .url("http://eyeglasses.invalid:$port/app/getfilelist")
                .build(),
        )
        // Bound a regression failure without weakening the production read timeout.
        call.timeout().timeout(5, TimeUnit.SECONDS)
        return call.execute().use { response ->
            assertEquals(200, response.code)
            checkNotNull(response.body).string()
        }
    }

    private class RecordingSocketFactory : SocketFactory() {
        private val delegate = SocketFactory.getDefault()
        val sockets = CopyOnWriteArrayList<Socket>()

        private fun record(socket: Socket): Socket = socket.also { sockets.add(it) }

        override fun createSocket(): Socket = record(delegate.createSocket())
        override fun createSocket(host: String, port: Int): Socket =
            record(delegate.createSocket(host, port))
        override fun createSocket(host: InetAddress, port: Int): Socket =
            record(delegate.createSocket(host, port))
        override fun createSocket(host: String, port: Int, local: InetAddress, localPort: Int): Socket =
            record(delegate.createSocket(host, port, local, localPort))
        override fun createSocket(host: InetAddress, port: Int, local: InetAddress, localPort: Int): Socket =
            record(delegate.createSocket(host, port, local, localPort))
    }

    private class LoopbackHttpServer(private val stallBody: Boolean = false) : Closeable {
        val address: InetAddress = InetAddress.getByAddress(byteArrayOf(127, 0, 0, 1))
        private val server = ServerSocket(0, 10, address).apply { soTimeout = 1_000 }
        val port: Int = server.localPort
        val requests = CopyOnWriteArrayList<String>()
        val failures = CopyOnWriteArrayList<IOException>()
        private val sockets = CopyOnWriteArrayList<Socket>()
        private val connectionCount = AtomicInteger()
        private val workers = Executors.newCachedThreadPool { runnable ->
            Thread(runnable, "eyevue-http-test").apply { isDaemon = true }
        }

        init {
            workers.execute {
                while (!server.isClosed) {
                    try {
                        val socket = server.accept().apply { soTimeout = 5_000 }
                        sockets.add(socket)
                        val connection = connectionCount.incrementAndGet()
                        workers.execute { serve(socket, connection) }
                    } catch (_: SocketTimeoutException) {
                        // Periodically recheck shutdown while waiting for a client.
                    } catch (failure: IOException) {
                        if (!server.isClosed) failures.add(failure)
                        break
                    }
                }
            }
        }

        private fun serve(socket: Socket, connection: Int) {
            try {
                socket.use {
                    val reader = socket.getInputStream().bufferedReader(Charsets.US_ASCII)
                    val output = socket.getOutputStream()
                    while (!server.isClosed) {
                        val requestLine = reader.readLine() ?: break
                        while (true) {
                            val header = reader.readLine() ?: return
                            if (header.isEmpty()) break
                        }
                        requests.add(requestLine)
                        val body = "connection-$connection".toByteArray(Charsets.US_ASCII)
                        output.write(
                            ("HTTP/1.1 200 OK\r\nContent-Length: ${body.size}\r\n" +
                                "Connection: keep-alive\r\n\r\n").toByteArray(Charsets.US_ASCII),
                        )
                        if (!stallBody) output.write(body)
                        output.flush()
                    }
                }
            } catch (failure: IOException) {
                if (!server.isClosed) failures.add(failure)
            }
        }

        override fun close() {
            server.close()
            sockets.forEach { runCatching { it.close() } }
            workers.shutdownNow()
            workers.awaitTermination(5, TimeUnit.SECONDS)
        }
    }
}
