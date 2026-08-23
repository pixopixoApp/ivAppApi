from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import Settings
from app.media_service import media_mode_is_oss
from app.models import MediaObject
from app.oss_storage import OssStorageError, object_key, public_url, upload_bytes

MAX_AVATAR_BYTES = 2 * 1024 * 1024

_CONTENT_TYPE_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/pjpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

_EXT_CONTENT_TYPE = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


class AvatarStorageError(ValueError):
    """Invalid avatar upload (type/size/path)."""


def _safe_user_id(user_id: str) -> str:
    uid = user_id.strip()
    safe = "".join(ch for ch in uid if ch.isalnum() or ch in "-_")
    if not safe or safe != uid:
        raise AvatarStorageError("invalid user_id for avatar path")
    return safe


def _resolve_ext(*, filename: str | None, content_type: str | None) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in _CONTENT_TYPE_EXT:
        return _CONTENT_TYPE_EXT[ctype]
    if filename:
        suffix = Path(filename).suffix.lstrip(".").lower()
        if suffix == "jpeg":
            return "jpg"
        if suffix in ("jpg", "png", "webp"):
            return suffix
    raise AvatarStorageError("avatar must be jpg, png, or webp")


def avatar_media_type(filename: str) -> str:
    ext = Path(filename).suffix.lstrip(".").lower()
    if ext == "jpeg":
        ext = "jpg"
    return _EXT_CONTENT_TYPE.get(ext, "application/octet-stream")


def avatars_dir(settings: Settings) -> Path:
    root = Path(settings.media_root) / "avatars"
    root.mkdir(parents=True, exist_ok=True)
    return root


def covers_dir(settings: Settings) -> Path:
    root = Path(settings.media_root) / "covers"
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_user_avatar(
    settings: Settings,
    *,
    user_id: str,
    raw: bytes,
    filename: str | None = None,
    content_type: str | None = None,
) -> str:
    """Write avatar under MEDIA_ROOT/avatars and return relative URL."""
    if not raw:
        raise AvatarStorageError("empty avatar upload")
    if len(raw) > MAX_AVATAR_BYTES:
        raise AvatarStorageError("avatar too large (max 2MB)")

    uid = _safe_user_id(user_id)
    ext = _resolve_ext(filename=filename, content_type=content_type)
    directory = avatars_dir(settings)

    for old in directory.glob(f"{uid}.*"):
        if old.name != f"{uid}.{ext}":
            old.unlink(missing_ok=True)

    dest = directory / f"{uid}.{ext}"
    dest.write_bytes(raw)
    return f"/media/avatars/{uid}.{ext}"


def store_user_avatar(
    db: Session,
    settings: Settings,
    *,
    user_id: str,
    raw: bytes,
    filename: str | None = None,
    content_type: str | None = None,
) -> tuple[str, str | None]:
    """Persist an avatar without leaving a server-local file in OSS mode."""
    if not media_mode_is_oss(settings):
        return (
            save_user_avatar(
                settings,
                user_id=user_id,
                raw=raw,
                filename=filename,
                content_type=content_type,
            ),
            None,
        )
    if not raw:
        raise AvatarStorageError("empty avatar upload")
    if len(raw) > MAX_AVATAR_BYTES:
        raise AvatarStorageError("avatar too large (max 2MB)")
    _safe_user_id(user_id)
    ext = _resolve_ext(filename=filename, content_type=content_type)
    media_type = _EXT_CONTENT_TYPE[ext]
    object_id = f"mo_{secrets.token_urlsafe(18)}"
    key = object_key(
        settings,
        "public",
        "avatars",
        object_id[-2:],
        f"{object_id}.{ext}",
    )
    digest = hashlib.sha256(raw).hexdigest()
    url = upload_bytes(
        settings,
        key=key,
        payload=raw,
        content_type=media_type,
        public=True,
        immutable=True,
        extra_headers={
            "x-oss-meta-pixo-object-id": object_id,
            "x-oss-meta-sha256": digest,
        },
    )
    now = datetime.now(timezone.utc)
    db.add(
        MediaObject(
            id=object_id,
            upload_session_id=None,
            purpose="avatar",
            origin="server_upload",
            visibility="public",
            state="ready",
            staging_key=key,
            object_key=key,
            original_filename=filename or f"avatar.{ext}",
            content_type=media_type,
            size_bytes=len(raw),
            sha256=digest,
            etag="",
            extra_json={},
            verified_at=now,
            created_at=now,
        )
    )
    return url, object_id


def store_cover_image(
    db: Session,
    settings: Settings,
    *,
    raw: bytes,
    filename: str | None = None,
    content_type: str | None = None,
) -> tuple[str, str]:
    """Persist a published cover image into ivapp media storage (purpose=cover).

    Returns (url, media_object_id). OSS mode stores to OSS + MediaObject; local
    dev mode writes under MEDIA_ROOT/covers and records a MediaObject row.
    """
    if not raw:
        raise AvatarStorageError("empty cover upload")
    if len(raw) > MAX_AVATAR_BYTES:
        raise AvatarStorageError("cover too large (max 2MB)")
    ext = _resolve_ext(filename=filename, content_type=content_type)
    media_type = _EXT_CONTENT_TYPE[ext]
    digest = hashlib.sha256(raw).hexdigest()
    now = datetime.now(timezone.utc)
    object_id = digest  # local mode uses sha256 as object id; OSS keeps mo_ ids

    if not media_mode_is_oss(settings):
        # local mode: write file under MEDIA_ROOT/covers, keep a MediaObject row
        directory = covers_dir(settings)
        dest = directory / f"{object_id}.{ext}"
        dest.write_bytes(raw)
        key = f"covers/{object_id}.{ext}"
        db.add(
            MediaObject(
                id=object_id,
                upload_session_id=None,
                purpose="cover",
                origin="server_upload",
                visibility="public",
                state="ready",
                staging_key=key,
                object_key=key,
                original_filename=filename or f"cover.{ext}",
                content_type=media_type,
                size_bytes=len(raw),
                sha256=digest,
                etag="",
                extra_json={},
                verified_at=now,
                created_at=now,
            )
        )
        url = f"/media/covers/{object_id}.{ext}"
        return url, object_id

    # 幂等：按内容 sha256 去重，已上传过的同一张封面直接复用已有 cover，
    # 避免在 OSS 里为相同封面反复创建对象。
    existing = (
        db.query(MediaObject)
        .filter(
            MediaObject.purpose == "cover",
            MediaObject.state == "ready",
            MediaObject.sha256 == digest,
        )
        .order_by(MediaObject.created_at.asc())
        .first()
    )
    if existing is not None:
        try:
            existing_url = public_url(settings, existing.object_key)
            return existing_url, existing.id
        except OssStorageError:
            # 已有记录但无法生成公开地址时，继续走新建流程。
            pass

    cover_id = f"mo_{secrets.token_urlsafe(18)}"
    key = object_key(
        settings,
        "public",
        "covers",
        cover_id[-2:],
        f"{cover_id}.{ext}",
    )
    url = upload_bytes(
        settings,
        key=key,
        payload=raw,
        content_type=media_type,
        public=True,
        immutable=True,
        extra_headers={
            "x-oss-meta-pixo-object-id": cover_id,
            "x-oss-meta-sha256": digest,
        },
    )
    db.add(
        MediaObject(
            id=cover_id,
            upload_session_id=None,
            purpose="cover",
            origin="server_upload",
            visibility="public",
            state="ready",
            staging_key=key,
            object_key=key,
            original_filename=filename or f"cover.{ext}",
            content_type=media_type,
            size_bytes=len(raw),
            sha256=digest,
            etag="",
            extra_json={},
            verified_at=now,
            created_at=now,
        )
    )
    return url, cover_id


def resolve_avatar_path(settings: Settings, filename: str) -> Path:
    name = filename.strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        raise AvatarStorageError("invalid avatar filename")
    stem = Path(name).stem
    suffix = Path(name).suffix.lstrip(".").lower()
    if suffix == "jpeg":
        suffix = "jpg"
        name = f"{stem}.jpg"
    if suffix not in ("jpg", "png", "webp"):
        raise AvatarStorageError("invalid avatar filename")
    _safe_user_id(stem)
    if name != f"{stem}.{suffix}":
        raise AvatarStorageError("invalid avatar filename")
    return avatars_dir(settings) / name
