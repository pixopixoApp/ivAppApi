"""FIFO creator coordinator: ivapp state -> private ivadmin jobs.

This process deliberately contains no model or Dify credentials.  ivadmin owns
video analysis through ivcore; this worker only submits, polls and persists the
C-end session/version state.
"""

from __future__ import annotations

import signal
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal, engine
from app.logging_config import get_logger, setup_logging
from app.models import CreatorCreation, CreatorUpload, CreatorVersion
from app.protocol_video import (
    RUNTIME_SPEC_VERSION,
    RuntimeSpecError,
    compile_runtime_spec,
)

log = get_logger(__name__)
_stop = False
_TERMINAL = frozenset({"ready", "failed", "cancelled", "published"})


class CreationError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class CreatorTransportError(RuntimeError):
    """Transient ivadmin/network failure; keep the job pollable."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _handle_signal(_signum, _frame) -> None:
    global _stop
    _stop = True


@contextmanager
def _global_worker_slot():
    """Use a MySQL connection-scoped advisory lock to enforce concurrency 1."""
    connection = engine.connect()
    acquired = True
    try:
        if engine.dialect.name == "mysql":
            acquired = bool(
                connection.execute(
                    text("SELECT GET_LOCK('ivapp_creator_worker_global', 0)")
                ).scalar()
            )
        yield acquired
    finally:
        if acquired and engine.dialect.name == "mysql":
            connection.execute(text("SELECT RELEASE_LOCK('ivapp_creator_worker_global')"))
        connection.close()


def _remote_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500] or f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail[:500]
    return f"HTTP {response.status_code}"


def _request(
    settings: Settings,
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    if not settings.creator_internal_key.strip():
        raise CreationError("IVADMIN_NOT_CONFIGURED", "Creator analysis is not configured.")
    url = f"{settings.ivadmin_base_url.rstrip('/')}{path}"
    headers = dict(kwargs.pop("headers", {}))
    headers["X-Creator-Internal-Key"] = settings.creator_internal_key
    try:
        response = httpx.request(
            method,
            url,
            headers=headers,
            timeout=settings.creator_ivadmin_timeout_seconds,
            **kwargs,
        )
    except httpx.RequestError as exc:
        raise CreatorTransportError(str(exc)) from exc
    if response.status_code >= 500:
        raise CreatorTransportError(_remote_error(response))
    if response.status_code >= 400:
        raise CreationError("IVADMIN_REJECTED", _remote_error(response))
    return response


def _parse_job(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise CreatorTransportError("ivadmin returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise CreatorTransportError("ivadmin returned an invalid job")
    return payload


def _first_run_id(db: Session, creation_id: str) -> str:
    first = (
        db.query(CreatorVersion)
        .filter(
            CreatorVersion.creation_id == creation_id,
            CreatorVersion.ivadmin_run_id != "",
        )
        .order_by(CreatorVersion.number.asc())
        .first()
    )
    if first is None:
        raise CreationError("SOURCE_RUN_MISSING", "The original analysis is unavailable.")
    return first.ivadmin_run_id


def _apply_normalization_payload(
    db: Session,
    upload: CreatorUpload,
    payload: dict[str, Any],
) -> None:
    remote_status = str(payload.get("status") or "").lower()
    if remote_status not in {"pending", "retry_wait", "running", "ready", "failed"}:
        raise CreatorTransportError("ivadmin returned an unknown normalization status")
    upload.normalization_job_id = str(payload.get("job_id") or upload.normalization_job_id)[:64]
    upload.normalization_profile = str(payload.get("profile") or "mobile-v1")[:32]
    upload.source_local_uri = str(payload.get("source_local_uri") or upload.source_local_uri)[:512]
    source_object_id = payload.get("source_media_object_id")
    if source_object_id:
        upload.media_object_id = str(source_object_id)[:64]
    if remote_status == "ready":
        playable_sha = str(payload.get("playable_sha256") or "").lower()
        playable_uri = str(payload.get("playable_local_uri") or "")
        playable_size = int(payload.get("playable_size_bytes") or 0)
        if len(playable_sha) != 64 or not playable_uri or playable_size <= 0:
            raise CreatorTransportError("ivadmin returned incomplete playable metadata")
        upload.normalization_status = "ready"
        upload.playable_sha256 = playable_sha
        upload.playable_local_uri = playable_uri[:512]
        upload.playable_size_bytes = playable_size
        playable_object_id = payload.get("playable_media_object_id")
        if playable_object_id:
            upload.playable_media_object_id = str(playable_object_id)[:64]
        upload.duration_ms = int(payload.get("duration_ms") or upload.duration_ms)
        upload.normalization_error = ""
    elif remote_status == "failed":
        upload.normalization_status = "failed"
        upload.normalization_error = "Video processing failed. Please upload it again."
        log.warning(
            "normalization failed upload_id=%s job_id=%s code=%s detail=%s",
            upload.id,
            upload.normalization_job_id,
            str(payload.get("error_code") or "")[:64],
            str(payload.get("error_message") or "")[:500],
        )
    else:
        upload.normalization_status = "normalizing"
        upload.normalization_error = ""
    db.add(upload)
    db.commit()


def process_upload_normalization(
    db: Session,
    settings: Settings,
    upload: CreatorUpload,
) -> None:
    if not upload.source_sha256:
        raise CreationError("SOURCE_CHECKSUM_MISSING", "The source video checksum is unavailable.")
    if upload.normalization_job_id:
        response = _request(
            settings,
            "GET",
            f"/internal/v1/mobile-creator/normalizations/{upload.normalization_job_id}",
        )
    else:
        response = _request(
            settings,
            "POST",
            "/internal/v1/mobile-creator/normalizations",
            json={
                "request_id": f"normalize:{upload.id}:mobile-v1",
                "owner_type": "creator_upload",
                "owner_id": upload.id,
                "source_sha256": upload.source_sha256,
                "source_size_bytes": upload.size_bytes,
                "source_filename": upload.original_filename,
                "source_media_object_id": upload.media_object_id,
            },
        )
    _apply_normalization_payload(db, upload, _parse_job(response))


def process_next_upload_normalization(settings: Settings | None = None) -> bool:
    """Submit/poll one upload normalization before creator analysis work."""
    settings = settings or get_settings()
    with SessionLocal() as db:
        row = (
            db.query(CreatorUpload)
            .filter(
                (CreatorUpload.normalization_status.in_(("pending", "normalizing")))
                | (
                    (CreatorUpload.normalization_status == "ready")
                    & (
                        (CreatorUpload.media_object_id.is_(None))
                        | (CreatorUpload.playable_media_object_id.is_(None))
                    )
                )
            )
            .order_by(CreatorUpload.created_at.asc())
            .with_for_update(skip_locked=True)
            .first()
        )
        if row is None:
            db.rollback()
            return False
        try:
            process_upload_normalization(db, settings, row)
        except CreatorTransportError as exc:
            db.rollback()
            log.warning("normalization service temporarily unavailable upload_id=%s error=%s", row.id, exc)
        except Exception as exc:
            db.rollback()
            fresh = db.get(CreatorUpload, row.id)
            if fresh is not None:
                fresh.normalization_status = "failed"
                fresh.normalization_error = (
                    str(exc)[:500]
                    if isinstance(exc, CreationError) and str(exc)
                    else "Video processing failed. Please try again."
                )
                db.add(fresh)
                db.commit()
            log.exception("upload normalization failed upload_id=%s", row.id)
        return True


def _submit_job(
    db: Session,
    settings: Settings,
    creation: CreatorCreation,
    version: CreatorVersion,
) -> dict[str, Any]:
    request_id = (
        version.request_id
        if version.retry_count == 0
        else f"{version.request_id}:retry:{version.retry_count}"
    )
    if version.number == 1:
        upload = db.get(CreatorUpload, creation.upload_id)
        if upload is None or upload.user_id != creation.user_id:
            raise CreationError("UPLOAD_MISSING", "The source video is no longer available.")
        if upload.normalization_status == "failed":
            raise CreationError(
                "NORMALIZATION_FAILED",
                upload.normalization_error or "Video processing failed. Please upload it again.",
            )
        if upload.normalization_status != "ready" or not upload.normalization_job_id:
            raise CreatorTransportError("video normalization is still pending")
        response = _request(
            settings,
            "POST",
            "/internal/v1/mobile-creator/jobs/from-normalization",
            json={
                "request_id": request_id,
                "creation_id": creation.id,
                "brief": version.brief,
                "normalization_job_id": upload.normalization_job_id,
            },
        )
    else:
        run_id = _first_run_id(db, creation.id)
        response = _request(
            settings,
            "POST",
            f"/internal/v1/mobile-creator/runs/{run_id}/versions",
            json={
                "request_id": request_id,
                "creation_id": creation.id,
                "brief": version.brief,
            },
        )
    return _parse_job(response)


def _sync_creation(db: Session, creation: CreatorCreation) -> None:
    if creation.status == "abandoned":
        return
    versions = (
        db.query(CreatorVersion)
        .filter(CreatorVersion.creation_id == creation.id)
        .order_by(CreatorVersion.number.asc())
        .all()
    )
    if not versions:
        return
    active = next((item for item in versions if item.status in {"queued", "running"}), None)
    current = active or versions[-1]
    creation.active_version_id = current.id
    creation.status = current.status
    creation.progress_stage = current.progress_stage
    creation.progress_percent = current.progress_percent
    creation.retry_count = current.retry_count
    creation.workflow_run_id = current.ivadmin_job_id
    creation.source_timeline = current.source_timeline
    creation.runtime_spec = current.runtime_spec
    creation.runtime_spec_version = current.runtime_spec_version
    creation.error_code = current.error_code
    creation.error_message = current.error_message
    creation.updated_at = _now()
    db.add(creation)


def _apply_remote_job(
    db: Session,
    creation: CreatorCreation,
    version: CreatorVersion,
    payload: dict[str, Any],
) -> None:
    remote_status = str(payload.get("status") or "").lower()
    if remote_status not in {"queued", "running", "ready", "failed", "cancelled"}:
        raise CreatorTransportError("ivadmin returned an unknown creator status")
    version.ivadmin_job_id = str(payload.get("job_id") or version.ivadmin_job_id)
    version.ivadmin_run_id = str(payload.get("run_id") or version.ivadmin_run_id)
    version.ivadmin_version = str(payload.get("version") or version.ivadmin_version)
    version.progress_stage = str(payload.get("progress_stage") or remote_status)[:64]
    try:
        version.progress_percent = max(0, min(100, int(payload.get("progress_percent") or 0)))
    except (TypeError, ValueError):
        version.progress_percent = 0

    if remote_status in {"queued", "running"}:
        version.status = "running"
    elif remote_status == "cancelled":
        version.status = "cancelled"
        version.progress_stage = "cancelled"
        version.error_code = "CANCELLED"
        version.error_message = "Creation was cancelled."
    elif remote_status == "failed":
        version.status = "failed"
        version.progress_stage = "failed"
        version.error_code = str(payload.get("error_code") or "ANALYSIS_FAILED")[:64]
        version.error_message = str(
            payload.get("error_message") or "Creation failed. Please try again."
        )[:500]
    else:
        timeline = payload.get("timeline")
        if not isinstance(timeline, dict):
            raise CreationError("TIMELINE_MISSING", "Analysis completed without a timeline.")
        try:
            runtime = compile_runtime_spec(
                item_id=f"{creation.id}-v{version.number}",
                content_mode="single",
                source=timeline,
                video_url=f"/api/v1/creator/previews/{creation.upload_id}",
            )
        except RuntimeSpecError as exc:
            raise CreationError("RUNTIME_COMPILE_FAILED", str(exc)) from exc
        version.status = "ready"
        version.progress_stage = "ready"
        version.progress_percent = 100
        version.source_timeline = timeline
        version.runtime_spec = runtime
        version.runtime_spec_version = RUNTIME_SPEC_VERSION
        version.error_code = ""
        version.error_message = ""
    version.updated_at = _now()
    db.add(version)
    _sync_creation(db, creation)
    db.commit()


def _cancel_remote(settings: Settings, version: CreatorVersion) -> dict[str, Any] | None:
    if not version.ivadmin_job_id:
        return None
    response = _request(
        settings,
        "POST",
        f"/internal/v1/mobile-creator/jobs/{version.ivadmin_job_id}/cancel",
    )
    return _parse_job(response)


def process_creator_version(
    db: Session,
    settings: Settings,
    version: CreatorVersion,
) -> None:
    creation = db.get(CreatorCreation, version.creation_id)
    if creation is None or creation.user_id != version.user_id:
        raise CreationError("CREATION_MISSING", "Creator session no longer exists.")

    if version.number == 1 and not version.ivadmin_job_id:
        upload = db.get(CreatorUpload, creation.upload_id)
        if upload is not None and upload.normalization_status in {"pending", "normalizing"}:
            version.status = "running"
            version.progress_stage = "normalize_video"
            version.progress_percent = max(version.progress_percent, 10)
            version.updated_at = _now()
            db.add(version)
            _sync_creation(db, creation)
            db.commit()
            return

    if version.cancel_requested:
        payload = _cancel_remote(settings, version)
        if payload is None:
            version.status = "cancelled"
            version.progress_stage = "cancelled"
            version.error_code = "CANCELLED"
            version.error_message = "Creation was cancelled."
            version.updated_at = _now()
            _sync_creation(db, creation)
            db.commit()
            return
        _apply_remote_job(db, creation, version, payload)
        return

    if not version.ivadmin_job_id:
        payload = _submit_job(db, settings, creation, version)
    else:
        response = _request(
            settings,
            "GET",
            f"/internal/v1/mobile-creator/jobs/{version.ivadmin_job_id}",
        )
        payload = _parse_job(response)
    _apply_remote_job(db, creation, version, payload)


def _claim_next(db: Session) -> CreatorVersion | None:
    # A global single consumer plus number ordering gives deterministic FIFO.
    # The extra predecessor check keeps that guarantee if more workers are ever
    # enabled accidentally.
    candidates = (
        db.query(CreatorVersion)
        .filter(CreatorVersion.status.in_(("queued", "running")))
        .order_by(CreatorVersion.created_at.asc(), CreatorVersion.number.asc())
        .with_for_update(skip_locked=True)
        .limit(100)
        .all()
    )
    for row in candidates:
        predecessor = (
            db.query(CreatorVersion)
            .filter(
                CreatorVersion.creation_id == row.creation_id,
                CreatorVersion.number < row.number,
                CreatorVersion.status.in_(("queued", "running")),
            )
            .first()
        )
        if predecessor is not None:
            continue
        if row.status == "queued":
            row.status = "running"
            row.progress_stage = "queued"
            row.progress_percent = max(1, row.progress_percent)
            row.updated_at = _now()
            creation = db.get(CreatorCreation, row.creation_id)
            if creation is not None:
                _sync_creation(db, creation)
            db.commit()
            db.refresh(row)
        return row
    db.rollback()
    return None


def _fail(db: Session, version: CreatorVersion, exc: Exception) -> None:
    db.rollback()
    fresh = db.get(CreatorVersion, version.id)
    if fresh is None:
        return
    fresh.status = "failed"
    fresh.progress_stage = "failed"
    fresh.error_code = exc.code if isinstance(exc, CreationError) else "CREATION_FAILED"
    fresh.error_message = (
        str(exc)[:500] if isinstance(exc, CreationError) and str(exc) else "Creation failed. Please try again."
    )
    fresh.updated_at = _now()
    creation = db.get(CreatorCreation, fresh.creation_id)
    if creation is not None:
        _sync_creation(db, creation)
    db.commit()
    log.exception("creator version failed version_id=%s code=%s", fresh.id, fresh.error_code)


def process_next_creator_version(settings: Settings | None = None) -> bool:
    """One deterministic coordinator tick; public for integration tests."""
    settings = settings or get_settings()
    with SessionLocal() as db:
        row = _claim_next(db)
        if row is None:
            return False
        try:
            process_creator_version(db, settings, row)
        except CreatorTransportError as exc:
            db.rollback()
            log.warning("ivadmin temporarily unavailable version_id=%s error=%s", row.id, exc)
        except Exception as exc:  # noqa: BLE001 - persist product-safe error
            _fail(db, row, exc)
        return True


def main() -> int:
    settings = get_settings()
    setup_logging(level=settings.log_level)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    log.info("creator coordinator started global_concurrency=1")

    # A submission with no remote job id is safe to replay: request_id makes
    # the private ivadmin endpoint idempotent. Existing remote jobs stay running
    # and are simply polled after a process restart.
    while not _stop:
        processed = False
        with _global_worker_slot() as acquired:
            if acquired:
                normalized = process_next_upload_normalization(settings)
                created = process_next_creator_version(settings)
                processed = normalized or created
        interval = 0.25 if processed else settings.creator_worker_poll_seconds
        time.sleep(max(0.25, interval))
    log.info("creator coordinator stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
