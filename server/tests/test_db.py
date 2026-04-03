import pytest
from app.db import Database

@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    yield d
    d.close()

def test_insert_and_get_album(db):
    album_id, created = db.insert_album(artist="Miles Davis", name="Kind of Blue", year=1959)
    assert album_id == 1
    assert created is True
    album = db.get_album(album_id)
    assert album["artist"] == "Miles Davis"
    assert album["name"] == "Kind of Blue"
    assert album["year"] == 1959

def test_insert_album_upsert(db):
    id1, created1 = db.insert_album(artist="Miles Davis", name="Kind of Blue", year=1959)
    id2, created2 = db.insert_album(artist="Miles Davis", name="Kind of Blue", year=1959)
    assert id1 == id2
    assert created1 is True
    assert created2 is False

def test_insert_album_upsert_fills_nulls(db):
    album_id, _ = db.insert_album(artist="Miles Davis", name="Kind of Blue")
    assert db.get_album(album_id)["discogs_url"] is None
    db.insert_album(artist="Miles Davis", name="Kind of Blue",
                    discogs_url="https://www.discogs.com/release/123")
    assert db.get_album(album_id)["discogs_url"] == "https://www.discogs.com/release/123"

def test_get_albums(db):
    db.insert_album(artist="Miles Davis", name="Kind of Blue", year=1959)
    db.insert_album(artist="John Coltrane", name="A Love Supreme", year=1965)
    albums = db.get_albums()
    assert len(albums) == 2

def test_delete_album_cascades(db):
    album_id, _ = db.insert_album(artist="Test", name="Album")
    track_id = db.insert_track(
        album_id=album_id, artist="Test", album="Album", track="Song"
    )
    db.insert_hashes([(111, track_id, 5)])
    db.delete_album(album_id)
    assert len(db.get_albums()) == 0
    assert len(db.get_tracks()) == 0
    assert len(db.lookup_hashes([111])) == 0

def test_update_album_cover(db):
    album_id, _ = db.insert_album(artist="Test", name="Album")
    db.update_album_cover(album_id, "1-test-album.png")
    album = db.get_album(album_id)
    assert album["cover_path"] == "1-test-album.png"

def test_update_album_discogs(db):
    album_id, _ = db.insert_album(artist="Test", name="Album")
    db.update_album_discogs(album_id, "https://www.discogs.com/release/123")
    album = db.get_album(album_id)
    assert album["discogs_url"] == "https://www.discogs.com/release/123"

def test_insert_and_get_track(db):
    album_id, _ = db.insert_album(artist="Pink Floyd", name="DSOTM", year=1973)
    track_id = db.insert_track(
        album_id=album_id, artist="Pink Floyd", album="DSOTM", track="Time",
        track_number=4, year=1973, duration_s=413.0, source_path="/music/time.flac"
    )
    assert track_id == 1
    tracks = db.get_tracks()
    assert len(tracks) == 1
    assert tracks[0]["artist"] == "Pink Floyd"

def test_insert_and_lookup_hashes(db):
    album_id, _ = db.insert_album(artist="Test", name="Test")
    track_id = db.insert_track(album_id=album_id, artist="Test", album="Test", track="Test")
    hashes = [(12345, track_id, 10), (67890, track_id, 20)]
    db.insert_hashes(hashes)
    results = db.lookup_hashes([12345, 67890, 99999])
    assert len(results) == 2
    assert (track_id, 10) in [(r[0], r[1]) for r in results[12345]]

def test_delete_track_cascades_hashes(db):
    album_id, _ = db.insert_album(artist="Test", name="Test")
    track_id = db.insert_track(album_id=album_id, artist="Test", album="Test", track="Test")
    db.insert_hashes([(111, track_id, 5)])
    db.delete_track(track_id)
    assert len(db.get_tracks()) == 0
    assert len(db.lookup_hashes([111])) == 0

def test_health_counts(db):
    album_id, _ = db.insert_album(artist="A", name="B")
    track_id = db.insert_track(album_id=album_id, artist="A", album="B", track="C")
    db.insert_hashes([(1, track_id, 1), (2, track_id, 2)])
    health = db.get_health()
    assert health["tracks_count"] == 1
    assert health["hashes_count"] == 2
    assert health["albums_count"] == 1
