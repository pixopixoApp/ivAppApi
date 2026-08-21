from __future__ import annotations

from typing import Protocol

import redis
from redis.exceptions import RedisError

from app.config import get_settings
from app.logging_config import get_logger

log = get_logger(__name__)

_RECENT_IMPRESSION_LIMIT = 3


class ImpressionUnavailableError(Exception):
    """Redis impression store is unreachable."""


class ImpressionStore(Protocol):
    def mark_seen(self, *, user_id: str, video_id: str) -> None: ...

    def list_seen_ids(self, *, user_id: str) -> set[str]: ...

    def list_recent_ids(self, *, user_id: str) -> list[str]: ...

    def clear_cycle(self, *, user_id: str) -> None: ...

    def clear_user(self, *, user_id: str) -> None: ...


def impression_key(user_id: str) -> str:
    return f"impression:{user_id}"


def recent_impression_key(user_id: str) -> str:
    return f"impression:recent:{user_id}"


def _decode_member(raw: bytes | str) -> str:
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


class RedisImpressionStore:
    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    def mark_seen(self, *, user_id: str, video_id: str) -> None:
        try:
            recent_key = recent_impression_key(user_id)
            pipe = self._client.pipeline(transaction=True)
            pipe.sadd(impression_key(user_id), video_id)
            pipe.lrem(recent_key, 0, video_id)
            pipe.lpush(recent_key, video_id)
            pipe.ltrim(recent_key, 0, _RECENT_IMPRESSION_LIMIT - 1)
            pipe.execute()
        except RedisError as exc:
            log.warning("impression mark_seen failed user_id=%s err=%s", user_id, exc)
            raise ImpressionUnavailableError(str(exc)) from exc

    def list_seen_ids(self, *, user_id: str) -> set[str]:
        try:
            members = self._client.smembers(impression_key(user_id))
        except RedisError as exc:
            log.warning("impression list_seen failed user_id=%s err=%s", user_id, exc)
            raise ImpressionUnavailableError(str(exc)) from exc
        return {_decode_member(raw) for raw in members}

    def list_recent_ids(self, *, user_id: str) -> list[str]:
        try:
            members = self._client.lrange(
                recent_impression_key(user_id),
                0,
                _RECENT_IMPRESSION_LIMIT - 1,
            )
        except RedisError as exc:
            log.warning("impression list_recent failed user_id=%s err=%s", user_id, exc)
            raise ImpressionUnavailableError(str(exc)) from exc
        return [_decode_member(raw) for raw in members]

    def clear_cycle(self, *, user_id: str) -> None:
        try:
            self._client.delete(impression_key(user_id))
        except RedisError as exc:
            log.warning("impression clear_cycle failed user_id=%s err=%s", user_id, exc)
            raise ImpressionUnavailableError(str(exc)) from exc

    def clear_user(self, *, user_id: str) -> None:
        try:
            self._client.delete(
                impression_key(user_id),
                recent_impression_key(user_id),
            )
        except RedisError as exc:
            log.warning("impression clear failed user_id=%s err=%s", user_id, exc)
            raise ImpressionUnavailableError(str(exc)) from exc


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
