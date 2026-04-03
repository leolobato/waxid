package cc.waxid.android.audio

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.nio.ByteBuffer
import java.nio.ByteOrder

object WavHelper {
    fun buildHeader(sampleRate: Int, dataSize: Int): ByteArray {
        val header = ByteArray(44)
        val buf = ByteBuffer.wrap(header).order(ByteOrder.LITTLE_ENDIAN)
        header[0] = 'R'.code.toByte(); header[1] = 'I'.code.toByte()
        header[2] = 'F'.code.toByte(); header[3] = 'F'.code.toByte()
        buf.putInt(4, 36 + dataSize)
        header[8] = 'W'.code.toByte(); header[9] = 'A'.code.toByte()
        header[10] = 'V'.code.toByte(); header[11] = 'E'.code.toByte()
        header[12] = 'f'.code.toByte(); header[13] = 'm'.code.toByte()
        header[14] = 't'.code.toByte(); header[15] = ' '.code.toByte()
        buf.putInt(16, 16)
        buf.putShort(20, 1)
        buf.putShort(22, 1)
        buf.putInt(24, sampleRate)
        buf.putInt(28, sampleRate * 2)
        buf.putShort(32, 2)
        buf.putShort(34, 16)
        header[36] = 'd'.code.toByte(); header[37] = 'a'.code.toByte()
        header[38] = 't'.code.toByte(); header[39] = 'a'.code.toByte()
        buf.putInt(40, dataSize)
        return header
    }

    fun readCircularBuffer(buffer: ShortArray, writeIndex: Int): ShortArray {
        val size = buffer.size
        val result = ShortArray(size)
        for (i in 0 until size) {
            result[i] = buffer[(writeIndex + i) % size]
        }
        return result
    }
}

class AudioCaptureManager {
    val bufferDurationS = 10.0
    private var sampleRate = 44100
    private var circularBuffer = ShortArray(0)
    private var writeIndex = 0
    private var samplesWritten = 0
    private var audioRecord: AudioRecord? = null
    private var recordingThread: Thread? = null

    private val _isCapturing = MutableStateFlow(false)
    val isCapturing: StateFlow<Boolean> = _isCapturing

    private val _bufferReady = MutableStateFlow(false)
    val bufferReady: StateFlow<Boolean> = _bufferReady

    val actualSampleRate: Int get() = sampleRate

    @SuppressLint("MissingPermission")
    fun start() {
        if (_isCapturing.value) return

        val nativeRate = android.media.AudioTrack.getNativeOutputSampleRate(
            android.media.AudioManager.STREAM_MUSIC
        )
        sampleRate = if (nativeRate > 0) nativeRate else 44100
        val bufferSize = (sampleRate * bufferDurationS).toInt()
        circularBuffer = ShortArray(bufferSize)
        writeIndex = 0
        samplesWritten = 0
        _bufferReady.value = false

        val minBufferSize = AudioRecord.getMinBufferSize(
            sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )

        audioRecord = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            minBufferSize * 2
        )

        audioRecord?.startRecording()
        _isCapturing.value = true
        Log.i("AudioCapture", "Started recording at ${sampleRate}Hz, state=${audioRecord?.recordingState}")

        recordingThread = Thread {
            val readBuffer = ShortArray(4096)
            var logCounter = 0
            while (_isCapturing.value) {
                val read = audioRecord?.read(readBuffer, 0, readBuffer.size) ?: -1
                if (read > 0) {
                    appendSamples(readBuffer, read)
                    logCounter++
                    if (logCounter % 100 == 0) {
                        Log.i("AudioCapture", "Still recording: samplesWritten=$samplesWritten, bufferReady=${_bufferReady.value}")
                    }
                } else {
                    Log.i("AudioCapture", "AudioRecord.read returned $read")
                }
            }
            Log.i("AudioCapture", "Recording thread exiting")
        }.apply {
            name = "AudioCapture"
            start()
        }
    }

    fun stop() {
        if (!_isCapturing.value) return
        _isCapturing.value = false
        recordingThread?.join(1000)
        recordingThread = null
        audioRecord?.stop()
        audioRecord?.release()
        audioRecord = null
        _bufferReady.value = false
        samplesWritten = 0
        writeIndex = 0
    }

    @Synchronized
    private fun appendSamples(samples: ShortArray, count: Int) {
        for (i in 0 until count) {
            circularBuffer[writeIndex] = samples[i]
            writeIndex = (writeIndex + 1) % circularBuffer.size
        }
        samplesWritten += count
        if (!_bufferReady.value && samplesWritten >= circularBuffer.size) {
            _bufferReady.value = true
        }
    }

    @Synchronized
    fun exportWav(): ByteArray? {
        if (!_bufferReady.value) return null

        val ordered = WavHelper.readCircularBuffer(circularBuffer, writeIndex)
        val dataSize = ordered.size * 2
        val header = WavHelper.buildHeader(sampleRate, dataSize)

        val pcmData = ByteBuffer.allocate(dataSize).order(ByteOrder.LITTLE_ENDIAN)
        for (sample in ordered) {
            pcmData.putShort(sample)
        }

        return header + pcmData.array()
    }
}
