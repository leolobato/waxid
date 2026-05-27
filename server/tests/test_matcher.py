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


def test_hint_track_ids_injects_each_below_threshold_track(tmp_path):
    """Multiple hinted tracks below CONFIG.min_count are all re-injected."""
    db = Database(str(tmp_path / "fp.db"))
    try:
        album_id, _ = db.insert_album(artist="A", name="Al", year=2020)
        t1 = db.insert_track(album_id, "A", "Al", "T1", track_number=1)
        t2 = db.insert_track(album_id, "A", "Al", "T2", track_number=2)
        t3 = db.insert_track(album_id, "A", "Al", "T3", track_number=3)
        # Insert just a handful of hashes per track (well below min_count).
        for tid in (t1, t2, t3):
            for f in range(3):
                db.insert_hashes([(1000 + f, tid, f)])

        # Query with the same hashes; without hints, none would clear min_count.
        query = [(1000 + f, f) for f in range(3)]

        no_hint = match_hashes(query, db, stoplist=None, hint_track_ids=None)
        assert no_hint == []

        with_hints = match_hashes(query, db, stoplist=None, hint_track_ids=[t1, t2, t3])
        returned_ids = {r["track_id"] for r in with_hints}
        assert {t1, t2, t3}.issubset(returned_ids), with_hints
    finally:
        db.close()
