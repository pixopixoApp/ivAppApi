from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.models import Follow, PublishedVideo, User, UserToken
from app.routers import admin as admin_router
from app.routers import feed as feed_router


class _MemoryImpressions:
    def list_seen_ids(self, *, user_id: str) -> set[str]:
        return set()

    def mark_seen(self, *, user_id: str, video_id: str) -> None:
        return None


def _user(db, user_id: str) -> str:
    now = datetime.now(timezone.utc)
    token = f"token-{user_id}"
    db.add(
        User(
            user_id=user_id,
            provider="email",
            subject=f"{user_id}@example.com",
            enabled=True,
            nickname=user_id.title(),
        )
    )
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


def _payload(*, version: str = "a" * 64) -> dict:
    return {
        "item_id": "html-neon-001",
        "version": version,
        "html_url": f"https://html.test/pixo/html/html-neon-001/{version}/index.html",
        "bridge_version": 1,
        "required_capabilities": [
            "motion",
            "microphoneLevel",
            "cameraStream",
            "haptics",
            "mediaControl",
        ],
        "title": "Neon Balance",
        "description": "Tilt and clap",
        "user_id": "author",
        "feed_weight": -10,
    }


def test_publish_html_is_idempotent_and_appears_in_every_video_list(
    db, monkeypatch
) -> None:
    token = _user(db, "viewer")
    _user(db, "author")
    db.add(Follow(follower_user_id="viewer", followee_user_id="author"))
    db.commit()
    monkeypatch.setattr(admin_router, "probe_html_entry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(feed_router, "get_impression_store", lambda: _MemoryImpressions())
    monkeypatch.setenv("PUBLIC_SHARE_BASE_URL", "https://api.pixopixo.cn")
    headers = {"X-Publish-Key": "test-publish-key"}

    with TestClient(app) as client:
        created = client.post("/internal/v1/publish-html", headers=headers, json=_payload())
        repeated = client.post("/internal/v1/publish-html", headers=headers, json=_payload())
        feed = client.post(
            "/video",
            headers={"Authorization": f"Bearer {token}"},
            json={"head": {"act": "video", "ver": "1.2"}, "body": {"limit": 1}},
        ).json()
        detail = client.post(
            "/video_detail",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "head": {"act": "video_detail", "ver": "1.2"},
                "body": {"video_id": "html-neon-001"},
            },
        ).json()
        following = client.post(
            "/following_feed",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "head": {"act": "following_feed", "ver": "1.2"},
                "body": {"limit": 10},
            },
        ).json()
        author_videos = client.post(
            "/user_videos",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "head": {"act": "user_videos", "ver": "1.2"},
                "body": {"user_id": "author", "limit": 10},
            },
        ).json()
        share_page = client.get("/api/v1/share/html-neon-001")

    assert created.status_code == 200
    assert created.json()["content_type"] == "html"
    assert created.json()["updated"] is False
    assert repeated.status_code == 200
    assert repeated.json()["updated"] is False

    for response in (feed, detail, following, author_videos):
        assert response["head"]["status"] == 0
        item = response["body"]["items"][0]
        assert item["item_id"] == "html-neon-001"
        assert item["content_type"] == "html"
        assert item["html_url"].startswith("https://html.test/")
        assert item["bridge_version"] == 1
        assert item["required_capabilities"] == _payload()["required_capabilities"]
        assert item["share_url"] == (
            "https://api.pixopixo.cn/api/v1/share/html-neon-001"
        )
        assert "video" not in item

    assert share_page.status_code == 200
    assert "pixo://work/html-neon-001" in share_page.text
    assert _payload()["html_url"] not in share_page.text

    db.expire_all()
    row = db.get(PublishedVideo, "html-neon-001")
    assert row is not None
    assert row.video_url is None
    assert row.timeline is None
    assert row.runtime_spec is None
    assert row.runtime_spec_version is None


def test_html_publish_rejects_untrusted_capability_and_cross_type_overwrite(
    db, monkeypatch
) -> None:
    _user(db, "author")
    monkeypatch.setattr(admin_router, "probe_html_entry", lambda *_args, **_kwargs: None)
    headers = {"X-Publish-Key": "test-publish-key"}
    with TestClient(app) as client:
        bad_origin = client.post(
            "/internal/v1/publish-html",
            headers=headers,
            json={**_payload(), "html_url": "https://attacker.test/index.html"},
        )
        bad_capability = client.post(
            "/internal/v1/publish-html",
            headers=headers,
            json={**_payload(), "required_capabilities": ["location"]},
        )
        mutable_url = client.post(
            "/internal/v1/publish-html",
            headers=headers,
            json={**_payload(), "html_url": "https://html.test/latest/index.html"},
        )
        nested_traversal = client.post(
            "/internal/v1/publish-html",
            headers=headers,
            json={
                **_payload(),
                "html_url": (
                    "https://html.test/pixo/html/html-neon-001/"
                    f"{'a' * 64}/%25252e%25252e/index.html"
                ),
            },
        )
        encoded_backslash = client.post(
            "/internal/v1/publish-html",
            headers=headers,
            json={
                **_payload(),
                "html_url": (
                    "https://html.test/pixo/html/html-neon-001/"
                    f"{'a' * 64}/assets%255cindex.html"
                ),
            },
        )
        created = client.post("/internal/v1/publish-html", headers=headers, json=_payload())
        runtime_overwrite = client.post(
            "/internal/v1/publish",
            headers=headers,
            data={
                "video_id": "html-neon-001",
                "version": "runtime-1",
                "user_id": "author",
                "content_mode": "single",
                "timeline": json.dumps({"interactions": []}),
            },
            files={"video": ("video.mp4", b"video", "video/mp4")},
        )

    assert bad_origin.status_code == 400
    assert bad_capability.status_code == 400
    assert mutable_url.status_code == 400
    assert nested_traversal.status_code == 400
    assert encoded_backslash.status_code == 400
    assert created.status_code == 200
    assert runtime_overwrite.status_code == 409


def test_html_same_version_cannot_change_metadata(db, monkeypatch) -> None:
    _user(db, "author")
    monkeypatch.setattr(admin_router, "probe_html_entry", lambda *_args, **_kwargs: None)
    headers = {"X-Publish-Key": "test-publish-key"}
    with TestClient(app) as client:
        assert client.post(
            "/internal/v1/publish-html", headers=headers, json=_payload()
        ).status_code == 200
        conflict = client.post(
            "/internal/v1/publish-html",
            headers=headers,
            json={**_payload(), "title": "Changed"},
        )
        updated = client.post(
            "/internal/v1/publish-html",
            headers=headers,
            json=_payload(version="b" * 64),
        )

    assert conflict.status_code == 409
    assert updated.status_code == 200
    assert updated.json()["updated"] is True
