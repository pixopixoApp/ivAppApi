from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app.models import (
    CreatorProfileAudit,
    CreatorTopic,
    CreatorTopicAssignment,
    PublishedVideo,
    User,
    UserToken,
)
from app.protocol_video import compile_runtime_spec


def _account(db, user_id: str, nickname: str) -> str:
    now = datetime.now(timezone.utc)
    token = f"token-{user_id}"
    db.add(User(
        user_id=user_id,
        provider="email",
        subject=f"{user_id}@example.com",
        nickname=nickname,
        enabled=True,
        created_at=now,
    ))
    db.add(UserToken(
        token=token,
        user_id=user_id,
        created_at=now,
        expires_at=now + timedelta(days=1),
    ))
    db.commit()
    return token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _public_video(db, video_id: str, user_id: str, *, likes: int, age: int) -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=age)
    timeline = {"interactions": [{"gesture": "tap", "gate_at_ms": 1000}]}
    spec = compile_runtime_spec(
        item_id=video_id,
        content_mode="single",
        source=timeline,
        video_url=f"/media/{video_id}.mp4",
    )
    db.add(PublishedVideo(
        id=video_id,
        user_id=user_id,
        video_url=f"/media/{video_id}.mp4",
        timeline=timeline,
        runtime_spec=spec,
        runtime_spec_version=spec["version"],
        version="1",
        title=video_id,
        review_status="approved",
        cdn_ready=True,
        distribution_enabled=True,
        like_count=likes,
        created_at=now,
        updated_at=now,
    ))
    db.commit()


def test_handle_change_history_cooldown_and_conflicts(db) -> None:
    first = _account(db, "user-a", "Hello World")
    second = _account(db, "user-b", "Hello World")

    with TestClient(app) as client:
        initial = client.get("/api/v1/channel", headers=_auth(first))
        assert initial.status_code == 200
        assert initial.json()["handle"] == "hello_world"
        renamed = client.patch(
            "/api/v1/channel", headers=_auth(first),
            json={"handle": "Hella_Studio"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["handle"] == "hella_studio"
        assert renamed.json()["share_url"] == "https://pixopixo.com/@hella_studio"
        old = client.get("/api/v1/public/creators/by-handle/hello_world")
        assert old.status_code == 200
        assert old.json()["handle"] == "hella_studio"
        cooldown = client.patch(
            "/api/v1/channel", headers=_auth(first), json={"handle": "another_name"},
        )
        assert cooldown.status_code == 409
        assert cooldown.json()["detail"]["code"] == "HANDLE_COOLDOWN"
        collision = client.patch(
            "/api/v1/channel", headers=_auth(second), json={"handle": "hella_studio"},
        )
        assert collision.status_code == 409
        assert collision.json()["detail"]["code"] == "HANDLE_TAKEN"
        reserved = client.get(
            "/api/v1/channel/handle-availability", headers=_auth(second),
            params={"handle": "admin"},
        )
        assert reserved.json()["code"] == "HANDLE_RESERVED"


def test_handle_unique_constraint_ends_concurrent_claim_race(db) -> None:
    tokens = [
        _account(db, "racer-a", "Racer A"),
        _account(db, "racer-b", "Racer B"),
    ]

    def claim(token: str) -> tuple[int, str]:
        with TestClient(app) as client:
            response = client.patch(
                "/api/v1/channel", headers=_auth(token), json={"handle": "same_finish"},
            )
            return response.status_code, response.json().get("handle", "")

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, tokens))

    assert sorted(status for status, _handle in outcomes) == [200, 409]
    assert sum(handle == "same_finish" for _status, handle in outcomes) == 1


def test_private_channel_fields_public_email_pins_topics_and_sorting(db) -> None:
    token = _account(db, "creator", "Creator")
    db.add_all([
        CreatorTopic(id="topic-art", name="Art & Design", enabled=True),
        CreatorTopic(id="topic-old", name="Archived", enabled=False),
    ])
    db.commit()
    _public_video(db, "newest", "creator", likes=2, age=1)
    _public_video(db, "popular", "creator", likes=20, age=30)

    with TestClient(app) as client:
        saved = client.patch(
            "/api/v1/channel",
            headers=_auth(token),
            json={
                "nickname": "Creator Studio",
                "bio": "Playful camera experiments.",
                "content_language": "zh-Hans",
                "collaboration_email": "Work@Example.com",
                "collaboration_email_public": False,
                "external_links": [{"label": "Portfolio", "url": "https://example.com/me"}],
                "topic_ids": ["topic-art"],
                "pinned_video_ids": ["newest"],
            },
        )
        assert saved.status_code == 200
        handle = saved.json()["handle"]
        assert saved.json()["collaboration_email"] == "work@example.com"
        public = client.get(f"/api/v1/public/creators/by-handle/{handle}")
        assert public.status_code == 200
        assert public.json()["collaboration_email"] is None
        assert public.json()["topics"] == [{"id": "topic-art", "name": "Art & Design"}]
        popular = client.get(
            "/api/v1/public/creators/creator/works", params={"sort": "popular"},
        )
        assert [item["video_id"] for item in popular.json()["items"]] == ["newest", "popular"]
        assert popular.json()["items"][0]["is_pinned"] is True
        made_public = client.patch(
            "/api/v1/channel", headers=_auth(token),
            json={"collaboration_email_public": True},
        )
        assert made_public.status_code == 200
        public = client.get(f"/api/v1/public/creators/by-handle/{handle}")
        assert public.json()["collaboration_email"] == "work@example.com"


def test_channel_images_are_decoded_reencoded_and_type_checked(db) -> None:
    token = _account(db, "image-user", "Image User")
    buffer = BytesIO()
    Image.new("RGB", (900, 500), (90, 140, 40)).save(buffer, format="PNG")
    image = buffer.getvalue()

    with TestClient(app) as client:
        avatar = client.post(
            "/api/v1/channel/avatar", headers=_auth(token),
            files={"file": ("avatar.png", image, "image/png")},
        )
        assert avatar.status_code == 200
        assert avatar.json()["avatar_url"].endswith(".webp")
        background = client.post(
            "/api/v1/channel/background?focus_x=0.25&focus_y=0.75",
            headers=_auth(token),
            files={"file": ("background.png", image, "image/png")},
        )
        assert background.status_code == 200
        assert background.json()["background_desktop_url"].endswith(".webp")
        assert background.json()["background_mobile_url"].endswith(".webp")
        forged = client.post(
            "/api/v1/channel/avatar", headers=_auth(token),
            files={"file": ("avatar.jpg", image, "image/jpeg")},
        )
        assert forged.status_code == 400
        deleted = client.delete("/api/v1/channel/background", headers=_auth(token))
        assert deleted.status_code == 200
        assert deleted.json()["background_url"] == ""


def test_topic_archive_replacement_is_transactional_deduplicated_and_audited(db) -> None:
    _account(db, "topic-user", "Topic User")
    db.add_all([
        CreatorTopic(id="source", name="Source", enabled=True),
        CreatorTopic(id="replacement", name="Replacement", enabled=True),
        CreatorTopic(id="other", name="Other", enabled=True),
        CreatorTopicAssignment(user_id="topic-user", topic_id="source", position=0),
        CreatorTopicAssignment(user_id="topic-user", topic_id="replacement", position=1),
        CreatorTopicAssignment(user_id="topic-user", topic_id="other", position=2),
    ])
    db.commit()
    headers = {"X-Publish-Key": "test-publish-key"}
    actor = {"actor_id": "operator", "actor_role": "operator", "source": "ivadmin"}

    with TestClient(app) as client:
        failed = client.post(
            "/internal/v1/creator-channels/topics/source/archive",
            headers=headers,
            json={**actor, "strategy": "replace", "replacement_topic_id": "missing"},
        )
        assert failed.status_code == 422
        assert db.get(CreatorTopic, "source").enabled is True
        archived = client.post(
            "/internal/v1/creator-channels/topics/source/archive",
            headers=headers,
            json={**actor, "strategy": "replace", "replacement_topic_id": "replacement"},
        )

    assert archived.status_code == 200
    db.expire_all()
    assignments = db.query(CreatorTopicAssignment).filter_by(user_id="topic-user").order_by(
        CreatorTopicAssignment.position
    ).all()
    assert [(row.topic_id, row.position) for row in assignments] == [
        ("replacement", 0), ("other", 1),
    ]
    relation_audit = db.query(CreatorProfileAudit).filter_by(
        user_id="topic-user", action="creator.topics.replace"
    ).one()
    assert [topic["id"] for topic in relation_audit.before_json["topics"]] == [
        "source", "replacement", "other",
    ]
    assert [topic["id"] for topic in relation_audit.after_json["topics"]] == [
        "replacement", "other",
    ]
