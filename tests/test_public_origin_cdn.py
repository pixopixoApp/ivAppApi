from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.cdn_cache import (
    CdnSubmission,
    active_public_urls,
    enqueue_prefetch,
    enqueue_refresh,
    process_once,
)
from app.config import get_settings
from app.models import (
    CdnCacheJob,
    CreatorCreation,
    CreatorVersion,
    MediaObject,
    PublishedMediaAsset,
    PublishedVideo,
    User,
)
from app.protocol_video import RUNTIME_SPEC_VERSION, compile_runtime_spec
from app.public_origin import (
    PublicOriginError,
    canonicalize_public_payload,
    canonicalize_public_url,
)
from app.public_origin_migration import migrate_public_origins

OLD_OSS = "https://pixopixo-us.oss-us-east-1.aliyuncs.com"
OLD_API = "https://api.pixopixo.cn"
CDN = "https://video.pixopixo.cn"
PUBLIC_PREFIX = "/ivapp-media/v1/public/"


def _settings(monkeypatch, *, cdn_enabled: bool = True):
    values = {
        "MEDIA_STORAGE_MODE": "oss",
        "ALIYUN_OSS_REGION": "us-east-1",
        "ALIYUN_OSS_BUCKET": "test-bucket",
        "ALIYUN_OSS_ACCESS_KEY_ID": "test-id",
        "ALIYUN_OSS_ACCESS_KEY_SECRET": "test-secret",
        "ALIYUN_OSS_PUBLIC_BASE_URL": CDN,
        "PUBLIC_MEDIA_LEGACY_ORIGINS": f"{OLD_OSS},{OLD_API}",
        "OSS_ROOT_PREFIX": "ivapp-media/v1",
        "CDN_CACHE_ENABLED": "true" if cdn_enabled else "false",
        "CDN_PREFETCH_ON_PUBLISH": "true",
        "CDN_DOMAIN": "video.pixopixo.cn",
        "CDN_WORKER_BATCH_SIZE": "50",
        "CDN_WORKER_MAX_ATTEMPTS": "3",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


def test_public_url_canonicalizer_is_allowlisted_and_signature_safe(monkeypatch) -> None:
    settings = _settings(monkeypatch)
    old_video = f"{OLD_OSS}{PUBLIC_PREFIX}runtime/work/pub/single.mp4"
    old_html = f"{OLD_API}{PUBLIC_PREFIX}html/work/version/index.html"
    assert canonicalize_public_url(settings, old_video) == (
        f"{CDN}{PUBLIC_PREFIX}runtime/work/pub/single.mp4"
    )
    assert canonicalize_public_url(settings, old_html) == (
        f"{CDN}{PUBLIC_PREFIX}html/work/version/index.html"
    )
    assert canonicalize_public_url(settings, f"{CDN}{PUBLIC_PREFIX}avatars/a.png") == (
        f"{CDN}{PUBLIC_PREFIX}avatars/a.png"
    )
    assert canonicalize_public_url(
        settings,
        f"{OLD_OSS}{PUBLIC_PREFIX}runtime/work.mp4?Expires=1&Signature=signed",
    ).startswith(OLD_OSS)
    assert canonicalize_public_url(
        settings,
        f"{OLD_OSS}/ivapp-media/v1/private/source.mp4",
    ).startswith(OLD_OSS)
    assert canonicalize_public_url(
        settings,
        "https://third-party.example/ivapp-media/v1/public/video.mp4",
    ).startswith("https://third-party.example")

    payload = {"video": [{"video": old_video}], "external": "https://example.test/a"}
    replacement = canonicalize_public_payload(settings, payload)
    assert replacement["video"][0]["video"].startswith(CDN)
    assert replacement["external"] == payload["external"]
    assert payload["video"][0]["video"].startswith(OLD_OSS)


def test_cache_outbox_deduplicates_and_worker_marks_provider_task(monkeypatch, db) -> None:
    settings = _settings(monkeypatch)
    url = f"{CDN}{PUBLIC_PREFIX}runtime/work/pub/single.mp4"
    jobs = enqueue_prefetch(db, settings, [url, url])
    db.commit()
    assert len(jobs) == 1

    class Provider:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[str]]] = []

        def submit(self, operation, urls):
            self.calls.append((operation, urls))
            return CdnSubmission(task_id="task-1", request_id="request-1")

    provider = Provider()
    assert process_once(db, settings, provider=provider) == 1
    row = db.query(CdnCacheJob).one()
    assert row.state == "succeeded"
    assert row.attempts == 1
    assert row.provider_task_id == "task-1"
    assert provider.calls == [("prefetch", [url])]

    with pytest.raises(PublicOriginError):
        enqueue_refresh(db, settings, [url + "?v=2"])
    with pytest.raises(PublicOriginError):
        enqueue_refresh(
            db,
            settings,
            [f"{OLD_OSS}{PUBLIC_PREFIX}runtime/work/pub/single.mp4"],
        )


def test_active_manifest_and_atomic_migration_use_media_bindings(monkeypatch, db) -> None:
    settings = _settings(monkeypatch)
    now = datetime.now(timezone.utc)
    runtime_key = "ivapp-media/v1/public/runtime/work/pub/single.mp4"
    avatar_key = "ivapp-media/v1/public/avatars/aa/avatar.png"
    runtime_media = MediaObject(
        id="mo_runtime",
        purpose="runtime_public",
        origin="server_copy",
        visibility="public",
        state="ready",
        staging_key=runtime_key,
        object_key=runtime_key,
        original_filename="single.mp4",
        content_type="video/mp4",
        size_bytes=10,
        sha256="a" * 64,
        etag="",
        extra_json={"legacy": f"{OLD_OSS}{PUBLIC_PREFIX}runtime/work/pub/single.mp4"},
        verified_at=now,
        created_at=now,
    )
    avatar_media = MediaObject(
        id="mo_avatar",
        purpose="avatar",
        origin="server_upload",
        visibility="public",
        state="ready",
        staging_key=avatar_key,
        object_key=avatar_key,
        original_filename="avatar.png",
        content_type="image/png",
        size_bytes=5,
        sha256="b" * 64,
        etag="",
        extra_json={},
        verified_at=now,
        created_at=now,
    )
    old_url = f"{OLD_OSS}/{runtime_key}"
    source = {"interactions": []}
    old_spec = compile_runtime_spec(
        item_id="work",
        content_mode="single",
        source=source,
        video_url=old_url,
    )
    video = PublishedVideo(
        id="work",
        content_type="runtime",
        video_url=old_url,
        timeline=source,
        runtime_spec=old_spec,
        runtime_spec_version=RUNTIME_SPEC_VERSION,
        html_url=None,
        bridge_version=None,
        required_capabilities=[],
        active_publication_id="pub",
        version="v1",
        title="Work",
        description="",
        user_id="user",
        content_mode="single",
        review_status="approved",
        distribution_enabled=True,
        created_at=now,
        updated_at=now,
    )
    user = User(
        user_id="user",
        provider="email",
        subject="user@example.test",
        enabled=True,
        nickname="User",
        avatar_url=f"{OLD_OSS}/{avatar_key}",
        avatar_media_object_id=avatar_media.id,
        source="app",
        created_at=now,
    )
    creation = CreatorCreation(
        id="creation",
        user_id="user",
        upload_id="upload",
        status="ready",
        runtime_spec=old_spec,
        created_at=now,
        updated_at=now,
    )
    version = CreatorVersion(
        id="version",
        creation_id="creation",
        user_id="user",
        number=1,
        request_id="request",
        status="ready",
        runtime_spec=old_spec,
        created_at=now,
        updated_at=now,
    )
    db.add_all(
        [
            runtime_media,
            avatar_media,
            video,
            user,
            creation,
            version,
            PublishedMediaAsset(
                video_id=video.id,
                publication_id="pub",
                version="v1",
                role="single",
                clip_id="",
                media_object_id=runtime_media.id,
                created_at=now,
            ),
        ]
    )
    db.commit()

    manifest = active_public_urls(db, settings)
    assert manifest == [f"{CDN}/{avatar_key}", f"{CDN}/{runtime_key}"]

    dry_run = migrate_public_origins(db, settings, apply=False)
    assert dry_run.changed_count >= 4
    db.refresh(video)
    assert video.video_url == old_url

    report = migrate_public_origins(db, settings, apply=True)
    assert not report.failures
    db.refresh(video)
    db.refresh(user)
    db.refresh(creation)
    assert video.video_url == f"{CDN}/{runtime_key}"
    assert video.runtime_spec["video"][0]["video"] == f"{CDN}/{runtime_key}"
    assert user.avatar_url == f"{CDN}/{avatar_key}"
    assert creation.runtime_spec["video"][0]["video"].startswith(CDN)

    verified = migrate_public_origins(db, settings, apply=False, verify=True)
    assert verified.changed_count == 0
    assert not verified.failures
