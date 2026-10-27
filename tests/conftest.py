from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from applier.config import (
    LLMConfig,
    PathConfig,
    RuntimeConfig,
    Settings,
    SourceConfig,
    load_profile,
)
from applier.db import create_db_engine, init_db


@pytest.fixture
def profile():
    return load_profile(Path("config/profile.yaml"))


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    value = create_db_engine(tmp_path / "applier.db")
    init_db(value)
    return value


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        source=SourceConfig(url="https://example.test/listings.json"),
        runtime=RuntimeConfig(
            headless=True,
            min_delay_seconds=0,
            max_delay_seconds=0,
            max_retries=0,
            navigation_timeout_ms=5_000,
        ),
        llm=LLMConfig(provider="ollama", model="llama3.2"),
        paths=PathConfig(
            database=tmp_path / "applier.db",
            resumes=tmp_path / "resumes",
            screenshots=tmp_path / "screenshots",
            traces=tmp_path / "traces",
            gmail_token=tmp_path / "gmail_token.json",
            gmail_credentials=tmp_path / "credentials.json",
        ),
    )
