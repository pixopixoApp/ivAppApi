from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import (
    CdnCacheJob,
    HtmlPackageAsset,
    MediaObject,
    PublishedVideo,
)
from app.private_cdn import (
    private_media_cache_url,
    sign_private_media_url,
)
from app.public_origin import (
    PublicOriginError,
    canonical_public_origin,
    canonical_public_url_for_key,
    canonicalize_public_url,
    require_canonical_public_url,
)

CacheOperation = Literal["prefetch", "refresh"]
_ON_DEMAND_REQUEST_ID = "fallback:on-demand"


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
    daily_budget = int(getattr(settings, "cdn_prefetch_daily_budget", 400))
    priority_reserve = int(getattr(settings, "cdn_prefetch_priority_reserve", 50))
    background_maximum = int(
        getattr(settings, "cdn_background_prewarm_max_urls", 100)
    )
    if not 1 <= daily_budget <= 500:
        raise CdnCacheError("CDN_PREFETCH_DAILY_BUDGET must be between 1 and 500")
    if not 0 <= priority_reserve <= 100:
        raise CdnCacheError("CDN_PREFETCH_PRIORITY_RESERVE must be between 0 and 100")
    if daily_budget + priority_reserve > 500:
        raise CdnCacheError("CDN prefetch budget plus reserve must not exceed 500")
    if not 1 <= background_maximum <= 400:
        raise CdnCacheError(
            "CDN_BACKGROUND_PREWARM_MAX_URLS must be between 1 and 400"
        )
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


def _enqueue_normalized_urls(
    db: Session,
    *,
    operation: CacheOperation,
    normalized: Iterable[str],
    force: bool,
    retry_failed: bool,
) -> list[CdnCacheJob]:
    return _enqueue_targets(
        db,
        operation=operation,
        targets=((url, url) for url in normalized),
        force=force,
        retry_failed=retry_failed,
    )


def _enqueue_targets(
    db: Session,
    *,
    operation: CacheOperation,
    targets: Iterable[tuple[str, str]],
    force: bool,
    retry_failed: bool,
) -> list[CdnCacheJob]:
    now = _now()
    jobs: list[CdnCacheJob] = []
    for identity, submission_url in sorted(set(targets)):
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        job = db.get(CdnCacheJob, _job_id(operation, digest))
        if job is None:
            job = CdnCacheJob(
                id=_job_id(operation, digest),
                operation=operation,
                url_hash=digest,
                url=submission_url,
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
        elif force or (retry_failed and job.state == "failed"):
            job.url = submission_url
            job.state = "pending"
            job.attempts = 0
            job.next_attempt_at = now
            job.lease_expires_at = None
            job.provider_task_id = ""
            job.request_id = ""
            job.error_message = ""
            job.updated_at = now
            db.add(job)
        elif job.state == "pending" and not job.provider_task_id:
            # Refresh an expiring private-media signature before submission,
            # while retaining the stable object URL as the idempotency key.
            job.url = submission_url
            job.updated_at = now
            db.add(job)
        jobs.append(job)
    return jobs


def enqueue_cache_urls(
    db: Session,
    settings: Settings,
    *,
    operation: CacheOperation,
    urls: Iterable[str],
    force: bool = False,
    retry_failed: bool = False,
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
    return _enqueue_normalized_urls(
        db,
        operation=operation,
        normalized=normalized,
        force=force,
        retry_failed=retry_failed,
    )


def enqueue_prefetch(
    db: Session,
    settings: Settings,
    urls: Iterable[str],
    *,
    retry_failed: bool = False,
    force: bool = False,
) -> list[CdnCacheJob]:
    return enqueue_cache_urls(
        db,
        settings,
        operation="prefetch",
        urls=urls,
        retry_failed=retry_failed,
        force=force,
    )


def enqueue_private_media_prefetch(
    db: Session,
    settings: Settings,
    object_keys: Iterable[str],
    *,
    retry_failed: bool = False,
    force: bool = False,
) -> list[CdnCacheJob]:
    """Queue private creator media with a stable identity and short-lived read URL."""
    if not settings.cdn_cache_enabled or not settings.creator_media_cdn_prefetch_enabled:
        return []
    validate_cdn_config(settings)
    targets = set()
    for key in sorted({str(raw_key).strip() for raw_key in object_keys}):
        cache_url = private_media_cache_url(settings, key=key)
        if not cache_url:
            continue
        targets.add(
            (
                cache_url,
                sign_private_media_url(
                    settings,
                    key=key,
                    expires_seconds=max(900, settings.private_media_cdn_ttl_seconds),
                ),
            )
        )
    return _enqueue_targets(
        db,
        operation="prefetch",
        targets=targets,
        force=force,
        retry_failed=retry_failed,
    )


def _private_url_is_fresh(url: str, settings: Settings) -> bool:
    query = parse_qs(urlsplit(url).query)
    current_time = int(time.time())
    try:
        if query.get("Expires"):
            return int(query["Expires"][0]) > current_time + 30
        if query.get("auth_key"):
            issued_at = int(query["auth_key"][0].split("-", 1)[0])
            return issued_at + settings.private_media_cdn_ttl_seconds > current_time + 30
    except (ValueError, IndexError):
        return False
    return False


def private_media_delivery_url(db: Session, settings: Settings, *, key: str) -> str:
    """Reuse the exact signed URL warmed by the private-media outbox."""
    if not settings.cdn_cache_enabled or not settings.creator_media_cdn_prefetch_enabled:
        return sign_private_media_url(
            settings,
            key=key,
            expires_seconds=settings.private_media_cdn_ttl_seconds,
        )
    jobs = enqueue_private_media_prefetch(db, settings, [key])
    if not jobs:
        return sign_private_media_url(
            settings,
            key=key,
            expires_seconds=settings.private_media_cdn_ttl_seconds,
        )
    job = jobs[0]
    if job.state == "failed" or not _private_url_is_fresh(job.url, settings):
        job = enqueue_private_media_prefetch(db, settings, [key], force=True)[0]
    db.commit()
    return job.url


def _warm_private_media_url(url: str, settings: Settings) -> None:
    """Populate the exact authenticated CDN cache entry used by the app."""
    maximum = int(settings.creator_video_max_bytes)
    total = 0
    try:
        with httpx.stream(
            "GET",
            url,
            follow_redirects=False,
            timeout=httpx.Timeout(connect=15.0, read=90.0, write=30.0, pool=15.0),
        ) as response:
            if response.status_code != 200:
                raise CdnCacheError(
                    f"private media warm request failed ({response.status_code})"
                )
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > maximum:
                    raise CdnCacheError("private media warm response exceeded the size limit")
    except httpx.HTTPError as exc:
        raise CdnCacheError("private media warm request was unavailable") from exc
    if total <= 0:
        raise CdnCacheError("private media warm response was empty")


def _is_private_prefetch(row: CdnCacheJob, settings: Settings) -> bool:
    path = urlsplit(row.url).path.lstrip("/")
    prefix = f"{settings.oss_root_prefix.strip('/')}/private/"
    return row.operation == "prefetch" and path.startswith(prefix)


def wait_for_cache_jobs(
    db: Session,
    job_ids: Iterable[str],
    *,
    timeout_seconds: float,
    poll_seconds: float = 5.0,
) -> dict[str, str]:
    """Wait for existing CDN jobs without processing them in this process."""
    ids = sorted(set(job_ids))
    if not ids:
        raise CdnCacheError("no CDN cache jobs were enqueued")
    if timeout_seconds <= 0:
        raise CdnCacheError("CDN wait timeout must be positive")
    if poll_seconds <= 0:
        raise CdnCacheError("CDN wait poll interval must be positive")

    deadline = time.monotonic() + timeout_seconds
    while True:
        # MySQL defaults to REPEATABLE READ. End the previous read-only
        # transaction so each poll observes commits made by the worker.
        db.rollback()
        db.expire_all()
        rows = [db.get(CdnCacheJob, job_id) for job_id in ids]
        if any(row is None for row in rows):
            raise CdnCacheError("a CDN cache job disappeared while waiting")
        states = {row.id: row.state for row in rows if row is not None}
        failures = [
            row for row in rows if row is not None and row.state == "failed"
        ]
        if failures:
            detail = next(
                (row.error_message for row in failures if row.error_message),
                "CDN prefetch failed",
            )
            raise CdnCacheError(detail)
        if states and set(states.values()) == {"succeeded"}:
            return states
        if time.monotonic() >= deadline:
            pending = ", ".join(
                f"{job_id}:{state}" for job_id, state in sorted(states.items())
            )
            raise CdnCacheError(f"timed out waiting for CDN jobs: {pending}")
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))


def wait_for_cache_jobs_submitted(
    db: Session,
    job_ids: Iterable[str],
    *,
    timeout_seconds: float,
    poll_seconds: float = 1.0,
) -> dict[str, str]:
    """Wait only until Alibaba accepts each job and returns a provider task ID."""
    ids = sorted(set(job_ids))
    if not ids:
        raise CdnCacheError("no CDN cache jobs were enqueued")
    if timeout_seconds <= 0:
        raise CdnCacheError("CDN submission timeout must be positive")
    if poll_seconds <= 0:
        raise CdnCacheError("CDN submission poll interval must be positive")

    deadline = time.monotonic() + timeout_seconds
    while True:
        db.rollback()
        db.expire_all()
        rows = [db.get(CdnCacheJob, job_id) for job_id in ids]
        if any(row is None for row in rows):
            raise CdnCacheError("a CDN cache job disappeared before submission")
        failures = [row for row in rows if row is not None and row.state == "failed"]
        if failures:
            detail = next(
                (row.error_message for row in failures if row.error_message),
                "CDN prefetch submission failed",
            )
            raise CdnCacheError(detail)
        provider_ids = {
            row.id: (
                row.provider_task_id
                or (
                    _ON_DEMAND_REQUEST_ID
                    if row.state == "succeeded"
                    and row.request_id == _ON_DEMAND_REQUEST_ID
                    else ""
                )
            )
            for row in rows
            if row is not None
            and (
                row.provider_task_id
                or (
                    row.state == "succeeded"
                    and row.request_id == _ON_DEMAND_REQUEST_ID
                )
            )
        }
        if len(provider_ids) == len(ids):
            return provider_ids
        if time.monotonic() >= deadline:
            missing = ", ".join(job_id for job_id in ids if job_id not in provider_ids)
            raise CdnCacheError(
                f"timed out waiting for CDN task submission: {missing}"
            )
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))


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
    """Return a bounded first-view manifest, not every asset in storage.

    Covers and entrypoints receive the greatest user-visible benefit from
    proactive warming. Story branches and HTML subresources remain immutable
    CDN URLs and fill on demand if a viewer actually needs them.
    """
    maximum = max(
        1,
        min(400, int(getattr(settings, "cdn_background_prewarm_max_urls", 100))),
    )
    videos = (
        db.query(PublishedVideo)
        .filter(
            PublishedVideo.is_deleted == 0,
            PublishedVideo.deleted_at.is_(None),
            PublishedVideo.review_status == "approved",
            PublishedVideo.distribution_enabled.is_(True),
            PublishedVideo.cdn_ready.is_(True),
        )
        .order_by(
            PublishedVideo.is_tutorial.desc(),
            PublishedVideo.feed_weight.desc(),
            PublishedVideo.updated_at.desc(),
            PublishedVideo.id.asc(),
        )
        .limit(maximum)
        .all()
    )
    cover_ids = {
        row.cover_media_object_id for row in videos if row.cover_media_object_id
    }
    covers = {
        row.id: row
        for row in (
            db.query(MediaObject).filter(MediaObject.id.in_(cover_ids)).all()
            if cover_ids
            else []
        )
        if row.visibility == "public" and row.state == "ready"
    }
    result: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        if not raw or len(result) >= maximum:
            return
        try:
            normalized = require_canonical_public_url(
                settings,
                canonicalize_public_url(settings, raw),
            )
        except PublicOriginError:
            return
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)

    for row in videos:
        cover = covers.get(row.cover_media_object_id or "")
        if cover is not None:
            add(canonical_public_url_for_key(settings, cover.object_key))
        add(row.html_url if row.content_type == "html" else row.video_url)
        if len(result) >= maximum:
            break
    return result


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


def _record_on_demand_prefetch_fallback(
    db: Session,
    job_ids: list[str],
) -> None:
    """Complete prefetch jobs when Alibaba's optional daily warm quota is full.

    The canonical URL still uses the CDN and fills the same immutable object on
    its first viewer request. A provider preload quota must therefore not block
    publication or turn a valid CDN delivery URL into a terminal failure.
    """
    now = _now()
    for job_id in job_ids:
        row = db.get(CdnCacheJob, job_id)
        if row is None:
            continue
        row.state = "succeeded"
        row.lease_expires_at = None
        row.provider_task_id = ""
        row.request_id = _ON_DEMAND_REQUEST_ID
        row.error_message = ""
        row.updated_at = now
        db.add(row)
    db.commit()


def _is_preload_quota_exceeded(error: Exception) -> bool:
    return "QuotaExceeded.Preload" in str(error)


def _daily_prefetch_usage(db: Session, settings: Settings) -> tuple[int, int]:
    now = _now()
    # Alibaba's daily quota rolls over on the provider's UTC+8 calendar day.
    # Convert the boundary back to UTC for the timestamp comparison.
    day_start = (
        now.astimezone(ZoneInfo("Asia/Shanghai"))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )
    rows = (
        db.query(CdnCacheJob)
        .filter(
            CdnCacheJob.operation == "prefetch",
            CdnCacheJob.request_id != "",
            CdnCacheJob.request_id != _ON_DEMAND_REQUEST_ID,
            CdnCacheJob.updated_at >= day_start,
        )
        .all()
    )
    priority = sum(_is_priority_prefetch(row, settings) for row in rows)
    return len(rows), len(rows) - priority


def _is_priority_prefetch(row: CdnCacheJob, settings: Settings) -> bool:
    path = urlsplit(row.url).path.lstrip("/")
    prefix = (
        f"{settings.oss_root_prefix.strip('/')}/public/"
        "app-releases/android/"
    )
    return path.startswith(prefix)


def _budget_prefetch_group(
    db: Session,
    settings: Settings,
    group: list[CdnCacheJob],
) -> tuple[list[CdnCacheJob], list[CdnCacheJob]]:
    """Reserve provider submissions for critical APKs and bounded routine work."""
    used_total, used_routine = _daily_prefetch_usage(db, settings)
    routine_limit = int(getattr(settings, "cdn_prefetch_daily_budget", 400))
    total_limit = routine_limit + int(
        getattr(settings, "cdn_prefetch_priority_reserve", 50)
    )
    priority = [row for row in group if _is_priority_prefetch(row, settings)]
    routine = [row for row in group if row not in priority]

    priority_count = min(len(priority), max(0, total_limit - used_total))
    selected = priority[:priority_count]
    used_after_priority = used_total + priority_count
    routine_count = min(
        len(routine),
        max(0, routine_limit - used_routine),
        max(0, total_limit - used_after_priority),
    )
    selected.extend(routine[:routine_count])
    selected_ids = {row.id for row in selected}
    fallback = [row for row in group if row.id not in selected_ids]
    return selected, fallback


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
    submitted = [row for row in rows if row.provider_task_id]
    unsubmitted = [row for row in rows if not row.provider_task_id]
    client = provider or (AlibabaCdnProvider(settings) if submitted else None)
    by_task: dict[str, list[CdnCacheJob]] = {}
    for row in submitted:
        by_task.setdefault(row.provider_task_id, []).append(row)
    for task_id, group in by_task.items():
        ids = [row.id for row in group]
        try:
            if client is None:  # pragma: no cover - guarded by submitted rows
                raise CdnCacheError("CDN provider is unavailable")
            result = client.status(task_id)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            _record_task_status(db, settings, ids, result=None, error=exc)
        else:
            _record_task_status(db, settings, ids, result=result, error=None)

    private = [row for row in unsubmitted if _is_private_prefetch(row, settings)]
    for row in private:
        try:
            _warm_private_media_url(row.url, settings)
        except Exception as exc:  # noqa: BLE001
            _record_task_status(
                db,
                settings,
                [row.id],
                result=CdnTaskResult(state="failed", error_message=str(exc)),
                error=None,
            )
        else:
            _record_task_status(
                db,
                settings,
                [row.id],
                result=CdnTaskResult(state="succeeded"),
                error=None,
            )

    public = [row for row in unsubmitted if row not in private]
    if public and client is None:
        client = AlibabaCdnProvider(settings)

    for operation in ("prefetch", "refresh"):
        group = [row for row in public if row.operation == operation]
        if not group:
            continue
        if operation == "prefetch":
            group, fallback = _budget_prefetch_group(db, settings, group)
            if fallback:
                _record_on_demand_prefetch_fallback(
                    db,
                    [row.id for row in fallback],
                )
            if not group:
                continue
        ids = [row.id for row in group]
        try:
            if client is None:  # pragma: no cover - guarded by non-empty group
                raise CdnCacheError("CDN provider is unavailable")
            submission = client.submit(operation, [row.url for row in group])
        # Provider SDKs expose several transport/server exception hierarchies.
        # The durable outbox is the boundary that retries all of them safely.
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            if operation == "prefetch" and _is_preload_quota_exceeded(exc):
                _record_on_demand_prefetch_fallback(db, ids)
            else:
                _record_submission(db, settings, ids, submission=None, error=exc)
        else:
            _record_submission(db, settings, ids, submission=submission, error=None)

    from app.cdn_publication import activate_ready_publications

    activated = activate_ready_publications(db)
    if activated:
        db.commit()
    return len(rows) + activated


def _status(db: Session, settings: Settings) -> dict[str, int]:
    result = {state: 0 for state in ("pending", "running", "succeeded", "failed")}
    for state, count in (
        db.query(CdnCacheJob.state, func.count(CdnCacheJob.id))
        .group_by(CdnCacheJob.state)
        .order_by(CdnCacheJob.state.asc())
        .all()
    ):
        result[state] = int(count)
    result["on_demand"] = int(
        db.query(func.count(CdnCacheJob.id))
        .filter(
            CdnCacheJob.state == "succeeded",
            CdnCacheJob.request_id == _ON_DEMAND_REQUEST_ID,
        )
        .scalar()
        or 0
    )
    total, routine = _daily_prefetch_usage(db, settings)
    routine_limit = int(getattr(settings, "cdn_prefetch_daily_budget", 400))
    total_limit = routine_limit + int(
        getattr(settings, "cdn_prefetch_priority_reserve", 50)
    )
    result.update(
        {
            "daily_submitted": total,
            "daily_routine_submitted": routine,
            "daily_apk_submitted": total - routine,
            "daily_routine_remaining": max(
                0,
                min(routine_limit - routine, total_limit - total),
            ),
            "daily_apk_capacity_remaining": max(0, total_limit - total),
        }
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage ivapp CDN cache jobs")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prewarm = subparsers.add_parser("prewarm", help="enqueue active public objects")
    prewarm.add_argument("--apply", action="store_true")
    prefetch = subparsers.add_parser(
        "prefetch",
        help="enqueue exact immutable public URLs",
    )
    prefetch.add_argument("urls", nargs="+")
    prefetch.add_argument("--apply", action="store_true")
    prefetch.add_argument("--wait", action="store_true")
    prefetch.add_argument(
        "--wait-submitted",
        action="store_true",
        help="wait only until Alibaba returns a provider task ID",
    )
    prefetch.add_argument("--retry-failed", action="store_true")
    prefetch.add_argument(
        "--resubmit",
        action="store_true",
        help="replace an incomplete provider task and submit the URL again",
    )
    prefetch.add_argument("--timeout", type=float, default=2100.0)
    prefetch.add_argument("--poll-seconds", type=float, default=5.0)
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
        elif args.command == "prefetch":
            if (args.wait or args.wait_submitted) and not args.apply:
                raise CdnCacheError("CDN wait options require --apply")
            if args.wait and args.wait_submitted:
                raise CdnCacheError("choose --wait or --wait-submitted, not both")
            if not settings.cdn_cache_enabled:
                raise CdnCacheError("CDN cache processing is disabled")
            if not settings.cdn_prefetch_on_publish:
                raise CdnCacheError("CDN prefetch-on-publish is disabled")
            urls = [require_canonical_public_url(settings, item) for item in args.urls]
            if args.apply:
                jobs = enqueue_prefetch(
                    db,
                    settings,
                    urls,
                    retry_failed=args.retry_failed,
                    force=args.resubmit,
                )
                db.commit()
                output: dict[str, object] = {
                    "validated": len(urls),
                    "enqueued": len(jobs),
                    "job_ids": [job.id for job in jobs],
                }
                if args.wait:
                    output["states"] = wait_for_cache_jobs(
                        db,
                        [job.id for job in jobs],
                        timeout_seconds=args.timeout,
                        poll_seconds=args.poll_seconds,
                    )
                elif args.wait_submitted:
                    output["provider_task_ids"] = wait_for_cache_jobs_submitted(
                        db,
                        [job.id for job in jobs],
                        timeout_seconds=args.timeout,
                        poll_seconds=args.poll_seconds,
                    )
            else:
                output = {"validated": len(urls), "enqueued": 0, "dry_run": True}
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
            output = _status(db, settings)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    try:
        raise SystemExit(main())
    except (CdnCacheError, PublicOriginError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from exc
