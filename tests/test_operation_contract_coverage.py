from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.models import CreatorAccessGrant, PublishedVideo, User, UserToken
from app.protocol_video import compile_runtime_spec

PUBLISH_HEADERS = {"X-Publish-Key": "test-publish-key"}
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _seed_identity(db, user_id: str, *, creator: bool = False) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    token = f"contract-token-{user_id}"
    db.add(
        User(
            user_id=user_id,
            provider="email",
            subject=f"{user_id}@example.test",
            nickname=user_id,
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
    if creator:
        db.add(
            CreatorAccessGrant(
                user_id=user_id,
                source="contract_test",
                granted_at=now,
            )
        )
    db.commit()
    return {"Authorization": f"Bearer {token}"}


def _assert_safe(response, *statuses: int) -> None:
    assert response.status_code in statuses, response.text
    assert response.status_code < 500, response.text


def test_high_side_effect_operations_have_safe_contract_coverage(db) -> None:
    applicant_auth = _seed_identity(db, "contract-applicant")
    creator_auth = _seed_identity(db, "contract-creator", creator=True)
    timeline = {"interactions": []}
    db.add(
        PublishedVideo(
            id="contract-destructive-video",
            content_type="runtime",
            video_url="/media/contract-destructive-video.mp4",
            timeline=timeline,
            runtime_spec=compile_runtime_spec(
                item_id="contract-destructive-video",
                content_mode="single",
                source=timeline,
                video_url="/media/contract-destructive-video.mp4",
            ),
            runtime_spec_version="1.1",
            version="1",
            user_id="contract-creator",
        )
    )
    db.commit()

    media_root = Path("/tmp/ivapp-pytest-media")
    (media_root / "avatars").mkdir(parents=True, exist_ok=True)
    (media_root / "avatars" / "contract.png").write_bytes(PNG_1X1)
    (media_root / "contract-story").mkdir(parents=True, exist_ok=True)
    (media_root / "contract-story" / "clip.mp4").write_bytes(b"contract-story")
    (media_root / "contract-single.mp4").write_bytes(b"contract-single")

    with TestClient(app) as client:
        _assert_safe(
            client.post(
                "/api/v1/creator/applications",
                headers=applicant_auth,
                json={"message": "contract coverage"},
            ),
            200,
        )
        _assert_safe(
            client.post(
                "/internal/v1/creator/applications/contract-applicant/decision",
                headers=PUBLISH_HEADERS,
                json={"status": "approved"},
            ),
            200,
        )

        for method, path in (
            ("DELETE", "/api/v1/creator/creations/missing-contract"),
            ("POST", "/api/v1/creator/creations/missing-contract/cancel"),
            ("POST", "/api/v1/creator/creations/missing-contract/retry"),
            ("POST", "/api/v1/creator/versions/missing-contract/cancel"),
            ("POST", "/api/v1/creator/versions/missing-contract/retry"),
        ):
            _assert_safe(client.request(method, path, headers=creator_auth), 404)

        _assert_safe(client.get("/api/v1/creator/previews/missing-contract"), 404)
        _assert_safe(
            client.post(
                "/api/v1/creator/uploads",
                headers=creator_auth,
                files={"file": ("bad.txt", b"not-video", "text/plain")},
            ),
            400,
            410,
        )
        _assert_safe(
            client.get(
                "/api/v1/creator/uploads/missing-contract/media",
                headers=creator_auth,
            ),
            400,
            404,
        )

        _assert_safe(
            client.post(
                "/deactivate",
                headers=applicant_auth,
                json={
                    "head": {"act": "deactivate", "ver": "1.2"},
                    "body": {"code": "000000"},
                },
            ),
            200,
        )
        _assert_safe(
            client.post(
                "/google_login",
                json={
                    "head": {"act": "google_login", "ver": "1.2"},
                    "body": {"id_token": "invalid-contract-token"},
                },
            ),
            200,
        )

        _assert_safe(
            client.post(
                "/internal/v1/html-imports/inspect",
                headers=PUBLISH_HEADERS,
                json={"source_object_id": "missing-contract"},
            ),
            400,
        )
        _assert_safe(
            client.post(
                "/internal/v1/html-imports/prepare",
                headers=PUBLISH_HEADERS,
                json={
                    "source_object_id": "missing-contract",
                    "item_id": "contract-html",
                    "title": "Contract HTML",
                    "user_id": "contract-creator",
                },
            ),
            400,
        )
        local_import = {
            "import_id": "him_contract",
            "source_sha256": "0" * 64,
            "source_bytes": 1,
        }
        _assert_safe(
            client.post(
                "/internal/v1/html-imports/prepare-local",
                headers=PUBLISH_HEADERS,
                json=local_import
                | {
                    "attempt_id": "0" * 32,
                    "item_id": "contract-html",
                    "title": "Contract HTML",
                    "user_id": "contract-creator",
                },
            ),
            400,
        )
        _assert_safe(
            client.post(
                "/internal/v1/html-imports/archive-local",
                headers=PUBLISH_HEADERS,
                json=local_import | {"filename": "source.zip"},
            ),
            400,
        )

        _assert_safe(
            client.post(
                "/internal/v1/media/objects/missing-contract/download-url",
                headers=PUBLISH_HEADERS,
            ),
            404,
        )
        _assert_safe(
            client.post(
                "/internal/v1/media/retire-legacy-json",
                headers=PUBLISH_HEADERS,
                json={
                    "object_ids": ["missing-contract"],
                    "reason": "run_json_backfill_v2",
                },
            ),
            404,
        )
        _assert_safe(
            client.post(
                "/internal/v1/media/upload-sessions",
                headers=PUBLISH_HEADERS,
                json={
                    "purpose": "admin_source",
                    "target_id": "contract-target",
                    "objects": [
                        {
                            "client_ref": "source",
                            "filename": "source.bin",
                            "content_type": "application/octet-stream",
                            "size_bytes": 1,
                            "sha256": "0" * 64,
                        }
                    ],
                },
            ),
            201,
            400,
        )
        _assert_safe(
            client.post(
                "/internal/v1/media/upload-sessions/missing-contract/finalize",
                headers=PUBLISH_HEADERS,
                json={"manifest_hash": "0" * 64},
            ),
            400,
            404,
        )
        _assert_safe(
            client.post(
                "/internal/v1/publish-assets",
                headers=PUBLISH_HEADERS,
                json={
                    "video_id": "contract-assets",
                    "version": "1",
                    "user_id": "contract-creator",
                    "timeline": {"interactions": []},
                    "assets": [
                        {"role": "single", "media_object_id": "missing-contract"}
                    ],
                },
            ),
            409,
        )
        _assert_safe(
            client.post(
                "/internal/v1/publish-cover",
                headers=PUBLISH_HEADERS,
                files={"file": ("cover.png", PNG_1X1, "image/png")},
            ),
            200,
        )

        _assert_safe(
            client.post(
                "/internal/v1/users",
                headers=PUBLISH_HEADERS,
                json={
                    "user_id": "contract-admin-user",
                    "subject": "contract-admin-user@example.test",
                    "nickname": "Contract Admin",
                },
            ),
            200,
        )
        _assert_safe(
            client.post(
                "/internal/v1/users/contract-admin-user/avatar",
                headers=PUBLISH_HEADERS,
                files={"file": ("avatar.png", PNG_1X1, "image/png")},
            ),
            200,
        )
        _assert_safe(
            client.post(
                "/internal/v1/users/contract-admin-user/deactivate",
                headers=PUBLISH_HEADERS,
            ),
            200,
        )

        _assert_safe(
            client.post(
                "/internal/v1/videos/contract-destructive-video/runtime-spec/recompile",
                headers=PUBLISH_HEADERS,
            ),
            200,
        )
        _assert_safe(
            client.post(
                "/internal/v1/videos/contract-destructive-video/trash",
                headers=PUBLISH_HEADERS,
            ),
            200,
        )
        _assert_safe(
            client.post(
                "/internal/v1/videos/contract-destructive-video/restore",
                headers=PUBLISH_HEADERS,
            ),
            200,
        )
        _assert_safe(
            client.delete(
                "/internal/v1/videos/contract-destructive-video",
                headers=PUBLISH_HEADERS,
            ),
            200,
        )

        _assert_safe(client.get("/media/avatars/contract.png"), 200)
        _assert_safe(client.get("/media/contract-story/clip.mp4"), 200)
        _assert_safe(client.get("/media/contract-single.mp4"), 200)
