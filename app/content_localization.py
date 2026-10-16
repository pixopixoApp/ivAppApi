from __future__ import annotations

import hashlib
import json
import re
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import Settings
from app.locale import SUPPORTED_LOCALES, canonical_locale
from app.logging_config import get_logger
from app.models import ContentLocalization, ContentTranslationJob, PublishedVideo

ENTITY_PUBLISHED_VIDEO = "published_video"
PROMPT_VERSION = "runtime-content-v1"
MAX_ATTEMPTS = 3
_BCP47_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_PRESERVE_RE = re.compile(
    r"https?://[^\s\]\[()<>]+|@[A-Za-z0-9_.-]+|\{[^{}]+\}|\b(?:Pixo|Pixopixo|Credits)\b"
)
log = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any, *, limit: int) -> str:
    return str(value or "").strip()[:limit]


def extract_source_payload(row: PublishedVideo) -> dict[str, Any]:
    interactions: dict[str, dict[str, str]] = {}
    spec = row.runtime_spec if isinstance(row.runtime_spec, dict) else {}
    clips = spec.get("video") if isinstance(spec, dict) else None
    if isinstance(clips, list):
        for clip in clips:
            if not isinstance(clip, dict):
                continue
            video_id = _text(clip.get("video_id"), limit=128)
            for interaction in clip.get("interactions") or []:
                if not isinstance(interaction, dict):
                    continue
                interaction_id = _text(interaction.get("id"), limit=128)
                if not video_id or not interaction_id:
                    continue
                visible = {
                    "description": _text(interaction.get("description"), limit=300),
                    "success_text": _text(interaction.get("success_text"), limit=300),
                    "miss_text": _text(interaction.get("miss_text"), limit=300),
                }
                interactions[f"{video_id}:{interaction_id}"] = visible
    return {
        "title": _text(row.title, limit=120),
        "description": _text(row.description, limit=4000),
        "interactions": interactions,
    }


def source_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def queue_content_translation(
    db: Session,
    row: PublishedVideo,
    *,
    source_locale_hint: str | None = None,
) -> ContentTranslationJob | None:
    """Queue without waiting for a model; HTML is intentionally unsupported."""
    if (row.content_type or "runtime") != "runtime":
        return None
    payload = extract_source_payload(row)
    digest = source_hash(payload)
    source_locale = canonical_locale(source_locale_hint) or "en"
    existing = (
        db.query(ContentTranslationJob)
        .filter(
            ContentTranslationJob.entity_type == ENTITY_PUBLISHED_VIDEO,
            ContentTranslationJob.entity_id == row.id,
            ContentTranslationJob.source_hash == digest,
            ContentTranslationJob.status.in_(("queued", "running", "partial", "ready")),
        )
        .order_by(ContentTranslationJob.created_at.desc())
        .first()
    )
    if existing is not None:
        return existing
    stale = (
        db.query(ContentTranslationJob)
        .filter(
            ContentTranslationJob.entity_type == ENTITY_PUBLISHED_VIDEO,
            ContentTranslationJob.entity_id == row.id,
            ContentTranslationJob.source_hash != digest,
            ContentTranslationJob.status.in_(
                ("queued", "running", "partial", "ready", "failed")
            ),
        )
        .all()
    )
    for old in stale:
        old.status = "stale"
        old.lease_until = None
        old.updated_at = _now()
    old_localizations = (
        db.query(ContentLocalization)
        .filter(
            ContentLocalization.entity_type == ENTITY_PUBLISHED_VIDEO,
            ContentLocalization.entity_id == row.id,
            ContentLocalization.source_hash != digest,
            ContentLocalization.status == "ready",
        )
        .all()
    )
    for localization in old_localizations:
        localization.status = "stale"
        localization.updated_at = _now()
    job_id = uuid.uuid4().hex
    # Detection is authoritative and happens in the same model call. Requesting
    # all nine keeps a wrong/missing hint from omitting the actual English target;
    # the detected source entry is validated and stored like every other locale.
    target_locales = list(SUPPORTED_LOCALES)
    job = ContentTranslationJob(
        id=job_id,
        entity_type=ENTITY_PUBLISHED_VIDEO,
        entity_id=row.id,
        source_hash=digest,
        source_locale=source_locale,
        target_locales=target_locales,
        source_payload=payload,
        status="queued",
        # Keep this identifier independent of the user-controlled entity id.
        # PublishedVideo ids may be 128 characters while both this column and
        # ivadmin's request schema intentionally cap request ids at 96.
        request_id=f"translation:{job_id}",
    )
    db.add(job)
    return job


def _valid_text(source: Any, candidate: Any, *, limit: int) -> str | None:
    if not isinstance(source, str) or not isinstance(candidate, str):
        return None
    value = candidate.strip()
    if bool(source.strip()) != bool(value) or len(value) > limit:
        return None
    if any(token not in value for token in _PRESERVE_RE.findall(source)):
        return None
    return value


def _valid_payload(candidate: Any, source: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None
    title = candidate.get("title")
    description = candidate.get("description")
    interactions = candidate.get("interactions")
    if not isinstance(title, str) or not isinstance(description, str):
        return None
    normalized_title = _valid_text(source.get("title"), title, limit=120)
    normalized_description = _valid_text(
        source.get("description"),
        description,
        limit=4000,
    )
    if normalized_title is None or normalized_description is None:
        return None
    source_interactions = source.get("interactions") or {}
    if not isinstance(interactions, dict) or set(interactions) != set(source_interactions):
        return None
    normalized_interactions: dict[str, dict[str, str]] = {}
    for stable_id, value in interactions.items():
        if not isinstance(value, dict) or set(value) != {
            "description", "success_text", "miss_text"
        }:
            return None
        normalized: dict[str, str] = {}
        for field in ("description", "success_text", "miss_text"):
            text = _valid_text(
                source_interactions[stable_id].get(field),
                value.get(field),
                limit=300,
            )
            if text is None:
                return None
            normalized[field] = text
        normalized_interactions[stable_id] = normalized
    return {
        "title": normalized_title,
        "description": normalized_description,
        "interactions": normalized_interactions,
    }


def _upsert_localization(
    db: Session,
    job: ContentTranslationJob,
    *,
    locale: str,
    payload: dict[str, Any],
    source_locale: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    row = (
        db.query(ContentLocalization)
        .filter(
            ContentLocalization.entity_type == job.entity_type,
            ContentLocalization.entity_id == job.entity_id,
            ContentLocalization.locale == locale,
        )
        .one_or_none()
    )
    if row is None:
        row = ContentLocalization(
            entity_type=job.entity_type,
            entity_id=job.entity_id,
            locale=locale,
            source_locale=source_locale,
            source_hash=job.source_hash,
            payload_json=payload,
        )
        db.add(row)
    elif row.source_hash != job.source_hash:
        row.source_hash = job.source_hash
        row.payload_json = payload
    else:
        row.payload_json = payload
    row.source_locale = source_locale
    row.status = "ready"
    row.model = model[:128]
    row.prompt_version = PROMPT_VERSION
    row.input_tokens = max(0, input_tokens)
    row.output_tokens = max(0, output_tokens)
    row.last_error = ""
    row.updated_at = _now()


def apply_translation_result(
    db: Session,
    job: ContentTranslationJob,
    body: dict[str, Any],
) -> None:
    current = db.get(PublishedVideo, job.entity_id)
    if current is None or source_hash(extract_source_payload(current)) != job.source_hash:
        job.status = "stale"
        job.lease_until = None
        job.updated_at = _now()
        db.commit()
        return
    # A retried delivery after a successful commit must not double-count token
    # usage or rewrite the same revision. Source changes are checked above so a
    # late response still marks the old job stale instead of being accepted.
    if job.status == "ready" and job.completed_at is not None:
        return
    raw_detected = str(body.get("detected_source_locale") or "").strip()
    detected = canonical_locale(raw_detected)
    source_locale = detected or (
        raw_detected[:16] if _BCP47_RE.fullmatch(raw_detected) else job.source_locale
    )
    translations = body.get("translations")
    if not isinstance(translations, dict):
        raise TypeError("translations must be an object")
    model = str(body.get("model") or "")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    job.source_locale = source_locale
    valid_locales: list[str] = []
    for raw_locale, candidate in translations.items():
        locale = canonical_locale(str(raw_locale))
        if locale not in set(job.target_locales or []):
            continue
        payload = _valid_payload(candidate, job.source_payload)
        if payload is None:
            continue
        # The canonical creator text remains authoritative for its own locale.
        # The model may echo it because detection and all targets share one
        # request, but storing that echo would mislabel original text as MT.
        if detected is not None and locale == detected:
            valid_locales.append(locale)
            continue
        _upsert_localization(
            db,
            job,
            locale=locale,
            payload=payload,
            source_locale=source_locale,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        valid_locales.append(locale)
    missing = [locale for locale in (job.target_locales or []) if locale not in valid_locales]
    job.input_tokens += max(0, input_tokens)
    job.output_tokens += max(0, output_tokens)
    job.lease_until = None
    job.updated_at = _now()
    if missing and job.attempt < MAX_ATTEMPTS:
        job.target_locales = missing
        job.status = "partial"
        job.next_retry_at = _now() + timedelta(seconds=2 ** max(job.attempt, 1))
        job.last_error = f"missing or invalid locales: {','.join(missing)}"
    elif missing:
        job.status = "failed"
        job.completed_at = _now()
        job.last_error = f"missing or invalid locales: {','.join(missing)}"
    else:
        job.status = "ready"
        job.completed_at = _now()
        job.next_retry_at = None
        job.last_error = ""
    db.commit()


def _claim_next(db: Session) -> ContentTranslationJob | None:
    now = _now()
    expired = (
        db.query(ContentTranslationJob)
        .filter(
            ContentTranslationJob.status == "running",
            ContentTranslationJob.lease_until.is_not(None),
            ContentTranslationJob.lease_until <= now,
        )
        .all()
    )
    for job in expired:
        job.lease_until = None
        job.updated_at = now
        job.last_error = "translation worker lease expired"
        if job.attempt >= MAX_ATTEMPTS:
            job.status = "failed"
            job.completed_at = now
        else:
            job.status = "partial"
            job.next_retry_at = now
    if expired:
        db.commit()
    row = (
        db.query(ContentTranslationJob)
        .filter(
            ContentTranslationJob.status.in_(("queued", "partial")),
            (ContentTranslationJob.next_retry_at.is_(None))
            | (ContentTranslationJob.next_retry_at <= now),
        )
        .order_by(ContentTranslationJob.created_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if row is None:
        db.rollback()
        return None
    row.status = "running"
    row.attempt += 1
    row.started_at = row.started_at or now
    row.lease_until = now + timedelta(minutes=3)
    row.updated_at = now
    db.commit()
    db.refresh(row)
    return row


def process_next_content_translation(
    db: Session,
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> bool:
    job = _claim_next(db)
    if job is None:
        return False
    url = f"{settings.ivadmin_base_url.rstrip('/')}/internal/v1/content-localizations/batch"
    try:
        with httpx.Client(
            timeout=settings.content_translation_request_timeout_seconds,
            trust_env=False,
            transport=transport,
        ) as client:
            response = client.post(
                url,
                headers={"X-Creator-Internal-Key": settings.creator_internal_key},
                json={
                    "request_id": job.request_id,
                    "entity_type": job.entity_type,
                    "entity_id": job.entity_id,
                    "source_hash": job.source_hash,
                    "source_locale_hint": job.source_locale,
                    "target_locales": job.target_locales,
                    "source": job.source_payload,
                },
            )
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict):
            raise TypeError("ivadmin returned invalid JSON")
        apply_translation_result(db, job, body)
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        db.rollback()
        fresh = db.get(ContentTranslationJob, job.id)
        if fresh is None:
            return True
        fresh.lease_until = None
        fresh.updated_at = _now()
        fresh.last_error = str(exc)[:1000]
        if fresh.attempt >= MAX_ATTEMPTS:
            fresh.status = "failed"
            fresh.completed_at = _now()
        else:
            fresh.status = "partial"
            fresh.next_retry_at = _now() + timedelta(seconds=2 ** fresh.attempt)
        db.commit()
        log.warning("content translation failed job_id=%s error=%s", fresh.id, exc)
    return True


def localized_content(
    db: Session,
    row: PublishedVideo,
    locale: str,
    *,
    enabled: bool,
) -> tuple[str, str, dict[str, Any] | None, dict[str, Any]]:
    source_locale = "en"
    base = {
        "requested_locale": locale,
        "resolved_locale": locale,
        "source_locale": source_locale,
        "status": "source",
        "machine_translated": False,
    }
    if (row.content_type or "runtime") == "html":
        return row.title or "", row.description or "", row.runtime_spec, {
            **base,
            "status": "unsupported_content_type",
        }
    if not enabled:
        return row.title or "", row.description or "", row.runtime_spec, base
    digest = source_hash(extract_source_payload(row))
    current_job = (
        db.query(ContentTranslationJob)
        .filter(
            ContentTranslationJob.entity_type == ENTITY_PUBLISHED_VIDEO,
            ContentTranslationJob.entity_id == row.id,
            ContentTranslationJob.source_hash == digest,
            ContentTranslationJob.status != "stale",
        )
        .order_by(ContentTranslationJob.created_at.desc())
        .first()
    )
    if current_job is not None:
        source_locale = current_job.source_locale
        base["source_locale"] = source_locale
    if canonical_locale(source_locale) == locale:
        return row.title or "", row.description or "", row.runtime_spec, base
    found = (
        db.query(ContentLocalization)
        .filter(
            ContentLocalization.entity_type == ENTITY_PUBLISHED_VIDEO,
            ContentLocalization.entity_id == row.id,
            ContentLocalization.locale == locale,
            ContentLocalization.source_hash == digest,
            ContentLocalization.status == "ready",
        )
        .one_or_none()
    )
    if found is None:
        return row.title or "", row.description or "", row.runtime_spec, {
            **base,
            "status": "pending",
        }
    payload = found.payload_json or {}
    translated_spec = deepcopy(row.runtime_spec)
    translations = payload.get("interactions") or {}
    if isinstance(translated_spec, dict) and isinstance(translations, dict):
        for clip in translated_spec.get("video") or []:
            if not isinstance(clip, dict):
                continue
            video_id = str(clip.get("video_id") or "")
            for interaction in clip.get("interactions") or []:
                if not isinstance(interaction, dict):
                    continue
                value = translations.get(f"{video_id}:{interaction.get('id') or ''}")
                if not isinstance(value, dict):
                    continue
                for field in ("description", "success_text", "miss_text"):
                    if isinstance(value.get(field), str):
                        interaction[field] = value[field]
    return (
        str(payload.get("title") or row.title or ""),
        str(payload.get("description") or row.description or ""),
        translated_spec,
        {
            "requested_locale": locale,
            "resolved_locale": locale,
            "source_locale": found.source_locale,
            "status": "ready",
            "machine_translated": True,
        },
    )
