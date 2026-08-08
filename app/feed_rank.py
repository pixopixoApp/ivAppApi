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
    """Ordered ids; login sink = unseen then seen; guest/None = as-is."""
    if not seen_ids:
        return list(ordered_ids)
    unseen = [vid for vid in ordered_ids if vid not in seen_ids]
    seen = [vid for vid in ordered_ids if vid in seen_ids]
    return unseen + seen


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
