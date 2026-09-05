// Ported from thetopham/Alternative-HeyCyan-App-and-SDK; see SOURCE_PROVENANCE.md.
package com.thetopham.manfred_companion.eyevue

/** Manifest metadata is an opaque identity hint, not a verified byte length. */
data class EyevuePhotoFingerprint(val reportedSize: Long, val timecode: Long)

/** Session-local policy: leave the baseline alone and wait for stable new photos. */
class EyevueNewPhotoTracker(baseline: Map<String, EyevuePhotoFingerprint>) {
    private val initial = baseline.toMap()
    private val imported = mutableSetOf<Pair<String, EyevuePhotoFingerprint>>()
    private var previous: Map<String, EyevuePhotoFingerprint> = emptyMap()

    /** Candidates remain eligible until the caller confirms a successful import. */
    fun observe(snapshot: Map<String, EyevuePhotoFingerprint>): Map<String, EyevuePhotoFingerprint> {
        val candidates = linkedMapOf<String, EyevuePhotoFingerprint>()
        for ((path, fingerprint) in snapshot) {
            if (initial[path] == fingerprint || (path to fingerprint) in imported) continue
            if (previous[path] == fingerprint) candidates[path] = fingerprint
        }
        // Replacing the whole snapshot resets stability for entries that disappeared.
        previous = snapshot.toMap()
        return candidates
    }

    fun markImported(path: String, fingerprint: EyevuePhotoFingerprint) {
        imported += path to fingerprint
    }
}
