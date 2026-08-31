from __future__ import annotations

import argparse
import copy
import json
from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.html_content import CONTENT_TYPE_RUNTIME
from app.media_service import MediaServiceError
from app.models import CreatorCreation, CreatorVersion, PublishedVideo
from app.oss_storage import OssStorageError
from app.protocol_video import (
    RUNTIME_SPEC_VERSION,
    SUPPORTED_RUNTIME_SPEC_VERSIONS,
    RuntimeSpecError,
    compile_runtime_spec,
    runtime_spec_version_from_compiled,
)
from app.public_origin import canonicalize_public_url
from app.public_text import record_entity_text
from app.publication_service import load_published_runtime_urls


@dataclass(frozen=True)
class BackfillFailure:
    video_id: str
    reason: str
    entity_type: str = "published_video"


@dataclass(frozen=True)
class BackfillReport:
    total: int
    compilable: int
    updated: int
    failures: list[BackfillFailure]


def compile_all_runtime_specs(db: Session, *, apply: bool) -> BackfillReport:
    settings = get_settings()
    rows = (
        db.query(PublishedVideo)
        .filter(PublishedVideo.content_type == CONTENT_TYPE_RUNTIME)
        .order_by(PublishedVideo.id.asc())
        .all()
    )
    compiled: list[tuple[PublishedVideo | CreatorVersion | CreatorCreation, dict]] = []
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
                    settings,
                    video_id=row.id,
                    publication_id=row.active_publication_id,
                )
            spec = compile_runtime_spec(
                item_id=row.id,
                content_mode=row.content_mode or "single",
                source=source,
                video_url=canonicalize_public_url(settings, row.video_url) or row.video_url,
                video_urls=story_urls,
            )
        except (MediaServiceError, OssStorageError, RuntimeSpecError) as exc:
            failures.append(BackfillFailure(video_id=row.id, reason=str(exc)))
            continue
        compiled.append((row, spec))

    creations = {
        row.id: row
        for row in db.query(CreatorCreation).order_by(CreatorCreation.id.asc()).all()
    }
    ready_versions = (
        db.query(CreatorVersion)
        .filter(CreatorVersion.status == "ready")
        .order_by(CreatorVersion.id.asc())
        .all()
    )
    compiled_versions: dict[str, dict] = {}
    for version in ready_versions:
        creation = creations.get(version.creation_id)
        if creation is None:
            failures.append(
                BackfillFailure(
                    video_id=version.id,
                    entity_type="creator_version",
                    reason="creator creation is missing",
                )
            )
            continue
        if not creation.upload_id:
            failures.append(
                BackfillFailure(
                    video_id=version.id,
                    entity_type="creator_version",
                    reason="creator preview upload_id is missing",
                )
            )
            continue
        if not isinstance(version.source_timeline, dict):
            failures.append(
                BackfillFailure(
                    video_id=version.id,
                    entity_type="creator_version",
                    reason="creator source_timeline is missing",
                )
            )
            continue
        try:
            spec = compile_runtime_spec(
                item_id=f"{creation.id}-v{version.number}",
                content_mode="single",
                source=version.source_timeline,
                video_url=f"/api/v1/creator/previews/{creation.upload_id}",
            )
        except RuntimeSpecError as exc:
            failures.append(
                BackfillFailure(
                    video_id=version.id,
                    entity_type="creator_version",
                    reason=str(exc),
                )
            )
            continue
        compiled.append((version, spec))
        compiled_versions[version.id] = spec

    for creation in creations.values():
        if not creation.active_version_id:
            continue
        spec = compiled_versions.get(creation.active_version_id)
        if spec is not None:
            compiled.append((creation, copy.deepcopy(spec)))

    updated = 0
    if apply:
        for row, spec in compiled:
            row.runtime_spec = spec
            row.runtime_spec_version = runtime_spec_version_from_compiled(spec)
            record_entity_text(db, row)
            updated += 1
        db.commit()
    else:
        db.rollback()
    return BackfillReport(
        total=len(compiled) + len(failures),
        compilable=len(compiled),
        updated=updated,
        failures=failures,
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
                "latest_runtime_spec_version": RUNTIME_SPEC_VERSION,
                "supported_runtime_spec_versions": sorted(
                    SUPPORTED_RUNTIME_SPEC_VERSIONS
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
