from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)


class ProtocolHeadIn(BaseModel):
    """协议 0.3 请求头（默认值对齐客户端示例）。"""

    act: str = Field(default="video", description="动作名")
    ver: str = Field(default="1.2", description="客户端版本号")
    time: str | None = Field(default="2024-06-19 12:00:00", description="客户端时间")
    token: str = Field(default="", description="用户 token；也可改用 Authorization: Bearer")
    ssid: str | None = Field(default=None, description="会话 ID，可选；服务端可回填")


class AuthProtocolHeadIn(BaseModel):
    """登录前接口请求头：无 token。"""

    act: str = Field(default="send_code", description="动作名")
    ver: str = Field(default="1.2", description="客户端版本号")
    time: str | None = Field(default="2024-06-19 12:00:00", description="客户端时间")
    ssid: str | None = Field(default=None, description="会话 ID，可选；服务端可回填")


class ProtocolHeadOut(BaseModel):
    """协议 0.3 响应头。"""

    act: str = Field(description="动作名")
    status: int = Field(description="0 成功，非 0 失败")
    ver: str = Field(description="服务端版本号")
    time: str = Field(description="服务端时间")
    ssid: str = Field(description="会话 ID")
    error_code: str | None = Field(default=None, description="机器可读错误码")
    message: str | None = Field(default=None, description="可直接展示的简短友好提示")
    retry_after_seconds: int | None = Field(default=None, description="建议重试等待秒数")

    @model_serializer(mode="wrap")
    def _omit_nulls(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        return {k: v for k, v in data.items() if v is not None}


class EmptyBody(BaseModel):
    """错误时的空 body（序列化为 {}）。"""

    model_config = ConfigDict(extra="forbid")


class Region(BaseModel):
    model_config = ConfigDict(extra="ignore")

    x: float
    y: float
    w: float
    h: float


class TimelineInteraction(BaseModel):
    """ivcore timeline.interactions[] 条目；多余字段保留。"""

    model_config = ConfigDict(extra="allow")

    gesture: str | None = None
    gate_at_ms: int | None = None
    gate_end_ms: int | None = Field(
        default=None, description="交互生效结束（绝对毫秒）；协议侧可换算为 response_window_ms"
    )
    reaction_start_ms: int | None = None
    reaction_end_ms: int | None = None
    hint: str | None = None
    pause_video: bool = True
    vision: dict[str, Any] | None = Field(
        default=None,
        description="camera_motion 的受控端侧视觉识别配置",
    )
    region: Region | None = None


class Timeline(BaseModel):
    """发布时写入的 timeline；允许 media/sequences 等扩展字段。"""

    model_config = ConfigDict(extra="allow")

    interactions: list[TimelineInteraction] = Field(default_factory=list)


class PublishRequest(BaseModel):
    video_id: str = Field(min_length=1, max_length=128, description="视频幂等键，建议用 run_id")
    video_url: str = Field(min_length=1, description="相对路径，如 /media/{video_id}.mp4")
    version: str = Field(min_length=1, max_length=64, description="本次发布内容版本（由 ivadmin 传入）")
    timeline: Timeline


class PublishResponse(BaseModel):
    video_id: str = Field(description="发布单元幂等键（item_id）")
    version: str = Field(description="内容版本号")
    video_url: str = Field(description="入口视频相对路径")
    user_id: str = Field(description="作者 user_id")
    content_mode: str = Field(description="内容模式：single 或 story")
    updated: bool = Field(description="是否覆盖已有发布（false=新建）")
    runtime_spec_version: str = Field(description="已持久化的播放协议版本")
    publication_id: str = Field(default="", description="不可变发布版本标识")
    cdn_status: Literal["ready", "warming"] = Field(
        default="ready",
        description="ready 后客户端才可见；warming 时旧版本继续服务",
    )
    poll_after_ms: int = Field(default=0, ge=0)
    content_type: Literal["runtime"] = "runtime"


class PublishHtmlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=128)
    package_id: str | None = Field(default=None, min_length=1, max_length=64)
    version: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="不可变内容包 SHA-256（小写十六进制）",
    )
    html_url: str = Field(min_length=1, max_length=2048)
    bridge_version: Literal[1]
    required_capabilities: list[str] = Field(default_factory=list, max_length=5)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1200)
    user_id: str = Field(min_length=1, max_length=64)
    feed_weight: int = 0


class PublishHtmlResponse(BaseModel):
    item_id: str
    version: str
    html_url: str
    bridge_version: Literal[1]
    required_capabilities: list[str]
    user_id: str
    content_type: Literal["html"] = "html"
    updated: bool


class UnpublishResponse(BaseModel):
    video_id: str = Field(description="已取消发布的视频 id")
    deleted: bool = Field(description="是否已删除")


class PublishedVideoInfo(BaseModel):
    video_id: str = Field(description="发布单元幂等键（item_id）")
    version: str = Field(description="内容版本号")
    content_type: Literal["runtime", "html"] = "runtime"
    video_url: str | None = Field(default=None, description="Runtime 入口视频相对路径")
    html_url: str | None = Field(default=None, description="HTML HTTPS 入口")
    bridge_version: int | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    user_id: str | None = Field(default=None, description="作者 user_id；存量可空")
    content_mode: str = Field(default="single", description="内容模式：single 或 story")
    feed_weight: int = Field(default=0, description="Feed 权重，越大越靠前")
    distribution_enabled: bool = Field(default=True, description="是否允许向 App / 公开接口分发")
    is_tutorial: bool = Field(default=False, description="是否教学片（全站至多一条，未看时 Feed 置顶）")
    runtime_spec_version: str | None = Field(default=None, description="已持久化的播放协议版本")
    created_at: str = Field(description="创建时间 ISO8601")
    updated_at: str = Field(description="更新时间 ISO8601")


class RuntimeSpecAuditOut(BaseModel):
    total: int
    ready: int
    missing_video_ids: list[str] = Field(default_factory=list)
    invalid: dict[str, str] = Field(default_factory=dict)


class FeedWeightUpdateRequest(BaseModel):
    feed_weight: int | None = Field(default=None, description="Feed 权重，越大越靠前；不传则不改")
    is_tutorial: bool | None = Field(
        default=None,
        description="是否教学片；不传则不改；true 时清其它片的教学标记",
    )
    distribution_enabled: bool | None = Field(
        default=None,
        description="是否允许向 App / 公开接口分发；false 时自动取消教学片标记",
    )


class ContentManagementUpdateRequest(BaseModel):
    """运营后台编辑已生成内容的受控字段。

    ``timeline`` 始终在服务端重新编译为 runtime_spec；客户端无法直接写入
    runtime_spec，从而避免展示配置和实际播放器协议失去一致性。
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1200)
    timeline: dict[str, Any] | None = None
    review_status: Literal["draft", "approved"] | None = None
    # 封面 media object id（已通过 /internal/v1/publish-cover 上传）；发布后换封面时由 ivadmin 回填。
    cover_media_object_id: str | None = Field(default=None, max_length=64)


class UserImpressionsOut(BaseModel):
    user_id: str = Field(description="用户 id")
    count: int = Field(description="去重池大小")
    video_ids: list[str] = Field(description="已曝光 video_id 列表（Redis Set）")


class AdminUserUpsertRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64, description="用户稳定 id")
    provider: str = Field(
        default="email", min_length=1, max_length=32, description="登录方式，默认 email"
    )
    subject: str = Field(
        min_length=1, max_length=256, description="登录主体（邮箱登录时为邮箱）"
    )
    enabled: bool | None = Field(default=None, description="是否启用；不传则更新时保持原值")
    nickname: str | None = Field(default=None, max_length=64, description="昵称；传空串可清空")
    avatar_url: str | None = Field(
        default=None,
        max_length=512,
        description="头像相对路径，如 /media/avatars/x.png；传空串可清空",
    )
    bio: str | None = Field(default=None, max_length=80, description="个人介绍；传空串可清空")


class AdminUserOut(BaseModel):
    user_id: str = Field(description="用户稳定 id")
    provider: str = Field(description="登录方式")
    subject: str = Field(description="登录主体")
    enabled: bool = Field(description="是否启用")
    nickname: str = Field(default="", description="昵称")
    avatar_url: str = Field(default="", description="头像相对路径")
    bio: str = Field(default="", description="个人介绍")
    source: str = Field(description="创建来源：app=真实用户，admin=管理后台")
    created_at: str = Field(description="创建时间 ISO8601")


class AdminUserDeactivateResponse(BaseModel):
    user_id: str = Field(description="用户 id")
    enabled: bool = Field(description="停用后为 false")


class BatchUsersRequest(BaseModel):
    user_ids: list[str] = Field(
        min_length=1, max_length=200, description="要查询的 user_id 列表"
    )


class BatchUsersResponse(BaseModel):
    items: list[AdminUserOut] = Field(description="查到的用户列表")
    missing: list[str] = Field(default_factory=list, description="未找到的 user_id")


class AdminUserListResponse(BaseModel):
    items: list[AdminUserOut] = Field(description="本页用户列表")
    total: int = Field(description="符合筛选条件的总条数")
    limit: int = Field(description="本页条数上限")
    offset: int = Field(description="偏移量")


class BatchVideosRequest(BaseModel):
    video_ids: list[str] = Field(min_length=1, max_length=200, description="要查询的 video_id 列表")


class BatchVideosResponse(BaseModel):
    items: list[PublishedVideoInfo]
    missing: list[str] = Field(default_factory=list, description="未找到的 video_id")


class RuntimeCapabilitiesIn(BaseModel):
    """Client-declared runtime versions; omission deliberately means legacy."""

    supported_experience_spec_versions: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=8,
        description=(
            "客户端可解析的 ExperienceSpec 版本；不传按旧客户端 1.0/1.1 处理"
        ),
    )

    @field_validator("supported_experience_spec_versions")
    @classmethod
    def _validate_runtime_versions(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized: list[str] = []
        for raw in value:
            version = raw.strip()
            if not version or len(version) > 16:
                raise ValueError("experience spec versions must be 1-16 characters")
            if version not in normalized:
                normalized.append(version)
        return normalized


class VideoBodyIn(RuntimeCapabilitiesIn):
    """App 拉视频列表；可空。limit 控制本批条数。"""

    limit: int = Field(default=10, ge=1, le=50, description="本批返回视频条数")
    cursor: str | None = Field(default=None, max_length=1024, description="服务端返回的不透明游标")


class VideoRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=ProtocolHeadIn)
    body: VideoBodyIn = Field(default_factory=VideoBodyIn)


class DetectionOut(BaseModel):
    """按交互类型可变的 detection；未知扩展字段允许透传，序列化排除 null。"""

    model_config = ConfigDict(extra="allow")

    confidence_threshold: float = Field(description="容错系数")
    response_window_ms: int = Field(description="输入就绪后的总响应窗口，毫秒")
    place: str = Field(description="引导/命中位置")
    min_duration_ms: int | None = None
    min_volume_score: int | None = None
    max_volume_score: int | None = None
    min_distance_dp: int | None = None
    min_travel_dp: int | None = None
    idle_timeout_ms: int | None = None
    min_scale_delta: float | None = None
    min_radius_dp: int | None = None
    max_closure_gap_dp: int | None = None
    min_motion_score: int | None = None
    max_motion_score: int | None = None
    min_angle_deg: float | None = None
    min_shake_score: int | None = None

    @model_serializer(mode="wrap")
    def _omit_nulls(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        return {k: v for k, v in data.items() if v is not None}


class FeedbackOut(BaseModel):
    animation: str
    animation_duration_ms: int
    vibrate: bool
    sound_effect: str


class ActionOut(BaseModel):
    """ExperienceSpec 结果动作；序列化排除 null，避免携带无关字段。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "continue",
        "retry_previous_point",
        "restart_video",
        "jump_video",
        "end_experience",
    ] = Field(
        description="continue | retry_previous_point | restart_video | jump_video | end_experience"
    )
    target_video_id: str | None = Field(
        default=None, description="jump_video 必填：同 item 内目标 video_id"
    )
    timing: Literal["immediate", "video_end"] | None = Field(
        default=None, description="jump_video 必填：immediate 或 video_end"
    )

    @model_validator(mode="after")
    def _validate_action_shape(self) -> ActionOut:
        if self.action == "jump_video":
            if not self.target_video_id or self.timing is None:
                raise ValueError("jump_video requires target_video_id and timing")
        elif self.action == "end_experience":
            if self.target_video_id is not None or self.timing is None:
                raise ValueError("end_experience requires timing and forbids target_video_id")
        elif self.target_video_id is not None or self.timing is not None:
            raise ValueError(f"{self.action} forbids target_video_id and timing")
        return self

    @model_serializer(mode="wrap")
    def _omit_nulls(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        return {k: v for k, v in data.items() if v is not None}


class InteractionOut(BaseModel):
    id: str
    type: str
    description: str
    offset_time_ms: int
    pause_video: bool
    detection: DetectionOut
    feedback: FeedbackOut
    on_success: ActionOut = Field(description="成功时的结果动作")
    on_miss: ActionOut = Field(description="失败/错过时的结果动作")


class ClipOut(BaseModel):
    video_id: str = Field(description="本段 clip id")
    video: str = Field(description="视频相对路径")
    interactions: list[InteractionOut] = Field(description="互动点列表（含分支 Action）")
    on_end: ActionOut | None = Field(
        default=None,
        description="v1.1 本段播完后的动作；复用同一 Action 联合",
    )

    @model_serializer(mode="wrap")
    def _omit_nulls(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        return {k: v for k, v in data.items() if v is not None}


class FeedItemOut(BaseModel):
    item_id: str = Field(description="发布单元 id（publish 的 video_id）")
    content_type: Literal["runtime", "html"] = Field(description="播放协议类型")
    title: str = Field(default="", description="作品标题")
    description: str = Field(default="", description="作品描述")
    share_url: str = Field(default="", description="可分享的作品落地页")
    user_id: str | None = Field(default=None, description="作者 user_id；存量可空")
    nickname: str = Field(default="", description="作者昵称；无作者或未设置则为空串")
    avatar_url: str = Field(default="", description="作者头像相对路径；无作者或未设置则为空串")
    play_count: int = Field(default=0, ge=0, description="去重登录用户播放量")
    is_following: bool = Field(default=False, description="当前登录用户是否关注作者")
    viewer_following_author: bool = Field(default=False, description="is_following 的兼容字段")
    following: bool = Field(default=False, description="is_following 的兼容字段")
    experience_spec_version: Literal["1.0", "1.1", "1.2"] | None = Field(
        default=None,
        description="Runtime ExperienceSpec 版本；HTML 内容不携带",
    )
    video: list[ClipOut] | None = Field(
        default=None, description="Runtime：单视频 length=1；Story 多段，入口 clip 在首位"
    )
    html_url: str | None = Field(default=None, description="HTML：受信 HTTPS 入口")
    bridge_version: int | None = Field(default=None, description="HTML Bridge 版本")
    required_capabilities: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_content_payload(self) -> FeedItemOut:
        if self.content_type == "runtime":
            if not self.video:
                raise ValueError("runtime feed item requires video")
            if self.experience_spec_version not in {"1.0", "1.1", "1.2"}:
                raise ValueError("runtime feed item requires a supported experience_spec_version")
            if self.html_url is not None or self.bridge_version is not None:
                raise ValueError("runtime feed item cannot contain HTML payload")
        else:
            if self.video is not None:
                raise ValueError("html feed item cannot contain runtime video")
            if self.experience_spec_version is not None:
                raise ValueError("html feed item cannot contain experience_spec_version")
            if not self.html_url or self.bridge_version != 1:
                raise ValueError("html feed item requires html_url and bridge_version=1")
        return self

    @model_serializer(mode="wrap")
    def _omit_inapplicable_fields(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        return {key: value for key, value in data.items() if value is not None}


class VideoBodyOut(BaseModel):
    items: list[FeedItemOut] = Field(description="Feed 发布单元列表")
    next_cursor: str | None = Field(default=None, description="下一批不透明游标")
    has_more: bool = Field(default=False, description="是否可继续请求")
    is_circular: bool = Field(default=False, description="是否为可循环的无限推荐流")


class VideoResponse(BaseModel):
    head: ProtocolHeadOut
    body: VideoBodyOut | EmptyBody


class VideoDetailBodyIn(RuntimeCapabilitiesIn):
    video_id: str = Field(
        min_length=1,
        max_length=128,
        description="发布单元 id（item_id，即 publish 的 video_id）",
    )


def _video_detail_head() -> ProtocolHeadIn:
    return ProtocolHeadIn(act="video_detail")


class VideoDetailRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=_video_detail_head)
    body: VideoDetailBodyIn


class VideoDetailResponse(BaseModel):
    head: ProtocolHeadOut
    body: VideoBodyOut | EmptyBody


class ImpressionBodyIn(BaseModel):
    video_id: str = Field(
        min_length=1,
        max_length=128,
        description="已曝光的发布单元 item_id",
    )


class ImpressionRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=ProtocolHeadIn)
    body: ImpressionBodyIn


class ImpressionResponse(BaseModel):
    head: ProtocolHeadOut
    body: EmptyBody


class TrackBodyIn(BaseModel):
    video_id: str = Field(min_length=1, max_length=128, description="发布视频 id（publish id）")
    data: str = Field(description="埋点字符串")


class TrackRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=ProtocolHeadIn)
    body: TrackBodyIn


class TrackResponse(BaseModel):
    head: ProtocolHeadOut
    body: EmptyBody


class AnalyticsLogOut(BaseModel):
    id: int = Field(description="日志自增 id")
    video_id: str = Field(description="发布视频 id")
    token: str = Field(description="客户端 token（游客可为 anonymous）")
    data: str = Field(description="埋点内容")
    created_at: str = Field(description="创建时间 ISO8601")


class AnalyticsLogsResponse(BaseModel):
    items: list[AnalyticsLogOut] = Field(description="埋点列表")


class SendCodeBodyIn(BaseModel):
    email: str = Field(description="接收验证码的邮箱")


class SendCodeRequest(BaseModel):
    head: AuthProtocolHeadIn = Field(default_factory=AuthProtocolHeadIn)
    body: SendCodeBodyIn


class SendCodeResponse(BaseModel):
    head: ProtocolHeadOut
    body: EmptyBody


def _deactivate_send_code_head() -> ProtocolHeadIn:
    return ProtocolHeadIn(act="deactivate_send_code")


class DeactivateSendCodeRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=_deactivate_send_code_head)
    body: EmptyBody = Field(default_factory=EmptyBody)


class DeactivateSendCodeResponse(BaseModel):
    head: ProtocolHeadOut
    body: EmptyBody


class VerifyBodyIn(BaseModel):
    email: str = Field(description="邮箱")
    code: str = Field(min_length=6, max_length=6, description="6 位数字验证码")


def _verify_head() -> AuthProtocolHeadIn:
    return AuthProtocolHeadIn(act="verify")


class VerifyRequest(BaseModel):
    head: AuthProtocolHeadIn = Field(default_factory=_verify_head)
    body: VerifyBodyIn


class VerifyBodyOut(BaseModel):
    token: str = Field(description="会话 token")
    user_id: str = Field(description="用户稳定 id")
    email: str = Field(description="登录邮箱")
    expires_at: str = Field(description="token 过期时间 ISO8601")
    needs_birthday: bool = Field(
        description="是否需要设置生日（birthday 为空时为 true）"
    )
    birthday: str = Field(default="", description="已保存生日；未设置为空串")
    is_under_13: bool | None = Field(default=None, description="已设置生日时是否未满 13 岁")


class VerifyResponse(BaseModel):
    head: ProtocolHeadOut
    body: VerifyBodyOut | EmptyBody


class GoogleLoginBodyIn(BaseModel):
    id_token: str = Field(min_length=1, description="Google Sign-In 返回的 ID Token JWT")


def _google_login_head() -> AuthProtocolHeadIn:
    return AuthProtocolHeadIn(act="google_login")


class GoogleLoginRequest(BaseModel):
    head: AuthProtocolHeadIn = Field(default_factory=_google_login_head)
    body: GoogleLoginBodyIn


class GoogleLoginResponse(BaseModel):
    """成功 body 与 /verify 同形。"""

    head: ProtocolHeadOut
    body: VerifyBodyOut | EmptyBody


class FollowBodyIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=64, description="被关注用户的 user_id")


class FollowRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=ProtocolHeadIn)
    body: FollowBodyIn


class FollowResponse(BaseModel):
    head: ProtocolHeadOut
    body: EmptyBody


class UnfollowRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=ProtocolHeadIn)
    body: FollowBodyIn


class UnfollowResponse(BaseModel):
    head: ProtocolHeadOut
    body: EmptyBody


class FollowingBodyIn(BaseModel):
    user_id: str | None = Field(
        default=None,
        max_length=64,
        description="目标用户 id；空则查当前登录用户",
    )
    limit: int = Field(default=50, ge=1, le=200, description="返回关注条数上限")
    cursor: str | None = Field(default=None, max_length=1024, description="下一页不透明游标")


class FollowingRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=ProtocolHeadIn)
    body: FollowingBodyIn = Field(default_factory=FollowingBodyIn)


class FollowingItemOut(BaseModel):
    user_id: str = Field(description="用户 id")
    nickname: str = Field(default="", description="昵称")
    avatar_url: str = Field(default="", description="头像相对路径")
    created_at: str = Field(description="关注关系创建时间 ISO8601")


class FollowingBodyOut(BaseModel):
    items: list[FollowingItemOut] = Field(description="关注列表")
    next_cursor: str | None = None
    has_more: bool = False


class FollowingResponse(BaseModel):
    head: ProtocolHeadOut
    body: FollowingBodyOut | EmptyBody


class FollowersBodyIn(BaseModel):
    user_id: str | None = Field(
        default=None,
        max_length=64,
        description="目标用户 id；空则查当前登录用户",
    )
    limit: int = Field(default=50, ge=1, le=200, description="返回粉丝条数上限")
    cursor: str | None = Field(default=None, max_length=1024, description="下一页不透明游标")


def _followers_head() -> ProtocolHeadIn:
    return ProtocolHeadIn(act="followers")


class FollowersRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=_followers_head)
    body: FollowersBodyIn = Field(default_factory=FollowersBodyIn)


class FollowersBodyOut(BaseModel):
    items: list[FollowingItemOut] = Field(description="粉丝列表（字段同 following item）")
    next_cursor: str | None = None
    has_more: bool = False


class FollowersResponse(BaseModel):
    head: ProtocolHeadOut
    body: FollowersBodyOut | EmptyBody


class ProfileBodyOut(BaseModel):
    user_id: str = Field(description="用户稳定 id")
    nickname: str = Field(default="", description="昵称")
    avatar_url: str = Field(default="", description="头像相对路径")
    bio: str = Field(default="", description="个人介绍")
    email: str = Field(default="", description="邮箱（邮箱登录时为 subject）")
    enabled: bool = Field(default=True, description="是否启用")
    following_count: int = Field(default=0, description="关注数")
    follower_count: int = Field(default=0, description="粉丝数")


def _profile_head() -> ProtocolHeadIn:
    return ProtocolHeadIn(act="profile")


class ProfileRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=_profile_head)
    body: EmptyBody = Field(default_factory=EmptyBody)


class ProfileResponse(BaseModel):
    head: ProtocolHeadOut
    body: ProfileBodyOut | EmptyBody


class ProfileUpdateBodyIn(BaseModel):
    nickname: str | None = Field(default=None, max_length=64, description="昵称；不传则不改")
    avatar_url: str | None = Field(
        default=None,
        max_length=512,
        description="头像相对路径（以 / 开头）；不传则不改；空串清空",
    )
    bio: str | None = Field(default=None, max_length=80, description="个人介绍；传空串可清空")


def _profile_update_head() -> ProtocolHeadIn:
    return ProtocolHeadIn(act="profile_update")


class ProfileUpdateRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=_profile_update_head)
    body: ProfileUpdateBodyIn = Field(default_factory=ProfileUpdateBodyIn)


class ProfileUpdateResponse(BaseModel):
    head: ProtocolHeadOut
    body: ProfileBodyOut | EmptyBody


class AvatarResponse(BaseModel):
    """上传头像成功后回资料同形。"""

    head: ProtocolHeadOut
    body: ProfileBodyOut | EmptyBody


class PublicProfileBodyOut(BaseModel):
    """他人公开资料（不含邮箱）。"""

    user_id: str = Field(description="用户稳定 id")
    nickname: str = Field(default="", description="昵称")
    avatar_url: str = Field(default="", description="头像相对路径")
    bio: str = Field(default="", description="个人介绍")
    enabled: bool = Field(default=True, description="是否启用")
    following_count: int = Field(default=0, description="关注数")
    follower_count: int = Field(default=0, description="粉丝数")
    is_following: bool = Field(
        default=False, description="当前登录用户是否已关注该用户"
    )


class UserProfileBodyIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=64, description="目标用户 id")


def _user_profile_head() -> ProtocolHeadIn:
    return ProtocolHeadIn(act="user_profile")


class UserProfileRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=_user_profile_head)
    body: UserProfileBodyIn


class UserProfileResponse(BaseModel):
    head: ProtocolHeadOut
    body: PublicProfileBodyOut | EmptyBody


class BirthdayBodyIn(BaseModel):
    birthday: str = Field(
        min_length=10,
        max_length=10,
        description="生日 YYYY-MM-DD",
    )


def _birthday_head() -> ProtocolHeadIn:
    return ProtocolHeadIn(act="birthday")


class BirthdayRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=_birthday_head)
    body: BirthdayBodyIn


class BirthdayBodyOut(BaseModel):
    birthday: str = Field(default="", description="已保存的生日 YYYY-MM-DD；未通过年龄门时可为空")
    needs_birthday: bool = Field(description="是否仍需设置生日")
    passed: bool = Field(description="年龄门是否通过（≥13）")


class BirthdayResponse(BaseModel):
    head: ProtocolHeadOut
    body: BirthdayBodyOut | EmptyBody


def _deactivate_head() -> ProtocolHeadIn:
    return ProtocolHeadIn(act="deactivate")


class DeactivateBodyIn(BaseModel):
    code: str = Field(min_length=6, max_length=6, description="邮箱验证码（先调 /send_code）")


class DeactivateRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=_deactivate_head)
    body: DeactivateBodyIn


class DeactivateBodyOut(BaseModel):
    scheduled_delete_at: str = Field(description="计划删除时间 ISO8601（申请时刻 + 缓冲天）")


class DeactivateResponse(BaseModel):
    head: ProtocolHeadOut
    body: DeactivateBodyOut | EmptyBody


class UserVideosBodyIn(RuntimeCapabilitiesIn):
    user_id: str = Field(min_length=1, max_length=64, description="作者 user_id")
    limit: int = Field(default=10, ge=1, le=50, description="返回条数上限")
    cursor: str | None = Field(default=None, max_length=1024, description="下一页不透明游标")


def _user_videos_head() -> ProtocolHeadIn:
    return ProtocolHeadIn(act="user_videos")


class UserVideosRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=_user_videos_head)
    body: UserVideosBodyIn


class UserVideosResponse(BaseModel):
    head: ProtocolHeadOut
    body: VideoBodyOut | EmptyBody


class MyVideosBodyIn(RuntimeCapabilitiesIn):
    limit: int = Field(default=10, ge=1, le=50, description="返回条数上限")
    cursor: str | None = Field(default=None, max_length=1024, description="下一页不透明游标")


def _my_videos_head() -> ProtocolHeadIn:
    return ProtocolHeadIn(act="my_videos")


class MyVideosRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=_my_videos_head)
    body: MyVideosBodyIn = Field(default_factory=MyVideosBodyIn)


class MyVideosResponse(BaseModel):
    head: ProtocolHeadOut
    body: VideoBodyOut | EmptyBody


class FollowingFeedBodyIn(RuntimeCapabilitiesIn):
    limit: int = Field(default=10, ge=1, le=50, description="返回条数上限")
    cursor: str | None = Field(default=None, max_length=1024, description="下一页不透明游标")


def _following_feed_head() -> ProtocolHeadIn:
    return ProtocolHeadIn(act="following_feed")


class FollowingFeedRequest(BaseModel):
    head: ProtocolHeadIn = Field(default_factory=_following_feed_head)
    body: FollowingFeedBodyIn = Field(default_factory=FollowingFeedBodyIn)


class FollowingFeedResponse(BaseModel):
    head: ProtocolHeadOut
    body: VideoBodyOut | EmptyBody
