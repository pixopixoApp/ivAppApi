from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def test_existing_runtime_rows_survive_html_content_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE alembic_version (
            version_num VARCHAR(32) NOT NULL PRIMARY KEY
        );
        INSERT INTO alembic_version VALUES ('20260810_0005');
        CREATE TABLE published_videos (
            id VARCHAR(128) NOT NULL PRIMARY KEY,
            video_url TEXT NOT NULL,
            timeline JSON NOT NULL,
            runtime_spec JSON NOT NULL,
            runtime_spec_version VARCHAR(32) NOT NULL,
            version VARCHAR(64) NOT NULL,
            title VARCHAR(120) NOT NULL,
            description TEXT NOT NULL,
            user_id VARCHAR(64),
            content_mode VARCHAR(16) NOT NULL,
            feed_weight INTEGER NOT NULL,
            is_tutorial BOOLEAN NOT NULL,
            deleted_at DATETIME,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        INSERT INTO published_videos VALUES (
            'legacy-runtime', '/media/legacy.mp4', '{}', '{}', '1.0', 'v1',
            'Legacy', '', NULL, 'single', 0, 0, NULL, '2026-08-01', '2026-08-01'
        );
        """
    )
    connection.commit()
    connection.close()

    repository_root = Path(__file__).resolve().parents[1]
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(repository_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    runtime = connection.execute(
        "SELECT * FROM published_videos WHERE id = 'legacy-runtime'"
    ).fetchone()
    assert runtime is not None
    assert runtime["content_type"] == "runtime"
    assert runtime["video_url"] == "/media/legacy.mp4"
    assert runtime["runtime_spec_version"] == "1.0"
    assert runtime["html_url"] is None
    assert runtime["bridge_version"] is None
    assert runtime["required_capabilities"] == "[]"

    connection.execute(
        """
        INSERT INTO published_videos (
            id, content_type, video_url, timeline, runtime_spec,
            runtime_spec_version, html_url, bridge_version,
            required_capabilities, version, title, description, user_id,
            content_mode, feed_weight, is_tutorial, deleted_at, created_at, updated_at
        ) VALUES (
            'html-new', 'html', NULL, NULL, NULL, NULL,
            'https://html.test/pixo/html/html-new/version/index.html', 1,
            '[]', 'version', 'HTML', '', NULL, 'single', 0, 0, NULL,
            '2026-08-10', '2026-08-10'
        )
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO published_videos (
                id, content_type, video_url, timeline, runtime_spec,
                runtime_spec_version, html_url, bridge_version,
                required_capabilities, version, title, description, user_id,
                content_mode, feed_weight, is_tutorial, deleted_at, created_at, updated_at
            ) VALUES (
                'invalid-hybrid', 'html', '/media/not-allowed.mp4', NULL, NULL, NULL,
                'https://html.test/pixo/html/invalid/version/index.html', 1,
                '[]', 'version', 'Invalid', '', NULL, 'single', 0, 0, NULL,
                '2026-08-10', '2026-08-10'
            )
            """
        )
    connection.close()
