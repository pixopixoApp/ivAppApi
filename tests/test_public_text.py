from __future__ import annotations

from app.models import PublicTextIssue, User
from app.public_copy import INTERACTION_INSTRUCTIONS
from app.public_text import detected_non_english_scripts
from app.public_text_audit import scan_public_text
from app.vision_targets import VISION_TARGETS


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
    assert len(INTERACTION_INSTRUCTIONS) == 39
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


def test_audit_is_non_blocking_and_preserves_canonical_text(db) -> None:
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
    db.refresh(row)
    assert row.nickname == nickname
