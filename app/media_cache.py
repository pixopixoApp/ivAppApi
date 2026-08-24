"""Shared, content-addressed local media store used by ivapp and ivadmin."""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

from app.config import Settings

FORMAT_VERSION = "pixo-media-cache-v1"
LOCAL_MEDIA_PREFIX = "local-cache://sha256/"


class LocalMediaCacheError(RuntimeError):
    pass


def valid_sha256(value: str | None) -> str | None:
    digest = (value or "").strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None
    return digest


def cache_root(settings: Settings) -> Path:
    return Path(settings.media_cache_root).resolve()


def initialize_cache(settings: Settings) -> Path:
    if not settings.media_cache_enabled:
        raise LocalMediaCacheError("shared local media cache is disabled")
    root = cache_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "FORMAT"
    if marker.exists():
        if marker.read_text(encoding="utf-8").strip() != FORMAT_VERSION:
            raise LocalMediaCacheError("unsupported shared media cache format")
    else:
        staging = root / f".FORMAT.{os.getpid()}.tmp"
        staging.write_text(FORMAT_VERSION + "\n", encoding="utf-8")
        try:
            os.link(staging, marker)
        except FileExistsError:
            pass
        finally:
            staging.unlink(missing_ok=True)
    for name, mode in (("objects", 0o755), ("uploads", 0o700), ("locks", 0o700)):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, mode)
    return root


def local_media_uri(sha256: str) -> str:
    digest = valid_sha256(sha256)
    if digest is None:
        raise ValueError("invalid media sha256")
    return f"{LOCAL_MEDIA_PREFIX}{digest}"


def sha256_from_uri(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw.startswith(LOCAL_MEDIA_PREFIX):
        return None
    return valid_sha256(raw[len(LOCAL_MEDIA_PREFIX) :])


def object_path(settings: Settings, sha256: str) -> Path:
    digest = valid_sha256(sha256)
    if digest is None:
        raise ValueError("invalid media sha256")
    return cache_root(settings) / "objects" / digest[:2] / f"{digest}.cache"


def local_path_for_sha256(
    settings: Settings,
    sha256: str,
    *,
    expected_size: int | None = None,
) -> Path | None:
    path = object_path(settings, sha256)
    try:
        if not path.is_file() or path.is_symlink():
            return None
        size = path.stat().st_size
        if size <= 0 or (expected_size is not None and size != int(expected_size)):
            return None
        os.utime(path, None)
        return path
    except OSError:
        return None


def upload_staging_path(settings: Settings, session_id: str, *, owner: str = "ivapp") -> Path:
    root = initialize_cache(settings)
    owner_key = "".join(char for char in owner.lower() if char.isalnum() or char in "-_")[:32]
    if not owner_key:
        raise ValueError("invalid cache upload owner")
    directory = root / "uploads" / owner_key
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    key = hashlib.sha256(session_id.strip().encode("utf-8")).hexdigest()
    return directory / f"{key}.part"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def object_lock(settings: Settings, key: str):
    normalized = valid_sha256(key) or hashlib.sha256(key.encode("utf-8")).hexdigest()
    directory = initialize_cache(settings) / "locks"
    lock_path = directory / f"{normalized}.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_upload_capacity(settings: Settings, size_bytes: int) -> None:
    size = int(size_bytes)
    if size <= 0:
        raise LocalMediaCacheError("upload size must be positive")
    root = initialize_cache(settings)
    free = shutil.disk_usage(root).free
    if free - size < int(settings.media_cache_min_free_bytes):
        raise LocalMediaCacheError("local media disk free-space floor would be exceeded")


def ensure_free_space_floor(settings: Settings) -> None:
    root = initialize_cache(settings)
    if shutil.disk_usage(root).free < int(settings.media_cache_min_free_bytes):
        raise LocalMediaCacheError("local media disk free-space floor was reached")


def commit_staged_upload(
    settings: Settings,
    staging: Path,
    *,
    sha256: str,
    size_bytes: int,
) -> Path:
    digest = valid_sha256(sha256)
    if digest is None:
        raise LocalMediaCacheError("invalid upload checksum")
    size = int(size_bytes)
    if not staging.is_file() or staging.is_symlink() or staging.stat().st_size != size:
        raise LocalMediaCacheError("uploaded size does not match reservation")
    if sha256_file(staging) != digest:
        raise LocalMediaCacheError("uploaded checksum does not match reservation")
    destination = object_path(settings, digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with object_lock(settings, digest):
        existing = local_path_for_sha256(settings, digest, expected_size=size)
        if existing is not None:
            if sha256_file(existing) != digest:
                raise LocalMediaCacheError("existing shared cache object is corrupt")
            staging.unlink(missing_ok=True)
            return existing
        destination.unlink(missing_ok=True)
        staging.replace(destination)
        os.chmod(destination, 0o644)
        with destination.open("rb") as handle:
            os.fsync(handle.fileno())
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return destination
