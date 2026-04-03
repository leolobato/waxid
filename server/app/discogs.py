"""Shared Discogs API helpers for side/position mapping."""
from __future__ import annotations

import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

_last_discogs_request = 0.0
DISCOGS_MIN_INTERVAL = 3.0


def _discogs_throttle() -> None:
    global _last_discogs_request
    elapsed = time.time() - _last_discogs_request
    if elapsed < DISCOGS_MIN_INTERVAL:
        time.sleep(DISCOGS_MIN_INTERVAL - elapsed)
    _last_discogs_request = time.time()


def extract_discogs_release_id(url: str) -> str | None:
    match = re.search(r"discogs\.com/release/(\d+)", url)
    return match.group(1) if match else None


def fetch_discogs_tracklist(
    discogs_url: str,
) -> tuple[dict[int, tuple[str | None, str]], list[dict]]:
    """Fetch tracklist from Discogs API and return (mapping, raw_tracks).

    Returns empty results on any failure (graceful fallback).
    """
    release_id = extract_discogs_release_id(discogs_url)
    if not release_id:
        logger.warning("Could not parse Discogs release ID from %s", discogs_url)
        return {}, []
    _discogs_throttle()
    try:
        r = requests.get(
            f"https://api.discogs.com/releases/{release_id}",
            headers={"User-Agent": "WaxID/1.0"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        logger.warning("Discogs API error: %s", e)
        return {}, []
    tracklist = [t for t in data.get("tracklist", []) if t.get("type_") == "track"]
    return match_discogs_tracklist(tracklist), tracklist


def match_discogs_tracklist(
    discogs_tracks: list[dict],
) -> dict[int, tuple[str | None, str]]:
    """Build {1-based-index: (side, position)} mapping from Discogs tracklist."""
    mapping: dict[int, tuple[str | None, str]] = {}
    for i, entry in enumerate(discogs_tracks, 1):
        position = entry.get("position", "")
        if not position:
            continue
        side = re.match(r"([A-Za-z]+)", position)
        side_str = side.group(1).upper() if side else None
        mapping[i] = (side_str, position)
    return mapping


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9\s]", "", title.lower()).strip()


def lookup_discogs_position(
    meta: dict,
    track_index: int,
    mapping: dict[int, tuple[str | None, str]],
    discogs_tracks: list[dict] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve (side, position) for a track. Index-first, title-fallback."""
    if not mapping:
        return None, None
    if track_index in mapping:
        return mapping[track_index]
    if discogs_tracks and meta.get("track"):
        norm_title = _normalize_title(meta["track"])
        for i, entry in enumerate(discogs_tracks, 1):
            if _normalize_title(entry.get("title", "")) == norm_title:
                if i in mapping:
                    return mapping[i]
    return None, None
