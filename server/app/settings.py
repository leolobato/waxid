from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class Settings(BaseModel):
    roon_enabled: bool = False
    roon_url: str = ""
    roon_zone_name: str = "Record Player"
    server_url: str = "http://localhost:8457"
    lastfm_session_key: str = ""
    lastfm_username: str = ""
    lastfm_enabled: bool = False


def load_settings(data_dir: Path) -> Settings:
    path = data_dir / "settings.json"
    if not path.exists():
        return Settings()
    try:
        data = json.loads(path.read_text())
        return Settings(**data)
    except Exception as e:
        logger.warning("Failed to load settings from %s: %s", path, e)
        return Settings()


def save_settings(data_dir: Path, settings: Settings) -> None:
    path = data_dir / "settings.json"
    data = settings.model_dump()
    tmp_fd, tmp_path = tempfile.mkstemp(dir=data_dir, suffix=".json")
    try:
        with open(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        Path(tmp_path).replace(path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise
