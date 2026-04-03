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
    for i in range(sgram.shape[0]):
        sgram[i, :] = lfilter(b, a, sgram[i, :])
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

    for col in range(n_frames):
        frame = sgram[:, col]
        candidates = []
        for i in range(1, n_freq - 1):
            if frame[i] > frame[i - 1] and frame[i] > frame[i + 1]:
                if frame[i] > threshold[i]:
                    candidates.append((frame[i], i))
        candidates.sort(reverse=True)
        frame_peaks = []
        for val, freq in candidates[:CONFIG.max_peaks_per_frame]:
            frame_peaks.append((col, freq))
            threshold[freq] = val
        peaks.extend(frame_peaks)
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
