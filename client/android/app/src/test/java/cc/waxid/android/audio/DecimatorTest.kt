package cc.waxid.android.audio

import org.junit.Assert.*
import org.junit.Test
import kotlin.math.PI
import kotlin.math.sin
import kotlin.math.sqrt

class DecimatorTest {

    private fun tone(freqHz: Double, sampleRate: Int, seconds: Double, amplitude: Double = 10000.0): ShortArray {
        val n = (sampleRate * seconds).toInt()
        return ShortArray(n) { i -> (amplitude * sin(2 * PI * freqHz * i / sampleRate)).toInt().toShort() }
    }

    private fun rms(samples: ShortArray, skip: Int): Double {
        var sum = 0.0
        for (i in skip until samples.size) sum += samples[i].toDouble() * samples[i]
        return sqrt(sum / (samples.size - skip))
    }

    @Test
    fun `produces one output per factor inputs`() {
        val out = Decimator.forFactor(4).process(ShortArray(44100), 44100)
        assertEquals(11025, out.size)
    }

    @Test
    fun `output count is exact across uneven chunks`() {
        val dec = Decimator.forFactor(4)
        var total = 0
        val chunkSizes = intArrayOf(4096, 1, 3, 4095, 500, 4096)  // sums to 12791
        for (size in chunkSizes) total += dec.process(ShortArray(size), size).size
        // outputs land on every 4th input sample regardless of chunking
        assertEquals(3198, total)
    }

    @Test
    fun `chunked processing equals single shot`() {
        val input = tone(1000.0, 44100, 0.5)
        val whole = Decimator.forFactor(4).process(input, input.size)

        val dec = Decimator.forFactor(4)
        val pieces = mutableListOf<Short>()
        var offset = 0
        while (offset < input.size) {
            val len = minOf(1234, input.size - offset)
            val part = input.copyOfRange(offset, offset + len)
            dec.process(part, len).forEach { pieces.add(it) }
            offset += len
        }
        assertArrayEquals(whole, pieces.toShortArray())
    }

    @Test
    fun `dc passes with unit gain`() {
        val input = ShortArray(44100) { 10000 }
        val out = Decimator.forFactor(4).process(input, input.size)
        // skip the filter warmup, then the output must settle at the input level
        val settled = out.copyOfRange(200, out.size).map { it.toDouble() }.average()
        assertEquals(10000.0, settled, 1.0)
    }

    @Test
    fun `tone below cutoff passes at full level`() {
        val input = tone(1000.0, 44100, 1.0)
        val out = Decimator.forFactor(4).process(input, input.size)
        val ratio = rms(out, skip = 200) / rms(input, skip = 800)
        assertEquals(1.0, ratio, 0.05)
    }

    @Test
    fun `tone above output nyquist is strongly attenuated`() {
        // 8 kHz would alias to 3025 Hz — right in the fingerprint band —
        // if the anti-alias filter were missing.
        val input = tone(8000.0, 44100, 1.0)
        val out = Decimator.forFactor(4).process(input, input.size)
        val ratio = rms(out, skip = 200) / rms(input, skip = 800)
        assertTrue("expected > 40 dB attenuation, got ratio $ratio", ratio < 0.01)
    }
}
