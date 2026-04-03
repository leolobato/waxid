import pytest
from app.db import Database
from app.matcher import match_hashes
from app.config import CONFIG

@pytest.fixture
def db_with_track(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    album_id, _ = db.insert_album(artist="Test", name="Album")
    track_id = db.insert_track(album_id=album_id, artist="Test", album="Album", track="Song")
    hashes = []
    for i in range(100):
        hashes.append((1000 + i, track_id, i * 5))
    db.insert_hashes(hashes)
    yield db, track_id
    db.close()

def test_match_finds_correct_track(db_with_track):
    db, track_id = db_with_track
    query_hashes = [(1000 + i, i * 5 - 50) for i in range(10, 30)]
    results = match_hashes(query_hashes, db)
    assert len(results) > 0
    assert results[0]["track_id"] == track_id
    assert results[0]["score"] >= CONFIG.min_count

def test_match_returns_empty_for_unknown(db_with_track):
    db, _ = db_with_track
    query_hashes = [(999999, i) for i in range(10)]
    results = match_hashes(query_hashes, db)
    assert len(results) == 0

def test_match_confidence_ratio(db_with_track):
    db, track_id = db_with_track
    album_id, _ = db.insert_album(artist="Other", name="Album2")
    track_id2 = db.insert_track(album_id=album_id, artist="Other", album="Album2", track="Song2")
    db.insert_hashes([(1000 + i, track_id2, i * 5 + 100) for i in range(10, 20)])
    query_hashes = [(1000 + i, i * 5 - 50) for i in range(10, 30)]
    results = match_hashes(query_hashes, db)
    assert len(results) >= 2, "Should match both tracks"
    assert results[0]["track_id"] == track_id, "First track should win"
    assert results[0]["confidence"] is not None
    assert results[0]["confidence"] > 1.0
