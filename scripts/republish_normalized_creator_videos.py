#!/usr/bin/env python3
"""Atomically switch legacy creator publications to normalized media."""

from __future__ import annotations

import argparse
import hashlib
import json

from app.cdn_cache import enqueue_prefetch
from app.cdn_publication import (
    CdnPublicationError,
    activate_ready_publications,
    require_runtime_cdn_gate,
    stage_publication_gate,
)
from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    CreatorCreation,
    CreatorUpload,
    MediaObject,
    PublishedMediaAsset,
    PublishedVideo,
)
from app.protocol_video import RUNTIME_SPEC_VERSION, compile_runtime_spec
from app.publication_service import RuntimeSourceAsset, publish_runtime_assets


def _bucket(value: str) -> int:
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:8], 16) % 100


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--canary-percent", type=int, default=5)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    if not 1 <= args.canary_percent <= 100 or args.limit < 1:
        parser.error("invalid migration bounds")
    settings = get_settings()
    migrated = 0
    warming = 0
    skipped = 0
    failures: list[dict[str, str]] = []

    with SessionLocal() as db:
        if args.apply:
            require_runtime_cdn_gate(settings)
        rows = (
            db.query(CreatorUpload, CreatorCreation, PublishedVideo)
            .join(CreatorCreation, CreatorCreation.upload_id == CreatorUpload.id)
            .join(PublishedVideo, PublishedVideo.id == CreatorCreation.id)
            .filter(
                CreatorUpload.normalization_status == "ready",
                CreatorUpload.playable_media_object_id.is_not(None),
                PublishedVideo.deleted_at.is_(None),
                PublishedVideo.is_deleted == 0,
            )
            .order_by(PublishedVideo.created_at.asc(), PublishedVideo.id.asc())
            .all()
        )
        selected = []
        for row in rows:
            upload, _creation, video = row
            if _bucket(video.id) >= args.canary_percent:
                continue
            playable = db.get(MediaObject, upload.playable_media_object_id)
            current = (
                db.query(PublishedMediaAsset)
                .filter(
                    PublishedMediaAsset.video_id == video.id,
                    PublishedMediaAsset.publication_id == video.active_publication_id,
                    PublishedMediaAsset.role == "single",
                )
                .first()
            )
            current_media = db.get(MediaObject, current.media_object_id) if current else None
            if playable is not None and current_media is not None and current_media.sha256 == playable.sha256:
                skipped += 1
                continue
            selected.append(row)
            if len(selected) >= args.limit:
                break
        for upload, _creation, video in selected:
            playable = db.get(MediaObject, upload.playable_media_object_id)
            if playable is None or playable.state != "ready":
                failures.append({"video_id": video.id, "error": "playable media is not ready"})
                continue
            if not args.apply:
                migrated += 1
                continue
            timeline = (
                video.timeline
                if isinstance(video.timeline, dict)
                else _creation.source_timeline
            )
            if not isinstance(timeline, dict):
                failures.append({"video_id": video.id, "error": "timeline is missing"})
                continue
            try:
                published = publish_runtime_assets(
                    db,
                    settings,
                    video_id=video.id,
                    version=f"mobile-v1-{playable.sha256[:12]}",
                    source_payload=timeline,
                    assets=[RuntimeSourceAsset(role="single", media=playable)],
                )
                final_url = published.urls["single"]
                runtime = compile_runtime_spec(
                    item_id=video.id,
                    content_mode="single",
                    source=timeline,
                    video_url=final_url,
                )
                enqueue_prefetch(db, settings, [final_url])
                gate = stage_publication_gate(
                    db,
                    video_id=video.id,
                    publication_id=published.publication_id,
                    urls=[final_url],
                    staged_payload={
                        "video_url": final_url,
                        "runtime_spec": runtime,
                        "runtime_spec_version": RUNTIME_SPEC_VERSION,
                        "active_publication_id": published.publication_id,
                    },
                )
                db.flush()
                activate_ready_publications(
                    db,
                    publication_ids=[published.publication_id],
                )
                db.flush()
                if gate.state == "failed":
                    raise CdnPublicationError(
                        gate.error_message or "CDN prefetch failed"
                    )
                db.commit()
                migrated += 1
                if gate.state != "active":
                    warming += 1
            except Exception as exc:  # noqa: BLE001 - report per-item batch failure
                db.rollback()
                failures.append({"video_id": video.id, "error": str(exc)[:300]})
        print(
            json.dumps(
                {
                    "mode": "apply" if args.apply else "dry-run",
                    "selected": len(selected),
                    "migrated_or_would_migrate": migrated,
                    "warming": warming,
                    "already_current": skipped,
                    "failures": failures,
                },
                sort_keys=True,
            )
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
