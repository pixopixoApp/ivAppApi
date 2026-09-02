"""Redis 推荐内容池全量重建（30 分钟定时，影子 + 原子 RENAME）。

数据来源：published_videos（feed_weight 1~5 直接映射 level）。
每档拆「全量 + new（created_at 在窗口内）」两个 Set，经 shadow 原子切换，零空窗。
部署前提：Redis 内容池全部建好（SCARD 校验通过）后才对外切流量。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.html_content import CONTENT_TYPE_HTML, CONTENT_TYPE_RUNTIME
from app.impressions import (
    ImpressionUnavailableError,
    get_recommend_store,
)
from app.logging_config import get_logger
from app.protocol_video import RuntimeSpecError, read_runtime_spec

log = get_logger(__name__)

# 默认 5 档
LEVELS = [1, 2, 3, 4, 5]


def _visible_filter(db: Session) -> str:
    """与 feed._ordered_pool 一致的可见性过滤（无 viewer 的全局池版本）。"""
    return (
        "is_deleted = 0 AND deleted_at IS NULL "
        "AND review_status = 'approved' "
        "AND distribution_enabled = 1 AND cdn_ready = 1 "
        "AND ("
        "  (content_type = 'runtime' AND runtime_spec IS NOT NULL "
        "   AND runtime_spec_version IS NOT NULL) "
        "  OR "
        "  (content_type = 'html' AND html_url IS NOT NULL AND bridge_version = 1)"
        ")"
    )


def _load_pool_rows(db: Session) -> list[dict]:
    """返回可见视频原始行（dict），交由后续做内容合法性过滤。"""
    stmt = text(
        "SELECT id, feed_weight, created_at, content_type, runtime_spec, "
        "       runtime_spec_version, html_url, bridge_version "
        "FROM published_videos "
        f"WHERE {_visible_filter(db)}"
    )
    rows = db.execute(stmt).mappings().fetchall()
    out = []
    for r in rows:
        row = dict(r)
        # MySQL JSON 列经原生 SQL 读出是 str，需解析为 dict
        spec = row.get("runtime_spec")
        if isinstance(spec, str):
            try:
                row["runtime_spec"] = json.loads(spec)
            except (ValueError, TypeError):
                row["runtime_spec"] = None
        out.append(row)
    return out


def _is_new(created_at: datetime, now: datetime, window_seconds: int) -> bool:
    if created_at is None:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return (now - created_at).total_seconds() <= window_seconds


def _row_playable(row: dict) -> bool:
    """校验该视频能正常构建 FeedItemOut（与 feed._item_from_published 一致）。

    runtime：read_runtime_spec 必须能解析通过；
    html：有受信 URL + bridge_version 即可（构建时不再二次校验失败）。

    返回 False 的行会从内容池剔除，避免推荐出“空壳”。
    """
    ctype = row.get("content_type")
    vid = row.get("id")
    if ctype == CONTENT_TYPE_RUNTIME:
        try:
            read_runtime_spec(
                row.get("runtime_spec"),
                item_id=vid,
                version=row.get("runtime_spec_version"),
            )
            return True
        except RuntimeSpecError as exc:
            log.warning(
                "recommend pool exclude invalid runtime id=%s version=%s err=%s",
                vid, row.get("runtime_spec_version"), exc,
            )
            return False
    if ctype == CONTENT_TYPE_HTML:
        # 建池时 bridge_version 已是 1 且 html_url 非空（见 _visible_filter）
        return True
    return False


def build_all_pools(*, settings: Settings | None = None) -> dict[int, tuple[int, int]]:
    """重建 5 档内容池。返回 {level: (all_count, new_count)}。"""
    settings = settings or get_settings()
    store = get_recommend_store()
    now = datetime.now(timezone.utc)
    new_window = settings.recommend_new_video_window_seconds

    db = SessionLocal()
    try:
        rows = _load_pool_rows(db)
    finally:
        db.close()

    # 按 feed_weight 归档（每视频只归一档），先过滤无法播放的内容
    buckets: dict[int, list[str]] = {lv: [] for lv in LEVELS}
    new_buckets: dict[int, list[str]] = {lv: [] for lv in LEVELS}
    for row in rows:
        if not _row_playable(row):
            continue
        vid = row["id"]
        weight = int(row.get("feed_weight") or 0)
        created_at = row.get("created_at")
        lv = weight if weight in LEVELS else (5 if weight > 5 else 1)
        buckets[lv].append(vid)
        if _is_new(created_at, now, new_window):
            new_buckets[lv].append(vid)

    counts: dict[int, tuple[int, int]] = {}
    for lv in LEVELS:
        store.shadow_rebuild(
            level=lv,
            all_ids=buckets[lv],
            new_ids=new_buckets[lv],
            batch=settings.recommend_pool_batch_size,
        )
        if not store.shadow_validate(level=lv):
            log.warning("recommend pool shadow empty, skip promote level=%s", lv)
            counts[lv] = (0, 0)
            continue
        store.shadow_promote(level=lv)
        counts[lv] = (len(buckets[lv]), len(new_buckets[lv]))
        log.info(
            "recommend pool rebuilt level=%s all=%d new=%d",
            lv, len(buckets[lv]), len(new_buckets[lv]),
        )
    return counts


def rebuild_once() -> None:
    """供定时任务/手动调用。Redis 不可用时仅告警，不中断。"""
    try:
        counts = build_all_pools()
        log.info("recommend pool rebuild done counts=%s", counts)
    except ImpressionUnavailableError as exc:
        log.warning("recommend pool rebuild skipped (redis unavailable): %s", exc)
    except Exception:  # noqa: BLE001 - 建池失败不应拖垮主服务
        log.exception("recommend pool rebuild failed")
