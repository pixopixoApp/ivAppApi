#!/usr/bin/env python3
"""Automated social-seed likes for prelaunch acceptance.

Runs as a cron-driven, idempotent task inside the ivapp container. Each run:

1. Reads the "internal interaction preview" switch and **stops if disabled**.
2. Loads the visible video pool (same filter the recommendation pool builder
   uses) and buckets it by ``feed_weight`` (level 1..5).
3. Loads the social-seed account batch from the internal account API.
4. Distributes a daily quota of likes across videos using the *exposure
   weights* of each level, so that like counts track what the recommender
   actually surfaces.
5. Places those likes on a US-Eastern-majority + UK-secondary time-of-day
   curve (so ``created_at`` looks like real human behaviour and future seed
   comments line up with believable timestamps).
6. Executes them through the validated ``/internal/v1/social-seed`` like
   endpoint, honouring the platform rate limits, idempotently.

Design notes
------------
* Only the actions themselves go through the HTTP API so that all of the
  existing validation (visibility, blocking, rate limiting, seed counting)
  applies. Reading is done from the same process/DB the API uses.
* ``video_likes`` has a ``(video_id, user_id)`` unique constraint, so a like
  is idempotent: replaying a run never double counts.
* **No separate state file.** Daily/hourly budgets and already-liked pairs are
  derived directly from ``video_likes.created_at`` each run, so the task is
  stateless and safe to re-run at any cadence.
* Level exposure weights come from the recommender's olive-shaped supply
  (default ``2/3/5/6/4`` over levels 1..5). A per-video lifetime like target
  is drawn from a per-level band (10 ~ a few hundred), so higher-exposure
  levels accumulate more likes.

Usage
-----
    python scripts/social_seed_like.py --dry-run
    python scripts/social_seed_like.py                 # normal cron run
    python scripts/social_seed_like.py --daily-total 1200
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# --------------------------------------------------------------------------
# Tunable constants (deliberately not CLI flags; see docs/social-seed-automation.md)
# --------------------------------------------------------------------------

DEFAULT_BATCH_ID = "prelaunch-v1"

# Level exposure weights. The recommender supplies 2/3/5/6/4 items per request
# (levels 1..5), so a single level-N video is exposed roughly
# (count_N / pool_size_N) relative to the others. We fold the *target counts*
# into a stable per-level weight; the exact constants were chosen from the
# production pool sizes so like volume tracks exposure.
LEVEL_WEIGHTS: dict[int, float] = {1: 10.0, 2: 15.0, 3: 25.0, 4: 30.0, 5: 20.0}

# Per-video lifetime like target band by level ("10 ~ a few hundred").
# Low-exposure levels receive few likes; high-exposure levels receive many.
# A video's exact target is drawn uniformly from its band.
LEVEL_LIFETIME_TARGETS: dict[int, tuple[int, int]] = {
    1: (10, 40),
    2: (40, 90),
    3: (90, 180),
    4: (180, 260),
    5: (200, 300),
}

# Global clamp for a per-video lifetime target.
LIKES_PER_VIDEO_MIN = 10
LIKES_PER_VIDEO_MAX = 300

# Ramp: a video does not receive all of its lifetime likes at once; they are
# spread over this many days so growth looks organic.
RAMP_DAYS_MIN = 2
RAMP_DAYS_MAX = 5

# Daily total likes across the whole pool.
DAILY_TOTAL_MIN = 500
DAILY_TOTAL_MAX = 2000

# Per-account safety ceilings (platform hard limit is 120 events/hour shared
# with comments, so stay well below it).
MAX_LIKES_PER_ACCOUNT_HOUR = 100
MAX_LIKES_PER_ACCOUNT_DAY = 30

# Guard-rail: never let a single run do more than this many API calls.
MAX_LIKES_PER_RUN = 400

# Time-of-day curve. Primary audience is US Eastern; UK is a leading secondary
# peak (the UK day starts ~5h earlier). Weights are per local hour 0..23.
TIMEZONE_CURVES: dict[str, list[float]] = {
    "America/New_York": [
        1, 1, 1, 1, 1, 2,          # 00-05 deep night
        6, 9, 11, 16, 18, 18,      # 06-11 morning rise
        14, 13, 14, 18, 20, 20,    # 12-17 afternoon
        24, 26, 24, 18, 10, 5,     # 18-23 evening peak -> wind down
    ],
    "Europe/London": [
        1, 1, 1, 1, 1, 2,          # 00-05 deep night
        5, 8, 10, 13, 15, 15,      # 06-11 morning rise
        12, 12, 13, 15, 17, 17,    # 12-17 afternoon
        20, 22, 22, 16, 9, 4,      # 18-23 evening peak -> wind down
    ],
}
# Fraction of likes attributed to each timezone curve.
TIMEZONE_MIX: dict[str, float] = {
    "America/New_York": 0.72,
    "Europe/London": 0.28,
}

# Minimum spacing between two likes performed by the same cron run, to avoid
# hammering the API in a burst.
SLEEP_MIN_SECONDS = 0.15
SLEEP_MAX_SECONDS = 0.6

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "data" / "social-seed" / "like-config.json"


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class VideoTarget:
    video_id: str
    level: int
    weight: float
    lifetime_target: int


@dataclass
class SeedAccount:
    user_id: str
    nickname: str = ""
    avatar_url: str = ""


@dataclass
class LikePlanItem:
    video_id: str
    actor_user_id: str
    batch_id: str
    scheduled_at: datetime  # UTC


@dataclass
class RunStats:
    liked: int = 0
    skipped_duplicate: int = 0
    failed: int = 0
    rate_limited: int = 0
    not_found: int = 0
    forbidden: int = 0
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Configuration loading
# --------------------------------------------------------------------------


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------
# HTTP helpers (internal API, X-Publish-Key)
# --------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, message: str, *, status: int = 0):
        super().__init__(message)
        self.status = status


def _api(
    *,
    base_url: str,
    publish_key: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    data = None
    headers = {"X-Publish-Key": publish_key}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ApiError(f"HTTP {exc.code}: {detail[:300]}", status=exc.code) from exc
    except URLError as exc:
        raise ApiError(f"unreachable: {exc.reason}") from exc
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError("invalid JSON response") from exc
    if not isinstance(result, dict):
        raise ApiError("unexpected response shape")
    return result


# --------------------------------------------------------------------------
# Preview switch — the whole task is a no-op when preview is disabled
# --------------------------------------------------------------------------


def preview_is_enabled(*, base_url: str, publish_key: str) -> bool:
    result = _api(
        base_url=base_url,
        publish_key=publish_key,
        method="GET",
        path="/internal/v1/social-seed-preview",
    )
    return bool(result.get("enabled"))


# --------------------------------------------------------------------------
# Load visible video pool (same visibility filter as the recommender builder)
# --------------------------------------------------------------------------


_VISIBLE_SQL = (
    "SELECT id, feed_weight FROM published_videos "
    "WHERE is_deleted = 0 AND deleted_at IS NULL "
    "AND review_status = 'approved' "
    "AND distribution_enabled = 1 AND cdn_ready = 1 "
    "AND ("
    "  (content_type = 'runtime' AND runtime_spec IS NOT NULL "
    "   AND runtime_spec_version IS NOT NULL) "
    "  OR "
    "  (content_type = 'html' AND html_url IS NOT NULL AND bridge_version = 1)"
    ")"
)


def load_video_targets(db, *, rng: random.Random) -> list[VideoTarget]:
    """Return visible videos with an exposure-weighted lifetime like target."""
    from sqlalchemy import text

    rows = db.execute(text(_VISIBLE_SQL)).all()
    targets: list[VideoTarget] = []
    for video_id, raw_weight in rows:
        weight = int(raw_weight or 0)
        if weight <= 0:
            continue  # not in the recommendation pool
        level = weight if weight in LEVEL_WEIGHTS else 5
        low, high = LEVEL_LIFETIME_TARGETS[level]
        lifetime = rng.randint(low, high)
        targets.append(
            VideoTarget(
                video_id=str(video_id),
                level=level,
                weight=float(LEVEL_WEIGHTS[level]),
                lifetime_target=max(LIKES_PER_VIDEO_MIN, min(lifetime, LIKES_PER_VIDEO_MAX)),
            )
        )
    return targets


# --------------------------------------------------------------------------
# Load seed accounts (internal API, paginated)
# --------------------------------------------------------------------------


def load_seed_accounts(
    *, base_url: str, publish_key: str, batch_id: str
) -> list[SeedAccount]:
    accounts: list[SeedAccount] = []
    cursor: str | None = None
    while True:
        query = f"?batch_id={batch_id}&limit=200"
        if cursor:
            query += f"&cursor={cursor}"
        page = _api(
            base_url=base_url,
            publish_key=publish_key,
            method="GET",
            path=f"/internal/v1/social-seed/accounts{query}",
        )
        for item in page.get("items", []):
            if isinstance(item, dict) and item.get("user_id"):
                accounts.append(
                    SeedAccount(
                        user_id=str(item["user_id"]),
                        nickname=str(item.get("nickname") or ""),
                        avatar_url=str(item.get("avatar_url") or ""),
                    )
                )
        cursor = page.get("next_cursor")
        if not page.get("has_more") or not cursor:
            break
    return accounts


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Scheduling: catch up to a daily quota spread over the time-of-day curves
# --------------------------------------------------------------------------


def cumulative_target_by_now(
    *,
    daily_total: int,
    now_utc: datetime,
) -> int:
    """How many of today's likes *should* have happened by ``now_utc``.

    Combines both timezone curves into a single per-UTC-hour expected rate and
    returns the cumulative expected count (rounded down). Because it only
    depends on the calendar day and ``now_utc``, repeated cron runs agree on
    the target, which keeps the task idempotent without any stored schedule.
    """
    if daily_total <= 0:
        return 0
    # Aggregate the two local curves into weights per UTC hour for today.
    hour_weights = _utc_hour_weights(now_utc)
    total_weight = sum(hour_weights)
    if total_weight <= 0:
        return 0

    now_hour = now_utc.astimezone(timezone.utc).hour
    minutes_into_hour = now_utc.astimezone(timezone.utc).minute / 60.0
    accrued = sum(hour_weights[:now_hour])
    accrued += hour_weights[now_hour] * minutes_into_hour
    return int(daily_total * accrued / total_weight)


def _utc_hour_weights(now_utc: datetime) -> list[float]:
    """Fold the local timezone curves onto the 24 UTC hours of today."""
    weights = [0.0] * 24
    for tz, mix in TIMEZONE_MIX.items():
        if tz not in TIMEZONE_CURVES:
            continue
        tzinfo = ZoneInfo(tz)
        curve = TIMEZONE_CURVES[tz]
        for utc_hour in range(24):
            # Map this UTC hour to the tz's local hour using today's offset.
            sample = now_utc.astimezone(timezone.utc).replace(
                hour=utc_hour, minute=0, second=0, microsecond=0
            )
            local_hour = sample.astimezone(tzinfo).hour
            weights[utc_hour] += mix * curve[local_hour]
    return weights


def plan_due_likes(
    *,
    targets: list[VideoTarget],
    count: int,
    now_utc: datetime,
    rng: random.Random,
) -> list[LikePlanItem]:
    """Plan ``count`` likes to execute *now* (catch-up to the day's curve).

    Times are stamped at ``now_utc`` with a small random jitter backwards so
    that a burst from a single run does not share one identical timestamp.
    """
    if count <= 0 or not targets:
        return []
    weighted_pool: list[VideoTarget] = []
    per_video_today_cap: dict[str, int] = {}
    for target in targets:
        repeats = max(1, int(round(target.weight / 5)))
        weighted_pool.extend([target] * repeats)
        per_video_today_cap[target.video_id] = max(
            1, target.lifetime_target // max(1, RAMP_DAYS_MAX)
        )
    today_count: dict[str, int] = {}
    plan: list[LikePlanItem] = []
    for _ in range(count):
        pool = [
            t
            for t in weighted_pool
            if today_count.get(t.video_id, 0) < per_video_today_cap.get(t.video_id, 1)
        ]
        if not pool:
            break
        target = rng.choice(pool)
        today_count[target.video_id] = today_count.get(target.video_id, 0) + 1
        jitter = timedelta(seconds=rng.randint(0, 240))
        plan.append(
            LikePlanItem(
                video_id=target.video_id,
                actor_user_id="",
                batch_id=DEFAULT_BATCH_ID,
                scheduled_at=now_utc - jitter,
            )
        )
    plan.sort(key=lambda item: item.scheduled_at)
    return plan


# --------------------------------------------------------------------------
# Account assignment with per-account rate limits
# --------------------------------------------------------------------------


def assign_accounts(
    *,
    plan: list[LikePlanItem],
    accounts: list[SeedAccount],
    rng: random.Random,
    seed_today: dict[str, int] | None = None,
    seed_this_hour: dict[str, int] | None = None,
) -> list[LikePlanItem]:
    """Assign a distinct account to each like slot respecting rate limits.

    ``seed_today`` / ``seed_this_hour`` carry the counters already present in
    the database for today/this hour, so daily/hourly ceilings hold globally
    across cron runs.
    """
    if not accounts:
        return []
    today = seed_today or {}
    hour = seed_this_hour or {}
    assigned: list[LikePlanItem] = []

    # Shuffle once; then walk with a rotating pointer for balance.
    order = list(accounts)
    rng.shuffle(order)
    pointer = 0

    for item in plan:
        chosen: SeedAccount | None = None
        for _ in range(len(order)):
            candidate = order[pointer % len(order)]
            pointer += 1
            if today.get(candidate.user_id, 0) >= MAX_LIKES_PER_ACCOUNT_DAY:
                continue
            if hour.get(candidate.user_id, 0) >= MAX_LIKES_PER_ACCOUNT_HOUR:
                continue
            chosen = candidate
            break
        if chosen is None:
            # Every account is at its limit; stop scheduling for now.
            break
        today[chosen.user_id] = today.get(chosen.user_id, 0) + 1
        hour[chosen.user_id] = hour.get(chosen.user_id, 0) + 1
        item.actor_user_id = chosen.user_id
        assigned.append(item)
    return assigned


# --------------------------------------------------------------------------
# State — derived entirely from the database (stateless, resumable, idempotent)
# --------------------------------------------------------------------------


@dataclass
class LikeState:
    """Seed-like counters derived from ``video_likes`` rows created so far."""

    today_total: int
    per_account_today: dict[str, int]
    per_account_hour: dict[str, int]
    done_keys: set[str]


def load_like_state(db, *, now_utc: datetime, batch_id: str) -> LikeState:
    """Read today's/this hour's seed likes straight from the DB.

    The database is the single source of truth: ``video_likes`` rows carry a
    ``(video_id, user_id)`` unique constraint (idempotent) and a real
    ``created_at`` timestamp, so we never need a separate manifest file.
    """
    from sqlalchemy import text

    day_start = now_utc.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    hour_start = now_utc.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )

    today_total = db.execute(
        text(
            "SELECT COUNT(*) FROM video_likes "
            "WHERE is_seed = 1 AND created_at >= :day_start"
        ),
        {"day_start": day_start},
    ).scalar() or 0

    per_account_today = {
        str(uid): int(cnt)
        for uid, cnt in db.execute(
            text(
                "SELECT user_id, COUNT(*) FROM video_likes "
                "WHERE is_seed = 1 AND created_at >= :day_start "
                "GROUP BY user_id"
            ),
            {"day_start": day_start},
        ).all()
    }
    per_account_hour = {
        str(uid): int(cnt)
        for uid, cnt in db.execute(
            text(
                "SELECT user_id, COUNT(*) FROM video_likes "
                "WHERE is_seed = 1 AND created_at >= :hour_start "
                "GROUP BY user_id"
            ),
            {"hour_start": hour_start},
        ).all()
    }

    # Already-liked (video, account) pairs for this batch — lets the scheduler
    # skip work the DB would reject anyway.
    done_keys = {
        f"{vid}:{uid}"
        for vid, uid in db.execute(
            text(
                "SELECT l.video_id, l.user_id FROM video_likes l "
                "JOIN users u ON u.user_id = l.user_id "
                "WHERE l.is_seed = 1 AND u.internal_batch = :batch_id"
            ),
            {"batch_id": batch_id},
        ).all()
    }
    return LikeState(
        today_total=int(today_total),
        per_account_today=per_account_today,
        per_account_hour=per_account_hour,
        done_keys=done_keys,
    )


def like_key(video_id: str, actor_user_id: str) -> str:
    return f"{video_id}:{actor_user_id}"


# --------------------------------------------------------------------------
# Main orchestration
# --------------------------------------------------------------------------


def _resolve_base_url() -> str:
    explicit = os.getenv("PIXO_BACKEND_URL", "").strip()
    if explicit:
        return explicit
    # Default: talk to the local ivapp process (script runs inside the container).
    port = os.getenv("PORT", "8100")
    return f"http://127.0.0.1:{port}"


def _resolve_publish_key() -> str:
    key = os.getenv("PIXO_PUBLISH_KEY", "").strip()
    if key:
        return key
    try:
        from app.config import get_settings

        return str(get_settings().publish_key).strip()
    except Exception:  # noqa: BLE001
        return ""


def _iso_week_key(now_utc: datetime) -> str:
    return now_utc.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _hour_key(now_utc: datetime) -> str:
    return now_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def run(
    *,
    dry_run: bool,
    daily_total: int,
    batch_id: str,
    config: dict[str, Any],
    now_utc: datetime | None = None,
    rng: random.Random | None = None,
    force: bool = False,
) -> RunStats:
    now_utc = now_utc or datetime.now(timezone.utc)
    rng = rng or random.Random()
    stats = RunStats()

    base_url = _resolve_base_url()
    publish_key = _resolve_publish_key()
    if not publish_key:
        raise SystemExit("social-seed-like: PIXO_PUBLISH_KEY is required")

    # --- 1. preview gate ---
    if not preview_is_enabled(base_url=base_url, publish_key=publish_key):
        print(json.dumps({"status": "skipped", "reason": "preview disabled"}))
        return stats

    # --- 2. load pool + accounts + today's DB-derived state ---
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        targets = load_video_targets(db, rng=rng)
        state = load_like_state(db, now_utc=now_utc, batch_id=batch_id)
    finally:
        db.close()
    accounts = load_seed_accounts(
        base_url=base_url, publish_key=publish_key, batch_id=batch_id
    )
    if not targets or not accounts:
        print(
            json.dumps(
                {
                    "status": "noop",
                    "videos": len(targets),
                    "accounts": len(accounts),
                }
            )
        )
        return stats

    # --- 3. catch up to the day's expected curve ---
    day = _iso_week_key(now_utc)
    done_keys = set(state.done_keys)
    # How many likes *should* have happened by now, capped by the daily budget.
    curve_target = min(daily_total, cumulative_target_by_now(
        daily_total=daily_total, now_utc=now_utc
    ))
    if force:
        curve_target = daily_total
    due_count = max(0, curve_target - state.today_total)
    due_count = min(due_count, MAX_LIKES_PER_RUN)

    plan = plan_due_likes(
        targets=targets,
        count=due_count,
        now_utc=now_utc,
        rng=rng,
    )
    plan = assign_accounts(
        plan=plan,
        accounts=accounts,
        rng=rng,
        seed_today=dict(state.per_account_today),
        seed_this_hour=dict(state.per_account_hour),
    )

    if dry_run:
        summary = _dry_run_summary(plan=plan, due=plan, targets=targets, accounts=accounts)
        summary["already_today"] = state.today_total
        summary["curve_target"] = curve_target
        summary["due_now"] = len(plan)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return stats

    # --- 4. execute ---
    for item in plan:
        key = like_key(item.video_id, item.actor_user_id)
        if key in done_keys:
            stats.skipped_duplicate += 1
            continue
        try:
            _api(
                base_url=base_url,
                publish_key=publish_key,
                method="PUT",
                path=f"/internal/v1/social-seed/videos/{item.video_id}/like",
                body={"actor_user_id": item.actor_user_id, "batch_id": batch_id},
            )
        except ApiError as exc:
            if exc.status == 429:
                stats.rate_limited += 1
            elif exc.status == 404:
                stats.not_found += 1
            elif exc.status == 403:
                stats.forbidden += 1
            else:
                stats.failed += 1
            if len(stats.errors) < 20:
                stats.errors.append(f"{item.video_id}/{item.actor_user_id}: {exc}")
            continue
        stats.liked += 1
        done_keys.add(key)
        sleep_for = rng.uniform(SLEEP_MIN_SECONDS, SLEEP_MAX_SECONDS)
        time.sleep(sleep_for)

    result = {
        "status": "ok",
        "day": day,
        "liked": stats.liked,
        "skipped_duplicate": stats.skipped_duplicate,
        "rate_limited": stats.rate_limited,
        "not_found": stats.not_found,
        "forbidden": stats.forbidden,
        "failed": stats.failed,
        "today_total": state.today_total + stats.liked,
        "errors": stats.errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return stats


def _dry_run_summary(
    *,
    plan: list[LikePlanItem],
    due: list[LikePlanItem],
    targets: list[VideoTarget],
    accounts: list[SeedAccount],
) -> dict[str, Any]:
    by_level: dict[int, int] = {}
    level_of = {t.video_id: t.level for t in targets}
    for item in plan:
        lv = level_of.get(item.video_id, 0)
        by_level[lv] = by_level.get(lv, 0) + 1
    distributions: dict[str, dict[int, int]] = {"video": {}, "account": {}}
    for item in plan:
        distributions["video"][item.video_id] = (
            distributions["video"].get(item.video_id, 0) + 1
        )
        distributions["account"][item.actor_user_id] = (
            distributions["account"].get(item.actor_user_id, 0) + 1
        )
    account_load = Counter(distributions["account"])
    return {
        "status": "dry-run",
        "videos": len(targets),
        "accounts": len(accounts),
        "planned_today": len(plan),
        "due_now": len(due),
        "planned_by_level": {str(k): by_level[k] for k in sorted(by_level)},
        "distinct_videos": len(distributions["video"]),
        "accounts_used": len(account_load),
        "max_likes_per_account": max(account_load.values()) if account_load else 0,
        "note": "no writes performed",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Automated social-seed likes.")
    parser.add_argument("--batch-id", default=DEFAULT_BATCH_ID)
    parser.add_argument(
        "--daily-total",
        type=int,
        default=None,
        help="Override today's total like budget (default from config or 500-2000).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Execute all of today's plan now, ignoring the time-of-day gate "
        "(use for one-off backfill/testing; still honours all rate limits).",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument(
        "--seed", type=int, default=None, help="Random seed (reproducible planning)."
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    daily_total = args.daily_total
    if daily_total is None:
        daily_total = int(
            config.get(
                "daily_total",
                random.Random().randint(DAILY_TOTAL_MIN, DAILY_TOTAL_MAX),
            )
        )
    daily_total = max(0, min(daily_total, DAILY_TOTAL_MAX))

    rng = random.Random(args.seed) if args.seed is not None else random.Random()

    stats = run(
        dry_run=args.dry_run,
        daily_total=daily_total,
        batch_id=args.batch_id,
        config=config,
        rng=rng,
        force=args.force,
    )
    return 0 if stats.failed == 0 or stats.liked > 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"social-seed-like: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
