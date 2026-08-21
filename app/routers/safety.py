from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth_user import AppUser, require_bearer_user
from app.db import get_db
from app.deps import require_publish_key
from app.models import ContentReport, Follow, PublishedVideo, User, UserBlock, UserToken
from app.schemas_safety import (
    BlockedUserOut,
    BlockMutationOut,
    SafetyReportCreated,
    SafetyReportDecisionRequest,
    SafetyReportOut,
    SafetyReportPage,
    SafetyReportRequest,
)

client_router = APIRouter(prefix="/api/v1/safety", tags=["safety"])
operations_router = APIRouter(
    prefix="/internal/v1/moderation",
    tags=["moderation"],
    dependencies=[Depends(require_publish_key)],
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _report_out(db: Session, row: ContentReport) -> SafetyReportOut:
    reporter = db.get(User, row.reporter_user_id)
    target_user = db.get(User, row.target_user_id) if row.target_user_id else None
    if row.target_type == "video":
        video = db.get(PublishedVideo, row.target_id)
        target_label = (video.title if video is not None else "") or row.target_id
    else:
        target_label = ((target_user.nickname if target_user else "") or row.target_id)
    return SafetyReportOut(
        id=row.id,
        reporter_user_id=row.reporter_user_id,
        target_type=row.target_type,
        target_id=row.target_id,
        target_user_id=row.target_user_id,
        target_label=target_label,
        reporter_label=((reporter.nickname if reporter else "") or row.reporter_user_id),
        reason=row.reason,
        details=row.details or "",
        status=row.status,
        resolution=row.resolution or "",
        reviewed_by=row.reviewed_by or "",
        reviewed_at=_iso(row.reviewed_at),
        created_at=_iso(row.created_at) or "",
        updated_at=_iso(row.updated_at) or "",
    )


@client_router.post("/reports", response_model=SafetyReportCreated)
def create_report(
    payload: SafetyReportRequest,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SafetyReportCreated:
    target_id = payload.target_id.strip()
    if payload.target_type == "video":
        video = db.get(PublishedVideo, target_id)
        if video is None or video.deleted_at is not None:
            raise HTTPException(status_code=404, detail="video not found")
        target_user_id = (video.user_id or "").strip() or None
    else:
        target = db.get(User, target_id)
        if target is None or not target.enabled:
            raise HTTPException(status_code=404, detail="user not found")
        target_user_id = target.user_id
    if target_user_id == user.user_id:
        raise HTTPException(status_code=400, detail="cannot report yourself")

    existing = (
        db.query(ContentReport)
        .filter(
            ContentReport.reporter_user_id == user.user_id,
            ContentReport.target_type == payload.target_type,
            ContentReport.target_id == target_id,
        )
        .one_or_none()
    )
    now = _now()
    if existing is None:
        existing = ContentReport(
            id=f"rpt_{secrets.token_urlsafe(18)}",
            reporter_user_id=user.user_id,
            target_type=payload.target_type,
            target_id=target_id,
            target_user_id=target_user_id,
            reason=payload.reason.strip(),
            details=payload.details.strip(),
            status="pending",
            created_at=now,
            updated_at=now,
        )
        db.add(existing)
    else:
        existing.reason = payload.reason.strip()
        existing.details = payload.details.strip()
        existing.target_user_id = target_user_id
        existing.status = "pending"
        existing.resolution = ""
        existing.reviewed_by = ""
        existing.reviewed_at = None
        existing.updated_at = now
    db.commit()
    return SafetyReportCreated(report_id=existing.id, status="pending")


@client_router.post("/blocks/{user_id}", response_model=BlockMutationOut)
def block_user(
    user_id: str,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> BlockMutationOut:
    target_id = user_id.strip()
    target = db.get(User, target_id) if target_id else None
    if target is None or not target.enabled:
        raise HTTPException(status_code=404, detail="user not found")
    if target_id == user.user_id:
        raise HTTPException(status_code=400, detail="cannot block yourself")
    row = (
        db.query(UserBlock)
        .filter(
            UserBlock.blocker_user_id == user.user_id,
            UserBlock.blocked_user_id == target_id,
        )
        .one_or_none()
    )
    if row is None:
        db.add(UserBlock(blocker_user_id=user.user_id, blocked_user_id=target_id))
    db.query(Follow).filter(
        ((Follow.follower_user_id == user.user_id) & (Follow.followee_user_id == target_id))
        | ((Follow.follower_user_id == target_id) & (Follow.followee_user_id == user.user_id))
    ).delete(synchronize_session=False)
    db.commit()
    return BlockMutationOut(user_id=target_id, blocked=True)


@client_router.delete("/blocks/{user_id}", response_model=BlockMutationOut)
def unblock_user(
    user_id: str,
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> BlockMutationOut:
    target_id = user_id.strip()
    if not target_id:
        raise HTTPException(status_code=400, detail="user_id required")
    db.query(UserBlock).filter(
        UserBlock.blocker_user_id == user.user_id,
        UserBlock.blocked_user_id == target_id,
    ).delete(synchronize_session=False)
    db.commit()
    return BlockMutationOut(user_id=target_id, blocked=False)


@client_router.get("/blocks", response_model=list[BlockedUserOut])
def list_blocks(
    user: Annotated[AppUser, Depends(require_bearer_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[BlockedUserOut]:
    rows = (
        db.query(UserBlock)
        .filter(UserBlock.blocker_user_id == user.user_id)
        .order_by(UserBlock.created_at.desc())
        .limit(500)
        .all()
    )
    result: list[BlockedUserOut] = []
    for row in rows:
        target = db.get(User, row.blocked_user_id)
        result.append(
            BlockedUserOut(
                user_id=row.blocked_user_id,
                nickname=(target.nickname if target else "") or "",
                avatar_url=(target.avatar_url if target else "") or "",
                created_at=_iso(row.created_at) or "",
            )
        )
    return result


@operations_router.get("/reports", response_model=SafetyReportPage)
def list_reports(
    db: Annotated[Session, Depends(get_db)],
    status: Literal["pending", "actioned", "dismissed"] | None = None,
    target_type: Literal["video", "user"] | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> SafetyReportPage:
    query = db.query(ContentReport)
    if status:
        query = query.filter(ContentReport.status == status)
    if target_type:
        query = query.filter(ContentReport.target_type == target_type)
    total = query.count()
    rows = query.order_by(ContentReport.created_at.desc()).offset(offset).limit(limit).all()
    return SafetyReportPage(
        items=[_report_out(db, row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@operations_router.post("/reports/{report_id}/decision", response_model=SafetyReportOut)
def decide_report(
    report_id: str,
    payload: SafetyReportDecisionRequest,
    db: Annotated[Session, Depends(get_db)],
) -> SafetyReportOut:
    row = db.get(ContentReport, report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    if payload.status == "dismissed" and payload.action != "none":
        raise HTTPException(
            status_code=400,
            detail="dismissed reports cannot apply a moderation action",
        )
    if payload.action == "remove_content":
        if row.target_type != "video":
            raise HTTPException(status_code=400, detail="remove_content requires a video report")
        video = db.get(PublishedVideo, row.target_id)
        if video is not None and video.deleted_at is None:
            video.deleted_at = _now()
    elif payload.action == "disable_user":
        target_user_id = (row.target_user_id or "").strip()
        target = db.get(User, target_user_id) if target_user_id else None
        if target is None:
            raise HTTPException(status_code=404, detail="target user not found")
        target.enabled = False
        db.query(UserToken).filter(UserToken.user_id == target.user_id).delete(
            synchronize_session=False
        )
    row.status = payload.status
    row.resolution = payload.resolution.strip()
    row.reviewed_by = payload.reviewed_by.strip()
    row.reviewed_at = _now()
    row.updated_at = row.reviewed_at
    db.commit()
    db.refresh(row)
    return _report_out(db, row)
