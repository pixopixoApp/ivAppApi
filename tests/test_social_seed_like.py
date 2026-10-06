from __future__ import annotations

import random
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from scripts import social_seed_like as like


def _targets(n_per_level: int = 3) -> list[like.VideoTarget]:
    return [
        like.VideoTarget(video_id=f"v-l1-{i}", level=1, weight=10.0, lifetime_target=20)
        for i in range(n_per_level)
    ] + [
        like.VideoTarget(video_id=f"v-l5-{i}", level=5, weight=20.0, lifetime_target=200)
        for i in range(n_per_level)
    ]


def _accounts(n: int = 10) -> list[like.SeedAccount]:
    return [like.SeedAccount(user_id=f"social-seed-prelaunch-v1-{i:03d}") for i in range(n)]


def test_higher_level_gets_higher_lifetime_target() -> None:
    """Level bands must be monotonically increasing for higher-exposure levels."""
    bands = like.LEVEL_LIFETIME_TARGETS
    # Level 1 (lowest exposure) gets the fewest likes.
    assert bands[1][1] <= bands[5][0] or bands[1] != bands[5]
    assert bands[1][0] < bands[3][0] < bands[5][0]
    assert bands[4][1] >= bands[3][1] >= bands[2][1] >= bands[1][1]


def test_cumulative_target_rises_through_the_day() -> None:
    base = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    early = like.cumulative_target_by_now(
        daily_total=1000, now_utc=base.replace(hour=4)
    )
    mid = like.cumulative_target_by_now(
        daily_total=1000, now_utc=base.replace(hour=16)
    )
    late = like.cumulative_target_by_now(
        daily_total=1000, now_utc=base.replace(hour=23, minute=59)
    )
    assert early < mid < late
    # At day's end it should approach the full budget.
    assert late >= 950


def test_plan_due_likes_respects_count_and_timestamps() -> None:
    rng = random.Random(42)
    now = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
    plan = like.plan_due_likes(
        targets=_targets(n_per_level=20), count=100, now_utc=now, rng=rng
    )
    assert len(plan) == 100
    # jitter is backwards from now, never in the future
    assert all(item.scheduled_at <= now for item in plan)


def test_plan_due_likes_is_reproducible_with_seed() -> None:
    now = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
    a = like.plan_due_likes(
        targets=_targets(), count=50, now_utc=now, rng=random.Random(7)
    )
    b = like.plan_due_likes(
        targets=_targets(), count=50, now_utc=now, rng=random.Random(7)
    )
    assert [(x.video_id, x.scheduled_at) for x in a] == [
        (x.video_id, x.scheduled_at) for x in b
    ]


def test_plan_due_likes_caps_likes_per_video_per_day() -> None:
    """A tiny pool must not be over-liked in a single run."""
    rng = random.Random(42)
    now = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
    targets = _targets(n_per_level=1)  # only 2 videos
    plan = like.plan_due_likes(
        targets=targets, count=1000, now_utc=now, rng=rng
    )
    counts: dict[str, int] = {}
    for item in plan:
        counts[item.video_id] = counts.get(item.video_id, 0) + 1
    for target in targets:
        cap = max(1, target.lifetime_target // like.RAMP_DAYS_MAX)
        assert counts.get(target.video_id, 0) <= cap


def test_account_assignment_respects_per_account_day_limit() -> None:
    rng = random.Random(5)
    now = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
    plan = like.plan_due_likes(
        targets=_targets(n_per_level=20), count=500, now_utc=now, rng=rng
    )
    accounts = _accounts(5)  # 5 accounts * 30 = 150 max
    assigned = like.assign_accounts(plan=plan, accounts=accounts, rng=rng)
    assert len(assigned) <= 5 * like.MAX_LIKES_PER_ACCOUNT_DAY
    counts: dict[str, int] = {}
    for item in assigned:
        counts[item.actor_user_id] = counts.get(item.actor_user_id, 0) + 1
    assert all(c <= like.MAX_LIKES_PER_ACCOUNT_DAY for c in counts.values())


def test_account_assignment_honours_carried_over_counters() -> None:
    rng = random.Random(9)
    now = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
    plan = like.plan_due_likes(
        targets=_targets(), count=100, now_utc=now, rng=rng
    )
    accounts = _accounts(2)
    carry = {a.user_id: like.MAX_LIKES_PER_ACCOUNT_DAY for a in accounts}
    assigned = like.assign_accounts(
        plan=plan, accounts=accounts, rng=rng, seed_today=dict(carry)
    )
    assert assigned == []


def test_like_key_is_stable() -> None:
    assert like.like_key("v1", "seed-1") == "v1:seed-1"


def test_load_like_state_counts_from_db(db) -> None:
    """State is derived from video_likes rows, not a manifest file."""
    from datetime import datetime, timezone

    from app.models import PublishedVideo, User, VideoLike
    from app.protocol_video import compile_runtime_spec

    db.add(User(
        user_id="social-seed-prelaunch-v1-001",
        provider="internal",
        subject="social-seed:prelaunch-v1:001",
        source="admin",
        internal_purpose="social_seed",
        internal_batch="prelaunch-v1",
        enabled=True,
    ))
    timeline = {"interactions": [{"gesture": "tap", "gate_at_ms": 1000}]}
    spec = compile_runtime_spec(
        item_id="video-1",
        content_mode="single",
        source=timeline,
        video_url="/media/video-1.mp4",
    )
    db.add(PublishedVideo(
        id="video-1",
        video_url="/media/video-1.mp4",
        timeline=timeline,
        runtime_spec=spec,
        runtime_spec_version=spec["version"],
        title="t",
        content_source="pgc",
    ))
    db.commit()

    now = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
    db.add(VideoLike(
        video_id="video-1",
        user_id="social-seed-prelaunch-v1-001",
        is_seed=True,
        created_at=datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc),
    ))
    db.commit()

    state = like.load_like_state(db, now_utc=now, batch_id="prelaunch-v1")
    assert state.today_total == 1
    assert state.per_account_today["social-seed-prelaunch-v1-001"] == 1
    assert state.per_account_hour.get("social-seed-prelaunch-v1-001", 0) == 0
    assert "video-1:social-seed-prelaunch-v1-001" in state.done_keys


def test_dry_run_summary_shape() -> None:
    rng = random.Random(11)
    now = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
    targets = _targets()
    plan = like.plan_due_likes(targets=targets, count=20, now_utc=now, rng=rng)
    summary = like._dry_run_summary(
        plan=plan, due=plan[:5], targets=targets, accounts=_accounts(3)
    )
    assert summary["status"] == "dry-run"
    assert summary["planned_today"] == 20
    assert summary["due_now"] == 5
    assert set(summary["planned_by_level"]) <= {"1", "5"}


# --------------------------------------------------------------------------
# Comment features
# --------------------------------------------------------------------------


def test_persona_assignment_is_deterministic() -> None:
    a = like.persona_for_account("social-seed-prelaunch-v1-001")
    b = like.persona_for_account("social-seed-prelaunch-v1-001")
    assert a == b
    assert a in like.PERSONAS
    seen = {
        like.persona_for_account(f"social-seed-prelaunch-v1-{i:03d}")
        for i in range(60)
    }
    assert len(seen) >= 3


def test_comment_quality_tilt_is_monotonic() -> None:
    assert like.COMMENT_LEVEL_FACTOR[5] > like.COMMENT_LEVEL_FACTOR[1]
    assert like.COMMENT_RATIO_MIN < like.COMMENT_RATIO_MAX


def test_fallback_comment_matches_interaction_family() -> None:
    rng = random.Random(2)
    t = like.VideoTarget(
        video_id="v1",
        level=1,
        weight=10.0,
        lifetime_target=20,
        interaction_family="mic",
    )
    body = like.fallback_comment(target=t, existing_bodies=set(), rng=rng)
    assert body in like.FALLBACK_COMMENTS["mic"]
    used = set(like.FALLBACK_COMMENTS["mic"]) - {body}
    body2 = like.fallback_comment(target=t, existing_bodies=used, rng=rng)
    assert body2 == body


def test_clean_comment_strips_noise() -> None:
    assert like._clean_comment('  "so fun"  ') == "so fun"
    assert like._clean_comment("Comment: the tap is clean") == "the tap is clean"
    assert like._clean_comment("first line\nsecond line") == "first line"
    assert like._clean_comment("") is None
    assert like._clean_comment("x" * 500) is None


def test_llm_unavailable_without_env(monkeypatch) -> None:
    monkeypatch.delenv(like.LLM_ENV_BASE_URL, raising=False)
    monkeypatch.delenv(like.LLM_ENV_API_KEY, raising=False)
    assert like.llm_available() is False
    t = like.VideoTarget(video_id="v1", level=1, weight=10.0, lifetime_target=20)
    assert (
        like.generate_comment_llm(
            target=t, persona="chill", existing_bodies=set(), rng=random.Random(1)
        )
        is None
    )


def test_plan_comments_respects_targets_and_account_limits() -> None:
    rng = random.Random(5)
    now = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
    targets = [
        like.VideoTarget(
            video_id=f"v{i}",
            level=3,
            weight=25.0,
            lifetime_target=100,
            comment_target=2,
        )
        for i in range(5)
    ]
    accounts = _accounts(50)
    state = like.CommentState(
        total_today=0,
        per_account_today={},
        per_account_hour={},
        per_video_total={},
        used_bodies_by_video={},
        used_accounts_by_video={},
    )
    plan = like.plan_comments(
        targets=targets,
        accounts=accounts,
        state=state,
        rng=rng,
        max_comments=100,
        now_utc=now,
    )
    assert len(plan) == 10  # 5 videos * 2 target
    per_video: dict[str, int] = {}
    per_account: dict[str, int] = {}
    for item in plan:
        per_video[item.video_id] = per_video.get(item.video_id, 0) + 1
        per_account[item.actor_user_id] = per_account.get(item.actor_user_id, 0) + 1
    assert all(v <= 2 for v in per_video.values())
    assert all(v <= like.MAX_COMMENTS_PER_ACCOUNT_DAY for v in per_account.values())
    seen_pairs = {(i.video_id, i.actor_user_id) for i in plan}
    assert len(seen_pairs) == len(plan)


def test_fallback_comment_avoids_globally_used() -> None:
    rng = random.Random(3)
    t = like.VideoTarget(
        video_id="v1", level=1, weight=10.0, lifetime_target=20,
        interaction_family="tap",
    )
    pool = set(like.FALLBACK_COMMENTS["tap"])
    # Mark all but one as globally used -> must pick the remaining one.
    remaining = "kept tapping just to see what happens"
    used = pool - {remaining}
    body = like.fallback_comment(
        target=t, existing_bodies=set(), rng=rng, globally_used=used
    )
    assert body == remaining


def test_interaction_family_mapping() -> None:
    assert like._interaction_family(["mic_blow"], []) == "mic"
    assert like._interaction_family(["camera_motion"], ["Smile at the camera"]) == "camera"
    assert like._interaction_family(["hold"], []) == "hold"
    assert like._interaction_family(["continuous_tap"], ["Tap"]) == "tap"
    assert like._interaction_family(["continuous_swipe"], []) == "swipe"
    assert like._interaction_family(["tilt_left"], []) == "tilt"
    assert like._interaction_family(["drag_up"], []) == "swipe"
    assert like._interaction_family(["draw_circle"], []) == "draw"
    assert like._interaction_family([], []) == "generic"


def test_parse_interaction_types_handles_json_string() -> None:
    assert like._parse_interaction_types('["tap", "swipe_up"]') == ["tap", "swipe_up"]
    assert like._parse_interaction_types(["hold"]) == ["hold"]
    assert like._parse_interaction_types(None) == []
    assert like._parse_interaction_types("not-json") == []
