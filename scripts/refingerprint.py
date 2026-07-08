#!/usr/bin/env python3
"""Re-fingerprint the library in place from an audit CSV.

Each CSV row maps an existing DB album_id to its folder in the music library
(columns: album_id, ..., ingest_path, ...). Audio files are matched to the
album's existing DB tracks by title (exact, then normalized, then unique
track number), and re-ingested via POST /ingest using the DB's own album and
track names — so the server replaces hashes in place and curated metadata
(side/position/Discogs) is preserved. Files that match no DB track are
reported and NOT ingested (they would create new tracks).

Usage:
    python refingerprint.py audit.csv --server http://localhost:8457 [--dry-run]
"""
from __future__ import annotations
import argparse
import csv
import json
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

import requests

from mutagen import File as MutagenFile

from ingest import discover_audio_files, extract_metadata


FILENAME_RE = re.compile(r"^\s*\d+[\s.\-_]+(.+)$")


def extract_meta(file_path: Path) -> dict | None:
    """extract_metadata plus MP4 atom support and a filename fallback, so
    m4a-only albums (whose tags ingest.py's helper can't read) still match."""
    meta = extract_metadata(file_path)
    if meta is not None:
        return meta
    audio = MutagenFile(str(file_path))
    tags = getattr(audio, "tags", None) or {}

    def tag(key):
        val = tags.get(key)
        return str(val[0]) if val else None

    track = tag("\xa9nam")
    number = None
    if tags.get("trkn"):
        number = tags["trkn"][0][0]
    if not track:
        m = FILENAME_RE.match(file_path.stem)
        if not m:
            return None
        track = m.group(1).strip()
        if number is None:
            number = int(re.match(r"\s*(\d+)", file_path.stem).group(1))
    duration = None
    if audio is not None and audio.info is not None:
        duration = round(audio.info.length, 1)
    return {
        "artist": tag("\xa9ART") or tag("aART") or "",
        "album": tag("\xa9alb") or "Unknown Album",
        "track": track,
        "track_number": number,
        "year": None,
        "duration_s": duration,
    }


def discover_files(folder: Path) -> list[Path]:
    """flac/mp3 via ingest.py's discovery; fall back to m4a-only folders
    (transcoded locally before upload — the server can't decode AAC)."""
    files = discover_audio_files(folder)
    if not files:
        files = sorted(f for f in folder.iterdir()
                       if f.is_file() and f.suffix.lower() == ".m4a")
    return files


def read_upload(file_path: Path) -> tuple[str, bytes]:
    """Return (filename, bytes) to upload, transcoding m4a to WAV."""
    if file_path.suffix.lower() == ".m4a":
        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(file_path), tmp.name],
                check=True, capture_output=True,
            )
            return file_path.stem + ".wav", Path(tmp.name).read_bytes()
    return file_path.name, file_path.read_bytes()


def norm_title(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def match_track(meta: dict, db_tracks: list[dict], claimed: set[int]) -> dict | None:
    """Match a file's tags to an unclaimed DB track: exact title, then
    normalized title, then normalized with the file title's trailing
    parenthetical stripped (e.g. "(Album Version)", "(1981)"), then unique
    track number."""
    available = [t for t in db_tracks if t["track_id"] not in claimed]
    for t in available:
        if t["track"] == meta["track"]:
            return t
    normed = norm_title(meta["track"])
    matches = [t for t in available if norm_title(t["track"]) == normed]
    if len(matches) == 1:
        return matches[0]
    stripped = re.sub(r"\s*\([^)]*\)\s*$", "", meta["track"])
    if stripped and stripped != meta["track"]:
        stripped_norm = norm_title(stripped)
        matches = [t for t in available if norm_title(t["track"]) == stripped_norm]
        if len(matches) == 1:
            return matches[0]
    if meta.get("track_number") is not None:
        matches = [t for t in available if t["track_number"] == meta["track_number"]]
        if len(matches) == 1:
            return matches[0]
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", help="Audit CSV with album_id and ingest_path columns")
    parser.add_argument("--server", default="http://localhost:8457")
    parser.add_argument("--dry-run", action="store_true", help="Match and report without ingesting")
    parser.add_argument("--start-album", type=int, default=0,
                        help="Skip albums with album_id lower than this (resume support)")
    args = parser.parse_args()

    with open(args.csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    stats = {"albums_ok": 0, "albums_skipped": 0, "replaced": 0,
             "renamed_exact_to_new_id": 0, "failed": 0}
    unmatched_files: list[str] = []
    tracks_without_file: list[str] = []
    skipped_albums: list[str] = []
    t0 = time.time()

    for row in rows:
        try:
            album_id = int(row["album_id"])
        except (KeyError, ValueError):
            continue
        if album_id < args.start_album:
            continue
        label = f"[{album_id}] {row.get('artist', '?')} - {row.get('album', '?')}"
        folder = Path(row.get("ingest_path", "").strip())
        if not row.get("ingest_path", "").strip() or not folder.is_dir():
            skipped_albums.append(f"{label}: folder missing ({folder})")
            stats["albums_skipped"] += 1
            print(f"SKIP {label}: no folder at {folder}", flush=True)
            continue

        r = requests.get(f"{args.server}/albums/{album_id}", timeout=30)
        if r.status_code != 200:
            skipped_albums.append(f"{label}: not in DB (HTTP {r.status_code})")
            stats["albums_skipped"] += 1
            print(f"SKIP {label}: album not in DB", flush=True)
            continue
        album = r.json()
        db_tracks = album["tracks"]

        audio_files = discover_files(folder)
        if not audio_files:
            skipped_albums.append(f"{label}: no audio files in {folder}")
            stats["albums_skipped"] += 1
            print(f"SKIP {label}: no audio files", flush=True)
            continue

        print(f"\n{label} — {len(audio_files)} files, {len(db_tracks)} DB tracks", flush=True)
        claimed: set[int] = set()
        for i, file_path in enumerate(audio_files, 1):
            # SMB mounts occasionally throw transient EBADF/EIO — retry the
            # tag read a few times before counting the file as failed.
            meta = None
            for attempt in range(3):
                try:
                    meta = extract_meta(file_path)
                    break
                except OSError as e:
                    if attempt == 2:
                        stats["failed"] += 1
                        unmatched_files.append(f"{label}: {file_path.name} (read error: {e})")
                        print(f"  [{i}/{len(audio_files)}] READ ERROR: {file_path.name}: {e}", flush=True)
                    else:
                        time.sleep(2 * (attempt + 1))
            else:
                continue
            if meta is None:
                unmatched_files.append(f"{label}: {file_path.name} (no tags)")
                print(f"  [{i}/{len(audio_files)}] NO TAGS: {file_path.name}", flush=True)
                continue
            db_track = match_track(meta, db_tracks, claimed)
            if db_track is None:
                unmatched_files.append(f"{label}: {file_path.name} (title: {meta['track']!r})")
                print(f"  [{i}/{len(audio_files)}] NO MATCH: {meta['track']!r}", flush=True)
                continue
            claimed.add(db_track["track_id"])
            if args.dry_run:
                print(f"  [{i}/{len(audio_files)}] would replace #{db_track['track_id']} {db_track['track']!r}", flush=True)
                continue

            # Use the DB's own names so the server's (album, track) upsert hits
            # the existing row; omit side/position so curated values survive.
            payload = {
                "album_id": album_id,
                "artist": db_track["artist"],
                "album": db_track["album"],
                "track": db_track["track"],
                "track_number": db_track["track_number"],
                "year": db_track["year"],
                "duration_s": meta["duration_s"],
            }
            resp = None
            for attempt in range(3):
                try:
                    upload_name, upload_bytes = read_upload(file_path)
                    resp = requests.post(
                        f"{args.server}/ingest",
                        files={"file": (upload_name, upload_bytes, "application/octet-stream")},
                        data={"metadata": json.dumps(payload)},
                        timeout=600,
                    )
                    resp.raise_for_status()
                    break
                except Exception as e:
                    resp = None
                    if attempt == 2:
                        stats["failed"] += 1
                        print(f"  [{i}/{len(audio_files)}] FAILED {db_track['track']!r}: {e}", flush=True)
                    else:
                        time.sleep(2 * (attempt + 1))
            if resp is None:
                continue
            result = resp.json()
            if result["track_id"] != db_track["track_id"]:
                # find_track missed the row we intended — should not happen.
                stats["renamed_exact_to_new_id"] += 1
                print(f"  [{i}/{len(audio_files)}] WARNING: created new track "
                      f"#{result['track_id']} instead of replacing #{db_track['track_id']}", flush=True)
            else:
                stats["replaced"] += 1
                print(f"  [{i}/{len(audio_files)}] replaced #{db_track['track_id']} "
                      f"{db_track['track']!r} ({result['num_hashes']} hashes)", flush=True)

        missing = [t for t in db_tracks if t["track_id"] not in claimed]
        for t in missing:
            tracks_without_file.append(f"{label}: #{t['track_id']} {t['track']!r}")
        stats["albums_ok"] += 1

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"Done in {elapsed / 60:.1f} min")
    print(f"Albums processed: {stats['albums_ok']}, skipped: {stats['albums_skipped']}")
    print(f"Tracks replaced: {stats['replaced']}, failed: {stats['failed']}, "
          f"unexpected new IDs: {stats['renamed_exact_to_new_id']}")
    if skipped_albums:
        print(f"\nSkipped albums ({len(skipped_albums)}):")
        for s in skipped_albums:
            print(f"  {s}")
    if unmatched_files:
        print(f"\nFiles with no matching DB track — NOT ingested ({len(unmatched_files)}):")
        for s in unmatched_files:
            print(f"  {s}")
    if tracks_without_file:
        print(f"\nDB tracks with no file — still carrying OLD hashes ({len(tracks_without_file)}):")
        for s in tracks_without_file:
            print(f"  {s}")
    sys.exit(1 if stats["failed"] else 0)


if __name__ == "__main__":
    main()
