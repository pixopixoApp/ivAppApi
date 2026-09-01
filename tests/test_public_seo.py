from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.models import PublishedVideo, User
from app.seo import source_hash


def _published(db, *, video_id: str = "work-seo-1") -> PublishedVideo:
    now = datetime.now(timezone.utc)
    db.add(
        User(
            user_id="seo-author",
            provider="email",
            subject="seo@example.com",
            nickname="Maya",
        )
    )
    row = PublishedVideo(
        id=video_id,
        content_type="runtime",
        video_url="https://video.example/work.mp4",
        timeline={"interactions": [{"gesture": "tap", "gate_at_ms": 1000}]},
        runtime_spec={
            "item_id": video_id,
            "experience_spec_version": "1.2",
            "video": [
                {
                    "video_id": video_id,
                    "video": "https://video.example/work.mp4",
                    "interactions": [],
                }
            ],
        },
        runtime_spec_version="1.2",
        required_capabilities=[],
        version="v1",
        title="Untitled Experience",
        description="",
        user_id="seo-author",
        review_status="approved",
        distribution_enabled=True,
        cdn_ready=True,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    return row


def test_backfill_generation_and_public_permalink(db, monkeypatch) -> None:
    monkeypatch.setenv("SEO_PUBLIC_BASE_URL", "https://pixopixo.com")
    get_settings.cache_clear()
    row = _published(db)
    headers = {"X-Publish-Key": "test-publish-key"}
    with TestClient(app) as client:
        backfill = client.post(
            "/internal/v1/seo/backfill",
            headers=headers,
            json={"limit": 10},
        )
        assert backfill.status_code == 200
        assert backfill.json()["queued"] == [row.id]

        digest = source_hash(db.get(PublishedVideo, row.id))
        generated = client.put(
            f"/internal/v1/seo/experiences/{row.id}",
            headers=headers,
            json={
                "title": "Tap to Wake the City",
                "description": "Tap at the right moment to bring a quiet neon city to life.",
                "meta_title": "Tap to Wake the City | Pixopixo",
                "meta_description": (
                    "Play an AI-powered interactive video where one well-timed tap "
                    "brings a quiet neon city to life on Pixopixo."
                ),
                "tags": ["interactive video", "tap"],
                "interaction_types": ["tap"],
                "interaction_summary": "Tap once at the prompted moment to continue the scene.",
                "duration_seconds": 12.5,
                "width": 1080,
                "height": 1920,
                "thumbnail_url": "https://pixopixo.com/posters/work-seo-1.jpg",
                "source_hash": digest,
                "model": "qwen3.7-plus",
                "prompt_version": "seo-v1",
            },
        )
        assert generated.status_code == 200
        assert generated.json()["ai_title_written"] is True
        assert generated.json()["ai_description_written"] is True

        listing = client.get("/api/v1/public/seo/experiences")
        slug = generated.json()["slug"]
        detail = client.get(f"/api/v1/public/seo/experiences/{slug}")
        resolved = client.get(f"/api/v1/public/seo/resolve/{row.id}")

    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert slug.startswith("tap-to-wake-the-city-")
    assert listing.json()["items"][0]["title"] == "Tap to Wake the City"
    assert listing.json()["items"][0]["embed_url"].endswith("/?play=work-seo-1")
    assert detail.status_code == 200
    assert detail.json()["canonical_url"].endswith(f"/experiences/{slug}")
    assert resolved.json()["canonical_url"] == detail.json()["canonical_url"]


def test_pending_or_unpublished_metadata_is_not_indexable(db) -> None:
    row = _published(db, video_id="work-seo-hidden")
    headers = {"X-Publish-Key": "test-publish-key"}
    with TestClient(app) as client:
        queued = client.post(
            "/internal/v1/seo/backfill",
            headers=headers,
            json={"limit": 10},
        )
        listing = client.get("/api/v1/public/seo/experiences")
        detail = client.get("/api/v1/public/seo/resolve/work-seo-hidden")
    assert queued.status_code == 200
    assert row.id in queued.json()["queued"]
    assert listing.json()["total"] == 0
    assert detail.status_code == 404


def test_english_seo_copy_does_not_overwrite_meaningful_original_copy(db) -> None:
    row = _published(db, video_id="work-seo-original-copy")
    row.title = "轻触唤醒城市"
    row.description = "在提示出现时轻触屏幕，让沉睡的城市重新亮起来。"
    db.commit()
    original_title = row.title
    original_description = row.description
    headers = {"X-Publish-Key": "test-publish-key"}

    with TestClient(app) as client:
        queued = client.post(
            "/internal/v1/seo/backfill",
            headers=headers,
            json={"limit": 10},
        )
        assert queued.status_code == 200
        digest = source_hash(db.get(PublishedVideo, row.id))
        generated = client.put(
            f"/internal/v1/seo/experiences/{row.id}",
            headers=headers,
            json={
                "title": "Tap to Wake the City",
                "description": (
                    "Tap at the right moment to bring a quiet neon city back to life."
                ),
                "meta_title": "Tap to Wake the City | Pixopixo",
                "meta_description": (
                    "Play an interactive Pixopixo story where a well-timed tap "
                    "brings a quiet neon city back to life."
                ),
                "tags": ["interactive video", "tap"],
                "interaction_types": ["tap"],
                "interaction_summary": (
                    "Tap once at the prompted moment to continue the scene."
                ),
                "source_hash": digest,
                "model": "qwen-test",
                "prompt_version": "seo-v1",
            },
        )
        detail = client.get(
            f"/api/v1/public/seo/experiences/{generated.json()['slug']}"
        )

    persisted = db.get(PublishedVideo, row.id)
    assert generated.status_code == 200
    assert generated.json()["ai_title_written"] is False
    assert generated.json()["ai_description_written"] is False
    assert persisted.title == original_title
    assert persisted.description == original_description
    assert detail.status_code == 200
    assert detail.json()["title"] == "Tap to Wake the City"
    assert detail.json()["description"].startswith("Tap at the right moment")
