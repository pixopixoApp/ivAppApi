from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401
    from sqlalchemy import inspect

    Base.metadata.create_all(bind=engine)

    # Lightweight alter for already-created tables (no Alembic yet).
    insp = inspect(engine)
    if insp.has_table("published_videos"):
        cols = {c["name"] for c in insp.get_columns("published_videos")}
        if "version" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE published_videos "
                        "ADD COLUMN version VARCHAR(64) NOT NULL DEFAULT ''"
                    )
                )
        if "user_id" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE published_videos "
                        "ADD COLUMN user_id VARCHAR(64) NULL"
                    )
                )
            insp = inspect(engine)
        if "content_mode" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE published_videos "
                        "ADD COLUMN content_mode VARCHAR(16) NOT NULL DEFAULT 'single'"
                    )
                )
            insp = inspect(engine)
            cols = {c["name"] for c in insp.get_columns("published_videos")}
        if "feed_weight" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE published_videos "
                        "ADD COLUMN feed_weight INT NOT NULL DEFAULT 0"
                    )
                )
            insp = inspect(engine)
            cols = {c["name"] for c in insp.get_columns("published_videos")}
        if "is_tutorial" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE published_videos "
                        "ADD COLUMN is_tutorial TINYINT(1) NOT NULL DEFAULT 0"
                    )
                )
            insp = inspect(engine)
        indexes = {idx["name"] for idx in insp.get_indexes("published_videos")}
        if "ix_published_videos_user_id" not in indexes:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE INDEX ix_published_videos_user_id "
                        "ON published_videos (user_id)"
                    )
                )
    if insp.has_table("users"):
        cols = {c["name"] for c in insp.get_columns("users")}
        if "enabled" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE users "
                        "ADD COLUMN enabled TINYINT(1) NOT NULL DEFAULT 1"
                    )
                )
        if "nickname" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE users "
                        "ADD COLUMN nickname VARCHAR(64) NOT NULL DEFAULT ''"
                    )
                )
        if "avatar_url" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE users "
                        "ADD COLUMN avatar_url VARCHAR(512) NOT NULL DEFAULT ''"
                    )
                )
        if "source" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE users "
                        "ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'app'"
                    )
                )
            insp = inspect(engine)
        if "birthday" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE users "
                        "ADD COLUMN birthday VARCHAR(10) NOT NULL DEFAULT ''"
                    )
                )
            insp = inspect(engine)
            cols = {c["name"] for c in insp.get_columns("users")}
        if "deletion_requested_at" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE users "
                        "ADD COLUMN deletion_requested_at DATETIME(6) NULL"
                    )
                )
            insp = inspect(engine)
            cols = {c["name"] for c in insp.get_columns("users")}
        if "scheduled_delete_at" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE users "
                        "ADD COLUMN scheduled_delete_at DATETIME(6) NULL"
                    )
                )
            insp = inspect(engine)
        indexes = {idx["name"] for idx in insp.get_indexes("users")}
        if "ix_users_source" not in indexes:
            with engine.begin() as conn:
                conn.execute(text("CREATE INDEX ix_users_source ON users (source)"))
    if insp.has_table("analytics_logs"):
        cols = {c["name"] for c in insp.get_columns("analytics_logs")}
        if "video_id" not in cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE analytics_logs "
                        "ADD COLUMN video_id VARCHAR(128) NOT NULL DEFAULT ''"
                    )
                )
            insp = inspect(engine)
        indexes = {idx["name"] for idx in insp.get_indexes("analytics_logs")}
        if "ix_analytics_logs_video_id" not in indexes:
            with engine.begin() as conn:
                conn.execute(
                    text("CREATE INDEX ix_analytics_logs_video_id ON analytics_logs (video_id)")
                )
    _migrate_user_tokens_to_user_id(insp)


def _migrate_user_tokens_to_user_id(insp) -> None:
    """Old user_tokens had provider/subject (or email); move to users + user_id."""
    from sqlalchemy import inspect as sa_inspect

    if not insp.has_table("user_tokens"):
        return
    cols = {c["name"] for c in insp.get_columns("user_tokens")}

    # email -> provider/subject (older shape)
    if "email" in cols and "subject" not in cols:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE user_tokens "
                    "ADD COLUMN provider VARCHAR(32) NOT NULL DEFAULT 'email'"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE user_tokens "
                    "ADD COLUMN subject VARCHAR(256) NOT NULL DEFAULT ''"
                )
            )
            conn.execute(text("UPDATE user_tokens SET subject = email WHERE subject = ''"))
            conn.execute(text("ALTER TABLE user_tokens DROP COLUMN email"))
        insp = sa_inspect(engine)
        cols = {c["name"] for c in insp.get_columns("user_tokens")}

    if "user_id" in cols and "provider" not in cols and "subject" not in cols:
        return

    if "user_id" not in cols:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE user_tokens ADD COLUMN user_id VARCHAR(64) NULL")
            )
        insp = sa_inspect(engine)
        cols = {c["name"] for c in insp.get_columns("user_tokens")}

    if "provider" in cols and "subject" in cols:
        import secrets

        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT DISTINCT provider, subject FROM user_tokens "
                    "WHERE subject IS NOT NULL AND subject != ''"
                )
            ).fetchall()
            for provider, subject in rows:
                existing = conn.execute(
                    text(
                        "SELECT user_id FROM users "
                        "WHERE provider = :p AND subject = :s LIMIT 1"
                    ),
                    {"p": provider, "s": subject},
                ).fetchone()
                if existing:
                    user_id = existing[0]
                else:
                    user_id = secrets.token_urlsafe(16)
                    conn.execute(
                        text(
                            "INSERT INTO users (user_id, provider, subject, created_at) "
                            "VALUES (:uid, :p, :s, UTC_TIMESTAMP(6))"
                        ),
                        {"uid": user_id, "p": provider, "s": subject},
                    )
                conn.execute(
                    text(
                        "UPDATE user_tokens SET user_id = :uid "
                        "WHERE provider = :p AND subject = :s "
                        "AND (user_id IS NULL OR user_id = '')"
                    ),
                    {"uid": user_id, "p": provider, "s": subject},
                )
            # Drop orphan tokens that never got a user_id
            conn.execute(text("DELETE FROM user_tokens WHERE user_id IS NULL OR user_id = ''"))
            conn.execute(text("ALTER TABLE user_tokens DROP COLUMN provider"))
            conn.execute(text("ALTER TABLE user_tokens DROP COLUMN subject"))
            # MySQL: ensure NOT NULL
            conn.execute(
                text(
                    "ALTER TABLE user_tokens "
                    "MODIFY COLUMN user_id VARCHAR(64) NOT NULL"
                )
            )

    insp = sa_inspect(engine)
    if insp.has_table("user_tokens"):
        indexes = {idx["name"] for idx in insp.get_indexes("user_tokens")}
        if "ix_user_tokens_user_id" not in indexes:
            with engine.begin() as conn:
                conn.execute(
                    text("CREATE INDEX ix_user_tokens_user_id ON user_tokens (user_id)")
                )
