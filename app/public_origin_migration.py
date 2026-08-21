from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.html_content import CONTENT_TYPE_HTML, CONTENT_TYPE_RUNTIME
from app.media_service import MediaServiceError
from app.models import (
    CreatorCreation,
    CreatorVersion,
    HtmlPackage,
    MediaObject,
    MediaUploadSession,
    PublishedVideo,
    User,
)
from app.oss_storage import OssStorageError
from app.protocol_video import RuntimeSpecError, read_runtime_spec
from app.public_origin import (
    PublicOriginError,
    canonical_public_url_for_key,
    canonicalize_public_payload,
    canonicalize_public_url,
)
from app.publication_service import load_published_runtime_urls


@dataclass(frozen=True)
class MigrationFailure:
    table: str
    row_id: str
    reason: str


@dataclass
class MigrationReport:
    mode: str
    changes: dict[str, int] = field(default_factory=dict)
    changed_ids: dict[str, list[str]] = field(default_factory=dict)
    failures: list[MigrationFailure] = field(default_factory=list)

    @property
    def changed_count(self) -> int:
        return sum(self.changes.values())


def _mark(report: MigrationReport, table: str, row_id: object) -> None:
    report.changes[table] = report.changes.get(table, 0) + 1
    report.changed_ids.setdefault(table, []).append(str(row_id))


def _set_json(row: object, attr: str, settings: Settings) -> bool:
    current = getattr(row, attr)
    replacement = canonicalize_public_payload(settings, current)
    if replacement == current:
        return False
    setattr(row, attr, replacement)
    return True


def _migrate_published_videos(
    db: Session,
    settings: Settings,
    report: MigrationReport,
) -> None:
    for row in db.query(PublishedVideo).order_by(PublishedVideo.id.asc()).all():
        changed = False
        original_updated_at = row.updated_at
        if row.content_type == CONTENT_TYPE_RUNTIME:
            source = row.timeline if isinstance(row.timeline, dict) else {}
            if row.active_publication_id:
                try:
                    urls = load_published_runtime_urls(
                        db,
                        settings,
                        video_id=row.id,
                        publication_id=row.active_publication_id,
                    )
                    if (row.content_mode or "single") == "story":
                        entry_id = str(source.get("entry_clip_id") or "")
                        entry_url = urls[entry_id]
                        story_urls = urls
                    else:
                        entry_url = urls["single"]
                        story_urls = None
                    runtime_spec = canonicalize_public_payload(settings, row.runtime_spec)
                    if not isinstance(runtime_spec, dict):
                        raise RuntimeSpecError("persisted runtime spec is missing")
                    clips = runtime_spec.get("video")
                    if not isinstance(clips, list):
                        raise RuntimeSpecError("persisted runtime clips are missing")
                    for clip in clips:
                        if not isinstance(clip, dict):
                            raise RuntimeSpecError("persisted runtime clip is invalid")
                        slot = (
                            str(clip.get("video_id") or "")
                            if story_urls is not None
                            else "single"
                        )
                        clip["video"] = urls[slot]
                    read_runtime_spec(
                        runtime_spec,
                        item_id=row.id,
                        version=row.runtime_spec_version,
                    )
                except (
                    KeyError,
                    MediaServiceError,
                    OssStorageError,
                    RuntimeSpecError,
                    PublicOriginError,
                ) as exc:
                    report.failures.append(
                        MigrationFailure(
                            table="published_videos",
                            row_id=row.id,
                            reason=str(exc),
                        )
                    )
                    continue
                if row.video_url != entry_url:
                    row.video_url = entry_url
                    changed = True
                if row.runtime_spec != runtime_spec:
                    row.runtime_spec = runtime_spec
                    changed = True
            else:
                replacement = canonicalize_public_url(settings, row.video_url)
                if replacement != row.video_url:
                    row.video_url = replacement
                    changed = True
                changed = _set_json(row, "runtime_spec", settings) or changed
            changed = _set_json(row, "timeline", settings) or changed
        elif row.content_type == CONTENT_TYPE_HTML:
            replacement = canonicalize_public_url(settings, row.html_url)
            if replacement != row.html_url:
                row.html_url = replacement
                changed = True
        if changed:
            # Host-only migration must not look like a content revision.
            row.updated_at = original_updated_at
            _mark(report, "published_videos", row.id)


def migrate_public_origins(
    db: Session,
    settings: Settings,
    *,
    apply: bool,
    verify: bool = False,
) -> MigrationReport:
    report = MigrationReport(mode="verify" if verify else "apply" if apply else "dry-run")
    _migrate_published_videos(db, settings, report)

    media_by_id = {
        row.id: row
        for row in db.query(MediaObject)
        .filter(MediaObject.visibility == "public", MediaObject.state == "ready")
        .all()
    }
    for row in db.query(User).order_by(User.user_id.asc()).all():
        replacement = canonicalize_public_url(settings, row.avatar_url)
        media = media_by_id.get(row.avatar_media_object_id or "")
        if media is not None:
            try:
                replacement = canonical_public_url_for_key(settings, media.object_key)
            except (OssStorageError, PublicOriginError) as exc:
                report.failures.append(
                    MigrationFailure("users", row.user_id, str(exc))
                )
                continue
        if replacement != row.avatar_url:
            row.avatar_url = replacement or ""
            _mark(report, "users", row.user_id)

    for row in db.query(HtmlPackage).order_by(HtmlPackage.id.asc()).all():
        replacement = canonicalize_public_url(settings, row.html_url)
        if replacement != row.html_url:
            row.html_url = replacement or ""
            _mark(report, "html_packages", row.id)

    for model, table, attrs in (
        (
            CreatorCreation,
            "creator_creations",
            ("analysis_result", "source_timeline", "runtime_spec"),
        ),
        (
            CreatorVersion,
            "creator_versions",
            ("source_timeline", "runtime_spec"),
        ),
    ):
        for row in db.query(model).order_by(model.id.asc()).all():
            original_updated_at = row.updated_at
            changed = False
            for attr in attrs:
                changed = _set_json(row, attr, settings) or changed
            if changed:
                row.updated_at = original_updated_at
                _mark(report, table, row.id)

    for row in db.query(MediaObject).order_by(MediaObject.id.asc()).all():
        if _set_json(row, "extra_json", settings):
            _mark(report, "media_objects", row.id)

    for row in db.query(MediaUploadSession).order_by(MediaUploadSession.id.asc()).all():
        original_updated_at = row.updated_at
        if _set_json(row, "context", settings):
            row.updated_at = original_updated_at
            _mark(report, "media_upload_sessions", row.id)

    if report.failures or not apply:
        db.rollback()
    else:
        db.commit()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Canonicalize persisted immutable public media URLs to the CDN origin."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="persist all changes atomically")
    mode.add_argument(
        "--verify",
        action="store_true",
        help="fail if any rewrite candidates or invalid publication bindings remain",
    )
    args = parser.parse_args()
    with SessionLocal() as db:
        report = migrate_public_origins(
            db,
            get_settings(),
            apply=args.apply,
            verify=args.verify,
        )
    payload: dict[str, Any] = asdict(report)
    payload["changed_count"] = report.changed_count
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if report.failures:
        return 1
    if args.verify and report.changed_count:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
