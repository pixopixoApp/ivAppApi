from __future__ import annotations

from typing import Any

from app.impressions import RedisImpressionStore


class _MemoryPipeline:
    def __init__(self, client: _MemoryRedis) -> None:
        self.client = client
        self.commands: list[tuple[str, tuple[Any, ...]]] = []

    def sadd(self, *args: Any) -> _MemoryPipeline:
        self.commands.append(("sadd", args))
        return self

    def lrem(self, *args: Any) -> _MemoryPipeline:
        self.commands.append(("lrem", args))
        return self

    def lpush(self, *args: Any) -> _MemoryPipeline:
        self.commands.append(("lpush", args))
        return self

    def ltrim(self, *args: Any) -> _MemoryPipeline:
        self.commands.append(("ltrim", args))
        return self

    def execute(self) -> list[Any]:
        return [getattr(self.client, name)(*args) for name, args in self.commands]


class _MemoryRedis:
    def __init__(self) -> None:
        self.sets: dict[str, set[str]] = {}
        self.lists: dict[str, list[str]] = {}

    def pipeline(self, *, transaction: bool) -> _MemoryPipeline:
        assert transaction is True
        return _MemoryPipeline(self)

    def sadd(self, key: str, value: str) -> int:
        values = self.sets.setdefault(key, set())
        before = len(values)
        values.add(value)
        return int(len(values) != before)

    def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    def lrem(self, key: str, count: int, value: str) -> int:
        assert count == 0
        values = self.lists.setdefault(key, [])
        before = len(values)
        self.lists[key] = [item for item in values if item != value]
        return before - len(self.lists[key])

    def lpush(self, key: str, value: str) -> int:
        values = self.lists.setdefault(key, [])
        values.insert(0, value)
        return len(values)

    def ltrim(self, key: str, start: int, stop: int) -> bool:
        self.lists[key] = self.lists.get(key, [])[start : stop + 1]
        return True

    def lrange(self, key: str, start: int, stop: int) -> list[str]:
        return list(self.lists.get(key, [])[start : stop + 1])

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            deleted += int(self.sets.pop(key, None) is not None)
            deleted += int(self.lists.pop(key, None) is not None)
        return deleted


def test_recent_impressions_are_unique_newest_first_and_bounded() -> None:
    store = RedisImpressionStore(_MemoryRedis())  # type: ignore[arg-type]

    for video_id in ("video-1", "video-2", "video-3", "video-4", "video-2"):
        store.mark_seen(user_id="viewer", video_id=video_id)

    assert store.list_seen_ids(user_id="viewer") == {
        "video-1",
        "video-2",
        "video-3",
        "video-4",
    }
    assert store.list_recent_ids(user_id="viewer") == [
        "video-2",
        "video-4",
        "video-3",
    ]


def test_cycle_reset_preserves_recent_queue_but_account_clear_removes_it() -> None:
    store = RedisImpressionStore(_MemoryRedis())  # type: ignore[arg-type]
    store.mark_seen(user_id="viewer", video_id="video-1")
    store.mark_seen(user_id="viewer", video_id="video-2")

    store.clear_cycle(user_id="viewer")

    assert store.list_seen_ids(user_id="viewer") == set()
    assert store.list_recent_ids(user_id="viewer") == ["video-2", "video-1"]

    store.clear_user(user_id="viewer")

    assert store.list_seen_ids(user_id="viewer") == set()
    assert store.list_recent_ids(user_id="viewer") == []
