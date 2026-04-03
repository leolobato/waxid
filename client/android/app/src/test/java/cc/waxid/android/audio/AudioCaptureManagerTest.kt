package cc.waxid.android.audio

import org.junit.Assert.*
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder

class AudioCaptureManagerTest {

    @Test
    fun `buildWavHeader produces valid RIFF header`() {
        val sampleRate = 44100
        val dataSize = 882000 // 10s * 44100 * 2 bytes
        val header = WavHelper.buildHeader(sampleRate, dataSize)

        assertEquals(44, header.size)
        val buf = ByteBuffer.wrap(header).order(ByteOrder.LITTLE_ENDIAN)
        assertEquals("RIFF", String(header, 0, 4))
        assertEquals(36 + dataSize, buf.getInt(4))
        assertEquals("WAVE", String(header, 8, 4))
        assertEquals("fmt ", String(header, 12, 4))
        assertEquals(16, buf.getInt(16))
        assertEquals(1.toShort(), buf.getShort(20))
        assertEquals(1.toShort(), buf.getShort(22))
        assertEquals(sampleRate, buf.getInt(24))
        assertEquals(sampleRate * 2, buf.getInt(28))
        assertEquals(2.toShort(), buf.getShort(32))
        assertEquals(16.toShort(), buf.getShort(34))
        assertEquals("data", String(header, 36, 4))
        assertEquals(dataSize, buf.getInt(40))
    }

    @Test
    fun `circular buffer reads in correct order`() {
        val bufferSize = 5
        val buffer = ShortArray(bufferSize)
        val samples = shortArrayOf(10, 20, 30, 40, 50, 60, 70)
        var writeIndex = 0
        for (s in samples) {
            buffer[writeIndex % bufferSize] = s
            writeIndex++
        }
        val result = WavHelper.readCircularBuffer(buffer, writeIndex % bufferSize)
        assertArrayEquals(shortArrayOf(30, 40, 50, 60, 70), result)
    }
}
