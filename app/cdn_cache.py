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


class CdnProvider(Protocol):
    def submit(
        self,
        operation: CacheOperation,
        urls: list[str],
    ) -> CdnSubmission: ...


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
        credentials = CredentialsClient(
            CredentialsConfig(
                type="ecs_ram_role",
                role_name=role_name or None,
                disable_imds_v1=True,
            )
        )
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


def _claim_jobs(db: Session, settings: Settings) -> list[str]:
    now = _now()
    lease_until = now + timedelta(seconds=max(30, settings.cdn_worker_lease_seconds))
    rows = (
        db.query(CdnCacheJob)
        .filter(
            CdnCacheJob.attempts < settings.cdn_worker_max_attempts,
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
        row.attempts += 1
        row.lease_expires_at = lease_until
        row.updated_at = now
        db.add(row)
    db.commit()
    return [row.id for row in rows]


def _finish_group(
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
            row.state = "succeeded"
            row.provider_task_id = submission.task_id[:255]
            row.request_id = submission.request_id[:128]
            row.error_message = ""
        else:
            message = str(error or "unknown CDN provider failure")[:500]
            row.error_message = message
            if row.attempts >= settings.cdn_worker_max_attempts:
                row.state = "failed"
            else:
                row.state = "pending"
                delay = min(3600, 5 * (2 ** max(0, row.attempts - 1)))
                row.next_attempt_at = now + timedelta(seconds=delay)
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
        return 0
    rows = [row for job_id in job_ids if (row := db.get(CdnCacheJob, job_id))]
    client = provider or AlibabaCdnProvider(settings)
    for operation in ("prefetch", "refresh"):
        group = [row for row in rows if row.operation == operation]
        if not group:
            continue
        ids = [row.id for row in group]
        try:
            submission = client.submit(operation, [row.url for row in group])
        # Provider SDKs expose several transport/server exception hierarchies.
        # The durable outbox is the boundary that retries all of them safely.
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            _finish_group(db, settings, ids, submission=None, error=exc)
        else:
            _finish_group(db, settings, ids, submission=submission, error=None)
    return len(rows)


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
