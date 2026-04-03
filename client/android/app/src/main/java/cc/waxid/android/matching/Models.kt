package cc.waxid.android.matching

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class MatchCandidate(
    @SerialName("track_id") val trackId: Int,
    val artist: String,
    val album: String,
    @SerialName("album_id") val albumId: Int? = null,
    val track: String,
    @SerialName("track_number") val trackNumber: Int? = null,
    val year: Int? = null,
    val side: String? = null,
    val position: String? = null,
    val score: Int,
    val confidence: Double? = null,
    @SerialName("offset_s") val offsetS: Double,
    @SerialName("duration_s") val durationS: Double? = null,
    @SerialName("discogs_url") val discogsUrl: String? = null,
    @SerialName("cover_url") val coverUrl: String? = null
)

sealed class MatchState {
    data object Idle : MatchState()
    data object Listening : MatchState()
    data class Matched(val candidate: MatchCandidate) : MatchState()
}

data class LogEntry(
    val id: Long = System.nanoTime(),
    val timestamp: Long = System.currentTimeMillis(),
    val message: String,
    val level: LogLevel
)

enum class LogLevel { INFO, SUCCESS, WARNING, ERROR }
