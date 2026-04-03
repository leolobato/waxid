package cc.waxid.android.matching

import android.util.Log
import cc.waxid.android.Config
import cc.waxid.android.audio.AudioCaptureManager
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

class MatchClient(private val audioCaptureManager: AudioCaptureManager) {
    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private val _state = MutableStateFlow<MatchState>(MatchState.Idle)
    val state: StateFlow<MatchState> = _state

    private val _logEntries = MutableStateFlow<List<LogEntry>>(emptyList())
    val logEntries: StateFlow<List<LogEntry>> = _logEntries

    private val _queryCount = MutableStateFlow(0)
    val queryCount: StateFlow<Int> = _queryCount

    private val _lastProcessingTimeMs = MutableStateFlow(0.0)
    val lastProcessingTimeMs: StateFlow<Double> = _lastProcessingTimeMs

    private val maxLogEntries = 50
    private var sendJob: Job? = null
    private var isSending = false

    fun log(message: String, level: LogLevel = LogLevel.INFO) {
        Log.i("MatchClient", message)
        val entry = LogEntry(message = message, level = level)
        _logEntries.value = (_logEntries.value + entry).takeLast(maxLogEntries)
    }

    fun start(scope: CoroutineScope) {
        if (sendJob != null) return
        _queryCount.value = 0
        _state.value = MatchState.Listening
        log("Started listening (sample rate: ${audioCaptureManager.actualSampleRate} Hz)")
        log("Filling buffer (10s)...")

        sendJob = scope.launch {
            while (isActive) {
                delay(3000)
                sendAudio()
            }
        }
    }

    fun stop() {
        sendJob?.cancel()
        sendJob = null
        _state.value = MatchState.Idle
        isSending = false
        log("Stopped")
    }

    private suspend fun sendAudio() {
        if (isSending) return
        val wavData = audioCaptureManager.exportWav()
        if (wavData == null) {
            if (!audioCaptureManager.bufferReady.value) {
                log("Buffer not ready yet...")
            }
            return
        }

        isSending = true
        _queryCount.value += 1
        val queryNum = _queryCount.value
        val wavSizeKB = wavData.size / 1024
        log("Query #$queryNum: sending ${wavSizeKB}KB...")

        val recordedAt = System.currentTimeMillis() / 1000.0

        try {
            val request = Request.Builder()
                .url("${Config.serverUrl}/listen")
                .header("X-Recorded-At", recordedAt.toString())
                .post(wavData.toRequestBody("audio/wav".toMediaType()))
                .build()

            val code = withContext(Dispatchers.IO) {
                httpClient.newCall(request).execute().use { it.code }
            }

            if (code != 202) {
                log("Server returned HTTP $code", LogLevel.ERROR)
            }
        } catch (e: Exception) {
            log("Send failed: ${e.javaClass.simpleName}: ${e.message}", LogLevel.ERROR)
            Log.e("MatchClient", "Send exception", e)
        } finally {
            isSending = false
        }
    }

    fun shutdown() {
        stop()
        httpClient.dispatcher.executorService.shutdown()
    }
}
