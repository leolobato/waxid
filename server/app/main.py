from __future__ import annotations
import asyncio
import logging
import os
import re
import tempfile
import time
import json
import zipfile as zipfile_mod
from pathlib import Path
from contextlib import asynccontextmanager

from mutagen import File as MutagenFile

from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException, Response
from fastapi.responses import FileResponse
from starlette.responses import RedirectResponse, StreamingResponse
from starlette.staticfiles import StaticFiles

from .config import CONFIG
from .db import Database
from .fingerprint import fingerprint_audio
from .matcher import match_hashes
from .models import (
    IngestResponse, MatchResponse, MatchCandidate,
    TrackMetadata, TrackInfo, HealthResponse,
    AlbumCreate, AlbumInfo, AlbumDetail, AlbumUpdate, TrackUpdate,
    BulkIngestResponse, BulkIngestError, NowPlayingResponse,
)
from .state import NowPlayingService
from .discogs import fetch_discogs_tracklist, lookup_discogs_position
from .settings import Settings, load_settings, save_settings
from .roon import RoonNotifier

logger = logging.getLogger(__name__)


def _get_db_path() -> str:
    return os.environ.get("WAXID_DB_PATH", str(Path(__file__).parent.parent / "data" / "fingerprints.db"))


def _slugify(text: str) -> str:
    s = text.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


db: Database | None = None
_settings: Settings | None = None
_data_dir: Path | None = None
_roon_notifier: RoonNotifier | None = None


VERSION = os.environ.get("GIT_COMMIT", "dev")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, _settings, _data_dir, _roon_notifier
    hash_limit = CONFIG.max_query_hashes or "unlimited"
    print(f"WaxID Server starting (commit: {VERSION}, max_query_hashes: {hash_limit})")
    db_path = _get_db_path()
    data_dir = Path(db_path).parent
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "covers").mkdir(exist_ok=True)
    _data_dir = data_dir
    _settings = load_settings(data_dir)
    _roon_notifier = RoonNotifier(now_playing, _settings)
    db = Database(db_path)
    yield
    if _roon_notifier:
        await _roon_notifier.shutdown()
    now_playing.shutdown()
    db.close()


app = FastAPI(title="WaxID Server", lifespan=lifespan)

now_playing = NowPlayingService()
_pending_audio: bytes | None = None
_processing = False


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def get_db() -> Database:
    assert db is not None, "Database not initialized"
    return db


def get_settings() -> Settings:
    assert _settings is not None, "Settings not loaded"
    return _settings


AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav"}


def _extract_tags(filepath: str) -> dict:
    """Extract metadata from audio file tags using mutagen."""
    try:
        audio = MutagenFile(filepath, easy=True)
        if audio is None:
            return {}
    except Exception:
        return {}

    def get_tag(keys):
        for k in keys:
            val = audio.get(k)
            if val:
                return str(val[0]) if isinstance(val, list) else str(val)
        return None

    track_number = get_tag(["tracknumber"])
    if track_number and "/" in track_number:
        track_number = track_number.split("/")[0]

    year_str = get_tag(["date", "year"])
    year = None
    if year_str:
        try:
            year = int(year_str[:4])
        except (ValueError, IndexError):
            pass

    duration = None
    if audio.info and hasattr(audio.info, "length"):
        duration = round(audio.info.length, 2)

    return {
        "album_artist": get_tag(["albumartist", "album_artist"]) or get_tag(["artist"]),
        "artist": get_tag(["artist"]),
        "album": get_tag(["album"]),
        "track": get_tag(["title"]),
        "track_number": int(track_number) if track_number else None,
        "year": year,
        "duration_s": duration,
    }


def _find_discogs_url(text: str) -> str | None:
    """Extract Discogs release URL from text."""
    m = re.search(r"https?://(?:www\.)?discogs\.com/release/[^\s\)\"'>]+", text)
    return m.group(0) if m else None


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
COVER_NAMES = {"cover", "front", "folder", "artwork", "album"}


def _find_cover_image(directory: str) -> str | None:
    """Find a cover image file in a directory (by name convention)."""
    for root, dirs, fnames in os.walk(directory):
        for fn in fnames:
            stem = os.path.splitext(fn)[0].lower()
            ext = os.path.splitext(fn)[1].lower()
            if ext in IMAGE_EXTENSIONS and stem in COVER_NAMES:
                return os.path.join(root, fn)
    # Fallback: any image file
    for root, dirs, fnames in os.walk(directory):
        for fn in fnames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in IMAGE_EXTENSIONS:
                return os.path.join(root, fn)
    return None


def _extract_embedded_cover(filepath: str) -> bytes | None:
    """Extract embedded cover art from an audio file."""
    try:
        from mutagen import File as MutagenRawFile
        audio = MutagenRawFile(filepath)
        if audio is None:
            return None
        # FLAC
        if hasattr(audio, "pictures") and audio.pictures:
            return audio.pictures[0].data
        # MP3 (ID3)
        if hasattr(audio, "tags") and audio.tags:
            for key in audio.tags:
                if key.startswith("APIC"):
                    return audio.tags[key].data
        # MP4/M4A
        if hasattr(audio, "tags") and audio.tags and "covr" in audio.tags:
            return bytes(audio.tags["covr"][0])
    except Exception:
        pass
    return None


def _save_cover_for_album(db, album_id: int, image_data: bytes, ext: str = ".jpg") -> None:
    """Save cover image data for an album."""
    album = db.get_album(album_id)
    if not album or album.get("cover_path"):
        return  # Already has a cover
    if ext == ".jpeg":
        ext = ".jpg"
    slug = _slugify(f"{album['artist']}-{album['name']}")
    filename = f"{album_id}-{slug}{ext}"
    covers_dir = Path(_get_db_path()).parent / "covers"
    covers_dir.mkdir(parents=True, exist_ok=True)
    with open(covers_dir / filename, "wb") as f:
        f.write(image_data)
    db.update_album_cover(album_id, filename)


async def _ingest_single_file(
    db, filepath: str, filename: str, discogs_url: str | None = None,
    extract_cover: bool = True,
    side: str | None = None, position: str | None = None,
) -> dict:
    """Ingest a single audio file: extract tags, upsert album, fingerprint."""
    tags = _extract_tags(filepath)
    artist = tags.get("album_artist") or tags.get("artist") or "Unknown Artist"
    album_name = tags.get("album") or "Unknown Album"
    track_name = tags.get("track") or os.path.splitext(filename)[0]

    # Upsert album
    album_id, created = db.insert_album(
        artist, album_name, year=tags.get("year"), discogs_url=discogs_url
    )

    # Try to extract embedded cover art from this audio file
    if extract_cover:
        cover_data = _extract_embedded_cover(filepath)
        if cover_data:
            _save_cover_for_album(db, album_id, cover_data)

    # Read audio bytes and fingerprint in thread
    with open(filepath, "rb") as f:
        audio_bytes = f.read()

    hashes = await asyncio.to_thread(fingerprint_audio, audio_bytes)

    # Insert track
    track_id = db.insert_track(
        album_id=album_id,
        artist=tags.get("artist") or artist,
        album=album_name,
        track=track_name,
        track_number=tags.get("track_number"),
        year=tags.get("year"),
        duration_s=tags.get("duration_s"),
        side=side,
        position=position,
    )

    # Insert hashes
    hash_rows = [(h, track_id, t) for h, t in hashes]
    db.insert_hashes(hash_rows)

    return {"track_id": track_id, "album_id": album_id, "album_created": created}


@app.post("/albums")
async def create_album(album: AlbumCreate, response: Response):
    album_id, created = get_db().insert_album(
        artist=album.artist, name=album.name,
        year=album.year, discogs_url=album.discogs_url,
    )
    response.status_code = 201 if created else 200
    return {"album_id": album_id}


@app.get("/albums", response_model=list[AlbumInfo])
async def list_albums():
    return [AlbumInfo(**a) for a in get_db().get_albums()]


@app.get("/albums/{album_id}", response_model=AlbumDetail)
async def get_album(album_id: int):
    album = get_db().get_album(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    tracks = get_db().get_tracks_for_album(album_id)
    return AlbumDetail(**album, tracks=[TrackInfo(**t) for t in tracks])


@app.delete("/albums/{album_id}")
async def delete_album(album_id: int):
    if not get_db().delete_album(album_id):
        raise HTTPException(status_code=404, detail="Album not found")
    return {"deleted": True}


@app.put("/albums/{album_id}")
async def update_album(album_id: int, body: AlbumUpdate):
    db = get_db()
    try:
        updated = db.update_album(
            album_id,
            artist=body.artist,
            name=body.name,
            year=body.year,
            discogs_url=body.discogs_url,
        )
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            raise HTTPException(409, "Album with that artist/name already exists")
        raise
    if updated is None:
        raise HTTPException(404, "Album not found")
    tracks = db.get_tracks_for_album(album_id)
    return AlbumInfo(
        album_id=updated["album_id"],
        artist=updated["artist"],
        name=updated["name"],
        year=updated["year"],
        discogs_url=updated["discogs_url"],
        cover_path=updated["cover_path"],
        track_count=len(tracks),
    )


@app.post("/albums/{album_id}/apply-discogs")
async def apply_discogs(album_id: int):
    """Fetch Discogs tracklist and update track side/position metadata."""
    db = get_db()
    album = db.get_album(album_id)
    if not album:
        raise HTTPException(404, "Album not found")
    discogs_url = album.get("discogs_url")
    if not discogs_url:
        raise HTTPException(400, "Album has no Discogs URL")

    mapping, discogs_tracks = await asyncio.to_thread(
        fetch_discogs_tracklist, discogs_url
    )
    if not discogs_tracks:
        raise HTTPException(502, "Could not fetch tracklist from Discogs")

    tracks = db.get_tracks_for_album(album_id)
    updated = []
    for track in tracks:
        side, position = lookup_discogs_position(
            {"track": track["track"]},
            track.get("track_number") or 0,
            mapping,
            discogs_tracks,
        )
        if side is not None or position is not None:
            db.update_track(
                track["track_id"],
                track=track["track"],
                track_number=track.get("track_number"),
                side=side,
                position=position,
            )
            updated.append({
                "track_id": track["track_id"],
                "track": track["track"],
                "side": side,
                "position": position,
            })

    return {"updated_count": len(updated), "tracks": updated}


@app.post("/albums/{album_id}/cover")
async def upload_cover(album_id: int, file: UploadFile = File(...)):
    album = get_db().get_album(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    ext = Path(file.filename).suffix.lower() if file.filename else ".jpg"
    if ext not in (".png", ".jpg", ".jpeg"):
        raise HTTPException(status_code=400, detail="Only JPEG/PNG accepted")
    if ext == ".jpeg":
        ext = ".jpg"
    slug = _slugify(f"{album['artist']}-{album['name']}")
    filename = f"{album_id}-{slug}{ext}"
    covers_dir = Path(_get_db_path()).parent / "covers"
    with open(covers_dir / filename, "wb") as f:
        f.write(await file.read())
    get_db().update_album_cover(album_id, filename)
    return {"cover_path": filename}


@app.get("/albums/{album_id}/cover")
async def get_cover(album_id: int):
    album = get_db().get_album(album_id)
    if not album or not album.get("cover_path"):
        raise HTTPException(status_code=404, detail="Cover not found")
    cover_file = Path(_get_db_path()).parent / "covers" / album["cover_path"]
    if not cover_file.exists():
        raise HTTPException(status_code=404, detail="Cover file missing")
    return FileResponse(str(cover_file))


@app.post("/ingest", response_model=IngestResponse)
async def ingest(file: UploadFile = File(...), metadata: str = Form(...)):
    meta = TrackMetadata(**json.loads(metadata))
    audio_bytes = await file.read()
    hashes = fingerprint_audio(audio_bytes)
    track_id = get_db().insert_track(
        album_id=meta.album_id, artist=meta.artist, album=meta.album, track=meta.track,
        track_number=meta.track_number, year=meta.year, duration_s=meta.duration_s,
        side=meta.side, position=meta.position,
    )
    db_hashes = [(h, track_id, t) for h, t in hashes]
    get_db().insert_hashes(db_hashes)
    return IngestResponse(track_id=track_id, num_hashes=len(hashes), duration_s=meta.duration_s)


@app.post("/ingest/bulk", response_model=BulkIngestResponse)
async def ingest_bulk(files: list[UploadFile] = File(...)):
    db = get_db()
    albums_created = 0
    tracks_ingested = 0
    errors = []

    for upload in files:
        try:
            content = await upload.read()
            filename = upload.filename or "unknown"
            ext = os.path.splitext(filename)[1].lower()

            if ext == ".zip":
                with tempfile.TemporaryDirectory() as tmpdir:
                    zpath = os.path.join(tmpdir, "upload.zip")
                    with open(zpath, "wb") as f:
                        f.write(content)
                    with zipfile_mod.ZipFile(zpath) as zf:
                        zf.extractall(tmpdir)

                    audio_files = []
                    discogs_url = None
                    for root, dirs, fnames in os.walk(tmpdir):
                        for fn in fnames:
                            fpath = os.path.join(root, fn)
                            fext = os.path.splitext(fn)[1].lower()
                            if fext in AUDIO_EXTENSIONS:
                                audio_files.append(fpath)
                            elif fext in (".md", ".txt"):
                                try:
                                    with open(fpath, "r", errors="ignore") as tf:
                                        url = _find_discogs_url(tf.read())
                                        if url:
                                            discogs_url = url
                                except Exception:
                                    pass

                    # Check for cover image in the zip
                    cover_image_path = _find_cover_image(tmpdir)

                    # Fetch Discogs side/position mapping if URL found
                    discogs_mapping = {}
                    discogs_tracks = []
                    if discogs_url:
                        try:
                            discogs_mapping, discogs_tracks = await asyncio.to_thread(
                                fetch_discogs_tracklist, discogs_url
                            )
                        except Exception as e:
                            logger.warning("Discogs fetch failed, proceeding without side/position: %s", e)

                    first_file = True
                    for track_idx, fpath in enumerate(sorted(audio_files), 1):
                        try:
                            tags = _extract_tags(fpath)
                            side, position = lookup_discogs_position(
                                {"track": tags.get("track")},
                                track_idx, discogs_mapping, discogs_tracks,
                            )
                            result = await _ingest_single_file(
                                db, fpath, os.path.basename(fpath),
                                discogs_url=discogs_url,
                                extract_cover=first_file and cover_image_path is None,
                                side=side, position=position,
                            )
                            # Upload folder cover image on first track (once we have album_id)
                            if first_file and cover_image_path:
                                img_ext = os.path.splitext(cover_image_path)[1].lower()
                                with open(cover_image_path, "rb") as img_f:
                                    _save_cover_for_album(db, result["album_id"], img_f.read(), img_ext)
                            first_file = False
                            if result.get("album_created"):
                                albums_created += 1
                            tracks_ingested += 1
                        except Exception as e:
                            errors.append({"file": os.path.basename(fpath), "error": str(e)})

            elif ext in AUDIO_EXTENSIONS:
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                    tmp.write(content)
                    tmp_path = tmp.name
                try:
                    result = await _ingest_single_file(db, tmp_path, filename)
                    if result.get("album_created"):
                        albums_created += 1
                    tracks_ingested += 1
                except Exception as e:
                    errors.append({"file": filename, "error": str(e)})
                finally:
                    os.unlink(tmp_path)
            else:
                errors.append({"file": filename, "error": f"Unsupported format: {ext}"})

        except Exception as e:
            errors.append({"file": upload.filename or "unknown", "error": str(e)})

    return BulkIngestResponse(
        albums_created=albums_created,
        tracks_ingested=tracks_ingested,
        errors=[BulkIngestError(**e) for e in errors],
    )


@app.post("/match", response_model=MatchResponse)
async def match(request: Request):
    audio_bytes = await request.body()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio body")
    print(f"Match request received ({len(audio_bytes)} bytes)")
    start = time.time()
    query_hashes = await asyncio.to_thread(fingerprint_audio, audio_bytes)
    print(f"Fingerprinted in {(time.time() - start) * 1000:.1f}ms ({len(query_hashes)} hashes)")
    results = await asyncio.to_thread(match_hashes, query_hashes, get_db())
    elapsed_ms = (time.time() - start) * 1000
    print(f"Match complete in {elapsed_ms:.1f}ms ({len(results)} results)")
    candidates = [MatchCandidate(**r) for r in results]
    return MatchResponse(results=candidates, processing_time_ms=round(elapsed_ms, 1))


@app.post("/listen", status_code=202)
async def listen(request: Request):
    global _pending_audio, _processing
    audio_bytes = await request.body()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio body")
    recorded_at_str = request.headers.get("x-recorded-at")
    recorded_at = float(recorded_at_str) if recorded_at_str else None
    _pending_audio = (audio_bytes, recorded_at)
    if not _processing:
        asyncio.create_task(_listen_loop())
    return {"status": "accepted"}


async def _listen_loop() -> None:
    global _pending_audio, _processing
    _processing = True
    try:
        while _pending_audio is not None:
            audio_bytes, recorded_at = _pending_audio
            _pending_audio = None
            await _process_audio(audio_bytes, recorded_at)
    finally:
        _processing = False


async def _process_audio(audio_bytes: bytes, recorded_at: float | None = None) -> None:
    try:
        print(f"[{_ts()}] Listen: processing {len(audio_bytes)} bytes")
        start = time.time()
        query_hashes = await asyncio.to_thread(fingerprint_audio, audio_bytes)
        print(f"[{_ts()}] Listen: fingerprinted in {(time.time() - start) * 1000:.1f}ms ({len(query_hashes)} hashes)")
        results = await asyncio.to_thread(match_hashes, query_hashes, get_db())
        elapsed_ms = (time.time() - start) * 1000
        candidates = [MatchCandidate(**r) for r in results]
        if candidates:
            top = candidates[0]
            print(f"[{_ts()}] Listen: {top.artist} - {top.track} (score:{top.score}, conf:{top.confidence}, {elapsed_ms:.0f}ms)")
        else:
            print(f"[{_ts()}] Listen: no match ({elapsed_ms:.0f}ms)")
        await now_playing.feed(candidates, recorded_at=recorded_at)
        state = now_playing.get_state()
        print(f"[{_ts()}] Listen: status={state.status}" + (f", track_id={state.track_id}" if state.track_id else ""))
    except Exception:
        import traceback
        print(f"[{_ts()}] Listen ERROR: {traceback.format_exc()}")


@app.get("/tracks", response_model=list[TrackInfo])
async def list_tracks():
    return [TrackInfo(**t) for t in get_db().get_tracks()]


@app.put("/tracks/{track_id}")
async def update_track(track_id: int, body: TrackUpdate):
    updated = get_db().update_track(
        track_id,
        track=body.track,
        track_number=body.track_number,
        side=body.side,
        position=body.position,
    )
    if updated is None:
        raise HTTPException(404, "Track not found")
    return updated


@app.delete("/tracks/{track_id}")
async def delete_track(track_id: int):
    if not get_db().delete_track(track_id):
        raise HTTPException(status_code=404, detail="Track not found")
    return {"deleted": True}


@app.get("/now-playing", response_model=NowPlayingResponse)
async def get_now_playing():
    return now_playing.get_state()


@app.get("/now-playing/stream")
async def now_playing_stream(request: Request):
    async def event_generator():
        current = now_playing.get_state()
        yield f"data: {current.model_dump_json()}\n\n"
        async for update in now_playing.subscribe(timeout=30.0):
            if await request.is_disconnected():
                break
            if update is None:
                yield ": keepalive\n\n"
            else:
                yield f"data: {update.model_dump_json()}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.get("/settings", response_model=Settings)
async def get_settings_endpoint():
    return get_settings()


@app.put("/settings")
async def update_settings(body: Settings):
    global _settings
    assert _data_dir is not None, "Data dir not initialized"
    save_settings(_data_dir, body)
    _settings = body
    if _roon_notifier:
        await _roon_notifier.reconfigure(body)
    return body


@app.get("/health", response_model=HealthResponse)
async def health():
    data = get_db().get_health()
    data["version"] = VERSION
    return data


@app.get("/")
async def root():
    index = Path(__file__).parent.parent / "web" / "index.html"
    return FileResponse(
        index, media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


@app.middleware("http")
async def no_cache_web_assets(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/web/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


web_dir = Path(__file__).parent.parent / "web"
if web_dir.is_dir():
    app.mount("/web", StaticFiles(directory=web_dir, html=True), name="web")
