"""Report the deterministic creator-handle backfill without changing the database."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

from sqlalchemy import inspect, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import engine

RESERVED_HANDLES = frozenset({
    "admin", "api", "assets", "create", "download", "explore", "help",
    "login", "me", "media", "moderator", "privacy", "settings", "support",
    "terms", "videos", "www", "pixopixo", "pixo",
})


def _nickname_handle(raw: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", raw or "").encode(
        "ascii", "ignore"
    ).decode()
    value = re.sub(r"[^a-z0-9_]+", "_", ascii_value.lower()).strip("_")
    return re.sub(r"_+", "_", value)[:30]


def _candidate(nickname: str, user_id: str, occupied: set[str]) -> tuple[str, bool]:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    base = _nickname_handle(nickname)
    candidates: list[str] = []
    if len(base) >= 3 and base not in RESERVED_HANDLES:
        candidates.extend((base, f"{base[:21]}_{digest[:8]}"))
    candidates.append(f"pixo_{digest[:10]}")
    for candidate in candidates:
        if candidate not in occupied and candidate not in RESERVED_HANDLES:
            return candidate, False
    for length in range(11, 25):
        candidate = f"pixo_{digest[:length]}"[:30]
        if candidate not in occupied:
            return candidate, True
    raise RuntimeError(f"could not allocate handle for {user_id}")


def main() -> None:
    schema = inspect(engine)
    tables = set(schema.get_table_names())
    user_columns = {column["name"] for column in schema.get_columns("users")}
    handle_expression = "handle" if "handle" in user_columns else "NULL AS handle"
    purpose_expression = (
        "internal_purpose"
        if "internal_purpose" in user_columns
        else "NULL AS internal_purpose"
    )
    with engine.connect() as connection:
        users = connection.execute(text(
            "SELECT user_id, nickname, "
            f"{purpose_expression}, {handle_expression} "
            "FROM users ORDER BY created_at ASC, user_id ASC"
        )).mappings().all()
        existing_handles = [str(row["handle"]) for row in users if row["handle"]]
        occupied = set(existing_handles)
        if "creator_handle_aliases" in tables:
            occupied.update(str(value) for (value,) in connection.execute(
                text("SELECT handle FROM creator_handle_aliases")
            ).all())
        generated: list[dict[str, str]] = []
        excluded = 0
        conflicts = 0
        for user in users:
            if user["handle"]:
                continue
            if user["internal_purpose"] not in (None, "", "social_seed"):
                excluded += 1
                continue
            handle, used_extended_fallback = _candidate(
                str(user["nickname"] or ""), str(user["user_id"]), occupied
            )
            if used_extended_fallback:
                conflicts += 1
            occupied.add(handle)
            generated.append({"user_id": str(user["user_id"]), "handle": handle})

        duplicates = sum(count > 1 for count in Counter(existing_handles).values())
        print(json.dumps({
            "account_count": len(users),
            "generated_handle_count": len(generated),
            "conflict_fallback_count": conflicts,
            "excluded_account_count": excluded,
            "duplicate_count": duplicates,
            "sample": generated[:20],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
