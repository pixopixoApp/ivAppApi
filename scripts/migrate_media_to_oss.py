#!/usr/bin/env python3
"""Inventory every legacy media file and copy it into ivapp's dedicated OSS tree.

Dry-run is the default. ``--apply`` is resumable and never deletes either the
source file or an OSS object. Known avatars, creator sources and published
runtime assets are rebound; every unknown/orphan file is retained under
``private/imports`` with its checksum in the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    CreatorUpload,
    MediaObject,
    PublishedMediaAsset,
    PublishedVideo,
    User,
)
from app.oss_storage import (
    OssObjectNotFoundError,
    copy_object,
    head_object,
    object_key,
    public_url,
    upload_file,
)
from app.protocol_video import RUNTIME_SPEC_VERSION, compile_runtime_spec


@dataclass(frozen=True)
class InventoryItem:
    relative_path: str
    size_bytes: int
    sha256: str
    content_type: str
    classification: str
    entity_id: str
    import_key: str
    final_key: str
    media_object_id: str


_SAFE_BATCH_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identifier(prefix: str, material: str) -> str:
    return f"{prefix}_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:40]}"


def _classify(db, relative: str) -> tuple[str, str]:
    path = Path(relative)
    if len(path.parts) == 2 and path.parts[0] == "avatars":
        user_id = path.stem
        if db.get(User, user_id) is not None:
            return "avatar", user_id
    if relative.startswith("private/"):
        storage_key = relative.removeprefix("private/")
        upload = (
            db.query(CreatorUpload)
            .filter(CreatorUpload.storage_key == storage_key)
            .one_or_none()
        )
        if upload is not None:
            return "creator_source", upload.id
    if len(path.parts) == 1 and path.suffix.lower() == ".mp4":
        video = db.get(PublishedVideo, path.stem)
        if video is not None and video.content_type == "runtime":
            return "runtime_single", video.id
    if len(path.parts) == 2 and path.suffix.lower() == ".mp4":
        video = db.get(PublishedVideo, path.parts[0])
        if video is not None and video.content_type == "runtime":
            return "runtime_clip", f"{video.id}:{path.stem}"
    return "orphan", ""


def inventory(root: Path, batch_id: str) -> list[InventoryItem]:
    settings = get_settings()
    result: list[InventoryItem] = []
    with SessionLocal() as db:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            digest = _sha256(path)
            classification, entity_id = _classify(db, relative)
            object_id = _identifier("moi", f"{relative}:{digest}")
            import_key = object_key(
                settings,
                "private",
                "imports",
                batch_id,
                object_id,
                relative,
            )
            extension = path.suffix.lower() or ".bin"
            if classification == "avatar":
                final_key = object_key(
                    settings,
                    "public",
                    "avatars",
                    object_id[-2:],
                    f"{object_id}{extension}",
                )
            elif classification == "creator_source":
                final_key = object_key(
                    settings,
                    "private",
                    "creator-sources",
                    object_id[-2:],
                    object_id,
                    f"source{extension}",
                )
            elif classification.startswith("runtime_"):
                video_id = entity_id.split(":", 1)[0]
                publication_id = _identifier("pubm", f"{batch_id}:{video_id}")[:48]
                final_key = (
                    object_key(
                        settings,
                        "public",
                        "runtime",
                        video_id,
                        publication_id,
                        "clips",
                        f"{entity_id.split(':', 1)[1]}.mp4",
                    )
                    if classification == "runtime_clip"
                    else object_key(
                        settings,
                        "public",
                        "runtime",
                        video_id,
                        publication_id,
                        "single.mp4",
                    )
                )
            else:
                final_key = import_key
            result.append(
                InventoryItem(
                    relative_path=relative,
                    size_bytes=path.stat().st_size,
                    sha256=digest,
                    content_type=mimetypes.guess_type(path.name)[0]
                    or "application/octet-stream",
                    classification=classification,
                    entity_id=entity_id,
                    import_key=import_key,
                    final_key=final_key,
                    media_object_id=object_id,
                )
            )
    return result


def _ready_media(db, object_id: str | None) -> bool:
    media = db.get(MediaObject, object_id) if object_id else None
    return media is not None and media.state == "ready"


def _runtime_has_ready_binding(db, video: PublishedVideo) -> bool:
    publication_id = video.active_publication_id
    if not publication_id:
        return False
    bindings = (
        db.query(PublishedMediaAsset)
        .filter(
            PublishedMediaAsset.video_id == video.id,
            PublishedMediaAsset.publication_id == publication_id,
        )
        .all()
    )
    if video.content_mode == "story":
        clips = (video.timeline or {}).get("clips") if isinstance(video.timeline, dict) else None
        if not isinstance(clips, dict) or not clips:
            return False
        by_clip = {
            binding.clip_id: binding
            for binding in bindings
            if binding.role == "clip"
        }
        return all(
            clip_id in by_clip and _ready_media(db, by_clip[clip_id].media_object_id)
            for clip_id in clips
        )
    singles = [binding for binding in bindings if binding.role == "single"]
    return len(singles) == 1 and _ready_media(db, singles[0].media_object_id)


def audit_database_references(root: Path) -> list[str]:
    """Find local media bindings that would fail once the legacy disk is detached."""
    blockers: list[str] = []
    with SessionLocal() as db:
        for video in (
            db.query(PublishedVideo)
            .filter(PublishedVideo.content_type == "runtime")
            .order_by(PublishedVideo.id.asc())
            .all()
        ):
            if _runtime_has_ready_binding(db, video):
                continue
            if video.content_mode == "story":
                clips = (video.timeline or {}).get("clips") if isinstance(video.timeline, dict) else None
                if not isinstance(clips, dict) or not clips:
                    blockers.append(f"runtime:{video.id}: story has no clip manifest")
                    continue
                for clip_id in clips:
                    path = root / video.id / f"{clip_id}.mp4"
                    if not path.is_file() or path.is_symlink():
                        blockers.append(
                            f"runtime:{video.id}: missing story clip {clip_id}.mp4"
                        )
            else:
                path = root / f"{video.id}.mp4"
                if not path.is_file() or path.is_symlink():
                    blockers.append(f"runtime:{video.id}: missing single video")

        for user in db.query(User).order_by(User.user_id.asc()).all():
            if _ready_media(db, user.avatar_media_object_id):
                continue
            parsed = urlsplit(user.avatar_url or "")
            marker = "/media/avatars/"
            if marker not in parsed.path:
                continue
            filename = parsed.path.rsplit("/", 1)[-1]
            path = root / "avatars" / filename
            if not path.is_file() or path.is_symlink():
                blockers.append(f"avatar:{user.user_id}: missing {filename}")

        for upload in db.query(CreatorUpload).order_by(CreatorUpload.id.asc()).all():
            if _ready_media(db, upload.media_object_id):
                continue
            storage_key = str(upload.storage_key or "").strip().replace("\\", "/")
            path = (root / "private" / storage_key).resolve()
            private_root = (root / "private").resolve()
            try:
                path.relative_to(private_root)
            except ValueError:
                blockers.append(f"creator:{upload.id}: unsafe storage key")
                continue
            if not path.is_file() or path.is_symlink():
                blockers.append(f"creator:{upload.id}: source file is missing")
    return blockers


def _ensure_uploaded(path: Path, item: InventoryItem) -> None:
    settings = get_settings()

    def assert_identity(metadata, *, label: str) -> None:
        remote_sha = metadata.headers.get("x-oss-meta-sha256", "").lower()
        if metadata.size_bytes != item.size_bytes or remote_sha != item.sha256:
            raise RuntimeError(
                f"immutable {label} identity mismatch: {item.relative_path}"
            )

    try:
        metadata = head_object(settings, key=item.import_key)
    except OssObjectNotFoundError:
        metadata = None
    if metadata is None:
        upload_file(
            settings,
            key=item.import_key,
            path=path,
            content_type=item.content_type,
            public=False,
            immutable=True,
            extra_headers={"x-oss-meta-sha256": item.sha256},
        )
        metadata = head_object(settings, key=item.import_key)
    assert_identity(metadata, label="import")
    if item.final_key != item.import_key:
        try:
            final = head_object(settings, key=item.final_key)
        except OssObjectNotFoundError:
            final = None
        if final is None:
            copy_object(
                settings,
                source_key=item.import_key,
                target_key=item.final_key,
                content_type=item.content_type,
                public=item.classification in {"avatar", "runtime_single", "runtime_clip"},
                immutable=True,
                extra_headers={"x-oss-meta-sha256": item.sha256},
            )
            final = head_object(settings, key=item.final_key)
        assert_identity(final, label="final")


def apply_inventory(root: Path, batch_id: str, items: list[InventoryItem]) -> None:
    if not _SAFE_BATCH_ID.fullmatch(batch_id):
        raise RuntimeError("batch_id must contain only letters, digits, '-' or '_'")
    symlinks = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_symlink()
    )
    blockers = audit_database_references(root)
    if symlinks or blockers:
        details = [*(f"symlink:{path}" for path in symlinks), *blockers]
        raise RuntimeError(
            "media migration preflight failed: " + "; ".join(details[:20])
        )
    settings = get_settings()
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        for item in items:
            _ensure_uploaded(root / item.relative_path, item)
            media = db.get(MediaObject, item.media_object_id)
            if media is None:
                media = MediaObject(
                    id=item.media_object_id,
                    upload_session_id=None,
                    purpose=(
                        "migration_import"
                        if item.classification == "orphan"
                        else f"legacy_{item.classification}"
                    ),
                    origin="migration",
                    visibility=(
                        "public"
                        if item.classification in {"avatar", "runtime_single", "runtime_clip"}
                        else "private"
                    ),
                    state="ready",
                    staging_key=item.import_key,
                    object_key=item.final_key,
                    original_filename=Path(item.relative_path).name,
                    content_type=item.content_type,
                    size_bytes=item.size_bytes,
                    sha256=item.sha256,
                    etag="",
                    extra_json={"legacy_relative_path": item.relative_path, "batch_id": batch_id},
                    verified_at=now,
                    created_at=now,
                )
                db.add(media)
                db.flush()
            elif (
                media.object_key != item.final_key
                or media.staging_key != item.import_key
                or media.size_bytes != item.size_bytes
                or media.sha256 != item.sha256
            ):
                raise RuntimeError(
                    f"media object identity mismatch for {item.relative_path}"
                )
            if item.classification == "avatar":
                user = db.get(User, item.entity_id)
                if user is not None:
                    user.avatar_media_object_id = media.id
                    user.avatar_url = public_url(settings, media.object_key)
            elif item.classification == "creator_source":
                upload = db.get(CreatorUpload, item.entity_id)
                if upload is not None:
                    upload.media_object_id = media.id
                    upload.storage_key = media.object_key
            elif item.classification.startswith("runtime_"):
                video_id, _, clip_id = item.entity_id.partition(":")
                publication_id = _identifier("pubm", f"{batch_id}:{video_id}")[:48]
                role = "clip" if item.classification == "runtime_clip" else "single"
                binding = (
                    db.query(PublishedMediaAsset)
                    .filter(
                        PublishedMediaAsset.video_id == video_id,
                        PublishedMediaAsset.publication_id == publication_id,
                        PublishedMediaAsset.role == role,
                        PublishedMediaAsset.clip_id == clip_id,
                    )
                    .one_or_none()
                )
                video = db.get(PublishedVideo, video_id)
                if binding is None and video is not None:
                    db.add(
                        PublishedMediaAsset(
                            video_id=video_id,
                            publication_id=publication_id,
                            version=video.version,
                            role=role,
                            clip_id=clip_id,
                            media_object_id=media.id,
                        )
                    )
                elif binding is not None and binding.media_object_id != media.id:
                    raise RuntimeError(
                        f"publication slot identity mismatch for {item.relative_path}"
                    )
        db.flush()
        for video_id in sorted(
            {item.entity_id.split(":", 1)[0] for item in items if item.classification.startswith("runtime_")}
        ):
            video = db.get(PublishedVideo, video_id)
            if video is None:
                continue
            publication_id = _identifier("pubm", f"{batch_id}:{video_id}")[:48]
            bindings = (
                db.query(PublishedMediaAsset)
                .filter(
                    PublishedMediaAsset.video_id == video_id,
                    PublishedMediaAsset.publication_id == publication_id,
                )
                .all()
            )
            media_by_slot = {
                binding.clip_id if binding.role == "clip" else "single": db.get(
                    MediaObject, binding.media_object_id
                )
                for binding in bindings
            }
            entry = "single"
            if video.content_mode == "story" and isinstance(video.timeline, dict):
                entry = str(video.timeline.get("entry_clip_id") or "")
            media = media_by_slot.get(entry)
            if media is None:
                continue
            video_url = public_url(settings, media.object_key)
            video.video_url = video_url
            video.active_publication_id = publication_id
            video.runtime_spec = compile_runtime_spec(
                item_id=video.id,
                content_mode=video.content_mode,
                source=video.timeline or {},
                video_url=video_url,
            )
            video.runtime_spec_version = RUNTIME_SPEC_VERSION
            video.updated_at = now
        db.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate every legacy media file to OSS")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not _SAFE_BATCH_ID.fullmatch(args.batch_id):
        parser.error("batch-id must contain only letters, digits, '-' or '_'")
    settings = get_settings()
    root = (args.root or Path(settings.media_root)).resolve()
    if not root.is_dir():
        parser.error(f"media root does not exist: {root}")
    items = inventory(root, args.batch_id)
    symlinks = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_symlink()
    )
    blockers = audit_database_references(root)
    report = {
        "batch_id": args.batch_id,
        "root": str(root),
        "apply": bool(args.apply),
        "file_count": len(items),
        "total_bytes": sum(item.size_bytes for item in items),
        "classifications": {
            name: sum(1 for item in items if item.classification == name)
            for name in sorted({item.classification for item in items})
        },
        "preflight": {
            "ok": not symlinks and not blockers,
            "symlinks": symlinks,
            "unresolved_database_references": blockers,
        },
        "items": [asdict(item) for item in items],
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.apply:
        if symlinks or blockers:
            raise SystemExit(
                "migration refused: inspect preflight in the generated manifest"
            )
        apply_inventory(root, args.batch_id, items)
    print(json.dumps({key: value for key, value in report.items() if key != "items"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
