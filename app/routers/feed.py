from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import and_, func, or_
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth_user import (
    issue_user_token,
    load_app_user,
    require_app_user,
    resolve_current_user,
    resolve_request_token,
)
from app.config import Settings, get_settings
from app.db import get_db
from app.feed_rank import (
    build_feed_sequence,
    cursor_after_recent,
    page_circular,
    pin_tutorial,
)
from app.google_auth import GoogleAuthUnavailable, verify_google_id_token
from app.html_content import (
    CONTENT_TYPE_HTML,
    CONTENT_TYPE_RUNTIME,
    HTML_BRIDGE_VERSION,
    HtmlContentError,
    normalize_required_capabilities,
)
from app.impressions import (
    ImpressionUnavailableError,
    content_pool_key,
    content_pool_new_key,
    get_impression_store,
    get_recommend_store,
    user_seen_key,
)
from redis.exceptions import RedisError
from app.logging_config import get_logger
from app.models import (
    AnalyticsLog,
    Follow,
    PublishedVideo,
    PublishedVideoSeo,
    RecommendCursor,
    User,
    VideoView,
)
from app.pagination import (
    CursorError,
    datetime_from_cursor_value,
    datetime_to_cursor_value,
    decode_cursor,
    encode_cursor,
)
from app.protocol_envelope import (
    google_login_error,
    google_login_ok,
    impression_error,
    impression_ok,
    resolve_ssid,
    seen_error,
    seen_ok,
    send_code_error,
    send_code_ok,
    track_error,
    track_ok,
    verify_error,
    verify_ok,
    video_detail_error,
    video_detail_ok,
    video_error,
    video_ok,
)
from app.protocol_video import (
    LEGACY_CLIENT_RUNTIME_SPEC_VERSIONS,
    RuntimeSpecError,
    normalize_client_runtime_spec_versions,
    read_runtime_spec,
)
from app.public_origin import canonicalize_public_payload, canonicalize_public_url
from app.safety import blocked_peer_ids, users_blocked_between
from app.schemas import (
    FeedItemOut,
    GoogleLoginRequest,
    GoogleLoginResponse,
    ImpressionRequest,
    ImpressionResponse,
    SeenRequest,
    SeenResponse,
    SendCodeRequest,
    SendCodeResponse,
    TrackRequest,
    TrackResponse,
    VerifyBodyOut,
    VerifyRequest,
    VerifyResponse,
    VideoBodyOut,
    VideoDetailRequest,
    VideoDetailResponse,
    VideoRequest,
    VideoResponse,
)
from app.share_urls import published_share_url
from app.users import get_or_create_user, is_author_visible, is_under_13, needs_birthday
from app.verification_codes import PURPOSE_LOGIN, find_valid_code, issue_email_code

public_router = APIRouter(tags=["feed"])
auth_router = APIRouter(tags=["feed"], dependencies=[Depends(require_app_user)])
log = get_logger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
def _normalize_email(raw: str) -> str:
    return raw.strip().lower()


def _valid_email(email: str) -> bool:
    return bool(email) and _EMAIL_RE.match(email) is not None


@dataclass(frozen=True)
class FeedItemContext:
    authors_by_id: dict[str, User]
    play_counts_by_video_id: dict[str, int]
    followed_author_ids: frozenset[str]
    seo_slugs_by_video_id: dict[str, str]


def _load_feed_item_context(
    db: Session,
    rows: list[PublishedVideo],
    *,
    viewer_user_id: str | None,
) -> FeedItemContext:
    video_ids = [row.id for row in rows]
    author_ids = {
        row.user_id
        for row in rows
        if row.user_id is not None and row.user_id.strip()
    }
    authors = (
        db.query(User).filter(User.user_id.in_(author_ids)).all()
        if author_ids
        else []
    )
    play_count_rows = (
        db.query(VideoView.video_id, func.count(VideoView.id))
        .filter(VideoView.video_id.in_(video_ids))
        .group_by(VideoView.video_id)
        .all()
        if video_ids
        else []
    )
    followed_author_ids: frozenset[str] = frozenset()
    seo_rows = (
        db.query(PublishedVideoSeo.video_id, PublishedVideoSeo.slug)
        .filter(
            PublishedVideoSeo.video_id.in_(video_ids),
            PublishedVideoSeo.status == "ready",
        )
        .all()
        if video_ids
        else []
    )
    eligible_follow_ids = author_ids - ({viewer_user_id} if viewer_user_id else set())
    if viewer_user_id and eligible_follow_ids:
        followed_author_ids = frozenset(
            followee_id
            for (followee_id,) in (
                db.query(Follow.followee_user_id)
                .filter(
                    Follow.follower_user_id == viewer_user_id,
                    Follow.followee_user_id.in_(eligible_follow_ids),
                )
                .all()
            )
        )
    return FeedItemContext(
        authors_by_id={author.user_id: author for author in authors},
        play_counts_by_video_id={
            video_id: int(count) for video_id, count in play_count_rows
        },
        followed_author_ids=followed_author_ids,
        seo_slugs_by_video_id={video_id: slug for video_id, slug in seo_rows},
    )


def _item_from_published(
    db: Session,
    row: PublishedVideo,
    *,
    settings: Settings,
    viewer_user_id: str | None = None,
    public_share_base_url: str = "",
    supported_runtime_spec_versions: frozenset[str] = LEGACY_CLIENT_RUNTIME_SPEC_VERSIONS,
    context: FeedItemContext | None = None,
) -> FeedItemOut | None:
    if getattr(row, "is_deleted", 0) != 0 or row.deleted_at is not None:
        return None
    if not bool(getattr(row, "distribution_enabled", True)):
        return None
    if not bool(getattr(row, "cdn_ready", True)):
        return None
    avatar_url = ""
    nickname = ""
    if row.user_id:
        author = (
            context.authors_by_id.get(row.user_id)
            if context is not None
            else db.get(User, row.user_id)
        )
        if author is not None:
            avatar_url = canonicalize_public_url(settings, author.avatar_url) or ""
            nickname = author.nickname or ""

    content_type = row.content_type or CONTENT_TYPE_RUNTIME
    clips = None
    html_url = None
    bridge_version = None
    required_capabilities: list[str] = []
    if content_type == CONTENT_TYPE_RUNTIME:
        if row.runtime_spec_version not in supported_runtime_spec_versions:
            return None
        try:
            clips = read_runtime_spec(
                canonicalize_public_payload(settings, row.runtime_spec),
                item_id=row.id,
                version=row.runtime_spec_version,
            )
        except RuntimeSpecError as exc:
            log.error(
                "published item excluded: invalid runtime spec video_id=%s err=%s",
                row.id,
                exc,
            )
            return None
    elif content_type == CONTENT_TYPE_HTML:
        if not row.html_url or row.bridge_version != HTML_BRIDGE_VERSION:
            log.error("published HTML item excluded: invalid payload item_id=%s", row.id)
            return None
        try:
            required_capabilities = normalize_required_capabilities(
                row.required_capabilities or []
            )
        except HtmlContentError as exc:
            log.error(
                "published HTML item excluded: invalid capabilities item_id=%s err=%s",
                row.id,
                exc,
            )
            return None
        html_url = canonicalize_public_url(settings, row.html_url)
        bridge_version = row.bridge_version
    else:
        log.error(
            "published item excluded: unknown content_type item_id=%s content_type=%s",
            row.id,
            content_type,
        )
        return None

    play_count = (
        context.play_counts_by_video_id.get(row.id, 0)
        if context is not None
        else db.query(VideoView).filter(VideoView.video_id == row.id).count()
    )
    viewer_following_author = False
    if viewer_user_id and row.user_id and viewer_user_id != row.user_id:
        viewer_following_author = (
            row.user_id in context.followed_author_ids
            if context is not None
            else (
                db.query(Follow)
                .filter(
                    Follow.follower_user_id == viewer_user_id,
                    Follow.followee_user_id == row.user_id,
                )
                .first()
                is not None
            )
        )
    return FeedItemOut(
        item_id=row.id,
        level=int(row.feed_weight or 0),
        content_type=content_type,
        title=row.title or "",
        description=row.description or "",
        share_url=published_share_url(
            content_type=content_type,
            item_id=row.id,
            public_game_base_url=settings.public_game_base_url,
            public_share_base_url=public_share_base_url,
            seo_public_base_url=settings.seo_public_base_url,
            seo_slug=(
                context.seo_slugs_by_video_id.get(row.id, "")
                if context is not None
                else (
                    seo.slug
                    if (seo := db.get(PublishedVideoSeo, row.id)) is not None
                    and seo.status == "ready"
                    else ""
                )
            ),
        ),
        user_id=row.user_id,
        nickname=nickname,
        avatar_url=avatar_url,
        play_count=play_count,
        is_following=viewer_following_author,
        viewer_following_author=viewer_following_author,
        following=viewer_following_author,
        experience_spec_version=(row.runtime_spec_version if content_type == CONTENT_TYPE_RUNTIME else None),
        video=clips,
        html_url=html_url,
        bridge_version=bridge_version,
        required_capabilities=required_capabilities,
    )


@dataclass(frozen=True)
class PublishedItemsPage:
    items: list[FeedItemOut]
    next_cursor: str | None
    has_more: bool


def _review_allows_direct_share(row: PublishedVideo) -> bool:
    """Allow unlisted UGC links while keeping recommendation surfaces moderated."""
    return row.review_status == "approved" or (
        row.content_source == "ugc" and row.review_status == "pending"
    )


def list_published_items(
    db: Session,
    *,
    settings: Settings,
    author_user_ids: list[str] | None = None,
    viewer_user_id: str | None = None,
    limit: int = 10,
    cursor: str | None = None,
    cursor_kind: str = "published_items",
    cursor_secret: str,
    public_share_base_url: str = "",
    supported_runtime_spec_versions: frozenset[str] = LEGACY_CLIENT_RUNTIME_SPEC_VERSIONS,
) -> PublishedItemsPage:
    """Keyset-paginate persisted, visible Runtime and HTML items (newest first)."""
    q = (
        db.query(PublishedVideo)
        .outerjoin(User, PublishedVideo.user_id == User.user_id)
        .filter(
            or_(
                and_(
                    PublishedVideo.content_type == CONTENT_TYPE_RUNTIME,
                    PublishedVideo.runtime_spec.is_not(None),
                    PublishedVideo.runtime_spec_version.in_(supported_runtime_spec_versions),
                ),
                and_(
                    PublishedVideo.content_type == CONTENT_TYPE_HTML,
                    PublishedVideo.html_url.is_not(None),
                    PublishedVideo.bridge_version == HTML_BRIDGE_VERSION,
                ),
            ),
            PublishedVideo.is_deleted == 0,
            PublishedVideo.deleted_at.is_(None),
            PublishedVideo.review_status == "approved",
            PublishedVideo.distribution_enabled.is_(True),
            PublishedVideo.cdn_ready.is_(True),
            or_(
                PublishedVideo.user_id.is_(None),
                PublishedVideo.user_id == "",
                User.enabled.is_(True),
            ),
        )
    )
    if author_user_ids is not None:
        if not author_user_ids:
            return PublishedItemsPage(items=[], next_cursor=None, has_more=False)
        q = q.filter(PublishedVideo.user_id.in_(author_user_ids))
    hidden_authors = blocked_peer_ids(db, viewer_user_id)
    if hidden_authors:
        q = q.filter(
            or_(
                PublishedVideo.user_id.is_(None),
                PublishedVideo.user_id == "",
                PublishedVideo.user_id.notin_(hidden_authors),
            )
        )
    if cursor:
        values = decode_cursor(cursor=cursor, kind=cursor_kind, secret=cursor_secret)
        cursor_versions = values.get("experience_spec_versions")
        if not (
            cursor_versions is None
            and supported_runtime_spec_versions
            == LEGACY_CLIENT_RUNTIME_SPEC_VERSIONS
        ) and cursor_versions != sorted(supported_runtime_spec_versions):
            raise CursorError("cursor runtime capabilities changed")
        created_at = datetime_from_cursor_value(values.get("created_at"))
        row_id = values.get("id")
        if not isinstance(row_id, str) or not row_id:
            raise CursorError("cursor item id missing")
        q = q.filter(
            or_(
                PublishedVideo.created_at < created_at,
                and_(
                    PublishedVideo.created_at == created_at,
                    PublishedVideo.id < row_id,
                ),
            )
        )
    rows = (
        q.order_by(PublishedVideo.created_at.desc(), PublishedVideo.id.desc())
        .limit(max((limit + 1) * 5, 50))
        .all()
    )
    context = _load_feed_item_context(
        db,
        rows,
        viewer_user_id=viewer_user_id,
    )
    accepted: list[tuple[PublishedVideo, FeedItemOut]] = []
    for row in rows:
        item = _item_from_published(
            db,
            row,
            settings=settings,
            viewer_user_id=viewer_user_id,
            public_share_base_url=public_share_base_url,
            supported_runtime_spec_versions=supported_runtime_spec_versions,
            context=context,
        )
        if item is None:
            continue
        accepted.append((row, item))
        if len(accepted) >= limit + 1:
            break
    has_more = len(accepted) > limit
    page = accepted[:limit]
    next_cursor = None
    if has_more and page:
        last_row = page[-1][0]
        next_cursor = encode_cursor(
            kind=cursor_kind,
            values={
                "created_at": datetime_to_cursor_value(last_row.created_at),
                "id": last_row.id,
                "experience_spec_versions": sorted(supported_runtime_spec_versions),
            },
            secret=cursor_secret,
        )
    return PublishedItemsPage(
        items=[item for _, item in page],
        next_cursor=next_cursor,
        has_more=has_more,
    )

def _ordered_pool(
    db: Session,
    *,
    viewer_user_id: str | None = None,
    supported_runtime_spec_versions: frozenset[str] = LEGACY_CLIENT_RUNTIME_SPEC_VERSIONS,
) -> tuple[list[str], str | None]:
    """Return (weight-ordered visible ids, tutorial_id or None)."""
    query = (
        db.query(
            PublishedVideo.id,
            PublishedVideo.user_id,
            PublishedVideo.is_tutorial,
        )
        .outerjoin(User, PublishedVideo.user_id == User.user_id)
        .filter(
            or_(
                and_(
                    PublishedVideo.content_type == CONTENT_TYPE_RUNTIME,
                    PublishedVideo.runtime_spec.is_not(None),
                    PublishedVideo.runtime_spec_version.in_(supported_runtime_spec_versions),
                ),
                and_(
                    PublishedVideo.content_type == CONTENT_TYPE_HTML,
                    PublishedVideo.html_url.is_not(None),
                    PublishedVideo.bridge_version == HTML_BRIDGE_VERSION,
                ),
            ),
            PublishedVideo.is_deleted == 0,
            PublishedVideo.deleted_at.is_(None),
            PublishedVideo.review_status == "approved",
            PublishedVideo.distribution_enabled.is_(True),
            PublishedVideo.cdn_ready.is_(True),
            or_(
                PublishedVideo.user_id.is_(None),
                PublishedVideo.user_id == "",
                User.enabled.is_(True),
            ),
        )
    )
    hidden_authors = blocked_peer_ids(db, viewer_user_id)
    if hidden_authors:
        query = query.filter(
            or_(
                PublishedVideo.user_id.is_(None),
                PublishedVideo.user_id == "",
                PublishedVideo.user_id.notin_(hidden_authors),
            )
        )
    rows = query.order_by(
        PublishedVideo.feed_weight.desc(),
        PublishedVideo.created_at.desc(),
        PublishedVideo.id.asc(),
    ).all()
    ordered: list[str] = []
    tutorial_id: str | None = None
    for row in rows:
        ordered.append(row.id)
        if bool(row.is_tutorial) and tutorial_id is None:
            tutorial_id = row.id
    return ordered, tutorial_id


def _locked_recommend_cursor(db: Session, *, state_key: str) -> RecommendCursor:
    """Create the cursor without a missing-row gap lock, then lock the row.

    MySQL REPEATABLE READ turns ``SELECT ... FOR UPDATE`` on a missing primary
    key into a gap lock. Concurrent first requests for different devices can
    then deadlock while inserting their independent cursor rows. An atomic
    insert/no-op-upsert establishes the row before the locking read.
    """
    dialect = db.get_bind().dialect.name
    if dialect == "mysql":
        statement = mysql_insert(RecommendCursor).values(token=state_key, cursor=0)
        db.execute(
            statement.on_duplicate_key_update(cursor=RecommendCursor.cursor)
        )
    elif dialect == "sqlite":
        statement = sqlite_insert(RecommendCursor).values(token=state_key, cursor=0)
        db.execute(
            statement.on_conflict_do_nothing(index_elements=[RecommendCursor.token])
        )
    else:
        state = (
            db.query(RecommendCursor)
            .filter(RecommendCursor.token == state_key)
            .with_for_update()
            .one_or_none()
        )
        if state is None:
            state = RecommendCursor(token=state_key, cursor=0)
            db.add(state)
            db.flush()
        return state
    return (
        db.query(RecommendCursor)
        .filter(RecommendCursor.token == state_key)
        .with_for_update()
        .one()
    )


def _next_video_ids(
    db: Session,
    *,
    state_key: str,
    limit: int,
    user_id: str | None,
    cursor_token: str | None,
    cursor_secret: str,
    supported_runtime_spec_versions: frozenset[str],
) -> tuple[list[str], str | None]:
    weight_ordered, tutorial_id = _ordered_pool(
        db,
        viewer_user_id=user_id,
        supported_runtime_spec_versions=supported_runtime_spec_versions,
    )
    cycle_ordered = pin_tutorial(weight_ordered, tutorial_id=tutorial_id)
    seen_ids: set[str] | None = None
    cycle_resume_cursor: int | None = None
    cycle_resume_anchor: str | None = None
    if user_id:
        try:
            impression_store = get_impression_store()
            seen_ids = impression_store.list_seen_ids(user_id=user_id)
            # A cycle is complete only when every currently eligible item was
            # actually played. Cursor-based prefetch must never reset a cycle
            # behind the active client; the explicit first-page/boundary request
            # owns the transition.
            if (
                cursor_token is None
                and cycle_ordered
                and set(cycle_ordered).issubset(seen_ids)
            ):
                recent_ids = impression_store.list_recent_ids(user_id=user_id)
                cycle_resume_cursor, cycle_resume_anchor = cursor_after_recent(
                    cycle_ordered,
                    recent_ids=recent_ids,
                )
                impression_store.clear_cycle(user_id=user_id)
                seen_ids = set()
                log.info(
                    "video feed cycle reset user_id=%s pool=%d recent=%s anchor=%s resume=%d",
                    user_id,
                    len(cycle_ordered),
                    recent_ids,
                    cycle_resume_anchor or "-",
                    cycle_resume_cursor,
                )
        except ImpressionUnavailableError:
            log.warning(
                "video feed redis unavailable; weight-only no sink user_id=%s",
                user_id,
            )
            seen_ids = None

    sequence = build_feed_sequence(cycle_ordered, seen_ids=seen_ids)
    n = len(sequence)

    state = None
    if cursor_token:
        values = decode_cursor(
            cursor=cursor_token,
            kind="recommendations",
            secret=cursor_secret,
        )
        cursor = values.get("index")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise CursorError("invalid recommendation cursor position")
        cursor_versions = values.get("experience_spec_versions")
        if not (
            cursor_versions is None
            and supported_runtime_spec_versions
            == LEGACY_CLIENT_RUNTIME_SPEC_VERSIONS
        ) and cursor_versions != sorted(supported_runtime_spec_versions):
            raise CursorError("cursor runtime capabilities changed")
    else:
        state = _locked_recommend_cursor(db, state_key=state_key)
        if cycle_resume_cursor is not None:
            cursor = cycle_resume_cursor
        else:
            cursor = state.cursor
    out, new_cursor = page_circular(sequence, cursor=cursor, limit=limit)

    if not cursor_token:
        assert state is not None
        state.cursor = new_cursor
        db.commit()

    next_cursor = None
    if sequence:
        next_cursor = encode_cursor(
            kind="recommendations",
            values={
                "index": new_cursor,
                "experience_spec_versions": sorted(supported_runtime_spec_versions),
            },
            secret=cursor_secret,
        )

    log.debug(
        "video feed token=%s user_id=%s pool=%d tutorial=%s seen=%s cursor=%d->%d limit=%d returned=%d",
        state_key,
        user_id or "-",
        n,
        tutorial_id or "-",
        len(seen_ids) if seen_ids is not None else "-",
        cursor % n if n else 0,
        new_cursor,
        limit,
        len(out),
    )
    return out, next_cursor


@public_router.post(
    "/send_code",
    response_model=SendCodeResponse,
    summary="发送邮箱验证码",
    description="向邮箱发送 6 位验证码；同邮箱有发送间隔限制。登录前接口，无需 token。"
    "成功 head.status=0；邮箱非法或限流失败 status=100。",
)
def post_send_code(
    payload: SendCodeRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SendCodeResponse:
    email = _normalize_email(payload.body.email)
    if not _valid_email(email):
        log.warning("send_code invalid email")
        return send_code_error(
            ver=settings.server_ver,
            head_in=payload.head,
            error_code="INVALID_EMAIL",
            message="Please enter a valid email address.",
        )

    result = issue_email_code(
        db,
        settings,
        email=email,
        purpose=PURPOSE_LOGIN,
    )
    if not result.ok:
        if result.error_code == "CODE_RATE_LIMITED":
            log.warning("send_code rate limited email=%s", email)
            return send_code_error(
                ver=settings.server_ver,
                head_in=payload.head,
                error_code=result.error_code,
                message="A code was just sent. Please wait a moment and try again.",
                retry_after_seconds=result.retry_after_seconds,
            )
        return send_code_error(
            ver=settings.server_ver,
            head_in=payload.head,
            error_code=result.error_code,
            message="We couldn’t send the email. Please try again shortly.",
        )

    log.info("send_code ok email=%s", email)
    return send_code_ok(ver=settings.server_ver, head_in=payload.head)


@public_router.post(
    "/verify",
    response_model=VerifyResponse,
    summary="校验验证码并登录",
    description="校验邮箱验证码，成功返回 token、user_id、email、expires_at、needs_birthday。"
    "登录前接口，无需 token。验证码错误或过期：head.status=101。",
)
def post_verify(
    payload: VerifyRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> VerifyResponse:
    email = _normalize_email(payload.body.email)
    code = payload.body.code.strip()
    if not _valid_email(email) or not code.isdigit() or len(code) != 6:
        return verify_error(status=101, ver=settings.server_ver, head_in=payload.head)

    now = datetime.now(timezone.utc)
    row = find_valid_code(
        db,
        email=email,
        code=code,
        purpose=PURPOSE_LOGIN,
        now=now,
    )
    if row is None:
        log.warning("verify bad code email=%s", email)
        return verify_error(status=101, ver=settings.server_ver, head_in=payload.head)

    row.used_at = now
    user = get_or_create_user(db, provider="email", subject=email)
    if not user.enabled:
        db.commit()
        log.warning("verify disabled user email=%s user_id=%s", email, user.user_id)
        return verify_error(status=101, ver=settings.server_ver, head_in=payload.head)
    session = issue_user_token(
        db,
        user_id=user.user_id,
        token_ttl_days=settings.token_ttl_days,
        now=now,
    )
    db.commit()

    body = VerifyBodyOut(
        token=session.token,
        user_id=user.user_id,
        email=email,
        expires_at=session.expires_at.isoformat(),
        needs_birthday=needs_birthday(user),
        birthday=user.birthday or "",
        is_under_13=is_under_13(user),
    )
    log.info("verify ok email=%s user_id=%s", email, user.user_id)
    return verify_ok(body=body, ver=settings.server_ver, head_in=payload.head)


@public_router.post(
    "/google_login",
    response_model=GoogleLoginResponse,
    summary="Google 登录",
    description="登录前接口。body.id_token 为 Android Google Sign-In 的 ID Token；"
    "服务端校验 audience=GOOGLE_CLIENT_ID 后按 provider=google + sub 建/找用户并发会话。"
    "成功 body 与 /verify 同形（token、user_id、email、expires_at、needs_birthday）；"
    "校验失败或账号停用：status=101；未配置 GOOGLE_CLIENT_ID：status=100。",
)
def post_google_login(
    payload: GoogleLoginRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> GoogleLoginResponse:
    client_ids = tuple(dict.fromkeys(
        value.strip()
        for value in (settings.google_client_ids or "").split(",")
        if value.strip()
    )) or ((settings.google_client_id or "").strip(),)
    client_ids = tuple(value for value in client_ids if value)
    if not client_ids:
        log.warning("google_login client ids not configured")
        return google_login_error(ver=settings.server_ver, head_in=payload.head)

    try:
        identity = verify_google_id_token(
            token=payload.body.id_token,
            client_ids=client_ids,
            timeout_seconds=settings.google_timeout_seconds,
        )
    except GoogleAuthUnavailable:
        return google_login_error(
            ver=settings.server_ver,
            head_in=payload.head,
            error_code="GOOGLE_UNAVAILABLE",
            message="Google sign-in is temporarily unavailable. Please try again shortly.",
        )
    except ValueError:
        return google_login_error(
            status=101,
            ver=settings.server_ver,
            head_in=payload.head,
            error_code="GOOGLE_TOKEN_INVALID",
            message="Google sign-in couldn’t be verified. Please try again.",
        )

    now = datetime.now(timezone.utc)
    user = get_or_create_user(db, provider="google", subject=identity.subject)
    if not user.enabled:
        db.commit()
        log.warning(
            "google_login disabled user_id=%s sub=%s",
            user.user_id,
            identity.subject[:8],
        )
        return google_login_error(status=101, ver=settings.server_ver, head_in=payload.head)

    session = issue_user_token(
        db,
        user_id=user.user_id,
        token_ttl_days=settings.token_ttl_days,
        now=now,
    )
    db.commit()

    body = VerifyBodyOut(
        token=session.token,
        user_id=user.user_id,
        email=identity.email,
        expires_at=session.expires_at.isoformat(),
        needs_birthday=needs_birthday(user),
        birthday=user.birthday or "",
        is_under_13=is_under_13(user),
    )
    log.info(
        "google_login ok user_id=%s sub=%s needs_birthday=%s",
        user.user_id,
        identity.subject[:8],
        body.needs_birthday,
    )
    return google_login_ok(body=body, ver=settings.server_ver, head_in=payload.head)


# ---------------------------------------------------------------------------
# Redis 推荐内容池（5 档随机采样）
# ---------------------------------------------------------------------------
_REC_LEVELS = [1, 2, 3, 4, 5]
# 低档少、中间档为主、高档精品保量的橄榄型供给（level1~level5 顺序，合计 20）。
# 作为 recommend_per_level_counts 未配置时的回退默认值。
_DEFAULT_PER_LEVEL_COUNTS = [2, 3, 5, 6, 4]
# 高质量优先遍历顺序（补位时用，保证补进来的也尽量高质量）。
_REC_LEVELS_HIGH_FIRST = list(reversed(_REC_LEVELS))


def _resolve_per_level_counts(settings: Settings) -> list[int]:
    """解析每档目标条数（level1~level5）。

    优先读取 recommend_per_level_counts（逗号分隔字符串）；未配置时使用默认的
    橄榄型 _DEFAULT_PER_LEVEL_COUNTS；仅当显式给出 5 个非负整数时才采用，
    格式非法则回退默认橄榄型。
    """
    raw = (settings.recommend_per_level_counts or "").strip()
    if raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if len(parts) == len(_REC_LEVELS):
            try:
                counts = [int(p) for p in parts]
                if all(c >= 0 for c in counts):
                    return counts
            except ValueError:
                pass
    return list(_DEFAULT_PER_LEVEL_COUNTS)


# 20 格体验曲线模板（期望档位，5 最高）。
# 设计意图：高质开场抓人 → 3/4 档为主体承接 → 中后段才回落低档 → 避免单调递减。
# 计数与默认橄榄型 _DEFAULT_PER_LEVEL_COUNTS(2/3/5/6/4) 一致。
_EXPERIENCE_LEVEL_TEMPLATE = [
    5, 4, 5, 4, 3, 4, 5, 4, 3, 4,
    3, 2, 4, 3, 2, 1, 2, 1, 3, 5,
]


def _arrange_by_experience_template(
    pool_by_level: dict[int, list[str]],
    *,
    template: list[int],
) -> list[str]:
    """把已抽到的各档视频按“体验曲线模板”重排。

    逐格按期望档位取出；该档为空时按“高→低”就近递补，避免低质冷场，
    也避免相邻出现 5→1 式骤降。模板覆盖后仍有剩余（自定义 counts 或缺档
    导致的数量差）时，按高质量优先追加到尾部，保证不漏内容。
    """
    # 每档内部先随机打散，让同档内多条不固定、有新鲜感。
    iterators: dict[int, list[str]] = {}
    for lv in _REC_LEVELS:
        bucket = list(pool_by_level.get(lv, []))
        random.shuffle(bucket)
        iterators[lv] = bucket

    def take_from(level: int) -> str | None:
        bucket = iterators.get(level)
        if bucket is None:
            return None
        return bucket.pop() if bucket else None

    ordered: list[str] = []
    for want in template:
        v = take_from(want)
        if v is None:
            # 期望档已空：先试更高质档，尽量维持体验不陡降
            for cand in _REC_LEVELS_HIGH_FIRST:
                if cand == want:
                    continue
                v = take_from(cand)
                if v is not None:
                    break
        if v is not None:
            ordered.append(v)

    # 兜底：把仍未放入模板的剩余内容（高质量优先）追加到末尾。
    for lv in _REC_LEVELS_HIGH_FIRST:
        ordered.extend(iterators.get(lv, ()))
    return ordered


def _rec_sample_from_key(
    store,
    *,
    key: str,
    batch: int,
    need: int,
    seen_key: str | None,
    global_sampled: set[str],
    result: list[str],
    allow_sampled: bool = False,
) -> int:
    """从单个 Redis Set 采样 batch 个，过滤【本次请求已选】+【已看】。

    allow_sampled=True 时跳过“本次请求已选”过滤，允许同一请求内出现重复
    （仅用于确需同请求重复的场景；回放已不再使用该开关）。
    返回实际新增数量。seen_key=None 表示放开“已看”过滤（回放/去重穿透用）。
    """
    got = 0
    cands = store.srandmember(key=key, count=batch)
    if not cands:
        return 0
    if allow_sampled:
        fresh = list(cands)
    else:
        # 本次请求去重
        fresh = [v for v in cands if v not in global_sampled]
        if not fresh:
            return 0
    if seen_key is not None:
        seen_flags = store.is_seen(seen_key=seen_key, video_ids=fresh)
        fresh = [v for v, seen in zip(fresh, seen_flags) if not seen]
    for v in fresh:
        if got >= need:
            break
        result.append(v)
        if not allow_sampled:
            # 只把真正返回的加入“本次已选”，避免把抽到但未返回的候选误标记，
            # 导致后续补位阶段可选内容被白白浪费 / 提前触发回放。
            global_sampled.add(v)
        got += 1
    return got


def _rec_sample_level(
    store,
    *,
    level: int,
    need: int,
    seen_key: str | None,
    global_sampled: set[str],
    new_window_seconds: int,
    base_sample: int,
    max_sample: int,
    max_retry: int,
) -> list[str]:
    """单档采样：新内容优先 + 存量补足 + 动态扩采。返回该档选中的 video_id 列表。"""
    out: list[str] = []
    got = 0
    # 1. 新内容优先（新视频不足 4 条时，不足部分由存量补）
    new_key = content_pool_new_key(level)
    got += _rec_sample_from_key(
        store,
        key=new_key,
        batch=base_sample,
        need=need,
        seen_key=seen_key,
        global_sampled=global_sampled,
        result=out,
    )
    # 2. 存量补足（动态扩采）
    all_key = content_pool_key(level)
    retry = 0
    while got < need and retry < max_retry:
        batch = min(base_sample * (retry + 1), max_sample)
        got += _rec_sample_from_key(
            store,
            key=all_key,
            batch=batch,
            need=need - got,
            seen_key=seen_key,
            global_sampled=global_sampled,
            result=out,
        )
        retry += 1
    return out


def _build_redis_recommend_ids(
    *,
    user_id: str | None,
    ssid: str,
    settings: Settings,
) -> tuple[list[str], bool, str]:
    """Redis 推荐主流程：橄榄型逐档抽样 → 高质量补位 → 回放 → 体验曲线排序。

    返回 (video_ids, is_rewind, seen_key)。video_ids 顺序即最终下发顺序。
    """
    store = get_recommend_store()
    total_target = settings.recommend_total_target
    per_level_counts = _resolve_per_level_counts(settings)
    new_window = settings.recommend_new_video_window_seconds
    base_sample = settings.recommend_base_sample
    max_sample = settings.recommend_max_sample
    max_retry = settings.recommend_max_retry

    # seen key：登录用户免；游客用 ssid 短 TTL 去重
    is_guest = not user_id
    seen_key = None
    if is_guest:
        seen_key = user_seen_key(ssid, is_guest=True)
        store.clean_expired_seen(
            seen_key=seen_key,
            expire_ts=int(__import__("time").time()) - settings.recommend_guest_seen_ttl_seconds,
        )
    else:
        seen_key = user_seen_key(user_id)
        store.clean_expired_seen(
            seen_key=seen_key,
            expire_ts=int(__import__("time").time())
            - settings.recommend_seen_expire_days * 86400,
        )

    global_sampled: set[str] = set()
    # 分档收集本次实际抽到的视频，以便最后按“体验曲线模板”重排下发顺序。
    pool_by_level: dict[int, list[str]] = {lv: [] for lv in _REC_LEVELS}

    def total_collected() -> int:
        return sum(len(v) for v in pool_by_level.values())

    # 1. 按橄榄型比例（中档为主、高档保量、低档少量）逐档抽样
    for lv, need in zip(_REC_LEVELS, per_level_counts):
        if need <= 0:
            continue
        pool_by_level[lv].extend(
            _rec_sample_level(
                store,
                level=lv,
                need=need,
                seen_key=seen_key,
                global_sampled=global_sampled,
                new_window_seconds=new_window,
                base_sample=base_sample,
                max_sample=max_sample,
                max_retry=max_retry,
            )
        )

    # 2. 全局补位（高质量优先：5→1），保证不足时补进来的也尽量高质量
    gap = total_target - total_collected()
    if gap > 0:
        for lv in _REC_LEVELS_HIGH_FIRST:
            if gap <= 0:
                break
            add = _rec_sample_level(
                store,
                level=lv,
                need=gap,
                seen_key=seen_key,
                global_sampled=global_sampled,
                new_window_seconds=new_window,
                base_sample=base_sample,
                max_sample=max_sample,
                max_retry=max_retry,
            )
            pool_by_level[lv].extend(add)
            gap -= len(add)

    # 3. 终极兜底：全网未看不足，放开去重回放（尽量补到 20 条）
    rewind = False
    if total_collected() < total_target:
        rewind = True
        loop = 0
        while total_collected() < total_target and loop < max_retry:
            loop += 1
            batch = min(base_sample * loop, max_sample)
            collected_before_round = total_collected()
            for lv in _REC_LEVELS:
                if total_collected() >= total_target:
                    break
                _rec_sample_from_key(
                    store,
                    key=content_pool_key(lv),
                    batch=batch,
                    need=total_target - total_collected(),
                    seen_key=None,  # 放开“已看”过滤（回放：允许与历史请求重复）
                    global_sampled=global_sampled,
                    result=pool_by_level[lv],
                    allow_sampled=False,  # 保留本次请求内去重，避免同一条响应里重复
                )
            if total_collected() == collected_before_round:
                # 整轮没有任何新增：池已不足以再填满，退出防死循环
                break

    # 4. 按“体验曲线模板”重排（高质开场/主体承接/避免单调递减/收尾钩子）
    ordered = _arrange_by_experience_template(
        pool_by_level,
        template=_EXPERIENCE_LEVEL_TEMPLATE,
    )
    final = ordered[:total_target]

    return final, rewind, seen_key



@public_router.post(
    "/video",
    response_model=VideoResponse,
    response_model_exclude_none=True,
    summary="拉取推荐视频列表",
    description="游客可访问（可不带 token）。教学片未看时置顶；否则 feed_weight 降序；"
    "登录用户在一个播放周期内只收到未看内容，整池播放完成后重置。"
    "成功 body.items[]（ExperienceSpec v1.0/v1.1；v1.1 Story 可选 video.on_end；无 transition）。"
    "单视频 video.length=1；Story 多段且入口 clip 在首位。池为空 status=100。",
)
def post_video(
    payload: VideoRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> VideoResponse:
    token = resolve_request_token(request, payload.head, act="video")
    user = resolve_current_user(request, db, token)
    limit = payload.body.limit
    supported_runtime_spec_versions = normalize_client_runtime_spec_versions(
        payload.body.supported_experience_spec_versions
    )
    ssid = resolve_ssid(payload.head)
    payload.head.ssid = ssid

    # ---- Redis 推荐方案（开关开启时优先，Redis 不可用自动降级 MySQL）----
    redis_video_ids: list[str] | None = None
    redis_is_rewind = False
    redis_seen_key: str | None = None
    if settings.feature_recommend_redis:
        try:
            redis_video_ids, redis_is_rewind, redis_seen_key = _build_redis_recommend_ids(
                user_id=user.user_id if user else None,
                ssid=ssid,
                settings=settings,
            )
        except (RedisError, ImpressionUnavailableError) as exc:
            log.warning(
                "video redis recommend unavailable, fallback to mysql token=%s err=%s",
                token, exc,
            )
            redis_video_ids = None
    if redis_video_ids is None:
        # ---- 降级：现有 MySQL 直查逻辑 ----
        capability_key = ",".join(sorted(supported_runtime_spec_versions)) or "none"
        state_key = (
            f"feed:user:{user.user_id}:spec:{capability_key}"
            if user
            else f"feed:ssid:{ssid}:spec:{capability_key}"
        )
        try:
            video_ids, next_cursor = _next_video_ids(
                db,
                state_key=state_key,
                limit=limit,
                user_id=user.user_id if user else None,
                cursor_token=payload.body.cursor,
                cursor_secret=settings.cursor_secret or settings.publish_key,
                supported_runtime_spec_versions=supported_runtime_spec_versions,
            )
        except CursorError as exc:
            log.warning("video invalid cursor err=%s", exc)
            return video_error(ver=settings.server_ver, head_in=payload.head)
        if not video_ids:
            log.warning("video feed empty pool token=%s", token)
            return video_error(ver=settings.server_ver, head_in=payload.head)

        rows_by_id = {
            row.id: row
            for row in db.query(PublishedVideo)
            .filter(PublishedVideo.id.in_(video_ids))
            .all()
        }
        context = _load_feed_item_context(
            db,
            list(rows_by_id.values()),
            viewer_user_id=user.user_id if user else None,
        )
        items: list[FeedItemOut] = []
        for vid in video_ids:
            row = rows_by_id.get(vid)
            if row is None or row.is_deleted != 0:
                log.warning("video feed skip missing/deleted id=%s", vid)
                continue
            item = _item_from_published(
                db,
                row,
                settings=settings,
                viewer_user_id=user.user_id if user else None,
                public_share_base_url=settings.public_share_base_url,
                supported_runtime_spec_versions=supported_runtime_spec_versions,
                context=context,
            )
            if item is not None:
                items.append(item)

        if not items:
            return video_error(ver=settings.server_ver, head_in=payload.head)

        body = VideoBodyOut(
            items=items,
            next_cursor=next_cursor,
            has_more=True,
            is_circular=True,
        )
        log.debug("video feed ok (mysql fallback) token=%s items=%d", token, len(items))
        return video_ok(body=body, ver=settings.server_ver, head_in=payload.head)

    # ---- Redis 方案：构建 items ----
    video_ids = redis_video_ids
    if not video_ids:
        log.warning("video feed empty redis pool token=%s", token)
        return video_error(ver=settings.server_ver, head_in=payload.head)

    rows_by_id = {
        row.id: row
        for row in db.query(PublishedVideo)
        .filter(PublishedVideo.id.in_(video_ids))
        .all()
    }
    context = _load_feed_item_context(
        db,
        list(rows_by_id.values()),
        viewer_user_id=user.user_id if user else None,
    )
    items: list[FeedItemOut] = []
    for vid in video_ids:
        row = rows_by_id.get(vid)
        if row is None or row.is_deleted != 0:
            continue
        item = _item_from_published(
            db,
            row,
            settings=settings,
            viewer_user_id=user.user_id if user else None,
            public_share_base_url=settings.public_share_base_url,
            supported_runtime_spec_versions=supported_runtime_spec_versions,
            context=context,
        )
        if item is not None:
            items.append(item)

    if not items:
        return video_error(ver=settings.server_ver, head_in=payload.head)

    # 曝光即标记：本次推荐的视频写入 seen（游客短 TTL，登录 7 天），
    # 实现游客“一次持续访问内去重”（下拉刷过的不再重复出现）。
    if redis_seen_key:
        try:
            is_guest = user is None
            ttl = (
                settings.recommend_guest_seen_ttl_seconds
                if is_guest
                else settings.recommend_seen_expire_days * 86400
            )
            store = get_recommend_store()
            # 只标记真正返回给客户端的 items，避免把候选但未真正返回的
            # （db 缺失/已删除/构建失败被跳过）误写入 seen 而后续被静默吞掉。
            for item in items:
                store.mark_seen(
                    seen_key=redis_seen_key,
                    video_id=item.item_id,
                    ttl_seconds=ttl,
                )
        except (RedisError, ImpressionUnavailableError):
            # 标记失败不影响本次返回（下次可能少量重复，可接受）
            log.warning("video redis mark_seen failed token=%s", token)

    body = VideoBodyOut(
        items=items,
        next_cursor=None,
        has_more=True,
        is_circular=True,
        is_rewind=redis_is_rewind,
    )
    log.debug("video feed ok (redis) token=%s items=%d is_rewind=%s", token, len(items), redis_is_rewind)
    return video_ok(body=body, ver=settings.server_ver, head_in=payload.head)


@public_router.post(
    "/video_detail",
    response_model=VideoDetailResponse,
    response_model_exclude_none=True,
    summary="按 item_id 单拉发布单元",
    description="游客可访问（可不带 token）。body.video_id 语义为**发布单元 item_id**；"
    "成功 body.items 为单元素数组（与 /video 同形，App v1.0）。"
    "不存在或作者已停用：status=100。",
)
def post_video_detail(
    payload: VideoDetailRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> VideoDetailResponse:
    video_id = payload.body.video_id.strip()
    supported_runtime_spec_versions = normalize_client_runtime_spec_versions(
        payload.body.supported_experience_spec_versions
    )
    if not video_id:
        return video_detail_error(ver=settings.server_ver, head_in=payload.head)

    row = db.get(PublishedVideo, video_id)
    if (
        row is None
        or row.is_deleted != 0
        or row.deleted_at is not None
        or not _review_allows_direct_share(row)
        or not row.distribution_enabled
        or not row.cdn_ready
        or (
            (row.content_type or CONTENT_TYPE_RUNTIME) == CONTENT_TYPE_RUNTIME
            and row.runtime_spec_version not in supported_runtime_spec_versions
        )
    ):
        log.warning("video_detail missing video_id=%s", video_id)
        return video_detail_error(ver=settings.server_ver, head_in=payload.head)

    if not is_author_visible(db, row.user_id):
        log.warning(
            "video_detail hidden disabled author video_id=%s user_id=%s",
            video_id,
            row.user_id,
        )
        return video_detail_error(ver=settings.server_ver, head_in=payload.head)

    token = resolve_request_token(request, payload.head, act="video_detail")
    viewer = resolve_current_user(request, db, token)
    if (
        viewer is not None
        and row.user_id
        and users_blocked_between(db, viewer.user_id, row.user_id)
    ):
        return video_detail_error(ver=settings.server_ver, head_in=payload.head)
    item = _item_from_published(
        db,
        row,
        settings=settings,
        viewer_user_id=viewer.user_id if viewer else None,
        public_share_base_url=settings.public_share_base_url,
        supported_runtime_spec_versions=supported_runtime_spec_versions,
        context=_load_feed_item_context(
            db,
            [row],
            viewer_user_id=viewer.user_id if viewer else None,
        ),
    )
    if item is None:
        return video_detail_error(ver=settings.server_ver, head_in=payload.head)

    log.debug("video_detail ok video_id=%s", video_id)
    return video_detail_ok(
        body=VideoBodyOut(items=[item]),
        ver=settings.server_ver,
        head_in=payload.head,
    )


@auth_router.post(
    "/track",
    response_model=TrackResponse,
    summary="上报埋点",
    description="需登录。body 含 video_id 与 data 字符串。成功 status=0；"
    "参数非法或视频不存在 status=100；无效 token status=101。",
)
def post_track(
    payload: TrackRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TrackResponse:
    token = resolve_request_token(request, payload.head, act="track")
    video_id = payload.body.video_id.strip()
    data = payload.body.data.strip() if isinstance(payload.body.data, str) else ""
    if not video_id or not data:
        log.warning("track invalid body token=%s video_id=%s", token, video_id)
        return track_error(ver=settings.server_ver, head_in=payload.head)

    tracked = db.get(PublishedVideo, video_id)
    if (
        tracked is None
        or tracked.is_deleted != 0
        or tracked.deleted_at is not None
        or not _review_allows_direct_share(tracked)
        or not tracked.distribution_enabled
        or not tracked.cdn_ready
    ):
        log.warning("track unknown video_id=%s token=%s", video_id, token)
        return track_error(ver=settings.server_ver, head_in=payload.head)

    db.add(AnalyticsLog(video_id=video_id, token=token, data=data))
    db.commit()
    log.info("track ok video_id=%s token=%s bytes=%d", video_id, token, len(data))
    return track_ok(ver=settings.server_ver, head_in=payload.head)


@auth_router.post(
    "/impression",
    response_model=ImpressionResponse,
    summary="上报曝光（播放周期去重）",
    description="需登录。body.video_id 写入 Redis 播放周期；本轮不再重复下发，整池完成后自动重置。"
    "成功 status=0；视频不存在或 Redis 不可用 status=100；无效 token status=101。",
)
def post_impression(
    payload: ImpressionRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ImpressionResponse:
    token = resolve_request_token(request, payload.head, act="impression")
    user = resolve_current_user(request, db, token)
    if user is None:
        # auth_router already requires login; defensive
        user = load_app_user(db, token)
    if user is None:
        return impression_error(
            status=101, ver=settings.server_ver, head_in=payload.head
        )

    video_id = payload.body.video_id.strip()
    impression_video = db.get(PublishedVideo, video_id) if video_id else None
    if (not video_id or impression_video is None or impression_video.is_deleted != 0
            or impression_video.deleted_at is not None
            or impression_video.review_status != "approved"
            or not impression_video.distribution_enabled
            or not impression_video.cdn_ready):
        log.warning(
            "impression invalid video_id=%s user_id=%s",
            video_id,
            user.user_id,
        )
        return impression_error(ver=settings.server_ver, head_in=payload.head)

    try:
        get_impression_store().mark_seen(user_id=user.user_id, video_id=video_id)
    except ImpressionUnavailableError:
        log.warning("impression redis unavailable user_id=%s", user.user_id)
        return impression_error(ver=settings.server_ver, head_in=payload.head)

    exists = (
        db.query(VideoView)
        .filter(VideoView.video_id == video_id, VideoView.user_id == user.user_id)
        .first()
    )
    if exists is None:
        db.add(VideoView(video_id=video_id, user_id=user.user_id))
        try:
            db.commit()
        except IntegrityError:
            # A concurrent impression may win the unique(video_id,user_id) insert.
            db.rollback()

    log.info("impression ok user_id=%s video_id=%s", user.user_id, video_id)
    return impression_ok(ver=settings.server_ver, head_in=payload.head)


@public_router.post(
    "/seen",
    response_model=SeenResponse,
    response_model_exclude_none=True,
    summary="上报用户已访问/播放，写入 user:seen 去重池",
    description="登录与游客均可。body.video_id 写入 user:seen:{userId}（登录，7 天）"
    "或 user:seen:guest:{ssid}（游客，短 TTL），供推荐去重使用。"
    "成功 status=0；视频不存在/不可见 status=100；Redis 不可用 status=100。",
)
def post_seen(
    payload: SeenRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SeenResponse:
    token = resolve_request_token(request, payload.head, act="seen")
    user = resolve_current_user(request, db, token)
    ssid = resolve_ssid(payload.head)
    payload.head.ssid = ssid

    video_id = payload.body.video_id.strip()
    if not video_id:
        return seen_error(ver=settings.server_ver, head_in=payload.head)

    video = db.get(PublishedVideo, video_id)
    if (
        video is None
        or video.is_deleted != 0
        or video.deleted_at is not None
        or video.review_status != "approved"
        or not video.distribution_enabled
        or not video.cdn_ready
    ):
        log.warning("seen invalid video_id=%s token=%s", video_id, token)
        return seen_error(ver=settings.server_ver, head_in=payload.head)

    # seen key：登录用户写 user:seen:{userId}；游客写 user:seen:guest:{ssid}
    is_guest = user is None
    seen_key = user_seen_key(user.user_id if user else ssid, is_guest=is_guest)

    try:
        store = get_recommend_store()
        ttl = (
            settings.recommend_guest_seen_ttl_seconds
            if is_guest
            else settings.recommend_seen_expire_days * 86400
        )
        store.mark_seen(seen_key=seen_key, video_id=video_id, ttl_seconds=ttl)
    except (RedisError, ImpressionUnavailableError):
        log.warning("seen redis unavailable token=%s video_id=%s", token, video_id)
        return seen_error(ver=settings.server_ver, head_in=payload.head)

    log.info("seen ok user_id=%s guest=%s video_id=%s", user.user_id if user else ssid, is_guest, video_id)
    return seen_ok(ver=settings.server_ver, head_in=payload.head)
