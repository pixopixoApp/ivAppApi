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
from app.media_service import media_mode_is_oss
from app.models import CreatorCreation, CreatorUpload, CreatorVersion, MediaObject
from app.protocol_video import (
    RUNTIME_SPEC_VERSION,
    RuntimeSpecError,
    compile_runtime_spec,
)
from app.storage import LocalMediaStorage, StorageError

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
        if upload.media_object_id:
            media = db.get(MediaObject, upload.media_object_id)
            if media is None or media.state != "ready":
                raise CreationError("UPLOAD_MISSING", "The source video is no longer available.")
            response = _request(
                settings,
                "POST",
                "/internal/v1/mobile-creator/jobs/from-media",
                json={
                    "request_id": request_id,
                    "creation_id": creation.id,
                    "brief": version.brief,
                    "media_object_id": media.id,
                    "filename": upload.original_filename,
                    "size_bytes": media.size_bytes,
                    "sha256": media.sha256,
                },
            )
        else:
            if media_mode_is_oss(settings):
                raise CreationError(
                    "UPLOAD_NOT_MIGRATED",
                    "The source video has not been migrated to object storage.",
                )
            try:
                source = LocalMediaStorage(settings).resolve(upload.storage_key)
            except StorageError as exc:
                raise CreationError("UPLOAD_MISSING", "The source video is no longer available.") from exc
            if not source.is_file():
                raise CreationError("UPLOAD_MISSING", "The source video is no longer available.")
            with source.open("rb") as stream:
                response = _request(
                    settings,
                    "POST",
                    "/internal/v1/mobile-creator/jobs",
                    data={
                        "request_id": request_id,
                        "creation_id": creation.id,
                        "brief": version.brief,
                    },
                    files={"video": (upload.original_filename, stream, "video/mp4")},
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
                processed = process_next_creator_version(settings)
        interval = 0.25 if processed else settings.creator_worker_poll_seconds
        time.sleep(max(0.25, interval))
    log.info("creator coordinator stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
