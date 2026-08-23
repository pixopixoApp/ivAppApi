"""Short-lived Type-A authenticated URLs for private media through CDN."""

from __future__ import annotations

import hashlib
import secrets
import time
from urllib.parse import urlencode, urlsplit

from app.config import Settings
from app.oss_storage import OssStorageError, assert_owned_key, sign_get_url


def _cdn_origin(settings: Settings) -> str | None:
    raw = settings.private_media_cdn_base_url.strip().rstrip("/")
    if not raw:
        return None
    parsed = urlsplit(raw)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise OssStorageError("PRIVATE_MEDIA_CDN_BASE_URL must be an HTTPS origin")
    authority = parsed.hostname.lower() if parsed.port in (None, 443) else f"{parsed.hostname.lower()}:{parsed.port}"
    return f"https://{authority}"


def sign_private_media_url(
    settings: Settings,
    *,
    key: str,
    expires_seconds: int | None = None,
    filename: str | None = None,
) -> str:
    """Prefer private CDN; retain direct OSS signing as a rollout fallback."""
    owned = assert_owned_key(settings, key)
    origin = _cdn_origin(settings)
    secret = settings.private_media_cdn_auth_key.strip()
    if not origin or not secret:
        return sign_get_url(
            settings,
            key=owned,
            expires_seconds=expires_seconds or settings.oss_private_get_ttl_seconds,
            filename=filename,
        )
    # Type-A expiry is timestamp + the TTL configured on the CDN domain. The
    # application setting documents that console value; it is not added here.
    timestamp = int(time.time())
    nonce = secrets.token_hex(8)
    uid = settings.private_media_cdn_auth_uid.strip() or "0"
    path = f"/{owned}"
    digest = hashlib.md5(
        f"{path}-{timestamp}-{nonce}-{uid}-{secret}".encode()
    ).hexdigest()
    query = urlencode({"auth_key": f"{timestamp}-{nonce}-{uid}-{digest}"})
    return f"{origin}{path}?{query}"
