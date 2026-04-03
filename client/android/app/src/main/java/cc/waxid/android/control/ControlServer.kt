package cc.waxid.android.control

import cc.waxid.android.Config
import cc.waxid.android.matching.MatchState
import io.ktor.http.*
import io.ktor.serialization.kotlinx.json.*
import io.ktor.server.application.*
import io.ktor.server.engine.*
import io.ktor.server.netty.*
import io.ktor.server.plugins.contentnegotiation.*
import io.ktor.server.response.*
import io.ktor.server.routing.*
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

@Serializable
data class StatusResponse(
    val state: String,
    val match: MatchInfo? = null
)

@Serializable
data class MatchInfo(
    val track_id: Int,
    val artist: String,
    val album: String,
    val track: String,
    val score: Int,
    val confidence: Double? = null,
    val offset_s: Double
)

@Serializable
data class CommandResponse(
    val status: String = "ok",
    val state: String,
    val message: String? = null
)

class ControlServer(
    private val onStart: () -> Unit,
    private val onStop: () -> Unit,
    private val getState: () -> MatchState
) {
    private var server: EmbeddedServer<*, *>? = null

    fun start() {
        server = embeddedServer(Netty, port = Config.controlPort, host = "0.0.0.0") {
            install(ContentNegotiation) {
                json(Json { encodeDefaults = true })
            }
            routing {
                post("/start") {
                    val currentState = getState()
                    if (currentState != MatchState.Idle) {
                        call.respond(CommandResponse(state = "listening", message = "already listening"))
                    } else {
                        onStart()
                        call.respond(CommandResponse(state = "listening"))
                    }
                }
                post("/stop") {
                    onStop()
                    call.respond(CommandResponse(state = "idle"))
                }
                get("/status") {
                    val state = getState()
                    val response = when (state) {
                        is MatchState.Idle -> StatusResponse(state = "idle")
                        is MatchState.Listening -> StatusResponse(state = "listening")
                        is MatchState.Matched -> StatusResponse(
                            state = "matched",
                            match = MatchInfo(
                                track_id = state.candidate.trackId,
                                artist = state.candidate.artist,
                                album = state.candidate.album,
                                track = state.candidate.track,
                                score = state.candidate.score,
                                confidence = state.candidate.confidence,
                                offset_s = state.candidate.offsetS
                            )
                        )
                    }
                    call.respond(response)
                }
            }
        }
        server?.start(wait = false)
    }

    fun stop() {
        server?.stop(1000, 2000)
        server = null
    }
}
