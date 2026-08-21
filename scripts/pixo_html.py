#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import html
import json
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import httpx

BRIDGE_VERSION = 1
ADAPTER_REVISION = "9"
_UPLOAD_CLIENT_TIMEOUT_SECONDS = 120.0
_UPLOAD_FINALIZE_TIMEOUT_SECONDS = 1800.0
ALLOWED_CAPABILITIES = (
    "motion",
    "microphoneLevel",
    "cameraStream",
    "haptics",
    "mediaControl",
)
MANIFEST_NAME = "pixo-html.json"
HOST_DIRECTORY = "pixo-host"
CONFIG_SCRIPT = f"{HOST_DIRECTORY}/pixo-html-config.js"
CLIENT_SCRIPT = f"{HOST_DIRECTORY}/pixo-native-client.js"
HOST_SCRIPT = f"{HOST_DIRECTORY}/pixo-html-host-sdk.js"
SCRIPT_SUFFIXES = frozenset({".js", ".mjs", ".cjs"})
STYLE_SUFFIXES = frozenset({".css"})
TEXT_PACKAGE_SUFFIXES = frozenset(
    {".html", ".htm", ".js", ".mjs", ".cjs", ".css", ".json"}
)
MAX_PACKAGE_OBJECTS = 1000
MAX_PACKAGE_OBJECT_BYTES = 2 * 1024 * 1024 * 1024
VIDEO_SUFFIXES = frozenset({".mp4", ".m4v", ".mov", ".webm", ".ogv"})
# One-second 16x16 VP9/WebM used only by headless Chromium.  Production media
# is validated separately with FFmpeg because the Playwright Chromium build
# used in containers does not include H.264.
PLAYWRIGHT_VIDEO_STUB = base64.b64decode(
    "GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAQ/EU2bdLpNu4tTq4QVSalmU6yBoU27i1OrhBZUrmtTrIHYTbuMU6uEElTDZ1OsggEgTbuMU6uEHFO7a1OsggQp7AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsirXsYMPQkBNgI1MYXZmNTkuMjcuMTAwV0GNTGF2ZjU5LjI3LjEwMESJiECPQAAAAAAAFlSua8OuAQAAAAAAADrXgQFzxYjClLPYgoIG4pyBACK1nIN1bmSIgQCGhVZfVlA5g4EBI+ODhAJ7yGrgi7CCAWi6ggKAmoECElTDZ0CBc3OgY8CAZ8iaRaOHRU5DT0RFUkSHjUxhdmY1OS4yNy4xMDBzc9tjwItjxYjClLPYgoIG4mfIpUWjh0VOQ09ERVJEh5hMYXZjNTkuMzcuMTAwIGxpYnZweC12cDlnyKJFo4hEVVJBVElPTkSHlDAwOjAwOjAxLjAwMDAwMDAwMAAAH0O2dUJ854EAo7eBAACAgkmDQgAWcCf2ADgkHBhKAACAYfYwAp6PcABnGpqEncO8dfhHkR5EePHjx48ePHjx48AAo5iBACoAhgBAkpwAUAAABCasAABYPoWXxkCjl4EAUwCGAECSnABO4AADIAAAWD6Fl8ZAo5eBAH0AhgBAkpwAUAAAAyAAAFg+hZfGQKOXgQCnAIYAQJKcAE1AAAMgAABYPoWXxkCjl4EA0ACGAECSnABQAAADIAAAWD6Fl8ZAo5eBAPoAhgBAkpwATuAAAyAAAFg+hZfGQKOXgQEkAIYAQJKcAFAAAAMgAABYPoWXxkCjl4EBTQCGAECSnABKIAADIAAAWD6Fl8ZAo5eBAXcAhgBAkpwAUAAAAyAAAFg+hZfGQKOXgQGhAIYAwJKcAEogAAMgAABYPoWXxkCjl4EBygCGAECSnABQAAADIAAAWD6Fl8ZAo5eBAfQAhgBAkpwATUAAAyAAAFg+hZfGQKOXgQIeAIYAQJKcAFAAAAMgAABYPoWXxkCjl4ECRwCGAECSnABO4AADIAAAWD6Fl8ZAo5eBAnEAhgBAkpwAUAAAAyAAAFg+hZfGQKOXgQKbAIYAQJKcAEogAAMgAABYPoWXxkCjl4ECxACGAECSnABQAAADIAAAWD6Fl8ZAo5eBAu4AhgBAkpwATuAAAyAAAFg+hZfGQKOXgQMYAIYAQJKcAFAAAAMgAABYPoWXxkCjl4EDQQCGAMCSnABKIAADIAAAWD6Fl8ZAo5eBA2sAhgBAkpwAUAAAAyAAAFg+hZfGQKOXgQOVAIYAQJKcAE7gAAMgAABYPoWXxkCjl4EDvgCGAECSnABQAAADIAAAWD6Fl8ZAHFO7a5G7j7OBALeK94EB8YIBp/CBAw=="
)
BASE64_VIDEO_PATTERN = re.compile(
    r"(?P<prefix>\b(?:src|href)\s*=\s*(?P<quote>['\"]))"
    r"data:video/(?P<subtype>[a-zA-Z0-9.+-]+);base64,(?P<data>[^'\"]+)"
    r"(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
GENERIC_BASE64_VIDEO_PATTERN = re.compile(
    r"(?P<quote>['\"])data:video/(?P<subtype>[a-zA-Z0-9.+-]+);base64,"
    r"(?P<data>[^'\"]+)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
LOCAL_URL_LITERAL_PATTERN = re.compile(
    r"(?P<quote>['\"])(?P<url>(?:file://[^'\"]+|/(?!/)[^'\"]+))(?P=quote)",
    re.IGNORECASE,
)
HTTPS_URL_LITERAL_PATTERN = re.compile(
    r"(?P<quote>['\"`])(?P<url>https://[^'\"`\\\s<>]+)(?P=quote)",
    re.IGNORECASE,
)
URL_ATTRIBUTE_PATTERN = re.compile(
    r"(?P<prefix>\b(?:src|href|poster|action)\s*=\s*(?P<quote>['\"]))"
    r"(?P<url>[^'\"]+)(?P=quote)",
    re.IGNORECASE,
)
CSS_URL_PATTERN = re.compile(r"url\(\s*(['\"]?)(?P<url>[^)'\"]+)\1\s*\)", re.IGNORECASE)
CSS_IMPORT_PATTERN = re.compile(
    r"@import\s+(?P<quote>['\"])(?P<url>[^'\"]+)(?P=quote)",
    re.IGNORECASE,
)
JS_STATIC_MODULE_PATTERN = re.compile(
    r"(?P<prefix>\b(?:import|export)\s+(?:[^;'\"]*?\bfrom\s+)?)"
    r"(?P<quote>['\"])(?P<url>[^'\"]+)(?P=quote)",
    re.IGNORECASE,
)
JS_DYNAMIC_MODULE_PATTERN = re.compile(
    r"(?P<prefix>\bimport\s*\(\s*)(?P<quote>['\"])(?P<url>[^'\"]+)"
    r"(?P=quote)(?P<suffix>\s*\))",
    re.IGNORECASE,
)
JS_IMPORT_META_URL_PATTERN = re.compile(
    r"(?P<prefix>\bnew\s+URL\s*\(\s*)(?P<quote>['\"])(?P<url>[^'\"]+)"
    r"(?P=quote)(?P<suffix>\s*,\s*import\s*\.\s*meta\s*\.\s*url\s*\))",
    re.IGNORECASE,
)


class HtmlPackageError(ValueError):
    pass


@dataclass(frozen=True)
class HtmlManifest:
    item_id: str
    entry: str
    title: str
    description: str
    bridge_version: int
    required_capabilities: tuple[str, ...]
    user_id: str | None = None
    feed_weight: int = 0


@dataclass(frozen=True)
class PreparedPackage:
    item_id: str
    version: str
    entry: str
    html_url: str
    user_id: str
    required_capabilities: tuple[str, ...]
    stage_directory: Path
    extracted_media: tuple[str, ...]
    compatibility_profile: str = "strict"


class _MarkupScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.inline_scripts: list[str] = []
        self._in_inline_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = {key.lower(): value or "" for key, value in attrs}
        tag_name = tag.lower()
        self.tags.append((tag_name, normalized))
        if tag_name == "script" and not normalized.get("src"):
            self._in_inline_script = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self._in_inline_script = False

    def handle_data(self, data: str) -> None:
        if self._in_inline_script:
            self.inline_scripts.append(data)


def load_manifest(source_directory: Path) -> HtmlManifest:
    path = source_directory / MANIFEST_NAME
    if not path.is_file():
        raise HtmlPackageError(f"missing {MANIFEST_NAME}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HtmlPackageError(f"invalid {MANIFEST_NAME}: {exc}") from exc
    if not isinstance(raw, dict):
        raise HtmlPackageError(f"{MANIFEST_NAME} must contain a JSON object")
    allowed_keys = {
        "item_id",
        "entry",
        "title",
        "description",
        "bridge_version",
        "required_capabilities",
        "user_id",
        "feed_weight",
    }
    extra = sorted(set(raw) - allowed_keys)
    if extra:
        raise HtmlPackageError(f"unsupported manifest fields: {', '.join(extra)}")
    item_id = _safe_identifier(raw.get("item_id"), "item_id", 128)
    entry = _safe_relative_path(raw.get("entry"), "entry")
    if not entry.lower().endswith((".html", ".htm")):
        raise HtmlPackageError("entry must be an HTML file")
    title = _required_text(raw.get("title"), "title", 120)
    description = _optional_text(raw.get("description", ""), "description", 1200)
    if raw.get("bridge_version") != BRIDGE_VERSION:
        raise HtmlPackageError("bridge_version must be 1")
    capabilities_raw = raw.get("required_capabilities", [])
    if not isinstance(capabilities_raw, list):
        raise HtmlPackageError("required_capabilities must be an array")
    capabilities: list[str] = []
    for value in capabilities_raw:
        if not isinstance(value, str) or value not in ALLOWED_CAPABILITIES:
            raise HtmlPackageError(f"unsupported capability: {value!r}")
        if value in capabilities:
            raise HtmlPackageError(f"duplicate capability: {value}")
        capabilities.append(value)
    user_id_raw = raw.get("user_id")
    user_id = (
        _safe_identifier(user_id_raw, "user_id", 64)
        if user_id_raw is not None
        else None
    )
    feed_weight = raw.get("feed_weight", 0)
    if isinstance(feed_weight, bool) or not isinstance(feed_weight, int):
        raise HtmlPackageError("feed_weight must be an integer")
    entry_path = source_directory / Path(entry)
    if not entry_path.is_file():
        raise HtmlPackageError(f"entry does not exist: {entry}")
    return HtmlManifest(
        item_id=item_id,
        entry=entry,
        title=title,
        description=description,
        bridge_version=BRIDGE_VERSION,
        required_capabilities=tuple(capabilities),
        user_id=user_id,
        feed_weight=feed_weight,
    )


def package_sha256(
    source_directory: Path,
    *,
    native_client_path: Path | None = None,
    host_sdk_path: Path | None = None,
) -> str:
    digest = hashlib.sha256()
    digest.update(f"pixo-html-adapter-v{ADAPTER_REVISION}\0".encode())
    root = source_directory.resolve()
    paths = sorted(source_directory.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise HtmlPackageError(f"symbolic links are not allowed: {relative}")
        if not path.is_file():
            continue
        encoded_path = relative.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    for label, dependency in (
        ("pixo-native-client.js", native_client_path),
        ("pixo-html-host-sdk.js", host_sdk_path),
    ):
        if dependency is None:
            continue
        if not dependency.is_file():
            raise HtmlPackageError(f"{label} not found: {dependency}")
        encoded_label = label.encode("utf-8")
        digest.update(len(encoded_label).to_bytes(4, "big"))
        digest.update(encoded_label)
        with dependency.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def stable_virtual_author(item_id: str) -> str:
    slot = int.from_bytes(hashlib.sha256(item_id.encode("utf-8")).digest(), "big") % 100
    return f"html_creator_{slot + 1:03d}"


def prepare_package(
    source_directory: Path,
    stage_directory: Path,
    *,
    public_base_url: str,
    native_client_path: Path,
    host_sdk_path: Path,
    approved_asset_origins: tuple[str, ...] = (),
    browser_compatibility: bool = False,
) -> PreparedPackage:
    source = source_directory.resolve()
    stage = stage_directory.resolve()
    if not source.is_dir():
        raise HtmlPackageError(f"source directory does not exist: {source}")
    if stage.exists() and any(stage.iterdir()):
        raise HtmlPackageError(f"stage directory must be empty: {stage}")
    manifest = load_manifest(source)
    if not native_client_path.is_file():
        raise HtmlPackageError(f"pixo-native-client.js not found: {native_client_path}")
    if not host_sdk_path.is_file():
        raise HtmlPackageError(f"HTML Host SDK not found: {host_sdk_path}")
    provisional_version = package_sha256(
        source,
        native_client_path=native_client_path,
        host_sdk_path=host_sdk_path,
    )
    base_url = _normalize_https_base(public_base_url)
    origins = tuple(_normalize_https_origin(value) for value in approved_asset_origins)
    vendor_cache: dict[str, str] = {}
    parsed_base = urlsplit(base_url)
    if parsed_base.path in ("", "/"):
        raise HtmlPackageError(
            "public_base_url must include the dedicated ivapp HTML object prefix"
        )
    html_root = base_url
    package_base_url = f"{html_root}/{quote(manifest.item_id)}/{provisional_version}/"
    html_url = urljoin(package_base_url, quote(manifest.entry, safe="/"))
    if (source / HOST_DIRECTORY).exists():
        raise HtmlPackageError(f"source directory cannot contain reserved path: {HOST_DIRECTORY}")

    stage.mkdir(parents=True, exist_ok=True)
    _copy_source_tree(source, stage)
    entry_path = stage / manifest.entry
    extracted: list[str] = []
    processed_text_assets: set[Path] = set()
    for asset_path in sorted(stage.rglob("*")):
        if (
            not asset_path.is_file()
            or asset_path.suffix.lower() not in SCRIPT_SUFFIXES | STYLE_SUFFIXES
        ):
            continue
        adapted_asset, asset_media = _adapt_text_asset(
            asset_path.read_text(encoding="utf-8"),
            suffix=asset_path.suffix.lower(),
            source_directory=source,
            stage_directory=stage,
            package_base_url=package_base_url,
            approved_asset_origins=origins,
            vendor_cache=vendor_cache,
            external_base_url=None,
        )
        asset_path.write_text(adapted_asset, encoding="utf-8")
        processed_text_assets.add(asset_path.resolve())
        extracted.extend(asset_media)
    host_dir = stage / HOST_DIRECTORY
    host_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(native_client_path, host_dir / "pixo-native-client.js")
    shutil.copy2(host_sdk_path, host_dir / "pixo-html-host-sdk.js")
    config = {
        "item_id": manifest.item_id,
        "bridge_version": BRIDGE_VERSION,
        "required_capabilities": list(manifest.required_capabilities),
        "compatibility_profile": "browser-v1" if browser_compatibility else "strict",
        "restart_on_reactivate": bool(browser_compatibility),
    }
    (host_dir / "pixo-html-config.js").write_text(
        "window.__PIXO_HTML_CONFIG__ = Object.freeze("
        + json.dumps(config, ensure_ascii=False, separators=(",", ":"))
        + ");\n",
        encoding="utf-8",
    )
    html_paths = sorted(
        path
        for path in stage.rglob("*")
        if path.is_file() and path.suffix.lower() in {".html", ".htm"}
    )
    for html_path in html_paths:
        relative = html_path.relative_to(stage)
        host_prefix = posixpath.relpath(
            HOST_DIRECTORY,
            relative.parent.as_posix() or ".",
        )
        adapted_markup, document_media = _adapt_html_document(
            html_path.read_text(encoding="utf-8"),
            source_directory=source,
            stage_directory=stage,
            package_base_url=package_base_url,
            host_script_prefix=host_prefix,
            approved_asset_origins=origins,
            vendor_cache=vendor_cache,
        )
        html_path.write_text(adapted_markup, encoding="utf-8")
        extracted.extend(document_media)
    # HTML and CSS/JS may vendor another stylesheet or module. Process every
    # newly downloaded text asset until the dependency graph is closed.
    while True:
        pending = [
            path
            for path in sorted(stage.rglob("*"))
            if path.is_file()
            and path.suffix.lower() in SCRIPT_SUFFIXES | STYLE_SUFFIXES
            and path.resolve() not in processed_text_assets
        ]
        if not pending:
            break
        if len(processed_text_assets) + len(pending) > 1000:
            raise HtmlPackageError("HTML package contains too many text dependencies")
        for asset_path in pending:
            relative_asset = asset_path.relative_to(stage).as_posix()
            adapted_asset, asset_media = _adapt_text_asset(
                asset_path.read_text(encoding="utf-8"),
                suffix=asset_path.suffix.lower(),
                source_directory=source,
                stage_directory=stage,
                package_base_url=package_base_url,
                approved_asset_origins=origins,
                vendor_cache=vendor_cache,
                external_base_url=vendor_cache.get(f"source:{relative_asset}"),
            )
            asset_path.write_text(adapted_asset, encoding="utf-8")
            processed_text_assets.add(asset_path.resolve())
            extracted.extend(asset_media)
    extracted = list(dict.fromkeys(extracted))
    _validate_staged_tree(
        stage,
        entry_path,
        origins,
        package_base_url=package_base_url,
    )
    _validate_package_limits(stage)
    version = _staged_package_sha256(stage, provisional_version=provisional_version)
    if version != provisional_version:
        for path in stage.rglob("*"):
            if path.is_file() and path.suffix.lower() in TEXT_PACKAGE_SUFFIXES:
                text = path.read_text(encoding="utf-8")
                if provisional_version in text:
                    path.write_text(text.replace(provisional_version, version), encoding="utf-8")
        package_base_url = package_base_url.replace(provisional_version, version)
        html_url = html_url.replace(provisional_version, version)
    return PreparedPackage(
        item_id=manifest.item_id,
        version=version,
        entry=manifest.entry,
        html_url=html_url,
        user_id=manifest.user_id or stable_virtual_author(manifest.item_id),
        required_capabilities=manifest.required_capabilities,
        stage_directory=stage,
        extracted_media=tuple(extracted),
        compatibility_profile="browser-v1" if browser_compatibility else "strict",
    )


def _copy_source_tree(source: Path, stage: Path) -> None:
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if path.is_symlink():
            raise HtmlPackageError(f"symbolic links are not allowed: {relative.as_posix()}")
        destination = stage / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def _validate_package_limits(stage: Path) -> None:
    files = [path for path in stage.rglob("*") if path.is_file()]
    if len(files) > MAX_PACKAGE_OBJECTS:
        raise HtmlPackageError(
            f"HTML package contains {len(files)} files; maximum is {MAX_PACKAGE_OBJECTS}"
        )
    for path in files:
        relative = path.relative_to(stage).as_posix()
        if len(relative) > 1024 or len(path.name) > 255:
            raise HtmlPackageError(f"HTML package path is too long: {relative[:160]}")
        if path.stat().st_size > MAX_PACKAGE_OBJECT_BYTES:
            raise HtmlPackageError(f"HTML package object exceeds 2GB: {relative}")


def _staged_package_sha256(stage: Path, *, provisional_version: str) -> str:
    digest = hashlib.sha256()
    digest.update(f"pixo-html-final-v{ADAPTER_REVISION}\0".encode())
    for path in sorted(stage.rglob("*"), key=lambda value: value.relative_to(stage).as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(stage).as_posix().encode("utf-8")
        payload = path.read_bytes()
        if path.suffix.lower() in TEXT_PACKAGE_SUFFIXES:
            payload = payload.replace(provisional_version.encode("ascii"), b"{PIXO_PACKAGE_VERSION}")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _adapt_html_document(
    markup: str,
    *,
    source_directory: Path,
    stage_directory: Path,
    package_base_url: str,
    host_script_prefix: str,
    approved_asset_origins: tuple[str, ...],
    vendor_cache: dict[str, str],
) -> tuple[str, list[str]]:
    lower = markup.lower()
    head_match = re.search(r"<head(?:\s[^>]*)?>", markup, re.IGNORECASE)
    if head_match is None:
        raise HtmlPackageError("entry HTML must contain <head> so host scripts load first")
    first_script = lower.find("<script")
    if first_script >= 0 and first_script < head_match.end():
        raise HtmlPackageError("business scripts cannot appear before <head>")

    extracted: list[str] = []
    def extract_video(match: re.Match[str]) -> str:
        relative = _store_embedded_video(
            subtype=match.group("subtype"),
            encoded_payload=match.group("data"),
            stage_directory=stage_directory,
        )
        if relative not in extracted:
            extracted.append(relative)
        quote_char = match.group("quote")
        media_url = html.escape(urljoin(package_base_url, relative), quote=True)
        return f"{match.group('prefix')}{media_url}{quote_char}"

    markup = BASE64_VIDEO_PATTERN.sub(extract_video, markup)

    def extract_video_literal(match: re.Match[str]) -> str:
        relative = _store_embedded_video(
            subtype=match.group("subtype"),
            encoded_payload=match.group("data"),
            stage_directory=stage_directory,
        )
        if relative not in extracted:
            extracted.append(relative)
        quote_char = match.group("quote")
        media_url = html.escape(urljoin(package_base_url, relative), quote=True)
        return f"{quote_char}{media_url}{quote_char}"

    markup = GENERIC_BASE64_VIDEO_PATTERN.sub(extract_video_literal, markup)

    def rewrite_url(match: re.Match[str]) -> str:
        raw_url = html.unescape(match.group("url")).strip()
        rewritten = (
            raw_url
            if raw_url.startswith(package_base_url)
            else
            _vendor_external_url(
                raw_url,
                stage_directory=stage_directory,
                package_base_url=package_base_url,
                approved_asset_origins=approved_asset_origins,
                vendor_cache=vendor_cache,
            )
            if raw_url.lower().startswith("https://")
            else _rewrite_local_url(
                raw_url,
                source_directory=source_directory,
                package_base_url=package_base_url,
            )
        )
        quote_char = match.group("quote")
        return f"{match.group('prefix')}{html.escape(rewritten, quote=True)}{quote_char}"

    markup = URL_ATTRIBUTE_PATTERN.sub(rewrite_url, markup)

    def rewrite_css_url(match: re.Match[str]) -> str:
        raw_url = html.unescape(match.group("url")).strip()
        rewritten = (
            raw_url
            if raw_url.startswith(package_base_url)
            else
            _vendor_external_url(
                raw_url,
                stage_directory=stage_directory,
                package_base_url=package_base_url,
                approved_asset_origins=approved_asset_origins,
                vendor_cache=vendor_cache,
            )
            if raw_url.lower().startswith("https://")
            else _rewrite_local_url(
                raw_url,
                source_directory=source_directory,
                package_base_url=package_base_url,
            )
        )
        return f"url({match.group(1)}{html.escape(rewritten, quote=True)}{match.group(1)})"

    markup = CSS_URL_PATTERN.sub(rewrite_css_url, markup)

    def rewrite_css_import(match: re.Match[str]) -> str:
        raw_url = html.unescape(match.group("url")).strip()
        rewritten = (
            raw_url
            if raw_url.startswith(package_base_url)
            else
            _vendor_external_url(
                raw_url,
                stage_directory=stage_directory,
                package_base_url=package_base_url,
                approved_asset_origins=approved_asset_origins,
                vendor_cache=vendor_cache,
            )
            if raw_url.lower().startswith("https://")
            else _rewrite_local_url(
                raw_url,
                source_directory=source_directory,
                package_base_url=package_base_url,
            )
        )
        quote_char = match.group("quote")
        return f"@import {quote_char}{html.escape(rewritten, quote=True)}{quote_char}"

    markup = CSS_IMPORT_PATTERN.sub(rewrite_css_import, markup)

    def rewrite_https_literal(match: re.Match[str]) -> str:
        raw_url = html.unescape(match.group("url")).strip()
        if raw_url.startswith(package_base_url):
            return match.group(0)
        rewritten = _vendor_external_url(
            raw_url,
            stage_directory=stage_directory,
            package_base_url=package_base_url,
            approved_asset_origins=approved_asset_origins,
            vendor_cache=vendor_cache,
        )
        return f"{match.group('quote')}{rewritten}{match.group('quote')}"

    # Includes dynamic imports and URL literals inside inline business scripts.
    markup = HTTPS_URL_LITERAL_PATTERN.sub(rewrite_https_literal, markup)
    # Approved origins are an ingestion allow-list only. Every fetched asset is
    # vendored into the immutable package, so runtime CSP remains self-only.
    asset_sources = ""
    inline_hashes = []
    for match in re.finditer(
        r"<script\b(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</script\s*>",
        markup,
        flags=re.IGNORECASE,
    ):
        if re.search(r"(?:^|\s)src\s*=", match.group("attrs"), re.IGNORECASE):
            continue
        digest = hashlib.sha256(match.group("body").encode("utf-8")).digest()
        source = "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"
        if source not in inline_hashes:
            inline_hashes.append(source)
    script_sources = " ".join(["'self'", *inline_hashes])
    csp = (
        f"default-src 'self'; script-src {script_sources}; "
        "script-src-attr 'unsafe-inline'; "
        f"style-src 'self' 'unsafe-inline' {asset_sources}; "
        f"img-src 'self' data: blob: {asset_sources}; "
        f"media-src 'self' blob: {asset_sources}; "
        f"font-src 'self' data: {asset_sources}; "
        f"connect-src 'self' {asset_sources}; frame-src 'self'; "
        "worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
    ).replace("  ", " ")
    script_paths = [
        posixpath.join(host_script_prefix, PurePosixPath(path).name)
        for path in (CONFIG_SCRIPT, CLIENT_SCRIPT, HOST_SCRIPT)
    ]
    injection = (
        f"\n<meta http-equiv=\"Content-Security-Policy\" content=\"{html.escape(csp, quote=True)}\">\n"
        "<link rel=\"icon\" href=\"data:,\">\n"
        f"<script src=\"{script_paths[0]}\"></script>\n"
        f"<script src=\"{script_paths[1]}\"></script>\n"
        f"<script src=\"{script_paths[2]}\"></script>\n"
    )
    markup = markup[: head_match.end()] + injection + markup[head_match.end() :]
    return markup, extracted


def _adapt_text_asset(
    text: str,
    *,
    suffix: str,
    source_directory: Path,
    stage_directory: Path,
    package_base_url: str,
    approved_asset_origins: tuple[str, ...],
    vendor_cache: dict[str, str],
    external_base_url: str | None,
) -> tuple[str, list[str]]:
    extracted: list[str] = []

    def extract_video_literal(match: re.Match[str]) -> str:
        relative = _store_embedded_video(
            subtype=match.group("subtype"),
            encoded_payload=match.group("data"),
            stage_directory=stage_directory,
        )
        if relative not in extracted:
            extracted.append(relative)
        quote_char = match.group("quote")
        return f"{quote_char}{urljoin(package_base_url, relative)}{quote_char}"

    text = GENERIC_BASE64_VIDEO_PATTERN.sub(extract_video_literal, text)

    def rewrite_literal(match: re.Match[str]) -> str:
        quote_char = match.group("quote")
        rewritten = _rewrite_local_url(
            match.group("url"),
            source_directory=source_directory,
            package_base_url=package_base_url,
        )
        return f"{quote_char}{rewritten}{quote_char}"

    if suffix in SCRIPT_SUFFIXES:
        text = LOCAL_URL_LITERAL_PATTERN.sub(rewrite_literal, text)

        def rewrite_module_dependency(match: re.Match[str]) -> str:
            raw_url = match.group("url").strip()
            rewritten = _vendor_text_dependency(
                raw_url,
                external_base_url=external_base_url,
                stage_directory=stage_directory,
                package_base_url=package_base_url,
                approved_asset_origins=approved_asset_origins,
                vendor_cache=vendor_cache,
                allow_plain_relative=False,
            )
            quote_char = match.group("quote")
            suffix_text = match.groupdict().get("suffix") or ""
            return f"{match.group('prefix')}{quote_char}{rewritten}{quote_char}{suffix_text}"

        text = JS_DYNAMIC_MODULE_PATTERN.sub(rewrite_module_dependency, text)
        text = JS_STATIC_MODULE_PATTERN.sub(rewrite_module_dependency, text)

        def rewrite_import_meta_url(match: re.Match[str]) -> str:
            raw_url = match.group("url").strip()
            rewritten = _vendor_text_dependency(
                raw_url,
                external_base_url=external_base_url,
                stage_directory=stage_directory,
                package_base_url=package_base_url,
                approved_asset_origins=approved_asset_origins,
                vendor_cache=vendor_cache,
                allow_plain_relative=True,
            )
            quote_char = match.group("quote")
            return (
                f"{match.group('prefix')}{quote_char}{rewritten}{quote_char}"
                f"{match.group('suffix')}"
            )

        text = JS_IMPORT_META_URL_PATTERN.sub(rewrite_import_meta_url, text)

        def rewrite_https_literal(match: re.Match[str]) -> str:
            raw_url = match.group("url").strip()
            if raw_url.startswith(package_base_url):
                return match.group(0)
            rewritten = _vendor_external_url(
                raw_url,
                stage_directory=stage_directory,
                package_base_url=package_base_url,
                approved_asset_origins=approved_asset_origins,
                vendor_cache=vendor_cache,
            )
            return f"{match.group('quote')}{rewritten}{match.group('quote')}"

        text = HTTPS_URL_LITERAL_PATTERN.sub(rewrite_https_literal, text)
    else:
        def rewrite_css_url(match: re.Match[str]) -> str:
            raw_url = match.group("url").strip()
            rewritten = (
                raw_url
                if raw_url.startswith(package_base_url)
                else
                _vendor_external_url(
                    raw_url,
                    stage_directory=stage_directory,
                    package_base_url=package_base_url,
                    approved_asset_origins=approved_asset_origins,
                    vendor_cache=vendor_cache,
                )
                if raw_url.lower().startswith("https://")
                else _vendor_text_dependency(
                    raw_url,
                    external_base_url=external_base_url,
                    stage_directory=stage_directory,
                    package_base_url=package_base_url,
                    approved_asset_origins=approved_asset_origins,
                    vendor_cache=vendor_cache,
                    allow_plain_relative=True,
                )
                if external_base_url
                else _rewrite_local_url(
                    raw_url,
                    source_directory=source_directory,
                    package_base_url=package_base_url,
                )
            )
            return f"url({match.group(1)}{rewritten}{match.group(1)})"

        text = CSS_URL_PATTERN.sub(rewrite_css_url, text)

        def rewrite_css_import(match: re.Match[str]) -> str:
            raw_url = match.group("url").strip()
            rewritten = (
                raw_url
                if raw_url.startswith(package_base_url)
                else
                _vendor_external_url(
                    raw_url,
                    stage_directory=stage_directory,
                    package_base_url=package_base_url,
                    approved_asset_origins=approved_asset_origins,
                    vendor_cache=vendor_cache,
                )
                if raw_url.lower().startswith("https://")
                else _vendor_text_dependency(
                    raw_url,
                    external_base_url=external_base_url,
                    stage_directory=stage_directory,
                    package_base_url=package_base_url,
                    approved_asset_origins=approved_asset_origins,
                    vendor_cache=vendor_cache,
                    allow_plain_relative=True,
                )
                if external_base_url
                else _rewrite_local_url(
                    raw_url,
                    source_directory=source_directory,
                    package_base_url=package_base_url,
                )
            )
            quote_char = match.group("quote")
            return f"@import {quote_char}{rewritten}{quote_char}"

        text = CSS_IMPORT_PATTERN.sub(rewrite_css_import, text)
    return text, extracted


def _vendor_text_dependency(
    raw_url: str,
    *,
    external_base_url: str | None,
    stage_directory: Path,
    package_base_url: str,
    approved_asset_origins: tuple[str, ...],
    vendor_cache: dict[str, str],
    allow_plain_relative: bool,
) -> str:
    lowered = raw_url.lower()
    if (
        not external_base_url
        or not raw_url
        or raw_url.startswith((package_base_url, "#"))
        or lowered.startswith(("data:", "blob:"))
    ):
        return raw_url
    parsed = urlsplit(raw_url)
    is_relative_url = raw_url.startswith(("./", "../", "/")) or (
        allow_plain_relative and not parsed.scheme and not parsed.netloc
    )
    if not is_relative_url:
        return raw_url
    resolved = urljoin(external_base_url, raw_url)
    return _vendor_external_url(
        resolved,
        stage_directory=stage_directory,
        package_base_url=package_base_url,
        approved_asset_origins=approved_asset_origins,
        vendor_cache=vendor_cache,
    )


def _vendor_external_url(
    raw_url: str,
    *,
    stage_directory: Path,
    package_base_url: str,
    approved_asset_origins: tuple[str, ...],
    vendor_cache: dict[str, str],
) -> str:
    cached = vendor_cache.get(f"url:{raw_url}")
    if cached:
        return urljoin(package_base_url, cached)
    origin = _origin(raw_url)
    if origin not in approved_asset_origins:
        raise HtmlPackageError(f"external resource origin is not approved: {origin}")
    request = Request(raw_url, headers={"User-Agent": "PixoHtmlPublisher/1"})
    try:
        with urlopen(request, timeout=30) as response:
            final_url = response.geturl()
            if not final_url.lower().startswith("https://") or _origin(final_url) not in approved_asset_origins:
                raise HtmlPackageError("external asset redirected outside approved HTTPS origins")
            content_type = response.headers.get_content_type().lower()
            if content_type in {"text/html", "application/xhtml+xml"}:
                raise HtmlPackageError("external HTML documents cannot be vendored")
            payload = response.read(256 * 1024 * 1024 + 1)
    except HtmlPackageError:
        raise
    except (HTTPError, URLError, OSError) as exc:
        raise HtmlPackageError(f"failed to vendor external asset: {origin}") from exc
    if not payload or len(payload) > 256 * 1024 * 1024:
        raise HtmlPackageError("external asset is empty or exceeds 256MB")
    suffix = Path(urlsplit(final_url).path).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = mimetypes.guess_extension(content_type) or ".bin"
    digest = hashlib.sha256(payload).hexdigest()
    identity = hashlib.sha256(f"{final_url}\0{digest}".encode()).hexdigest()
    relative = f"vendor/{identity[:24]}{suffix}"
    destination = stage_directory / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.read_bytes() != payload:
        raise HtmlPackageError("vendored asset hash collision")
    destination.write_bytes(payload)
    vendor_cache[f"url:{raw_url}"] = relative
    vendor_cache[f"url:{final_url}"] = relative
    vendor_cache[f"source:{relative}"] = final_url
    return urljoin(package_base_url, relative)


def _store_embedded_video(
    *,
    subtype: str,
    encoded_payload: str,
    stage_directory: Path,
) -> str:
    normalized_subtype = subtype.lower()
    extension = {
        "mp4": "mp4",
        "webm": "webm",
        "ogg": "ogv",
        "quicktime": "mov",
    }.get(
        normalized_subtype,
        re.sub(r"[^a-z0-9]", "", normalized_subtype) or "bin",
    )
    try:
        payload = base64.b64decode(
            re.sub(r"\s+", "", encoded_payload),
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise HtmlPackageError("invalid Base64 video payload") from exc
    if not payload:
        raise HtmlPackageError("embedded video payload is empty")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    relative = f"media/embedded-{digest}.{extension}"
    destination = stage_directory / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.read_bytes() != payload:
        raise HtmlPackageError(f"embedded media hash collision: {relative}")
    destination.write_bytes(payload)
    return relative


def _rewrite_local_url(raw_url: str, *, source_directory: Path, package_base_url: str) -> str:
    parsed = urlsplit(raw_url)
    lowered = raw_url.lower()
    if lowered.startswith(("data:", "blob:")) or raw_url.startswith("#"):
        return raw_url
    if lowered.startswith("javascript:"):
        raise HtmlPackageError("javascript: URLs are not allowed")
    if lowered.startswith("file://"):
        if parsed.netloc not in ("", "localhost"):
            raise HtmlPackageError("remote file:// URLs are not allowed")
        local_path = Path(_fully_decode_url_path(parsed.path)).resolve()
        relative = _relative_to_source(local_path, source_directory)
        return _versioned_local_url(
            relative,
            package_base_url=package_base_url,
            query=parsed.query,
            fragment=parsed.fragment,
        )
    if raw_url.startswith("/") and not raw_url.startswith("//"):
        decoded_path = _fully_decode_url_path(parsed.path)
        absolute_path = Path(decoded_path).resolve()
        source_root_path = (source_directory / decoded_path.lstrip("/")).resolve()
        if absolute_path.is_file():
            relative = _relative_to_source(absolute_path, source_directory)
        elif source_root_path.is_file():
            relative = _relative_to_source(source_root_path, source_directory)
        else:
            raise HtmlPackageError(f"absolute local reference does not exist: {raw_url}")
        return _versioned_local_url(
            relative,
            package_base_url=package_base_url,
            query=parsed.query,
            fragment=parsed.fragment,
        )
    return raw_url


def _versioned_local_url(
    relative: Path,
    *,
    package_base_url: str,
    query: str,
    fragment: str,
) -> str:
    target = urlsplit(
        urljoin(package_base_url, quote(relative.as_posix(), safe="/")),
    )
    return urlunsplit((target.scheme, target.netloc, target.path, query, fragment))


def _validate_staged_tree(
    stage_directory: Path,
    entry_path: Path,
    approved_asset_origins: tuple[str, ...],
    *,
    package_base_url: str,
) -> None:
    html_paths = sorted(
        path
        for path in stage_directory.rglob("*")
        if path.is_file() and path.suffix.lower() in {".html", ".htm"}
    )
    for html_path in html_paths:
        scanner = _MarkupScanner()
        scanner.feed(html_path.read_text(encoding="utf-8"))
        if html_path == entry_path and not any(tag == "video" for tag, _attrs in scanner.tags):
            raise HtmlPackageError("entry HTML must contain at least one <video>")

        relative = html_path.relative_to(stage_directory)
        host_prefix = posixpath.relpath(
            HOST_DIRECTORY,
            relative.parent.as_posix() or ".",
        )
        expected_scripts = [
            posixpath.join(host_prefix, PurePosixPath(path).name)
            for path in (CONFIG_SCRIPT, CLIENT_SCRIPT, HOST_SCRIPT)
        ]
        actual_scripts = [
            attrs.get("src") for tag, attrs in scanner.tags if tag == "script"
        ]
        if actual_scripts[:3] != expected_scripts:
            raise HtmlPackageError(
                "Pixo host scripts must precede every business script in "
                f"{relative.as_posix()}"
            )

        for tag, attrs in scanner.tags:
            if tag == "base":
                raise HtmlPackageError("<base> is not allowed")
            if tag == "meta" and attrs.get("http-equiv", "").lower() == "refresh":
                raise HtmlPackageError("meta refresh is not allowed")
            if "srcdoc" in attrs:
                raise HtmlPackageError("iframe srcdoc is not allowed")
            if attrs.get("target", "").lower() == "_blank":
                raise HtmlPackageError("popup targets are not allowed")
            if tag == "script" and attrs.get("src"):
                if attrs["src"].strip().lower().startswith(("data:", "blob:")):
                    raise HtmlPackageError("inline script URLs are not allowed")
                _validate_url_reference(
                    attrs["src"],
                    stage_directory,
                    approved_asset_origins,
                    package_base_url=package_base_url,
                    base_directory=html_path.parent,
                    external_allowed=False,
                )
            if tag == "iframe" and attrs.get("src"):
                if attrs["src"].strip().lower().startswith(("data:", "blob:")):
                    raise HtmlPackageError("inline iframe URLs are not allowed")
                _validate_url_reference(
                    attrs["src"],
                    stage_directory,
                    approved_asset_origins,
                    package_base_url=package_base_url,
                    base_directory=html_path.parent,
                    external_allowed=False,
                )
            for name in ("src", "href", "poster", "action"):
                value = attrs.get(name)
                if value:
                    _validate_url_reference(
                        value,
                        stage_directory,
                        approved_asset_origins,
                        package_base_url=package_base_url,
                        base_directory=html_path.parent,
                    )
            for value in _srcset_urls(attrs.get("srcset", "")):
                _validate_url_reference(
                    value,
                    stage_directory,
                    approved_asset_origins,
                    package_base_url=package_base_url,
                    base_directory=html_path.parent,
                )
            for match in CSS_URL_PATTERN.finditer(attrs.get("style", "")):
                _validate_url_reference(
                    match.group("url"),
                    stage_directory,
                    approved_asset_origins,
                    package_base_url=package_base_url,
                    base_directory=html_path.parent,
                )
        for script in scanner.inline_scripts:
            lowered = script.lower()
            if (
                "window.open" in lowered
                or "target='_blank'" in lowered
                or 'target="_blank"' in lowered
            ):
                raise HtmlPackageError("popup code is not allowed")
            _reject_unapproved_literals(
                script,
                approved_asset_origins,
                package_base_url=package_base_url,
            )

    for path in stage_directory.rglob("*"):
        if path.is_symlink():
            raise HtmlPackageError(f"staged symbolic link is not allowed: {path}")
        if path.is_file() and path.suffix.lower() in (
            SCRIPT_SUFFIXES | STYLE_SUFFIXES | {".html", ".htm"}
        ):
            text = path.read_text(encoding="utf-8")
            if path.suffix.lower() in SCRIPT_SUFFIXES and re.search(
                r"\bwindow\s*\.\s*open\s*\(",
                text,
                re.IGNORECASE,
            ):
                raise HtmlPackageError(
                    f"popup code is not allowed in {path.relative_to(stage_directory)}"
                )
            if "file://" in text.lower():
                raise HtmlPackageError(f"file:// reference remains in {path.relative_to(stage_directory)}")
            if "content://" in text.lower():
                raise HtmlPackageError(
                    f"content:// reference remains in {path.relative_to(stage_directory)}"
                )
            # Only reject explicit protocol-relative URL literals, not JS comments.
            if re.search(r"(?<!https:)//", text) and re.search(r"['\"]//[^/'\"]", text):
                raise HtmlPackageError(
                    f"protocol-relative URL remains in {path.relative_to(stage_directory)}"
                )
            _reject_unapproved_literals(
                text,
                approved_asset_origins,
                package_base_url=package_base_url,
            )
            if path.suffix.lower() == ".css":
                css_references = [
                    *(match.group("url") for match in CSS_URL_PATTERN.finditer(text)),
                    *(match.group("url") for match in CSS_IMPORT_PATTERN.finditer(text)),
                ]
                for reference in css_references:
                    _validate_url_reference(
                        reference,
                        stage_directory,
                        approved_asset_origins,
                        package_base_url=package_base_url,
                        base_directory=path.parent,
                    )


def _validate_url_reference(
    value: str,
    stage_directory: Path,
    approved_asset_origins: tuple[str, ...],
    *,
    package_base_url: str,
    base_directory: Path,
    external_allowed: bool = True,
) -> None:
    raw = html.unescape(value).strip()
    lowered = raw.lower()
    if not raw or raw.startswith("#") or lowered.startswith(("data:", "blob:")):
        return
    if lowered.startswith(("http://", "file://", "javascript:", "content://")) or raw.startswith("//"):
        raise HtmlPackageError(f"unsafe URL is not allowed: {raw[:120]}")
    if lowered.startswith("https://"):
        if raw.startswith(package_base_url):
            raw_path = _fully_decode_url_path(urlsplit(raw).path)
            base_path = _fully_decode_url_path(urlsplit(package_base_url).path)
            if (
                "\\" in raw_path
                or any(part in (".", "..") for part in PurePosixPath(raw_path).parts)
                or not raw_path.startswith(base_path)
            ):
                raise HtmlPackageError(f"path escapes the content directory: {raw}")
            return
        origin = _origin(raw)
        if not external_allowed or origin not in approved_asset_origins:
            raise HtmlPackageError(f"external resource origin is not approved: {origin}")
        return
    parsed_path = _fully_decode_url_path(urlsplit(raw).path)
    if "\\" in parsed_path:
        raise HtmlPackageError(f"unsafe path separator is not allowed: {raw}")
    relative = PurePosixPath(parsed_path)
    if relative.is_absolute():
        raise HtmlPackageError(f"path escapes the content directory: {raw}")
    target = (base_directory / Path(relative.as_posix())).resolve()
    try:
        target.relative_to(stage_directory.resolve())
    except ValueError as exc:
        raise HtmlPackageError(f"path escapes the content directory: {raw}") from exc
    if not target.exists():
        raise HtmlPackageError(f"referenced resource does not exist: {raw}")


def _reject_http_literals(text: str) -> None:
    if re.search(r"\bhttp://", text, re.IGNORECASE):
        raise HtmlPackageError("HTTP URLs are not allowed")


def _reject_unapproved_literals(
    text: str,
    approved_asset_origins: tuple[str, ...],
    *,
    package_base_url: str,
) -> None:
    _reject_http_literals(text)
    for value in re.findall(r"https://[^\s'\"<>]+", text, re.IGNORECASE):
        value = html.unescape(value).rstrip(".,;:)]}")
        if value.startswith(package_base_url):
            continue
        if _origin(value) not in approved_asset_origins:
            raise HtmlPackageError(f"external URL literal is not approved: {_origin(value)}")


def _srcset_urls(raw: str) -> list[str]:
    return [part.strip().split()[0] for part in raw.split(",") if part.strip()]


def _fully_decode_url_path(raw_path: str) -> str:
    decoded = raw_path
    for _ in range(8):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    else:
        if unquote(decoded) != decoded:
            raise HtmlPackageError("URL path has excessive nested encoding")
    if any(ord(character) < 32 for character in decoded):
        raise HtmlPackageError("URL path contains control characters")
    if unquote(decoded) != decoded:
        raise HtmlPackageError("URL path has excessive nested encoding")
    return decoded


def _validate_video_first_frames(stage_directory: Path) -> list[str]:
    videos = sorted(
        path
        for path in stage_directory.rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
    )
    if not videos:
        raise HtmlPackageError("HTML package does not contain a supported video asset")
    executable = shutil.which("ffmpeg")
    if not executable:
        raise HtmlPackageError("FFmpeg is required to validate HTML video assets")
    validated: list[str] = []
    for path in videos:
        try:
            result = subprocess.run(
                [
                    executable,
                    "-v",
                    "error",
                    "-xerror",
                    "-i",
                    str(path),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise HtmlPackageError(
                f"video first-frame validation timed out: {path.relative_to(stage_directory)}"
            ) from exc
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[:500]
            raise HtmlPackageError(
                f"video cannot decode its first frame: {path.relative_to(stage_directory)}"
                + (f" ({detail})" if detail else "")
            )
        validated.append(path.relative_to(stage_directory).as_posix())
    return validated


def _wait_for_page_predicate(page: Any, expression: str, *, timeout_ms: int) -> None:
    """Poll through CDP without CSP-blocked page-side eval timers."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if page.evaluate(f"Boolean({expression})"):
            return
        page.wait_for_timeout(50)
    raise HtmlPackageError(f"Playwright QA timed out waiting for: {expression}")


def run_playwright_qa(package: PreparedPackage, *, timeout_ms: int = 15_000) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise HtmlPackageError(
            "Playwright is required; install requirements-html-publisher.txt and chromium"
        ) from exc

    validated_videos = _validate_video_first_frames(package.stage_directory)
    parsed_entry = urlsplit(package.html_url)
    encoded_entry = quote(package.entry, safe="/")
    if not parsed_entry.path.endswith(encoded_entry):
        raise HtmlPackageError("HTML package entry URL does not match the staged entry")
    version_path = parsed_entry.path[: -len(encoded_entry)]
    if not version_path.endswith("/"):
        raise HtmlPackageError("HTML package entry URL has no version directory")
    decoded_version_path = _fully_decode_url_path(version_path)
    package_prefix = f"{parsed_entry.scheme}://{parsed_entry.netloc}{version_path}"
    entry_url = package.html_url
    console_errors: list[str] = []
    page_errors: list[str] = []
    request_failures: list[str] = []
    try:
        with sync_playwright() as playwright:
            executable_path = os.getenv("PIXO_PLAYWRIGHT_CHROMIUM_EXECUTABLE", "").strip()
            browser = playwright.chromium.launch(
                headless=True,
                args=["--autoplay-policy=no-user-gesture-required", "--use-fake-ui-for-media-stream"],
                executable_path=executable_path or None,
            )
            context = browser.new_context(permissions=["camera"])

            def serve_package(route: Any) -> None:
                try:
                    request_path = _fully_decode_url_path(urlsplit(route.request.url).path)
                except HtmlPackageError:
                    route.abort("blockedbyclient")
                    return
                if not request_path.startswith(decoded_version_path):
                    route.abort("blockedbyclient")
                    return
                relative = request_path[len(decoded_version_path) :]
                candidate = (package.stage_directory / relative).resolve()
                try:
                    candidate.relative_to(package.stage_directory.resolve())
                except ValueError:
                    route.abort("blockedbyclient")
                    return
                if not candidate.is_file():
                    route.fulfill(status=404, body="not found")
                    return
                if candidate.suffix.lower() in VIDEO_SUFFIXES:
                    route.fulfill(
                        status=200,
                        body=PLAYWRIGHT_VIDEO_STUB,
                        content_type="video/webm",
                        headers={"Cache-Control": "no-store"},
                    )
                    return
                route.fulfill(
                    status=200,
                    body=candidate.read_bytes(),
                    content_type=mimetypes.guess_type(candidate.name)[0]
                    or "application/octet-stream",
                    headers={"Cache-Control": "no-store"},
                )

            context.route(f"{package_prefix}**", serve_package)
            context.add_init_script(
                """
                (() => {
                  window.__PIXO_QA_CALLS = [];
                  window.PixoNativeBridge = {
                    post(raw) {
                      const request = JSON.parse(raw);
                      window.__PIXO_QA_CALLS.push(request);
                      if (request.kind !== 'request') return;
                      let result = {status: 'active'};
                      if (request.method === 'deviceInfo') {
                        const configured = window.__PIXO_HTML_CONFIG__ || {};
                        result = {
                          platform:'android', bridge_version:1,
                          capabilities: configured.required_capabilities || []
                        };
                      } else if (request.method === 'requestCapability') {
                        result = {status:'granted'};
                      }
                      setTimeout(() => window.__pixoNativeReceive && window.__pixoNativeReceive({
                        v:1,kind:'response',id:String(request.id),ok:true,result
                      }), 0);
                      if (request.method === 'startMicrophoneLevel') {
                        setTimeout(() => window.__pixoNativeReceive && window.__pixoNativeReceive({
                          v:1,kind:'event',name:'microphoneLevel',
                          data:{status:'active',rms:0.25,peak:0.25,volume_score:25}
                        }), 10);
                      }
                    }
                  };
                  if (!navigator.mediaDevices) navigator.mediaDevices = {};
                  navigator.mediaDevices.getUserMedia = async constraints => {
                    window.__PIXO_QA_MEDIA_CONSTRAINTS = constraints;
                    if (constraints && constraints.audio) {
                      window.__PIXO_QA_RAW_AUDIO_REQUESTED = true;
                      throw new DOMException('audio denied','NotAllowedError');
                    }
                    const canvas = document.createElement('canvas');
                    canvas.width = 16; canvas.height = 16;
                    canvas.getContext('2d').fillRect(0, 0, 16, 16);
                    return canvas.captureStream(1);
                  };
                })();
                """
            )
            page = context.new_page()
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on("requestfailed", lambda request: request_failures.append(request.url))
            page.goto(entry_url, wait_until="networkidle", timeout=timeout_ms)
            _wait_for_page_predicate(
                page,
                "document.querySelectorAll('video').length > 0",
                timeout_ms=timeout_ms,
            )
            exercise = page.evaluate(
                """
                async capabilities => {
                  window.dispatchEvent(new CustomEvent('pixo:host-state', {
                    detail:{active:true,allowAudio:true}
                  }));
                  const api = window.PixoNative;
                  if (!api) throw new Error('PixoNative is missing');
                  await api.getDeviceInfo();
                  for (const name of capabilities) await api.requestCapability(name);
                  if (api.startMotion) { await api.startMotion(); await api.stopMotion(); }
                  if (api.startMicrophoneLevel) {
                    await api.startMicrophoneLevel(); await api.stopMicrophoneLevel();
                  }
                  if (api.vibrate) await api.vibrate('light');
                  if (api.setMediaPlayback) await api.setMediaPlayback({play:false,muted:true});
                  if (capabilities.includes('cameraStream')) {
                    const stream = await navigator.mediaDevices.getUserMedia({video:true,audio:false});
                    window.__PIXO_QA_STREAM = stream;
                    const video = document.querySelector('video');
                    if (video && !video.currentSrc && !video.src) {
                      video.srcObject = stream;
                      const played = video.play();
                      if (played && played.catch) played.catch(() => {});
                    }
                  }
                  let rawAudioRejected = false;
                  let microphoneCompatibilityAvailable = false;
                  try {
                    const microphoneStream = await navigator.mediaDevices.getUserMedia({
                      video:false,audio:true
                    });
                    if ((window.__PIXO_HTML_CONFIG__ || {}).compatibility_profile === 'browser-v1') {
                      const Context = window.AudioContext || window.webkitAudioContext;
                      if (!Context) throw new Error('AudioContext is unavailable');
                      const context = new Context();
                      const source = context.createMediaStreamSource(microphoneStream);
                      const analyser = context.createAnalyser();
                      source.connect(analyser);
                      await new Promise(resolve => setTimeout(resolve, 30));
                      const values = new Uint8Array(analyser.fftSize);
                      analyser.getByteTimeDomainData(values);
                      microphoneCompatibilityAvailable = values.some(value => value !== 128);
                      microphoneStream.getTracks().forEach(track => track.stop());
                      if (context.close) await context.close().catch(() => {});
                    }
                  } catch (_) {
                    rawAudioRejected = true;
                  }
                  await new Promise(resolve => setTimeout(resolve, 500));
                  const videoStateBeforeInactive = Array.from(document.querySelectorAll('video')).map(video => ({
                    readyState: video.readyState,
                    width: video.videoWidth,
                    height: video.videoHeight,
                    hasSource: Boolean(video.currentSrc || video.src || video.srcObject)
                  }));
                  window.dispatchEvent(new CustomEvent('pixo:host-state', {
                    detail:{active:false,allowAudio:false}
                  }));
                  await new Promise(resolve => setTimeout(resolve, 50));
                  return {
                    methods: window.__PIXO_QA_CALLS.map(call => call.method).filter(Boolean),
                    capabilities: Object.keys(api.capabilities || {}).filter(key => api.capabilities[key]),
                    rawAudioRejected,
                    microphoneCompatibilityAvailable,
                    rawAudioReachedWebView: Boolean(window.__PIXO_QA_RAW_AUDIO_REQUESTED),
                    streamTrackStates: window.__PIXO_QA_STREAM
                      ? window.__PIXO_QA_STREAM.getTracks().map(track => track.readyState)
                      : [],
                    trackedStreamCount: window.__PIXO_HTML_HOST_SDK__.trackedStreamCount(),
                    videoStateBeforeInactive
                  };
                }
                """,
                list(package.required_capabilities),
            )
            video_state = exercise["videoStateBeforeInactive"]
            browser.close()
    except Exception as exc:
        raise HtmlPackageError(
            f"Playwright QA could not run: {exc.__class__.__name__}: {exc}"
        ) from exc

    if console_errors or page_errors or request_failures:
        raise HtmlPackageError(
            "Playwright QA failed: "
            + json.dumps(
                {
                    "console_errors": console_errors,
                    "page_errors": page_errors,
                    "request_failures": request_failures,
                },
                ensure_ascii=False,
            )
        )
    if set(exercise["capabilities"]) != set(package.required_capabilities):
        raise HtmlPackageError("Host SDK exposed capabilities that differ from the manifest")
    if exercise["rawAudioReachedWebView"]:
        raise HtmlPackageError("raw microphone getUserMedia reached the WebView")
    if package.compatibility_profile == "browser-v1":
        if (
            "microphoneLevel" in package.required_capabilities
            and not exercise["microphoneCompatibilityAvailable"]
        ):
            raise HtmlPackageError("microphone-level browser compatibility is unavailable")
    elif not exercise["rawAudioRejected"]:
        raise HtmlPackageError("raw microphone getUserMedia was not blocked by the Host SDK")
    if exercise["streamTrackStates"] and any(
        state != "ended" for state in exercise["streamTrackStates"]
    ):
        raise HtmlPackageError("camera MediaStream tracks remained live after active=false")
    if exercise["trackedStreamCount"] != 0:
        raise HtmlPackageError("Host SDK retained a MediaStream after active=false")
    sourced_videos = [state for state in video_state if state["hasSource"]]
    if not sourced_videos:
        raise HtmlPackageError("no video acquired a media source during Playwright QA")
    if not any(
        state["readyState"] >= 2 or (state["width"] > 0 and state["height"] > 0)
        for state in sourced_videos
    ):
        raise HtmlPackageError("no video reached its first decodable frame")
    return {
        "entry_url": entry_url,
        "bridge_methods": exercise["methods"],
        "video_count": len(video_state),
        "video_first_frame_checked": bool(validated_videos),
        "validated_video_assets": validated_videos,
        "browser_compatibility": package.compatibility_profile == "browser-v1",
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_to_oss(
    package: PreparedPackage,
    *,
    backend_url: str | None = None,
    publish_key: str | None = None,
) -> dict[str, Any]:
    """Upload through exact ivapp policies; this tool never receives an OSS AccessKey."""
    base = (backend_url or os.getenv("PIXO_BACKEND_URL", "")).strip().rstrip("/")
    key = (publish_key or os.getenv("PIXO_PUBLISH_KEY", "")).strip()
    if not base or not key:
        raise HtmlPackageError("--upload requires PIXO_BACKEND_URL and PIXO_PUBLISH_KEY")
    paths = sorted(path for path in package.stage_directory.rglob("*") if path.is_file())
    declarations = []
    by_relative: dict[str, Path] = {}
    for path in paths:
        relative = path.relative_to(package.stage_directory).as_posix()
        by_relative[relative] = path
        declarations.append(
            {
                "client_ref": relative,
                "relative_path": relative,
                "filename": path.name,
                "content_type": mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
                "size_bytes": path.stat().st_size,
                "sha256": _file_sha256(path),
            }
        )
    headers = {"X-Publish-Key": key}
    try:
        with httpx.Client(
            timeout=_UPLOAD_CLIENT_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            init = client.post(
                f"{base}/internal/v1/media/upload-sessions",
                headers=headers,
                json={
                    "purpose": "html_asset",
                    "target_id": package.item_id,
                    "idempotency_key": f"html-{package.item_id}-{package.version}",
                    "context": {
                        "version": package.version,
                        "entry_path": package.entry,
                        # The caller just completed structural validation and
                        # browser QA against these exact staged bytes.
                        "server_prevalidated": True,
                    },
                    "objects": declarations,
                },
            )
            if init.status_code >= 400:
                raise HtmlPackageError(
                    f"upload init returned HTTP {init.status_code}: {init.text[:500]}"
                )
            session = init.json()
            uploaded: list[str] = []
            policies = list(session.get("uploads") or [])

            def upload_one(policy: dict[str, Any]) -> str:
                relative = str(policy.get("client_ref") or "")
                path = by_relative.get(relative)
                if path is None:
                    raise HtmlPackageError(f"upload policy references unknown file: {relative}")
                policy_url = str(policy.get("url") or "")
                parsed_policy_url = urlsplit(policy_url)
                if parsed_policy_url.scheme.lower() != "https" or not parsed_policy_url.hostname:
                    raise HtmlPackageError("upload policy returned a non-HTTPS OSS URL")
                # A dedicated client per worker avoids sharing a socket pool
                # across threads and lets large, independent media assets use
                # the available upstream bandwidth concurrently.
                with (
                    httpx.Client(
                        timeout=_UPLOAD_CLIENT_TIMEOUT_SECONDS,
                        follow_redirects=False,
                    ) as upload_client,
                    path.open("rb") as stream,
                ):
                    response = upload_client.post(
                        policy_url,
                        data={
                            str(name): str(value)
                            for name, value in policy["fields"].items()
                        },
                        files={
                            "file": (
                                path.name,
                                stream,
                                "application/octet-stream",
                            )
                        },
                    )
                if response.status_code not in (200, 201, 204, 409):
                    raise HtmlPackageError(
                        f"OSS upload failed for {relative} with HTTP {response.status_code}"
                    )
                return relative

            with ThreadPoolExecutor(max_workers=max(1, min(4, len(policies) or 1))) as executor:
                futures = [executor.submit(upload_one, policy) for policy in policies]
                for future in as_completed(futures):
                    uploaded.append(future.result())
            uploaded.sort()
            finalized = client.post(
                f"{base}/internal/v1/media/upload-sessions/{session['session_id']}/finalize",
                headers=headers,
                json={"manifest_hash": package.version},
                timeout=_UPLOAD_FINALIZE_TIMEOUT_SECONDS,
            )
            if finalized.status_code >= 400:
                raise HtmlPackageError(
                    f"upload finalize returned HTTP {finalized.status_code}: {finalized.text[:500]}"
                )
            result = finalized.json()
    except httpx.HTTPError as exc:
        raise HtmlPackageError(f"upload API unavailable: {exc}") from exc
    if not result.get("package_id"):
        raise HtmlPackageError("upload finalize did not return package_id")
    return {
        "session_id": session["session_id"],
        "package_id": result["package_id"],
        "objects": result.get("objects") or [],
        "uploaded_paths": uploaded,
    }


def validate_android_approval(path: Path, package: PreparedPackage) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HtmlPackageError(f"invalid Android approval file: {exc}") from exc
    if not isinstance(raw, dict):
        raise HtmlPackageError("Android approval must be a JSON object")
    if raw.get("item_id") != package.item_id or raw.get("version") != package.version:
        raise HtmlPackageError("Android approval does not match this immutable package")
    if raw.get("verified") is not True:
        raise HtmlPackageError("Android approval must set verified=true")
    checks = raw.get("checks")
    required_checks = {"next_releases_resources", "background_releases_resources"}
    if "motion" in package.required_capabilities:
        required_checks.add("motion")
    if "microphoneLevel" in package.required_capabilities:
        required_checks.add("microphone_level")
    if "cameraStream" in package.required_capabilities:
        required_checks.add("camera_stream")
    if {"microphoneLevel", "cameraStream"}.issubset(package.required_capabilities):
        required_checks.add("camera_and_microphone_together")
    if not isinstance(checks, dict) or any(checks.get(name) is not True for name in required_checks):
        raise HtmlPackageError("Android approval is missing a required true check")
    if not _optional_text(raw.get("device_model", ""), "device_model", 120):
        raise HtmlPackageError("Android approval requires device_model")
    tested_at = raw.get("tested_at")
    try:
        datetime.fromisoformat(str(tested_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HtmlPackageError("Android approval requires ISO-8601 tested_at") from exc
    return raw


def publish_metadata(
    package: PreparedPackage,
    manifest: HtmlManifest,
    *,
    backend_url: str,
    publish_key: str,
    package_id: str,
) -> dict[str, Any]:
    payload = {
        "item_id": package.item_id,
        "package_id": package_id,
        "version": package.version,
        "html_url": package.html_url,
        "bridge_version": BRIDGE_VERSION,
        "required_capabilities": list(package.required_capabilities),
        "title": manifest.title,
        "description": manifest.description,
        "user_id": package.user_id,
        "feed_weight": manifest.feed_weight,
    }
    request = Request(
        f"{backend_url.rstrip('/')}/internal/v1/publish-html",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Publish-Key": publish_key},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HtmlPackageError(f"publish API returned HTTP {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise HtmlPackageError(f"publish API unavailable: {exc.reason}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HtmlPackageError("publish API returned invalid JSON") from exc


def android_preview_command(package: PreparedPackage) -> str:
    capabilities = ",".join(package.required_capabilities)
    return (
        "adb shell am start -n "
        "com.pixopixo.pixoandroid/.debug.PixoRemoteHtmlPreviewActivity "
        f"--es url '{package.html_url}' --es item_id '{package.item_id}' "
        f"--ei bridge_version 1 --es capabilities '{capabilities}'"
    )


def _default_native_client() -> Path:
    # The publisher pins its reviewed bridge client beside this tool.  This
    # intentionally avoids a production dependency on a sibling Android repo.
    return Path(__file__).resolve().with_name("pixo_native_client.js")


def _default_host_sdk() -> Path:
    return Path(__file__).resolve().with_name("pixo_html_host_sdk.js")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare, verify, preview and publish a reviewed Pixo HTML package."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, help="Copy the verified staging package here")
    parser.add_argument("--public-base-url", default=os.getenv("PIXO_HTML_PUBLIC_BASE_URL", ""))
    parser.add_argument("--asset-origin", action="append", default=[])
    parser.add_argument("--native-client", type=Path, default=_default_native_client())
    parser.add_argument("--host-sdk", type=Path, default=_default_host_sdk())
    parser.add_argument("--skip-playwright", action="store_true", help="Only allowed for local prepare tests")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--android-approval", type=Path)
    parser.add_argument("--backend-url", default=os.getenv("PIXO_BACKEND_URL", ""))
    args = parser.parse_args(argv)
    if not args.public_base_url:
        parser.error("--public-base-url or PIXO_HTML_PUBLIC_BASE_URL is required")
    if args.publish:
        args.upload = True
    if args.upload and args.skip_playwright:
        parser.error("Playwright QA cannot be skipped for upload or publish")
    if args.publish and args.android_approval is None:
        parser.error("--publish requires --android-approval from a completed device test")
    output = args.output.resolve() if args.output else None
    if output is not None and output.exists():
        parser.error(f"output already exists: {output}")

    manifest = load_manifest(args.source.resolve())
    with tempfile.TemporaryDirectory(prefix="pixo-html-stage-") as temporary:
        package = prepare_package(
            args.source,
            Path(temporary),
            public_base_url=args.public_base_url,
            native_client_path=args.native_client,
            host_sdk_path=args.host_sdk,
            approved_asset_origins=tuple(args.asset_origin),
        )
        qa = None if args.skip_playwright else run_playwright_qa(package)
        if output is not None:
            shutil.copytree(package.stage_directory, output)
        approval = (
            validate_android_approval(args.android_approval, package)
            if args.publish and args.android_approval
            else None
        )
        backend_url = args.backend_url.strip()
        publish_key = os.getenv("PIXO_PUBLISH_KEY", "").strip()
        if args.upload and (not backend_url or not publish_key):
            raise HtmlPackageError("--upload requires PIXO_BACKEND_URL and PIXO_PUBLISH_KEY")
        upload_result = (
            upload_to_oss(
                package,
                backend_url=backend_url,
                publish_key=publish_key,
            )
            if args.upload
            else None
        )
        published = None
        if args.publish:
            published = publish_metadata(
                package,
                manifest,
                backend_url=backend_url,
                publish_key=publish_key,
                package_id=str(upload_result["package_id"]),
            )
        result = {
            **{key: str(value) if isinstance(value, Path) else value for key, value in asdict(package).items()},
            "stage_directory": str(output) if output is not None else None,
            "qa": qa,
            "upload": upload_result,
            "uploaded_objects": (upload_result or {}).get("objects", []),
            "android_preview_command": android_preview_command(package) if args.upload else None,
            "android_approval": approval,
            "published": published,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _safe_identifier(value: Any, label: str, max_length: int) -> str:
    text = str(value).strip() if isinstance(value, str) else ""
    if not text or len(text) > max_length or any(
        not (character.isalnum() or character in "-_") for character in text
    ):
        raise HtmlPackageError(f"invalid {label}")
    return text


def _safe_relative_path(value: Any, label: str) -> str:
    text = str(value).strip() if isinstance(value, str) else ""
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise HtmlPackageError(f"invalid {label}")
    return path.as_posix()


def _required_text(value: Any, label: str, max_length: int) -> str:
    text = _optional_text(value, label, max_length)
    if not text:
        raise HtmlPackageError(f"{label} is required")
    return text


def _optional_text(value: Any, label: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise HtmlPackageError(f"{label} must be a string")
    text = value.strip()
    if len(text) > max_length:
        raise HtmlPackageError(f"{label} is too long")
    return text


def _normalize_https_base(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parsed = urlsplit(value)
    decoded_path = unquote(parsed.path)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path in ("", "/")
        or parsed.query
        or parsed.fragment
        or ".." in PurePosixPath(decoded_path).parts
        or "//" in parsed.path
    ):
        raise HtmlPackageError(
            "public base URL must be an HTTPS URL with a dedicated object prefix"
        )
    path_parts = PurePosixPath(decoded_path.strip("/").lower()).parts
    if (
        len(path_parts) < 4
        or path_parts[0] != "ivapp-media"
        or path_parts[-2:] != ("public", "html")
    ):
        raise HtmlPackageError(
            "public base URL must use ivapp-media/<version>/public/html"
        )
    return f"{_origin(value)}{parsed.path.rstrip('/')}"


def _normalize_https_origin(raw: str) -> str:
    parsed = urlsplit(raw.strip())
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise HtmlPackageError(f"asset origin must be an HTTPS origin: {raw}")
    return _origin(raw)


def _origin(raw: str) -> str:
    parsed = urlsplit(raw)
    if not parsed.hostname:
        raise HtmlPackageError(f"invalid URL: {raw[:120]}")
    port = parsed.port
    authority = parsed.hostname.lower() if port in (None, 443) else f"{parsed.hostname.lower()}:{port}"
    return f"{parsed.scheme.lower()}://{authority}"


def _relative_to_source(path: Path, source_directory: Path) -> Path:
    try:
        relative = path.relative_to(source_directory.resolve())
    except ValueError as exc:
        raise HtmlPackageError(f"local path escapes source directory: {path}") from exc
    if not path.is_file():
        raise HtmlPackageError(f"local referenced file does not exist: {path}")
    return relative


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise HtmlPackageError(f"missing environment variable: {name}")
    return value


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HtmlPackageError as exc:
        print(f"pixo-html: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
