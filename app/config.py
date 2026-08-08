from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore")

    database_url: str
    publish_key: str
    server_ver: str = "1.2"
    log_level: str = "INFO"
    # Where published mp4 files live (compose bind: ./volumes → /volumes)
    media_root: str = "/volumes/media"

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
    # 账号删除缓冲天数（验证码通过后写 scheduled_delete_at）
    account_deletion_buffer_days: int = 30

    # Google Sign-In：校验 id_token 的 audience（Web Client ID）；secret 本期备用
    google_client_id: str = ""
    google_client_secret: str = ""

    # Redis（曝光去重池）
    redis_url: str = "redis://redis:6379/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
