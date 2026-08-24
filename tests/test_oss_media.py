from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.media_api import DirectUploadObjectRequest, InternalUploadSessionRequest
from app.media_cache import object_path
from app.media_service import (
    MediaServiceError,
    create_upload_session,
    finalize_upload_session,
)
from app.models import (
    MediaObject,
    MediaUploadSession,
    PublishedMediaAsset,
    PublishedVideo,
)
from app.oss_storage import (
    OssImmutableConflictError,
    OssObjectMetadata,
    OssObjectNotFoundError,
    OssStorageError,
    copy_object,
    create_post_upload,
    download_file,
    head_object,
    validate_oss_config,
)
from app.publication_service import load_published_runtime_urls
from scripts.migrate_media_to_oss import audit_database_references


def _oss_settings(monkeypatch):
    monkeypatch.setenv("MEDIA_STORAGE_MODE", "oss")
    monkeypatch.setenv("ALIYUN_OSS_REGION", "cn-beijing")
    monkeypatch.setenv("ALIYUN_OSS_BUCKET", "existing-bucket")
    monkeypatch.setenv("ALIYUN_OSS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("ALIYUN_OSS_ACCESS_KEY_SECRET", "test-secret")
    monkeypatch.setenv("ALIYUN_OSS_PUBLIC_BASE_URL", "https://cdn.test")
    monkeypatch.setenv("OSS_ROOT_PREFIX", "ivapp-media/v1")
    get_settings.cache_clear()
    return get_settings()


def test_internal_backup_contract_accepts_creator_original_and_playable() -> None:
    request = InternalUploadSessionRequest(
        purpose="creator_video",
        target_id="upload_123",
        objects=[
            DirectUploadObjectRequest(
                client_ref="playable.mp4",
                filename="playable.mp4",
                content_type="video/mp4",
                size_bytes=1234,
                sha256="a" * 64,
            )
        ],
    )
    assert request.purpose == "creator_video"


def test_post_policy_is_exact_key_size_type_and_never_exposes_secret(monkeypatch) -> None:
    settings = _oss_settings(monkeypatch)
    url, fields = create_post_upload(
        settings,
        key="ivapp-media/v1/ingress/client/session/object.mp4",
        content_type="video/mp4",
        size_bytes=1234,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        extra_fields={"x-oss-meta-sha256": "a" * 64},
    )
    policy = json.loads(base64.b64decode(fields["policy"]))
    assert url.startswith("https://existing-bucket.oss-cn-beijing.aliyuncs.com")
    assert ["eq", "$key", "ivapp-media/v1/ingress/client/session/object.mp4"] in policy["conditions"]
    assert ["content-length-range", 1234, 1234] in policy["conditions"]
    assert ["eq", "$x-oss-content-type", "video/mp4"] in policy["conditions"]
    assert "test-secret" not in json.dumps(fields)


def test_archived_motioncue_oss_environment_names_remain_reusable(monkeypatch) -> None:
    for name in (
        "ALIYUN_OSS_REGION",
        "ALIYUN_OSS_BUCKET",
        "ALIYUN_OSS_ACCESS_KEY_ID",
        "ALIYUN_OSS_ACCESS_KEY_SECRET",
        "ALIYUN_OSS_PUBLIC_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MOTIONCUE_ALIYUN_OSS_REGION", "cn-shanghai")
    monkeypatch.setenv("MOTIONCUE_ALIYUN_OSS_BUCKET", "legacy-bucket")
    monkeypatch.setenv("MOTIONCUE_ALIYUN_OSS_ACCESS_KEY_ID", "legacy-id")
    monkeypatch.setenv("MOTIONCUE_ALIYUN_OSS_ACCESS_KEY_SECRET", "legacy-secret")
    monkeypatch.setenv(
        "MOTIONCUE_ALIYUN_OSS_PUBLIC_BASE_URL",
        "https://legacy-cdn.test",
    )
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.aliyun_oss_region == "cn-shanghai"
    assert settings.aliyun_oss_bucket == "legacy-bucket"
    assert settings.aliyun_oss_access_key_id == "legacy-id"
    assert settings.aliyun_oss_public_base_url == "https://legacy-cdn.test"


def test_oss_public_base_must_be_a_pathless_https_origin(monkeypatch) -> None:
    _oss_settings(monkeypatch)
    monkeypatch.setenv(
        "ALIYUN_OSS_PUBLIC_BASE_URL",
        "https://cdn.test/old-folder",
    )
    get_settings.cache_clear()
    with pytest.raises(OssStorageError, match="HTTPS origin"):
        validate_oss_config(get_settings())


@pytest.mark.parametrize(
    "legacy_prefix",
    ["motioncue/new", "works/new", "pixo/releases", "ivapp-media"],
)
def test_oss_root_requires_the_new_versioned_namespace(
    monkeypatch,
    legacy_prefix: str,
) -> None:
    _oss_settings(monkeypatch)
    monkeypatch.setenv("OSS_ROOT_PREFIX", legacy_prefix)
    get_settings.cache_clear()
    with pytest.raises(OssStorageError, match="ivapp-media/<version>"):
        validate_oss_config(get_settings())


def test_oss_adapter_exposes_typed_not_found_and_immutable_conflict(
    monkeypatch,
) -> None:
    settings = _oss_settings(monkeypatch)

    class NoSuchKey(Exception):
        pass

    class ServerError(Exception):
        def __init__(self, *, status: int, code: str):
            self.status = status
            self.code = code

    class Bucket:
        @staticmethod
        def head_object(_key: str):
            raise NoSuchKey

        @staticmethod
        def copy_object(*_args, **_kwargs):
            raise ServerError(status=400, code="ObjectAlreadyExists")

    fake_oss2 = SimpleNamespace(
        exceptions=SimpleNamespace(NoSuchKey=NoSuchKey, ServerError=ServerError)
    )
    monkeypatch.setattr("app.oss_storage._oss2", lambda: fake_oss2)
    monkeypatch.setattr("app.oss_storage._bucket", lambda _settings: Bucket())

    with pytest.raises(OssObjectNotFoundError):
        head_object(settings, key="ivapp-media/v1/private/missing.bin")
    with pytest.raises(OssImmutableConflictError):
        copy_object(
            settings,
            source_key="ivapp-media/v1/private/source.bin",
            target_key="ivapp-media/v1/private/target.bin",
            content_type="application/octet-stream",
            public=False,
        )


def test_download_file_retries_official_resumable_download_after_timeout(
    monkeypatch,
    tmp_path,
) -> None:
    settings = _oss_settings(monkeypatch)
    payload = b"0123456789abcdef"
    calls: list[dict] = []

    class Bucket:
        bucket_name = "existing-bucket"

    class Store:
        def __init__(self, *, root: str, dir: str):
            self.root = root
            self.dir = dir

        @staticmethod
        def make_store_key(_bucket: str, _key: str, _filename: str) -> str:
            return "checkpoint"

        @staticmethod
        def delete(_key: str) -> None:
            raise FileNotFoundError

    def resumable_download(_bucket, key: str, filename: str, **kwargs):
        calls.append({"key": key, "filename": filename, **kwargs})
        if len(calls) == 1:
            raise ConnectionError("simulated OSS response timeout")
        Path(filename).write_bytes(payload)

    fake_oss2 = SimpleNamespace(
        ResumableDownloadStore=Store,
        resumable_download=resumable_download,
    )

    monkeypatch.setattr(
        "app.oss_storage.head_object",
        lambda _settings, *, key: OssObjectMetadata(
            size_bytes=len(payload),
            content_type="application/octet-stream",
            etag="stable-etag",
            headers={},
        ),
    )
    monkeypatch.setattr("app.oss_storage._bucket", lambda _settings: Bucket())
    monkeypatch.setattr("app.oss_storage._oss2", lambda: fake_oss2)

    destination = tmp_path / "download.bin"
    download_file(
        settings,
        key="ivapp-media/v1/private/download.bin",
        path=destination,
    )

    assert destination.read_bytes() == payload
    assert len(calls) == 2
    assert calls[-1]["multiget_threshold"] == 8 * 1024 * 1024
    assert calls[-1]["part_size"] == 4 * 1024 * 1024
    assert calls[-1]["num_threads"] == 4


def test_download_file_removes_partial_file_after_retry_exhaustion(
    monkeypatch,
    tmp_path,
) -> None:
    settings = _oss_settings(monkeypatch)
    attempts = 0

    class Bucket:
        bucket_name = "existing-bucket"

    class Store:
        def __init__(self, *, root: str, dir: str):
            self.root = root
            self.dir = dir

        @staticmethod
        def make_store_key(_bucket: str, _key: str, _filename: str) -> str:
            return "checkpoint"

        @staticmethod
        def delete(_key: str) -> None:
            raise FileNotFoundError

    def resumable_download(_bucket, _key: str, _filename: str, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise ConnectionError("simulated persistent timeout")

    fake_oss2 = SimpleNamespace(
        ResumableDownloadStore=Store,
        resumable_download=resumable_download,
    )

    monkeypatch.setattr(
        "app.oss_storage.head_object",
        lambda _settings, *, key: OssObjectMetadata(
            size_bytes=16,
            content_type="application/octet-stream",
            etag="stable-etag",
            headers={},
        ),
    )
    monkeypatch.setattr("app.oss_storage._bucket", lambda _settings: Bucket())
    monkeypatch.setattr("app.oss_storage._oss2", lambda: fake_oss2)

    destination = tmp_path / "download.bin"
    with pytest.raises(OssStorageError, match="after retrying"):
        download_file(
            settings,
            key="ivapp-media/v1/private/download.bin",
            path=destination,
        )

    assert attempts == 6
    assert not destination.exists()
    assert not (tmp_path / ".download.bin.pixo-part").exists()


def test_internal_upload_is_verified_then_copied_to_dedicated_prefix(
    db,
    monkeypatch,
) -> None:
    settings = _oss_settings(monkeypatch)
    payload = b"verified-runtime-video"
    digest = hashlib.sha256(payload).hexdigest()
    copied: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "app.media_service.create_post_upload",
        lambda _settings, **kwargs: (
            "https://oss.test",
            {"key": kwargs["key"], "policy": "signed"},
        ),
    )
    session = create_upload_session(
        db,
        settings,
        actor_type="internal",
        actor_id="publish-key",
        purpose="runtime_asset",
        target_id="run_001",
        context={"version": "v1", "publication_id": "snapshot_1"},
        objects=[
            DirectUploadObjectRequest(
                client_ref="single.mp4",
                filename="single.mp4",
                relative_path="single.mp4",
                content_type="video/mp4",
                size_bytes=len(payload),
                sha256=digest,
            )
        ],
    )
    media = db.query(MediaObject).filter(MediaObject.upload_session_id == session.session_id).one()
    assert media.staging_key.startswith("ivapp-media/v1/ingress/internal/")
    assert media.object_key.startswith("ivapp-media/v1/private/admin-runs/run_001/publish-inputs/")

    monkeypatch.setattr(
        "app.media_service.head_object",
        lambda _settings, *, key: OssObjectMetadata(
            size_bytes=len(payload),
            content_type="video/mp4",
            etag="etag",
            headers={"x-oss-meta-sha256": digest},
        ),
    )

    def fake_download(_settings, *, key: str, path: str | Path, expected_etag=None):
        Path(path).write_bytes(payload)

    def fake_copy(_settings, *, source_key: str, target_key: str, **_kwargs):
        copied.append((source_key, target_key))
        return target_key

    monkeypatch.setattr("app.media_service.download_file", fake_download)
    monkeypatch.setattr("app.media_service.copy_object", fake_copy)
    result = finalize_upload_session(
        db,
        settings,
        session_id=session.session_id,
        actor_type="internal",
        actor_id="publish-key",
    )
    assert result.state == "ready"
    assert result.objects[0].sha256 == digest
    assert copied == [(media.staging_key, media.object_key)]
    db.refresh(media)
    assert media.state == "ready"


def test_publication_binding_resolves_direct_oss_clip_urls(db, monkeypatch) -> None:
    settings = _oss_settings(monkeypatch)
    media = MediaObject(
        id="mop_clip",
        upload_session_id="session",
        purpose="runtime_public",
        origin="server_copy",
        visibility="public",
        state="ready",
        staging_key="ivapp-media/v1/public/runtime/story/pub/clips/a.mp4",
        object_key="ivapp-media/v1/public/runtime/story/pub/clips/a.mp4",
        original_filename="a.mp4",
        content_type="video/mp4",
        size_bytes=10,
        sha256="a" * 64,
        etag="",
        extra_json={},
    )
    db.add(media)
    db.add(
        PublishedMediaAsset(
            video_id="story",
            publication_id="pub",
            version="1",
            role="clip",
            clip_id="a",
            media_object_id=media.id,
        )
    )
    db.commit()

    assert load_published_runtime_urls(
        db,
        settings,
        video_id="story",
        publication_id="pub",
    ) == {
        "a": "https://cdn.test/ivapp-media/v1/public/runtime/story/pub/clips/a.mp4"
    }


def test_internal_upload_idempotency_reissues_same_object_and_reuses_ready_session(
    db,
    monkeypatch,
) -> None:
    settings = _oss_settings(monkeypatch)
    monkeypatch.setattr(
        "app.media_service.create_post_upload",
        lambda _settings, **kwargs: (
            "https://oss.test",
            {"key": kwargs["key"], "policy": "signed"},
        ),
    )
    declaration = DirectUploadObjectRequest(
        client_ref="frame.jpg",
        filename="frame.jpg",
        relative_path="frame.jpg",
        content_type="image/jpeg",
        size_bytes=1,
        sha256=hashlib.sha256(b"x").hexdigest(),
    )
    first = create_upload_session(
        db,
        settings,
        actor_type="internal",
        actor_id="publish-key",
        purpose="admin_artifact",
        target_id="run_001",
        context={"version": "v1", "snapshot_id": "stable"},
        objects=[declaration],
        session_id="mus_i_stable",
    )
    second = create_upload_session(
        db,
        settings,
        actor_type="internal",
        actor_id="publish-key",
        purpose="admin_artifact",
        target_id="run_001",
        context={"version": "v1", "snapshot_id": "stable"},
        objects=[declaration],
        session_id="mus_i_stable",
    )
    assert first.session_id == second.session_id
    assert first.uploads[0].object_id == second.uploads[0].object_id
    assert db.query(MediaObject).count() == 1

    media = db.query(MediaObject).one()
    session = db.get(MediaUploadSession, first.session_id)
    media.state = "ready"
    session.state = "ready"
    db.commit()
    ready = create_upload_session(
        db,
        settings,
        actor_type="internal",
        actor_id="publish-key",
        purpose="admin_artifact",
        target_id="run_001",
        context={"version": "v1", "snapshot_id": "stable"},
        objects=[declaration],
        session_id="mus_i_stable",
    )
    assert ready.state == "ready"
    assert ready.uploads == []

    changed = declaration.model_copy(update={"sha256": "b" * 64})
    with pytest.raises(MediaServiceError, match="does not match"):
        create_upload_session(
            db,
            settings,
            actor_type="internal",
            actor_id="publish-key",
            purpose="admin_artifact",
            target_id="run_001",
            context={"version": "v1", "snapshot_id": "stable"},
            objects=[changed],
            session_id="mus_i_stable",
        )


def test_run_artifact_rejects_json_business_documents(db, monkeypatch) -> None:
    settings = _oss_settings(monkeypatch)
    payload = b'{"ok":true}'
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        "app.media_service.create_post_upload",
        lambda _settings, **kwargs: (
            "https://oss.test",
            {"key": kwargs["key"], "policy": "signed"},
        ),
    )
    with pytest.raises(MediaServiceError, match="unsupported content type"):
        create_upload_session(
            db,
            settings,
            actor_type="internal",
            actor_id="publish-key",
            purpose="admin_artifact",
            target_id="run_migration",
            context={"version": "legacy", "snapshot_id": "migration_server_123_20260811"},
            objects=[
                DirectUploadObjectRequest(
                    client_ref="analysis.json",
                    filename="analysis.json",
                    relative_path="analysis.json",
                    content_type="application/json",
                    size_bytes=len(payload),
                    sha256=digest,
                )
            ],
        )


def test_html_entry_is_verified_as_document_before_public_copy(db, monkeypatch) -> None:
    settings = _oss_settings(monkeypatch)
    payload = b"this is not html"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        "app.media_service.create_post_upload",
        lambda _settings, **kwargs: (
            "https://oss.test",
            {"key": kwargs["key"], "policy": "signed"},
        ),
    )
    session = create_upload_session(
        db,
        settings,
        actor_type="internal",
        actor_id="publish-key",
        purpose="html_asset",
        target_id="html_test",
        context={"version": "a" * 64, "entry_path": "index.html"},
        objects=[
            DirectUploadObjectRequest(
                client_ref="index.html",
                filename="index.html",
                relative_path="index.html",
                content_type="text/html",
                size_bytes=len(payload),
                sha256=digest,
            )
        ],
    )
    monkeypatch.setattr(
        "app.media_service.head_object",
        lambda _settings, *, key: OssObjectMetadata(
            size_bytes=len(payload),
            content_type="text/html",
            etag="etag",
            headers={"x-oss-meta-sha256": digest},
        ),
    )
    monkeypatch.setattr(
        "app.media_service.download_file",
        lambda _settings, *, key, path, expected_etag=None: Path(path).write_bytes(payload),
    )

    with pytest.raises(MediaServiceError, match="does not contain an HTML document"):
        finalize_upload_session(
            db,
            settings,
            session_id=session.session_id,
            actor_type="internal",
            actor_id="publish-key",
            manifest_hash="a" * 64,
        )


def test_server_prevalidated_html_skips_cross_region_redownload(db, monkeypatch) -> None:
    settings = _oss_settings(monkeypatch)
    payload = b"<!doctype html><html><head></head><body><video></video></body></html>"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        "app.media_service.create_post_upload",
        lambda _settings, **kwargs: (
            "https://oss.test",
            {"key": kwargs["key"], "policy": "signed"},
        ),
    )
    session = create_upload_session(
        db,
        settings,
        actor_type="internal",
        actor_id="publish-key",
        purpose="html_asset",
        target_id="html_prevalidated",
        context={
            "version": "b" * 64,
            "entry_path": "index.html",
            "server_prevalidated": True,
        },
        objects=[
            DirectUploadObjectRequest(
                client_ref="index.html",
                filename="index.html",
                relative_path="index.html",
                content_type="text/html",
                size_bytes=len(payload),
                sha256=digest,
            )
        ],
    )
    monkeypatch.setattr(
        "app.media_service.head_object",
        lambda _settings, *, key: OssObjectMetadata(
            size_bytes=len(payload),
            content_type="text/html",
            etag="etag",
            headers={"x-oss-meta-sha256": digest},
        ),
    )
    monkeypatch.setattr(
        "app.media_service.download_file",
        lambda *_args, **_kwargs: pytest.fail("prevalidated HTML must not be downloaded"),
    )
    monkeypatch.setattr(
        "app.media_service.copy_object",
        lambda _settings, **kwargs: kwargs["target_key"],
    )

    result = finalize_upload_session(
        db,
        settings,
        session_id=session.session_id,
        actor_type="internal",
        actor_id="publish-key",
        manifest_hash="b" * 64,
    )

    assert result.state == "ready"
    assert result.objects[0].state == "ready"


def test_internal_backup_uses_shared_cache_instead_of_redownload(
    db,
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MEDIA_CACHE_ENABLED", "true")
    monkeypatch.setenv("MEDIA_CACHE_ROOT", str(tmp_path / "media-cache"))
    settings = _oss_settings(monkeypatch)
    payload = b"locally-verified-story-clip"
    digest = hashlib.sha256(payload).hexdigest()
    cached = object_path(settings, digest)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(payload)
    monkeypatch.setattr(
        "app.media_service.create_post_upload",
        lambda _settings, **kwargs: (
            "https://oss.test",
            {"key": kwargs["key"], "policy": "signed"},
        ),
    )
    session = create_upload_session(
        db,
        settings,
        actor_type="internal",
        actor_id="publish-key",
        purpose="admin_artifact",
        target_id="story_001",
        context={"local_sha256": digest, "backup_job_id": "backup_001"},
        objects=[
            DirectUploadObjectRequest(
                client_ref="clip.mp4",
                filename="clip.mp4",
                relative_path="clip.mp4",
                content_type="video/mp4",
                size_bytes=len(payload),
                sha256=digest,
            )
        ],
    )
    monkeypatch.setattr(
        "app.media_service.head_object",
        lambda _settings, *, key: OssObjectMetadata(
            size_bytes=len(payload),
            content_type="video/mp4",
            etag="etag",
            headers={"x-oss-meta-sha256": digest},
        ),
    )
    monkeypatch.setattr(
        "app.media_service.download_file",
        lambda *_args, **_kwargs: pytest.fail(
            "shared-cache backup must not be downloaded from OSS"
        ),
    )
    monkeypatch.setattr(
        "app.media_service.copy_object",
        lambda _settings, **kwargs: kwargs["target_key"],
    )

    result = finalize_upload_session(
        db,
        settings,
        session_id=session.session_id,
        actor_type="internal",
        actor_id="publish-key",
    )

    assert result.state == "ready"
    assert result.objects[0].sha256 == digest


def test_media_migration_preflight_reports_missing_runtime_file(db, tmp_path: Path) -> None:
    db.add(
        PublishedVideo(
            id="legacy_missing",
            content_type="runtime",
            video_url="/media/legacy_missing.mp4",
            timeline={},
            runtime_spec={"schema": "pixo.runtime.v1", "version": "1.0", "video": []},
            runtime_spec_version="1.0",
            required_capabilities=[],
            version="v1",
            content_mode="single",
        )
    )
    db.commit()

    assert audit_database_references(tmp_path) == [
        "runtime:legacy_missing: missing single video"
    ]

    (tmp_path / "legacy_missing.mp4").write_bytes(b"legacy")
    assert audit_database_references(tmp_path) == []
