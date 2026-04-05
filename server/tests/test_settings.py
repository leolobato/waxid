import json
import pytest
from pathlib import Path
from app.settings import Settings, load_settings, save_settings


class TestSettingsModel:
    def test_defaults(self):
        s = Settings()
        assert s.roon_enabled is False
        assert s.roon_url == ""
        assert s.roon_zone_name == "Record Player"
        assert s.server_url == "http://localhost:8457"
        assert s.lastfm_session_key == ""
        assert s.lastfm_username == ""
        assert s.lastfm_enabled is False


class TestLoadSettings:
    def test_returns_defaults_when_file_missing(self, tmp_path):
        s = load_settings(tmp_path)
        assert s.roon_enabled is False
        assert s.roon_zone_name == "Record Player"

    def test_loads_from_file(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({
            "roon_enabled": True,
            "roon_url": "http://10.0.1.9:8377",
            "roon_zone_name": "Vinyl Turntable",
            "server_url": "http://10.0.1.9:8457",
        }))
        s = load_settings(tmp_path)
        assert s.roon_enabled is True
        assert s.roon_url == "http://10.0.1.9:8377"
        assert s.roon_zone_name == "Vinyl Turntable"

    def test_returns_defaults_on_corrupt_json(self, tmp_path):
        (tmp_path / "settings.json").write_text("{bad json")
        s = load_settings(tmp_path)
        assert s.roon_enabled is False

    def test_returns_defaults_on_invalid_fields(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({
            "roon_enabled": "not_a_bool",
        }))
        s = load_settings(tmp_path)
        assert s.roon_enabled is False


class TestSaveSettings:
    def test_round_trip(self, tmp_path):
        original = Settings(
            roon_enabled=True,
            roon_url="http://10.0.1.9:8377",
            roon_zone_name="My Zone",
            server_url="http://10.0.1.9:8457",
        )
        save_settings(tmp_path, original)
        loaded = load_settings(tmp_path)
        assert loaded == original

    def test_file_is_valid_json(self, tmp_path):
        save_settings(tmp_path, Settings())
        data = json.loads((tmp_path / "settings.json").read_text())
        assert "roon_enabled" in data

    def test_lastfm_round_trip(self, tmp_path):
        original = Settings(
            lastfm_enabled=True,
            lastfm_session_key="abc123session",
            lastfm_username="testuser",
        )
        save_settings(tmp_path, original)
        loaded = load_settings(tmp_path)
        assert loaded.lastfm_enabled is True
        assert loaded.lastfm_session_key == "abc123session"
        assert loaded.lastfm_username == "testuser"
