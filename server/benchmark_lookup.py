"""Benchmark match_hashes with 3 lookup strategies using a real audio file."""
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.config import CONFIG
from app.db import Database
from app.fingerprint import fingerprint_audio

DB_PATH = "data/fingerprints.db"
AUDIO_FILE = "/Volumes/music/Collection/Bob Dylan/1964 The Times They Are A-Changin'/02 Ballad of Hollis Brown (Album Version).flac"
ITERATIONS = 20
# Simulate 3-second clips by taking slices of the full fingerprint
CLIP_SECONDS = 3


def lookup_in_batched(db, hash_values, batch_size=500):
    """Original: WHERE hash IN (...) with batching."""
    result = defaultdict(list)
    for i in range(0, len(hash_values), batch_size):
        batch = hash_values[i:i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        rows = db.conn.execute(
            f"SELECT hash, track_id, t_frame FROM hashes WHERE hash IN ({placeholders})",
            batch,
        ).fetchall()
        for row in rows:
            result[row[0]].append((row[1], row[2]))
    return dict(result)


def lookup_temp_table(db, hash_values):
    """New: temp table + JOIN."""
    result = defaultdict(list)
    db.conn.execute("CREATE TEMP TABLE IF NOT EXISTS query_hashes (hash INTEGER NOT NULL)")
    db.conn.execute("DELETE FROM query_hashes")
    db.conn.executemany("INSERT INTO query_hashes VALUES (?)", [(h,) for h in hash_values])
    rows = db.conn.execute(
        "SELECT h.hash, h.track_id, h.t_frame "
        "FROM hashes h INNER JOIN query_hashes q ON h.hash = q.hash"
    ).fetchall()
    for row in rows:
        result[row[0]].append((row[1], row[2]))
    return dict(result)


def match_with_lookup(query_hashes, db, lookup_fn):
    """Run the full match pipeline with a given lookup function."""
    if not query_hashes:
        return []

    hash_values = [h for h, _ in query_hashes]
    query_time_map = defaultdict(list)
    for h, t_q in query_hashes:
        query_time_map[h].append(t_q)

    db_matches = lookup_fn(db, hash_values)

    votes = defaultdict(int)
    for h_val, db_entries in db_matches.items():
        for t_q in query_time_map[h_val]:
            for track_id, t_db in db_entries:
                offset = t_db - t_q
                votes[(track_id, offset)] += 1

    if not votes:
        return []

    track_offsets = defaultdict(lambda: defaultdict(int))
    for (track_id, offset), count in votes.items():
        track_offsets[track_id][offset] += count

    track_best = {}
    for track_id, offset_counts in track_offsets.items():
        for offset, count in offset_counts.items():
            total = sum(offset_counts.get(offset + d, 0)
                        for d in range(-CONFIG.match_win, CONFIG.match_win + 1))
            if total < CONFIG.min_count:
                continue
            if track_id not in track_best or total > track_best[track_id][0]:
                track_best[track_id] = (total, offset)

    sorted_tracks = sorted(track_best.items(), key=lambda x: x[1][0], reverse=True)
    return sorted_tracks[:CONFIG.max_results]


def bench(query_hashes, db, lookup_fn, iterations):
    times = []
    result = None
    for _ in range(iterations):
        t0 = time.perf_counter()
        result = match_with_lookup(query_hashes, db, lookup_fn)
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
    db = Database(DB_PATH)
    health = db.get_health()
    print(f"DB: {health['tracks_count']} tracks, {health['hashes_count']:,} hashes")

    print(f"\nFingerprinting: {Path(AUDIO_FILE).name}")
    audio_bytes = Path(AUDIO_FILE).read_bytes()
    all_hashes = fingerprint_audio(audio_bytes)
    print(f"Total hashes from full track: {len(all_hashes):,}")

    # Take a 3-second clip from the middle
    frames_per_sec = CONFIG.sample_rate / CONFIG.hop_length
    clip_frames = int(CLIP_SECONDS * frames_per_sec)
    mid_frame = max(h[1] for h in all_hashes) // 2
    clip_hashes = [(h, t) for h, t in all_hashes if mid_frame <= t < mid_frame + clip_frames]
    print(f"Clip hashes ({CLIP_SECONDS}s from middle): {len(clip_hashes):,}")
    print(f"Iterations: {ITERATIONS}\n")

    strategies = [
        ("1. IN() + sample@500", lambda db, hv: lookup_in_batched(db, random.sample(hv, min(500, len(hv))))),
        ("2. IN() no sampling ", lambda db, hv: lookup_in_batched(db, hv)),
        ("3. Temp table join  ", lambda db, hv: lookup_temp_table(db, hv)),
    ]

    print(f"{'Strategy':<25} {'mean':>8} {'min':>8} {'max':>8} {'matches':>8} {'score':>8}")
    print("-" * 73)

    for name, lookup_fn in strategies:
        stats = bench(clip_hashes, db, lookup_fn, ITERATIONS)
        print(f"{name:<25} {stats['mean_ms']:7.1f}ms {stats['min_ms']:7.1f}ms "
              f"{stats['max_ms']:7.1f}ms {stats['matches']:>8} {stats['top_score']:>8}")

    db.close()


if __name__ == "__main__":
    main()
