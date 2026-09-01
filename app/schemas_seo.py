from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_NON_ENGLISH_PUBLIC = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


class SeoMetadataWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=20, max_length=1200)
    meta_title: str = Field(min_length=10, max_length=70)
    meta_description: str = Field(min_length=50, max_length=180)
    slug: str | None = Field(default=None, max_length=160)
    tags: list[str] = Field(default_factory=list, max_length=12)
    interaction_types: list[str] = Field(default_factory=list, max_length=31)
    interaction_summary: str = Field(min_length=10, max_length=500)
    duration_seconds: float | None = Field(default=None, ge=0, le=86_400)
    width: int | None = Field(default=None, ge=1, le=16_384)
    height: int | None = Field(default=None, ge=1, le=16_384)
    thumbnail_url: str = Field(default="", max_length=2048)
    source_hash: str = Field(min_length=64, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=64)

    @field_validator("tags", "interaction_types")
    @classmethod
    def _clean_list(cls, value: list[str]) -> list[str]:
        clean: list[str] = []
        for raw in value:
            item = str(raw).strip().lower()[:64]
            if _NON_ENGLISH_PUBLIC.search(item):
                raise ValueError("SEO list values must be English")
            if item and item not in clean:
                clean.append(item)
        return clean

    @field_validator(
        "title",
        "description",
        "meta_title",
        "meta_description",
        "interaction_summary",
    )
    @classmethod
    def _require_english_public_copy(cls, value: str) -> str:
        if _NON_ENGLISH_PUBLIC.search(value):
            raise ValueError("SEO public copy must be English")
        return value


class SeoStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["pending", "generating", "failed", "stale"]
    error: str = Field(default="", max_length=4000)


class SeoBackfillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = False
    include_failed: bool = True
    limit: int = Field(default=500, ge=1, le=2000)


class SeoAdminEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_title: str | None = Field(default=None, min_length=3, max_length=120)
    page_description: str | None = Field(default=None, min_length=20, max_length=1200)
    meta_title: str | None = Field(default=None, min_length=10, max_length=70)
    meta_description: str | None = Field(default=None, min_length=50, max_length=180)
    tags: list[str] | None = Field(default=None, max_length=12)
    interaction_summary: str | None = Field(default=None, min_length=10, max_length=500)
    title_locked: bool | None = None
    description_locked: bool | None = None

    @field_validator(
        "page_title",
        "page_description",
        "meta_title",
        "meta_description",
        "interaction_summary",
    )
    @classmethod
    def _require_english_public_copy(cls, value: str | None) -> str | None:
        if value is not None and _NON_ENGLISH_PUBLIC.search(value):
            raise ValueError("SEO public copy must be English")
        return value

    @field_validator("tags")
    @classmethod
    def _require_english_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(_NON_ENGLISH_PUBLIC.search(item) for item in value):
            raise ValueError("SEO tags must be English")
        return value
