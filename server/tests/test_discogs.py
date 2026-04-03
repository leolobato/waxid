import pytest
from app.discogs import (
    extract_discogs_release_id,
    match_discogs_tracklist,
    lookup_discogs_position,
)


def test_extract_release_id_standard_url():
    url = "https://www.discogs.com/release/12345-Some-Album"
    assert extract_discogs_release_id(url) == "12345"


def test_extract_release_id_no_match():
    assert extract_discogs_release_id("https://example.com") is None


def test_match_discogs_tracklist_vinyl():
    tracks = [
        {"position": "A1", "title": "Track One", "type_": "track"},
        {"position": "A2", "title": "Track Two", "type_": "track"},
        {"position": "B1", "title": "Track Three", "type_": "track"},
    ]
    mapping = match_discogs_tracklist(tracks)
    assert mapping == {1: ("A", "A1"), 2: ("A", "A2"), 3: ("B", "B1")}


def test_match_discogs_tracklist_empty_position():
    tracks = [{"position": "", "title": "Intro", "type_": "track"}]
    mapping = match_discogs_tracklist(tracks)
    assert mapping == {}


def test_lookup_by_index():
    mapping = {1: ("A", "A1"), 2: ("A", "A2")}
    assert lookup_discogs_position({}, 1, mapping) == ("A", "A1")
    assert lookup_discogs_position({}, 2, mapping) == ("A", "A2")


def test_lookup_fallback_to_title():
    mapping = {1: ("A", "A1"), 2: ("B", "B1")}
    discogs_tracks = [
        {"title": "Track One", "position": "A1"},
        {"title": "Track Two", "position": "B1"},
    ]
    result = lookup_discogs_position(
        {"track": "Track Two"}, 99, mapping, discogs_tracks
    )
    assert result == ("B", "B1")


def test_lookup_no_mapping():
    assert lookup_discogs_position({}, 1, {}) == (None, None)
