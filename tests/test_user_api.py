from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.models import EmailCode, Follow, PublishedVideo, User, UserToken, VideoView
from app.protocol_video import RUNTIME_SPEC_VERSION, compile_runtime_spec
from app.routers import feed as feed_router


def _user(db, user_id: str, *, enabled: bool = True, birthday: str = "") -> str:
    token = f"token-{user_id}"
    now = datetime.now(timezone.utc)
    db.add(
        User(
            user_id=user_id,
            provider="email",
            subject=f"{user_id}@example.com",
            enabled=enabled,
            birthday=birthday,
        )
    )
    if enabled:
        db.add(
            UserToken(
                token=token,
                user_id=user_id,
                created_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
    db.commit()
    return token


def _legacy_json(act: str, token: str, body: dict) -> dict:
    return {"head": {"act": act, "ver": "1.2", "token": token}, "body": body}


def test_profile_bio_and_under_13_birthday_are_persisted(db) -> None:
    token = _user(db, "kid")
    with TestClient(app) as client:
        updated = client.post(
            "/profile_update",
            json=_legacy_json("profile_update", token, {"bio": "Hello"}),
        ).json()
        birthday = client.post(
            "/birthday",
            json=_legacy_json("birthday", token, {"birthday": "2020-01-01"}),
        ).json()
    assert updated["body"]["bio"] == "Hello"
    assert birthday["head"]["status"] == 0
    assert birthday["body"] == {
        "birthday": "2020-01-01",
        "needs_birthday": False,
        "passed": False,
    }
    db.expire_all()
    assert db.get(User, "kid").birthday == "2020-01-01"


def test_login_and_deactivation_codes_have_independent_limits(db) -> None:
    token = _user(db, "member")
    with TestClient(app) as client:
        login = client.post(
            "/send_code",
            json={
                "head": {"act": "send_code", "ver": "1.2"},
                "body": {"email": "member@example.com"},
            },
        ).json()
        deactivate = client.post(
            "/deactivate/send_code",
            json=_legacy_json("deactivate_send_code", token, {}),
        ).json()
        limited = client.post(
            "/send_code",
            json={
                "head": {"act": "send_code", "ver": "1.2"},
                "body": {"email": "member@example.com"},
            },
        ).json()
    assert login["head"]["status"] == 0
    assert deactivate["head"]["status"] == 0
    assert limited["head"]["error_code"] == "CODE_RATE_LIMITED"
    assert limited["head"]["retry_after_seconds"] > 0
    purposes = {
        row.purpose for row in db.query(EmailCode).filter(EmailCode.email == "member@example.com")
    }
    assert purposes == {"login", "deactivate"}


def test_follow_rejects_disabled_target(db) -> None:
    token = _user(db, "viewer")
    _user(db, "disabled", enabled=False)
    with TestClient(app) as client:
        result = client.post(
            "/follow",
            json=_legacy_json("follow", token, {"user_id": "disabled"}),
        ).json()
    assert result["head"]["status"] == 100
    assert db.query(Follow).count() == 0


def test_avatar_upload_no_longer_calls_storage_with_wrong_signature(db) -> None:
    token = _user(db, "avatar-user")
    with TestClient(app) as client:
        response = client.post(
            "/avatar",
            data={"token": token},
            files={"file": ("avatar.png", b"not-a-real-image-but-nonempty", "image/png")},
        )
    assert response.status_code == 200
    assert response.json()["head"]["status"] == 0
    assert response.json()["body"]["avatar_url"].endswith("avatar-user.png")


class _MemoryImpressions:
    def __init__(self) -> None:
        self.seen: dict[str, set[str]] = {}
        self.recent: dict[str, list[str]] = {}

    def list_seen_ids(self, *, user_id: str) -> set[str]:
        return set(self.seen.get(user_id, set()))

    def mark_seen(self, *, user_id: str, video_id: str) -> None:
        self.seen.setdefault(user_id, set()).add(video_id)
        current = self.recent.setdefault(user_id, [])
        self.recent[user_id] = [video_id] + [
            item_id for item_id in current if item_id != video_id
        ][:2]

    def list_recent_ids(self, *, user_id: str) -> list[str]:
        return list(self.recent.get(user_id, []))

    def clear_cycle(self, *, user_id: str) -> None:
        self.seen.pop(user_id, None)

    def clear_user(self, *, user_id: str) -> None:
        self.seen.pop(user_id, None)
        self.recent.pop(user_id, None)


def _published(db, video_id: str, user_id: str, *, created_at: datetime) -> None:
    timeline = {"interactions": [{"gesture": "tap", "gate_at_ms": 1000}]}
    spec = compile_runtime_spec(
        item_id=video_id,
        content_mode="single",
        source=timeline,
        video_url=f"/media/{video_id}.mp4",
    )
    db.add(
        PublishedVideo(
            id=video_id,
            video_url=f"/media/{video_id}.mp4",
            timeline=timeline,
            runtime_spec=spec,
            runtime_spec_version=RUNTIME_SPEC_VERSION,
            version="1",
            user_id=user_id,
            content_mode="single",
            created_at=created_at,
            updated_at=created_at,
        )
    )


def test_feed_fields_circular_cursor_play_count_and_finite_list_pagination(db, monkeypatch) -> None:
    token = _user(db, "viewer")
    _user(db, "author")
    db.add(Follow(follower_user_id="viewer", followee_user_id="author"))
    now = datetime.now(timezone.utc)
    for index in range(3):
        _published(
            db,
            f"video-{index}",
            "author",
            created_at=now - timedelta(minutes=index),
        )
    db.commit()
    memory = _MemoryImpressions()
    monkeypatch.setattr(feed_router, "get_impression_store", lambda: memory)

    with TestClient(app) as client:
        first = client.post(
            "/video",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "head": {"act": "video", "ver": "1.2", "ssid": "device-a"},
                "body": {"limit": 2},
            },
        ).json()
        second = client.post(
            "/video",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "head": {"act": "video", "ver": "1.2", "ssid": "device-a"},
                "body": {"limit": 2, "cursor": first["body"]["next_cursor"]},
            },
        ).json()
        impression = client.post(
            "/impression",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "head": {"act": "impression", "ver": "1.2"},
                "body": {"video_id": "video-0"},
            },
        ).json()
        page1 = client.post(
            "/user_videos",
            json=_legacy_json(
                "user_videos",
                token,
                {"user_id": "author", "limit": 2},
            ),
        ).json()
        page2 = client.post(
            "/user_videos",
            json=_legacy_json(
                "user_videos",
                token,
                {
                    "user_id": "author",
                    "limit": 2,
                    "cursor": page1["body"]["next_cursor"],
                },
            ),
        ).json()

    assert first["body"]["is_circular"] is True
    assert first["body"]["has_more"] is True
    assert all(item["is_following"] for item in first["body"]["items"])
    assert all(item["viewer_following_author"] for item in first["body"]["items"])
    assert second["head"]["status"] == 0
    assert impression["head"]["status"] == 0
    assert db.query(VideoView).filter(VideoView.video_id == "video-0").count() == 1
    ids1 = [item["item_id"] for item in page1["body"]["items"]]
    ids2 = [item["item_id"] for item in page2["body"]["items"]]
    assert len(ids1) == 2
    assert len(ids2) == 1
    assert not set(ids1) & set(ids2)
    assert page1["body"]["has_more"] is True
    assert page2["body"]["has_more"] is False


def test_feed_request_after_final_impression_resumes_after_highest_ranked_recent(
    db,
    monkeypatch,
) -> None:
    token = _user(db, "cycle-viewer")
    _user(db, "cycle-author")
    now = datetime.now(timezone.utc)
    video_ids = [f"cycle-video-{index}" for index in range(3)]
    for index, video_id in enumerate(video_ids):
        _published(
            db,
            video_id,
            "cycle-author",
            created_at=now - timedelta(minutes=index),
        )
    db.commit()
    memory = _MemoryImpressions()
    monkeypatch.setattr(feed_router, "get_impression_store", lambda: memory)

    with TestClient(app) as client:
        for video_id in video_ids:
            impression = client.post(
                "/impression",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "head": {"act": "impression", "ver": "1.2"},
                    "body": {"video_id": video_id},
                },
            ).json()
            assert impression["head"]["status"] == 0

        next_cycle = client.post(
            "/video",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "head": {"act": "video", "ver": "1.2", "ssid": "cycle-device"},
                "body": {"limit": 2},
            },
        ).json()

    assert next_cycle["head"]["status"] == 0
    assert [item["item_id"] for item in next_cycle["body"]["items"]] == video_ids[1:]
    assert memory.seen.get("cycle-viewer", set()) == set()
    assert memory.recent["cycle-viewer"] == list(reversed(video_ids))


def test_new_item_seen_before_old_cycle_tail_does_not_repeat_at_fresh_cycle_head(
    db,
    monkeypatch,
) -> None:
    token = _user(db, "new-tail-viewer")
    _user(db, "new-tail-author")
    now = datetime.now(timezone.utc)
    old_video_ids = [f"old-video-{index}" for index in range(3)]
    for index, video_id in enumerate(old_video_ids):
        _published(
            db,
            video_id,
            "new-tail-author",
            created_at=now - timedelta(minutes=index + 1),
        )
    db.commit()
    memory = _MemoryImpressions()
    monkeypatch.setattr(feed_router, "get_impression_store", lambda: memory)

    with TestClient(app) as client:
        # The viewer had already consumed one old item when a newly published
        # item jumped to the front. They then played that new item followed by
        # the two remaining items from the old cycle.
        for video_id in (old_video_ids[0],):
            response = client.post(
                "/impression",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "head": {"act": "impression", "ver": "1.2"},
                    "body": {"video_id": video_id},
                },
            ).json()
            assert response["head"]["status"] == 0

        _published(
            db,
            "new-video",
            "new-tail-author",
            created_at=now + timedelta(minutes=1),
        )
        db.commit()

        for video_id in ["new-video", old_video_ids[1], old_video_ids[2]]:
            impression = client.post(
                "/impression",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "head": {"act": "impression", "ver": "1.2"},
                    "body": {"video_id": video_id},
                },
            ).json()
            assert impression["head"]["status"] == 0

        fresh_cycle = client.post(
            "/video",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "head": {"act": "video", "ver": "1.2", "ssid": "new-tail-device"},
                "body": {"limit": 2},
            },
        ).json()

    assert [item["item_id"] for item in fresh_cycle["body"]["items"]] == old_video_ids[
        :2
    ]
    assert memory.recent["new-tail-viewer"] == [
        old_video_ids[2],
        old_video_ids[1],
        "new-video",
    ]


def test_following_and_follower_lists_expose_stable_cursor_pagination(db) -> None:
    token = _user(db, "viewer")
    for user_id in ("author-1", "author-2", "author-3", "fan-1", "fan-2", "fan-3"):
        _user(db, user_id)
    now = datetime.now(timezone.utc)
    db.add_all(
        [
            Follow(
                follower_user_id="viewer",
                followee_user_id=f"author-{index}",
                created_at=now - timedelta(seconds=index),
            )
            for index in range(1, 4)
        ]
        + [
            Follow(
                follower_user_id=f"fan-{index}",
                followee_user_id="viewer",
                created_at=now - timedelta(seconds=index),
            )
            for index in range(1, 4)
        ]
    )
    db.commit()

    def page(client, endpoint: str, cursor: str | None = None) -> dict:
        body: dict = {"limit": 2}
        if cursor is not None:
            body["cursor"] = cursor
        return client.post(
            endpoint,
            json=_legacy_json(endpoint.removeprefix("/"), token, body),
        ).json()["body"]

    with TestClient(app) as client:
        following_1 = page(client, "/following")
        following_2 = page(client, "/following", following_1["next_cursor"])
        followers_1 = page(client, "/followers")
        followers_2 = page(client, "/followers", followers_1["next_cursor"])

    for first, second in ((following_1, following_2), (followers_1, followers_2)):
        first_ids = {item["user_id"] for item in first["items"]}
        second_ids = {item["user_id"] for item in second["items"]}
        assert len(first_ids) == 2
        assert len(second_ids) == 1
        assert first_ids.isdisjoint(second_ids)
        assert first["has_more"] is True
        assert first["next_cursor"]
        assert second["has_more"] is False
        assert second["next_cursor"] is None


def test_verify_returns_saved_birthday_and_under_13_state(db) -> None:
    _user(db, "young", birthday="2020-01-01")
    now = datetime.now(timezone.utc)
    db.add(
        EmailCode(
            email="young@example.com",
            purpose="login",
            code="123456",
            created_at=now,
            expires_at=now + timedelta(minutes=10),
        )
    )
    db.commit()
    with TestClient(app) as client:
        response = client.post(
            "/verify",
            json={
                "head": {"act": "verify", "ver": "1.2"},
                "body": {"email": "young@example.com", "code": "123456"},
            },
        ).json()
    assert response["head"]["status"] == 0
    assert response["body"]["birthday"] == "2020-01-01"
    assert response["body"]["needs_birthday"] is False
    assert response["body"]["is_under_13"] is True
