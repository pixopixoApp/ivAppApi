from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol
from urllib.parse import urlsplit

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import (
    CdnCacheJob,
    HtmlPackageAsset,
    MediaObject,
    PublishedMediaAsset,
    PublishedVideo,
    User,
)
from app.public_origin import (
    PublicOriginError,
    canonical_public_origin,
    canonical_public_url_for_key,
    require_canonical_public_url,
)

CacheOperation = Literal["prefetch", "refresh"]


class CdnCacheError(RuntimeError):
    pass


@dataclass(frozen=True)
class CdnSubmission:
    task_id: str
    request_id: str


@dataclass(frozen=True)
class CdnTaskResult:
    state: Literal["pending", "succeeded", "failed"]
    error_message: str = ""


class CdnProvider(Protocol):
    def submit(
        self,
        operation: CacheOperation,
        urls: list[str],
    ) -> CdnSubmission: ...

    def status(self, task_id: str) -> CdnTaskResult: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate_cdn_config(settings: Settings) -> None:
    if not settings.cdn_cache_enabled:
        return
    host = (urlsplit(canonical_public_origin(settings)).hostname or "").lower()
    if not settings.cdn_domain.strip() or settings.cdn_domain.strip().lower() != host:
        raise CdnCacheError("CDN_DOMAIN must exactly match ALIYUN_OSS_PUBLIC_BASE_URL")
    if not (1 <= int(settings.cdn_worker_batch_size) <= 100):
        raise CdnCacheError("CDN_WORKER_BATCH_SIZE must be between 1 and 100")
    if int(settings.cdn_worker_max_attempts) < 1:
        raise CdnCacheError("CDN_WORKER_MAX_ATTEMPTS must be positive")
    if float(settings.cdn_provider_poll_seconds) <= 0:
        raise CdnCacheError("CDN_PROVIDER_POLL_SECONDS must be positive")
    if bool(settings.aliyun_cdn_access_key_id) != bool(
        settings.aliyun_cdn_access_key_secret
    ):
        raise CdnCacheError(
            "ALIYUN_CDN_ACCESS_KEY_ID and ALIYUN_CDN_ACCESS_KEY_SECRET must be set together"
        )


def _job_id(operation: CacheOperation, url_hash: str) -> str:
    return f"cdn_{operation}_{url_hash[:40]}"


def enqueue_cache_urls(
    db: Session,
    settings: Settings,
    *,
    operation: CacheOperation,
    urls: Iterable[str],
    force: bool = False,
) -> list[CdnCacheJob]:
    """Insert idempotent outbox rows in the caller's existing transaction."""
    if not settings.cdn_cache_enabled:
        return []
    if operation == "prefetch" and not settings.cdn_prefetch_on_publish:
        return []
    if operation not in {"prefetch", "refresh"}:
        raise CdnCacheError("unsupported CDN cache operation")
    validate_cdn_config(settings)
    normalized = sorted(
        {
            require_canonical_public_url(settings, str(raw).strip())
            for raw in urls
            if str(raw).strip()
        }
    )
    now = _now()
    jobs: list[CdnCacheJob] = []
    for url in normalized:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        job = db.get(CdnCacheJob, _job_id(operation, digest))
        if job is None:
            job = CdnCacheJob(
                id=_job_id(operation, digest),
                operation=operation,
                url_hash=digest,
                url=url,
                state="pending",
                attempts=0,
                next_attempt_at=now,
                lease_expires_at=None,
                provider_task_id="",
                request_id="",
                error_message="",
                created_at=now,
                updated_at=now,
            )
            db.add(job)
        elif force:
            job.state = "pending"
            job.attempts = 0
            job.next_attempt_at = now
            job.lease_expires_at = None
            job.provider_task_id = ""
            job.request_id = ""
            job.error_message = ""
            job.updated_at = now
            db.add(job)
        jobs.append(job)
    return jobs


def enqueue_prefetch(
    db: Session,
    settings: Settings,
    urls: Iterable[str],
) -> list[CdnCacheJob]:
    return enqueue_cache_urls(
        db,
        settings,
        operation="prefetch",
        urls=urls,
    )


def enqueue_refresh(
    db: Session,
    settings: Settings,
    urls: Iterable[str],
) -> list[CdnCacheJob]:
    return enqueue_cache_urls(
        db,
        settings,
        operation="refresh",
        urls=urls,
        force=True,
    )


def html_package_public_urls(
    db: Session,
    settings: Settings,
    *,
    package_id: str,
) -> list[str]:
    rows = (
        db.query(MediaObject)
        .join(HtmlPackageAsset, HtmlPackageAsset.media_object_id == MediaObject.id)
        .filter(
            HtmlPackageAsset.package_id == package_id,
            MediaObject.visibility == "public",
            MediaObject.state == "ready",
        )
        .all()
    )
    return sorted(
        {canonical_public_url_for_key(settings, row.object_key) for row in rows}
    )


def active_public_urls(db: Session, settings: Settings) -> list[str]:
    """Build a bounded prewarm manifest for active content and current avatars."""
    runtime = (
        db.query(MediaObject)
        .join(PublishedMediaAsset, PublishedMediaAsset.media_object_id == MediaObject.id)
        .join(
            PublishedVideo,
            and_(
                PublishedVideo.id == PublishedMediaAsset.video_id,
                PublishedVideo.active_publication_id
                == PublishedMediaAsset.publication_id,
            ),
        )
        .filter(
            PublishedVideo.is_deleted == 0,
            PublishedVideo.deleted_at.is_(None),
            PublishedVideo.review_status == "approved",
            PublishedVideo.distribution_enabled.is_(True),
            PublishedVideo.cdn_ready.is_(True),
            MediaObject.visibility == "public",
            MediaObject.state == "ready",
        )
        .all()
    )
    html = (
        db.query(MediaObject)
        .join(HtmlPackageAsset, HtmlPackageAsset.media_object_id == MediaObject.id)
        .join(PublishedVideo, PublishedVideo.html_package_id == HtmlPackageAsset.package_id)
        .filter(
            PublishedVideo.is_deleted == 0,
            PublishedVideo.deleted_at.is_(None),
            PublishedVideo.review_status == "approved",
            PublishedVideo.distribution_enabled.is_(True),
            PublishedVideo.cdn_ready.is_(True),
            MediaObject.visibility == "public",
            MediaObject.state == "ready",
        )
        .all()
    )
    avatars = (
        db.query(MediaObject)
        .join(User, User.avatar_media_object_id == MediaObject.id)
        .filter(
            User.enabled.is_(True),
            MediaObject.visibility == "public",
            MediaObject.state == "ready",
        )
        .all()
    )
    return sorted(
        {
            canonical_public_url_for_key(settings, row.object_key)
            for row in [*runtime, *html, *avatars]
        }
    )


class AlibabaCdnProvider:
    def __init__(self, settings: Settings):
        validate_cdn_config(settings)
        try:
            from alibabacloud_cdn20180510.client import Client as CdnClient
            from alibabacloud_credentials.client import Client as CredentialsClient
            from alibabacloud_credentials.models import Config as CredentialsConfig
            from alibabacloud_tea_openapi.models import Config as OpenApiConfig
        except ImportError as exc:  # pragma: no cover - deployment dependency failure
            raise CdnCacheError("Alibaba Cloud CDN SDK is not installed") from exc

        role_name = os.environ.get("ALIBABA_CLOUD_ECS_METADATA", "").strip()
        if settings.aliyun_cdn_access_key_id:
            credentials_config = CredentialsConfig(
                type="access_key",
                access_key_id=settings.aliyun_cdn_access_key_id,
                access_key_secret=settings.aliyun_cdn_access_key_secret,
            )
        else:
            credentials_config = CredentialsConfig(
                type="ecs_ram_role",
                role_name=role_name or None,
                disable_imds_v1=True,
            )
        credentials = CredentialsClient(credentials_config)
        self._client = CdnClient(
            OpenApiConfig(
                credential=credentials,
                endpoint="cdn.aliyuncs.com",
                region_id=settings.cdn_api_region.strip() or "cn-hangzhou",
                connect_timeout=10_000,
                read_timeout=30_000,
            )
        )

    def submit(
        self,
        operation: CacheOperation,
        urls: list[str],
    ) -> CdnSubmission:
        if not urls:
            raise CdnCacheError("cannot submit an empty CDN task")
        from alibabacloud_cdn20180510 import models

        object_path = "\n".join(urls)
        if operation == "prefetch":
            response = self._client.push_object_cache(
                models.PushObjectCacheRequest(object_path=object_path)
            )
            body = response.body
            return CdnSubmission(
                task_id=str(body.push_task_id or ""),
                request_id=str(body.request_id or ""),
            )
        if operation == "refresh":
            response = self._client.refresh_object_caches(
                models.RefreshObjectCachesRequest(
                    object_path=object_path,
                    object_type="File",
                )
            )
            body = response.body
            return CdnSubmission(
                task_id=str(body.refresh_task_id or ""),
                request_id=str(body.request_id or ""),
            )
        raise CdnCacheError("unsupported CDN cache operation")

    def status(self, task_id: str) -> CdnTaskResult:
        if not task_id.strip():
            raise CdnCacheError("CDN task id is empty")
        from alibabacloud_cdn20180510 import models

        response = self._client.describe_refresh_task_by_id(
            models.DescribeRefreshTaskByIdRequest(task_id=task_id)
        )
        tasks = list(response.body.tasks or [])
        # The task can be briefly absent immediately after PushObjectCache
        # returns. Treat that provider consistency window as still pending.
        if not tasks:
            return CdnTaskResult(state="pending")
        statuses = {str(item.status or "").strip().lower() for item in tasks}
        terminal_failures = {"failed", "timeout", "canceled"}
        failed_statuses = statuses & terminal_failures
        if failed_statuses:
            details = [
                str(item.description or "").strip()
                for item in tasks
                if str(item.status or "").strip().lower() in terminal_failures
            ]
            return CdnTaskResult(
                state="failed",
                error_message=next(
                    (item for item in details if item),
                    f"CDN task ended with {','.join(sorted(failed_statuses))}",
                ),
            )
        if statuses == {"complete"}:
            return CdnTaskResult(state="succeeded")
        return CdnTaskResult(state="pending")


def _claim_jobs(db: Session, settings: Settings) -> list[str]:
    now = _now()
    lease_until = now + timedelta(seconds=max(30, settings.cdn_worker_lease_seconds))
    rows = (
        db.query(CdnCacheJob)
        .filter(
            or_(
                CdnCacheJob.provider_task_id != "",
                CdnCacheJob.attempts < settings.cdn_worker_max_attempts,
            ),
            or_(
                and_(
                    CdnCacheJob.state == "pending",
                    CdnCacheJob.next_attempt_at <= now,
                ),
                and_(
                    CdnCacheJob.state == "running",
                    CdnCacheJob.lease_expires_at.is_not(None),
                    CdnCacheJob.lease_expires_at <= now,
                ),
            ),
        )
        .order_by(CdnCacheJob.created_at.asc(), CdnCacheJob.id.asc())
        .with_for_update(skip_locked=True)
        .limit(max(1, min(100, settings.cdn_worker_batch_size)))
        .all()
    )
    for row in rows:
        row.state = "running"
        # Attempts count provider submissions, not harmless status polls.
        if not row.provider_task_id:
            row.attempts += 1
        row.lease_expires_at = lease_until
        row.updated_at = now
        db.add(row)
    db.commit()
    return [row.id for row in rows]


def _record_submission(
    db: Session,
    settings: Settings,
    job_ids: list[str],
    *,
    submission: CdnSubmission | None,
    error: Exception | None,
) -> None:
    now = _now()
    for job_id in job_ids:
        row = db.get(CdnCacheJob, job_id)
        if row is None:
            continue
        row.lease_expires_at = None
        if error is None and submission is not None:
            if not submission.task_id.strip():
                error = CdnCacheError("CDN provider returned an empty task id")
            else:
                row.state = "pending"
                row.provider_task_id = submission.task_id[:255]
                row.request_id = submission.request_id[:128]
                row.error_message = ""
                row.next_attempt_at = now + timedelta(
                    seconds=max(1.0, float(settings.cdn_provider_poll_seconds))
                )
        if error is not None or submission is None:
            message = str(error or "unknown CDN provider failure")[:500]
            row.error_message = message
            row.provider_task_id = ""
            if row.attempts >= settings.cdn_worker_max_attempts:
                row.state = "failed"
            else:
                row.state = "pending"
                delay = min(3600, 5 * (2 ** max(0, row.attempts - 1)))
                row.next_attempt_at = now + timedelta(seconds=delay)
        row.updated_at = now
        db.add(row)
    db.commit()


def _record_task_status(
    db: Session,
    settings: Settings,
    job_ids: list[str],
    *,
    result: CdnTaskResult | None,
    error: Exception | None,
) -> None:
    now = _now()
    for job_id in job_ids:
        row = db.get(CdnCacheJob, job_id)
        if row is None:
            continue
        row.lease_expires_at = None
        if error is not None or result is None:
            # A DescribeRefreshTaskById transport failure does not mean that
            # the already accepted prefetch failed. Keep polling the same task.
            row.state = "pending"
            row.error_message = str(error or "CDN task status unavailable")[:500]
            row.next_attempt_at = now + timedelta(
                seconds=max(5.0, float(settings.cdn_provider_poll_seconds))
            )
        elif result.state == "succeeded":
            row.state = "succeeded"
            row.error_message = ""
        elif result.state == "failed":
            row.error_message = (result.error_message or "CDN prefetch failed")[:500]
            row.provider_task_id = ""
            if row.attempts >= settings.cdn_worker_max_attempts:
                row.state = "failed"
            else:
                row.state = "pending"
                delay = min(3600, 5 * (2 ** max(0, row.attempts - 1)))
                row.next_attempt_at = now + timedelta(seconds=delay)
        else:
            row.state = "pending"
            row.error_message = ""
            row.next_attempt_at = now + timedelta(
                seconds=max(1.0, float(settings.cdn_provider_poll_seconds))
            )
        row.updated_at = now
        db.add(row)
    db.commit()


def process_once(
    db: Session,
    settings: Settings,
    *,
    provider: CdnProvider | None = None,
) -> int:
    if not settings.cdn_cache_enabled:
        return 0
    job_ids = _claim_jobs(db, settings)
    if not job_ids:
        from app.cdn_publication import activate_ready_publications

        activated = activate_ready_publications(db)
        if activated:
            db.commit()
        return activated
    rows = [row for job_id in job_ids if (row := db.get(CdnCacheJob, job_id))]
    client = provider or AlibabaCdnProvider(settings)
    submitted = [row for row in rows if row.provider_task_id]
    unsubmitted = [row for row in rows if not row.provider_task_id]
    by_task: dict[str, list[CdnCacheJob]] = {}
    for row in submitted:
        by_task.setdefault(row.provider_task_id, []).append(row)
    for task_id, group in by_task.items():
        ids = [row.id for row in group]
        try:
            result = client.status(task_id)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            _record_task_status(db, settings, ids, result=None, error=exc)
        else:
            _record_task_status(db, settings, ids, result=result, error=None)

    for operation in ("prefetch", "refresh"):
        group = [row for row in unsubmitted if row.operation == operation]
        if not group:
            continue
        ids = [row.id for row in group]
        try:
            submission = client.submit(operation, [row.url for row in group])
        # Provider SDKs expose several transport/server exception hierarchies.
        # The durable outbox is the boundary that retries all of them safely.
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            _record_submission(db, settings, ids, submission=None, error=exc)
        else:
            _record_submission(db, settings, ids, submission=submission, error=None)

    from app.cdn_publication import activate_ready_publications

    activated = activate_ready_publications(db)
    if activated:
        db.commit()
    return len(rows) + activated


def _status(db: Session) -> dict[str, int]:
    result = {state: 0 for state in ("pending", "running", "succeeded", "failed")}
    for state, count in (
        db.query(CdnCacheJob.state, func.count(CdnCacheJob.id))
        .group_by(CdnCacheJob.state)
        .order_by(CdnCacheJob.state.asc())
        .all()
    ):
        result[state] = int(count)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage ivapp CDN cache jobs")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prewarm = subparsers.add_parser("prewarm", help="enqueue active public objects")
    prewarm.add_argument("--apply", action="store_true")
    refresh = subparsers.add_parser("refresh", help="enqueue an exact-file emergency refresh")
    refresh.add_argument("urls", nargs="+")
    refresh.add_argument("--apply", action="store_true")
    subparsers.add_parser("drain-once", help="submit one worker batch")
    subparsers.add_parser("status", help="show durable queue counts")
    args = parser.parse_args()
    settings = get_settings()
    with SessionLocal() as db:
        if args.command == "prewarm":
            urls = active_public_urls(db, settings)
            if args.apply:
                jobs = enqueue_prefetch(db, settings, urls)
                db.commit()
                output = {"discovered": len(urls), "enqueued": len(jobs)}
            else:
                output = {"discovered": len(urls), "enqueued": 0, "dry_run": True}
        elif args.command == "refresh":
            urls = [require_canonical_public_url(settings, item) for item in args.urls]
            if args.apply:
                jobs = enqueue_refresh(db, settings, urls)
                db.commit()
                output = {"validated": len(urls), "enqueued": len(jobs)}
            else:
                output = {"validated": len(urls), "enqueued": 0, "dry_run": True}
        elif args.command == "drain-once":
            output = {"processed": process_once(db, settings)}
        else:
            output = _status(db)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    try:
        raise SystemExit(main())
    except (CdnCacheError, PublicOriginError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from exc
