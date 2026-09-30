from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.models import (
    Comment,
    PublishedVideo,
    SocialNotification,
    SocialRateEvent,
    User,
    UserBlock,
    UserToken,
)
from app.protocol_video import compile_runtime_spec
from app.web_session import WEB_CSRF_COOKIE, WEB_SESSION_COOKIE


def _user(db, user_id: str) -> str:
    now = datetime.now(timezone.utc)
    token = f"token-{user_id}"
    db.add(User(
        user_id=user_id,
        provider="email",
        subject=f"{user_id}@example.com",
        nickname=user_id.title(),
        enabled=True,
    ))
    db.add(UserToken(
        token=token,
        user_id=user_id,
        created_at=now,
        expires_at=now + timedelta(days=1),
    ))
    db.commit()
    return token


def _video(db, video_id: str, author_id: str) -> None:
    now = datetime.now(timezone.utc)
    timeline = {"interactions": [{"gesture": "tap", "gate_at_ms": 1000}]}
    spec = compile_runtime_spec(
        item_id=video_id,
        content_mode="single",
        source=timeline,
        video_url=f"/media/{video_id}.mp4",
    )
    db.add(PublishedVideo(
        id=video_id,
        video_url=f"/media/{video_id}.mp4",
        timeline=timeline,
        runtime_spec=spec,
        runtime_spec_version=spec["version"],
        version="1",
        title="Playable lights",
        user_id=author_id,
        review_status="approved",
        cdn_ready=True,
        distribution_enabled=True,
        created_at=now,
        updated_at=now,
    ))
    creator = db.get(User, author_id)
    creator.creator_activated_at = now
    db.commit()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_public_creator_like_comment_reply_and_notifications(db) -> None:
    author = _user(db, "author")
    viewer = _user(db, "viewer")
    replier = _user(db, "replier")
    _video(db, "video-1", "author")

    with TestClient(app) as client:
        profile = client.get("/api/v1/public/creators/author")
        follow = client.put("/api/v1/social/creators/author/follow", headers=_auth(viewer))
        like = client.put("/api/v1/social/videos/video-1/like", headers=_auth(viewer))
        comment = client.post(
            "/api/v1/social/videos/video-1/comments",
            headers=_auth(viewer),
            json={"body": "This interaction feels great."},
        )
        comment_id = comment.json()["id"]
        reply = client.post(
            f"/api/v1/social/comments/{comment_id}/replies",
            headers=_auth(replier),
            json={"body": "Agreed."},
        )
        client.put(f"/api/v1/social/comments/{comment_id}/like", headers=_auth(replier))
        comments = client.get("/api/v1/public/videos/video-1/comments")
        replies = client.get(f"/api/v1/public/comments/{comment_id}/replies")
        notifications = client.get("/api/v1/social/notifications", headers=_auth(author))

    assert profile.status_code == 200
    assert follow.json() == {"active": True, "follower_count": 1}
    assert like.json() == {"active": True, "like_count": 1}
    assert comment.status_code == 200 and reply.status_code == 200
    assert comments.json()["items"][0]["reply_count"] == 1
    assert replies.json()["items"][0]["body"] == "Agreed."
    assert {item["type"] for item in notifications.json()["items"]} == {
        "follow", "video_like", "comment"
    }
    video = db.get(PublishedVideo, "video-1")
    assert (video.like_count, video.comment_count) == (1, 2)


def test_like_is_idempotent_and_reconcile_repairs_counts(db) -> None:
    _user(db, "author")
    viewer = _user(db, "viewer")
    _video(db, "video-1", "author")
    with TestClient(app) as client:
        first = client.put("/api/v1/social/videos/video-1/like", headers=_auth(viewer))
        second = client.put("/api/v1/social/videos/video-1/like", headers=_auth(viewer))
        db.get(PublishedVideo, "video-1").like_count = 99
        db.commit()
        reconciled = client.post(
            "/internal/v1/social/reconcile",
            headers={"X-Publish-Key": "test-publish-key"},
        )
    assert first.json()["like_count"] == 1
    assert second.json()["like_count"] == 1
    assert reconciled.json()["videos_updated"] == 1
    assert db.get(PublishedVideo, "video-1").like_count == 1
    assert db.query(SocialNotification).filter_by(type="video_like").count() == 1


def test_liked_videos_respect_declared_runtime_capabilities(db) -> None:
    _user(db, "author")
    viewer = _user(db, "viewer")
    _video(db, "video-modern", "author")
    video = db.get(PublishedVideo, "video-modern")
    video.runtime_spec_version = "1.2"
    video.runtime_spec = {**video.runtime_spec, "version": "1.2"}
    db.commit()
    version = video.runtime_spec_version

    with TestClient(app) as client:
        client.put("/api/v1/social/videos/video-modern/like", headers=_auth(viewer))
        legacy = client.get("/api/v1/social/liked", headers=_auth(viewer))
        supported = client.get(
            "/api/v1/social/liked",
            headers=_auth(viewer),
            params={"experience_spec_versions": f"1.0,{version}"},
        )

    assert legacy.status_code == 200
    assert legacy.json()["items"] == []
    assert supported.status_code == 200
    assert [item["video_id"] for item in supported.json()["items"]] == ["video-modern"]


def test_block_filters_comments_and_prevents_interaction(db) -> None:
    _user(db, "author")
    viewer = _user(db, "viewer")
    _video(db, "video-1", "author")
    db.add(Comment(
        id="cmt-blocked",
        video_id="video-1",
        author_user_id="viewer",
        body="hidden by block",
    ))
    db.add(UserBlock(blocker_user_id="author", blocked_user_id="viewer"))
    db.commit()
    with TestClient(app) as client:
        blocked = client.put("/api/v1/social/videos/video-1/like", headers=_auth(viewer))
        comments = client.get(
            "/api/v1/public/videos/video-1/comments",
            headers=_auth("token-author"),
        )
    assert blocked.status_code == 403
    assert comments.json()["items"] == []


def test_web_social_mutations_require_double_submit_csrf(db) -> None:
    _user(db, "author")
    token = _user(db, "viewer")
    _video(db, "video-1", "author")
    db.add(Comment(id="comment-author", video_id="video-1", author_user_id="author", body="report me"))
    db.commit()
    with TestClient(app) as client:
        client.cookies.set(WEB_SESSION_COOKIE, token)
        client.get("/api/v1/web/config")
        rejected = client.put("/api/v1/web/social/videos/video-1/like")
        accepted = client.put(
            "/api/v1/web/social/videos/video-1/like",
            headers={"X-Pixo-CSRF": client.cookies.get(WEB_CSRF_COOKIE) or ""},
        )
        reported = client.post(
            "/api/v1/web/social/comments/comment-author/report",
            headers={"X-Pixo-CSRF": client.cookies.get(WEB_CSRF_COOKIE) or ""},
            json={"reason": "harassment"},
        )
    assert rejected.status_code == 403
    assert accepted.status_code == 200
    assert reported.status_code == 200


def test_comment_length_is_unicode_codepoint_limited(db) -> None:
    _user(db, "author")
    viewer = _user(db, "viewer")
    _video(db, "video-1", "author")
    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/social/videos/video-1/comments",
            headers=_auth(viewer),
            json={"body": "界" * 280},
        )
        rejected = client.post(
            "/api/v1/social/videos/video-1/comments",
            headers=_auth(viewer),
            json={"body": "界" * 281},
        )
    assert accepted.status_code == 200
    assert rejected.status_code == 422


def test_social_toggle_rate_counts_unlikes_and_noop_retries(db) -> None:
    _user(db, "author")
    viewer = _user(db, "viewer")
    _video(db, "video-1", "author")
    now = datetime.now(timezone.utc)
    db.add_all(SocialRateEvent(user_id="viewer", kind="video_like", created_at=now) for _ in range(120))
    db.commit()
    with TestClient(app) as client:
        response = client.delete("/api/v1/social/videos/video-1/like", headers=_auth(viewer))
    assert response.status_code == 429
