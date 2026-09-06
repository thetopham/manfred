package com.thetopham.manfred_companion.eyevue

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class EyevueApDiscoveryPolicyTest {
    private val startedUs = 20_000_000L
    private val nowUs = 25_000_000L
    private val ssid = "test-glasses"

    @Test
    fun strongerCachedResultFromPreviousApSessionCannotWin() {
        val stale = observation(bssid = "10:22:33:44:55:66", timestampUs = startedUs - 1, signalDbm = -10)
        val fresh = observation(bssid = "20:22:33:44:55:66", signalDbm = -65)
        assertEquals(EyevueApDiscoveryPolicy.Target(fresh.bssid!!, 2417), select(stale, fresh))
    }

    @Test
    fun observationMustBeStrictlyAfterConnectionStartAndNotInTheFuture() {
        assertNull(select(observation(timestampUs = startedUs)))
        assertNull(select(observation(timestampUs = 0)))
        assertNull(select(observation(timestampUs = nowUs + 1)))
        assertEquals(2417, select(observation(timestampUs = startedUs + 1))?.frequencyMhz)
    }

    @Test
    fun ageLimitStillAppliesDuringLongerDiscovery() {
        val observed = observation(timestampUs = startedUs + 1)
        assertEquals(2417, EyevueApDiscoveryPolicy.select(
            ssid, listOf(observed), startedUs, observed.timestampUs + 10_000_000,
        )?.frequencyMhz)
        assertNull(EyevueApDiscoveryPolicy.select(
            ssid, listOf(observed), startedUs, observed.timestampUs + 10_000_001,
        ))
    }

    @Test
    fun exactSsidAndPskSecurityAreRequired() {
        assertNull(select(observation(ssid = "Test-glasses")))
        assertNull(select(observation(ssid = "\"test-glasses\"")))
        assertNull(select(observation(capabilities = "[ESS]")))
        assertNull(select(observation(capabilities = "[WPA2-EAP-CCMP][ESS]")))
        assertNull(select(observation(capabilities = "[WPA-PSK-TKIP][ESS]")))
        assertEquals(2417, select(observation(capabilities = "[RSN-PSK-CCMP][ESS]"))?.frequencyMhz)
        assertEquals(2417, select(observation(capabilities = "[WPA2-PSK+SAE-CCMP][ESS]"))?.frequencyMhz)
    }

    @Test
    fun malformedMulticastAndRedactedIdentitiesCannotBePinned() {
        for (invalid in listOf(null, "", "zz:22:33:44:55:66", "1:22:33:44:55:66",
            "10-22-33-44-55-66", "01:22:33:44:55:66", "ff:ff:ff:ff:ff:ff",
            "00:00:00:00:00:00", "02:00:00:00:00:00",
        )) {
            assertNull("Must reject $invalid", select(observation(bssid = invalid)))
        }
        // Locally administered unicast identities are valid; privacy randomization is not multicast.
        assertEquals("02:22:33:aa:bb:cc", select(observation(bssid = "02:22:33:AA:BB:CC"))?.bssid)
    }

    @Test
    fun selectedChannelComesFromFreshStrongestObservation() {
        val weaker = observation(bssid = "10:22:33:44:55:66", frequencyMhz = 2457, signalDbm = -65)
        val stronger = observation(bssid = "20:22:33:44:55:66", frequencyMhz = 2417, signalDbm = -40)
        assertEquals(EyevueApDiscoveryPolicy.Target(stronger.bssid!!, 2417), select(weaker, stronger))
        assertNull(select(observation(frequencyMhz = 0)))
    }

    @Test
    fun preflightConsumesExistingBudgetInsteadOfExtendingIt() {
        val start = 1_000L
        assertEquals(60_000, EyevueApDiscoveryPolicy.networkRequestTimeoutMs(start, start))
        val afterPreflight = start + EyevueApDiscoveryPolicy.PREFLIGHT_TIMEOUT_MS
        assertEquals(49_000L, EyevueApDiscoveryPolicy.remainingConnectionMs(start, afterPreflight))
        assertEquals(49_000, EyevueApDiscoveryPolicy.networkRequestTimeoutMs(start, afterPreflight))
        assertEquals(1, EyevueApDiscoveryPolicy.networkRequestTimeoutMs(start, start + 64_999))
        assertEquals(0, EyevueApDiscoveryPolicy.networkRequestTimeoutMs(start, start + 65_000))
        assertEquals(0L, EyevueApDiscoveryPolicy.remainingConnectionMs(start, start + 90_000))
    }

    private fun select(vararg observations: EyevueApDiscoveryPolicy.Observation) =
        EyevueApDiscoveryPolicy.select(ssid, observations.toList(), startedUs, nowUs)

    private fun observation(
        ssid: String = this.ssid,
        bssid: String? = "10:22:33:44:55:66",
        frequencyMhz: Int = 2417,
        signalDbm: Int = -40,
        timestampUs: Long = nowUs - 1,
        capabilities: String = "[WPA2-PSK-CCMP][ESS]",
    ) = EyevueApDiscoveryPolicy.Observation(ssid, bssid, frequencyMhz, signalDbm, timestampUs, capabilities)
}
