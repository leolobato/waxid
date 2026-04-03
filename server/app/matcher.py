from __future__ import annotations
import random
from collections import defaultdict
from .config import CONFIG
from .db import Database

MAX_QUERY_HASHES = 500


def match_hashes(
    query_hashes: list[tuple[int, int]], db: Database
) -> list[dict]:
    """Match query hashes against the database using offset voting.
    Args:
        query_hashes: list of (hash_value, query_frame_time)
        db: Database instance
    Returns:
        List of match results sorted by score descending.
    """
    if not query_hashes:
        return []

    if len(query_hashes) > MAX_QUERY_HASHES:
        query_hashes = random.sample(query_hashes, MAX_QUERY_HASHES)

    hash_values = [h for h, _ in query_hashes]
    query_time_map: dict[int, list[int]] = defaultdict(list)
    for h, t_q in query_hashes:
        query_time_map[h].append(t_q)

    db_matches = db.lookup_hashes(hash_values)

    # Offset voting: count (track_id, offset) pairs
    votes: dict[tuple[int, int], int] = defaultdict(int)
    for h_val, db_entries in db_matches.items():
        for t_q in query_time_map[h_val]:
            for track_id, t_db in db_entries:
                offset = t_db - t_q
                votes[(track_id, offset)] += 1

    if not votes:
        return []

    # Group votes by track_id, sum neighborhoods first, then threshold
    track_offsets: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for (track_id, offset), count in votes.items():
        track_offsets[track_id][offset] += count

    track_best: dict[int, tuple[int, int]] = {}
    for track_id, offset_counts in track_offsets.items():
        for offset, count in offset_counts.items():
            total = 0
            for d in range(-CONFIG.match_win, CONFIG.match_win + 1):
                total += offset_counts.get(offset + d, 0)
            if total < CONFIG.min_count:
                continue
            if track_id not in track_best or total > track_best[track_id][0]:
                track_best[track_id] = (total, offset)

    if not track_best:
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

    return results
