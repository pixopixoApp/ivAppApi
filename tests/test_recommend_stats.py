from __future__ import annotations

import pytest

from app.models import RecommendStat
from app.routers.feed import _record_recommend_stats


class FakeRecommendStore:
    """最小内存版 RecommendStore：只实现统计去重所需接口。"""

    def __init__(self) -> None:
        self.counted: dict[str, set[str]] = {}

    def filter_counted(self, *, counter_key: str, video_ids: list[str]) -> list[str]:
        seen = self.counted.setdefault(counter_key, set())
        out: list[str] = []
        for vid in video_ids:
            if vid not in seen and vid not in out:
                out.append(vid)
        return out

    def mark_counted(self, *, counter_key: str, video_ids: list[str], ttl_seconds=None) -> None:
        self.counted.setdefault(counter_key, set()).update(video_ids)


@pytest.fixture
def fake_store(monkeypatch):
    store = FakeRecommendStore()
    monkeypatch.setattr("app.routers.feed.get_recommend_store", lambda: store)
    return store


def _rows(db) -> dict[str, RecommendStat]:
    return {r.video_id: r for r in db.query(RecommendStat).all()}


def test_counts_accumulate_and_dedupe(db, fake_store) -> None:
    # 同一会话：一次调用内按 video_id 去重；再次下发新增视频才累计。
    _record_recommend_stats(
        db, video_ids=["v1", "v2", "v1"], counter_key="c1", counter_ttl_seconds=60
    )
    rows = _rows(db)
    assert rows["v1"].count == 1
    assert rows["v2"].count == 1
    assert rows["v1"].first_recommended_at is not None
    assert rows["v1"].last_recommended_at is not None

    _record_recommend_stats(
        db, video_ids=["v1", "v3"], counter_key="c1", counter_ttl_seconds=60
    )
    rows = _rows(db)
    assert rows["v1"].count == 1  # 策略 B：同会话 v1 已计过，不再 +1
    assert rows["v2"].count == 1
    assert rows["v3"].count == 1


def test_empty_is_noop(db, fake_store) -> None:
    _record_recommend_stats(db, video_ids=[], counter_key="c1", counter_ttl_seconds=60)
    assert db.query(RecommendStat).count() == 0


def test_last_recommended_at_advances(db, fake_store) -> None:
    # 不同会话对同一视频各计一次 -> count 累加、last 前进
    _record_recommend_stats(db, video_ids=["v1"], counter_key="c1", counter_ttl_seconds=60)
    first = db.query(RecommendStat).filter_by(video_id="v1").one().last_recommended_at
    _record_recommend_stats(db, video_ids=["v1"], counter_key="c2", counter_ttl_seconds=60)
    second = db.query(RecommendStat).filter_by(video_id="v1").one().last_recommended_at
    assert second >= first
    assert db.query(RecommendStat).filter_by(video_id="v1").one().count == 2


def test_no_identity_is_not_counted(db, fake_store) -> None:
    """策略 A：无可信会话身份（counter_key=None）时不计入统计。"""
    _record_recommend_stats(db, video_ids=["v1", "v2"], counter_key=None)
    assert db.query(RecommendStat).count() == 0


def test_same_session_repeat_does_not_inflate(db, fake_store) -> None:
    """策略 B：同一会话反复请求同一批视频，计数不再增长（防压测刷量）。"""
    for _ in range(5):
        _record_recommend_stats(
            db, video_ids=["v1", "v2", "v3"], counter_key="same-sess", counter_ttl_seconds=60
        )
    rows = _rows(db)
    assert rows["v1"].count == 1
    assert rows["v2"].count == 1
    assert rows["v3"].count == 1


def test_distinct_sessions_accumulate(db, fake_store) -> None:
    """不同会话各自贡献 1 次，累计 == 会话数。"""
    for i in range(10):
        _record_recommend_stats(
            db, video_ids=["v1"], counter_key=f"sess-{i}", counter_ttl_seconds=60
        )
    assert db.query(RecommendStat).filter_by(video_id="v1").one().count == 10
