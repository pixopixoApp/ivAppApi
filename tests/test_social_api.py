from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.models import (
    Comment,
    Follow,
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


def test_public_creator_profile_is_available_before_first_publication(db) -> None:
    _user(db, "new-creator")

    with TestClient(app) as client:
        profile = client.get("/api/v1/public/creators/new-creator")
        works = client.get("/api/v1/public/creators/new-creator/works")

    assert profile.status_code == 200
    assert profile.json()["user_id"] == "new-creator"
    assert profile.json()["work_count"] == 0
    assert works.status_code == 200
    assert works.json()["items"] == []


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


def test_social_seed_interactions_are_isolated_and_preview_gated(db) -> None:
    _user(db, "author")
    _video(db, "video-1", "author")
    db.add(User(
        user_id="social-seed-prelaunch-v1-001",
        provider="internal",
        subject="social-seed:prelaunch-v1:001",
        nickname="AmberBadger",
        avatar_url="/media/avatars/seed.png",
        source="admin",
        internal_purpose="social_seed",
        internal_batch="prelaunch-v1",
        enabled=True,
    ))
    db.commit()
    headers = {"X-Publish-Key": "test-publish-key"}
    actor = {
        "actor_user_id": "social-seed-prelaunch-v1-001",
        "batch_id": "prelaunch-v1",
    }
    comment_body = {
        **actor,
        "body": "The timing on this interaction feels crisp.",
        "idempotency_key": "robot-run-1-comment-1",
    }

    with TestClient(app) as client:
        accounts = client.get(
            "/internal/v1/social-seed/accounts",
            headers=headers,
            params={"batch_id": "prelaunch-v1"},
        )
        liked = client.put(
            "/internal/v1/social-seed/videos/video-1/like",
            headers=headers,
            json=actor,
        )
        liked_again = client.put(
            "/internal/v1/social-seed/videos/video-1/like",
            headers=headers,
            json=actor,
        )
        comment = client.post(
            "/internal/v1/social-seed/videos/video-1/comments",
            headers=headers,
            json=comment_body,
        )
        replay = client.post(
            "/internal/v1/social-seed/videos/video-1/comments",
            headers=headers,
            json=comment_body,
        )
        hidden_engagement = client.get("/api/v1/public/videos/video-1/engagement")
        hidden_comments = client.get("/api/v1/public/videos/video-1/comments")
        hidden_profile = client.get(
            "/api/v1/public/creators/social-seed-prelaunch-v1-001"
        )
        enabled = client.put(
            "/internal/v1/social-seed-preview",
            headers=headers,
            json={"enabled": True, "updated_by": "test-manager"},
        )
        shown_engagement = client.get("/api/v1/public/videos/video-1/engagement")
        shown_comments = client.get("/api/v1/public/videos/video-1/comments")
        shown_creator = client.get("/api/v1/public/creators/author")
        shown_detail = client.post(
            "/video_detail",
            json={
                "head": {"act": "video_detail", "ver": "1.2"},
                "body": {
                    "video_id": "video-1",
                    "supported_experience_spec_versions": ["1.0", "1.1", "1.2"],
                },
            },
        )
        disabled = client.put(
            "/internal/v1/social-seed-preview",
            headers=headers,
            json={"enabled": False, "updated_by": "release-check"},
        )
        hidden_again = client.get("/api/v1/public/videos/video-1/comments")

    assert accounts.json()["items"][0]["user_id"] == actor["actor_user_id"]
    assert liked.json() == {"active": True, "like_count": 0}
    assert liked_again.json() == liked.json()
    assert replay.json()["id"] == comment.json()["id"]
    assert hidden_engagement.json()["like_count"] == 0
    assert hidden_engagement.json()["comment_count"] == 0
    assert hidden_comments.json()["items"] == []
    assert hidden_profile.status_code == 404
    assert enabled.json()["enabled"] is True
    assert shown_engagement.json()["like_count"] == 1
    assert shown_engagement.json()["comment_count"] == 1
    assert shown_comments.json()["items"][0]["body"] == comment_body["body"]
    assert shown_creator.json()["received_like_count"] == 1
    assert shown_detail.json()["body"]["items"][0]["like_count"] == 1
    assert shown_detail.json()["body"]["items"][0]["comment_count"] == 1
    assert disabled.json()["enabled"] is False
    assert hidden_again.json()["items"] == []
    video = db.get(PublishedVideo, "video-1")
    assert (video.like_count, video.comment_count) == (0, 0)
    assert (video.seed_like_count, video.seed_comment_count) == (1, 1)
    assert db.query(SocialNotification).count() == 0


def test_seed_second_comment_hits_rate_check_without_typeerror(db) -> None:
    """Posting a second comment within 5s must return 429, not crash (500).

    Regression: the rate check compared a naive DB datetime with an aware one.
    """
    _user(db, "author")
    _video(db, "video-1", "author")
    db.add(User(
        user_id="social-seed-prelaunch-v1-009",
        provider="internal",
        subject="social-seed:prelaunch-v1:009",
        source="admin",
        internal_purpose="social_seed",
        internal_batch="prelaunch-v1",
        enabled=True,
    ))
    db.commit()
    headers = {"X-Publish-Key": "test-publish-key"}
    first = {
        "actor_user_id": "social-seed-prelaunch-v1-009",
        "batch_id": "prelaunch-v1",
        "body": "first comment here",
        "idempotency_key": "run-a:video-1:1",
    }
    second = {
        **first,
        "body": "second comment right after",
        "idempotency_key": "run-a:video-1:2",
    }
    with TestClient(app) as client:
        r1 = client.post(
            "/internal/v1/social-seed/videos/video-1/comments",
            headers=headers,
            json=first,
        )
        r2 = client.post(
            "/internal/v1/social-seed/videos/video-1/comments",
            headers=headers,
            json=second,
        )
    assert r1.status_code == 200
    # Second comment within 5s is rate-limited (429), never a 500.
    assert r2.status_code == 429


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
    assert legacy.json()["total_count"] == 0
    assert supported.status_code == 200
    assert [item["video_id"] for item in supported.json()["items"]] == ["video-modern"]
    assert supported.json()["total_count"] == 1


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


def test_social_state_batches_cookie_viewer_state_and_filters_blocks(db) -> None:
    _user(db, "author")
    viewer = _user(db, "viewer")
    _user(db, "blocked-author")
    _video(db, "video-1", "author")
    _video(db, "video-blocked", "blocked-author")
    db.add(Follow(follower_user_id="viewer", followee_user_id="author"))
    db.add(UserBlock(blocker_user_id="viewer", blocked_user_id="blocked-author"))
    db.commit()

    with TestClient(app) as client:
        client.cookies.set(WEB_SESSION_COOKIE, viewer)
        client.get("/api/v1/web/config")
        client.put(
            "/api/v1/web/social/videos/video-1/like",
            headers={"X-Pixo-CSRF": client.cookies.get(WEB_CSRF_COOKIE) or ""},
        )
        response = client.get(
            "/api/v1/public/social/state",
            params=[
                ("video_id", "video-1"),
                ("video_id", "video-blocked"),
                ("creator_id", "author"),
                ("creator_id", "blocked-author"),
            ],
        )

    assert response.status_code == 200
    assert response.json()["videos"]["video-1"]["viewer_liked"] is True
    assert "video-blocked" not in response.json()["videos"]
    assert response.json()["creators"]["author"] == {
        "follower_count": 1,
        "viewer_following": True,
    }
    assert "blocked-author" not in response.json()["creators"]


def test_social_state_rejects_more_than_twelve_ids(db) -> None:
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/public/social/state",
            params=[("video_id", f"video-{index}") for index in range(13)],
        )
    assert response.status_code == 422


def test_web_follow_lists_require_cookie_session_and_paginate(db) -> None:
    viewer = _user(db, "viewer")
    _user(db, "author")
    _user(db, "peer")
    _video(db, "video-1", "author")
    db.add(Follow(follower_user_id="peer", followee_user_id="author"))
    db.add(Follow(follower_user_id="author", followee_user_id="peer"))
    db.commit()

    with TestClient(app) as client:
        rejected = client.get("/api/v1/web/social/creators/author/followers")
        client.cookies.set(WEB_SESSION_COOKIE, viewer)
        followers = client.get(
            "/api/v1/web/social/creators/author/followers", params={"limit": 1}
        )
        following = client.get(
            "/api/v1/web/social/creators/author/following", params={"limit": 1}
        )
        capabilities = client.get("/api/v1/public/capabilities")

    assert rejected.status_code == 401
    assert [item["user_id"] for item in followers.json()["items"]] == ["peer"]
    assert [item["user_id"] for item in following.json()["items"]] == ["peer"]
    assert capabilities.json()["web_immersive_feed"] is True
