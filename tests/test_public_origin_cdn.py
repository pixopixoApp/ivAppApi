from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.cdn_cache import (
    AlibabaCdnProvider,
    CdnSubmission,
    CdnTaskResult,
    active_public_urls,
    enqueue_prefetch,
    enqueue_refresh,
    process_once,
)
from app.cdn_publication import CdnPublicationError, stage_publication_gate
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
from app.private_cdn import sign_private_media_url
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


def test_private_media_url_uses_short_lived_cdn_type_a_signature(monkeypatch) -> None:
    settings = _settings(monkeypatch)
    monkeypatch.setenv("PRIVATE_MEDIA_CDN_BASE_URL", "https://private-video.pixopixo.cn")
    monkeypatch.setenv("PRIVATE_MEDIA_CDN_AUTH_KEY", "cdn-secret")
    monkeypatch.setenv("PRIVATE_MEDIA_CDN_AUTH_UID", "0")
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr("app.private_cdn.time.time", lambda: 1_700_000_000)
    monkeypatch.setattr("app.private_cdn.secrets.token_hex", lambda _length: "nonce")
    key = "ivapp-media/v1/private/creator-sources/aa/source.mp4"
    timestamp = 1_700_000_000
    path = f"/{key}"
    digest = hashlib.md5(
        f"{path}-{timestamp}-nonce-0-cdn-secret".encode()
    ).hexdigest()

    assert sign_private_media_url(settings, key=key, expires_seconds=120) == (
        f"https://private-video.pixopixo.cn{path}"
        f"?auth_key={timestamp}-nonce-0-{digest}"
    )


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

        def status(self, task_id):
            assert task_id == "task-1"
            return CdnTaskResult(state="succeeded")

    provider = Provider()
    assert process_once(db, settings, provider=provider) == 1
    row = db.query(CdnCacheJob).one()
    assert row.state == "pending"
    assert row.attempts == 1
    assert row.provider_task_id == "task-1"
    assert provider.calls == [("prefetch", [url])]

    row.next_attempt_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    db.add(row)
    db.commit()
    assert process_once(db, settings, provider=provider) == 1
    db.refresh(row)
    assert row.state == "succeeded"
    assert row.attempts == 1

    with pytest.raises(PublicOriginError):
        enqueue_refresh(db, settings, [url + "?v=2"])
    with pytest.raises(PublicOriginError):
        enqueue_refresh(
            db,
            settings,
            [f"{OLD_OSS}{PUBLIC_PREFIX}runtime/work/pub/single.mp4"],
        )


@pytest.mark.parametrize("provider_status", ["Failed", "Timeout", "Canceled"])
def test_provider_terminal_failures_do_not_poll_forever(provider_status) -> None:
    task = SimpleNamespace(status=provider_status, description="provider stopped")
    response = SimpleNamespace(body=SimpleNamespace(tasks=[task]))

    class Client:
        def describe_refresh_task_by_id(self, request):
            assert request.task_id == "task-terminal"
            return response

    provider = AlibabaCdnProvider.__new__(AlibabaCdnProvider)
    provider._client = Client()
    result = provider.status("task-terminal")

    assert result.state == "failed"
    assert result.error_message == "provider stopped"


def test_superseded_publication_cannot_be_reopened(monkeypatch, db) -> None:
    _settings(monkeypatch)
    first_url = f"{CDN}{PUBLIC_PREFIX}runtime/work/first/single.mp4"
    second_url = f"{CDN}{PUBLIC_PREFIX}runtime/work/second/single.mp4"
    stage_publication_gate(
        db,
        video_id="work",
        publication_id="first",
        urls=[first_url],
    )
    db.commit()
    stage_publication_gate(
        db,
        video_id="work",
        publication_id="second",
        urls=[second_url],
    )
    db.commit()
    db.expire_all()

    with pytest.raises(CdnPublicationError, match="superseded"):
        stage_publication_gate(
            db,
            video_id="work",
            publication_id="first",
            urls=[first_url],
        )


def test_publication_keeps_old_url_until_provider_confirms_prefetch(monkeypatch, db) -> None:
    settings = _settings(monkeypatch)
    now = datetime.now(timezone.utc)
    old_url = f"{CDN}{PUBLIC_PREFIX}runtime/work/old/single.mp4"
    new_url = f"{CDN}{PUBLIC_PREFIX}runtime/work/new/single.mp4"
    source = {"interactions": []}
    new_spec = compile_runtime_spec(
        item_id="work",
        content_mode="single",
        source=source,
        video_url=new_url,
    )
    row = PublishedVideo(
        id="work",
        content_type="runtime",
        video_url=old_url,
        timeline=source,
        runtime_spec=compile_runtime_spec(
            item_id="work",
            content_mode="single",
            source=source,
            video_url=old_url,
        ),
        runtime_spec_version=RUNTIME_SPEC_VERSION,
        html_url=None,
        bridge_version=None,
        required_capabilities=[],
        active_publication_id="old",
        version="v1",
        title="Work",
        description="",
        user_id=None,
        content_mode="single",
        review_status="approved",
        distribution_enabled=True,
        cdn_ready=True,
        created_at=now,
        updated_at=now,
    )
    creation = CreatorCreation(
        id="work",
        user_id="user",
        upload_id="upload",
        status="published",
        runtime_spec=row.runtime_spec,
        runtime_spec_version=RUNTIME_SPEC_VERSION,
        created_at=now,
        updated_at=now,
    )
    db.add_all([row, creation])
    enqueue_prefetch(db, settings, [new_url])
    gate = stage_publication_gate(
        db,
        video_id=row.id,
        publication_id="new",
        urls=[new_url],
        staged_payload={
            "video_url": new_url,
            "runtime_spec": new_spec,
            "runtime_spec_version": RUNTIME_SPEC_VERSION,
            "active_publication_id": "new",
            "version": "v2",
        },
    )
    db.commit()

    class Provider:
        def __init__(self) -> None:
            self.complete = False

        def submit(self, operation, urls):
            assert operation == "prefetch"
            assert urls == [new_url]
            return CdnSubmission(task_id="task-new", request_id="request-new")

        def status(self, task_id):
            assert task_id == "task-new"
            return CdnTaskResult(
                state="succeeded" if self.complete else "pending"
            )

    provider = Provider()
    assert process_once(db, settings, provider=provider) == 1
    db.refresh(row)
    db.refresh(gate)
    assert row.video_url == old_url
    assert row.active_publication_id == "old"
    assert gate.state == "warming"
    db.refresh(creation)
    assert creation.runtime_spec["video"][0]["video"] == old_url

    job = db.query(CdnCacheJob).one()
    job.next_attempt_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    db.add(job)
    db.commit()
    assert process_once(db, settings, provider=provider) == 1
    db.refresh(row)
    db.refresh(gate)
    assert row.video_url == old_url
    assert gate.state == "warming"

    provider.complete = True
    job.next_attempt_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    db.add(job)
    db.commit()
    assert process_once(db, settings, provider=provider) == 2
    db.refresh(row)
    db.refresh(gate)
    assert row.video_url == new_url
    assert row.active_publication_id == "new"
    assert row.version == "v2"
    assert row.cdn_ready is True
    assert gate.state == "active"
    db.refresh(creation)
    assert creation.runtime_spec["video"][0]["video"] == new_url


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
