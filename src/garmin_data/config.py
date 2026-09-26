"""Configuration loading.

Precedence (highest first): environment variables, TOML config file, defaults.

Config file lookup order:
1. ``$GARMIN_DATA_CONFIG`` (explicit path)
2. ``./config.toml`` (current working directory)
3. ``~/.config/garmin-data/config.toml``
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = Path("~/.local/share/garmin-data")
DEFAULT_TOKENSTORE = Path("~/.garminconnect")


@dataclass(frozen=True)
class Settings:
    """Runtime settings. All paths are expanded (``~`` resolved)."""

    data_dir: Path
    tokenstore: Path
    request_delay_s: float = 0.5
    incremental_overlap_days: int = 3
    initial_sync_days: int = 30
    max_fetch_attempts: int = 3
    is_cn: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "garmin.duckdb"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"


def _config_file() -> Path | None:
    explicit = os.getenv("GARMIN_DATA_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    for candidate in (Path("config.toml"), Path("~/.config/garmin-data/config.toml").expanduser()):
        if candidate.is_file():
            return candidate
    return None


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from file + environment."""
    path = config_path or _config_file()
    raw: dict = {}
    if path is not None and path.is_file():
        with path.open("rb") as f:
            raw = tomllib.load(f)

    storage = raw.get("storage", {})
    auth = raw.get("auth", {})
    sync = raw.get("sync", {})

    data_dir = os.getenv("GARMIN_DATA_DIR") or storage.get("data_dir") or DEFAULT_DATA_DIR
    tokenstore = os.getenv("GARMINTOKENS") or auth.get("tokenstore") or DEFAULT_TOKENSTORE

    return Settings(
        data_dir=Path(data_dir).expanduser(),
        tokenstore=Path(tokenstore).expanduser(),
        request_delay_s=float(sync.get("request_delay_s", 0.5)),
        incremental_overlap_days=int(sync.get("incremental_overlap_days", 3)),
        initial_sync_days=int(sync.get("initial_sync_days", 30)),
        max_fetch_attempts=int(sync.get("max_fetch_attempts", 3)),
        is_cn=bool(auth.get("is_cn", False)),
    )
