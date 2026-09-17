from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from test_creator_story import _save_plan, _seed

from app.main import app
from app.models import CreatorCreation, CreatorVersion, CreditReservation


def test_drafts_are_owner_scoped_lightweight_keyset_pages(db, monkeypatch):
    creation_id, user_id, headers = _seed(db, monkeypatch)
    _seed(db, monkeypatch, suffix="other")
    stamp = datetime.now(timezone.utc)
    for index, status in enumerate(("ready", "failed", "cancelled", "published", "pending_review", "rejected", "deleted", "abandoned")):
        db.add(CreatorCreation(id=f"draft-{index}", user_id=user_id, status=status,
            source_mode="prompt", source_prompt=f"Scene {index}", experience_mode="auto",
            updated_at=stamp + timedelta(seconds=index), runtime_spec={"heavy": "not in list"}))
    db.commit()
    with TestClient(app) as client:
        assert client.get("/api/v1/creator/drafts").status_code == 401
        first = client.get("/api/v1/creator/drafts?limit=2", headers=headers)
        assert first.status_code == 200, first.text
        page = first.json()
        assert page["total"] == 4
        assert [item["status"] for item in page["items"]] == ["cancelled", "failed"]
        assert all("runtime_spec" not in item and "versions" not in item and "preview_url" not in item for item in page["items"])
        second = client.get("/api/v1/creator/drafts", params={"limit": 2, "cursor": page["next_cursor"]}, headers=headers).json()
        assert second["total"] == 4 and second["next_cursor"] is None
        assert {item["creation_id"] for item in second["items"]} == {creation_id, "draft-0"}
        source = next(item for item in second["items"] if item["creation_id"] == creation_id)
        assert source["title"] == "source.mp4" and source["initial_request_id"] == "prepare-main"
        assert source["duration_ms"] == 5000
        assert client.get("/api/v1/creator/drafts?cursor=invalid", headers=headers).status_code == 400
        assert client.get("/api/v1/creator/drafts", params={"cursor": page["next_cursor"]}, headers={"Authorization": "Bearer token-story-user-other"}).status_code == 400
        assert client.get("/api/v1/creator/drafts?limit=101", headers=headers).status_code == 422


def test_multiple_prepared_drafts_can_coexist_but_ai_admission_is_cross_draft(db, monkeypatch):
    creation_id, user_id, headers = _seed(db, monkeypatch, extra_credits=10)
    with TestClient(app) as client:
        response = client.post("/api/v1/creator/creations", headers=headers,
            json={"source_mode": "upload", "upload_id": "up_story_main", "brief": "New source", "request_id": "second-prepare", "defer_analysis": True})
        assert response.status_code == 202, response.text
        second_id = response.json()["creation_id"]
        assert second_id != creation_id
        # Simulate durable running analysis on the first creation.
        db.add(CreatorVersion(id="busy-v1", creation_id=creation_id, user_id=user_id,
            number=1, request_id="busy-request", brief="Analyze", status="running"))
        db.commit()
        before = db.query(CreditReservation).count()
        busy = client.post(f"/api/v1/creator/creations/{second_id}/versions", headers=headers,
            json={"brief": "Analyze the second", "request_id": "second-analyze"})
        assert busy.status_code == 409, busy.text
        assert busy.json()["detail"]["code"] == "CREATOR_BUSY"
        assert busy.json()["detail"]["creation_id"] == creation_id
        assert db.query(CreditReservation).count() == before
        assert db.get(CreatorCreation, second_id).status == "source_ready"
        # A new source-ready checkpoint still requires no AI and is permitted.
        third = client.post("/api/v1/creator/creations", headers=headers,
            json={"source_mode": "upload", "upload_id": "up_story_main", "brief": "Third", "request_id": "third-prepare", "defer_analysis": True})
        assert third.status_code == 202, third.text


def test_story_b_and_c_share_one_creation_but_block_other_creation(db, monkeypatch):
    creation_id, user_id, headers = _seed(db, monkeypatch, extra_credits=10)
    db.add(CreatorCreation(id="other-local", user_id=user_id, upload_id="up_story_main", status="source_ready"))
    db.commit()
    with TestClient(app) as client:
        _save_plan(client, creation_id, headers)
        generated = client.post(f"/api/v1/creator/creations/{creation_id}/story-generations", headers=headers,
            json={"revision": 1, "request_id": "bc", "targets": ["B", "C"], "operation": "generate"})
        assert generated.status_code == 202, generated.text
        other = client.post("/api/v1/creator/creations/other-local/versions", headers=headers,
            json={"brief": "Analyze", "request_id": "other-analyze"})
        assert other.status_code == 409 and other.json()["detail"]["creation_id"] == creation_id
        deleted = client.delete(f"/api/v1/creator/creations/{creation_id}", headers=headers)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["status"] == "abandoned"
        assert creation_id not in {item["creation_id"] for item in client.get("/api/v1/creator/drafts", headers=headers).json()["items"]}


@pytest.mark.parametrize("status", ["published", "pending_review", "rejected", "deleted"])
def test_deleting_drafts_does_not_delete_submitted_or_published_work(db, monkeypatch, status):
    creation_id, _, headers = _seed(db, monkeypatch)
    db.get(CreatorCreation, creation_id).status = status
    db.commit()
    with TestClient(app) as client:
        assert client.delete(f"/api/v1/creator/creations/{creation_id}", headers=headers).status_code == 409
    db.expire_all()
    assert db.get(CreatorCreation, creation_id).status == status


def test_abandoned_draft_cannot_be_resurrected_by_retry(db, monkeypatch):
    creation_id, user_id, headers = _seed(db, monkeypatch)
    db.add(CreatorVersion(id="retry-deleted", creation_id=creation_id, user_id=user_id,
        number=1, request_id="deleted-task", brief="Analyze", status="queued"))
    db.commit()
    with TestClient(app) as client:
        assert client.delete(f"/api/v1/creator/creations/{creation_id}", headers=headers).status_code == 200
        assert client.post("/api/v1/creator/versions/retry-deleted/retry", headers=headers).status_code == 409
        assert client.post(f"/api/v1/creator/creations/{creation_id}/retry", headers=headers).status_code == 409
        assert client.post(f"/api/v1/creator/creations/{creation_id}/versions", headers=headers,
            json={"brief": "Analyze", "request_id": "should-not-recreate"}).status_code == 409
    db.expire_all()
    assert db.get(CreatorCreation, creation_id).status == "abandoned"
    assert db.get(CreatorVersion, "retry-deleted").status == "cancelled"
