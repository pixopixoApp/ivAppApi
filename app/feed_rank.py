from __future__ import annotations


def pin_tutorial(
    weight_ordered_ids: list[str],
    *,
    tutorial_id: str | None,
) -> list[str]:
    """Put tutorial at front of weight-ordered pool (at most one)."""
    if not tutorial_id:
        return list(weight_ordered_ids)
    rest = [vid for vid in weight_ordered_ids if vid != tutorial_id]
    if tutorial_id in weight_ordered_ids:
        return [tutorial_id] + rest
    # Tutorial not in visible pool (e.g. disabled author) — ignore pin.
    return rest


def build_feed_sequence(
    ordered_ids: list[str],
    *,
    seen_ids: set[str] | None,
) -> list[str]:
    """Ordered ids; a logged-in cycle contains only genuinely unseen items.

    The caller resets the Redis set atomically-at-boundary once the eligible
    pool is exhausted.  Appending seen items here made a short pool repeat
    before the user had consumed a full cycle.
    """
    if not seen_ids:
        return list(ordered_ids)
    return [vid for vid in ordered_ids if vid not in seen_ids]


def cursor_after_recent(
    ordered_ids: list[str],
    *,
    recent_ids: list[str],
) -> tuple[int, str | None]:
    """Resume after the highest-ranked recent item still in the pool.

    The pool is already in recommendation order (normally newest content first
    within the same weight). Choosing the earliest pool entry among the three
    recent impressions prevents a newly published item from jumping back to the
    front when one or two older tail items were played after it.
    """
    if not ordered_ids:
        return 0, None
    index_by_id = {video_id: index for index, video_id in enumerate(ordered_ids)}
    candidates = [
        (index_by_id[video_id], video_id)
        for video_id in recent_ids
        if video_id in index_by_id
    ]
    if candidates:
        index, video_id = min(candidates)
        return (index + 1) % len(ordered_ids), video_id
    return 0, None


def page_circular(
    sequence: list[str],
    *,
    cursor: int,
    limit: int,
) -> tuple[list[str], int]:
    """Take up to `limit` ids from circular cursor; return (page, new_cursor)."""
    n = len(sequence)
    if n == 0:
        return [], 0
    start = cursor % n
    out = [sequence[(start + i) % n] for i in range(min(limit, n))]
    new_cursor = (start + len(out)) % n
    return out, new_cursor
