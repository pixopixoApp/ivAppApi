from __future__ import annotations

from app.feed_rank import cursor_after_recent, page_circular


def test_cursor_after_recent_uses_highest_ranked_item_not_last_played() -> None:
    ordered = ["new", "old-1", "old-2", "old-3"]

    cursor, anchor = cursor_after_recent(
        ordered,
        recent_ids=["old-3", "old-2", "new"],
    )
    page, _ = page_circular(ordered, cursor=cursor, limit=2)

    assert anchor == "new"
    assert page == ["old-1", "old-2"]


def test_cursor_after_recent_ignores_items_that_left_the_pool() -> None:
    ordered = ["video-1", "video-2", "video-3"]

    cursor, anchor = cursor_after_recent(
        ordered,
        recent_ids=["video-3", "unpublished", "video-2"],
    )
    page, _ = page_circular(ordered, cursor=cursor, limit=2)

    assert anchor == "video-2"
    assert page == ["video-3", "video-1"]


def test_cursor_after_recent_defaults_to_head_without_available_history() -> None:
    cursor, anchor = cursor_after_recent(
        ["video-1", "video-2"],
        recent_ids=["unpublished"],
    )

    assert cursor == 0
    assert anchor is None
