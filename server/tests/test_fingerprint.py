import numpy as np
import librosa
from app.config import CONFIG
from app.fingerprint import preprocess_audio, compute_spectrogram, find_peaks, generate_hashes, fingerprint_audio

def test_preprocess_returns_mono_at_target_sample_rate():
    sr = 44100
    duration = 1.0
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    stereo = np.stack([np.sin(2 * np.pi * 440 * t), np.sin(2 * np.pi * 440 * t)]).T
    import io, soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, stereo, sr, format="WAV", subtype="PCM_16")
    buf.seek(0)
    audio = preprocess_audio(buf.read())
    assert audio.ndim == 1, "Should be mono"
    expected_length = int(duration * CONFIG.sample_rate)
    assert abs(len(audio) - expected_length) <= 2
    assert audio.dtype == np.float32


def test_spectrogram_shape_and_bins():
    audio = np.random.randn(11025).astype(np.float32)
    sgram = compute_spectrogram(audio)
    # With center=True librosa pads n_fft//2 on each side before framing
    padded_length = len(audio) + CONFIG.n_fft
    n_frames = 1 + (padded_length - CONFIG.n_fft) // CONFIG.hop_length
    # 256 bins from FFT, minus 1 top bin = 255
    assert sgram.shape[0] == (CONFIG.n_fft // 2 + 1) - 1
    assert sgram.shape[1] == n_frames
    assert sgram.dtype == np.float64 or sgram.dtype == np.float32


def test_find_peaks_respects_max_per_frame():
    sgram = np.zeros((255, 100), dtype=np.float64)
    for i in range(10):
        sgram[i * 25, 50] = 10.0 + i
    peaks = find_peaks(sgram)
    frame_50_peaks = [p for p in peaks if p[0] == 50]
    assert len(frame_50_peaks) <= CONFIG.max_peaks_per_frame

def test_find_peaks_returns_time_freq_tuples():
    import librosa
    audio = librosa.tone(440, sr=CONFIG.sample_rate, duration=2.0).astype(np.float32)
    sgram = compute_spectrogram(audio)
    peaks = find_peaks(sgram)
    assert len(peaks) > 0
    for frame, freq in peaks:
        assert 0 <= frame < sgram.shape[1]
        assert 0 <= freq < sgram.shape[0]


def test_generate_hashes_from_peaks():
    peaks = [(10, 50), (15, 55), (20, 60), (25, 80), (30, 45)]
    hashes = generate_hashes(peaks)
    assert len(hashes) > 0
    for h, t in hashes:
        assert isinstance(h, int)
        assert h >= 0
        assert h < (1 << 22)
        assert isinstance(t, int)

def test_generate_hashes_respects_fanout():
    peaks = [(i * 5, 100) for i in range(20)]
    hashes = generate_hashes(peaks)
    from collections import Counter
    anchor_counts = Counter(t for _, t in hashes)
    for count in anchor_counts.values():
        assert count <= CONFIG.fanout

def test_hash_encoding_roundtrip():
    f1, f2, dt = 100, 120, 10
    h = (f1 << 14) | ((f2 - f1 + 31) << 6) | dt
    decoded_f1 = (h >> 14) & 0xFF
    decoded_delta = ((h >> 6) & 0x3F) - 31
    decoded_dt = h & 0x3F
    assert decoded_f1 == f1
    assert decoded_delta == f2 - f1
    assert decoded_dt == dt


def test_fingerprint_audio_end_to_end():
    import io, soundfile as sf
    sr = 44100
    t = np.linspace(0, 5.0, int(sr * 5.0), endpoint=False)
    audio = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    wav_bytes = buf.getvalue()
    hashes = fingerprint_audio(wav_bytes)
    assert len(hashes) > 0
    for h, t_frame in hashes:
        assert isinstance(h, int)
        assert 0 <= h < (1 << 22)
        assert isinstance(t_frame, int)
        assert t_frame >= 0


def test_compute_rms_dbfs_int16_silent():
    import wave, io, math, numpy as np
    from app.fingerprint import compute_rms_dbfs
    sr = 11025
    samples = np.zeros(sr, dtype=np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(samples.tobytes())
    assert compute_rms_dbfs(buf.getvalue()) == -math.inf


def test_compute_rms_dbfs_int16_full_scale():
    import wave, io, numpy as np
    from app.fingerprint import compute_rms_dbfs
    sr = 11025
    samples = np.full(sr, 32767, dtype=np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(samples.tobytes())
    assert compute_rms_dbfs(buf.getvalue()) >= -0.01  # ~0 dBFS


def test_compute_rms_dbfs_uint8_silent_is_centered_at_128():
    """8-bit WAV is unsigned 0..255; a flat 128 stream is digital silence,
    not full-scale. Older treatment as signed int8 would have called this loud."""
    import wave, io, math, numpy as np
    from app.fingerprint import compute_rms_dbfs
    sr = 11025
    samples = np.full(sr, 128, dtype=np.uint8)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(1); w.setframerate(sr)
        w.writeframes(samples.tobytes())
    assert compute_rms_dbfs(buf.getvalue()) == -math.inf


def test_compute_rms_dbfs_rejects_unsupported_width():
    import wave, io, pytest, numpy as np
    from app.fingerprint import compute_rms_dbfs
    sr = 11025
    samples = np.zeros(sr, dtype=np.int32)
    buf = io.BytesIO()
    # 24-bit WAV: write raw frames with sampwidth=3.
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(3); w.setframerate(sr)
        # 3 bytes per sample, all zero.
        w.writeframes(b"\x00\x00\x00" * sr)
    with pytest.raises(ValueError):
        compute_rms_dbfs(buf.getvalue())
