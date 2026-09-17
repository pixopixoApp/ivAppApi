from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.models import PublishedVideo, User, UserToken
from app.protocol_video import compile_runtime_spec
from app.routers import feed as feed_router


class _FakeSeen:
    """Records user:seen writes."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []

    def mark_seen(self, *, seen_key: str, video_id: str, ttl_seconds=None) -> None:
        self.writes.append((seen_key, video_id))


class _FakeImpressions:
    def __init__(self) -> None:
        self.seen: dict[str, set[str]] = {}

    def mark_seen(self, *, user_id: str, video_id: str) -> None:
        self.seen.setdefault(user_id, set()).add(video_id)

    def list_seen_ids(self, *, user_id: str) -> set[str]:
        return set(self.seen.get(user_id, set()))

    def list_recent_ids(self, *, user_id: str) -> list[str]:
        return []

    def clear_cycle(self, *, user_id: str) -> None:
        self.seen.pop(user_id, None)

    def clear_user(self, *, user_id: str) -> None:
        self.seen.pop(user_id, None)


def _user(db, user_id: str) -> str:
    token = f"token-{user_id}"
    now = datetime.now(timezone.utc)
    db.add(User(user_id=user_id, provider="email", subject=f"{user_id}@example.com", enabled=True))
    from datetime import timedelta
    db.add(UserToken(token=token, user_id=user_id, created_at=now, expires_at=now + timedelta(days=1)))
    db.commit()
    return token


def _published(db, video_id: str, user_id: str) -> None:
    timeline = {"interactions": [{"gesture": "tap", "gate_at_ms": 1000}]}
    spec = compile_runtime_spec(
        item_id=video_id, content_mode="single", source=timeline,
        video_url=f"/media/{video_id}.mp4",
    )
    now = datetime.now(timezone.utc)
    db.add(PublishedVideo(
        id=video_id, video_url=f"/media/{video_id}.mp4", timeline=timeline,
        runtime_spec=spec, runtime_spec_version=spec["version"], version="1",
        user_id=user_id, content_mode="single", created_at=now, updated_at=now,
    ))


def test_impression_writes_user_seen(db, monkeypatch) -> None:
    token = _user(db, "imp-user")
    _published(db, "imp-video-1", "imp-user")
    db.commit()

    fake_seen = _FakeSeen()
    fake_imp = _FakeImpressions()
    monkeypatch.setattr(feed_router, "get_recommend_store", lambda: fake_seen)
    monkeypatch.setattr(feed_router, "get_impression_store", lambda: fake_imp)

    with TestClient(app) as client:
        resp = client.post(
            "/impression",
            headers={"Authorization": f"Bearer {token}"},
            json={"head": {"act": "impression", "ver": "1.2"}, "body": {"video_id": "imp-video-1"}},
        ).json()

    assert resp["head"]["status"] == 0
    # 同时写入推荐去重池 user:seen:{user_id}
    assert ("user:seen:imp-user", "imp-video-1") in fake_seen.writes
    # 也写入播放周期池
    assert "imp-video-1" in fake_imp.seen["imp-user"]


def test_seen_still_writes_user_seen(db, monkeypatch) -> None:
    token = _user(db, "seen-user")
    _published(db, "seen-video-1", "seen-user")
    db.commit()

    fake_seen = _FakeSeen()
    monkeypatch.setattr(feed_router, "get_recommend_store", lambda: fake_seen)

    with TestClient(app) as client:
        resp = client.post(
            "/seen",
            headers={"Authorization": f"Bearer {token}"},
            json={"head": {"act": "seen", "ver": "1.2"}, "body": {"video_id": "seen-video-1"}},
        ).json()

    assert resp["head"]["status"] == 0
    assert ("user:seen:seen-user", "seen-video-1") in fake_seen.writes


def test_video_exposure_marking_is_gated_by_switch() -> None:
    import inspect
    src = inspect.getsource(feed_router.post_video)
    # 曝光即标记必须受 feature_seen_client_report 控制
    assert "not settings.feature_seen_client_report" in src
