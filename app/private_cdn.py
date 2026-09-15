"""Short-lived OSS- or CDN-signed URLs for private media through the CDN."""

from __future__ import annotations

import hashlib
import secrets
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

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


def _public_cdn_origin(settings: Settings) -> str | None:
    raw = settings.aliyun_oss_public_base_url.strip().rstrip("/")
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
        return None
    authority = (
        parsed.hostname.lower()
        if parsed.port in (None, 443)
        else f"{parsed.hostname.lower()}:{parsed.port}"
    )
    return f"https://{authority}"


def private_media_cache_url(settings: Settings, *, key: str) -> str | None:
    """Return a stable CDN cache target without persisting expiring credentials."""
    owned = assert_owned_key(settings, key)
    private_prefix = f"{settings.oss_root_prefix.strip('/')}/private/"
    if not owned.startswith(private_prefix):
        raise OssStorageError("private CDN cache key must be in the private namespace")
    origin = _cdn_origin(settings) or _public_cdn_origin(settings)
    return f"{origin}/{owned}" if origin else None


def _sign_oss_url_via_public_cdn(
    settings: Settings,
    *,
    key: str,
    expires_seconds: int | None,
    filename: str | None,
) -> str:
    direct = sign_get_url(
        settings,
        key=key,
        expires_seconds=expires_seconds or settings.oss_private_get_ttl_seconds,
        filename=filename,
    )
    origin = _public_cdn_origin(settings)
    if not origin:
        return direct
    cdn = urlsplit(origin)
    signed = urlsplit(direct)
    return urlunsplit((cdn.scheme, cdn.netloc, signed.path, signed.query, ""))


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
        return _sign_oss_url_via_public_cdn(
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
