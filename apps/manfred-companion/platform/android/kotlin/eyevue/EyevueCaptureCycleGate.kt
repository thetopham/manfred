package com.thetopham.manfred_companion.eyevue

/**
 * Opens one fetch cycle after a post-arm photo busy/idle cycle and a fresh media count.
 * Call from a single serialized event stream. The session owns any timeout and AP lifecycle.
 */
class EyevueCaptureCycleGate {
    var latestMediaCount: Int? = null
        private set

    var isArmed: Boolean = false
        private set

    /** Fresh count evidence starts a deadline but cannot authorize a fetch without busy/idle. */
    val hasFreshCount: Boolean
        get() = isArmed && latestMediaCount?.let {
            ((it - baselineCount) and MAX_COUNT) in 1..MAX_FORWARD_ADVANCE
        } == true

    private var baselineCount = 0
    private var photoBusy = false
    private var photoCompleted = false

    /** Arm only after AP cleanup, using the most recently confirmed media count. */
    fun arm(baselineCount: Int) {
        require(baselineCount in 0..MAX_COUNT) { "Media count must be an unsigned 16-bit value" }
        this.baselineCount = baselineCount
        latestMediaCount = baselineCount
        photoBusy = false
        photoCompleted = false
        isArmed = true
    }

    /** Keep count observations during a fetch, but discard any capture cycle in progress. */
    fun disarm() {
        isArmed = false
        photoBusy = false
        photoCompleted = false
    }

    /** Returns true only for the event that completes an armed capture cycle. */
    fun onPhotoBusy(busy: Boolean): Boolean {
        if (!isArmed) return false
        if (busy) {
            photoBusy = true
            photoCompleted = false
        } else if (photoBusy) {
            photoBusy = false
            photoCompleted = true
        }
        return takeReadyCycle()
    }

    /** Invalid decoded counts are ignored. Disarmed valid counts still update the baseline hint. */
    fun onMediaCount(count: Int): Boolean {
        if (count !in 0..MAX_COUNT) return false
        if (isArmed) {
            // Half-range ordering permits 65535 -> 0, while rejecting older/lower count replays.
            // A jump of half the range or more is ambiguous and needs a new confirmed baseline.
            val advance = (count - baselineCount) and MAX_COUNT
            val observedAdvance = ((latestMediaCount ?: baselineCount) - baselineCount) and MAX_COUNT
            if (advance > MAX_FORWARD_ADVANCE || advance < observedAdvance) return false
        }
        latestMediaCount = count
        return takeReadyCycle()
    }

    private fun takeReadyCycle(): Boolean {
        if (!hasFreshCount || photoBusy || !photoCompleted) return false
        disarm()
        return true
    }

    private companion object {
        const val MAX_COUNT = 0xffff
        const val MAX_FORWARD_ADVANCE = 0x7fff
    }
}
