import asyncio
import io
import json
import struct
import zipfile
import zlib
import pytest
import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WAXID_DB_PATH", str(tmp_path / "test.db"))
    import importlib
    import app.main
    importlib.reload(app.main)
    from app.main import app
    with TestClient(app) as c:
        yield c


def _make_wav(duration=5.0, sr=44100, freq=440.0) -> bytes:
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    audio = np.sin(2 * np.pi * freq * t).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _make_png():
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr_data = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b'IHDR' + ihdr_data) & 0xffffffff
    ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + struct.pack('>I', ihdr_crc)
    raw = b'\x00\x00\x00\x00'
    idat_data = zlib.compress(raw)
    idat_crc = zlib.crc32(b'IDAT' + idat_data) & 0xffffffff
    idat = struct.pack('>I', len(idat_data)) + b'IDAT' + idat_data + struct.pack('>I', idat_crc)
    iend_crc = zlib.crc32(b'IEND') & 0xffffffff
    iend = struct.pack('>I', 0) + b'IEND' + struct.pack('>I', iend_crc)
    return sig + ihdr + idat + iend


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["tracks_count"] == 0
    assert data["albums_count"] == 0


def test_create_album(client):
    r = client.post("/albums", json={"artist": "Miles Davis", "name": "Kind of Blue", "year": 1959})
    assert r.status_code == 201
    data = r.json()
    assert data["album_id"] == 1


def test_create_album_upsert(client):
    r1 = client.post("/albums", json={"artist": "Miles Davis", "name": "Kind of Blue"})
    r2 = client.post("/albums", json={"artist": "Miles Davis", "name": "Kind of Blue"})
    assert r1.json()["album_id"] == r2.json()["album_id"]
    assert r2.status_code == 200


def test_list_albums(client):
    client.post("/albums", json={"artist": "A", "name": "B"})
    client.post("/albums", json={"artist": "C", "name": "D"})
    r = client.get("/albums")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_get_album_detail(client):
    r = client.post("/albums", json={"artist": "Test", "name": "Album"})
    album_id = r.json()["album_id"]
    wav = _make_wav()
    metadata = json.dumps({"album_id": album_id, "artist": "Test", "album": "Album", "track": "Song"})
    client.post("/ingest", files={"file": ("test.wav", wav, "audio/wav")}, data={"metadata": metadata})
    r = client.get(f"/albums/{album_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["artist"] == "Test"
    assert len(data["tracks"]) == 1


def test_delete_album(client):
    r = client.post("/albums", json={"artist": "Test", "name": "Album"})
    album_id = r.json()["album_id"]
    r = client.delete(f"/albums/{album_id}")
    assert r.status_code == 200
    assert len(client.get("/albums").json()) == 0


def test_upload_and_get_cover(client):
    r = client.post("/albums", json={"artist": "Test", "name": "Album"})
    album_id = r.json()["album_id"]
    png_bytes = _make_png()
    r = client.post(f"/albums/{album_id}/cover",
                     files={"file": ("cover.png", png_bytes, "image/png")})
    assert r.status_code == 200
    assert "cover_path" in r.json()
    r = client.get(f"/albums/{album_id}/cover")
    assert r.status_code == 200


def test_get_cover_404(client):
    r = client.post("/albums", json={"artist": "Test", "name": "Album"})
    album_id = r.json()["album_id"]
    r = client.get(f"/albums/{album_id}/cover")
    assert r.status_code == 404


def test_health_includes_albums(client):
    client.post("/albums", json={"artist": "A", "name": "B"})
    r = client.get("/health")
    assert r.json()["albums_count"] == 1


def test_ingest_and_list(client):
    r = client.post("/albums", json={"artist": "Test", "name": "Album"})
    album_id = r.json()["album_id"]
    wav = _make_wav()
    metadata = json.dumps({"album_id": album_id, "artist": "Test", "album": "Album", "track": "Song"})
    r = client.post("/ingest", files={"file": ("test.wav", wav, "audio/wav")},
                     data={"metadata": metadata})
    assert r.status_code == 200
    data = r.json()
    assert data["track_id"] == 1
    assert data["num_hashes"] > 0
    r = client.get("/tracks")
    assert r.status_code == 200
    tracks = r.json()
    assert len(tracks) == 1
    assert tracks[0]["artist"] == "Test"
    assert tracks[0]["album_id"] == album_id


def test_match_after_ingest(client):
    r = client.post("/albums", json={"artist": "Test", "name": "Album"})
    album_id = r.json()["album_id"]
    wav = _make_wav(duration=10.0, freq=440.0)
    metadata = json.dumps({"album_id": album_id, "artist": "Test", "album": "Album", "track": "Song"})
    client.post("/ingest", files={"file": ("test.wav", wav, "audio/wav")},
                data={"metadata": metadata})
    r = client.post("/match", content=wav,
                     headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200
    data = r.json()
    assert len(data["results"]) > 0
    assert data["results"][0]["artist"] == "Test"
    assert data["results"][0]["album_id"] == album_id


def test_match_includes_duration(client):
    r = client.post("/albums", json={"artist": "Test", "name": "Album"})
    album_id = r.json()["album_id"]
    wav = _make_wav(duration=10.0, freq=440.0)
    metadata = json.dumps({
        "album_id": album_id, "artist": "Test", "album": "Album",
        "track": "Song", "duration_s": 180.5
    })
    client.post("/ingest", files={"file": ("test.wav", wav, "audio/wav")},
                data={"metadata": metadata})
    r = client.post("/match", content=wav, headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200
    data = r.json()
    assert len(data["results"]) > 0
    assert data["results"][0]["duration_s"] == 180.5


def test_delete_track(client):
    r = client.post("/albums", json={"artist": "Test", "name": "Album"})
    album_id = r.json()["album_id"]
    wav = _make_wav()
    metadata = json.dumps({"album_id": album_id, "artist": "Test", "album": "Album", "track": "Song"})
    r = client.post("/ingest", files={"file": ("test.wav", wav, "audio/wav")},
                     data={"metadata": metadata})
    track_id = r.json()["track_id"]
    r = client.delete(f"/tracks/{track_id}")
    assert r.status_code == 200
    r = client.get("/tracks")
    assert len(r.json()) == 0


def test_update_album(client):
    # Create album first
    r = client.post("/albums", json={"artist": "Radiohead", "name": "Amnesiac", "year": 2001})
    album_id = r.json()["album_id"]
    # Update year
    r = client.put(f"/albums/{album_id}", json={"year": 2002})
    assert r.status_code == 200
    assert r.json()["year"] == 2002
    assert r.json()["artist"] == "Radiohead"


def test_update_album_not_found(client):
    r = client.put("/albums/9999", json={"year": 2002})
    assert r.status_code == 404


def test_update_album_unique_conflict(client):
    client.post("/albums", json={"artist": "Radiohead", "name": "Amnesiac"})
    client.post("/albums", json={"artist": "Radiohead", "name": "Kid A"})
    r2 = client.get("/albums")
    kid_a_id = [a for a in r2.json() if a["name"] == "Kid A"][0]["album_id"]
    # Try renaming Kid A to Amnesiac — should conflict
    r = client.put(f"/albums/{kid_a_id}", json={"name": "Amnesiac"})
    assert r.status_code == 409


async def _read_first_sse_event(asgi_app, path="/now-playing/stream"):
    """Call the ASGI app directly and return (status, headers, first_data_line).

    Starlette's TestClient buffers the entire streaming response, so
    ``iter_lines()`` blocks forever on long-lived SSE generators.
    Invoking the ASGI app at the protocol level lets us grab the first
    ``data:`` chunk then cancel the generator.
    """
    status = None
    headers = {}
    body_parts: list[str] = []
    got_event = asyncio.Event()

    async def receive():
        await asyncio.Event().wait()  # block forever (no request body)

    async def send(message):
        nonlocal status, headers
        if message["type"] == "http.response.start":
            status = message["status"]
            headers = {k.decode(): v.decode() for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                body_parts.append(body.decode())
                if any(l.startswith("data:") for l in "".join(body_parts).split("\n")):
                    got_event.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0"},
        "http_version": "1.1", "method": "GET", "path": path,
        "root_path": "", "query_string": b"", "headers": [],
    }
    task = asyncio.create_task(asgi_app(scope, receive, send))
    try:
        await asyncio.wait_for(got_event.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        pass
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return status, headers, "".join(body_parts)


def test_now_playing_stream_initial_idle(client):
    """SSE stream sends idle state when nothing is playing."""
    from app.main import app as asgi_app
    status, headers, body = asyncio.run(_read_first_sse_event(asgi_app))
    assert status == 200
    assert "text/event-stream" in headers.get("content-type", "")
    for line in body.split("\n"):
        if line.startswith("data:"):
            data = json.loads(line[len("data:"):].strip())
            assert data["status"] == "idle"
            break


def test_match_does_not_update_now_playing(client):
    """After a match, now-playing state is NOT updated (only /listen feeds it)."""
    wav = _make_wav(duration=5.0, freq=440.0)
    r = client.post("/albums", json={"artist": "Radiohead", "name": "OK Computer"})
    album_id = r.json()["album_id"]
    meta = {"album_id": album_id, "artist": "Radiohead", "album": "OK Computer", "track": "Airbag"}
    client.post("/ingest", files={"file": ("test.wav", wav, "audio/wav")}, data={"metadata": json.dumps(meta)})
    client.post("/match", content=_make_wav(duration=5.0, freq=440.0), headers={"Content-Type": "audio/wav"})
    # now-playing should still be idle
    from app.main import app as asgi_app
    status, _, body = asyncio.run(_read_first_sse_event(asgi_app))
    assert status == 200
    for line in body.split("\n"):
        if line.startswith("data:"):
            data = json.loads(line[len("data:"):].strip())
            assert data["status"] in ("idle", "listening")
            break


def test_bulk_ingest_single_file(client):
    """Bulk ingest with a single audio file."""
    wav = _make_wav(duration=3.0, freq=440.0)
    r = client.post(
        "/ingest/bulk",
        files=[("files", ("track1.wav", wav, "audio/wav"))],
    )
    assert r.status_code == 200
    data = r.json()
    assert data["tracks_ingested"] == 1
    assert data["albums_created"] == 1
    assert data["errors"] == []


def test_root_serves_index(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "WaxID" in r.text


def test_web_serves_index(client):
    r = client.get("/web/index.html")
    assert r.status_code == 200
    assert "WaxID" in r.text


def test_bulk_ingest_zip_with_discogs_txt(client):
    """Bulk ingest with a zip containing audio + discogs URL in txt."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        wav = _make_wav(duration=3.0, freq=440.0)
        zf.writestr("track1.wav", wav)
        zf.writestr("notes.txt", "https://www.discogs.com/release/12345-Test\n")
    buf.seek(0)
    r = client.post(
        "/ingest/bulk",
        files=[("files", ("album.zip", buf.read(), "application/zip"))],
    )
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data["albums_created"], int)
    assert isinstance(data["tracks_ingested"], int)


def test_bulk_ingest_zip_populates_side_position(client, monkeypatch):
    """Bulk ingest with Discogs URL populates side/position from API."""
    fake_mapping = {1: ("A", "A1"), 2: ("A", "A2")}
    fake_tracks = [
        {"title": "Song One", "position": "A1"},
        {"title": "Song Two", "position": "A2"},
    ]
    import app.main
    monkeypatch.setattr(
        app.main, "fetch_discogs_tracklist", lambda url: (fake_mapping, fake_tracks)
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("01-song-one.wav", _make_wav(duration=3.0, freq=440.0))
        zf.writestr("02-song-two.wav", _make_wav(duration=3.0, freq=880.0))
        zf.writestr("notes.txt", "https://www.discogs.com/release/99999-Test\n")
    buf.seek(0)

    r = client.post(
        "/ingest/bulk",
        files=[("files", ("album.zip", buf.read(), "application/zip"))],
    )
    assert r.status_code == 200
    assert r.json()["tracks_ingested"] == 2

    tracks = client.get("/tracks").json()
    tracks_sorted = sorted(tracks, key=lambda t: t["track_id"])
    assert tracks_sorted[0]["side"] == "A"
    assert tracks_sorted[0]["position"] == "A1"
    assert tracks_sorted[1]["side"] == "A"
    assert tracks_sorted[1]["position"] == "A2"


def test_listen_returns_202(client):
    r = client.post("/listen", content=b"\x00" * 100,
                     headers={"Content-Type": "audio/wav"})
    assert r.status_code == 202
    assert r.json() == {"status": "accepted"}


def test_listen_empty_body_returns_400(client):
    r = client.post("/listen", content=b"")
    assert r.status_code == 400


def test_get_now_playing_returns_idle(client):
    r = client.get("/now-playing")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] in ("idle", "listening", "playing")


class TestSettingsAPI:
    def test_get_settings_returns_defaults(self, client):
        r = client.get("/settings")
        assert r.status_code == 200
        data = r.json()
        assert data["roon_enabled"] is False
        assert data["roon_zone_name"] == "Record Player"
        assert data["server_url"] == "http://localhost:8457"

    def test_put_settings_saves_and_returns(self, client):
        body = {
            "roon_enabled": True,
            "roon_url": "http://10.0.1.9:8377",
            "roon_zone_name": "Record Player",
            "server_url": "http://10.0.1.9:8457",
        }
        r = client.put("/settings", json=body)
        assert r.status_code == 200
        assert r.json()["roon_enabled"] is True

        # Verify persisted
        r2 = client.get("/settings")
        assert r2.json()["roon_enabled"] is True

    def test_put_settings_validates(self, client):
        r = client.put("/settings", json={"roon_enabled": "not_bool"})
        assert r.status_code == 422
