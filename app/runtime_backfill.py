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
    apply_default_circle_directions,
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


KNOWN_ROTATION_DIRECTIONS: dict[str, dict[int, str]] = {
    # These prompts contain an explicit counterclockwise arrow in their media.
    "5d71abc9-d3a2-4fe5-9843-22b729c3c384": {3620: "counterclockwise"},
    "87299ca4-ab06-4fb4-98b7-24b5b29b74a5": {3780: "counterclockwise"},
}


def _has_draw_circle(source: dict) -> bool:
    def timeline_has_circle(timeline: dict) -> bool:
        return any(
            isinstance(interaction, dict)
            and interaction.get("gesture") == "draw_circle"
            for interaction in timeline.get("interactions") or []
        )

    clips = source.get("clips")
    if isinstance(clips, dict):
        return any(
            isinstance(body, dict)
            and isinstance(body.get("timeline"), dict)
            and timeline_has_circle(body["timeline"])
            for body in clips.values()
        )
    return timeline_has_circle(source)


def backfill_known_rotation_directions(db: Session, *, apply: bool) -> BackfillReport:
    """Persist audited rotate directions in source timelines and compiled specs."""
    settings = get_settings()
    rows = {
        row.id: row
        for row in db.query(PublishedVideo)
        .filter(PublishedVideo.id.in_(KNOWN_ROTATION_DIRECTIONS))
        .order_by(PublishedVideo.id.asc())
        .all()
    }
    compiled: list[tuple[PublishedVideo, dict, dict]] = []
    failures: list[BackfillFailure] = []
    for video_id, directions_by_gate in KNOWN_ROTATION_DIRECTIONS.items():
        row = rows.get(video_id)
        if row is None:
            failures.append(BackfillFailure(video_id=video_id, reason="published video is missing"))
            continue
        if not row.video_url:
            failures.append(BackfillFailure(video_id=video_id, reason="runtime video_url is missing"))
            continue
        source = copy.deepcopy(row.timeline) if isinstance(row.timeline, dict) else {}
        interactions = source.get("interactions")
        if not isinstance(interactions, list):
            failures.append(BackfillFailure(video_id=video_id, reason="source interactions are missing"))
            continue
        invalid = False
        for gate_at_ms, direction in directions_by_gate.items():
            matches = [
                item
                for item in interactions
                if isinstance(item, dict)
                and item.get("gesture") == "rotate"
                and item.get("gate_at_ms") == gate_at_ms
            ]
            if len(matches) != 1:
                failures.append(
                    BackfillFailure(
                        video_id=video_id,
                        reason=f"expected one rotate interaction at {gate_at_ms}ms, found {len(matches)}",
                    )
                )
                invalid = True
                break
            matches[0]["rotation_direction"] = direction
        if invalid:
            continue
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
            failures.append(BackfillFailure(video_id=video_id, reason=str(exc)))
            continue
        compiled.append((row, source, spec))

    updated = 0
    if apply:
        for row, source, spec in compiled:
            row.timeline = source
            row.runtime_spec = spec
            row.runtime_spec_version = runtime_spec_version_from_compiled(spec)
            record_entity_text(db, row)
            updated += 1
        db.commit()
    else:
        db.rollback()
    return BackfillReport(
        total=len(KNOWN_ROTATION_DIRECTIONS),
        compilable=len(compiled),
        updated=updated,
        failures=failures,
    )


def compile_all_runtime_specs(
    db: Session,
    *,
    apply: bool,
    only_circle_directions: bool = False,
) -> BackfillReport:
    settings = get_settings()
    rows = (
        db.query(PublishedVideo)
        .filter(PublishedVideo.content_type == CONTENT_TYPE_RUNTIME)
        .order_by(PublishedVideo.id.asc())
        .all()
    )
    compiled: list[
        tuple[PublishedVideo | CreatorVersion | CreatorCreation, dict, dict]
    ] = []
    failures: list[BackfillFailure] = []
    for row in rows:
        if not row.video_url:
            failures.append(
                BackfillFailure(video_id=row.id, reason="runtime video_url is missing")
            )
            continue
        original_source = row.timeline if isinstance(row.timeline, dict) else {}
        if only_circle_directions and not _has_draw_circle(original_source):
            continue
        source = apply_default_circle_directions(original_source)
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
        compiled.append((row, spec, source))

    creations = {
        row.id: row
        for row in db.query(CreatorCreation).order_by(CreatorCreation.id.asc()).all()
    }
    ready_versions = (
        db.query(CreatorVersion)
        .filter(CreatorVersion.status.in_(("ready", "published")))
        .order_by(CreatorVersion.id.asc())
        .all()
    )
    compiled_versions: dict[str, tuple[dict, dict]] = {}
    for version in ready_versions:
        if not isinstance(version.source_timeline, dict):
            if only_circle_directions:
                continue
            failures.append(
                BackfillFailure(
                    video_id=version.id,
                    entity_type="creator_version",
                    reason="creator source_timeline is missing",
                )
            )
            continue
        if only_circle_directions and not _has_draw_circle(version.source_timeline):
            continue
        source = apply_default_circle_directions(version.source_timeline)
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
        is_story = isinstance(source.get("clips"), dict)
        if not is_story and not creation.upload_id:
            failures.append(
                BackfillFailure(
                    video_id=version.id,
                    entity_type="creator_version",
                    reason="creator preview upload_id is missing",
                )
            )
            continue
        story_urls = None
        video_url = f"/api/v1/creator/previews/{creation.upload_id}"
        if is_story:
            runtime_clips = (
                version.runtime_spec.get("video")
                if isinstance(version.runtime_spec, dict)
                else None
            )
            if not isinstance(runtime_clips, list):
                failures.append(
                    BackfillFailure(
                        video_id=version.id,
                        entity_type="creator_version",
                        reason="creator Story runtime media URLs are missing",
                    )
                )
                continue
            story_urls = {
                str(clip.get("video_id")): str(clip.get("video") or "")
                for clip in runtime_clips
                if isinstance(clip, dict) and clip.get("video_id")
            }
            entry_clip_id = str(source.get("entry_clip_id") or "")
            video_url = story_urls.get(entry_clip_id, "")
        try:
            spec = compile_runtime_spec(
                item_id=f"{creation.id}-v{version.number}",
                content_mode="story" if is_story else "single",
                source=source,
                video_url=video_url,
                video_urls=story_urls,
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
        compiled.append((version, spec, source))
        compiled_versions[version.id] = (spec, source)

    for creation in creations.values():
        if not creation.active_version_id:
            continue
        compiled_version = compiled_versions.get(creation.active_version_id)
        if compiled_version is not None:
            spec, source = compiled_version
            compiled.append((creation, copy.deepcopy(spec), copy.deepcopy(source)))

    updated = 0
    if apply:
        for row, spec, source in compiled:
            if isinstance(row, PublishedVideo):
                row.timeline = source
            else:
                row.source_timeline = source
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
    parser.add_argument(
        "--rotation-directions",
        action="store_true",
        help="Backfill only audited rotate directions into published source timelines.",
    )
    parser.add_argument(
        "--circle-directions",
        action="store_true",
        help="Audit draw_circle works and backfill missing directions with the canonical default.",
    )
    args = parser.parse_args()

    if args.rotation_directions and args.circle_directions:
        parser.error("choose only one direction backfill mode")

    with SessionLocal() as db:
        if args.rotation_directions:
            report = backfill_known_rotation_directions(db, apply=args.apply)
        else:
            report = compile_all_runtime_specs(
                db,
                apply=args.apply,
                only_circle_directions=args.circle_directions,
            )
    print(
        json.dumps(
            {
                **asdict(report),
                "mode": "apply" if args.apply else "dry-run",
                "scope": (
                    "audited_rotation_directions"
                    if args.rotation_directions
                    else "circle_directions"
                    if args.circle_directions
                    else "all_runtime_specs"
                ),
                "latest_runtime_spec_version": RUNTIME_SPEC_VERSION,
                "supported_runtime_spec_versions": sorted(
                    SUPPORTED_RUNTIME_SPEC_VERSIONS
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
