"""Seed a deterministic creator/social demo in the isolated local preview DB."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import Comment, Follow, PublishedVideo, PublishedVideoSeo, User
from app.protocol_video import compile_runtime_spec

CREATOR_ID = "pixo-preview-creator"
VIDEO_IDS = ("preview-neon-city", "preview-orbit-tap", "preview-light-trail")


def main() -> None:
    database = os.environ.get("DATABASE_URL", "")
    if not database.startswith("sqlite:///") or "web-dev-creator-v2" not in database:
        raise SystemExit("Social preview seeding only supports the isolated Web preview SQLite DB")

    now = datetime.now(timezone.utc)
    posters = (
        "https://media.pixopixo.com/ivapp-media/v1/public/covers/jO/mo_p4wMItMDFohMlrVtwSZhPvjO.jpg",
        "https://media.pixopixo.com/ivapp-media/v1/public/covers/c9/mo_vUzu9x_hsSp8cqcagrZywwc9.jpg",
        "https://media.pixopixo.com/ivapp-media/v1/public/covers/rT/mo__ZVzC-lB2u6uW3HVRk5JWsrT.jpg",
    )
    titles = ("Wake the neon city", "Catch the orbit", "Run toward the light")
    interactions = (("tap", "swipe_up"), ("pinch_out",), ("hand_thumb_up", "tap"))
    like_counts = (4821, 3132, 2504)
    comment_counts = (128, 64, 39)

    with SessionLocal() as db:
        creator = db.get(User, CREATOR_ID)
        if creator is None:
            creator = User(
                user_id=CREATOR_ID,
                provider="preview",
                subject=CREATOR_ID,
            )
            db.add(creator)
        creator.enabled = True
        creator.nickname = "Luma Playlab"
        creator.bio = "Tiny cinematic worlds that react to you. Tap, move, and change the story."
        creator.creator_activated_at = now - timedelta(days=90)

        commenters = (
            ("preview-maya", "Maya"),
            ("preview-leo", "Leo"),
        )
        for user_id, nickname in commenters:
            user = db.get(User, user_id)
            if user is None:
                user = User(user_id=user_id, provider="preview", subject=user_id)
                db.add(user)
            user.enabled = True
            user.nickname = nickname

        db.query(Comment).filter(Comment.video_id.in_(VIDEO_IDS)).delete(
            synchronize_session=False
        )
        db.query(PublishedVideoSeo).filter(
            PublishedVideoSeo.video_id.in_(VIDEO_IDS)
        ).delete(synchronize_session=False)
        db.query(PublishedVideo).filter(PublishedVideo.id.in_(VIDEO_IDS)).delete(
            synchronize_session=False
        )
        db.query(Follow).filter(
            (Follow.followee_user_id == CREATOR_ID)
            | (Follow.follower_user_id == CREATOR_ID)
        ).delete(synchronize_session=False)

        for index, video_id in enumerate(VIDEO_IDS):
            timeline = {
                "interactions": [
                    {"gesture": "tap", "gate_at_ms": 900 + position * 1300}
                    for position, _interaction in enumerate(interactions[index])
                ]
            }
            video_url = f"https://media.pixopixo.com/runtime/{video_id}.mp4"
            spec = compile_runtime_spec(
                item_id=video_id,
                content_mode="single",
                source=timeline,
                video_url=video_url,
            )
            created_at = now - timedelta(days=index * 4 + 1)
            db.add(
                PublishedVideo(
                    id=video_id,
                    content_type="runtime",
                    video_url=video_url,
                    timeline=timeline,
                    runtime_spec=spec,
                    runtime_spec_version=spec["version"],
                    version="preview-v1",
                    title=titles[index],
                    description="A playable Pixo short created for the local social preview.",
                    user_id=CREATOR_ID,
                    content_mode="single",
                    distribution_enabled=True,
                    cdn_ready=True,
                    content_source="ugc",
                    review_status="approved",
                    like_count=like_counts[index],
                    comment_count=comment_counts[index],
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
            db.add(
                PublishedVideoSeo(
                    video_id=video_id,
                    slug=f"{video_id}-1000{index}",
                    page_title=titles[index],
                    page_description="A playable interactive video on Pixopixo.",
                    meta_title=f"{titles[index]} | Pixopixo",
                    meta_description="Play this interactive Pixo short.",
                    interaction_types=list(interactions[index]),
                    thumbnail_url=posters[index],
                    status="ready",
                    source_hash=f"preview-{index}",
                    created_at=created_at,
                    updated_at=created_at,
                )
            )

        root = Comment(
            id="preview-comment-root",
            video_id=VIDEO_IDS[0],
            author_user_id="preview-maya",
            body="The tap transition feels surprisingly magical.",
            like_count=18,
            reply_count=1,
            created_at=now - timedelta(hours=2),
        )
        db.add(root)
        db.add(
            Comment(
                id="preview-comment-reply",
                video_id=VIDEO_IDS[0],
                author_user_id="preview-leo",
                root_comment_id=root.id,
                reply_to_user_id=root.author_user_id,
                body="And the sound cue makes the whole moment land.",
                like_count=6,
                created_at=now - timedelta(hours=1),
            )
        )
        for index in range(248):
            db.add(
                Follow(
                    follower_user_id=f"preview-follower-{index:03d}",
                    followee_user_id=CREATOR_ID,
                )
            )
        for index in range(12):
            db.add(
                Follow(
                    follower_user_id=CREATOR_ID,
                    followee_user_id=f"preview-following-{index:03d}",
                )
            )
        db.commit()

    print(f"Social preview ready: {CREATOR_ID}")


if __name__ == "__main__":
    main()
