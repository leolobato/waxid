#!/usr/bin/env python3
"""Ingestion script: reads audio files and feeds them to the WaxID server."""
from __future__ import annotations
import argparse
import json
import re
import sys
import time
import tomllib
from pathlib import Path

import requests
from mutagen import File as MutagenFile

try:
    from server.app.discogs import (
        _discogs_throttle,
        extract_discogs_release_id,
        fetch_discogs_tracklist,
        match_discogs_tracklist,
        lookup_discogs_position,
    )
except ImportError:
    # Fallback: running outside server package (e.g. standalone script)
    _last_discogs_request = 0.0
    DISCOGS_MIN_INTERVAL = 3.0

    def _discogs_throttle():
        global _last_discogs_request
        elapsed = time.time() - _last_discogs_request
        if elapsed < DISCOGS_MIN_INTERVAL:
            time.sleep(DISCOGS_MIN_INTERVAL - elapsed)
        _last_discogs_request = time.time()

    def extract_discogs_release_id(url):
        match = re.search(r"discogs\.com/release/(\d+)", url)
        return match.group(1) if match else None

    def match_discogs_tracklist(discogs_tracks):
        mapping = {}
        for i, entry in enumerate(discogs_tracks, 1):
            position = entry.get("position", "")
            if not position:
                continue
            side = re.match(r"([A-Za-z]+)", position)
            side_str = side.group(1).upper() if side else None
            mapping[i] = (side_str, position)
        return mapping

    def fetch_discogs_tracklist(discogs_url):
        release_id = extract_discogs_release_id(discogs_url)
        if not release_id:
            print(f"  Warning: could not parse Discogs release ID from {discogs_url}")
            return {}, []
        _discogs_throttle()
        try:
            r = requests.get(
                f"https://api.discogs.com/releases/{release_id}",
                headers={"User-Agent": "WaxID/1.0"}, timeout=15,
            )
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            print(f"  Warning: Discogs API error: {e}")
            return {}, []
        tracklist = [t for t in data.get("tracklist", []) if t.get("type_") == "track"]
        return match_discogs_tracklist(tracklist), tracklist

    def _normalize_title(title):
        return re.sub(r"[^a-z0-9\s]", "", title.lower()).strip()

    def lookup_discogs_position(meta, track_index, mapping, discogs_tracks=None):
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


DEFAULT_SERVER_URL = "http://localhost:8457"
DEFAULT_CONFIG_PATH = Path.home() / ".waxid.toml"
SUPPORTED_EXTENSIONS = {".flac", ".mp3"}

DISC_PATTERN = re.compile(r"(?i)^(?:cd|dis[ck])[\s\-_]?\d+$")
COVER_NAMES = ["cover", "front", "folder"]
COVER_EXTENSIONS_PREFERRED = [".png"]
COVER_EXTENSIONS_FALLBACK = [".jpg", ".jpeg"]


def load_config(config_path: str | None = None, server_override: str | None = None) -> dict:
    config = {"server_url": DEFAULT_SERVER_URL}
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if path.exists():
        with open(path, "rb") as f:
            file_config = tomllib.load(f)
        if "server_url" in file_config:
            config["server_url"] = file_config["server_url"]
    if server_override:
        config["server_url"] = server_override
    return config


def discover_album_folders(path: str, recursive: bool = False) -> list[Path]:
    """Discover album folders. Each folder with audio files is an album.
    Subfolders matching disc patterns are merged into the parent."""
    p = Path(path)
    if not p.is_dir():
        return [p.parent] if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS else []
    folders = []
    if recursive:
        for d in sorted(p.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if _has_audio(d):
                folders.append(d)
            else:
                folders.extend(discover_album_folders(str(d), recursive=True))
    else:
        if _has_audio(p):
            folders.append(p)
    return folders


def _has_audio(folder: Path) -> bool:
    for f in folder.iterdir():
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
            return True
        if f.is_dir() and DISC_PATTERN.match(f.name):
            for sub in f.iterdir():
                if sub.is_file() and sub.suffix.lower() in SUPPORTED_EXTENSIONS:
                    return True
    return False


def discover_audio_files(album_folder: Path) -> list[Path]:
    files = []
    direct = sorted(f for f in album_folder.iterdir()
                    if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS)
    files.extend(direct)
    disc_dirs = sorted(d for d in album_folder.iterdir()
                       if d.is_dir() and DISC_PATTERN.match(d.name))
    for disc_dir in disc_dirs:
        disc_files = sorted(f for f in disc_dir.iterdir()
                            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS)
        files.extend(disc_files)
    return files


def extract_metadata(file_path: Path) -> dict | None:
    audio = MutagenFile(str(file_path))
    if audio is None:
        return None

    def get_tag(keys: list[str]) -> str | None:
        for key in keys:
            val = audio.get(key)
            if val:
                return str(val[0]) if isinstance(val, list) else str(val)
        return None

    artist = get_tag(["artist", "TPE1", "TPE2"])
    album = get_tag(["album", "TALB"])
    track = get_tag(["title", "TIT2"])
    if not artist or not track:
        return None

    track_num_str = get_tag(["tracknumber", "TRCK"])
    track_number = None
    if track_num_str:
        track_number = int(track_num_str.split("/")[0])

    year_str = get_tag(["date", "TDRC", "TYER"])
    year = None
    if year_str:
        year = int(str(year_str)[:4])

    duration_s = None
    if audio.info and hasattr(audio.info, "length"):
        duration_s = round(audio.info.length, 1)

    return {
        "artist": artist,
        "album": album or "Unknown Album",
        "track": track,
        "track_number": track_number,
        "year": year,
        "duration_s": duration_s,
    }


def extract_album_metadata(file_path: Path) -> dict | None:
    audio = MutagenFile(str(file_path))
    if audio is None:
        return None

    def get_tag(keys: list[str]) -> str | None:
        for key in keys:
            val = audio.get(key)
            if val:
                return str(val[0]) if isinstance(val, list) else str(val)
        return None

    album_artist = get_tag(["albumartist", "TPE2"]) or get_tag(["artist", "TPE1"])
    album_name = get_tag(["album", "TALB"]) or "Unknown Album"
    year_str = get_tag(["date", "TDRC", "TYER"])
    year = int(str(year_str)[:4]) if year_str else None
    if not album_artist:
        return None
    return {"album_artist": album_artist, "album_name": album_name, "year": year}


def discover_cover_art(album_folder: Path, audio_files: list[Path],
                       discogs_url: str | None = None) -> tuple[Path, str] | None:
    # Folder images (prefer PNG)
    for ext_list in [COVER_EXTENSIONS_PREFERRED, COVER_EXTENSIONS_FALLBACK]:
        for name in COVER_NAMES:
            for ext in ext_list:
                for f in album_folder.iterdir():
                    if f.is_file() and f.stem.lower() == name and f.suffix.lower() == ext:
                        mime = "image/png" if ext == ".png" else "image/jpeg"
                        return (f, mime)
    # Embedded art
    for audio_path in audio_files:
        audio = MutagenFile(str(audio_path))
        if audio is None:
            continue
        if hasattr(audio, "pictures") and audio.pictures:
            pic = audio.pictures[0]
            ext = ".png" if pic.mime == "image/png" else ".jpg"
            tmp = album_folder / f"_embedded_cover{ext}"
            tmp.write_bytes(pic.data)
            return (tmp, pic.mime)
        for key in audio.keys():
            if key.startswith("APIC"):
                pic = audio[key]
                ext = ".png" if pic.mime == "image/png" else ".jpg"
                tmp = album_folder / f"_embedded_cover{ext}"
                tmp.write_bytes(pic.data)
                return (tmp, pic.mime)
    # Discogs fallback
    if discogs_url:
        result = fetch_discogs_cover(discogs_url, album_folder)
        if result:
            return result
    return None


def parse_discogs_url(cli_url: str | None, album_folder: Path) -> str | None:
    if cli_url:
        return cli_url
    notes_path = album_folder / "notes.md"
    if notes_path.exists():
        content = notes_path.read_text()
        match = re.search(r"https?://(?:www\.)?discogs\.com/release/[^\s)\"']+", content)
        if match:
            return match.group(0)
    return None


def fetch_discogs_cover(discogs_url: str, album_folder: Path) -> tuple[Path, str] | None:
    release_id = extract_discogs_release_id(discogs_url)
    if not release_id:
        return None
    _discogs_throttle()
    try:
        r = requests.get(
            f"https://api.discogs.com/releases/{release_id}",
            headers={"User-Agent": "WaxID/1.0"}, timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException:
        return None
    images = data.get("images", [])
    primary = next((img for img in images if img.get("type") == "primary"), None)
    if not primary:
        primary = images[0] if images else None
    if not primary:
        return None
    _discogs_throttle()
    try:
        img_r = requests.get(primary["uri"], headers={"User-Agent": "WaxID/1.0"}, timeout=30)
        img_r.raise_for_status()
    except requests.RequestException:
        return None
    content_type = img_r.headers.get("content-type", "")
    ext = ".png" if "png" in content_type else ".jpg"
    mime = "image/png" if ext == ".png" else "image/jpeg"
    tmp = album_folder / f"_discogs_cover{ext}"
    tmp.write_bytes(img_r.content)
    return (tmp, mime)


def ingest_file(file_path: Path, metadata: dict, server_url: str) -> dict | None:
    meta_json = json.dumps(metadata)
    mime = "audio/mpeg" if file_path.suffix.lower() == ".mp3" else "audio/flac"
    for attempt in range(2):
        try:
            with open(file_path, "rb") as f:
                r = requests.post(
                    f"{server_url}/ingest",
                    files={"file": (file_path.name, f, mime)},
                    data={"metadata": meta_json},
                    timeout=120,
                )
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == 0:
                print(f"  Retrying after error: {e}")
                continue
            print(f"  Failed after retry: {e}")
            return None


def main():
    parser = argparse.ArgumentParser(
        description="Ingest audio files into the WaxID fingerprint server."
    )
    parser.add_argument("path", help="Path to album folder or music library")
    parser.add_argument("--server", help="Server URL (overrides config file)")
    parser.add_argument("--recursive", "-r", action="store_true", help="Scan directories recursively for album folders")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be ingested without doing it")
    parser.add_argument("--config", default=None, help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--discogs", default=None, help="Discogs release URL for this album")
    args = parser.parse_args()

    config = load_config(config_path=args.config, server_override=args.server)
    server_url = config["server_url"]

    album_folders = discover_album_folders(args.path, recursive=args.recursive)
    if not album_folders:
        print(f"No album folders with audio files found at: {args.path}")
        sys.exit(1)

    if not args.dry_run:
        try:
            r = requests.get(f"{server_url}/health", timeout=30)
            r.raise_for_status()
            print(f"Server OK: {server_url}")
        except requests.RequestException as e:
            print(f"Error: cannot reach server at {server_url}: {e}")
            sys.exit(1)

    for folder in album_folders:
        ingest_album(folder, server_url, args.discogs, args.dry_run)


def ingest_album(album_folder: Path, server_url: str, discogs_cli: str | None, dry_run: bool):
    audio_files = discover_audio_files(album_folder)
    if not audio_files:
        print(f"No audio files in {album_folder}")
        return

    album_meta = extract_album_metadata(audio_files[0])
    if album_meta is None:
        print(f"SKIP (missing tags): {album_folder}")
        return

    print(f"\nAlbum: {album_meta['album_artist']} - {album_meta['album_name']}")
    print(f"  Folder: {album_folder}")
    print(f"  Tracks: {len(audio_files)}")

    if dry_run:
        for f in audio_files:
            meta = extract_metadata(f)
            if meta:
                print(f"  WOULD INGEST: {meta['track']}")
        return

    discogs_url = parse_discogs_url(discogs_cli, album_folder)
    album_payload = {
        "artist": album_meta["album_artist"],
        "name": album_meta["album_name"],
        "year": album_meta["year"],
        "discogs_url": discogs_url,
    }
    r = requests.post(f"{server_url}/albums", json=album_payload, timeout=30)
    r.raise_for_status()
    album_id = r.json()["album_id"]
    print(f"  Album ID: {album_id}")

    cover = discover_cover_art(album_folder, audio_files, discogs_url=discogs_url)
    if cover:
        cover_path, cover_mime = cover
        with open(cover_path, "rb") as f:
            ext = cover_path.suffix
            requests.post(
                f"{server_url}/albums/{album_id}/cover",
                files={"file": (f"cover{ext}", f, cover_mime)},
                timeout=30,
            )
        print(f"  Cover: {cover_path.name}")

    discogs_mapping = {}
    discogs_tracks = []
    if discogs_url:
        discogs_mapping, discogs_tracks = fetch_discogs_tracklist(discogs_url)
        if discogs_mapping:
            print(f"  Discogs: {len(discogs_mapping)} tracks mapped")

    for i, file_path in enumerate(audio_files, 1):
        meta = extract_metadata(file_path)
        if meta is None:
            print(f"  [{i}/{len(audio_files)}] SKIP (missing tags): {file_path.name}")
            continue
        side, position = lookup_discogs_position(meta, i, discogs_mapping, discogs_tracks)
        ingest_meta = {
            "album_id": album_id,
            "artist": meta["artist"],
            "album": meta["album"],
            "track": meta["track"],
            "track_number": meta["track_number"],
            "year": meta["year"],
            "duration_s": meta["duration_s"],
            "side": side,
            "position": position,
        }
        result = ingest_file(file_path, ingest_meta, server_url)
        pos_str = f" ({position})" if position else ""
        if result:
            print(f"  [{i}/{len(audio_files)}] {meta['track']}{pos_str} ({result['num_hashes']} hashes)")
        else:
            print(f"  [{i}/{len(audio_files)}] FAILED: {meta['track']}")


if __name__ == "__main__":
    main()
