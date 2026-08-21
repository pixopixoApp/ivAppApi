from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.html_content import CONTENT_TYPE_RUNTIME
from app.media_service import MediaServiceError
from app.models import PublishedVideo
from app.oss_storage import OssStorageError
from app.protocol_video import (
    RUNTIME_SPEC_VERSION,
    RuntimeSpecError,
    compile_runtime_spec,
)
from app.public_origin import canonicalize_public_url
from app.publication_service import load_published_runtime_urls


@dataclass(frozen=True)
class BackfillFailure:
    video_id: str
    reason: str


@dataclass(frozen=True)
class BackfillReport:
    total: int
    compilable: int
    updated: int
    failures: list[BackfillFailure]


def compile_all_runtime_specs(db: Session, *, apply: bool) -> BackfillReport:
    rows = (
        db.query(PublishedVideo)
        .filter(PublishedVideo.content_type == CONTENT_TYPE_RUNTIME)
        .order_by(PublishedVideo.id.asc())
        .all()
    )
    compiled: list[tuple[PublishedVideo, dict]] = []
    failures: list[BackfillFailure] = []
    for row in rows:
        if not row.video_url:
            failures.append(
                BackfillFailure(video_id=row.id, reason="runtime video_url is missing")
            )
            continue
        source = row.timeline if isinstance(row.timeline, dict) else {}
        try:
            story_urls = None
            if (row.content_mode or "single") == "story" and row.active_publication_id:
                story_urls = load_published_runtime_urls(
                    db,
                    get_settings(),
                    video_id=row.id,
                    publication_id=row.active_publication_id,
                )
            spec = compile_runtime_spec(
                item_id=row.id,
                content_mode=row.content_mode or "single",
                source=source,
                video_url=canonicalize_public_url(get_settings(), row.video_url) or row.video_url,
                video_urls=story_urls,
            )
        except (MediaServiceError, OssStorageError, RuntimeSpecError) as exc:
            failures.append(BackfillFailure(video_id=row.id, reason=str(exc)))
            continue
        compiled.append((row, spec))

    if failures:
        db.rollback()
        return BackfillReport(
            total=len(rows),
            compilable=len(compiled),
            updated=0,
            failures=failures,
        )

    updated = 0
    if apply:
        for row, spec in compiled:
            row.runtime_spec = spec
            row.runtime_spec_version = RUNTIME_SPEC_VERSION
            updated += 1
        db.commit()
    else:
        db.rollback()
    return BackfillReport(
        total=len(rows),
        compilable=len(compiled),
        updated=updated,
        failures=[],
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compile persisted App runtime specs for all historic works."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the result. Without this flag the command is a dry run.",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        report = compile_all_runtime_specs(db, apply=args.apply)
    print(
        json.dumps(
            {
                **asdict(report),
                "mode": "apply" if args.apply else "dry-run",
                "runtime_spec_version": RUNTIME_SPEC_VERSION,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if report.failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
