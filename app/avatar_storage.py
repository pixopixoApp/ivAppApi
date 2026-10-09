from __future__ import annotations

import hashlib
import io
import secrets
import warnings
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from PIL.Image import DecompressionBombError, DecompressionBombWarning
from sqlalchemy.orm import Session

from app.config import Settings
from app.media_service import media_mode_is_oss
from app.models import MediaObject
from app.oss_storage import OssStorageError, object_key, public_url, upload_bytes

MAX_AVATAR_BYTES = 5 * 1024 * 1024
MAX_BACKGROUND_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
AVATAR_SIZE = 512

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


def _decoded_image(
    raw: bytes,
    *,
    content_type: str | None,
    max_bytes: int,
    kind: str,
) -> Image.Image:
    if not raw:
        raise AvatarStorageError(f"empty {kind} upload")
    if len(raw) > max_bytes:
        raise AvatarStorageError(f"{kind} too large")
    claimed = (content_type or "").split(";")[0].strip().lower()
    if claimed and claimed not in _CONTENT_TYPE_EXT:
        raise AvatarStorageError(f"{kind} must be jpg, png, or webp")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DecompressionBombWarning)
            opened = Image.open(io.BytesIO(raw))
            opened.load()
        if (opened.width * opened.height) > MAX_IMAGE_PIXELS:
            raise AvatarStorageError(f"{kind} exceeds the 40 megapixel limit")
        if bool(getattr(opened, "is_animated", False)) or int(getattr(opened, "n_frames", 1)) != 1:
            raise AvatarStorageError(f"animated {kind} images are not supported")
        actual = str(opened.format or "").upper()
        actual_type = {
            "JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp",
        }.get(actual)
        if actual_type is None:
            raise AvatarStorageError(f"{kind} must be jpg, png, or webp")
        claimed_normalized = "image/jpeg" if claimed in {"image/jpg", "image/pjpeg"} else claimed
        if claimed_normalized and claimed_normalized != actual_type:
            raise AvatarStorageError(f"{kind} content type does not match the image")
        return ImageOps.exif_transpose(opened).convert("RGB")
    except AvatarStorageError:
        raise
    except (UnidentifiedImageError, OSError, DecompressionBombError, DecompressionBombWarning) as exc:
        raise AvatarStorageError(f"invalid {kind} image") from exc


def _webp(image: Image.Image, *, quality: int = 88) -> bytes:
    output = io.BytesIO()
    image.save(output, format="WEBP", quality=quality, method=6, exif=b"")
    return output.getvalue()


def prepare_avatar(raw: bytes, *, content_type: str | None) -> bytes:
    image = _decoded_image(
        raw, content_type=content_type, max_bytes=MAX_AVATAR_BYTES, kind="avatar"
    )
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    square = image.crop((left, top, left + side, top + side)).resize(
        (AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS
    )
    return _webp(square)


def _cover_crop(image: Image.Image, size: tuple[int, int], focus_x: float, focus_y: float) -> Image.Image:
    target_width, target_height = size
    target_ratio = target_width / target_height
    source_ratio = image.width / image.height
    if source_ratio > target_ratio:
        crop_height = image.height
        crop_width = round(crop_height * target_ratio)
    else:
        crop_width = image.width
        crop_height = round(crop_width / target_ratio)
    center_x = max(0.0, min(1.0, focus_x)) * image.width
    center_y = max(0.0, min(1.0, focus_y)) * image.height
    left = max(0, min(image.width - crop_width, round(center_x - crop_width / 2)))
    top = max(0, min(image.height - crop_height, round(center_y - crop_height / 2)))
    return image.crop((left, top, left + crop_width, top + crop_height)).resize(
        size, Image.Resampling.LANCZOS
    )


def prepare_backgrounds(
    raw: bytes, *, content_type: str | None, focus_x: float, focus_y: float
) -> tuple[bytes, bytes]:
    image = _decoded_image(
        raw, content_type=content_type, max_bytes=MAX_BACKGROUND_BYTES, kind="background"
    )
    desktop = _webp(_cover_crop(image, (2400, 800), focus_x, focus_y), quality=86)
    mobile = _webp(_cover_crop(image, (1080, 1350), focus_x, focus_y), quality=86)
    return desktop, mobile


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


def backgrounds_dir(settings: Settings) -> Path:
    root = Path(settings.media_root) / "backgrounds"
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
    uid = _safe_user_id(user_id)
    del filename
    raw = prepare_avatar(raw, content_type=content_type)
    ext = "webp"
    directory = avatars_dir(settings)
    digest = hashlib.sha256(raw).hexdigest()[:16]
    dest = directory / f"{uid}-{digest}.{ext}"
    dest.write_bytes(raw)
    return f"/media/avatars/{dest.name}"


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
    _safe_user_id(user_id)
    raw = prepare_avatar(raw, content_type=content_type)
    ext = "webp"
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


def store_channel_background(
    db: Session,
    settings: Settings,
    *,
    user_id: str,
    raw: bytes,
    filename: str | None,
    content_type: str | None,
    focus_x: float,
    focus_y: float,
) -> tuple[str, str]:
    """Decode once and publish immutable desktop/mobile background variants."""
    uid = _safe_user_id(user_id)
    desktop, mobile = prepare_backgrounds(
        raw, content_type=content_type, focus_x=focus_x, focus_y=focus_y
    )
    variants = (("desktop", desktop), ("mobile", mobile))
    urls: dict[str, str] = {}
    if not media_mode_is_oss(settings):
        directory = backgrounds_dir(settings)
        for variant, payload in variants:
            digest = hashlib.sha256(payload).hexdigest()[:16]
            name = f"{uid}-{variant}-{digest}.webp"
            (directory / name).write_bytes(payload)
            urls[variant] = f"/media/backgrounds/{name}"
        return urls["desktop"], urls["mobile"]

    now = datetime.now(timezone.utc)
    for variant, payload in variants:
        object_id = f"mo_{secrets.token_urlsafe(18)}"
        key = object_key(
            settings,
            "public",
            "creator-backgrounds",
            object_id[-2:],
            f"{object_id}-{variant}.webp",
        )
        digest = hashlib.sha256(payload).hexdigest()
        url = upload_bytes(
            settings,
            key=key,
            payload=payload,
            content_type="image/webp",
            public=True,
            immutable=True,
            extra_headers={
                "x-oss-meta-pixo-object-id": object_id,
                "x-oss-meta-sha256": digest,
                "x-oss-meta-variant": variant,
            },
        )
        db.add(MediaObject(
            id=object_id,
            upload_session_id=None,
            purpose="creator_background",
            origin="server_upload",
            visibility="public",
            state="ready",
            staging_key=key,
            object_key=key,
            original_filename=filename or "background.webp",
            content_type="image/webp",
            size_bytes=len(payload),
            sha256=digest,
            etag="",
            extra_json={"variant": variant},
            verified_at=now,
            created_at=now,
        ))
        urls[variant] = url
    return urls["desktop"], urls["mobile"]


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


def resolve_background_path(settings: Settings, filename: str) -> Path:
    name = filename.strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        raise AvatarStorageError("invalid background filename")
    if Path(name).suffix.lower() != ".webp":
        raise AvatarStorageError("invalid background filename")
    stem = Path(name).stem
    if not stem or any(not (character.isalnum() or character in "-_") for character in stem):
        raise AvatarStorageError("invalid background filename")
    return backgrounds_dir(settings) / name
