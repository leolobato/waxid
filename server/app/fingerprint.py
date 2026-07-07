from __future__ import annotations
import io
import numpy as np
import librosa
import soundfile as sf
from scipy.signal import lfilter
from .config import CONFIG


def preprocess_audio(audio_bytes: bytes) -> np.ndarray:
    """Load audio from bytes, convert to mono, resample to target sample rate.
    Returns float32 numpy array at CONFIG.sample_rate Hz, mono.
    """
    buf = io.BytesIO(audio_bytes)
    audio, sr = sf.read(buf, dtype="float32")
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    if sr != CONFIG.sample_rate:
        audio = librosa.resample(audio, orig_sr=sr, target_sr=CONFIG.sample_rate)
    return audio.astype(np.float32)


def compute_spectrogram(audio: np.ndarray) -> np.ndarray:
    """Compute log-magnitude spectrogram with onset emphasis, following audfprint.
    Returns a 2D array of shape (n_freq_bins, n_frames).
    """
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(CONFIG.n_fft) / CONFIG.n_fft)
    stft = librosa.stft(
        audio, n_fft=CONFIG.n_fft, hop_length=CONFIG.hop_length,
        window=window, center=True,
    )
    sgram = np.abs(stft)
    sgram = sgram[:-1, :]  # Remove highest frequency bin
    max_val = sgram.max()
    if max_val > 0:
        sgram = np.log(np.maximum(sgram, max_val / 1e6))
    else:
        sgram = np.zeros_like(sgram)
    sgram -= sgram.mean()
    pole = CONFIG.hpf_pole
    b = np.array([1.0, -1.0])
    a = np.array([1.0, -pole])
    # Filter every frequency row along the time axis in one call. Rows are
    # independent, so this matches the old per-row loop exactly; cast back to
    # the input dtype so downstream peak comparisons stay bit-for-bit identical.
    sgram = lfilter(b, a, sgram, axis=1).astype(sgram.dtype, copy=False)
    return sgram


def find_peaks(sgram: np.ndarray) -> list[tuple[int, int]]:
    """Two-pass peak detection following audfprint.
    Forward pass: decaying threshold, max peaks per frame.
    Backward pass: prune peaks that don't maintain significance.
    Returns list of (frame_index, freq_bin) tuples.
    """
    n_freq, n_frames = sgram.shape
    frames_per_sec = CONFIG.sample_rate / CONFIG.hop_length
    density_ratio = CONFIG.target_density / frames_per_sec
    a_dec = (1.0 - 0.01 * density_ratio) ** 1.0
    a_dec = max(0.5, min(a_dec, 0.9999))

    peaks = []
    threshold = np.zeros(n_freq)
    max_per_frame = CONFIG.max_peaks_per_frame

    # The column loop stays sequential (the threshold decays across frames),
    # but the per-bin local-maximum scan is vectorized. This is equivalent to
    # the old inner loop: interior bins strictly greater than both neighbours
    # and above the current threshold, then the strongest `max_per_frame` of
    # them (ties broken by higher freq, matching the old reverse tuple sort).
    for col in range(n_frames):
        frame = sgram[:, col]
        interior = frame[1:-1]
        is_peak = (
            (interior > frame[:-2])
            & (interior > frame[2:])
            & (interior > threshold[1:-1])
        )
        freqs = np.nonzero(is_peak)[0] + 1
        if freqs.size:
            vals = frame[freqs]
            order = np.lexsort((-freqs, -vals))[:max_per_frame]
            for k in order:
                freq = int(freqs[k])
                peaks.append((col, freq))
                threshold[freq] = vals[k]
        threshold *= a_dec

    if not peaks:
        return peaks

    peaks.sort(key=lambda p: (p[0], p[1]))
    pruned = []
    for i, (col, freq) in enumerate(peaks):
        val = sgram[freq, col]
        keep = True
        for j in range(i + 1, min(i + 10, len(peaks))):
            col2, freq2 = peaks[j]
            if col2 > col + 5:
                break
            if abs(freq2 - freq) <= 3 and sgram[freq2, col2] > val * 1.5:
                keep = False
                break
        if keep:
            pruned.append((col, freq))
    return pruned


def fingerprint_audio(audio_bytes: bytes) -> list[tuple[int, int]]:
    """Full fingerprinting pipeline: bytes -> hashes.
    Returns list of (hash_value, frame_time) tuples.
    """
    audio = preprocess_audio(audio_bytes)
    sgram = compute_spectrogram(audio)
    peaks = find_peaks(sgram)
    hashes = generate_hashes(peaks)
    return hashes


def compute_rms_dbfs(audio_bytes: bytes) -> float:
    """Return the RMS energy of a WAV blob in dBFS (0 dBFS = full-scale)."""
    import wave
    import io
    import math
    with wave.open(io.BytesIO(audio_bytes), "rb") as w:
        sample_width = w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if not frames:
        return -math.inf
    # Per the WAV spec, 8-bit samples are unsigned (0..255 centered at 128);
    # 16-bit and 32-bit are signed.
    if sample_width == 1:
        samples = np.frombuffer(frames, dtype=np.uint8).astype(np.float64) - 128.0
        full_scale = 128.0
    elif sample_width == 2:
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float64)
        full_scale = float(np.iinfo(np.int16).max)
    elif sample_width == 4:
        samples = np.frombuffer(frames, dtype=np.int32).astype(np.float64)
        full_scale = float(np.iinfo(np.int32).max)
    else:
        # 24-bit and exotic widths aren't expected from the Android client.
        raise ValueError(f"Unsupported sample width: {sample_width} bytes")
    if samples.size == 0:
        return -math.inf
    rms = math.sqrt(float(np.mean(samples ** 2)))
    if rms <= 0:
        return -math.inf
    return 20.0 * math.log10(rms / full_scale)


def generate_hashes(peaks: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Generate landmark hashes from peak pairs.
    For each peak, pairs with up to `fanout` subsequent peaks within
    time and frequency constraints. Returns list of (hash_value, anchor_time).
    """
    peaks_sorted = sorted(peaks, key=lambda p: p[0])
    hashes = []
    for i, (t1, f1) in enumerate(peaks_sorted):
        paired = 0
        for j in range(i + 1, len(peaks_sorted)):
            if paired >= CONFIG.fanout:
                break
            t2, f2 = peaks_sorted[j]
            dt = t2 - t1
            if dt < CONFIG.min_dt:
                continue
            if dt > CONFIG.max_dt:
                break
            df = f2 - f1
            if abs(df) > CONFIG.max_df:
                continue
            hash_val = (
                (f1 & 0xFF) << 14
                | ((df + CONFIG.freq_delta_bias) & 0x3F) << 6
                | (dt & 0x3F)
            )
            hashes.append((hash_val, t1))
            paired += 1
    return hashes
