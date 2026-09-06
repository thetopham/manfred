package com.thetopham.manfred_companion.eyevue

/** Pure selection/deadline policy for the optional, experimentally faster AP discovery path. */
internal object EyevueApDiscoveryPolicy {
    const val CONNECTION_TIMEOUT_MS = 65_000L
    const val PREFLIGHT_TIMEOUT_MS = 16_000L
    const val MAX_COMPLETED_SCANS = 2
    private const val MAX_RESULT_AGE_US = 10_000_000L
    private val macPattern = Regex("^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
    private val wpa2PskPattern = Regex("\\[(?:WPA2|RSN)-PSK(?:[-+\\]])")

    data class Observation(
        val ssid: String,
        val bssid: String?,
        val frequencyMhz: Int,
        val signalDbm: Int,
        val timestampUs: Long,
        val capabilities: String,
    )

    data class Target(val bssid: String, val frequencyMhz: Int)

    fun select(
        ssid: String,
        observations: List<Observation>,
        connectStartedUs: Long,
        nowUs: Long,
    ): Target? = observations.asSequence()
        .filter {
            it.ssid == ssid &&
                it.timestampUs > connectStartedUs &&
                it.timestampUs <= nowUs &&
                nowUs - it.timestampUs <= MAX_RESULT_AGE_US &&
                it.frequencyMhz > 0 &&
                isUnicastBssid(it.bssid) &&
                wpa2PskPattern.containsMatchIn(it.capabilities)
        }
        .maxWithOrNull(compareBy<Observation> { it.signalDbm }.thenBy { it.timestampUs })
        ?.let { Target(requireNotNull(it.bssid).lowercase(), it.frequencyMhz) }

    fun remainingConnectionMs(startedMs: Long, nowMs: Long): Long =
        (CONNECTION_TIMEOUT_MS - (nowMs - startedMs).coerceAtLeast(0L)).coerceAtLeast(0L)

    fun networkRequestTimeoutMs(startedMs: Long, nowMs: Long): Int =
        remainingConnectionMs(startedMs, nowMs).coerceAtMost(60_000L).toInt()

    private fun isUnicastBssid(value: String?): Boolean {
        if (value == null || !macPattern.matches(value)) return false
        val octets = value.split(':').map { it.toInt(16) }
        return octets[0] and 1 == 0 && octets.any { it != 0 } &&
            // Android's redacted placeholder is not an observed access-point identity.
            !value.equals("02:00:00:00:00:00", ignoreCase = true)
    }
}
