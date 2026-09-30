from __future__ import annotations

import hashlib
import hmac

from app.config import Settings


def viewer_key(settings: Settings, *, user_id: str | None, anonymous_id: str | None) -> str:
    namespace = f"user:{user_id}" if user_id else f"guest:{anonymous_id or 'anonymous'}"
    secret = settings.viewer_key_secret.strip() or settings.cursor_secret.strip()
    if not secret:
        raise RuntimeError("VIEWER_KEY_SECRET or CURSOR_SECRET is required")
    return hmac.new(secret.encode(), namespace.encode(), hashlib.sha256).hexdigest()
