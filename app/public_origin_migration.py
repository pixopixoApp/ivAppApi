from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.html_content import CONTENT_TYPE_HTML, CONTENT_TYPE_RUNTIME
from app.media_service import MediaServiceError
from app.models import (
    AppVersion,
    CdnCacheJob,
    CdnPublicationGate,
    CreatorCreation,
    CreatorSourceGeneration,
    CreatorVersion,
    HtmlPackage,
    MediaObject,
    MediaUploadSession,
    PublishedVideo,
    PublishedVideoSeo,
    User,
)
from app.oss_storage import OssStorageError
from app.protocol_video import RuntimeSpecError, read_runtime_spec
from app.public_origin import (
    PublicOriginError,
    canonical_public_origin,
    canonical_public_url_for_key,
    canonicalize_public_url,
    public_media_origins,
    public_path_prefix,
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


_LEGACY_DELIVERY_ORIGINS = frozenset(
    {
        "https://video.pixopixo.cn",
        "https://cdn.pixopixo.cn",
        "https://pixopixo-us.oss-us-east-1.aliyuncs.com",
    }
)


def _mark(report: MigrationReport, table: str, row_id: object) -> None:
    report.changes[table] = report.changes.get(table, 0) + 1
    report.changed_ids.setdefault(table, []).append(str(row_id))


def _canonicalize_delivery_url(settings: Settings, raw: str) -> str:
    """Canonicalize managed paths plus legacy delivery paths outside v1."""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw
    if parsed.fragment or parsed.scheme.lower() != "https" or not parsed.hostname:
        return raw
    source_origin = f"https://{parsed.hostname.lower()}"
    canonical = urlsplit(canonical_public_origin(settings))
    if source_origin in public_media_origins(settings):
        if parsed.path.startswith(public_path_prefix(settings)):
            return urlunsplit((canonical.scheme, canonical.netloc, parsed.path, "", ""))
        private_prefix = f"/{settings.oss_root_prefix.strip('/')}/private/"
        if parsed.path.startswith(private_prefix):
            return urlunsplit(
                (canonical.scheme, canonical.netloc, parsed.path, parsed.query, "")
            )
    if source_origin in _LEGACY_DELIVERY_ORIGINS and parsed.path.startswith("/"):
        return urlunsplit(
            (canonical.scheme, canonical.netloc, parsed.path, parsed.query, "")
        )
    return raw


def _canonicalize_delivery_payload(settings: Settings, value: Any) -> Any:
    if isinstance(value, str):
        return _canonicalize_delivery_url(settings, value)
    if isinstance(value, dict):
        return {
            key: _canonicalize_delivery_payload(settings, item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_canonicalize_delivery_payload(settings, item) for item in value]
    if isinstance(value, tuple):
        return tuple(_canonicalize_delivery_payload(settings, item) for item in value)
    return value


def _set_json(row: object, attr: str, settings: Settings) -> bool:
    current = getattr(row, attr)
    replacement = _canonicalize_delivery_payload(settings, current)
    if replacement == current:
        return False
    setattr(row, attr, replacement)
    return True


def _cdn_job_target(settings: Settings, raw: str) -> tuple[str, str]:
    """Return stable identity and submission URL for one persisted CDN job."""
    value = str(raw or "").strip()
    parsed = urlsplit(value)
    root = settings.oss_root_prefix.strip("/")
    private_prefix = f"/{root}/private/"
    public_url = canonicalize_public_url(settings, value)
    if public_url != value or parsed.path.startswith(f"/{root}/public/"):
        normalized = public_url or value
        return normalized, normalized
    source_origin = (
        f"{parsed.scheme.lower()}://{parsed.hostname.lower()}"
        if parsed.scheme.lower() == "https" and parsed.hostname
        else ""
    )
    if (
        parsed.path.startswith(private_prefix)
        and source_origin in public_media_origins(settings)
    ):
        canonical = urlsplit(canonical_public_origin(settings))
        submission = urlunsplit(
            (canonical.scheme, canonical.netloc, parsed.path, parsed.query, "")
        )
        identity = urlunsplit(
            (canonical.scheme, canonical.netloc, parsed.path, "", "")
        )
        return identity, submission
    return value, value


def _migrate_cdn_jobs(
    db: Session,
    settings: Settings,
    report: MigrationReport,
) -> None:
    for row in db.query(CdnCacheJob).order_by(CdnCacheJob.id.asc()).all():
        identity, submission = _cdn_job_target(settings, row.url)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        expected_id = f"cdn_{row.operation}_{digest[:40]}"
        if (
            row.id == expected_id
            and row.url_hash == digest
            and row.url == submission
        ):
            continue
        collision = db.get(CdnCacheJob, expected_id)
        if collision is not None and collision is not row:
            if (
                collision.operation == row.operation
                and collision.url_hash == digest
                and collision.url == submission
            ):
                # A previous partial cutover may already have created the
                # canonical job while retaining the legacy identity row. The
                # canonical row is the durable winner, including its provider
                # completion state.
                db.delete(row)
                _mark(report, "cdn_cache_jobs", row.id)
                continue
            report.failures.append(
                MigrationFailure(
                    "cdn_cache_jobs",
                    row.id,
                    f"canonical job id collides with {collision.id}",
                )
            )
            continue
        original_updated_at = row.updated_at
        identity_changed = row.id != expected_id or row.url_hash != digest
        row.id = expected_id
        row.url_hash = digest
        row.url = submission
        public_prefix = f"/{settings.oss_root_prefix.strip('/')}/public/"
        if identity_changed and urlsplit(identity).path.startswith(public_prefix):
            # A provider task submitted for the legacy origin cannot prove that
            # the canonical CDN URL is warm. Re-submit public objects after the
            # cutover instead of carrying a stale succeeded state forward.
            row.state = "pending"
            row.attempts = 0
            row.next_attempt_at = datetime.now(timezone.utc)
            row.lease_expires_at = None
            row.provider_task_id = ""
            row.request_id = ""
            row.error_message = ""
        else:
            row.updated_at = original_updated_at
        _mark(report, "cdn_cache_jobs", expected_id)


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
                    runtime_spec = _canonicalize_delivery_payload(settings, row.runtime_spec)
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
                replacement = _canonicalize_delivery_url(settings, row.video_url or "")
                if replacement != row.video_url:
                    row.video_url = replacement
                    changed = True
                changed = _set_json(row, "runtime_spec", settings) or changed
            changed = _set_json(row, "timeline", settings) or changed
        elif row.content_type == CONTENT_TYPE_HTML:
            replacement = _canonicalize_delivery_url(settings, row.html_url or "")
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

    for row in db.query(PublishedVideoSeo).order_by(PublishedVideoSeo.video_id.asc()).all():
        replacement = _canonicalize_delivery_url(settings, row.thumbnail_url or "")
        if replacement != row.thumbnail_url:
            original_updated_at = row.updated_at
            row.thumbnail_url = replacement or ""
            row.updated_at = original_updated_at
            _mark(report, "published_video_seo", row.video_id)

    media_by_id = {
        row.id: row
        for row in db.query(MediaObject)
        .filter(MediaObject.visibility == "public", MediaObject.state == "ready")
        .all()
    }
    for row in db.query(User).order_by(User.user_id.asc()).all():
        replacement = _canonicalize_delivery_url(settings, row.avatar_url or "")
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
        replacement = _canonicalize_delivery_url(settings, row.html_url or "")
        if replacement != row.html_url:
            row.html_url = replacement or ""
            _mark(report, "html_packages", row.id)

    for model, table, attrs in (
        (
            CreatorCreation,
            "creator_creations",
            ("analysis_result", "source_timeline", "runtime_spec", "story_plan"),
        ),
        (
            CreatorVersion,
            "creator_versions",
            ("source_timeline", "runtime_spec", "previewed_paths"),
        ),
        (
            CreatorSourceGeneration,
            "creator_source_generations",
            ("input_json", "preset_json"),
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

    for row in db.query(CdnPublicationGate).order_by(CdnPublicationGate.publication_id.asc()).all():
        original_updated_at = row.updated_at
        changed = _set_json(row, "urls", settings)
        changed = _set_json(row, "staged_payload", settings) or changed
        if changed:
            row.updated_at = original_updated_at
            _mark(report, "cdn_publication_gates", row.publication_id)

    for row in db.query(AppVersion).order_by(AppVersion.platform.asc()).all():
        replacement = _canonicalize_delivery_url(settings, row.store_url or "")
        if replacement != row.store_url:
            original_updated_at = row.updated_at
            row.store_url = replacement or ""
            row.updated_at = original_updated_at
            _mark(report, "app_versions", row.platform)

    _migrate_cdn_jobs(db, settings, report)

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
