package cc.waxid.android.matching

import org.junit.Assert.*
import org.junit.Test

class MatchClientTest {

    @Test
    fun `stabilize returns null with fewer than 3 results`() {
        val results = listOf(
            makeCandidate(1, score = 10, confidence = 2.0),
            makeCandidate(1, score = 10, confidence = 2.0)
        )
        assertNull(StabilityChecker.check(results))
    }

    @Test
    fun `stabilize confirms when same track in 2 of last 3`() {
        val results = listOf(
            null,
            makeCandidate(42, score = 10, confidence = 2.0),
            null,
            makeCandidate(42, score = 10, confidence = 2.0),
            makeCandidate(42, score = 10, confidence = 2.0)
        )
        val match = StabilityChecker.check(results)
        assertNotNull(match)
        assertEquals(42, match!!.trackId)
    }

    @Test
    fun `stabilize returns null when no track has 2 of 3`() {
        val results = listOf(
            makeCandidate(1, score = 10, confidence = 2.0),
            makeCandidate(2, score = 10, confidence = 2.0),
            makeCandidate(3, score = 10, confidence = 2.0)
        )
        assertNull(StabilityChecker.check(results))
    }

    @Test
    fun `stabilize uses only last 3 results`() {
        val results = listOf(
            makeCandidate(1, score = 10, confidence = 2.0),
            makeCandidate(1, score = 10, confidence = 2.0),
            makeCandidate(2, score = 10, confidence = 2.0),
            makeCandidate(3, score = 10, confidence = 2.0),
            makeCandidate(2, score = 10, confidence = 2.0)
        )
        val match = StabilityChecker.check(results)
        assertNotNull(match)
        assertEquals(2, match!!.trackId)
    }

    @Test
    fun `low score results are filtered out`() {
        val candidate = makeCandidate(42, score = 3, confidence = 2.0)
        assertFalse(StabilityChecker.isValidResult(candidate))
    }

    @Test
    fun `low confidence results are filtered out`() {
        val candidate = makeCandidate(42, score = 10, confidence = 1.0)
        assertFalse(StabilityChecker.isValidResult(candidate))
    }

    @Test
    fun `null confidence is accepted`() {
        val candidate = makeCandidate(42, score = 10, confidence = null)
        assertTrue(StabilityChecker.isValidResult(candidate))
    }

    private fun makeCandidate(
        trackId: Int,
        score: Int = 10,
        confidence: Double? = 2.0
    ) = MatchCandidate(
        trackId = trackId,
        artist = "Artist",
        album = "Album",
        track = "Track",
        score = score,
        confidence = confidence,
        offsetS = 0.0
    )
}
