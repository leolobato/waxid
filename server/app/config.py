import os
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
    max_query_hashes: int = 0  # 0 = unlimited; positive value caps query hashes for faster matching
    max_hash_fanout: int = 500  # ignore hashes with more than this many DB entries (0 = no stoplist)
    max_results: int = 5

    @property
    def frame_duration_s(self) -> float:
        return self.hop_length / self.sample_rate

def _load_config() -> FingerprintConfig:
    overrides = {}
    env_val = os.environ.get("WAXID_MAX_QUERY_HASHES")
    if env_val is not None:
        overrides["max_query_hashes"] = int(env_val)
    env_val = os.environ.get("WAXID_MAX_HASH_FANOUT")
    if env_val is not None:
        overrides["max_hash_fanout"] = int(env_val)
    return FingerprintConfig(**overrides)

CONFIG = _load_config()
