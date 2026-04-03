from dataclasses import dataclass

@dataclass(frozen=True)
class FingerprintConfig:
    sample_rate: int = 11025
    n_fft: int = 512
    hop_length: int = 256
    hpf_pole: float = 0.98
    target_density: float = 20.0
    max_peaks_per_frame: int = 5
    fanout: int = 3
    min_dt: int = 2
    max_dt: int = 63
    max_df: int = 31
    freq_delta_bias: int = 31
    match_win: int = 2
    min_count: int = 15
    max_results: int = 5

    @property
    def frame_duration_s(self) -> float:
        return self.hop_length / self.sample_rate

CONFIG = FingerprintConfig()
