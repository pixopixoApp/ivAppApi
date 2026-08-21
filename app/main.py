from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.auth_user import AppAuthError
from app.cdn_cache import validate_cdn_config
from app.config import get_settings
from app.logging_config import get_logger, setup_logging
from app.oss_storage import validate_oss_config
from app.protocol_envelope import auth_fail_payload
from app.routers import admin, feed, media_storage, platform, safety, user

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    setup_logging(level=settings.log_level)
    log.info("starting ivapp log_level=%s", settings.log_level)
    media_mode = settings.media_storage_mode.strip().lower()
    if media_mode not in {"local", "oss"}:
        raise RuntimeError("MEDIA_STORAGE_MODE must be local or oss")
    if media_mode == "local":
        Path(settings.media_root).mkdir(parents=True, exist_ok=True)
    else:
        validate_oss_config(settings)
    validate_cdn_config(settings)
    log.info(
        "database migrations are managed by Alembic media_storage_mode=%s media_root=%s",
        settings.media_storage_mode,
        settings.media_root,
    )
    yield
    log.info("shutting down ivapp")


app = FastAPI(
    title="ivapp",
    version="0.1.0",
    description="互动短视频 C 端 API：登录、Feed、用户资料/关注、内部发布管理。",
    openapi_tags=[
        {"name": "feed", "description": "登录与 Feed（/video、/video_detail 可游客；/track 需登录）"},
        {"name": "user", "description": "需登录：资料与关注"},
        {"name": "admin", "description": "内部管理（Header: X-Publish-Key）"},
        {"name": "media", "description": "媒体文件直出"},
    ],
    lifespan=lifespan,
)
app.include_router(admin.router)
app.include_router(admin.media_router)
app.include_router(media_storage.router)
app.include_router(feed.public_router)
app.include_router(feed.auth_router)
app.include_router(user.upload_router)
app.include_router(user.auth_router)
app.include_router(platform.public_router)
app.include_router(platform.creator_router)
app.include_router(platform.operations_router)
app.include_router(safety.client_router)
app.include_router(safety.operations_router)


@app.exception_handler(AppAuthError)
async def app_auth_error_handler(_request: Request, exc: AppAuthError) -> JSONResponse:
    settings = get_settings()
    payload = auth_fail_payload(
        act=exc.act,
        status=exc.status,
        ver=settings.server_ver,
        head_in=exc.head_in,
    )
    return JSONResponse(status_code=200, content=payload)


@app.get(
    "/health",
    summary="健康检查",
    description="探活接口，返回 `{\"status\":\"ok\"}`。无需鉴权。",
)
def health() -> dict[str, str]:
    return {"status": "ok"}
