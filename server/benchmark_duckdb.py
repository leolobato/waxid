#!/usr/bin/env python3
"""
Benchmark: DuckDB vs SQLite hash lookup strategies.

Imports hashes from fingerprints.db (SQLite) into a DuckDB file,
then benchmarks all lookup strategies side by side.

Run in the same directory as fingerprints.db:
    python3 benchmark_duckdb.py <audio_file> [fingerprints.db]

Requires: pip install numpy librosa soundfile scipy duckdb
"""
import random
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Config (matches server/app/config.py defaults)
# ---------------------------------------------------------------------------
SAMPLE_RATE = 11025
N_FFT = 512
HOP_LENGTH = 256
HPF_POLE = 0.98
TARGET_DENSITY = 20.0
MAX_PEAKS_PER_FRAME = 5
FANOUT = 3
MIN_DT = 2
MAX_DT = 63
MAX_DF = 31
FREQ_DELTA_BIAS = 31
MATCH_WIN = 2
MIN_COUNT = 15
MAX_RESULTS = 5

DB_PATH = "fingerprints.db"
DUCKDB_PATH = "fingerprints.duckdb"
ITERATIONS = 20
CLIP_SECONDS = 3

# ---------------------------------------------------------------------------
# Fingerprinting (self-contained copy of the pipeline)
# ---------------------------------------------------------------------------
import numpy as np
import librosa
import soundfile as sf
import io
from scipy.signal import lfilter


def fingerprint_audio(audio_bytes):
    buf = io.BytesIO(audio_bytes)
    audio, sr = sf.read(buf, dtype="float32")
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    if sr != SAMPLE_RATE:
        audio = librosa.resample(audio, orig_sr=sr, target_sr=SAMPLE_RATE)
    audio = audio.astype(np.float32)

    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)
    stft = librosa.stft(audio, n_fft=N_FFT, hop_length=HOP_LENGTH, window=window, center=True)
    sgram = np.abs(stft)[:-1, :]
    max_val = sgram.max()
    if max_val > 0:
        sgram = np.log(np.maximum(sgram, max_val / 1e6))
    else:
        sgram = np.zeros_like(sgram)
    sgram -= sgram.mean()
    b = np.array([1.0, -1.0])
    a = np.array([1.0, -HPF_POLE])
    for i in range(sgram.shape[0]):
        sgram[i, :] = lfilter(b, a, sgram[i, :])

    n_freq, n_frames = sgram.shape
    frames_per_sec = SAMPLE_RATE / HOP_LENGTH
    density_ratio = TARGET_DENSITY / frames_per_sec
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
        for val, freq in candidates[:MAX_PEAKS_PER_FRAME]:
            frame_peaks.append((col, freq))
            threshold[freq] = val
        peaks.extend(frame_peaks)
        threshold *= a_dec
    if not peaks:
        return []
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

    peaks_sorted = sorted(pruned, key=lambda p: p[0])
    hashes = []
    for i, (t1, f1) in enumerate(peaks_sorted):
        paired = 0
        for j in range(i + 1, len(peaks_sorted)):
            if paired >= FANOUT:
                break
            t2, f2 = peaks_sorted[j]
            dt = t2 - t1
            if dt < MIN_DT:
                continue
            if dt > MAX_DT:
                break
            df = f2 - f1
            if abs(df) > MAX_DF:
                continue
            hash_val = (f1 & 0xFF) << 14 | ((df + FREQ_DELTA_BIAS) & 0x3F) << 6 | (dt & 0x3F)
            hashes.append((hash_val, t1))
            paired += 1
    return hashes


# ---------------------------------------------------------------------------
# DuckDB setup
# ---------------------------------------------------------------------------

def setup_duckdb(sqlite_path, duckdb_path):
    """Import hashes table from SQLite into DuckDB."""
    import duckdb

    duck_file = Path(duckdb_path)
    if duck_file.exists():
        # Check if already populated
        conn = duckdb.connect(str(duck_file))
        try:
            count = conn.execute("SELECT COUNT(*) FROM hashes").fetchone()[0]
            if count > 0:
                print(f"DuckDB already exists with {count:,} hashes, reusing")
                return conn
        except Exception:
            pass
        conn.close()
        duck_file.unlink()

    print("Importing SQLite hashes into DuckDB...")
    conn = duckdb.connect(str(duck_file))
    conn.execute("INSTALL sqlite; LOAD sqlite;")
    conn.execute(f"ATTACH '{sqlite_path}' AS sqlite_db (TYPE sqlite, READ_ONLY)")

    t0 = time.perf_counter()
    conn.execute("""
        CREATE TABLE hashes AS
        SELECT hash, track_id, t_frame FROM sqlite_db.hashes
    """)
    elapsed = time.perf_counter() - t0
    count = conn.execute("SELECT COUNT(*) FROM hashes").fetchone()[0]
    size_mb = duck_file.stat().st_size / 1024 / 1024
    print(f"Imported {count:,} hashes in {elapsed:.1f}s ({size_mb:.0f} MB DuckDB file)")

    print("Creating index...")
    t0 = time.perf_counter()
    conn.execute("CREATE INDEX idx_hash ON hashes(hash)")
    elapsed = time.perf_counter() - t0
    print(f"Index created in {elapsed:.1f}s")

    conn.execute("DETACH sqlite_db")
    return conn


# ---------------------------------------------------------------------------
# SQLite lookup strategies
# ---------------------------------------------------------------------------

def sqlite_lookup_in_batched(conn, hash_values, batch_size=500):
    result = defaultdict(list)
    for i in range(0, len(hash_values), batch_size):
        batch = hash_values[i:i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT hash, track_id, t_frame FROM hashes WHERE hash IN ({placeholders})",
            batch,
        ).fetchall()
        for row in rows:
            result[row[0]].append((row[1], row[2]))
    return dict(result)


# ---------------------------------------------------------------------------
# DuckDB lookup strategies
# ---------------------------------------------------------------------------

def duckdb_lookup_in(conn, hash_values):
    """DuckDB: WHERE hash IN (...)."""
    result = defaultdict(list)
    placeholders = ",".join(str(h) for h in hash_values)
    rows = conn.execute(
        f"SELECT hash, track_id, t_frame FROM hashes WHERE hash IN ({placeholders})"
    ).fetchall()
    for row in rows:
        result[row[0]].append((row[1], row[2]))
    return dict(result)


def duckdb_lookup_values_join(conn, hash_values):
    """DuckDB: VALUES list + JOIN."""
    result = defaultdict(list)
    values = ",".join(f"({h})" for h in hash_values)
    rows = conn.execute(
        f"SELECT h.hash, h.track_id, h.t_frame "
        f"FROM hashes h INNER JOIN (VALUES {values}) AS q(hash) ON h.hash = q.hash"
    ).fetchall()
    for row in rows:
        result[row[0]].append((row[1], row[2]))
    return dict(result)


# ---------------------------------------------------------------------------
# Matching (shared voting logic)
# ---------------------------------------------------------------------------

def match_with_lookup(query_hashes, conn, lookup_fn):
    if not query_hashes:
        return []
    hash_values = [h for h, _ in query_hashes]
    query_time_map = defaultdict(list)
    for h, t_q in query_hashes:
        query_time_map[h].append(t_q)

    db_matches = lookup_fn(conn, hash_values)

    votes = defaultdict(int)
    for h_val, db_entries in db_matches.items():
        for t_q in query_time_map[h_val]:
            for track_id, t_db in db_entries:
                votes[(track_id, t_db - t_q)] += 1
    if not votes:
        return []

    track_offsets = defaultdict(lambda: defaultdict(int))
    for (track_id, offset), count in votes.items():
        track_offsets[track_id][offset] += count

    track_best = {}
    for track_id, offset_counts in track_offsets.items():
        for offset, count in offset_counts.items():
            total = sum(offset_counts.get(offset + d, 0)
                        for d in range(-MATCH_WIN, MATCH_WIN + 1))
            if total < MIN_COUNT:
                continue
            if track_id not in track_best or total > track_best[track_id][0]:
                track_best[track_id] = (total, offset)

    return sorted(track_best.items(), key=lambda x: x[1][0], reverse=True)[:MAX_RESULTS]


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def bench(query_hashes, conn, lookup_fn, iterations):
    times = []
    result = None
    for _ in range(iterations):
        t0 = time.perf_counter()
        result = match_with_lookup(query_hashes, conn, lookup_fn)
        t1 = time.perf_counter()
        times.append(t1 - t0)
    top_score = result[0][1][0] if result else 0
    return {
        "mean_ms": sum(times) / len(times) * 1000,
        "min_ms": min(times) * 1000,
        "max_ms": max(times) * 1000,
        "matches": len(result) if result else 0,
        "top_score": top_score,
    }


def main():
    import duckdb

    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <audio_file> [db_path]")
        print(f"  db_path defaults to {DB_PATH}")
        sys.exit(1)

    audio_file = sys.argv[1]
    db_path = sys.argv[2] if len(sys.argv) > 2 else DB_PATH

    if not Path(audio_file).exists():
        print(f"Audio file not found: {audio_file}")
        sys.exit(1)
    if not Path(db_path).exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    # --- SQLite setup ---
    sqlite_conn = sqlite3.connect(db_path)
    sqlite_conn.execute("PRAGMA journal_mode=WAL")
    sqlite_conn.execute("PRAGMA mmap_size=3221225472")
    sqlite_conn.execute("PRAGMA cache_size=-256000")
    sqlite_conn.execute("PRAGMA temp_store=MEMORY")

    hash_count = sqlite_conn.execute("SELECT COUNT(*) FROM hashes").fetchone()[0]
    tracks = sqlite_conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    albums = sqlite_conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
    print(f"DB: {albums} albums, {tracks} tracks, {hash_count:,} hashes ({Path(db_path).stat().st_size / 1024 / 1024:.0f} MB)")

    # --- DuckDB setup ---
    duck_conn = setup_duckdb(db_path, DUCKDB_PATH)

    # --- Fingerprint audio ---
    print(f"\nFingerprinting: {Path(audio_file).name}")
    audio_bytes = Path(audio_file).read_bytes()
    all_hashes = fingerprint_audio(audio_bytes)
    print(f"Total hashes from full track: {len(all_hashes):,}")

    frames_per_sec = SAMPLE_RATE / HOP_LENGTH
    clip_frames = int(CLIP_SECONDS * frames_per_sec)
    mid_frame = max(h[1] for h in all_hashes) // 2
    clip_hashes = [(h, t) for h, t in all_hashes if mid_frame <= t < mid_frame + clip_frames]
    print(f"Clip hashes ({CLIP_SECONDS}s from middle): {len(clip_hashes):,}")
    print(f"Iterations: {ITERATIONS}\n")

    # --- Strategies ---
    strategies = [
        # SQLite
        ("SQLite sample@500",
         sqlite_conn,
         lambda c, hv: sqlite_lookup_in_batched(c, random.sample(hv, min(500, len(hv))))),
        ("SQLite sample@1000",
         sqlite_conn,
         lambda c, hv: sqlite_lookup_in_batched(c, random.sample(hv, min(1000, len(hv))))),
        ("SQLite no sampling",
         sqlite_conn,
         lambda c, hv: sqlite_lookup_in_batched(c, hv)),
        # DuckDB
        ("DuckDB sample@500",
         duck_conn,
         lambda c, hv: duckdb_lookup_in(c, random.sample(hv, min(500, len(hv))))),
        ("DuckDB sample@1000",
         duck_conn,
         lambda c, hv: duckdb_lookup_in(c, random.sample(hv, min(1000, len(hv))))),
        ("DuckDB no sampling",
         duck_conn,
         lambda c, hv: duckdb_lookup_in(c, hv)),
        ("DuckDB VALUES join",
         duck_conn,
         lambda c, hv: duckdb_lookup_values_join(c, hv)),
    ]

    print(f"{'Strategy':<25} {'mean':>8} {'min':>8} {'max':>8} {'matches':>8} {'score':>8}")
    print("-" * 73)

    for name, conn, lookup_fn in strategies:
        stats = bench(clip_hashes, conn, lookup_fn, ITERATIONS)
        print(f"{name:<25} {stats['mean_ms']:7.1f}ms {stats['min_ms']:7.1f}ms "
              f"{stats['max_ms']:7.1f}ms {stats['matches']:>8} {stats['top_score']:>8}")

    sqlite_conn.close()
    duck_conn.close()


if __name__ == "__main__":
    main()
