#!/usr/bin/env python3
"""Import a validated, one-time Codex localization backfill.

This importer deliberately reuses the production localization validator and
source-hash guard. It never updates canonical creator text.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.content_localization import (
    apply_translation_result,
    extract_source_payload,
    source_hash,
)
from app.db import SessionLocal
from app.locale import SUPPORTED_LOCALES
from app.models import ContentTranslationJob, PublishedVideo


def _records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError(f"line {line_number}: record must be an object")
            records.append(value)
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--model", default="codex-gpt-5.6-sol")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    records = _records(args.input)
    seen: set[str] = set()
    imported = 0
    skipped = 0

    with SessionLocal() as db:
        for index, record in enumerate(records, start=1):
            job_id = str(record.get("job_id") or "")
            if not job_id or job_id in seen:
                raise ValueError(f"record {index}: missing or duplicate job_id")
            seen.add(job_id)

            job = db.get(ContentTranslationJob, job_id)
            if job is None:
                raise ValueError(f"record {index}: unknown job {job_id}")
            if job.entity_id != str(record.get("entity_id") or ""):
                raise ValueError(f"record {index}: entity_id mismatch for {job_id}")
            if job.source_hash != str(record.get("source_hash") or ""):
                raise ValueError(f"record {index}: source_hash mismatch for {job_id}")

            current = db.get(PublishedVideo, job.entity_id)
            if current is None:
                raise ValueError(f"record {index}: missing content {job.entity_id}")
            if source_hash(extract_source_payload(current)) != job.source_hash:
                raise ValueError(f"record {index}: source changed for {job.entity_id}")

            if job.status == "ready" and job.completed_at is not None:
                skipped += 1
                continue
            if args.dry_run:
                imported += 1
                continue

            translations = record.get("translations")
            if not isinstance(translations, dict):
                raise TypeError(f"record {index}: translations must be an object")
            if set(translations) != set(SUPPORTED_LOCALES):
                raise ValueError(f"record {index}: locale set mismatch")

            # A previously partial server job may contain only its missing
            # locales. The Codex backfill always provides the complete set.
            job.target_locales = list(SUPPORTED_LOCALES)
            job.status = "stale"
            job.completed_at = None
            apply_translation_result(
                db,
                job,
                {
                    "detected_source_locale": record.get("detected_source_locale"),
                    "translations": translations,
                    "model": args.model,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            )
            db.refresh(job)
            if job.status != "ready":
                raise ValueError(
                    f"record {index}: production validation failed for {job_id}: "
                    f"{job.last_error}"
                )
            imported += 1

    print(json.dumps({"validated": len(records), "imported": imported, "skipped": skipped}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
