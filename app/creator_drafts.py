"""Lightweight draft discovery and per-account generation admission."""
from __future__ import annotations

import base64
import json
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models import (
    CreatorCreation,
    CreatorSourceGeneration,
    CreatorUpload,
    CreatorVersion,
)

DRAFT_EXCLUDED = ("published", "pending_review", "rejected", "deleted", "abandoned")


def require_other_creations_idle(db: Session, user_id: str, creation_id: str = "") -> None:
    # source_ready is an editable draft; cancelled provider jobs remain busy
    # until durable cancellation is confirmed, even after logical deletion.
    for model in (CreatorSourceGeneration, CreatorVersion):
        busy = db.query(model).filter(model.user_id == user_id,
            model.creation_id != creation_id, model.status.in_(("queued", "running"))).first()
        if busy is not None:
            raise HTTPException(409, {"code": "CREATOR_BUSY",
                "message": "Another draft is generating. Your new draft is saved; wait for that task to finish.",
                "creation_id": busy.creation_id})


def draft_page(db: Session, user_id: str, limit: int, cursor: str | None) -> dict:
    query = db.query(CreatorCreation).filter(CreatorCreation.user_id == user_id,
        CreatorCreation.status.notin_(DRAFT_EXCLUDED))
    total = query.count()
    if cursor:
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if value["user"] != user_id:
                raise ValueError("wrong owner")
            stamp, identifier = datetime.fromisoformat(value["updated"]), value["id"]
            if not isinstance(identifier, str) or not identifier or len(identifier) > 64:
                raise ValueError("invalid identifier")
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(400, "invalid draft cursor") from exc
        query = query.filter(or_(CreatorCreation.updated_at < stamp,
            and_(CreatorCreation.updated_at == stamp, CreatorCreation.id < identifier)))
    rows = query.order_by(CreatorCreation.updated_at.desc(), CreatorCreation.id.desc()).limit(limit + 1).all()
    page = rows[:limit]
    identifiers = [row.id for row in page]
    uploads = {row.id: row for row in db.query(CreatorUpload).filter(
        CreatorUpload.id.in_([row.upload_id for row in page if row.upload_id]),
        CreatorUpload.user_id == user_id).all()}
    initial = {}
    for model, ordering in ((CreatorSourceGeneration, CreatorSourceGeneration.attempt),
                            (CreatorVersion, CreatorVersion.number)):
        for job in db.query(model).filter(model.creation_id.in_(identifiers),
                model.user_id == user_id).order_by(ordering.asc()).all():
            initial.setdefault(job.creation_id, job.request_id)
    items = []
    for row in page:
        upload = uploads.get(row.upload_id)
        title = (upload.original_filename if upload and row.source_mode != "prompt"
                 else row.source_prompt or row.brief or "Untitled draft")
        items.append({"creation_id": row.id, "initial_request_id": row.request_id or initial.get(row.id),
            "title": title[:120], "source_mode": row.source_mode,
            "experience_mode": row.experience_mode, "status": row.status,
            "progress_stage": row.progress_stage, "progress_percent": row.progress_percent,
            "duration_ms": upload.duration_ms if upload else 0,
            "updated_at": row.updated_at.isoformat()})
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps({"user": user_id,
            "updated": last.updated_at.isoformat(), "id": last.id}).encode()).decode().rstrip("=")
    return {"items": items, "total": total, "next_cursor": next_cursor}
