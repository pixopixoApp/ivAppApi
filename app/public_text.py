"""English-first public text inspection that never rejects user content."""

from __future__ import annotations

import hashlib
import logging
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    CreatorCreation,
    CreatorSourceGeneration,
    CreatorVersion,
    PublicTextIssue,
    PublishedVideo,
    User,
)

log = logging.getLogger(__name__)

_IGNORED_TYPES = (bytes, bytearray)


def _letter_script(character: str) -> str | None:
    """Return a broad non-Latin script name for a visible letter."""
    if not character.isalpha():
        return None
    name = unicodedata.name(character, "")
    if "LATIN" in name:
        return None
    for token, label in (
        ("CJK", "Han"),
        ("IDEOGRAPH", "Han"),
        ("HIRAGANA", "Hiragana"),
        ("KATAKANA", "Katakana"),
        ("HANGUL", "Hangul"),
        ("CYRILLIC", "Cyrillic"),
        ("ARABIC", "Arabic"),
        ("HEBREW", "Hebrew"),
        ("DEVANAGARI", "Devanagari"),
        ("THAI", "Thai"),
        ("GREEK", "Greek"),
    ):
        if token in name:
            return label
    return "Other"


def detected_non_english_scripts(text: str) -> tuple[str, ...]:
    return tuple(sorted({script for char in text if (script := _letter_script(char))}))


def has_non_english_script(text: str) -> bool:
    return bool(detected_non_english_scripts(text))


def iter_text_values(value: Any, *, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path or "$", value
        return
    if value is None or isinstance(value, _IGNORED_TYPES):
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from iter_text_values(child, path=child_path)
        return
    if isinstance(value, Sequence):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]" if path else f"[{index}]"
            yield from iter_text_values(child, path=child_path)


def inspect_public_fields(fields: Mapping[str, Any]) -> dict[str, tuple[str, tuple[str, ...]]]:
    issues: dict[str, tuple[str, tuple[str, ...]]] = {}
    for field, value in fields.items():
        for path, text in iter_text_values(value, path=field):
            scripts = detected_non_english_scripts(text)
            if scripts:
                issues[path] = (text, scripts)
    return issues


def record_public_text_issues(
    db: Session,
    *,
    entity_type: str,
    entity_id: str,
    fields: Mapping[str, Any],
) -> int:
    """Upsert audit findings without changing or rejecting source content."""
    now = datetime.now(timezone.utc)
    findings = inspect_public_fields(fields)
    existing = {
        row.field_path: row
        for row in db.query(PublicTextIssue)
        .filter(
            PublicTextIssue.entity_type == entity_type,
            PublicTextIssue.entity_id == entity_id,
        )
        .all()
    }
    for field_path, (text, scripts) in findings.items():
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        row = existing.get(field_path)
        if row is None:
            row = PublicTextIssue(
                entity_type=entity_type,
                entity_id=entity_id,
                field_path=field_path,
                detected_scripts=list(scripts),
                sample_sha256=digest,
                status="open",
                first_seen_at=now,
                last_seen_at=now,
                resolved_at=None,
            )
        else:
            row.detected_scripts = list(scripts)
            row.sample_sha256 = digest
            row.status = "open"
            row.last_seen_at = now
            row.resolved_at = None
        db.add(row)
        log.warning(
            "public_text_non_english entity_type=%s entity_id=%s field=%s scripts=%s sha256=%s",
            entity_type,
            entity_id,
            field_path,
            ",".join(scripts),
            digest,
        )
    for field_path, row in existing.items():
        if field_path not in findings and row.status == "open":
            row.status = "resolved"
            row.last_seen_at = now
            row.resolved_at = now
            db.add(row)
    return len(findings)


def record_user_text(db: Session, row: User) -> int:
    return record_public_text_issues(
        db,
        entity_type="user",
        entity_id=row.user_id,
        fields={"nickname": row.nickname, "bio": row.bio},
    )


def record_published_video_text(db: Session, row: PublishedVideo) -> int:
    return record_public_text_issues(
        db,
        entity_type="published_video",
        entity_id=row.id,
        fields={
            "title": row.title,
            "description": row.description,
            "timeline": row.timeline,
            "runtime_spec": row.runtime_spec,
        },
    )


def record_creator_creation_text(db: Session, row: CreatorCreation) -> int:
    return record_public_text_issues(
        db,
        entity_type="creator_creation",
        entity_id=row.id,
        fields={
            "source_prompt": row.source_prompt,
            "brief": row.brief,
            "analysis_result": row.analysis_result,
            "source_timeline": row.source_timeline,
            "runtime_spec": row.runtime_spec,
            "error_message": row.error_message,
        },
    )


def record_creator_generation_text(db: Session, row: CreatorSourceGeneration) -> int:
    return record_public_text_issues(
        db,
        entity_type="creator_source_generation",
        entity_id=row.id,
        fields={
            "original_prompt": row.original_prompt,
            "prompt_summary": row.prompt_summary,
            "generation_prompt": row.generation_prompt,
            "interaction_brief": row.interaction_brief,
            "error_message": row.error_message,
        },
    )


def record_creator_version_text(db: Session, row: CreatorVersion) -> int:
    return record_public_text_issues(
        db,
        entity_type="creator_version",
        entity_id=row.id,
        fields={
            "brief": row.brief,
            "source_timeline": row.source_timeline,
            "runtime_spec": row.runtime_spec,
            "error_message": row.error_message,
        },
    )


def public_text_entity(
    row: User
    | PublishedVideo
    | CreatorCreation
    | CreatorSourceGeneration
    | CreatorVersion,
) -> tuple[str, str, dict[str, Any]]:
    """Return the stable audit identity and public fields for a supported row."""
    if isinstance(row, User):
        return "user", row.user_id, {"nickname": row.nickname, "bio": row.bio}
    if isinstance(row, PublishedVideo):
        return "published_video", row.id, {
            "title": row.title,
            "description": row.description,
            "timeline": row.timeline,
            "runtime_spec": row.runtime_spec,
        }
    if isinstance(row, CreatorCreation):
        return "creator_creation", row.id, {
            "source_prompt": row.source_prompt,
            "brief": row.brief,
            "analysis_result": row.analysis_result,
            "source_timeline": row.source_timeline,
            "runtime_spec": row.runtime_spec,
            "error_message": row.error_message,
        }
    if isinstance(row, CreatorSourceGeneration):
        return "creator_source_generation", row.id, {
            "original_prompt": row.original_prompt,
            "prompt_summary": row.prompt_summary,
            "generation_prompt": row.generation_prompt,
            "interaction_brief": row.interaction_brief,
            "error_message": row.error_message,
        }
    if isinstance(row, CreatorVersion):
        return "creator_version", row.id, {
            "brief": row.brief,
            "source_timeline": row.source_timeline,
            "runtime_spec": row.runtime_spec,
            "error_message": row.error_message,
        }
    raise TypeError(f"unsupported public text entity: {type(row).__name__}")


def record_entity_text(
    db: Session,
    row: User
    | PublishedVideo
    | CreatorCreation
    | CreatorSourceGeneration
    | CreatorVersion,
) -> int:
    entity_type, entity_id, fields = public_text_entity(row)
    return record_public_text_issues(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        fields=fields,
    )
