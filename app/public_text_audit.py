"""Inventory non-English public text without exposing the original values."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    CreatorCreation,
    CreatorSourceGeneration,
    CreatorVersion,
    PublishedVideo,
    User,
)
from app.public_text import (
    inspect_public_fields,
    public_text_entity,
    record_entity_text,
)

_MODELS = (
    User,
    PublishedVideo,
    CreatorCreation,
    CreatorSourceGeneration,
    CreatorVersion,
)


@dataclass(frozen=True)
class AuditFinding:
    entity_type: str
    entity_id: str
    field_path: str
    detected_scripts: tuple[str, ...]
    sample_sha256: str


@dataclass(frozen=True)
class AuditReport:
    entities_scanned: int
    findings: list[AuditFinding]
    recorded: bool


def iter_public_text_rows(db: Session) -> Iterator[object]:
    for model in _MODELS:
        yield from db.query(model).order_by(model.__table__.primary_key.columns[0]).all()


def scan_public_text(db: Session, *, record: bool) -> AuditReport:
    findings: list[AuditFinding] = []
    entity_count = 0
    for row in iter_public_text_rows(db):
        entity_count += 1
        entity_type, entity_id, fields = public_text_entity(row)
        for field_path, (value, scripts) in inspect_public_fields(fields).items():
            findings.append(
                AuditFinding(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    field_path=field_path,
                    detected_scripts=scripts,
                    sample_sha256=hashlib.sha256(value.encode("utf-8")).hexdigest(),
                )
            )
        if record:
            record_entity_text(db, row)
    if record:
        db.commit()
    else:
        db.rollback()
    return AuditReport(
        entities_scanned=entity_count,
        findings=findings,
        recorded=record,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit public text scripts without changing public content."
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Persist findings. Without this flag the command is read-only.",
    )
    args = parser.parse_args()
    with SessionLocal() as db:
        report = scan_public_text(db, record=args.record)
    print(json.dumps(asdict(report), ensure_ascii=True, indent=2))
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
