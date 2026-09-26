from __future__ import annotations

from pathlib import Path

import pytest

from garmin_data.config import Settings
from garmin_data.store import Store


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, tokenstore=tmp_path / "tokens", request_delay_s=0.0)


@pytest.fixture
def store(settings: Settings):
    with Store(settings.db_path) as s:
        yield s
