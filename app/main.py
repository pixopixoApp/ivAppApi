from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.auth_user import AppAuthError
from app.config import get_settings
from app.db import init_db
from app.logging_config import get_logger, setup_logging
from app.protocol_envelope import auth_fail_payload
from app.routers import admin, feed, user

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    setup_logging(level=settings.log_level)
    log.info("starting ivapp log_level=%s", settings.log_level)
    Path(settings.media_root).mkdir(parents=True, exist_ok=True)
    init_db()
    log.info("database ready media_root=%s", settings.media_root)
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
app.include_router(feed.public_router)
app.include_router(feed.auth_router)
app.include_router(user.upload_router)
app.include_router(user.auth_router)


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
