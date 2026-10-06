#!/usr/bin/env python3
"""Automated social-seed likes + comments for prelaunch acceptance.

Runs as a cron-driven, idempotent task inside the ivapp container. Each run:

1. Reads the "internal interaction preview" switch and **stops if disabled**
   (both likes and comments are a no-op when preview is off).
2. Loads the visible video pool (same filter the recommendation pool builder
   uses) with each video's quality level (``feed_weight`` 1..5) and its
   grounded comment context (title, description, interaction summary/types/hints).
3. Loads the social-seed account batch from the internal account API.
4. **Likes**: distributes a daily quota across videos by level exposure weight,
   placed on a US-Eastern-majority + UK-secondary time-of-day curve.
5. **Comments**: rarer than likes (random ~10:1 ratio, tilted by quality level),
   generated per video from its interaction gameplay via an LLM (persona-driven),
   with a per-interaction template fallback when the LLM is unavailable.
6. Executes both through the validated ``/internal/v1/social-seed`` endpoints,
   honouring the platform rate limits, idempotently.

Design notes
------------
* Only the actions themselves go through the HTTP API so that all of the
  existing validation (visibility, blocking, rate limiting, seed counting)
  applies. Reading is done from the same process/DB the API uses.
* ``video_likes``/``comments`` have unique constraints, so actions are
  idempotent: replaying a run never double counts.
* **No separate state file.** Daily/hourly budgets and already-done pairs are
  derived directly from the DB each run, so the task is stateless and safe to
  re-run at any cadence.
* Comment text is generated from the video's own gameplay so it stays grounded
  and varied; accounts carry a fixed persona for a consistent voice.
* LLM config is read from the environment (SOCIAL_SEED_LLM_BASE_URL/API_KEY/
  MODEL); when unset the task falls back to per-interaction templates.

Usage
-----
    python scripts/social_seed_like.py --dry-run
    python scripts/social_seed_like.py                 # normal cron run
    python scripts/social_seed_like.py --daily-total 1200
    python scripts/social_seed_like.py --comments-only
"""

from __future__ import annotations

import argparse
import hashlib
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

# --------------------------------------------------------------------------
# Comments — merged into the same task (like + comment).
# --------------------------------------------------------------------------

# Overall like:comment ratio. Comments are far rarer than likes on a real app.
# We do NOT apply a fixed ratio; each video's comment target is its lifetime
# like target times a random ratio in this band (mean ~0.10), then scaled by
# the level quality factor below.
COMMENT_RATIO_MIN = 0.06
COMMENT_RATIO_MAX = 0.16

# Quality tilt: higher-exposure (higher feed_weight) videos attract more
# comments per like; low-quality videos can end up with zero comments.
COMMENT_LEVEL_FACTOR: dict[int, float] = {1: 0.35, 2: 0.7, 3: 1.0, 4: 1.5, 5: 2.0}

# A video never receives more than this many seed comments in total, and low
# target videos may receive none.
COMMENT_PER_VIDEO_MAX = 30
COMMENT_PER_VIDEO_MIN_ELIGIBLE = 1

# Per-account comment ceilings (platform hard limit is 30/hour and >=5s spacing,
# shared with likes in the rate table, so stay conservative).
MAX_COMMENTS_PER_ACCOUNT_HOUR = 20
MAX_COMMENTS_PER_ACCOUNT_DAY = 8

# Cap number of comments executed per run.
MAX_COMMENTS_PER_RUN = 60

# Comment text rules.
COMMENT_MAX_CHARS = 200

# LLM configuration is read from the environment so no key lives in the repo.
# When unset, comment generation falls back to the built-in template library.
LLM_ENV_BASE_URL = "SOCIAL_SEED_LLM_BASE_URL"
LLM_ENV_API_KEY = "SOCIAL_SEED_LLM_API_KEY"
LLM_ENV_MODEL = "SOCIAL_SEED_LLM_MODEL"
LLM_DEFAULT_MODEL = "qwen3.7-plus"
LLM_TIMEOUT_SECONDS = 40.0
LLM_TEMPERATURE = 0.9

# Commenter personas. Each seed account is deterministically bound to one, so
# its voice stays consistent across videos.
PERSONAS: dict[str, str] = {
    "curious": "curious and asks short genuine questions about how the effect works",
    "hype": "easily excited, uses enthusiastic language and occasional emoji",
    "chill": "casual and brief, often just a few words, lowercase, low effort",
    "techy": "observes technical detail: timing, tracking, responsiveness, polish",
    "emotional": "reacts with feelings, often mentions how it made them feel",
}

# Built-in fallback comment templates, grouped by interaction family. Used only
# when the LLM is unavailable; kept per-interaction so fallbacks still match the
# gameplay instead of being generic.
FALLBACK_COMMENTS: dict[str, tuple[str, ...]] = {
    "tap": (
        "the tap timing is so satisfying",
        "kept tapping just to see what happens",
        "didn't expect that after the tap lol",
        "ok the tap part got me",
    ),
    "swipe": (
        "the swipe felt so smooth",
        "swiping back and forth is weirdly fun",
        "didn't know swiping would do that",
        "the swipe transition is clean",
    ),
    "hold": (
        "holding it down actually worked, nice",
        "love when holding reveals something",
        "the press and hold is such a good touch",
    ),
    "mic": (
        "blew into my mic not expecting it to work lol",
        "the mic detection is surprisingly accurate",
        "my mic picked it up first try",
    ),
    "camera": (
        "the camera tracking is actually wild",
        "moved around and it tracked me, cool",
        "the face tracking works really well",
    ),
    "tilt": (
        "tilting my phone actually changed things, fun",
        "the tilt controls are so smooth",
    ),
    "shake": (
        "shaking my phone did something, love it",
        "the shake reaction is great",
    ),
    "draw": (
        "drawing on the screen is so fun",
        "tracing it out actually worked",
        "the drawing part is really satisfying",
    ),
    "generic": (
        "so fun", "need more like this", "this is great", "love these",
        "kept replaying it", "so creative",
    ),
}

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
    # Comment context: grounded features used for comment generation.
    title: str = ""
    description: str = ""
    interaction_summary: str = ""
    interaction_types: tuple[str, ...] = ()
    interaction_hints: tuple[str, ...] = ()
    comment_target: int = 0
    # Raw interaction family (tap/swipe/hold/mic/camera/tilt/shake/generic).
    interaction_family: str = "generic"


@dataclass
class SeedAccount:
    user_id: str
    nickname: str = ""
    avatar_url: str = ""
    persona: str = "chill"


@dataclass
class LikePlanItem:
    video_id: str
    actor_user_id: str
    batch_id: str
    scheduled_at: datetime  # UTC


@dataclass
class CommentPlanItem:
    video_id: str
    actor_user_id: str
    batch_id: str
    body: str
    idempotency_key: str
    scheduled_at: datetime  # UTC


@dataclass
class RunStats:
    liked: int = 0
    commented: int = 0
    skipped_duplicate: int = 0
    failed: int = 0
    rate_limited: int = 0
    not_found: int = 0
    forbidden: int = 0
    comment_failed: int = 0
    comment_rate_limited: int = 0
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
    "SELECT p.id, p.feed_weight, p.title, p.description, p.timeline, "
    "       s.interaction_types, s.interaction_summary "
    "FROM published_videos p "
    "LEFT JOIN published_video_seo s ON s.video_id = p.id "
    "WHERE p.is_deleted = 0 AND p.deleted_at IS NULL "
    "AND p.review_status = 'approved' "
    "AND p.distribution_enabled = 1 AND p.cdn_ready = 1 "
    "AND ("
    "  (p.content_type = 'runtime' AND p.runtime_spec IS NOT NULL "
    "   AND p.runtime_spec_version IS NOT NULL) "
    "  OR "
    "  (p.content_type = 'html' AND p.html_url IS NOT NULL AND p.bridge_version = 1)"
    ")"
)


def _interaction_family(types: list[str], hints: list[str]) -> str:
    """Map raw interaction types/hints to a coarse family for fallback text."""
    joined = " ".join([*(types or []), *(hints or [])]).lower()
    if any(k in joined for k in ("mic", "blow", "clap", "sound", "volume", "voice")):
        return "mic"
    if any(k in joined for k in ("camera", "face", "smile", "vision", "motion")):
        return "camera"
    if any(k in joined for k in ("tilt", "rotate", "turn")):
        return "tilt"
    if any(k in joined for k in ("shake", "grab", "wave")):
        return "shake"
    if any(k in joined for k in ("hold", "press")):
        return "hold"
    if any(k in joined for k in ("swipe", "drag", "scrub")):
        return "swipe"
    if any(k in joined for k in ("tap",)):
        return "tap"
    if any(k in joined for k in ("erase", "draw", "pinch", "circle")):
        return "draw"
    return "generic"


def _timeline_hints(timeline: Any) -> list[str]:
    import json

    tl = timeline
    if isinstance(tl, str):
        try:
            tl = json.loads(tl)
        except (ValueError, TypeError):
            tl = None
    hints: list[str] = []
    for item in (tl or {}).get("interactions", []) or []:
        if isinstance(item, dict):
            hint = str(item.get("hint") or "").strip()
            if hint:
                hints.append(hint)
    return hints


def _parse_interaction_types(value: Any) -> list[str]:
    """``interaction_types`` is a JSON column that may arrive as str or list."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    if isinstance(value, (list, tuple)):
        return [str(t) for t in value if t is not None]
    return []


def load_video_targets(db, *, rng: random.Random) -> list[VideoTarget]:
    """Return visible videos with like + comment targets and comment context."""
    from sqlalchemy import text

    rows = db.execute(text(_VISIBLE_SQL)).all()
    targets: list[VideoTarget] = []
    for row in rows:
        video_id, raw_weight, title, description, timeline, types, summary = row
        weight = int(raw_weight or 0)
        if weight <= 0:
            continue  # not in the recommendation pool
        level = weight if weight in LEVEL_WEIGHTS else 5
        low, high = LEVEL_LIFETIME_TARGETS[level]
        lifetime = rng.randint(low, high)
        lifetime = max(LIKES_PER_VIDEO_MIN, min(lifetime, LIKES_PER_VIDEO_MAX))
        # Comment target: proportional to like target, tilted by quality level.
        ratio = rng.uniform(COMMENT_RATIO_MIN, COMMENT_RATIO_MAX)
        comment_target = int(round(lifetime * ratio * COMMENT_LEVEL_FACTOR[level]))
        comment_target = max(0, min(comment_target, COMMENT_PER_VIDEO_MAX))
        hints = _timeline_hints(timeline)
        type_list = _parse_interaction_types(types)
        targets.append(
            VideoTarget(
                video_id=str(video_id),
                level=level,
                weight=float(LEVEL_WEIGHTS[level]),
                lifetime_target=lifetime,
                title=str(title or ""),
                description=str(description or ""),
                interaction_summary=str(summary or ""),
                interaction_types=tuple(type_list),
                interaction_hints=tuple(hints),
                comment_target=comment_target,
                interaction_family=_interaction_family(type_list, hints),
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
                uid = str(item["user_id"])
                accounts.append(
                    SeedAccount(
                        user_id=uid,
                        nickname=str(item.get("nickname") or ""),
                        avatar_url=str(item.get("avatar_url") or ""),
                        persona=persona_for_account(uid),
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
# Comments — persona, DB-derived state, LLM generation, template fallback
# --------------------------------------------------------------------------


def persona_for_account(user_id: str) -> str:
    """Deterministically bind a seed account to one persona."""
    names = sorted(PERSONAS)
    digest = hashlib.sha256(user_id.encode("utf-8")).digest()
    return names[digest[0] % len(names)]


@dataclass
class CommentState:
    """Seed-comment counters derived from the ``comments`` table."""

    total_today: int
    per_account_today: dict[str, int]
    per_account_hour: dict[str, int]
    per_video_total: dict[str, int]
    used_bodies_by_video: dict[str, set[str]]
    used_accounts_by_video: dict[str, set[str]]


def load_comment_state(db, *, now_utc: datetime, batch_id: str) -> CommentState:
    from sqlalchemy import text

    day_start = now_utc.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    hour_start = now_utc.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    total_today = db.execute(
        text(
            "SELECT COUNT(*) FROM comments "
            "WHERE is_seed = 1 AND deleted_at IS NULL AND created_at >= :s"
        ),
        {"s": day_start},
    ).scalar() or 0
    per_account_today = {
        str(u): int(c)
        for u, c in db.execute(
            text(
                "SELECT author_user_id, COUNT(*) FROM comments "
                "WHERE is_seed = 1 AND deleted_at IS NULL AND created_at >= :s "
                "GROUP BY author_user_id"
            ),
            {"s": day_start},
        ).all()
    }
    per_account_hour = {
        str(u): int(c)
        for u, c in db.execute(
            text(
                "SELECT author_user_id, COUNT(*) FROM comments "
                "WHERE is_seed = 1 AND deleted_at IS NULL AND created_at >= :s "
                "GROUP BY author_user_id"
            ),
            {"s": hour_start},
        ).all()
    }
    per_video_total = {
        str(v): int(c)
        for v, c in db.execute(
            text(
                "SELECT video_id, COUNT(*) FROM comments "
                "WHERE is_seed = 1 AND deleted_at IS NULL GROUP BY video_id"
            )
        ).all()
    }
    used_bodies_by_video: dict[str, set[str]] = {}
    used_accounts_by_video: dict[str, set[str]] = {}
    for vid, author, body in db.execute(
        text(
            "SELECT video_id, author_user_id, body FROM comments "
            "WHERE is_seed = 1 AND deleted_at IS NULL"
        )
    ).all():
        used_bodies_by_video.setdefault(str(vid), set()).add(str(body))
        used_accounts_by_video.setdefault(str(vid), set()).add(str(author))
    return CommentState(
        total_today=int(total_today),
        per_account_today=per_account_today,
        per_account_hour=per_account_hour,
        per_video_total=per_video_total,
        used_bodies_by_video=used_bodies_by_video,
        used_accounts_by_video=used_accounts_by_video,
    )


def _llm_config() -> tuple[str, str, str]:
    return (
        os.getenv(LLM_ENV_BASE_URL, "").strip(),
        os.getenv(LLM_ENV_API_KEY, "").strip(),
        os.getenv(LLM_ENV_MODEL, LLM_DEFAULT_MODEL).strip() or LLM_DEFAULT_MODEL,
    )


def llm_available() -> bool:
    base, key, _ = _llm_config()
    return bool(base and key)


def generate_comment_llm(
    *, target: VideoTarget, persona: str, existing_bodies: set[str], rng: random.Random
) -> str | None:
    """Ask the LLM for one short grounded English comment. None on failure."""
    base, key, model = _llm_config()
    if not base or not key:
        return None
    import httpx

    avoid = "; ".join(sorted(existing_bodies))[:600] or "(none)"
    rules = [
        "Output ONE short English comment only, no quotes, no explanation.",
        f"Maximum {COMMENT_MAX_CHARS} characters.",
        "Sound like a real viewer, casual, not an ad, no hashtags, no links.",
        "Ground it in the described interaction; do not invent story events, people, brands or outcomes.",
        f"Voice/persona: {PERSONAS.get(persona, PERSONAS['chill'])}.",
        "Vary length, casing and emoji naturally; do not always end with punctuation.",
        f"Do NOT reuse or closely paraphrase any of these existing comments: {avoid}.",
    ]
    payload = {
        "task": "Write one social comment for this Pixopixo interactive video.",
        "content": {
            "title": target.title,
            "description": target.description[:600],
            "interaction_summary": target.interaction_summary[:600],
            "interaction_types": list(target.interaction_types),
            "interaction_hints": list(target.interaction_hints)[:4],
        },
        "rules": rules,
    }
    try:
        with httpx.Client(timeout=LLM_TIMEOUT_SECONDS, trust_env=False) as client:
            resp = client.post(
                base.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "temperature": LLM_TEMPERATURE,
                    "max_tokens": 80,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You write short, natural viewer comments. Output only the comment text.",
                        },
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                },
            )
            resp.raise_for_status()
            body = resp.json()
            text = str(body["choices"][0]["message"]["content"] or "").strip()
    except Exception:  # noqa: BLE001 - fall back to templates on any LLM error
        return None
    return _clean_comment(text)


def _clean_comment(text: str) -> str | None:
    text = text.strip().strip('"').strip("'").strip()
    # Drop a leading "Comment:" style prefix the model may add.
    for prefix in ("comment:", "comment -", "comment —"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip()
    # Keep first line only.
    text = text.splitlines()[0].strip() if text else ""
    if not text or len(text) > COMMENT_MAX_CHARS:
        return None
    return text


def fallback_comment(
    *,
    target: VideoTarget,
    existing_bodies: set[str],
    rng: random.Random,
    globally_used: set[str] | None = None,
) -> str:
    pool = FALLBACK_COMMENTS.get(target.interaction_family) or FALLBACK_COMMENTS["generic"]
    gused = globally_used or set()
    # Prefer: family pool unused per-video AND globally; then relax constraints.
    candidates = [c for c in pool if c not in existing_bodies and c not in gused]
    if not candidates:
        candidates = [c for c in pool if c not in existing_bodies]
    if not candidates:
        candidates = [c for c in pool if c not in gused]
    if not candidates:
        candidates = list(pool)
    return rng.choice(candidates)


def plan_comments(
    *,
    targets: list[VideoTarget],
    accounts: list[SeedAccount],
    state: CommentState,
    rng: random.Random,
    max_comments: int,
    now_utc: datetime,
) -> list[CommentPlanItem]:
    """Decide which videos still need comments and by/with what.

    ``max_comments`` is the per-run ceiling (already the catch-up delta against
    the day's comment curve, computed by the caller). A comment's body is
    generated lazily during execution (LLM or fallback), so this only selects
    (video, account) pairs and their idempotency keys.
    """
    if max_comments <= 0 or not accounts:
        return []

    # Candidate videos that still have comment headroom, weighted by level.
    weighted: list[VideoTarget] = []
    for t in targets:
        remaining = t.comment_target - state.per_video_total.get(t.video_id, 0)
        if remaining <= 0:
            continue
        repeats = max(1, int(round(t.weight / 5)))
        weighted.extend([t] * repeats)
    if not weighted:
        return []

    acc_today = dict(state.per_account_today)
    acc_hour = dict(state.per_account_hour)
    used_video_accounts = {k: set(v) for k, v in state.used_accounts_by_video.items()}
    today_count = dict(state.per_video_total)

    plan: list[CommentPlanItem] = []
    # Build a rotating account order for balance.
    order = list(accounts)
    rng.shuffle(order)
    pointer = 0
    attempts = 0
    max_attempts = max_comments * 8
    while len(plan) < max_comments and attempts < max_attempts:
        attempts += 1
        target = rng.choice(weighted)
        remaining = target.comment_target - today_count.get(target.video_id, 0)
        if remaining <= 0:
            continue
        # Pick an account that has not commented this video and is under limits.
        chosen: SeedAccount | None = None
        for _ in range(len(order)):
            cand = order[pointer % len(order)]
            pointer += 1
            if cand.user_id in used_video_accounts.get(target.video_id, set()):
                continue
            if acc_today.get(cand.user_id, 0) >= MAX_COMMENTS_PER_ACCOUNT_DAY:
                continue
            if acc_hour.get(cand.user_id, 0) >= MAX_COMMENTS_PER_ACCOUNT_HOUR:
                continue
            chosen = cand
            break
        if chosen is None:
            continue
        today_count[target.video_id] = today_count.get(target.video_id, 0) + 1
        acc_today[chosen.user_id] = acc_today.get(chosen.user_id, 0) + 1
        acc_hour[chosen.user_id] = acc_hour.get(chosen.user_id, 0) + 1
        used_video_accounts.setdefault(target.video_id, set()).add(chosen.user_id)
        key = f"seed-comment:{DEFAULT_BATCH_ID}:{target.video_id}:{chosen.user_id}"
        plan.append(
            CommentPlanItem(
                video_id=target.video_id,
                actor_user_id=chosen.user_id,
                batch_id=DEFAULT_BATCH_ID,
                body="",  # filled at execution time
                idempotency_key=key,
                scheduled_at=now_utc,
            )
        )
    return plan


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
    enable_likes: bool = True,
    enable_comments: bool = True,
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
        comment_state = load_comment_state(db, now_utc=now_utc, batch_id=batch_id)
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
    account_by_id = {a.user_id: a for a in accounts}
    target_by_id = {t.video_id: t for t in targets}

    # --- 3. catch up to the day's expected like curve ---
    day = _iso_week_key(now_utc)
    done_keys = set(state.done_keys)
    curve_target = min(daily_total, cumulative_target_by_now(
        daily_total=daily_total, now_utc=now_utc
    ))
    if force:
        curve_target = daily_total
    due_count = max(0, curve_target - state.today_total)
    due_count = min(due_count, MAX_LIKES_PER_RUN)

    if not enable_likes:
        due_count = 0
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

    # --- 3b. plan comments (independent catch-up; quality-tilted targets) ---
    # Daily comment budget derives from the like budget at ~10:1, but is also
    # clamped so it never exceeds the pool's total comment demand.
    comment_budget = int(daily_total * rng.uniform(COMMENT_RATIO_MIN, COMMENT_RATIO_MAX))
    comment_budget = min(comment_budget, MAX_COMMENTS_PER_RUN * 8)
    if force:
        comment_due = MAX_COMMENTS_PER_RUN
    else:
        comment_curve_target = min(
            comment_budget,
            cumulative_target_by_now(daily_total=comment_budget, now_utc=now_utc),
        )
        comment_due = max(0, comment_curve_target - comment_state.total_today)
        comment_due = min(comment_due, MAX_COMMENTS_PER_RUN)
    comment_plan = (
        plan_comments(
            targets=targets,
            accounts=accounts,
            state=comment_state,
            rng=rng,
            max_comments=comment_due,
            now_utc=now_utc,
        )
        if enable_comments
        else []
    )

    if dry_run:
        summary = _dry_run_summary(plan=plan, due=plan, targets=targets, accounts=accounts)
        summary["already_today"] = state.today_total
        summary["curve_target"] = curve_target
        summary["due_now"] = len(plan)
        summary["comments_planned"] = len(comment_plan)
        summary["comments_already_today"] = comment_state.total_today
        summary["llm_available"] = llm_available()
        summary["comment_target_total"] = sum(t.comment_target for t in targets)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return stats

    # --- 4. execute likes ---
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
        time.sleep(rng.uniform(SLEEP_MIN_SECONDS, SLEEP_MAX_SECONDS))

    # --- 5. execute comments (generate body, then post) ---
    globally_used_bodies: set[str] = set()
    for item in comment_plan:
        target = target_by_id.get(item.video_id)
        account = account_by_id.get(item.actor_user_id)
        if target is None or account is None:
            continue
        existing_bodies = comment_state.used_bodies_by_video.setdefault(
            item.video_id, set()
        )
        body = generate_comment_llm(
            target=target,
            persona=account.persona,
            existing_bodies=existing_bodies | globally_used_bodies,
            rng=rng,
        )
        if body is None:
            body = fallback_comment(
                target=target,
                existing_bodies=existing_bodies,
                rng=rng,
                globally_used=globally_used_bodies,
            )
        existing_bodies.add(body)
        globally_used_bodies.add(body)
        try:
            _api(
                base_url=base_url,
                publish_key=publish_key,
                method="POST",
                path=f"/internal/v1/social-seed/videos/{item.video_id}/comments",
                body={
                    "actor_user_id": item.actor_user_id,
                    "batch_id": batch_id,
                    "body": body,
                    "idempotency_key": item.idempotency_key,
                },
            )
        except ApiError as exc:
            if exc.status == 429:
                stats.comment_rate_limited += 1
            else:
                stats.comment_failed += 1
            if len(stats.errors) < 20:
                stats.errors.append(
                    f"comment {item.video_id}/{item.actor_user_id}: {exc}"
                )
            continue
        stats.commented += 1
        time.sleep(rng.uniform(SLEEP_MIN_SECONDS, SLEEP_MAX_SECONDS))

    result = {
        "status": "ok",
        "day": day,
        "liked": stats.liked,
        "commented": stats.commented,
        "skipped_duplicate": stats.skipped_duplicate,
        "rate_limited": stats.rate_limited,
        "not_found": stats.not_found,
        "forbidden": stats.forbidden,
        "failed": stats.failed,
        "comment_failed": stats.comment_failed,
        "comment_rate_limited": stats.comment_rate_limited,
        "today_total": state.today_total + stats.liked,
        "comments_today_total": comment_state.total_today + stats.commented,
        "llm_available": llm_available(),
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
    parser.add_argument(
        "--no-comments", action="store_true", help="Only likes; skip commenting."
    )
    parser.add_argument(
        "--comments-only", action="store_true", help="Only comments; skip likes."
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
        enable_likes=not args.comments_only,
        enable_comments=not args.no_comments,
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
