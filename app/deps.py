from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException

from app.config import Settings, get_settings
from app.logging_config import get_logger

log = get_logger(__name__)


def require_publish_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_publish_key: Annotated[str | None, Header(alias="X-Publish-Key")] = None,
) -> None:
    if not x_publish_key or x_publish_key != settings.publish_key:
        log.warning("rejected: invalid X-Publish-Key")
        raise HTTPException(status_code=401, detail="invalid publish key")
