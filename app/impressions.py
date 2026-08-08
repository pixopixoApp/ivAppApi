from __future__ import annotations

from typing import Protocol

import redis
from redis.exceptions import RedisError

from app.config import get_settings
from app.logging_config import get_logger

log = get_logger(__name__)


class ImpressionUnavailableError(Exception):
    """Redis impression store is unreachable."""


class ImpressionStore(Protocol):
    def mark_seen(self, *, user_id: str, video_id: str) -> None: ...

    def list_seen_ids(self, *, user_id: str) -> set[str]: ...


def impression_key(user_id: str) -> str:
    return f"impression:{user_id}"


class RedisImpressionStore:
    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    def mark_seen(self, *, user_id: str, video_id: str) -> None:
        try:
            self._client.sadd(impression_key(user_id), video_id)
        except RedisError as exc:
            log.warning("impression mark_seen failed user_id=%s err=%s", user_id, exc)
            raise ImpressionUnavailableError(str(exc)) from exc

    def list_seen_ids(self, *, user_id: str) -> set[str]:
        try:
            members = self._client.smembers(impression_key(user_id))
        except RedisError as exc:
            log.warning("impression list_seen failed user_id=%s err=%s", user_id, exc)
            raise ImpressionUnavailableError(str(exc)) from exc
        out: set[str] = set()
        for raw in members:
            if isinstance(raw, bytes):
                out.add(raw.decode("utf-8"))
            else:
                out.add(str(raw))
        return out


_store: RedisImpressionStore | None = None


def get_impression_store() -> RedisImpressionStore:
    global _store
    if _store is None:
        settings = get_settings()
        client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
        _store = RedisImpressionStore(client)
    return _store
