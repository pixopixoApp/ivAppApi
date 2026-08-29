from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore")

    database_url: str
    pixo_environment: Literal["development", "production"] | None = None
    rds_host: str = ""
    publish_key: str
    cursor_secret: str = ""
    server_ver: str = "1.2"
    log_level: str = "INFO"
    # Where published mp4 files live (compose bind: ./volumes → /volumes)
    media_root: str = "/volumes/media"

    # Shared local-first store. ivapp writes creator ingress here and ivadmin
    # reads the exact same immutable SHA object from its matching bind mount.
    media_cache_enabled: bool = False
    media_cache_root: str = "/data/media-cache"
    media_cache_min_free_bytes: int = 20 * 1024 * 1024 * 1024
    creator_local_upload_enabled: bool = False
    creator_local_upload_chunk_bytes: int = 8 * 1024 * 1024
    creator_local_upload_ttl_seconds: int = 3600
    creator_legacy_oss_upload_enabled: bool = True

    # Canonical media storage. ``local`` remains available only for the
    # additive migration/read-compatibility release; production cutover uses
    # ``oss`` and removes the media bind mount.
    media_storage_mode: str = "local"
    media_read_fallback_local: bool = True
    aliyun_oss_region: str = Field(
        default="",
        validation_alias=AliasChoices("ALIYUN_OSS_REGION", "MOTIONCUE_ALIYUN_OSS_REGION"),
    )
    aliyun_oss_bucket: str = Field(
        default="",
        validation_alias=AliasChoices("ALIYUN_OSS_BUCKET", "MOTIONCUE_ALIYUN_OSS_BUCKET"),
    )
    aliyun_oss_access_key_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ALIYUN_OSS_ACCESS_KEY_ID",
            "MOTIONCUE_ALIYUN_OSS_ACCESS_KEY_ID",
        ),
    )
    aliyun_oss_access_key_secret: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ALIYUN_OSS_ACCESS_KEY_SECRET",
            "MOTIONCUE_ALIYUN_OSS_ACCESS_KEY_SECRET",
        ),
    )
    aliyun_oss_public_base_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ALIYUN_OSS_PUBLIC_BASE_URL",
            "MOTIONCUE_ALIYUN_OSS_PUBLIC_BASE_URL",
        ),
    )
    # Comma-separated compatibility origins whose immutable public object URLs
    # are canonicalized to ALIYUN_OSS_PUBLIC_BASE_URL on every API read.
    public_media_legacy_origins: str = ""
    # Public browser/WebView origin for immutable HTML packages. This may be a
    # first-party Nginx/CDN origin even when other media uses the OSS origin.
    html_public_base_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "HTML_PUBLIC_BASE_URL",
            "PIXO_HTML_PUBLIC_BASE_URL",
        ),
    )
    oss_root_prefix: str = "ivapp-media/v1"
    oss_connect_timeout_seconds: float = 10.0
    oss_upload_ttl_seconds: int = 600
    oss_private_get_ttl_seconds: int = 300
    oss_max_concurrency: int = 16
    # Type-A authenticated CDN origin for private draft/original playback.
    # The CDN origin must be configured to read the private OSS bucket.
    private_media_cdn_base_url: str = ""
    private_media_cdn_auth_key: str = ""
    private_media_cdn_auth_uid: str = "0"
    private_media_cdn_ttl_seconds: int = 900

    # Email auth — 阿里企业邮箱（465 SSL）
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: bool = False
    smtp_ssl: bool = True
    code_ttl_seconds: int = 600
    token_ttl_days: int = 30
    send_code_interval_seconds: int = 60
    # Google Sign-In：校验 id_token 的 audience（Web Client ID）；secret 本期备用
    google_client_id: str = ""
    # Comma-separated Android/iOS OAuth client IDs.  Keep the legacy singular
    # field during migration so existing Android deployments remain valid.
    google_client_ids: str = ""
    web_google_client_id: str = ""
    google_client_secret: str = ""
    google_timeout_seconds: float = 5.0

    # Redis（曝光去重池）
    redis_url: str = "redis://redis:6379/0"

    # Creator orchestration. ivapp owns C-end state while ivadmin exclusively
    # owns ivcore/model/Dify execution.
    ivadmin_base_url: str = "http://host.docker.internal:8000"
    creator_internal_key: str = ""
    creator_ivadmin_timeout_seconds: float = 30.0
    creator_worker_poll_seconds: float = 2.0
    creator_video_max_bytes: int = 120 * 1024 * 1024
    creator_video_max_duration_seconds: int = 30
    creator_text_to_video_enabled: bool = False
    creator_video_daily_quota: int = 3
    creator_video_draft_ttl_days: int = 30
    creator_access_mode: Literal[
        "invite", "web_open", "android_open", "all_open"
    ] = "invite"
    public_share_base_url: str = ""
    # Canonical browser player used for shareable Runtime permalinks.
    public_game_base_url: str = "https://demo.pixopixo.cn/game/"

    # Immutable, reviewed HTML packages. Comma-separated HTTPS origins only.
    html_trusted_origins: str = ""
    html_publish_probe_timeout_seconds: float = 5.0
    # ZIP imports are transient: source archives stay private in OSS while
    # extraction happens in a temporary directory only.
    html_import_max_zip_bytes: int = 512 * 1024 * 1024
    html_import_max_unpacked_bytes: int = 2 * 1024 * 1024 * 1024
    html_import_max_files: int = 1000
    html_import_require_playwright: bool = True
    # Shared host-mounted spool. ivadmin writes source.zip; ivapp only resolves
    # it from a validated import id and never accepts a caller-supplied path.
    html_import_spool_root: str = "/data/html-imports"
    html_import_progress_timeout_seconds: float = 10.0

    # CDN cache work is persisted in MySQL and submitted asynchronously. The
    # worker uses an ECS RAM role or an explicitly configured CDN-capable RAM
    # key. A dedicated least-privilege principal remains preferred.
    cdn_cache_enabled: bool = False
    cdn_prefetch_on_publish: bool = True
    cdn_domain: str = ""
    cdn_api_region: str = "cn-hangzhou"
    # Prefer an ECS RAM role. These optional keys also support an existing
    # runtime RAM principal after operations intentionally grants CDN access.
    aliyun_cdn_access_key_id: str = ""
    aliyun_cdn_access_key_secret: str = ""
    cdn_worker_poll_seconds: float = 2.0
    cdn_provider_poll_seconds: float = 10.0
    cdn_worker_batch_size: int = 50
    cdn_worker_max_attempts: int = 6
    cdn_worker_lease_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()


def validate_environment_contract(settings: Settings) -> None:
    """Fail closed when an explicitly configured runtime crosses environments."""
    environment = settings.pixo_environment
    if environment is None:
        return

    host = (make_url(settings.database_url).host or "").strip().lower()
    local_hosts = {"mysql", "ivapp-mysql", "localhost", "127.0.0.1"}
    if environment == "development":
        if host not in local_hosts:
            raise RuntimeError("development ivapp must use the local Docker MySQL service")
        if settings.public_share_base_url.rstrip("/") != "https://api.pixopixo.cn":
            raise RuntimeError("development ivapp must publish .cn share URLs")
        if settings.public_game_base_url.rstrip("/") != "https://demo.pixopixo.cn/game":
            raise RuntimeError("development ivapp must use demo.pixopixo.cn/game")
        return

    expected_rds_host = settings.rds_host.strip().lower()
    if host in local_hosts or not host or not expected_rds_host or host != expected_rds_host:
        raise RuntimeError("production ivapp must use the private RDS endpoint")
    if settings.public_share_base_url.rstrip("/") != "https://api.pixopixo.com":
        raise RuntimeError("production ivapp must publish .com share URLs")
    if settings.public_game_base_url.rstrip("/") != "https://www.pixopixo.com":
        raise RuntimeError("production ivapp must use www.pixopixo.com")
    if settings.aliyun_oss_public_base_url.rstrip("/") != "https://video.pixopixo.cn":
        raise RuntimeError(
            "production media must remain on video.pixopixo.cn until the CDN cutover"
        )
