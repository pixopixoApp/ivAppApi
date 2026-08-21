from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from app.config import Settings


class PublicOriginError(ValueError):
    """A configured or persisted public-media URL is unsafe or malformed."""


def normalize_https_origin(raw: str, *, label: str) -> str:
    value = str(raw or "").strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise PublicOriginError(f"{label} must be a valid HTTPS origin") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise PublicOriginError(f"{label} must be an HTTPS origin without a path")
    authority = (
        parsed.hostname.lower()
        if port in (None, 443)
        else f"{parsed.hostname.lower()}:{port}"
    )
    return f"https://{authority}"


def canonical_public_origin(settings: Settings) -> str:
    return normalize_https_origin(
        settings.aliyun_oss_public_base_url,
        label="ALIYUN_OSS_PUBLIC_BASE_URL",
    )


def public_path_prefix(settings: Settings) -> str:
    root = settings.oss_root_prefix.strip().strip("/").replace("\\", "/")
    if not root or any(part in ("", ".", "..") for part in root.split("/")):
        raise PublicOriginError("OSS_ROOT_PREFIX is invalid")
    return f"/{root}/public/"


def _origin(parsed: SplitResult) -> str | None:
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    authority = (
        parsed.hostname.lower()
        if port in (None, 443)
        else f"{parsed.hostname.lower()}:{port}"
    )
    return f"https://{authority}"


def public_media_origins(settings: Settings) -> frozenset[str]:
    canonical = canonical_public_origin(settings)
    legacy: set[str] = {canonical}
    for raw in settings.public_media_legacy_origins.split(","):
        value = raw.strip()
        if value:
            legacy.add(normalize_https_origin(value, label="PUBLIC_MEDIA_LEGACY_ORIGINS"))
    return frozenset(legacy)


def canonicalize_public_url(settings: Settings, raw: str | None) -> str | None:
    """Map only immutable public ivapp objects from an allowlisted origin to CDN.

    Relative URLs, external URLs, private object paths and signed/query URLs are
    intentionally left untouched. This keeps compatibility and prevents an OSS
    signature from being copied to a different host.
    """
    if raw is None or not isinstance(raw, str) or not raw:
        return raw
    if not settings.aliyun_oss_public_base_url.strip():
        return raw
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw
    source_origin = _origin(parsed)
    if (
        source_origin is None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(public_path_prefix(settings))
        or source_origin not in public_media_origins(settings)
    ):
        return raw
    return urlunsplit(("https", urlsplit(canonical_public_origin(settings)).netloc, parsed.path, "", ""))


def canonicalize_public_payload(settings: Settings, value: Any) -> Any:
    """Recursively canonicalize URL strings without mutating persisted JSON."""
    if isinstance(value, str):
        return canonicalize_public_url(settings, value)
    if isinstance(value, Mapping):
        return {
            key: canonicalize_public_payload(settings, item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [canonicalize_public_payload(settings, item) for item in value]
    if isinstance(value, tuple):
        return tuple(canonicalize_public_payload(settings, item) for item in value)
    return value


def canonical_public_url_for_key(settings: Settings, key: str) -> str:
    """Build a CDN URL from an owned public object key without OSS credentials."""
    from app.oss_storage import assert_owned_key

    owned = assert_owned_key(settings, key)
    if not f"/{owned}".startswith(public_path_prefix(settings)):
        raise PublicOriginError("object key is outside the immutable public namespace")
    return f"{canonical_public_origin(settings)}/{owned}"


def require_canonical_public_url(settings: Settings, raw: str) -> str:
    """Validate one exact, query-free CDN URL and return its normalized form."""
    canonical = canonicalize_public_url(settings, raw)
    if canonical != raw:
        raise PublicOriginError("CDN task URL must already use the canonical public origin")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise PublicOriginError("CDN task URL is malformed") from exc
    if (
        _origin(parsed) != canonical_public_origin(settings)
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(public_path_prefix(settings))
        or "//" in parsed.path
        or any(part in (".", "..") for part in parsed.path.split("/"))
    ):
        raise PublicOriginError("CDN task URL is outside the immutable public namespace")
    return raw
