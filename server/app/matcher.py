from __future__ import annotations
import logging
import random
import time

import numpy as np

from .config import CONFIG
from .db import Database

logger = logging.getLogger(__name__)

def match_hashes(
    query_hashes: list[tuple[int, int]], db: Database,
    stoplist: set[int] | None = None,
) -> list[dict]:
    """Match query hashes against the database using offset voting.
    Args:
        query_hashes: list of (hash_value, query_frame_time)
        db: Database instance
        stoplist: set of hash values to ignore (too common to be discriminative)
    Returns:
        List of match results sorted by score descending.
    """
    if not query_hashes:
        return []

    if stoplist:
        query_hashes = [(h, t) for h, t in query_hashes if h not in stoplist]
        if not query_hashes:
            return []

    if CONFIG.max_query_hashes > 0 and len(query_hashes) > CONFIG.max_query_hashes:
        query_hashes = random.sample(query_hashes, CONFIG.max_query_hashes)

    t0 = time.perf_counter()

    q_arr = np.array(query_hashes, dtype=np.int64)  # columns: hash, t_q
    q_hashes = q_arr[:, 0]
    q_times = q_arr[:, 1]

    hash_values = q_hashes.tolist()
    db_hashes, db_track_ids, db_t_frames = db.lookup_hashes_flat(hash_values)
    t_lookup = time.perf_counter()

    if len(db_hashes) == 0:
        logger.debug("match: lookup=%.1fms, 0 votes", (t_lookup - t0) * 1000)
        return []

    # Vectorized cross-join on matching hashes using sorted merge
    # Sort both sides by hash for searchsorted-based join
    q_sort = np.argsort(q_hashes)
    q_hashes_s = q_hashes[q_sort]
    q_times_s = q_times[q_sort]

    db_sort = np.argsort(db_hashes)
    db_hashes_s = db_hashes[db_sort]
    db_track_ids_s = db_track_ids[db_sort]
    db_t_frames_s = db_t_frames[db_sort]

    # For each unique hash, find ranges in both arrays and cross-join
    unique_hashes = np.unique(db_hashes_s)
    q_left = np.searchsorted(q_hashes_s, unique_hashes, side='left')
    q_right = np.searchsorted(q_hashes_s, unique_hashes, side='right')
    db_left = np.searchsorted(db_hashes_s, unique_hashes, side='left')
    db_right = np.searchsorted(db_hashes_s, unique_hashes, side='right')

    # Pre-compute sizes for pre-allocation
    q_sizes = q_right - q_left
    db_sizes = db_right - db_left
    pair_sizes = q_sizes * db_sizes
    total_pairs = int(pair_sizes.sum())

    if total_pairs == 0:
        logger.debug("match: lookup=%.1fms, 0 votes", (t_lookup - t0) * 1000)
        return []

    # Fully vectorized cross-join: compute expanded indices without a Python loop
    # For each hash group i with nq query entries and nd DB entries,
    # the cross product has nq*nd pairs. Within position p of group i:
    #   db_local_idx = p // nq[i],  q_local_idx = p % nq[i]
    group_starts = np.empty(len(pair_sizes), dtype=np.int64)
    group_starts[0] = 0
    np.cumsum(pair_sizes[:-1], out=group_starts[1:])
    pos_in_group = np.arange(total_pairs, dtype=np.int64) - np.repeat(group_starts, pair_sizes)
    nq_expanded = np.repeat(q_sizes, pair_sizes)

    q_idx = np.repeat(q_left, pair_sizes) + pos_in_group % nq_expanded
    db_idx = np.repeat(db_left, pair_sizes) + pos_in_group // nq_expanded

    all_track_ids = db_track_ids_s[db_idx]
    all_offsets = db_t_frames_s[db_idx] - q_times_s[q_idx]

    # Encode (track_id, offset) as single int64 for fast 1D unique
    OFFSET_SHIFT = np.int64(1 << 31)
    keys = (all_track_ids << np.int64(32)) | (all_offsets + OFFSET_SHIFT).astype(np.int64)
    unique_keys, counts = np.unique(keys, return_counts=True)
    u_track_ids = (unique_keys >> np.int64(32)).astype(np.int64)
    u_offsets = (unique_keys & np.int64(0xFFFFFFFF)) - OFFSET_SHIFT

    t_voting = time.perf_counter()

    # Scoring: for each track, windowed sum of vote counts using searchsorted
    win = CONFIG.match_win
    track_best: dict[int, tuple[int, int]] = {}

    # Data is already sorted by (track_id, offset) from np.unique on encoded keys
    # Find boundaries of each track_id
    track_boundaries = np.searchsorted(u_track_ids, np.unique(u_track_ids), side='left')
    track_boundaries = np.append(track_boundaries, len(u_track_ids))

    for i in range(len(track_boundaries) - 1):
        start, end = track_boundaries[i], track_boundaries[i + 1]
        s_offsets = u_offsets[start:end]
        s_counts = counts[start:end]

        left = np.searchsorted(s_offsets, s_offsets - win, side='left')
        right = np.searchsorted(s_offsets, s_offsets + win, side='right')
        cumsum = np.empty(len(s_counts) + 1, dtype=np.int64)
        cumsum[0] = 0
        np.cumsum(s_counts, out=cumsum[1:])
        windowed = cumsum[right] - cumsum[left]

        above_mask = windowed >= CONFIG.min_count
        if not np.any(above_mask):
            continue
        # Zero out below-threshold so argmax picks only valid entries
        windowed_filtered = np.where(above_mask, windowed, np.int64(0))
        best_idx = np.argmax(windowed_filtered)
        tid = int(u_track_ids[start])
        track_best[tid] = (int(windowed[best_idx]), int(s_offsets[best_idx]))

    t_scoring = time.perf_counter()

    if not track_best:
        logger.debug("match: lookup=%.1fms, voting=%.1fms, scoring=%.1fms, no results",
                      (t_lookup - t0) * 1000, (t_voting - t_lookup) * 1000, (t_scoring - t_voting) * 1000)
        return []

    sorted_tracks = sorted(track_best.items(), key=lambda x: x[1][0], reverse=True)

    results = []
    for i, (track_id, (score, offset_frames)) in enumerate(
        sorted_tracks[:CONFIG.max_results]
    ):
        confidence = None
        if i == 0 and len(sorted_tracks) > 1:
            second_score = sorted_tracks[1][1][0]
            if second_score > 0:
                confidence = score / second_score

        track_info = db.get_track_with_album(track_id)
        if track_info is None:
            continue

        offset_s = offset_frames * CONFIG.frame_duration_s
        cover_url = f"/albums/{track_info['album_id']}/cover" if track_info.get("cover_path") else None

        results.append({
            "track_id": track_id,
            "artist": track_info["artist"],
            "album": track_info["album"],
            "album_id": track_info["album_id"],
            "track": track_info["track"],
            "track_number": track_info["track_number"],
            "year": track_info["year"],
            "side": track_info["side"],
            "position": track_info["position"],
            "score": score,
            "confidence": confidence,
            "offset_s": round(offset_s, 1),
            "duration_s": track_info["duration_s"],
            "discogs_url": track_info.get("discogs_url"),
            "cover_url": cover_url,
        })

    t_end = time.perf_counter()
    logger.debug("match: lookup=%.1fms, voting=%.1fms, scoring=%.1fms, total=%.1fms (%d hashes, %d results)",
                 (t_lookup - t0) * 1000, (t_voting - t_lookup) * 1000,
                 (t_scoring - t_voting) * 1000, (t_end - t0) * 1000,
                 len(hash_values), len(results))
    return results
