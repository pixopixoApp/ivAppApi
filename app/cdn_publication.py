from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    CdnCacheJob,
    CdnPublicationGate,
    CreatorCreation,
    PublishedVideo,
)
from app.public_text import record_creator_creation_text, record_published_video_text


class CdnPublicationError(RuntimeError):
    pass


_STAGED_FIELDS = {
    "content_type",
    "video_url",
    "timeline",
    "runtime_spec",
    "runtime_spec_version",
    "html_url",
    "bridge_version",
    "required_capabilities",
    "active_publication_id",
    "html_package_id",
    "version",
    "title",
    "description",
    "cover_media_object_id",
    "user_id",
    "content_mode",
    "feed_weight",
    "content_source",
    "review_status",
    "is_tutorial",
    "is_deleted",
    "deleted_at",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def require_runtime_cdn_gate(settings: Settings) -> None:
    """Fail closed: OSS runtime publications must never bypass CDN readiness."""
    if not settings.cdn_cache_enabled or not settings.cdn_prefetch_on_publish:
        raise CdnPublicationError(
            "CDN publication gate is unavailable; publication was not activated"
        )


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _apply_payload(
    db: Session,
    gate: CdnPublicationGate,
    row: PublishedVideo,
) -> None:
    payload = dict(gate.staged_payload or {})
    unknown = set(payload) - _STAGED_FIELDS
    if unknown:
        raise CdnPublicationError(
            f"unsupported staged publication fields: {', '.join(sorted(unknown))}"
        )
    if bool(payload.get("is_tutorial")):
        (
            db.query(PublishedVideo)
            .filter(
                PublishedVideo.id != row.id,
                PublishedVideo.is_tutorial.is_(True),
            )
            .update({PublishedVideo.is_tutorial: False}, synchronize_session=False)
        )
    for field, value in payload.items():
        setattr(row, field, value)
    row.cdn_ready = True
    row.updated_at = _now()
    db.add(row)
    record_published_video_text(db, row)
    creation = db.get(CreatorCreation, row.id)
    if creation is not None and isinstance(row.runtime_spec, dict):
        creation.runtime_spec = row.runtime_spec
        creation.runtime_spec_version = row.runtime_spec_version
        creation.updated_at = row.updated_at
        db.add(creation)
        record_creator_creation_text(db, creation)


def stage_publication_gate(
    db: Session,
    *,
    video_id: str,
    publication_id: str,
    urls: Iterable[str],
    staged_payload: dict[str, Any] | None = None,
) -> CdnPublicationGate:
    normalized = sorted({str(url).strip() for url in urls if str(url).strip()})
    if not normalized:
        raise CdnPublicationError("runtime publication has no CDN URLs")
    now = _now()
    gate = db.get(CdnPublicationGate, publication_id)
    if gate is not None:
        if gate.video_id != video_id or sorted(gate.urls or []) != normalized:
            raise CdnPublicationError("publication gate identity mismatch")
        if gate.state == "superseded":
            raise CdnPublicationError(
                gate.error_message or "publication was superseded by a newer version"
            )
        gate.staged_payload = dict(staged_payload or {})
        gate.updated_at = now
        if gate.state == "active":
            row = db.get(PublishedVideo, video_id)
            if row is None:
                raise CdnPublicationError("active publication row is missing")
            _apply_payload(db, gate, row)
        db.add(gate)
        return gate

    (
        db.query(CdnPublicationGate)
        .filter(
            CdnPublicationGate.video_id == video_id,
            CdnPublicationGate.state == "warming",
        )
        .update(
            {
                CdnPublicationGate.state: "superseded",
                CdnPublicationGate.error_message: "superseded by a newer publication",
                CdnPublicationGate.updated_at: now,
            },
            synchronize_session=False,
        )
    )
    gate = CdnPublicationGate(
        publication_id=publication_id,
        video_id=video_id,
        urls=normalized,
        staged_payload=dict(staged_payload or {}),
        state="warming",
        error_message="",
        created_at=now,
        updated_at=now,
        activated_at=None,
    )
    db.add(gate)
    return gate


def _jobs_for_gate(db: Session, gate: CdnPublicationGate) -> list[CdnCacheJob]:
    hashes = {_url_hash(url) for url in gate.urls or []}
    if not hashes:
        return []
    return (
        db.query(CdnCacheJob)
        .filter(
            CdnCacheJob.operation == "prefetch",
            CdnCacheJob.url_hash.in_(hashes),
        )
        .all()
    )


def activate_ready_publications(
    db: Session,
    *,
    publication_ids: Iterable[str] | None = None,
    limit: int = 50,
) -> int:
    query = db.query(CdnPublicationGate).filter(
        CdnPublicationGate.state == "warming"
    )
    selected = sorted({str(value) for value in publication_ids or [] if str(value)})
    if selected:
        query = query.filter(CdnPublicationGate.publication_id.in_(selected))
    gates = (
        query.order_by(CdnPublicationGate.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(max(1, min(100, int(limit))))
        .all()
    )
    changed = 0
    now = _now()
    for gate in gates:
        expected = {_url_hash(url) for url in gate.urls or []}
        jobs = _jobs_for_gate(db, gate)
        by_hash = {job.url_hash: job for job in jobs}
        if expected - set(by_hash):
            continue
        failed = [job for job in jobs if job.state == "failed"]
        if failed:
            gate.state = "failed"
            gate.error_message = (
                failed[0].error_message or "CDN prefetch failed"
            )[:500]
            gate.updated_at = now
            db.add(gate)
            changed += 1
            continue
        if not jobs or any(job.state != "succeeded" for job in jobs):
            continue
        row = db.get(PublishedVideo, gate.video_id)
        if row is None:
            gate.state = "failed"
            gate.error_message = "staged published video row is missing"
            gate.updated_at = now
            db.add(gate)
            changed += 1
            continue
        try:
            _apply_payload(db, gate, row)
        except CdnPublicationError as exc:
            gate.state = "failed"
            gate.error_message = str(exc)[:500]
            gate.updated_at = now
            db.add(gate)
            changed += 1
            continue
        gate.state = "active"
        gate.error_message = ""
        gate.activated_at = now
        gate.updated_at = now
        db.add(gate)
        changed += 1
    return changed


def cancel_warming_publications(
    db: Session,
    *,
    video_id: str,
    reason: str = "publication cancelled",
) -> int:
    now = _now()
    return int(
        db.query(CdnPublicationGate)
        .filter(
            CdnPublicationGate.video_id == video_id,
            CdnPublicationGate.state == "warming",
        )
        .update(
            {
                CdnPublicationGate.state: "superseded",
                CdnPublicationGate.error_message: reason[:500],
                CdnPublicationGate.updated_at: now,
            },
            synchronize_session=False,
        )
    )
