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
    hint_track_id: int | None = None,
) -> list[dict]:
    """Match query hashes against the database using offset voting.
    Args:
        query_hashes: list of (hash_value, query_frame_time)
        db: Database instance
        stoplist: set of hash values to ignore (too common to be discriminative)
        hint_track_id: if set, always include this track's best score in the
            results (even below min_count). Used by the listen loop to feed
            the state machine a continuous maintain signal for the currently
            playing track on weak frames.
    Returns:
        List of match results sorted by score descending.
    """
    n_input = len(query_hashes)
    if not query_hashes:
        return []

    if stoplist:
        query_hashes = [(h, t) for h, t in query_hashes if h not in stoplist]
        if not query_hashes:
            logger.debug("match: all %d hashes in stoplist", n_input)
            return []

    if CONFIG.max_query_hashes > 0 and len(query_hashes) > CONFIG.max_query_hashes:
        query_hashes = random.sample(query_hashes, CONFIG.max_query_hashes)

    n_after_stoplist = len(query_hashes)
    t0 = time.perf_counter()

    q_arr = np.array(query_hashes, dtype=np.int64)  # columns: hash, t_q
    q_hashes = q_arr[:, 0]
    q_times = q_arr[:, 1]

    hash_values = q_hashes.tolist()
    db_hashes, db_track_ids, db_t_frames = db.lookup_hashes_flat(hash_values)
    t_lookup = time.perf_counter()

    if len(db_hashes) == 0:
        logger.debug(
            "match: lookup=%.1fms, 0 votes (input=%d, after_stoplist=%d, db_hits=0)",
            (t_lookup - t0) * 1000, n_input, n_after_stoplist,
        )
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
        logger.debug(
            "match: lookup=%.1fms, 0 votes (input=%d, after_stoplist=%d, db_hits=%d)",
            (t_lookup - t0) * 1000, n_input, n_after_stoplist, len(db_hashes),
        )
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
    near_miss: list[tuple[int, int]] = []  # (track_id, best_windowed_score) for below-threshold
    hint_entry: tuple[int, int] | None = None  # (score, offset) for hint_track_id

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

        tid = int(u_track_ids[start])
        best_idx_unfiltered = int(np.argmax(windowed))
        if tid == hint_track_id:
            hint_entry = (int(windowed[best_idx_unfiltered]), int(s_offsets[best_idx_unfiltered]))
        above_mask = windowed >= CONFIG.min_count
        if not np.any(above_mask):
            near_miss.append((tid, int(windowed.max())))
            continue
        # Zero out below-threshold so argmax picks only valid entries
        windowed_filtered = np.where(above_mask, windowed, np.int64(0))
        best_idx = np.argmax(windowed_filtered)
        track_best[tid] = (int(windowed[best_idx]), int(s_offsets[best_idx]))

    # Inject the hinted track if it got votes but didn't clear min_count.
    if hint_track_id is not None and hint_track_id not in track_best and hint_entry is not None:
        track_best[hint_track_id] = hint_entry

    t_scoring = time.perf_counter()

    if not track_best:
        near_miss.sort(key=lambda x: x[1], reverse=True)
        top_str = ", ".join(f"tid={t} best={s}" for t, s in near_miss[:3]) or "none"
        logger.debug(
            "match: lookup=%.1fms, voting=%.1fms, scoring=%.1fms, no results "
            "(input=%d, after_stoplist=%d, db_hits=%d, tracks_voted=%d, min_count=%d, top=[%s])",
            (t_lookup - t0) * 1000, (t_voting - t_lookup) * 1000, (t_scoring - t_voting) * 1000,
            n_input, n_after_stoplist, len(db_hashes), len(near_miss), CONFIG.min_count, top_str,
        )
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
    logger.debug(
        "match: lookup=%.1fms, voting=%.1fms, scoring=%.1fms, total=%.1fms "
        "(input=%d, after_stoplist=%d, db_hits=%d, tracks_voted=%d, %d results)",
        (t_lookup - t0) * 1000, (t_voting - t_lookup) * 1000,
        (t_scoring - t_voting) * 1000, (t_end - t0) * 1000,
        n_input, n_after_stoplist, len(db_hashes),
        len(track_best) + len(near_miss), len(results),
    )
    return results
