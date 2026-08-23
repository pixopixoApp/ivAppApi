#!/usr/bin/env python3
"""Queue deterministic canary/batch normalization for legacy creator videos."""

from __future__ import annotations

import argparse
import hashlib
import json

from app.db import SessionLocal
from app.models import CreatorUpload


def _bucket(upload_id: str) -> int:
    return int(hashlib.sha256(upload_id.encode("utf-8")).hexdigest()[:8], 16) % 100


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="persist pending state")
    parser.add_argument("--min-bytes", type=int, default=10 * 1024 * 1024)
    parser.add_argument("--canary-percent", type=int, default=5)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.min_bytes < 0 or not 1 <= args.canary_percent <= 100 or args.limit < 1:
        parser.error("invalid migration bounds")

    with SessionLocal() as db:
        rows = (
            db.query(CreatorUpload)
            .filter(
                CreatorUpload.normalization_status == "legacy_pending",
                CreatorUpload.source_sha256 != "",
                CreatorUpload.size_bytes >= args.min_bytes,
            )
            .order_by(CreatorUpload.created_at.asc(), CreatorUpload.id.asc())
            .all()
        )
        eligible = [row for row in rows if _bucket(row.id) < args.canary_percent]
        selected = eligible[: args.limit]
        if args.apply:
            for row in selected:
                row.normalization_status = "pending"
                row.normalization_error = ""
                db.add(row)
            db.commit()
        print(
            json.dumps(
                {
                    "mode": "apply" if args.apply else "dry-run",
                    "minimum_bytes": args.min_bytes,
                    "canary_percent": args.canary_percent,
                    "eligible": len(eligible),
                    "selected": len(selected),
                    "remaining_after_batch": max(0, len(eligible) - len(selected)),
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
