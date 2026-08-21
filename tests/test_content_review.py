from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.models import AnalyticsLog, PublishedVideo, User, VideoView
from app.protocol_video import RUNTIME_SPEC_VERSION, compile_runtime_spec


def test_pending_ugc_is_not_public_until_reviewed(db):
    db.add(User(user_id="creator", provider="email", subject="creator@example.test", source="app"))
    timeline = {"interactions": []}
    db.add(
        PublishedVideo(
            id="ugc-1", content_type="runtime", video_url="/media/ugc-1.mp4", timeline=timeline,
            runtime_spec=compile_runtime_spec(item_id="ugc-1", content_mode="single", source=timeline, video_url="/media/ugc-1.mp4"),
            runtime_spec_version=RUNTIME_SPEC_VERSION, version="1", user_id="creator", content_source="ugc",
            review_status="pending", created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    headers = {"X-Publish-Key": "test-publish-key"}
    with TestClient(app) as client:
        hidden = client.post("/video", json={"head": {"act": "video", "ver": "1.2"}, "body": {"limit": 10}}).json()
        listed = client.get("/internal/v1/content-management?source=ugc&status=pending", headers=headers).json()
        reviewed = client.post("/internal/v1/videos/ugc-1/review", headers=headers, json={"status": "approved", "reviewed_by": "ops"})
        public = client.post("/video", json={"head": {"act": "video", "ver": "1.2"}, "body": {"limit": 10}}).json()
    assert hidden["head"]["status"] == 100
    assert listed["total"] == 1
    assert reviewed.status_code == 200
    assert public["body"]["items"][0]["item_id"] == "ugc-1"


def test_video_metrics_are_aggregated_without_returning_raw_events(db):
    db.add(
        PublishedVideo(
            id="metric-1",
            content_type="runtime",
            video_url="/media/metric-1.mp4",
            timeline={"interactions": []},
            runtime_spec=compile_runtime_spec(
                item_id="metric-1",
                content_mode="single",
                source={"interactions": []},
                video_url="/media/metric-1.mp4",
            ),
            runtime_spec_version=RUNTIME_SPEC_VERSION,
            version="1",
        )
    )
    db.add_all(
        [
            VideoView(video_id="metric-1", user_id="viewer-a"),
            VideoView(video_id="metric-1", user_id="viewer-b"),
            AnalyticsLog(video_id="metric-1", token="viewer-a", data='{"event":"player_session_start"}'),
        ]
    )
    db.commit()

    with TestClient(app) as client:
        response = client.get(
            "/internal/v1/videos/metric-1/metrics",
            headers={"X-Publish-Key": "test-publish-key"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["video_id"] == "metric-1"
    assert payload["unique_view_count"] == 2
    assert payload["telemetry_event_count"] == 1
    assert payload["first_viewed_at"]
    assert payload["last_viewed_at"]
    assert payload["last_telemetry_at"]
    assert "items" not in payload


def test_non_distributed_content_remains_in_internal_operations_only(db):
    db.add(
        PublishedVideo(
            id="qa-vision-1",
            content_type="runtime",
            video_url="/media/qa-vision-1.mp4",
            timeline={"interactions": []},
            runtime_spec=compile_runtime_spec(
                item_id="qa-vision-1",
                content_mode="single",
                source={"interactions": []},
                video_url="/media/qa-vision-1.mp4",
            ),
            runtime_spec_version=RUNTIME_SPEC_VERSION,
            version="1",
            distribution_enabled=False,
        )
    )
    db.commit()

    headers = {"X-Publish-Key": "test-publish-key"}
    with TestClient(app) as client:
        feed = client.post(
            "/video",
            json={"head": {"act": "video", "ver": "1.2"}, "body": {"limit": 10}},
        ).json()
        detail = client.post(
            "/video_detail",
            json={
                "head": {"act": "video_detail", "ver": "1.2"},
                "body": {"video_id": "qa-vision-1"},
            },
        ).json()
        internal = client.get("/internal/v1/content-management/qa-vision-1", headers=headers)
        updated = client.post(
            "/internal/v1/videos/qa-vision-1/feed",
            headers=headers,
            json={"distribution_enabled": True},
        )
        visible = client.post(
            "/video",
            json={"head": {"act": "video", "ver": "1.2"}, "body": {"limit": 10}},
        ).json()

    assert feed["head"]["status"] == 100
    assert detail["head"]["status"] == 100
    assert internal.status_code == 200
    assert internal.json()["distribution_enabled"] is False
    assert updated.status_code == 200
    assert visible["body"]["items"][0]["item_id"] == "qa-vision-1"


def test_operator_can_edit_a_runtime_draft_without_making_it_public(db):
    db.add(
        PublishedVideo(
            id="draft-runtime-1",
            content_type="runtime",
            video_url="/media/draft-runtime-1.mp4",
            timeline={"interactions": []},
            runtime_spec=compile_runtime_spec(
                item_id="draft-runtime-1",
                content_mode="single",
                source={"interactions": []},
                video_url="/media/draft-runtime-1.mp4",
            ),
            runtime_spec_version=RUNTIME_SPEC_VERSION,
            version="draft-v1",
            review_status="approved",
        )
    )
    db.commit()

    headers = {"X-Publish-Key": "test-publish-key"}
    with TestClient(app) as client:
        edited = client.patch(
            "/internal/v1/content-management/draft-runtime-1",
            headers=headers,
            json={
                "title": "运营草稿",
                "review_status": "draft",
                "timeline": {"interactions": [{"gesture": "hold", "gate_at_ms": 1000}]},
            },
        )
        listed = client.get("/internal/v1/content-management?status=draft", headers=headers)
        public = client.post(
            "/video",
            json={"head": {"act": "video", "ver": "1.2"}, "body": {"limit": 10}},
        ).json()

    assert edited.status_code == 200
    assert edited.json()["title"] == "运营草稿"
    assert edited.json()["status"] == "draft"
    assert edited.json()["distribution_enabled"] is False
    assert edited.json()["runtime_spec"]["video"][0]["interactions"][0]["offset_time_ms"] == 1000
    assert listed.json()["total"] == 1
    assert public["head"]["status"] == 100
