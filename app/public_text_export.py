"""Export raw public text for an explicitly authorized offline translation pass.

Unlike the normal audit command, this command contains source values and must
be redirected to a protected file rather than copied into application logs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any

from app.db import SessionLocal
from app.public_text import inspect_public_fields, public_text_entity
from app.public_text_audit import iter_public_text_rows


def build_translation_template(db) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for row in iter_public_text_rows(db):
        entity_type, entity_id, fields = public_text_entity(row)
        for field_path, (source, scripts) in inspect_public_fields(fields).items():
            entries.append(
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "field_path": field_path,
                    "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                    "detected_scripts": list(scripts),
                    "source": source,
                    "replacement": None,
                }
            )
    return {"version": 1, "entries": entries}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export an offline public-text translation template with raw values."
    )
    parser.add_argument(
        "--include-source",
        action="store_true",
        help="Required acknowledgement that raw public values will be emitted.",
    )
    args = parser.parse_args()
    if not args.include_source:
        parser.error("--include-source is required")
    with SessionLocal() as db:
        template = build_translation_template(db)
    print(json.dumps(template, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
