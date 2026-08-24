from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Protocol

from fastapi import UploadFile

from app.config import Settings


class StorageError(ValueError):
    pass


class MediaStorage(Protocol):
    def resolve(self, key: str) -> Path: ...

    def delete(self, key: str) -> None: ...

    def publish_copy(self, *, source_key: str, item_id: str) -> tuple[Path, str]: ...

    def publish_file(self, *, source: Path, item_id: str) -> tuple[Path, str]: ...


def _safe_component(value: str, *, label: str) -> str:
    safe = "".join(char for char in value if char.isalnum() or char in "-_")
    if not safe or safe != value:
        raise StorageError(f"invalid {label}")
    return safe


class LocalMediaStorage:
    """Local-volume implementation behind a storage boundary for later OSS use."""

    def __init__(self, settings: Settings) -> None:
        self._root = Path(settings.media_root)
        self._private_root = self._root / "private"

    def upload_key(self, *, user_id: str, upload_id: str) -> str:
        uid = _safe_component(user_id, label="user_id")
        identifier = _safe_component(upload_id, label="upload_id")
        return f"creator_uploads/{uid}/{identifier}.mp4"

    def resolve(self, key: str) -> Path:
        pure = PurePosixPath(key)
        if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
            raise StorageError("invalid storage key")
        candidate = self._private_root.joinpath(*pure.parts).resolve()
        root = self._private_root.resolve()
        if candidate != root and root not in candidate.parents:
            raise StorageError("storage key escapes private root")
        return candidate

    async def save_upload(
        self,
        upload: UploadFile,
        *,
        key: str,
        max_bytes: int,
    ) -> int:
        destination = self.resolve(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        size = 0
        try:
            with temporary.open("wb") as stream:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise StorageError(f"video exceeds {max_bytes} bytes")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if size == 0:
                raise StorageError("empty video upload")
            os.replace(temporary, destination)
            return size
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def delete(self, key: str) -> None:
        self.resolve(key).unlink(missing_ok=True)

    def publish_copy(self, *, source_key: str, item_id: str) -> tuple[Path, str]:
        source = self.resolve(source_key)
        return self.publish_file(source=source, item_id=item_id)

    def publish_file(self, *, source: Path, item_id: str) -> tuple[Path, str]:
        identifier = _safe_component(item_id, label="item_id")
        if not source.is_file():
            raise StorageError("uploaded video is missing")
        destination = self._root / f"{identifier}.mp4"
        temporary = self._root / f".{identifier}.publishing.mp4"
        self._root.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination, f"/media/{identifier}.mp4"
