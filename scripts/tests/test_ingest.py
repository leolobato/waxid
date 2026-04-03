import sys
import subprocess
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_load_config_from_file(tmp_path):
    from ingest import load_config
    config_path = tmp_path / "config.toml"
    config_path.write_text('server_url = "http://mynas:8457"\n')
    config = load_config(config_path=str(config_path))
    assert config["server_url"] == "http://mynas:8457"


def test_load_config_missing_file():
    from ingest import load_config
    config = load_config(config_path="/nonexistent/path.toml")
    assert config["server_url"] == "http://localhost:8457"


def test_load_config_cli_overrides_file(tmp_path):
    from ingest import load_config
    config_path = tmp_path / "config.toml"
    config_path.write_text('server_url = "http://mynas:8457"\n')
    config = load_config(config_path=str(config_path), server_override="http://other:8457")
    assert config["server_url"] == "http://other:8457"


def _make_flac(path, artist="Test Artist", album="Test Album", title="Test Track",
               tracknumber="1", date="2020", albumartist=None):
    import soundfile as sf_lib
    from mutagen.flac import FLAC
    audio = np.zeros(11025, dtype=np.float32)
    sf_lib.write(str(path), audio, 11025, format="FLAC")
    f = FLAC(str(path))
    f["artist"] = artist
    f["album"] = album
    f["title"] = title
    f["tracknumber"] = tracknumber
    f["date"] = date
    if albumartist:
        f["albumartist"] = albumartist
    f.save()


def test_extract_metadata_flac(tmp_path):
    from ingest import extract_metadata
    flac_path = tmp_path / "track.flac"
    _make_flac(flac_path, artist="Pink Floyd", album="DSOTM", title="Time",
               tracknumber="4", date="1973")
    meta = extract_metadata(flac_path)
    assert meta["artist"] == "Pink Floyd"
    assert meta["album"] == "DSOTM"
    assert meta["track"] == "Time"
    assert meta["track_number"] == 4
    assert meta["year"] == 1973


def test_extract_metadata_missing_tags(tmp_path):
    from ingest import extract_metadata
    import soundfile as sf_lib
    flac_path = tmp_path / "notags.flac"
    audio = np.zeros(11025, dtype=np.float32)
    sf_lib.write(str(flac_path), audio, 11025, format="FLAC")
    meta = extract_metadata(flac_path)
    assert meta is None


# --- discover_album_folders ---

def test_discover_album_folders_flat(tmp_path):
    from ingest import discover_album_folders
    _make_flac(tmp_path / "track1.flac")
    _make_flac(tmp_path / "track2.flac")
    folders = discover_album_folders(str(tmp_path), recursive=False)
    assert folders == [tmp_path]


def test_discover_album_folders_recursive(tmp_path):
    from ingest import discover_album_folders
    album1 = tmp_path / "Artist - Album1"
    album1.mkdir()
    _make_flac(album1 / "01.flac")
    album2 = tmp_path / "Artist - Album2"
    album2.mkdir()
    _make_flac(album2 / "01.flac")
    _make_flac(album2 / "02.flac")
    folders = discover_album_folders(str(tmp_path), recursive=True)
    assert len(folders) == 2
    assert album1 in folders
    assert album2 in folders


def test_discover_album_folders_single_file(tmp_path):
    from ingest import discover_album_folders
    f = tmp_path / "song.flac"
    f.touch()
    folders = discover_album_folders(str(f), recursive=False)
    assert folders == [tmp_path]


def test_discover_album_folders_disc_subfolder(tmp_path):
    """Folder with only disc subfolders is treated as a single album."""
    from ingest import discover_album_folders
    cd1 = tmp_path / "CD1"
    cd1.mkdir()
    _make_flac(cd1 / "01.flac")
    cd2 = tmp_path / "CD2"
    cd2.mkdir()
    _make_flac(cd2 / "01.flac")
    folders = discover_album_folders(str(tmp_path), recursive=False)
    assert folders == [tmp_path]


def test_discover_album_folders_nested_no_audio_skipped(tmp_path):
    """Subfolder with no audio (and not a disc pattern) is skipped."""
    from ingest import discover_album_folders
    sub = tmp_path / "artwork"
    sub.mkdir()
    (sub / "cover.jpg").touch()
    folders = discover_album_folders(str(tmp_path), recursive=False)
    assert folders == []


# --- disc pattern ---

def test_disc_pattern_matches():
    from ingest import DISC_PATTERN
    assert DISC_PATTERN.match("CD1")
    assert DISC_PATTERN.match("cd1")
    assert DISC_PATTERN.match("Disc 1")
    assert DISC_PATTERN.match("Disk2")
    assert DISC_PATTERN.match("disc-1")
    assert DISC_PATTERN.match("DISC_2")


def test_disc_pattern_no_match():
    from ingest import DISC_PATTERN
    assert not DISC_PATTERN.match("artwork")
    assert not DISC_PATTERN.match("extras")
    assert not DISC_PATTERN.match("Bonus")


# --- discover_audio_files ---

def test_discover_audio_files_includes_disc_subfolders(tmp_path):
    from ingest import discover_audio_files
    _make_flac(tmp_path / "track1.flac")
    cd1 = tmp_path / "CD1"
    cd1.mkdir()
    _make_flac(cd1 / "01.flac")
    _make_flac(cd1 / "02.flac")
    files = discover_audio_files(tmp_path)
    assert len(files) == 3
    assert tmp_path / "track1.flac" in files
    assert cd1 / "01.flac" in files
    assert cd1 / "02.flac" in files


# --- extract_album_metadata ---

def test_extract_album_metadata_uses_albumartist(tmp_path):
    from ingest import extract_album_metadata
    flac_path = tmp_path / "track.flac"
    _make_flac(flac_path, artist="Track Artist", album="My Album",
               albumartist="Various Artists")
    meta = extract_album_metadata(flac_path)
    assert meta["album_artist"] == "Various Artists"
    assert meta["album_name"] == "My Album"


def test_extract_album_metadata_falls_back_to_artist(tmp_path):
    from ingest import extract_album_metadata
    flac_path = tmp_path / "track.flac"
    _make_flac(flac_path, artist="Pink Floyd", album="The Wall")
    meta = extract_album_metadata(flac_path)
    assert meta["album_artist"] == "Pink Floyd"
    assert meta["album_name"] == "The Wall"


def test_extract_album_metadata_year(tmp_path):
    from ingest import extract_album_metadata
    flac_path = tmp_path / "track.flac"
    _make_flac(flac_path, artist="A", album="B", date="1979")
    meta = extract_album_metadata(flac_path)
    assert meta["year"] == 1979


def test_extract_album_metadata_missing_artist_returns_none(tmp_path):
    from ingest import extract_album_metadata
    import soundfile as sf_lib
    from mutagen.flac import FLAC
    flac_path = tmp_path / "track.flac"
    audio = np.zeros(11025, dtype=np.float32)
    sf_lib.write(str(flac_path), audio, 11025, format="FLAC")
    f = FLAC(str(flac_path))
    f["album"] = "No Artist Album"
    f["title"] = "Some Track"
    f.save()
    meta = extract_album_metadata(flac_path)
    assert meta is None


# --- cover art discovery ---

def test_discover_cover_art_prefers_png_over_jpg(tmp_path):
    from ingest import discover_cover_art
    (tmp_path / "cover.jpg").write_bytes(b"fake-jpg")
    (tmp_path / "cover.png").write_bytes(b"fake-png")
    result = discover_cover_art(tmp_path, [])
    assert result is not None
    path, mime = result
    assert path.suffix == ".png"
    assert mime == "image/png"


def test_discover_cover_art_finds_front_jpg(tmp_path):
    from ingest import discover_cover_art
    (tmp_path / "front.jpg").write_bytes(b"fake-jpg")
    result = discover_cover_art(tmp_path, [])
    assert result is not None
    path, mime = result
    assert path.name == "front.jpg"
    assert mime == "image/jpeg"


def test_discover_cover_art_returns_none_when_absent(tmp_path):
    from ingest import discover_cover_art
    result = discover_cover_art(tmp_path, [])
    assert result is None


def test_discover_cover_art_case_insensitive(tmp_path):
    from ingest import discover_cover_art
    (tmp_path / "Cover.JPG").write_bytes(b"fake")
    result = discover_cover_art(tmp_path, [])
    assert result is not None


# --- parse_discogs_url ---

def test_parse_discogs_url_from_cli():
    from ingest import parse_discogs_url
    from pathlib import Path
    url = "https://www.discogs.com/release/12345"
    assert parse_discogs_url(url, Path("/nonexistent")) == url


def test_parse_discogs_url_from_notes_md(tmp_path):
    from ingest import parse_discogs_url
    notes = tmp_path / "notes.md"
    notes.write_text("See https://www.discogs.com/release/98765-Artist-Album for details\n")
    result = parse_discogs_url(None, tmp_path)
    assert result == "https://www.discogs.com/release/98765-Artist-Album"


def test_parse_discogs_url_none_when_absent(tmp_path):
    from ingest import parse_discogs_url
    result = parse_discogs_url(None, tmp_path)
    assert result is None


def test_parse_discogs_url_cli_takes_priority(tmp_path):
    from ingest import parse_discogs_url
    notes = tmp_path / "notes.md"
    notes.write_text("https://www.discogs.com/release/11111\n")
    result = parse_discogs_url("https://www.discogs.com/release/99999", tmp_path)
    assert result == "https://www.discogs.com/release/99999"


# --- extract_discogs_release_id ---

def test_extract_discogs_release_id_standard():
    from ingest import extract_discogs_release_id
    assert extract_discogs_release_id("https://www.discogs.com/release/12345") == "12345"


def test_extract_discogs_release_id_with_slug():
    from ingest import extract_discogs_release_id
    assert extract_discogs_release_id(
        "https://www.discogs.com/release/12345-Artist-Album-Title"
    ) == "12345"


def test_extract_discogs_release_id_invalid():
    from ingest import extract_discogs_release_id
    assert extract_discogs_release_id("https://www.discogs.com/artist/67890") is None


# --- match_discogs_tracklist ---

def test_match_discogs_tracklist_vinyl_sides():
    from ingest import match_discogs_tracklist
    tracks = [
        {"position": "A1", "title": "Speak to Me", "type_": "track"},
        {"position": "A2", "title": "Breathe", "type_": "track"},
        {"position": "B1", "title": "Time", "type_": "track"},
        {"position": "B2", "title": "Money", "type_": "track"},
    ]
    mapping = match_discogs_tracklist(tracks)
    assert mapping[1] == ("A", "A1")
    assert mapping[2] == ("A", "A2")
    assert mapping[3] == ("B", "B1")
    assert mapping[4] == ("B", "B2")


def test_match_discogs_tracklist_numeric_positions():
    from ingest import match_discogs_tracklist
    tracks = [
        {"position": "1", "title": "Track One", "type_": "track"},
        {"position": "2", "title": "Track Two", "type_": "track"},
    ]
    mapping = match_discogs_tracklist(tracks)
    assert mapping[1] == (None, "1")
    assert mapping[2] == (None, "2")


def test_match_discogs_tracklist_skips_empty_position():
    from ingest import match_discogs_tracklist
    tracks = [
        {"position": "", "title": "Heading", "type_": "track"},
        {"position": "A1", "title": "Real Track", "type_": "track"},
    ]
    mapping = match_discogs_tracklist(tracks)
    assert 1 not in mapping
    assert 2 in mapping


# --- lookup_discogs_position ---

def test_lookup_discogs_position_by_track_number():
    from ingest import lookup_discogs_position
    mapping = {1: ("A", "A1"), 2: ("A", "A2"), 3: ("B", "B1")}
    meta = {"track_number": 2, "track": "Breathe"}
    side, pos = lookup_discogs_position(meta, 2, mapping)
    assert side == "A"
    assert pos == "A2"


def test_lookup_discogs_position_fallback_to_index():
    from ingest import lookup_discogs_position
    mapping = {1: ("A", "A1"), 2: ("A", "A2")}
    meta = {"track_number": None, "track": "Something"}
    side, pos = lookup_discogs_position(meta, 1, mapping)
    assert side == "A"
    assert pos == "A1"


def test_lookup_discogs_position_fallback_to_title_match():
    from ingest import lookup_discogs_position
    mapping = {3: ("B", "B1")}
    discogs_tracks = [
        {"position": "A1", "title": "Speak to Me"},
        {"position": "A2", "title": "Breathe"},
        {"position": "B1", "title": "Time"},
    ]
    meta = {"track_number": 99, "track": "Time"}
    side, pos = lookup_discogs_position(meta, 99, mapping, discogs_tracks)
    assert side == "B"
    assert pos == "B1"


def test_lookup_discogs_position_empty_mapping():
    from ingest import lookup_discogs_position
    meta = {"track_number": 1, "track": "Something"}
    side, pos = lookup_discogs_position(meta, 1, {})
    assert side is None
    assert pos is None


# --- dry run integration ---

def test_ingest_dry_run(tmp_path):
    _make_flac(tmp_path / "track1.flac", artist="A", album="B", title="C")
    _make_flac(tmp_path / "track2.flac", artist="A", album="B", title="D")
    result = subprocess.run(
        [sys.executable, "ingest.py", str(tmp_path), "--dry-run"],
        capture_output=True, text=True, cwd=str(Path(__file__).parent.parent)
    )
    assert result.returncode == 0
    assert "WOULD INGEST" in result.stdout
    assert "A - B" in result.stdout
