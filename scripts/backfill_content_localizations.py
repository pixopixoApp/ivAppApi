from __future__ import annotations

import argparse
import time

from app.content_localization import queue_content_translation
from app.db import SessionLocal
from app.models import PublishedVideo


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Queue translations for published Runtime works without changing source text."
    )
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--delay-seconds", type=float, default=0.25)
    args = parser.parse_args()
    queued = 0
    offset = 0
    with SessionLocal() as db:
        while True:
            rows = (
                db.query(PublishedVideo)
                .filter(
                    PublishedVideo.is_deleted == 0,
                    PublishedVideo.content_type == "runtime",
                )
                .order_by(PublishedVideo.created_at.asc())
                .offset(offset)
                .limit(max(1, min(args.batch_size, 500)))
                .all()
            )
            if not rows:
                break
            for row in rows:
                if queue_content_translation(db, row) is not None:
                    queued += 1
            db.commit()
            offset += len(rows)
            time.sleep(max(0.0, args.delay_seconds))
    print(f"queued_or_existing={queued}")


if __name__ == "__main__":
    main()
