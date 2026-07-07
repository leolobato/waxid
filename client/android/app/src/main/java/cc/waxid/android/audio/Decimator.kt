package cc.waxid.android.audio

import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.roundToInt
import kotlin.math.sin

/**
 * Streaming FIR decimator: anti-alias low-pass, then keep every [factor]-th
 * sample.
 *
 * A naive "every 4th sample" would fold everything above the output Nyquist
 * (5512 Hz for 44100 -> 11025) straight into the band the fingerprinter
 * reads, so the signal is filtered with a windowed-sinc FIR first. Only the
 * kept samples are computed (polyphase-style), and filter history carries
 * across process() calls so audio can be fed in arbitrary chunk sizes.
 */
class Decimator(private val factor: Int, private val taps: FloatArray) {

    private val history = FloatArray(taps.size - 1)
    private var phase = 0  // input samples to skip before the next output

    fun process(input: ShortArray, count: Int): ShortArray {
        if (count <= 0) return ShortArray(0)
        val hist = history.size
        val work = FloatArray(hist + count)
        System.arraycopy(history, 0, work, 0, hist)
        for (i in 0 until count) work[hist + i] = input[i].toFloat()

        val outCount = if (count > phase) (count - phase - 1) / factor + 1 else 0
        val out = ShortArray(outCount)
        var outIdx = 0
        var i = phase
        while (i < count) {
            var acc = 0f
            val newest = hist + i
            for (k in taps.indices) acc += taps[k] * work[newest - k]
            out[outIdx++] = acc.roundToInt().coerceIn(-32768, 32767).toShort()
            i += factor
        }
        phase = i - count

        System.arraycopy(work, work.size - hist, history, 0, hist)
        return out
    }

    companion object {
        /**
         * Decimator for an integer factor. The cutoff sits at ~84% of the
         * output Nyquist so the transition band finishes just below it —
         * comparable to the rolloff librosa's resampler applied on the
         * server, which this replaces.
         */
        fun forFactor(factor: Int, numTaps: Int = 101): Decimator {
            val cutoff = 0.42 / factor  // fraction of the input sample rate
            return Decimator(factor, designLowPass(numTaps, cutoff))
        }

        private fun designLowPass(numTaps: Int, cutoff: Double): FloatArray {
            // Hamming-windowed sinc, normalized to unit DC gain.
            val m = numTaps - 1
            val taps = FloatArray(numTaps)
            var sum = 0.0
            for (i in 0 until numTaps) {
                val x = i - m / 2.0
                val sinc = if (x == 0.0) 2 * cutoff else sin(2 * PI * cutoff * x) / (PI * x)
                val window = 0.54 - 0.46 * cos(2 * PI * i / m)
                val t = sinc * window
                taps[i] = t.toFloat()
                sum += t
            }
            for (i in taps.indices) taps[i] = (taps[i] / sum).toFloat()
            return taps
        }
    }
}
