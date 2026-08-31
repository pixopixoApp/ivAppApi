from __future__ import annotations

import hashlib

import pytest

from app.models import PublicTextIssue, User
from app.public_copy import INTERACTION_INSTRUCTIONS
from app.public_text import detected_non_english_scripts
from app.public_text_audit import scan_public_text
from app.public_text_export import build_translation_template
from app.public_text_translate import (
    TranslationManifestError,
    apply_translation_manifest,
)
from app.vision_targets import VISION_TARGETS


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _user(user_id: str, nickname: str, bio: str = "") -> User:
    return User(
        user_id=user_id,
        provider="email",
        subject=f"{user_id}@example.com",
        enabled=True,
        nickname=nickname,
        bio=bio,
        source="app",
    )


def test_script_detection_allows_latin_and_reports_non_latin() -> None:
    assert detected_non_english_scripts("Pixo 123 ✨") == ()
    assert detected_non_english_scripts("你好 Pixo") == ("Han",)
    assert detected_non_english_scripts("こんにちは") == ("Hiragana",)


def test_canonical_runtime_and_vision_copy_is_english() -> None:
    assert len(INTERACTION_INSTRUCTIONS) == 32
    assert all(
        not detected_non_english_scripts(instruction)
        for instruction in INTERACTION_INSTRUCTIONS.values()
    )
    assert len(VISION_TARGETS) == 16
    assert all(
        not detected_non_english_scripts(str(value[key]))
        for value in VISION_TARGETS.values()
        for key in ("label", "instruction")
    )


def test_audit_and_translation_are_non_blocking_and_idempotent(db) -> None:
    nickname = "像素玩家"
    row = _user("language-user", nickname)
    db.add(row)
    db.commit()

    dry_run = scan_public_text(db, record=False)
    assert dry_run.entities_scanned == 1
    assert [(item.entity_type, item.field_path) for item in dry_run.findings] == [
        ("user", "nickname")
    ]
    assert db.query(PublicTextIssue).count() == 0

    recorded = scan_public_text(db, record=True)
    assert len(recorded.findings) == 1
    assert db.query(PublicTextIssue).filter_by(status="open").count() == 1
    template = build_translation_template(db)
    assert template["entries"][0]["source"] == nickname
    assert template["entries"][0]["replacement"] is None

    manifest = {
        "version": 1,
        "entries": [
            {
                "entity_type": "user",
                "entity_id": row.user_id,
                "field_path": "nickname",
                "source_sha256": _digest(nickname),
                "replacement": "Pixel Player",
            }
        ],
    }
    preview = apply_translation_manifest(db, manifest, apply=False)
    assert preview.changed == 1
    db.refresh(row)
    assert row.nickname == nickname

    applied = apply_translation_manifest(db, manifest, apply=True)
    assert applied.changed == 1
    db.refresh(row)
    assert row.nickname == "Pixel Player"
    assert db.query(PublicTextIssue).filter_by(status="resolved").count() == 1

    repeated = apply_translation_manifest(db, manifest, apply=True)
    assert repeated.changed == 0
    assert repeated.already_applied == 1


def test_translation_manifest_rolls_back_every_entry_on_stale_source(db) -> None:
    first = _user("first", "第一位")
    second = _user("second", "第二位")
    db.add_all([first, second])
    db.commit()
    manifest = {
        "version": 1,
        "entries": [
            {
                "entity_type": "user",
                "entity_id": first.user_id,
                "field_path": "nickname",
                "source_sha256": _digest(first.nickname),
                "replacement": "First",
            },
            {
                "entity_type": "user",
                "entity_id": second.user_id,
                "field_path": "nickname",
                "source_sha256": "0" * 64,
                "replacement": "Second",
            },
        ],
    }

    with pytest.raises(TranslationManifestError, match="source changed since audit"):
        apply_translation_manifest(db, manifest, apply=True)

    db.refresh(first)
    db.refresh(second)
    assert first.nickname == "第一位"
    assert second.nickname == "第二位"
