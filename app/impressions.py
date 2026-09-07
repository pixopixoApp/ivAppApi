from __future__ import annotations

from typing import Protocol

import redis
from redis.exceptions import RedisError

from app.config import get_settings
from app.logging_config import get_logger

log = get_logger(__name__)

_RECENT_IMPRESSION_LIMIT = 3

# ---------------------------------------------------------------------------
# Redis 推荐内容池
# ---------------------------------------------------------------------------
# 内容池（每档一个 Set）：
#   content:level:{1..5}        全量（含新内容）
#   content:level:{1..5}:new    新内容（created_at 在窗口内）
#   content:level:{1..5}:_shadow         影子（30分钟重建用，全量）
#   content:level:{1..5}:_shadow:new     影子（重建用，新内容）
# 用户已看（ZSet）：member=videoId, score=播放时间戳
#   登录：user:seen:{userId}         （7 天窗口 + TTL）
#   游客：user:seen:guest:{ssid}     （短 TTL 防膨胀）


def content_pool_key(level: int) -> str:
    return f"content:level:{level}"


def content_pool_new_key(level: int) -> str:
    return f"content:level:{level}:new"


def content_pool_shadow_key(level: int) -> str:
    return f"content:level:{level}:_shadow"


def content_pool_new_shadow_key(level: int) -> str:
    return f"content:level:{level}:_shadow:new"


def user_seen_key(user_id: str, *, is_guest: bool = False) -> str:
    if is_guest:
        return f"user:seen:guest:{user_id}"
    return f"user:seen:{user_id}"


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


class RedisRecommendStore:
    """推荐内容池 + 用户已看（ZSet）专用存储。"""

    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    # ---- 用户已看（ZSet）----
    def clean_expired_seen(self, *, seen_key: str, expire_ts: int) -> None:
        """剔除 score <= expire_ts 的过期记录。"""
        try:
            self._client.zremrangebyscore(seen_key, 0, expire_ts)
        except RedisError as exc:
            log.warning("recommend clean_seen failed key=%s err=%s", seen_key, exc)
            raise ImpressionUnavailableError(str(exc)) from exc

    def is_seen(self, *, seen_key: str, video_ids: list[str]) -> list[bool]:
        """批量判断 video_ids 是否已看过（ZSCORE != None）。返回等长 bool 列表。"""
        try:
            pipe = self._client.pipeline(transaction=False)
            for vid in video_ids:
                pipe.zscore(seen_key, vid)
            res = pipe.execute()
            return [score is not None for score in res]
        except RedisError as exc:
            log.warning("recommend is_seen failed key=%s err=%s", seen_key, exc)
            raise ImpressionUnavailableError(str(exc)) from exc

    def mark_seen(self, *, seen_key: str, video_id: str, ttl_seconds: int | None = None) -> None:
        """写入已看（score=当前时间戳）；可选设置 TTL 自动过期防膨胀。"""
        try:
            now = int(__import__("time").time())
            pipe = self._client.pipeline(transaction=True)
            pipe.zadd(seen_key, {video_id: now})
            if ttl_seconds is not None:
                pipe.expire(seen_key, ttl_seconds)
            pipe.execute()
        except RedisError as exc:
            log.warning("recommend mark_seen failed key=%s err=%s", seen_key, exc)
            raise ImpressionUnavailableError(str(exc)) from exc

    # ---- 内容池采样 ----
    def srandmember(self, *, key: str, count: int) -> list[str]:
        try:
            members = self._client.srandmember(key, count)
        except RedisError as exc:
            log.warning("recommend srandmember failed key=%s err=%s", key, exc)
            raise ImpressionUnavailableError(str(exc)) from exc
        return [str(m) for m in members] if members else []

    # ---- 内容池影子重建（30 分钟全量更新）----
    def shadow_rebuild(self, *, level: int, all_ids: list[str], new_ids: list[str], batch: int = 500) -> None:
        """重建指定档位的 shadow keys（全量 + 新内容）。"""
        all_key = content_pool_shadow_key(level)
        new_key = content_pool_new_shadow_key(level)
        try:
            pipe = self._client.pipeline(transaction=True)
            pipe.delete(all_key, new_key)
            pipe.execute()
            for i in range(0, len(all_ids), batch):
                self._client.sadd(all_key, *all_ids[i : i + batch])
            for i in range(0, len(new_ids), batch):
                self._client.sadd(new_key, *new_ids[i : i + batch])
        except RedisError as exc:
            log.warning(
                "recommend shadow_rebuild failed level=%s all=%d new=%d err=%s",
                level, len(all_ids), len(new_ids), exc,
            )
            raise ImpressionUnavailableError(str(exc)) from exc

    def shadow_validate(self, *, level: int) -> bool:
        """校验 shadow key 非空才允许切换，防止空窗事故。"""
        try:
            all_count = self._client.scard(content_pool_shadow_key(level))
            # 允许“新内容”为空（新视频不足时），但“全量”必须非空
            return bool(all_count)
        except RedisError as exc:
            log.warning("recommend shadow_validate failed level=%s err=%s", level, exc)
            raise ImpressionUnavailableError(str(exc)) from exc

    def shadow_promote(self, *, level: int) -> None:
        """原子切换：RENAME shadow → 主 key（全量 + 新内容）。

        当 new_shadow 不存在（该档无新视频）时，直接清空旧的 new_key，
        避免残留上一轮的新内容；全量池始终非空才走到这里。
        """
        try:
            all_key = content_pool_key(level)
            new_key = content_pool_new_key(level)
            all_shadow = content_pool_shadow_key(level)
            new_shadow = content_pool_new_shadow_key(level)
            pipe = self._client.pipeline(transaction=True)
            pipe.rename(all_shadow, all_key)
            if self._client.exists(new_shadow):
                pipe.rename(new_shadow, new_key)
            else:
                pipe.delete(new_key)
            pipe.execute()
        except RedisError as exc:
            log.warning("recommend shadow_promote failed level=%s err=%s", level, exc)
            raise ImpressionUnavailableError(str(exc)) from exc


_recommend_store: RedisRecommendStore | None = None


def get_recommend_store() -> RedisRecommendStore:
    global _recommend_store
    if _recommend_store is None:
        settings = get_settings()
        client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
        _recommend_store = RedisRecommendStore(client)
    return _recommend_store
