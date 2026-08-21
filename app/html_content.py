from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from app.config import Settings

CONTENT_TYPE_RUNTIME = "runtime"
CONTENT_TYPE_HTML = "html"
HTML_BRIDGE_VERSION = 1
HTML_CAPABILITIES = frozenset(
    {
        "motion",
        "microphoneLevel",
        "cameraStream",
        "haptics",
        "mediaControl",
    }
)
_HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})


class HtmlContentError(ValueError):
    pass


def normalize_required_capabilities(raw: Iterable[str]) -> list[str]:
    capabilities: list[str] = []
    seen: set[str] = set()
    for value in raw:
        capability = str(value).strip()
        if capability not in HTML_CAPABILITIES:
            raise HtmlContentError(f"unsupported HTML capability: {capability or '<empty>'}")
        if capability in seen:
            raise HtmlContentError(f"duplicate HTML capability: {capability}")
        seen.add(capability)
        capabilities.append(capability)
    return capabilities


def configured_html_origins(settings: Settings) -> frozenset[str]:
    origins: set[str] = set()
    for raw in settings.html_trusted_origins.split(","):
        value = raw.strip()
        if not value:
            continue
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise HtmlContentError(
                "HTML_TRUSTED_ORIGINS entries must be HTTPS origins without paths"
            )
        origins.add(_origin(parsed))
    return frozenset(origins)


def validate_trusted_html_url(raw_url: str, settings: Settings) -> str:
    value = raw_url.strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise HtmlContentError("html_url must be an absolute HTTPS URL without credentials or fragment")
    if _origin(parsed) not in configured_html_origins(settings):
        raise HtmlContentError("html_url origin is not configured as trusted")
    if not parsed.path or parsed.path.endswith("/"):
        raise HtmlContentError("html_url must identify an HTML entry file")
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path, parsed.query, ""))


def validate_html_package_url(
    raw_url: str,
    *,
    item_id: str,
    version: str,
    settings: Settings,
) -> str:
    value = validate_trusted_html_url(raw_url, settings)
    parsed = urlsplit(value)
    if parsed.query:
        raise HtmlContentError("html_url must not contain a query")
    decoded_path = _fully_decode_path(parsed.path)
    if "\\" in decoded_path or any(ord(character) < 32 for character in decoded_path):
        raise HtmlContentError("html_url contains an unsafe path")
    segments = decoded_path.split("/")
    if any(segment in (".", "..") for segment in segments) or any(
        not segment for segment in segments[5:]
    ):
        raise HtmlContentError("html_url path traversal is not allowed")
    if settings.media_storage_mode.strip().lower() == "oss":
        root_parts = settings.oss_root_prefix.strip().strip("/").split("/")
        expected_prefix = ["", *root_parts, "public", "html", item_id, version]
    else:
        expected_prefix = ["", "pixo", "html", item_id, version]
    prefix_length = len(expected_prefix)
    if (
        segments[:prefix_length] != expected_prefix
        or len(segments) <= prefix_length
        or not segments[-1]
    ):
        raise HtmlContentError(
            "html_url is outside the immutable HTML package directory"
        )
    return value


def _fully_decode_path(raw_path: str) -> str:
    decoded = raw_path
    for _ in range(8):
        next_value = unquote(decoded)
        if next_value == decoded:
            return decoded
        decoded = next_value
    if unquote(decoded) != decoded:
        raise HtmlContentError("html_url path has excessive nested encoding")
    return decoded


def probe_html_entry(url: str, settings: Settings) -> None:
    """Verify the immutable entry without following redirects or downloading the package."""
    try:
        with (
            httpx.Client(
                timeout=settings.html_publish_probe_timeout_seconds,
                follow_redirects=False,
            ) as client,
            client.stream(
                "GET",
                url,
                headers={"Accept": "text/html", "Range": "bytes=0-4095"},
            ) as response,
        ):
            if response.is_redirect:
                raise HtmlContentError("html_url redirects are not allowed")
            if response.status_code not in (200, 206):
                raise HtmlContentError(
                    f"html_url probe returned HTTP {response.status_code}"
                )
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type not in _HTML_MEDIA_TYPES:
                raise HtmlContentError("html_url must return text/html")
            prefix = b""
            for chunk in response.iter_bytes():
                prefix += chunk
                if len(prefix) >= 4096:
                    break
            sample = prefix[:4096].lstrip().lower()
            if b"<html" not in sample and b"<!doctype html" not in sample:
                raise HtmlContentError("html_url response does not contain an HTML document")
    except HtmlContentError:
        raise
    except httpx.HTTPError as exc:
        raise HtmlContentError(f"html_url probe failed: {exc.__class__.__name__}") from exc


def _origin(parsed) -> str:
    host = (parsed.hostname or "").lower()
    port = parsed.port
    authority = host if port in (None, 443) else f"{host}:{port}"
    return f"https://{authority}"
