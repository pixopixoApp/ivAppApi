from __future__ import annotations

import hashlib
import mimetypes
import re
import secrets
import tempfile
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import TypeVar

from sqlalchemy.orm import Session

from app.config import Settings
from app.media_api import (
    DirectUploadObjectRequest,
    DirectUploadPolicy,
    FinalizedMediaObjectOut,
    FinalizedUploadSessionOut,
    UploadSessionOut,
)
from app.media_cache import local_path_for_sha256, valid_sha256
from app.models import MediaObject, MediaUploadSession
from app.oss_storage import (
    OssImmutableConflictError,
    OssObjectNotFoundError,
    copy_object,
    create_post_upload,
    download_file,
    head_object,
    is_transient_oss_error,
    object_key,
    public_url,
)
from app.video_probe import VideoProbeError, probe_video


class MediaServiceError(ValueError):
    pass


_T = TypeVar("_T")


def _with_oss_retries(operation: Callable[[], _T]) -> _T:
    for attempt in range(4):
        try:
            return operation()
        except Exception as exc:
            if attempt == 3 or not is_transient_oss_error(exc):
                raise
            time.sleep(0.5 * (2**attempt))
    raise AssertionError("unreachable")


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SAFE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_TYPES = {
    # Run business JSON belongs in ivadmin MySQL.  These OSS purposes are
    # intentionally binary-only so a future writer cannot recreate a full
    # JSON workspace snapshot by accident.
    "admin_source": frozenset({"video/mp4", "application/mp4"}),
    "admin_artifact": frozenset({
        "video/mp4", "application/mp4", "image/jpeg", "image/png", "image/webp",
    }),
    "runtime_asset": frozenset({"video/mp4", "application/mp4"}),
    "html_asset": None,
    "html_import_source": frozenset({"application/zip", "application/x-zip-compressed"}),
    "migration_import": None,
    "creator_video": frozenset({"video/mp4", "application/mp4"}),
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def media_mode_is_oss(settings: Settings) -> bool:
    return settings.media_storage_mode.strip().lower() == "oss"


def require_oss_mode(settings: Settings) -> None:
    if not media_mode_is_oss(settings):
        raise MediaServiceError("OSS media storage is not enabled")


def safe_id(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise MediaServiceError(f"invalid {label}")
    return normalized


def safe_relative_path(value: str, *, label: str = "relative_path") -> str:
    normalized = value.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
        or any(ord(character) < 32 for character in normalized)
    ):
        raise MediaServiceError(f"invalid {label}")
    return path.as_posix()


def safe_filename(value: str) -> str:
    normalized = Path(value.strip()).name
    if not normalized or normalized != value.strip() or normalized in (".", ".."):
        raise MediaServiceError("invalid filename")
    if any(ord(character) < 32 for character in normalized):
        raise MediaServiceError("invalid filename")
    return normalized[:255]


def normalize_content_type(
    raw: str, *, filename: str, purpose: str, allow_legacy_json: bool = False
) -> str:
    value = raw.split(";", 1)[0].strip().lower()
    if not value or "/" not in value or any(character.isspace() for character in value):
        value = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    allowed = _ALLOWED_TYPES.get(purpose)
    if (
        allowed is not None
        and value not in allowed
        and not (purpose == "admin_artifact" and allow_legacy_json and value == "application/json")
    ):
        raise MediaServiceError(f"unsupported content type for {purpose}")
    if purpose == "html_asset" and value in {"text/html", "application/javascript", "text/javascript", "text/css"}:
        return value
    return value


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def _normalized_expiry(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if not suffix or len(suffix) > 16 or not re.fullmatch(r"\.[a-z0-9]+", suffix):
        return ".bin"
    return suffix


def _final_key(
    settings: Settings,
    *,
    purpose: str,
    target_id: str,
    object_id: str,
    filename: str,
    relative_path: str | None,
    context: dict,
) -> tuple[str, str]:
    target = safe_id(target_id, label="target_id")
    extension = _extension(filename)
    if purpose == "creator_video":
        return object_key(
            settings,
            "private",
            "creator-sources",
            object_id[-2:],
            object_id,
            f"source{extension}",
        ), "private"
    if purpose == "admin_source":
        return object_key(
            settings,
            "private",
            "v2",
            "ivadmin",
            "runs",
            target,
            "sources",
            object_id,
            f"source{extension}",
        ), "private"
    if purpose == "admin_artifact":
        version = safe_id(str(context.get("version") or "v1"), label="version")
        snapshot = safe_id(
            str(context.get("snapshot_id") or context.get("publication_id") or object_id),
            label="snapshot_id",
        )
        relative = safe_relative_path(relative_path or filename)
        return object_key(
            settings,
            "private",
            "v2",
            "ivadmin",
            "runs",
            target,
            "version-media",
            version,
            snapshot,
            relative,
        ), "private"
    if purpose == "runtime_asset":
        version = safe_id(str(context.get("version") or "v1"), label="version")
        publication = safe_id(
            str(context.get("publication_id") or object_id), label="publication_id"
        )
        relative = safe_relative_path(relative_path or filename)
        return object_key(
            settings,
            "private",
            "admin-runs",
            target,
            "publish-inputs",
            version,
            publication,
            relative,
        ), "private"
    if purpose == "html_asset":
        version = safe_id(str(context.get("version") or ""), label="version")
        relative = safe_relative_path(relative_path or filename)
        return object_key(
            settings,
            "public",
            "html",
            target,
            version,
            relative,
        ), "public"
    if purpose == "html_import_source":
        return object_key(
            settings,
            "private",
            "html-imports",
            target,
            "sources",
            object_id,
            "source.zip",
        ), "private"
    if purpose == "migration_import":
        relative = safe_relative_path(relative_path or filename)
        return object_key(
            settings,
            "private",
            "imports",
            target,
            object_id,
            relative,
        ), "private"
    raise MediaServiceError("unsupported upload purpose")


def create_upload_session(
    db: Session,
    settings: Settings,
    *,
    actor_type: str,
    actor_id: str,
    purpose: str,
    target_id: str,
    context: dict,
    objects: Iterable[DirectUploadObjectRequest],
    session_id: str | None = None,
) -> UploadSessionOut:
    require_oss_mode(settings)
    if purpose not in _ALLOWED_TYPES:
        raise MediaServiceError("unsupported upload purpose")
    target = safe_id(target_id, label="target_id")
    session_identifier = safe_id(session_id or _new_id("mus"), label="session_id")
    declarations = list(objects)
    expires_at = now_utc() + timedelta(seconds=max(60, min(3600, settings.oss_upload_ttl_seconds)))
    existing_session = db.get(MediaUploadSession, session_identifier)
    if existing_session is not None:
        if (
            existing_session.actor_type != actor_type[:24]
            or existing_session.actor_id != actor_id[:128]
            or existing_session.purpose != purpose
            or existing_session.target_id != target
            or dict(existing_session.context or {}) != dict(context)
        ):
            raise MediaServiceError("idempotency key was reused for a different upload")
        existing_objects = _session_objects(db, session_identifier)
        by_ref = {
            str((item.extra_json or {}).get("client_ref") or ""): item
            for item in existing_objects
        }
        if len(existing_objects) != len(declarations) or len(by_ref) != len(declarations):
            raise MediaServiceError("idempotent upload declaration does not match")
        policies: list[DirectUploadPolicy] = []
        for declaration in declarations:
            filename = safe_filename(declaration.filename)
            relative = (
                safe_relative_path(declaration.relative_path)
                if declaration.relative_path
                else None
            )
            content_type = normalize_content_type(
                declaration.content_type,
                filename=filename,
                purpose=purpose,
            )
            media = by_ref.get(declaration.client_ref)
            if media is None:
                raise MediaServiceError("idempotent upload declaration does not match")
            expected_key, expected_visibility = _final_key(
                settings,
                purpose=purpose,
                target_id=target,
                object_id=media.id,
                filename=filename,
                relative_path=relative,
                context=context,
            )
            if (
                media.original_filename != filename
                or media.content_type != content_type
                or media.size_bytes != declaration.size_bytes
                or media.sha256 != declaration.sha256.lower()
                or str((media.extra_json or {}).get("relative_path") or "")
                != (relative or "")
                or media.object_key != expected_key
                or media.visibility != expected_visibility
            ):
                raise MediaServiceError("idempotent upload declaration does not match")
            if existing_session.state != "ready" and media.state != "ready":
                upload_url, fields = create_post_upload(
                    settings,
                    key=media.staging_key,
                    content_type=media.content_type,
                    size_bytes=media.size_bytes,
                    expires_at=expires_at,
                    extra_fields={
                        "x-oss-meta-pixo-object-id": media.id,
                        "x-oss-meta-pixo-session-id": session_identifier,
                        "x-oss-meta-sha256": media.sha256,
                    },
                )
                policies.append(
                    DirectUploadPolicy(
                        object_id=media.id,
                        client_ref=declaration.client_ref,
                        url=upload_url,
                        fields=fields,
                        expires_at=expires_at.isoformat(),
                    )
                )
        if existing_session.state != "ready":
            existing_session.state = "pending"
            existing_session.expires_at = expires_at
            existing_session.updated_at = now_utc()
            db.add(existing_session)
            db.commit()
        return UploadSessionOut(
            session_id=existing_session.id,
            purpose=existing_session.purpose,
            state=existing_session.state,
            expires_at=(
                _normalized_expiry(existing_session.expires_at).isoformat()
                if existing_session.state == "ready"
                else expires_at.isoformat()
            ),
            uploads=policies,
        )

    row = MediaUploadSession(
        id=session_identifier,
        actor_type=actor_type[:24],
        actor_id=actor_id[:128],
        purpose=purpose,
        state="pending",
        target_id=target,
        manifest_hash="",
        context=dict(context),
        expires_at=expires_at,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    db.add(row)
    policies: list[DirectUploadPolicy] = []
    seen_keys: set[str] = set()
    for declaration in declarations:
        filename = safe_filename(declaration.filename)
        relative = (
            safe_relative_path(declaration.relative_path)
            if declaration.relative_path
            else None
        )
        content_type = normalize_content_type(
            declaration.content_type,
            filename=filename,
            purpose=purpose,
            allow_legacy_json=bool((context or {}).get("legacy_workspace_compat")),
        )
        sha256 = declaration.sha256.lower()
        if not _SAFE_SHA256.fullmatch(sha256):
            raise MediaServiceError("invalid sha256")
        object_id = _new_id("mo")
        staging_key = object_key(
            settings,
            "ingress",
            "client" if actor_type == "user" else "internal",
            session_identifier,
            f"{object_id}{_extension(filename)}",
        )
        final_key, visibility = _final_key(
            settings,
            purpose=purpose,
            target_id=target,
            object_id=object_id,
            filename=filename,
            relative_path=relative,
            context=context,
        )
        if final_key in seen_keys:
            raise MediaServiceError("duplicate destination path")
        if db.query(MediaObject.id).filter(MediaObject.object_key == final_key).first():
            raise MediaServiceError("immutable media target already belongs to another upload")
        seen_keys.add(final_key)
        media = MediaObject(
            id=object_id,
            upload_session_id=session_identifier,
            purpose=purpose,
            origin="client_upload" if actor_type == "user" else "internal_upload",
            visibility=visibility,
            state="declared",
            staging_key=staging_key,
            object_key=final_key,
            original_filename=filename,
            content_type=content_type,
            size_bytes=declaration.size_bytes,
            sha256=sha256,
            etag="",
            extra_json={
                "client_ref": declaration.client_ref,
                "relative_path": relative or "",
            },
            created_at=now_utc(),
        )
        db.add(media)
        upload_url, fields = create_post_upload(
            settings,
            key=staging_key,
            content_type=content_type,
            size_bytes=declaration.size_bytes,
            expires_at=expires_at,
            extra_fields={
                "x-oss-meta-pixo-object-id": object_id,
                "x-oss-meta-pixo-session-id": session_identifier,
                "x-oss-meta-sha256": sha256,
            },
        )
        policies.append(
            DirectUploadPolicy(
                object_id=object_id,
                client_ref=declaration.client_ref,
                url=upload_url,
                fields=fields,
                expires_at=expires_at.isoformat(),
            )
        )
    db.commit()
    return UploadSessionOut(
        session_id=row.id,
        purpose=row.purpose,
        state=row.state,
        expires_at=expires_at.isoformat(),
        uploads=policies,
    )


def _session_objects(db: Session, session_id: str) -> list[MediaObject]:
    return (
        db.query(MediaObject)
        .filter(MediaObject.upload_session_id == session_id)
        .order_by(MediaObject.created_at.asc(), MediaObject.id.asc())
        .all()
    )


def _result(settings: Settings, session: MediaUploadSession, objects: list[MediaObject]) -> FinalizedUploadSessionOut:
    return FinalizedUploadSessionOut(
        session_id=session.id,
        purpose=session.purpose,
        state="ready",
        objects=[
            FinalizedMediaObjectOut(
                object_id=item.id,
                client_ref=str((item.extra_json or {}).get("client_ref") or ""),
                purpose=item.purpose,
                state=item.state,
                object_key=item.object_key,
                content_type=item.content_type,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                public_url=(public_url(settings, item.object_key) if item.visibility == "public" else None),
            )
            for item in objects
        ],
    )


def _verify_and_promote(
    settings: Settings,
    session_context: dict,
    root: Path,
    item: MediaObject,
    verify_content: bool,
    verified_local_path: Path | None = None,
) -> tuple[MediaObject, str, dict]:
    """Verify one uploaded object and copy it to its immutable destination."""
    metadata = _with_oss_retries(
        lambda: head_object(settings, key=item.staging_key)
    )
    client_ref = (item.extra_json or {}).get("client_ref", item.id)
    if metadata.size_bytes != item.size_bytes:
        raise MediaServiceError(f"uploaded size mismatch for {client_ref}")
    remote_type = metadata.content_type.split(";", 1)[0].strip().lower()
    if remote_type and remote_type != item.content_type:
        raise MediaServiceError(f"uploaded content type mismatch for {client_ref}")
    remote_sha = metadata.headers.get("x-oss-meta-sha256", "").lower()
    if remote_sha and remote_sha != item.sha256:
        raise MediaServiceError(f"uploaded checksum metadata mismatch for {client_ref}")

    local_path = verified_local_path or (root / item.id)
    if verify_content:
        _with_oss_retries(
            lambda: download_file(
                settings,
                key=item.staging_key,
                path=local_path,
                expected_etag=metadata.etag,
            )
        )
        if local_path.stat().st_size != item.size_bytes or _sha256_file(local_path) != item.sha256:
            raise MediaServiceError(f"uploaded checksum mismatch for {client_ref}")

    updates: dict = {}
    if (
        item.purpose == "html_asset"
        and str((item.extra_json or {}).get("relative_path") or "")
        == str(session_context.get("entry_path") or "")
        and verify_content
    ):
        if item.content_type not in {"text/html", "application/xhtml+xml"}:
            raise MediaServiceError("HTML package entry is not text/html")
        with local_path.open("rb") as html_stream:
            sample = html_stream.read(4096).lstrip().lower()
        if b"<html" not in sample and b"<!doctype html" not in sample:
            raise MediaServiceError(
                "HTML package entry does not contain an HTML document"
            )
    if item.purpose == "creator_video":
        try:
            video = probe_video(local_path)
        except VideoProbeError as exc:
            raise MediaServiceError(str(exc)) from exc
        if video.duration_ms > settings.creator_video_max_duration_seconds * 1000:
            raise MediaServiceError(
                f"video must be {settings.creator_video_max_duration_seconds} seconds or shorter"
            )
        updates["duration_ms"] = video.duration_ms

    try:
        _with_oss_retries(
            lambda: copy_object(
                settings,
                source_key=item.staging_key,
                target_key=item.object_key,
                content_type=item.content_type,
                public=item.visibility == "public",
                immutable=True,
                expected_etag=metadata.etag,
                extra_headers={
                    "x-oss-meta-pixo-object-id": item.id,
                    "x-oss-meta-sha256": item.sha256,
                },
            )
        )
    except OssImmutableConflictError as copy_error:
        try:
            existing = _with_oss_retries(
                lambda: head_object(settings, key=item.object_key)
            )
        except OssObjectNotFoundError:
            raise copy_error
        existing_sha = existing.headers.get("x-oss-meta-sha256", "").lower()
        if existing.size_bytes != item.size_bytes or existing_sha != item.sha256:
            raise MediaServiceError(
                "immutable media target already exists with different content"
            ) from copy_error
    return item, metadata.etag or "", updates


def _verified_shared_local_cache_path(
    settings: Settings,
    session_context: dict,
    item: MediaObject,
) -> Path | None:
    """Verify an internal backup against the cache mounted by both services."""
    if not settings.media_cache_enabled:
        return None
    declared = valid_sha256(str(session_context.get("local_sha256") or ""))
    if declared is None or declared != item.sha256:
        return None
    path = local_path_for_sha256(
        settings,
        declared,
        expected_size=item.size_bytes,
    )
    if path is None or _sha256_file(path) != declared:
        return None
    return path


def finalize_upload_session(
    db: Session,
    settings: Settings,
    *,
    session_id: str,
    actor_type: str,
    actor_id: str,
    manifest_hash: str = "",
) -> FinalizedUploadSessionOut:
    require_oss_mode(settings)
    session = (
        db.query(MediaUploadSession)
        .filter(MediaUploadSession.id == session_id)
        .with_for_update()
        .one_or_none()
    )
    if session is None or session.actor_type != actor_type or session.actor_id != actor_id:
        raise MediaServiceError("upload session not found")
    objects = _session_objects(db, session.id)
    if session.state == "ready":
        return _result(settings, session, objects)
    if session.state != "pending":
        raise MediaServiceError("upload session is not finalizable")
    if _normalized_expiry(session.expires_at) < now_utc():
        session.state = "expired"
        session.updated_at = now_utc()
        db.commit()
        raise MediaServiceError("upload session has expired")
    if not objects:
        raise MediaServiceError("upload session is empty")

    session.state = "verifying"
    session.updated_at = now_utc()
    db.flush()
    try:
        with tempfile.TemporaryDirectory(prefix="ivapp-media-verify-") as temporary:
            root = Path(temporary)
            pending = [item for item in objects if item.state != "ready"]
            context = dict(session.context or {})
            # Admin sources, legacy migrations and browser-QA'd HTML packages
            # come from authenticated server workflows that computed their
            # SHA-256 immediately before HTTPS upload. Enforcing signed
            # size/type/hash metadata plus immutable OSS copy is sufficient;
            # downloading the same trusted bytes across regions repeats work.
            # User uploads and unmarked HTML/runtime paths retain byte verification.
            trusted_server_upload = (
                actor_type == "internal"
                and (
                    session.purpose == "admin_source"
                    or (
                        session.purpose == "html_asset"
                        and context.get("server_prevalidated") is True
                    )
                    or (
                        session.purpose == "admin_artifact"
                        and str(context.get("version") or "") == "legacy"
                        and str(context.get("snapshot_id") or "").startswith("migration_")
                    )
                )
            )
            locally_verified = {
                item.id: _verified_shared_local_cache_path(settings, context, item)
                for item in pending
            }
            concurrency = max(1, min(settings.oss_max_concurrency, len(pending) or 1))
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = [
                    executor.submit(
                        _verify_and_promote,
                        settings,
                        context,
                        root,
                        item,
                        not trusted_server_upload and locally_verified[item.id] is None,
                        locally_verified[item.id],
                    )
                    for item in pending
                ]
                verified = [future.result() for future in as_completed(futures)]
            for item, etag, updates in verified:
                if updates:
                    item.extra_json = {**(item.extra_json or {}), **updates}
                item.state = "ready"
                item.etag = etag
                item.verified_at = now_utc()
                db.add(item)
        session.state = "ready"
        session.manifest_hash = manifest_hash.strip().lower()
        session.finalized_at = now_utc()
        session.updated_at = now_utc()
        db.commit()
    except Exception:
        db.rollback()
        failed = db.get(MediaUploadSession, session_id)
        if failed is not None and failed.state != "ready":
            failed.state = "pending"
            failed.updated_at = now_utc()
            db.commit()
        raise
    return _result(settings, session, objects)
