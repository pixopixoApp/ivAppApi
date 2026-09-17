"""Audit or repair CDN cache metadata on finalized OSS media objects."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from app.config import Settings, get_settings
from app.oss_storage import (
    OssObjectNotFoundError,
    head_object,
    immutable_cache_control,
    update_object_cache_control,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass(frozen=True)
class CacheMetadataFailure:
    media_id: str
    object_key: str
    reason: str


@dataclass
class CacheMetadataReport:
    mode: str
    scanned: int = 0
    compliant: int = 0
    changes: int = 0
    changed_ids: list[str] = field(default_factory=list)
    failures: list[CacheMetadataFailure] = field(default_factory=list)


def migrate_cache_metadata(
    db: Session,
    settings: Settings,
    *,
    apply: bool = False,
    verify: bool = False,
) -> CacheMetadataReport:
    """Apply origin-owned cache policy to immutable, finalized media only."""
    from app.models import MediaObject

    report = CacheMetadataReport(
        mode="verify" if verify else "apply" if apply else "dry-run"
    )
    rows = (
        db.query(MediaObject)
        .filter(MediaObject.state == "ready")
        .order_by(MediaObject.id.asc())
        .all()
    )
    targets = [
        (row.id, row.object_key, row.visibility == "public", row.etag or "")
        for row in rows
    ]

    def inspect_target(
        target: tuple[str, str, bool, str],
    ) -> tuple[str, CacheMetadataFailure | None]:
        media_id, object_key, public, etag = target
        expected = immutable_cache_control(
            public=public,
            private_max_age_seconds=settings.private_media_cdn_ttl_seconds,
        )
        try:
            metadata = head_object(settings, key=object_key)
            actual = metadata.headers.get("cache-control", "").strip()
            if actual == expected:
                return "compliant", None
            if not apply:
                return "change", None
            update_object_cache_control(
                settings,
                key=object_key,
                cache_control=expected,
                public=public,
                expected_etag=etag or metadata.etag,
            )
            verified = head_object(settings, key=object_key)
            if verified.headers.get("cache-control", "").strip() != expected:
                raise RuntimeError("OSS did not retain the requested Cache-Control")
            return "changed", None
        except OssObjectNotFoundError:
            return "failure", CacheMetadataFailure(
                media_id,
                object_key,
                "ready object is missing",
            )
        except Exception as exc:  # noqa: BLE001 - every object must be reported
            return "failure", CacheMetadataFailure(media_id, object_key, str(exc))

    def record(
        target: tuple[str, str, bool, str],
        result: tuple[str, CacheMetadataFailure | None],
    ) -> None:
        media_id, _object_key, _public, _etag = target
        report.scanned += 1
        status, failure = result
        if status in {"compliant", "changed"}:
            report.compliant += 1
        if status in {"change", "changed"}:
            report.changes += 1
            report.changed_ids.append(media_id)
        if failure is not None:
            report.failures.append(failure)

    if targets:
        # Initialize the shared OSS concurrency limiter before worker threads
        # race to create their thread-local clients.
        first = targets[0]
        record(first, inspect_target(first))
        worker_count = max(1, min(16, int(settings.oss_max_concurrency)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            for target, result in zip(
                targets[1:],
                executor.map(inspect_target, targets[1:]),
                strict=True,
            ):
                record(target, result)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit or repair Cache-Control metadata for finalized OSS media. "
            "Dry-run is the default."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    from app.db import SessionLocal

    with SessionLocal() as db:
        report = migrate_cache_metadata(
            db,
            get_settings(),
            apply=args.apply,
            verify=args.verify,
        )
    payload: dict[str, Any] = asdict(report)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if report.failures:
        return 1
    if args.verify and report.changes:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
