"""Apply a reviewed, hash-bound English translation manifest atomically."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    CreatorCreation,
    CreatorSourceGeneration,
    CreatorVersion,
    PublishedVideo,
    User,
)
from app.public_text import detected_non_english_scripts, record_entity_text

_ENTITY_MODELS: dict[str, tuple[type, frozenset[str]]] = {
    "user": (User, frozenset({"nickname", "bio"})),
    "published_video": (
        PublishedVideo,
        frozenset({"title", "description", "timeline", "runtime_spec"}),
    ),
    "creator_creation": (
        CreatorCreation,
        frozenset(
            {
                "source_prompt",
                "brief",
                "analysis_result",
                "source_timeline",
                "runtime_spec",
                "error_message",
            }
        ),
    ),
    "creator_source_generation": (
        CreatorSourceGeneration,
        frozenset(
            {
                "original_prompt",
                "prompt_summary",
                "generation_prompt",
                "interaction_brief",
                "error_message",
            }
        ),
    ),
    "creator_version": (
        CreatorVersion,
        frozenset({"brief", "source_timeline", "runtime_spec", "error_message"}),
    ),
}
_PATH_PART = re.compile(r"(?:^|\.)([^.\[\]]+)|\[(\d+)\]")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class TranslationManifestError(ValueError):
    pass


@dataclass(frozen=True)
class TranslationReport:
    entries: int
    changed: int
    already_applied: int
    applied: bool


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _path_parts(path: str) -> list[str | int]:
    parts: list[str | int] = []
    position = 0
    for match in _PATH_PART.finditer(path):
        if match.start() != position:
            raise TranslationManifestError(f"invalid field_path: {path}")
        parts.append(match.group(1) if match.group(1) is not None else int(match.group(2)))
        position = match.end()
    if position != len(path) or not parts:
        raise TranslationManifestError(f"invalid field_path: {path}")
    return parts


def _leaf(root: Any, parts: list[str | int], *, field_path: str) -> Any:
    value = root
    for part in parts:
        try:
            value = value[part]
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslationManifestError(f"field_path not found: {field_path}") from exc
    return value


def _replace_leaf(root: Any, parts: list[str | int], replacement: str, *, field_path: str) -> Any:
    if not parts:
        return replacement
    updated = copy.deepcopy(root)
    parent = updated
    for part in parts[:-1]:
        try:
            parent = parent[part]
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslationManifestError(f"field_path not found: {field_path}") from exc
    try:
        parent[parts[-1]] = replacement
    except (KeyError, IndexError, TypeError) as exc:
        raise TranslationManifestError(f"field_path not writable: {field_path}") from exc
    return updated


def apply_translation_manifest(
    db: Session, manifest: dict[str, Any], *, apply: bool
) -> TranslationReport:
    if manifest.get("version") != 1 or not isinstance(manifest.get("entries"), list):
        raise TranslationManifestError("manifest must contain version=1 and entries[]")
    changed = 0
    already_applied = 0
    touched: dict[tuple[str, str], object] = {}
    seen: set[tuple[str, str, str]] = set()
    try:
        for index, entry in enumerate(manifest["entries"]):
            if not isinstance(entry, dict):
                raise TranslationManifestError(f"entry {index} must be an object")
            entity_type = str(entry.get("entity_type") or "")
            entity_id = str(entry.get("entity_id") or "")
            field_path = str(entry.get("field_path") or "")
            expected_hash = str(entry.get("source_sha256") or "").lower()
            replacement = entry.get("replacement")
            if entity_type not in _ENTITY_MODELS or not entity_id:
                raise TranslationManifestError(f"entry {index} has an invalid entity")
            if not isinstance(replacement, str):
                raise TranslationManifestError(f"entry {index} replacement must be text")
            if not replacement.strip():
                raise TranslationManifestError(f"entry {index} replacement must not be blank")
            if detected_non_english_scripts(replacement):
                raise TranslationManifestError(
                    f"entry {index} replacement still contains a non-English script"
                )
            if not _SHA256.fullmatch(expected_hash):
                raise TranslationManifestError(f"entry {index} has an invalid source_sha256")
            identity = (entity_type, entity_id, field_path)
            if identity in seen:
                raise TranslationManifestError(f"duplicate manifest entry: {identity}")
            seen.add(identity)
            model, allowed_columns = _ENTITY_MODELS[entity_type]
            parts = _path_parts(field_path)
            column = parts.pop(0)
            if not isinstance(column, str) or column not in allowed_columns:
                raise TranslationManifestError(f"field is not translatable: {field_path}")
            row = db.get(model, entity_id)
            if row is None:
                raise TranslationManifestError(f"entity not found: {entity_type}/{entity_id}")
            root = getattr(row, column)
            current = _leaf(root, parts, field_path=field_path)
            if not isinstance(current, str):
                raise TranslationManifestError(f"field is not text: {field_path}")
            if current == replacement:
                already_applied += 1
                touched[(entity_type, entity_id)] = row
                continue
            if _digest(current) != expected_hash:
                raise TranslationManifestError(
                    f"source changed since audit: {entity_type}/{entity_id}/{field_path}"
                )
            setattr(
                row,
                column,
                _replace_leaf(root, parts, replacement, field_path=field_path),
            )
            touched[(entity_type, entity_id)] = row
            changed += 1
        if apply:
            for row in touched.values():
                record_entity_text(db, row)
            db.commit()
        else:
            db.rollback()
    except Exception:
        db.rollback()
        raise
    return TranslationReport(
        entries=len(manifest["entries"]),
        changed=changed,
        already_applied=already_applied,
        applied=apply,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply a reviewed English public-text translation manifest."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit all translations. Without this flag the command is a dry run.",
    )
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        with SessionLocal() as db:
            report = apply_translation_manifest(db, manifest, apply=args.apply)
    except (OSError, json.JSONDecodeError, TranslationManifestError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True, indent=2))
        return 1
    print(json.dumps(asdict(report), ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
