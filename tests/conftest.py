from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/ivapp-pytest.db")
os.environ.setdefault("PUBLISH_KEY", "test-publish-key")
os.environ.setdefault("CURSOR_SECRET", "test-cursor-secret")
os.environ.setdefault("MEDIA_ROOT", "/tmp/ivapp-pytest-media")
# The developer .env intentionally runs against OSS. Unit tests must remain
# hermetic unless an individual test explicitly opts into the OSS adapter.
os.environ["MEDIA_STORAGE_MODE"] = "local"
os.environ["MEDIA_READ_FALLBACK_LOCAL"] = "true"
os.environ.setdefault("SMTP_HOST", "")
os.environ.setdefault("HTML_TRUSTED_ORIGINS", "https://html.test")

from app import models  # noqa: F401
from app.config import get_settings
from app.db import Base, SessionLocal, engine


@pytest.fixture(autouse=True)
def clean_database() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    get_settings.cache_clear()
    media = Path("/tmp/ivapp-pytest-media")
    shutil.rmtree(media, ignore_errors=True)
    media.mkdir(parents=True, exist_ok=True)
    yield
    Base.metadata.drop_all(bind=engine)
    shutil.rmtree(media, ignore_errors=True)


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session
