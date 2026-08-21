from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ReportTargetType = Literal["video", "user"]
ReportStatus = Literal["pending", "actioned", "dismissed"]


class SafetyReportRequest(BaseModel):
    target_type: ReportTargetType
    target_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=64)
    details: str = Field(default="", max_length=500)


class SafetyReportOut(BaseModel):
    id: str
    reporter_user_id: str
    target_type: ReportTargetType
    target_id: str
    target_user_id: str | None = None
    target_label: str = ""
    reporter_label: str = ""
    reason: str
    details: str
    status: ReportStatus
    resolution: str
    reviewed_by: str
    reviewed_at: str | None = None
    created_at: str
    updated_at: str


class SafetyReportCreated(BaseModel):
    report_id: str
    status: ReportStatus


class BlockedUserOut(BaseModel):
    user_id: str
    nickname: str
    avatar_url: str
    created_at: str


class BlockMutationOut(BaseModel):
    user_id: str
    blocked: bool


class SafetyReportPage(BaseModel):
    items: list[SafetyReportOut]
    total: int
    limit: int
    offset: int


class SafetyReportDecisionRequest(BaseModel):
    status: Literal["actioned", "dismissed"]
    action: Literal["none", "remove_content", "disable_user"] = "none"
    resolution: str = Field(default="", max_length=500)
    reviewed_by: str = Field(default="", max_length=64)
