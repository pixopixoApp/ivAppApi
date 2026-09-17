from __future__ import annotations

from app.models import RecommendStat
from app.routers.feed import _record_recommend_stats


def test_counts_accumulate_and_dedupe(db) -> None:
    _record_recommend_stats(db, video_ids=["v1", "v2", "v1"])
    rows = {r.video_id: r for r in db.query(RecommendStat).all()}
    assert rows["v1"].count == 1  # within one call, deduped by video_id
    assert rows["v2"].count == 1
    assert rows["v1"].first_recommended_at is not None
    assert rows["v1"].last_recommended_at is not None

    _record_recommend_stats(db, video_ids=["v1", "v3"])
    rows = {r.video_id: r for r in db.query(RecommendStat).all()}
    assert rows["v1"].count == 2
    assert rows["v2"].count == 1
    assert rows["v3"].count == 1


def test_empty_is_noop(db) -> None:
    _record_recommend_stats(db, video_ids=[])
    assert db.query(RecommendStat).count() == 0


def test_last_recommended_at_advances(db) -> None:
    _record_recommend_stats(db, video_ids=["v1"])
    first = db.query(RecommendStat).filter_by(video_id="v1").one().last_recommended_at
    _record_recommend_stats(db, video_ids=["v1"])
    second = db.query(RecommendStat).filter_by(video_id="v1").one().last_recommended_at
    assert second >= first
