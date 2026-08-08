from __future__ import annotations

from pathlib import Path

from app.config import Settings

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
