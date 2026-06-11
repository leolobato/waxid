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

def test_all_tracks_above_min_count_are_returned(db_with_track):
    """v2: the state machine counts every credible track per frame, so the
    matcher must not truncate results to a top-N."""
    db, track_id = db_with_track
    extra_ids = []
    for n in range(6):
        album_id, _ = db.insert_album(artist=f"X{n}", name=f"Album{n}")
        tid = db.insert_track(album_id=album_id, artist=f"X{n}",
                              album=f"Album{n}", track=f"Song{n}")
        # Same hash values at a distinct constant offset per track, so each
        # track accumulates ~20 aligned votes (well above min_count).
        db.insert_hashes([(1000 + i, tid, i * 5 + (n + 1) * 1000)
                          for i in range(10, 30)])
        extra_ids.append(tid)
    query_hashes = [(1000 + i, i * 5 - 50) for i in range(10, 30)]
    results = match_hashes(query_hashes, db)
    returned = {r["track_id"] for r in results}
    assert returned == {track_id, *extra_ids}, f"got only {len(returned)} tracks"
