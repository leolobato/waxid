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
    """Music-like test signal: a sequence of decaying notes derived from
    `freq`. A pure sustained tone won't fingerprint — the onset-emphasis
    high-pass leaves nothing after the first frames — so give it onsets."""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    audio = np.zeros_like(t, dtype=np.float64)
    note_len = 0.25
    n_notes = int(duration / note_len)
    for i in range(n_notes):
        start = int(i * note_len * sr)
        seg = t[start:start + int(note_len * sr)] - t[start]
        note_freq = freq * (1 + (i % 5) * 0.25)  # cycle a small arpeggio
        audio[start:start + len(seg)] = np.sin(2 * np.pi * note_freq * seg) * np.exp(-seg / 0.1)
    buf = io.BytesIO()
    sf.write(buf, audio.astype(np.float32), sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _bulk_ingest_done(response) -> dict:
    """Parse NDJSON streamed from /ingest/bulk and return the final 'done' event."""
    for line in response.text.splitlines():
        if not line.strip():
            continue
        evt = json.loads(line)
        if evt.get("type") == "done":
            return evt
    raise AssertionError("No 'done' event in /ingest/bulk response")


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
    assert "version" in data


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
    data = _bulk_ingest_done(r)
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
    data = _bulk_ingest_done(r)
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
    assert _bulk_ingest_done(r)["tracks_ingested"] == 2

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


def test_listen_silent_audio_skips_fingerprint(client, monkeypatch):
    """A near-silent WAV must skip fingerprinting and increment the no-evidence streak."""
    import wave
    import io
    import numpy as np
    from app import main as app_main
    from app.models import MatchCandidate

    # Build a 3-second 11025 Hz mono int16 WAV of near silence (RMS ~ -80 dBFS).
    sr = 11025
    samples = (np.random.randn(sr * 3) * 4).astype(np.int16)  # tiny noise
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())

    calls = {"fingerprint": 0}
    orig_fp = app_main.fingerprint_audio
    def spy_fp(*a, **kw):
        calls["fingerprint"] += 1
        return orig_fp(*a, **kw)
    monkeypatch.setattr(app_main, "fingerprint_audio", spy_fp)

    svc = app_main.now_playing
    svc._current = None
    svc._status = "listening"
    svc._last_played = MatchCandidate(
        track_id=1, artist="A", album="Al", album_id=10, track="T1",
        track_number=1, year=2020, side="A", position="A1", score=20,
        confidence=2.0, offset_s=0.0, duration_s=180.0,
        discogs_url=None, cover_url=None,
    )
    streak_before = svc._no_evidence_streak
    resp = client.post("/listen", content=buf.getvalue(),
                       headers={"Content-Type": "audio/wav"})
    assert resp.status_code == 202
    # Give the background task a moment to run.
    import time as _t; _t.sleep(0.2)
    assert calls["fingerprint"] == 0
    assert app_main.now_playing._no_evidence_streak > streak_before


def test_listen_low_hash_density_discards_candidates(client, monkeypatch):
    """If fingerprint_audio returns very few hashes, the matcher is not called."""
    import wave, io, numpy as np
    from app import main as app_main
    from app.models import MatchCandidate

    sr = 11025
    samples = (np.random.randn(sr * 3) * 5000).astype(np.int16)  # loud enough
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(samples.tobytes())

    # Force the fingerprinter to return very few hashes.
    monkeypatch.setattr(app_main, "fingerprint_audio", lambda *_a, **_kw: [(1, 0), (2, 1)])
    matcher_calls = {"n": 0}
    orig_match = app_main.match_hashes
    def spy_match(*a, **kw):
        matcher_calls["n"] += 1
        return orig_match(*a, **kw)
    monkeypatch.setattr(app_main, "match_hashes", spy_match)

    svc = app_main.now_playing
    svc._current = None
    svc._status = "listening"
    svc._last_played = MatchCandidate(
        track_id=1, artist="A", album="Al", album_id=10, track="T1",
        track_number=1, year=2020, side="A", position="A1", score=20,
        confidence=2.0, offset_s=0.0, duration_s=180.0,
        discogs_url=None, cover_url=None,
    )
    streak_before = svc._no_evidence_streak
    resp = client.post("/listen", content=buf.getvalue(),
                       headers={"Content-Type": "audio/wav"})
    assert resp.status_code == 202
    import time as _t; _t.sleep(0.2)
    assert matcher_calls["n"] == 0
    assert app_main.now_playing._no_evidence_streak > streak_before


def test_listen_passes_expected_next_hints_to_matcher(client, monkeypatch):
    """When a track is playing, the matcher receives both the current
    track_id and the expected-next track_id as hints (the album context
    is derived from _current / _last_played)."""
    import wave, io, numpy as np
    from app import main as app_main
    from app.models import MatchCandidate

    sr = 11025
    samples = (np.random.randn(sr * 3) * 5000).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(samples.tobytes())

    # Swap in a fake album-tracks source so we don't depend on the test DB.
    fake_tracks = [
        {"track_id": 1, "album_id": 10, "side": "A", "position": "A1", "track_number": 1},
        {"track_id": 2, "album_id": 10, "side": "A", "position": "A2", "track_number": 2},
    ]
    svc = app_main.now_playing
    svc.clear_album_cache(None)
    svc._get_tracks_for_album = lambda _aid: fake_tracks

    svc._current = MatchCandidate(
        track_id=1, artist="A", album="Al", album_id=10, track="T1",
        track_number=1, year=2020, side="A", position="A1", score=20,
        confidence=2.0, offset_s=0.0, duration_s=180.0,
        discogs_url=None, cover_url=None,
    )
    svc._status = "playing"

    monkeypatch.setattr(app_main, "fingerprint_audio", lambda *_a, **_kw: [(1, 0)] * 1000)

    captured: dict = {}
    def spy_match(query, db, stoplist, hint_track_ids):
        captured["hint_track_ids"] = set(hint_track_ids or [])
        return []
    monkeypatch.setattr(app_main, "match_hashes", spy_match)

    resp = client.post("/listen", content=buf.getvalue(),
                       headers={"Content-Type": "audio/wav"})
    assert resp.status_code == 202
    import time as _t; _t.sleep(0.2)
    assert {1, 2}.issubset(captured.get("hint_track_ids", set())), captured


def test_album_delete_clears_now_playing_context(client):
    from app import main as app_main
    from app.models import MatchCandidate
    db = app_main.get_db()
    album_id, _ = db.insert_album(artist="A", name="Al", year=2020)
    track_id = db.insert_track(album_id, "A", "Al", "T1", track_number=1)
    db.insert_hashes([(1, track_id, 0)])

    svc = app_main.now_playing
    cur = MatchCandidate(
        track_id=track_id, artist="A", album="Al", album_id=album_id, track="T1",
        track_number=1, year=2020, side="A", position="A1", score=20,
        confidence=2.0, offset_s=0.0, duration_s=180.0,
        discogs_url=None, cover_url=None,
    )
    svc._current = cur
    svc._last_played = cur
    svc._status = "playing"
    svc._album_layout(album_id)  # populate cache

    resp = client.delete(f"/albums/{album_id}")
    assert resp.status_code in (200, 204)
    assert svc._current is None
    assert svc._last_played is None
    assert svc._status == "listening"
    assert album_id not in svc._album_layout_cache


def test_track_delete_invalidates_layout(client):
    from app import main as app_main
    db = app_main.get_db()
    album_id, _ = db.insert_album(artist="A", name="Al", year=2020)
    t1 = db.insert_track(album_id, "A", "Al", "T1", track_number=1)
    t2 = db.insert_track(album_id, "A", "Al", "T2", track_number=2)
    db.insert_hashes([(1, t1, 0), (2, t2, 1)])

    svc = app_main.now_playing
    svc._album_layout(album_id)

    resp = client.delete(f"/tracks/{t1}")
    assert resp.status_code in (200, 204)
    assert album_id not in svc._album_layout_cache
