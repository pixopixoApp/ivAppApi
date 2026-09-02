from __future__ import annotations

import secrets
from datetime import datetime, timezone

from app.schemas import (
    AuthProtocolHeadIn,
    AvatarResponse,
    BirthdayBodyOut,
    BirthdayResponse,
    DeactivateBodyOut,
    DeactivateResponse,
    DeactivateSendCodeResponse,
    EmptyBody,
    FollowersBodyOut,
    FollowersResponse,
    FollowingBodyOut,
    FollowingFeedResponse,
    FollowingResponse,
    FollowResponse,
    GoogleLoginResponse,
    ImpressionResponse,
    MyVideosResponse,
    SeenResponse,
    ProfileBodyOut,
    ProfileResponse,
    ProfileUpdateResponse,
    ProtocolHeadIn,
    ProtocolHeadOut,
    PublicProfileBodyOut,
    SendCodeResponse,
    TrackResponse,
    UnfollowResponse,
    UserProfileResponse,
    UserVideosResponse,
    VerifyBodyOut,
    VerifyResponse,
    VideoBodyOut,
    VideoDetailResponse,
    VideoResponse,
)

HeadIn = ProtocolHeadIn | AuthProtocolHeadIn


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def resolve_token(head: ProtocolHeadIn) -> str:
    if isinstance(head.token, str) and head.token.strip():
        return head.token.strip()
    return "anonymous"


def resolve_ssid(head: HeadIn | None) -> str:
    if head is not None and isinstance(head.ssid, str) and head.ssid.strip():
        return head.ssid.strip()
    return secrets.token_hex(8)


def make_head(
    *,
    act: str,
    status: int,
    ver: str,
    head_in: HeadIn | None = None,
    error_code: str | None = None,
    message: str | None = None,
    retry_after_seconds: int | None = None,
) -> ProtocolHeadOut:
    out_ver = ver
    if head_in is not None and head_in.ver.strip():
        out_ver = head_in.ver.strip()
    return ProtocolHeadOut(
        act=act,
        status=status,
        ver=out_ver,
        time=_now_str(),
        ssid=resolve_ssid(head_in),
        error_code=error_code,
        message=message,
        retry_after_seconds=retry_after_seconds,
    )


def video_ok(
    *,
    body: VideoBodyOut,
    ver: str,
    head_in: ProtocolHeadIn,
) -> VideoResponse:
    return VideoResponse(
        head=make_head(act="video", status=0, ver=ver, head_in=head_in),
        body=body,
    )


def video_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn,
) -> VideoResponse:
    return VideoResponse(
        head=make_head(act="video", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def video_detail_ok(
    *,
    body: VideoBodyOut,
    ver: str,
    head_in: AuthProtocolHeadIn | ProtocolHeadIn,
) -> VideoDetailResponse:
    return VideoDetailResponse(
        head=make_head(act="video_detail", status=0, ver=ver, head_in=head_in),
        body=body,
    )


def video_detail_error(
    *,
    status: int = 100,
    ver: str,
    head_in: AuthProtocolHeadIn | ProtocolHeadIn,
) -> VideoDetailResponse:
    return VideoDetailResponse(
        head=make_head(act="video_detail", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def track_ok(*, ver: str, head_in: ProtocolHeadIn) -> TrackResponse:
    return TrackResponse(
        head=make_head(act="track", status=0, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def track_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn,
) -> TrackResponse:
    return TrackResponse(
        head=make_head(act="track", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def impression_ok(*, ver: str, head_in: ProtocolHeadIn) -> ImpressionResponse:
    return ImpressionResponse(
        head=make_head(act="impression", status=0, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def impression_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn,
) -> ImpressionResponse:
    return ImpressionResponse(
        head=make_head(act="impression", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def seen_ok(*, ver: str, head_in: ProtocolHeadIn) -> SeenResponse:
    return SeenResponse(
        head=make_head(act="seen", status=0, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def seen_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn,
) -> SeenResponse:
    return SeenResponse(
        head=make_head(act="seen", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def send_code_ok(*, ver: str, head_in: AuthProtocolHeadIn) -> SendCodeResponse:
    return SendCodeResponse(
        head=make_head(act="send_code", status=0, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def send_code_error(
    *,
    status: int = 100,
    ver: str,
    head_in: AuthProtocolHeadIn,
    error_code: str | None = None,
    message: str | None = None,
    retry_after_seconds: int | None = None,
) -> SendCodeResponse:
    return SendCodeResponse(
        head=make_head(
            act="send_code",
            status=status,
            ver=ver,
            head_in=head_in,
            error_code=error_code,
            message=message,
            retry_after_seconds=retry_after_seconds,
        ),
        body=EmptyBody(),
    )


def deactivate_send_code_ok(
    *, ver: str, head_in: ProtocolHeadIn
) -> DeactivateSendCodeResponse:
    return DeactivateSendCodeResponse(
        head=make_head(act="deactivate_send_code", status=0, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def deactivate_send_code_error(
    *,
    ver: str,
    head_in: ProtocolHeadIn,
    status: int = 100,
    error_code: str | None = None,
    message: str | None = None,
    retry_after_seconds: int | None = None,
) -> DeactivateSendCodeResponse:
    return DeactivateSendCodeResponse(
        head=make_head(
            act="deactivate_send_code",
            status=status,
            ver=ver,
            head_in=head_in,
            error_code=error_code,
            message=message,
            retry_after_seconds=retry_after_seconds,
        ),
        body=EmptyBody(),
    )


def verify_ok(
    *,
    body: VerifyBodyOut,
    ver: str,
    head_in: AuthProtocolHeadIn,
) -> VerifyResponse:
    return VerifyResponse(
        head=make_head(act="verify", status=0, ver=ver, head_in=head_in),
        body=body,
    )


def verify_error(
    *,
    status: int = 100,
    ver: str,
    head_in: AuthProtocolHeadIn,
) -> VerifyResponse:
    return VerifyResponse(
        head=make_head(act="verify", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def google_login_ok(
    *,
    body: VerifyBodyOut,
    ver: str,
    head_in: AuthProtocolHeadIn,
) -> GoogleLoginResponse:
    return GoogleLoginResponse(
        head=make_head(act="google_login", status=0, ver=ver, head_in=head_in),
        body=body,
    )


def google_login_error(
    *,
    status: int = 100,
    ver: str,
    head_in: AuthProtocolHeadIn,
    error_code: str | None = None,
    message: str | None = None,
) -> GoogleLoginResponse:
    return GoogleLoginResponse(
        head=make_head(
            act="google_login",
            status=status,
            ver=ver,
            head_in=head_in,
            error_code=error_code,
            message=message,
        ),
        body=EmptyBody(),
    )


def auth_fail_payload(*, act: str, status: int, ver: str, head_in: HeadIn | None) -> dict:
    """Protocol JSON for AppAuthError (status=101)."""
    return {
        "head": make_head(act=act, status=status, ver=ver, head_in=head_in).model_dump(),
        "body": {},
    }


def follow_ok(*, ver: str, head_in: ProtocolHeadIn) -> FollowResponse:
    return FollowResponse(
        head=make_head(act="follow", status=0, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def follow_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn,
) -> FollowResponse:
    return FollowResponse(
        head=make_head(act="follow", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def unfollow_ok(*, ver: str, head_in: ProtocolHeadIn) -> UnfollowResponse:
    return UnfollowResponse(
        head=make_head(act="unfollow", status=0, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def unfollow_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn,
) -> UnfollowResponse:
    return UnfollowResponse(
        head=make_head(act="unfollow", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def following_ok(
    *,
    body: FollowingBodyOut,
    ver: str,
    head_in: ProtocolHeadIn,
) -> FollowingResponse:
    return FollowingResponse(
        head=make_head(act="following", status=0, ver=ver, head_in=head_in),
        body=body,
    )


def following_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn,
) -> FollowingResponse:
    return FollowingResponse(
        head=make_head(act="following", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def profile_ok(
    *,
    body: ProfileBodyOut,
    ver: str,
    head_in: ProtocolHeadIn,
) -> ProfileResponse:
    return ProfileResponse(
        head=make_head(act="profile", status=0, ver=ver, head_in=head_in),
        body=body,
    )


def profile_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn,
) -> ProfileResponse:
    return ProfileResponse(
        head=make_head(act="profile", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def profile_update_ok(
    *,
    body: ProfileBodyOut,
    ver: str,
    head_in: ProtocolHeadIn,
) -> ProfileUpdateResponse:
    return ProfileUpdateResponse(
        head=make_head(act="profile_update", status=0, ver=ver, head_in=head_in),
        body=body,
    )


def profile_update_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn,
) -> ProfileUpdateResponse:
    return ProfileUpdateResponse(
        head=make_head(act="profile_update", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def avatar_ok(
    *,
    body: ProfileBodyOut,
    ver: str,
    head_in: ProtocolHeadIn | None = None,
) -> AvatarResponse:
    return AvatarResponse(
        head=make_head(act="avatar", status=0, ver=ver, head_in=head_in),
        body=body,
    )


def avatar_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn | None = None,
) -> AvatarResponse:
    return AvatarResponse(
        head=make_head(act="avatar", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def followers_ok(
    *,
    body: FollowersBodyOut,
    ver: str,
    head_in: ProtocolHeadIn,
) -> FollowersResponse:
    return FollowersResponse(
        head=make_head(act="followers", status=0, ver=ver, head_in=head_in),
        body=body,
    )


def followers_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn,
) -> FollowersResponse:
    return FollowersResponse(
        head=make_head(act="followers", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def user_profile_ok(
    *,
    body: PublicProfileBodyOut,
    ver: str,
    head_in: ProtocolHeadIn,
) -> UserProfileResponse:
    return UserProfileResponse(
        head=make_head(act="user_profile", status=0, ver=ver, head_in=head_in),
        body=body,
    )


def user_profile_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn,
) -> UserProfileResponse:
    return UserProfileResponse(
        head=make_head(act="user_profile", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def birthday_ok(
    *,
    body: BirthdayBodyOut,
    ver: str,
    head_in: ProtocolHeadIn,
) -> BirthdayResponse:
    return BirthdayResponse(
        head=make_head(act="birthday", status=0, ver=ver, head_in=head_in),
        body=body,
    )


def birthday_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn,
    body: BirthdayBodyOut | None = None,
) -> BirthdayResponse:
    return BirthdayResponse(
        head=make_head(act="birthday", status=status, ver=ver, head_in=head_in),
        body=body if body is not None else EmptyBody(),
    )


def deactivate_ok(
    *,
    body: DeactivateBodyOut,
    ver: str,
    head_in: ProtocolHeadIn,
) -> DeactivateResponse:
    return DeactivateResponse(
        head=make_head(act="deactivate", status=0, ver=ver, head_in=head_in),
        body=body,
    )


def deactivate_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn,
) -> DeactivateResponse:
    return DeactivateResponse(
        head=make_head(act="deactivate", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def user_videos_ok(
    *,
    body: VideoBodyOut,
    ver: str,
    head_in: ProtocolHeadIn,
) -> UserVideosResponse:
    return UserVideosResponse(
        head=make_head(act="user_videos", status=0, ver=ver, head_in=head_in),
        body=body,
    )


def user_videos_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn,
) -> UserVideosResponse:
    return UserVideosResponse(
        head=make_head(act="user_videos", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def my_videos_ok(
    *,
    body: VideoBodyOut,
    ver: str,
    head_in: ProtocolHeadIn,
) -> MyVideosResponse:
    return MyVideosResponse(
        head=make_head(act="my_videos", status=0, ver=ver, head_in=head_in),
        body=body,
    )


def my_videos_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn,
) -> MyVideosResponse:
    return MyVideosResponse(
        head=make_head(act="my_videos", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )


def following_feed_ok(
    *,
    body: VideoBodyOut,
    ver: str,
    head_in: ProtocolHeadIn,
) -> FollowingFeedResponse:
    return FollowingFeedResponse(
        head=make_head(act="following_feed", status=0, ver=ver, head_in=head_in),
        body=body,
    )


def following_feed_error(
    *,
    status: int = 100,
    ver: str,
    head_in: ProtocolHeadIn,
) -> FollowingFeedResponse:
    return FollowingFeedResponse(
        head=make_head(act="following_feed", status=status, ver=ver, head_in=head_in),
        body=EmptyBody(),
    )
