"""Safe ZIP -> immutable reviewed Pixo HTML package workflows.

Legacy imports can still read a private OSS source.  The one-click workflow
reads a durable host-mounted source once, works only in a derived temporary
copy, reports stage progress, and archives the untouched ZIP after preview is
ready.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy.orm import Session

from app.config import Settings
from app.media_service import media_mode_is_oss, safe_id, safe_relative_path
from app.models import MediaObject
from app.oss_storage import (
    OssObjectNotFoundError,
    OssStorageError,
    download_file,
    head_object,
    object_key,
    public_url,
    upload_file,
)
from app.users import apply_user_update
from scripts.pixo_html import (
    ALLOWED_CAPABILITIES,
    HtmlPackageError,
    _default_host_sdk,
    _default_native_client,
    prepare_package,
    run_playwright_qa,
    upload_to_oss,
)
from scripts.seed_html_creators import html_creators


class HtmlImportError(ValueError):
    pass


logger = logging.getLogger(__name__)
_HTML_SUFFIXES = frozenset({".html", ".htm"})
_AI_SOURCE_SUFFIXES = frozenset({".html", ".htm", ".js", ".mjs", ".cjs"})
_SAFE_IMPORT_ID = re.compile(r"^him_[A-Za-z0-9_-]{1,56}$")
_MAX_AI_SNIPPET_CHARS = 80_000
_MAX_AI_CONTEXT_CHARS = 320_000
_BRIDGE_HINTS = {
    "motion": re.compile(
        r"(?:startMotion|DeviceMotionEvent|DeviceOrientationEvent|"
        r"['\"]device(?:motion|orientation(?:absolute)?)['\"])",
        re.IGNORECASE,
    ),
    "microphoneLevel": re.compile(
        r"(?:startMicrophoneLevel|microphoneLevel|\bvolume_score\b|\bpeak_score\b)",
        re.IGNORECASE,
    ),
    "cameraStream": re.compile(r"(?:cameraStream|\bvideoinput\b)", re.IGNORECASE),
    "haptics": re.compile(r"(?:navigator\s*\.\s*vibrate\s*\(|\.vibrate\s*\(|\bhaptic\b)", re.IGNORECASE),
    "mediaControl": re.compile(r"(?:setMediaPlayback|mediaControl)", re.IGNORECASE),
}
_GET_USER_MEDIA_CALL = re.compile(
    r"(?:navigator\s*\.\s*mediaDevices\s*\.\s*)?getUserMedia\s*\("
    r"(?P<constraints>[\s\S]{0,1600}?)\)",
    re.IGNORECASE,
)
_GET_USER_MEDIA_REFERENCE = re.compile(r"\bgetUserMedia\b", re.IGNORECASE)
_UNSUPPORTED_BROWSER_FEATURES = {
    "microphone_recording": re.compile(r"\bMediaRecorder\b", re.IGNORECASE),
    "speech_recognition": re.compile(
        r"\b(?:webkit)?SpeechRecognition\b", re.IGNORECASE
    ),
    "display_capture": re.compile(r"\bgetDisplayMedia\s*\(", re.IGNORECASE),
    "raw_audio_processing": re.compile(
        r"\b(?:AudioWorklet|createScriptProcessor)\b", re.IGNORECASE
    ),
    "frequency_audio_analysis": re.compile(
        r"\bget(?:Byte|Float)FrequencyData\s*\(", re.IGNORECASE
    ),
}


def _html_public_root(settings: Settings) -> str:
    """Return the immutable HTML root, optionally through a first-party proxy/CDN."""
    configured = (
        str(getattr(settings, "html_public_base_url", "") or "")
        .strip()
        .rstrip("/")
    )
    if not configured:
        return public_url(settings, object_key(settings, "public", "html")).rstrip("/")
    parsed = urlsplit(configured)
    expected_path = "/" + object_key(settings, "public", "html").strip("/")
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != expected_path
    ):
        raise HtmlImportError(
            "HTML_PUBLIC_BASE_URL must be an HTTPS origin plus the immutable HTML object prefix"
        )
    authority = (
        parsed.hostname.lower()
        if parsed.port in (None, 443)
        else f"{parsed.hostname.lower()}:{parsed.port}"
    )
    return urlunsplit(("https", authority, expected_path, "", ""))


_IGNORED_ARCHIVE_PARTS = frozenset({"__MACOSX"})
_MAX_SCANNED_TEXT_BYTES = 8 * 1024 * 1024


def _source_object(db: Session, object_id: str) -> MediaObject:
    row = db.get(MediaObject, object_id.strip())
    if row is None or row.purpose != "html_import_source" or row.state != "ready":
        raise HtmlImportError("verified HTML source archive was not found")
    return row


def _ensure_html_creator_pool(db: Session) -> None:
    """Create the fixed 100 non-login authors idempotently before first publish."""
    for creator in html_creators():
        payload = creator.payload()
        apply_user_update(
            db,
            user_id=creator.user_id,
            provider=str(payload["provider"]),
            subject=str(payload["subject"]),
            enabled=True,
            nickname=str(payload["nickname"]),
            avatar_url="",
            bio=str(payload["bio"]),
            source="admin",
            create_if_missing=True,
        )
    db.commit()


def _safe_extract(archive: Path, destination: Path, settings: Settings) -> list[str]:
    if archive.stat().st_size > settings.html_import_max_zip_bytes:
        raise HtmlImportError("source ZIP exceeds the configured size limit")
    try:
        with zipfile.ZipFile(archive) as source:
            entries = source.infolist()
            if not entries or len(entries) > settings.html_import_max_files:
                raise HtmlImportError("ZIP has no files or exceeds the file-count limit")
            total = 0
            paths: list[str] = []
            for info in entries:
                name = info.filename.replace("\\", "/")
                path = PurePosixPath(name)
                # UNIX symlink bits live in the upper external attributes.
                is_link = ((info.external_attr >> 16) & 0o170000) == 0o120000
                if (
                    not name
                    or path.is_absolute()
                    or any(part in ("", ".", "..") for part in path.parts)
                    or is_link
                ):
                    raise HtmlImportError("ZIP contains an unsafe path or symbolic link")
                if info.is_dir():
                    continue
                total += int(info.file_size)
                if total > settings.html_import_max_unpacked_bytes:
                    raise HtmlImportError("ZIP exceeds the unpacked-size limit")
                if (
                    any(part in _IGNORED_ARCHIVE_PARTS for part in path.parts)
                    or path.name == ".DS_Store"
                    or path.name.startswith("._")
                ):
                    continue
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(info) as input_stream, target.open("wb") as output_stream:
                    shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
                paths.append(path.as_posix())
    except zipfile.BadZipFile as exc:
        raise HtmlImportError("source is not a valid ZIP archive") from exc
    return sorted(paths)


def _entry_candidates(root: Path) -> list[str]:
    candidates = [path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() and path.suffix.lower() in _HTML_SUFFIXES]
    return sorted(candidates)


def _choose_entry(root: Path, requested: str | None) -> str:
    candidates = _entry_candidates(root)
    if not candidates:
        raise HtmlImportError("ZIP does not contain an HTML entry")
    if requested:
        entry = safe_relative_path(requested, label="entry")
        if entry not in candidates:
            raise HtmlImportError("selected entry is not an HTML file in the ZIP")
        return entry
    for preferred in ("index.html", "index.htm"):
        if preferred in candidates:
            return preferred
    if len(candidates) == 1:
        return candidates[0]
    raise HtmlImportError("multiple HTML entries; choose one explicitly")


def _choose_entry_automatically(root: Path, requested: str | None) -> tuple[str, bool]:
    """Choose deterministically and flag when an operator should review it."""
    candidates = _entry_candidates(root)
    if not candidates:
        raise HtmlImportError("ZIP does not contain an HTML entry")
    if requested:
        entry = safe_relative_path(requested, label="entry")
        if entry not in candidates:
            raise HtmlImportError("selected entry is not an HTML file in the ZIP")
        return entry, False
    for preferred in ("index.html", "index.htm"):
        if preferred in candidates:
            return preferred, len(candidates) > 1
    ranked = sorted(
        candidates,
        key=lambda value: (
            0 if PurePosixPath(value).name.lower() in {"index.html", "index.htm"} else 1,
            len(PurePosixPath(value).parts),
            len(value),
            value.lower(),
        ),
    )
    return ranked[0], len(candidates) > 1


def _media_constraint_enabled(constraints: str, name: str) -> bool | None:
    field = re.search(
        rf"\b{name}\s*:\s*(?P<value>[^,}}\n]+|{{)",
        constraints,
        re.IGNORECASE,
    )
    if field is None:
        return None
    value = field.group("value").strip().lower()
    return not re.match(r"^(?:false|null|0)\b", value)


def _scan(root: Path) -> dict[str, Any]:
    capabilities: set[str] = set()
    external_urls: set[str] = set()
    unsupported: set[str] = set()
    warnings: set[str] = set()
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".html", ".htm", ".js", ".mjs", ".cjs", ".css"}:
            try:
                payload = path.read_bytes()
                if len(payload) > _MAX_SCANNED_TEXT_BYTES:
                    half = _MAX_SCANNED_TEXT_BYTES // 2
                    payload = payload[:half] + b"\n" + payload[-half:]
                    warnings.add("large_text_asset_was_partially_scanned")
                text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HtmlImportError(f"text asset is not UTF-8: {path.relative_to(root)}") from exc
            capabilities.update(
                name for name, pattern in _BRIDGE_HINTS.items() if pattern.search(text)
            )
            external_urls.update(
                re.findall(r"https?://[^\s'\"<>`]+", text, flags=re.IGNORECASE)
            )
            unsupported.update(
                name
                for name, pattern in _UNSUPPORTED_BROWSER_FEATURES.items()
                if pattern.search(text)
            )
            media_calls = list(_GET_USER_MEDIA_CALL.finditer(text))
            for match in media_calls:
                constraints = match.group("constraints")
                audio = _media_constraint_enabled(constraints, "audio")
                video = _media_constraint_enabled(constraints, "video")
                if audio:
                    capabilities.add("microphoneLevel")
                if video:
                    capabilities.add("cameraStream")
                if audio and video:
                    unsupported.add("combined_camera_and_microphone_capture")
                if audio is None and video is None:
                    # Dynamic constraints cannot be proven statically.  Grant the
                    # two guarded media capabilities and report the uncertainty.
                    capabilities.update({"microphoneLevel", "cameraStream"})
                    warnings.add("dynamic_get_user_media_constraints")
            if _GET_USER_MEDIA_REFERENCE.search(text) and not media_calls:
                # Bracket notation, aliases and wrappers cannot be classified
                # safely without executing untrusted code. Grant only the two
                # guarded media shims and make the uncertainty visible.
                capabilities.update({"microphoneLevel", "cameraStream"})
                warnings.add("dynamic_get_user_media_constraints")
    suggested = [name for name in ALLOWED_CAPABILITIES if name in capabilities]
    return {
        "entry_candidates": _entry_candidates(root),
        "suggested_capabilities": suggested,
        "external_urls": sorted(external_urls)[:100],
        "compatibility_profile": "browser-v1",
        "unsupported_features": sorted(unsupported),
        "compatibility_warnings": sorted(warnings),
        "contains_existing_manifest": (root / "pixo-html.json").exists(),
    }


def inspect_source(db: Session, settings: Settings, *, source_object_id: str) -> dict[str, Any]:
    row = _source_object(db, source_object_id)
    with tempfile.TemporaryDirectory(prefix="pixo-html-inspect-") as temporary:
        archive = Path(temporary) / "source.zip"
        root = Path(temporary) / "source"
        root.mkdir()
        try:
            download_file(settings, key=row.object_key, path=archive, expected_etag=row.etag or None)
        except OssStorageError as exc:
            raise HtmlImportError(f"cannot download source ZIP: {exc}") from exc
        files = _safe_extract(archive, root, settings)
        result = _scan(root)
        result.update({"source_object_id": row.id, "file_count": len(files), "source_sha256": row.sha256})
        return result


def prepare_source(
    db: Session,
    settings: Settings,
    *,
    source_object_id: str,
    item_id: str,
    entry: str | None,
    title: str,
    description: str,
    user_id: str,
    required_capabilities: list[str],
) -> dict[str, Any]:
    if not media_mode_is_oss(settings):
        raise HtmlImportError("HTML imports require MEDIA_STORAGE_MODE=oss")
    item = safe_id(item_id, label="item_id")
    author = safe_id(user_id, label="user_id")
    requested_capabilities = list(
        dict.fromkeys(str(value).strip() for value in required_capabilities)
    )
    if any(value not in ALLOWED_CAPABILITIES for value in requested_capabilities):
        raise HtmlImportError("HTML import requested an unsupported capability")
    row = _source_object(db, source_object_id)
    _ensure_html_creator_pool(db)
    html_base = _html_public_root(settings)
    with tempfile.TemporaryDirectory(prefix="pixo-html-import-") as temporary:
        working = Path(temporary)
        archive, root, stage = working / "source.zip", working / "source", working / "stage"
        root.mkdir()
        try:
            download_file(settings, key=row.object_key, path=archive, expected_etag=row.etag or None)
        except OssStorageError as exc:
            raise HtmlImportError(f"cannot download source ZIP: {exc}") from exc
        _safe_extract(archive, root, settings)
        inspection = _scan(root)
        detected = set(inspection["suggested_capabilities"])
        requested = set(requested_capabilities)
        capabilities = [
            value for value in ALLOWED_CAPABILITIES if value in detected | requested
        ]
        selected_entry = _choose_entry(root, entry)
        # An operator-provided manifest must never control IDs, author or permissions.
        (root / "pixo-html.json").write_text(json.dumps({
            "item_id": item, "entry": selected_entry, "title": title.strip(),
            "description": description.strip(), "bridge_version": 1,
            "required_capabilities": capabilities, "user_id": author,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            package = prepare_package(
                root, stage, public_base_url=html_base,
                native_client_path=_default_native_client(), host_sdk_path=_default_host_sdk(),
                browser_compatibility=True,
            )
            qa = run_playwright_qa(package) if settings.html_import_require_playwright else {"skipped": True}
            uploaded = upload_to_oss(package, backend_url="http://127.0.0.1:8100", publish_key=settings.publish_key)
        except HtmlPackageError as exc:
            raise HtmlImportError(str(exc)) from exc
        return {
            "item_id": package.item_id, "version": package.version, "entry": package.entry,
            "html_url": package.html_url, "user_id": package.user_id,
            "required_capabilities": list(package.required_capabilities), "package_id": uploaded["package_id"],
            "compatibility_profile": package.compatibility_profile,
            "inspection": inspection,
            "qa": qa, "android_preview_command": (
                "adb shell am start -n com.pixopixo.pixoandroid/.debug.PixoRemoteHtmlPreviewActivity "
                f"--es url '{package.html_url}' --es item_id '{package.item_id}' --ei bridge_version 1 "
                f"--es capabilities '{','.join(package.required_capabilities)}'"
            ),
        }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_source(
    settings: Settings,
    *,
    import_id: str,
    expected_sha256: str,
    expected_bytes: int,
) -> Path:
    if not _SAFE_IMPORT_ID.fullmatch(import_id):
        raise HtmlImportError("invalid HTML import id")
    root = Path(settings.html_import_spool_root).resolve()
    archive = (root / import_id / "source.zip").resolve()
    try:
        archive.relative_to(root)
    except ValueError as exc:
        raise HtmlImportError("invalid HTML import source path") from exc
    if not archive.is_file():
        raise HtmlImportError("local HTML source archive is unavailable")
    if archive.stat().st_size != expected_bytes:
        raise HtmlImportError("local HTML source size changed after upload")
    if _file_sha256(archive) != expected_sha256.lower():
        raise HtmlImportError("local HTML source checksum changed after upload")
    return archive


def _progress_headers(settings: Settings) -> dict[str, str]:
    key = settings.creator_internal_key.strip()
    if not key:
        raise HtmlImportError("HTML import progress integration is not configured")
    return {"X-Creator-Internal-Key": key}


def _notify_progress(
    settings: Settings,
    *,
    import_id: str,
    attempt_id: str,
    stage: str,
    stage_index: int,
    progress_percent: int,
    detail: str,
    skipped_stages: list[str] | None = None,
) -> None:
    try:
        with httpx.Client(
            trust_env=False,
            timeout=max(2.0, settings.html_import_progress_timeout_seconds),
        ) as client:
            response = client.post(
                f"{settings.ivadmin_base_url.rstrip('/')}/internal/v1/html-imports/{import_id}/progress",
                headers=_progress_headers(settings),
                json={
                    "attempt_id": attempt_id,
                    "stage": stage,
                    "stage_index": stage_index,
                    "progress_percent": progress_percent,
                    "detail": detail,
                    "skipped_stages": skipped_stages,
                },
            )
    except httpx.HTTPError as exc:
        # The main request still returns a definitive result, so a transient
        # progress callback must not invalidate an otherwise healthy package.
        logger.warning("HTML import progress callback failed: %s", exc)
        return
    if response.status_code == 409:
        raise HtmlImportError("HTML import attempt is no longer active")
    if response.status_code in {401, 403, 422}:
        raise HtmlImportError(
            f"HTML import progress callback was rejected (HTTP {response.status_code})"
        )
    if response.status_code >= 500:
        logger.warning(
            "HTML import progress callback unavailable: HTTP %s",
            response.status_code,
        )


def _collect_ai_snippets(root: Path, *, entry: str) -> list[dict[str, Any]]:
    paths = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in _AI_SOURCE_SUFFIXES
    ]

    def rank(path: Path) -> tuple[int, int, str]:
        relative = path.relative_to(root).as_posix()
        if relative == entry:
            return (0, 0, relative)
        try:
            sample = path.read_text(encoding="utf-8")[:200_000]
        except (OSError, UnicodeDecodeError):
            sample = ""
        bridge_related = any(pattern.search(sample) for pattern in _BRIDGE_HINTS.values())
        return (1 if bridge_related else 2, len(PurePosixPath(relative).parts), relative)

    snippets: list[dict[str, Any]] = []
    total = 0
    for path in sorted(paths, key=rank):
        if len(snippets) >= 5 or total >= _MAX_AI_CONTEXT_CHARS:
            break
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        truncated = len(text) > _MAX_AI_SNIPPET_CHARS
        if truncated:
            half = _MAX_AI_SNIPPET_CHARS // 2
            content = text[:half] + "\n/* PIXO_CONTEXT_OMITTED */\n" + text[-half:]
        else:
            content = text
        remaining = _MAX_AI_CONTEXT_CHARS - total
        if len(content) > remaining:
            content = content[:remaining]
            truncated = True
        if not content:
            continue
        file_hash = _file_sha256(path)
        snippets.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": file_hash,
            "expected_sha256": file_hash,
            "content": content,
            "truncated": truncated,
        })
        total += len(content)
    return snippets


def _request_ai_patch(
    settings: Settings,
    *,
    import_id: str,
    attempt_id: str,
    root: Path,
    entry: str,
    inspection: dict[str, Any],
    qa_error: str,
) -> dict[str, Any]:
    try:
        with httpx.Client(trust_env=False, timeout=110.0) as client:
            response = client.post(
                f"{settings.ivadmin_base_url.rstrip('/')}/internal/v1/html-imports/{import_id}/ai-patch",
                headers=_progress_headers(settings),
                json={
                    "attempt_id": attempt_id,
                    "inspection": inspection,
                    "snippets": _collect_ai_snippets(root, entry=entry),
                    "qa_error": qa_error[:4000],
                },
            )
    except httpx.HTTPError as exc:
        return {
            "outcome": "unavailable",
            "summary": f"AI 兼容修复服务暂不可用：{str(exc)[:300]}",
            "edits": [],
            "required_capabilities": [],
            "call_number": 0,
        }
    if response.status_code == 409:
        raise HtmlImportError("HTML import attempt is no longer active")
    if response.status_code >= 400:
        return {
            "outcome": "unavailable",
            "summary": f"AI 兼容修复请求失败（HTTP {response.status_code}）",
            "edits": [],
            "required_capabilities": [],
            "call_number": 0,
        }
    try:
        value = response.json()
    except ValueError:
        value = None
    if not isinstance(value, dict):
        return {
            "outcome": "unavailable",
            "summary": "AI 兼容修复返回了无效响应",
            "edits": [],
            "required_capabilities": [],
            "call_number": 0,
        }
    return value


def _request_ai_patch_with_heartbeat(
    settings: Settings,
    *,
    repair_number: int,
    import_id: str,
    attempt_id: str,
    root: Path,
    entry: str,
    inspection: dict[str, Any],
    qa_error: str,
) -> dict[str, Any]:
    stop = threading.Event()
    started = time.monotonic()

    def heartbeat() -> None:
        while not stop.wait(10):
            elapsed = max(10, int(time.monotonic() - started))
            try:
                _notify_progress(
                    settings,
                    import_id=import_id,
                    attempt_id=attempt_id,
                    stage="ai_repair",
                    stage_index=5,
                    progress_percent=min(69, 59 + repair_number * 2),
                    detail=f"第 {repair_number} 次受限 AI 兼容修复仍在运行，已等待 {elapsed} 秒",
                )
            except HtmlImportError:
                logger.warning("HTML import %s AI heartbeat was rejected", import_id)
                return

    thread = threading.Thread(
        target=heartbeat,
        name=f"html-ai-heartbeat-{import_id[-8:]}",
        daemon=True,
    )
    thread.start()
    try:
        return _request_ai_patch(
            settings,
            import_id=import_id,
            attempt_id=attempt_id,
            root=root,
            entry=entry,
            inspection=inspection,
            qa_error=qa_error,
        )
    finally:
        stop.set()
        thread.join(timeout=2)


def _ai_repairable_error(exc: HtmlPackageError) -> bool:
    """Do not spend model calls on infrastructure, media, or ZIP defects."""
    detail = str(exc)
    if detail.startswith("Playwright QA failed:"):
        return True
    return detail.startswith("Playwright QA could not run:") and any(
        marker in detail
        for marker in ("ReferenceError", "TypeError", "NotSupportedError", "SecurityError")
    )


def _upload_preview_with_heartbeat(
    settings: Settings,
    *,
    package: Any,
    import_id: str,
    attempt_id: str,
    skipped_stages: list[str],
) -> dict[str, Any]:
    stop = threading.Event()
    started = time.monotonic()

    def heartbeat() -> None:
        while not stop.wait(10):
            elapsed = max(10, int(time.monotonic() - started))
            try:
                _notify_progress(
                    settings,
                    import_id=import_id,
                    attempt_id=attempt_id,
                    stage="uploading_preview",
                    stage_index=7,
                    progress_percent=86,
                    detail=f"正在上传不可变预览资源，已持续 {elapsed} 秒；任务仍在正常运行",
                    skipped_stages=skipped_stages,
                )
            except HtmlImportError:
                logger.warning("HTML import %s upload heartbeat was rejected", import_id)
                return

    thread = threading.Thread(
        target=heartbeat,
        name=f"html-preview-heartbeat-{import_id[-8:]}",
        daemon=True,
    )
    thread.start()
    try:
        return upload_to_oss(
            package,
            backend_url="http://127.0.0.1:8100",
            publish_key=settings.publish_key,
        )
    finally:
        stop.set()
        thread.join(timeout=2)


def _apply_ai_patch(root: Path, response: dict[str, Any]) -> list[dict[str, Any]]:
    if response.get("outcome") != "patch":
        return []
    edits = response.get("edits")
    if not isinstance(edits, list) or not edits:
        raise HtmlImportError("AI patch did not contain any edits")
    pending: list[tuple[Path, str]] = []
    audit: list[dict[str, Any]] = []
    seen: set[str] = set()
    forbidden_new = re.compile(
        r"https?://|\bWebSocket\b|\bgetDisplayMedia\b|\bMediaRecorder\b|"
        r"\b(?:webkit)?SpeechRecognition\b|\baudio\s*:\s*true\b|"
        r"(?:window\s*\.\s*)?PixoNative\s*=",
        re.IGNORECASE,
    )
    for edit in edits:
        if not isinstance(edit, dict):
            raise HtmlImportError("AI patch contains an invalid edit")
        relative = safe_relative_path(str(edit.get("path") or ""), label="AI patch path")
        if relative in seen:
            raise HtmlImportError("AI patch edits the same file more than once")
        seen.add(relative)
        if relative == "pixo-html.json" or relative.startswith("pixo-host/"):
            raise HtmlImportError("AI patch attempted to edit a reserved platform file")
        path = (root / relative).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise HtmlImportError("AI patch path escapes the package") from exc
        if not path.is_file() or path.suffix.lower() not in _AI_SOURCE_SUFFIXES:
            raise HtmlImportError("AI patch targeted an unsupported source file")
        before_sha = _file_sha256(path)
        if before_sha != str(edit.get("expected_sha256") or "").lower():
            raise HtmlImportError("AI patch source hash does not match")
        text = path.read_text(encoding="utf-8")
        replacements = edit.get("replacements")
        if not isinstance(replacements, list) or not replacements:
            raise HtmlImportError("AI patch edit has no replacements")
        for replacement in replacements:
            if not isinstance(replacement, dict):
                raise HtmlImportError("AI patch contains an invalid replacement")
            old, new = replacement.get("old"), replacement.get("new")
            if not isinstance(old, str) or not isinstance(new, str) or not old or old == new:
                raise HtmlImportError("AI patch replacement is invalid")
            if text.count(old) != 1:
                raise HtmlImportError("AI patch replacement is not uniquely anchored")
            if forbidden_new.search(new):
                raise HtmlImportError("AI patch attempted to introduce a forbidden API or URL")
            text = text.replace(old, new, 1)
        pending.append((path, text))
        audit.append({
            "path": relative,
            "before_sha256": before_sha,
            "replacement_count": len(replacements),
        })
    for index, (path, text) in enumerate(pending):
        path.write_text(text, encoding="utf-8")
        audit[index]["after_sha256"] = _file_sha256(path)
    return audit


def _write_trusted_manifest(
    root: Path,
    *,
    item_id: str,
    entry: str,
    title: str,
    description: str,
    capabilities: list[str],
    user_id: str,
) -> None:
    (root / "pixo-html.json").write_text(
        json.dumps(
            {
                "item_id": item_id,
                "entry": entry,
                "title": title.strip(),
                "description": description.strip(),
                "bridge_version": 1,
                "required_capabilities": capabilities,
                "user_id": user_id,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def prepare_local_source(
    db: Session,
    settings: Settings,
    *,
    import_id: str,
    attempt_id: str,
    source_sha256: str,
    source_bytes: int,
    item_id: str,
    entry: str | None,
    title: str,
    description: str,
    user_id: str,
    required_capabilities: list[str],
) -> dict[str, Any]:
    """Prepare a package from the shared spool and return only after preview is ready."""
    if not media_mode_is_oss(settings):
        raise HtmlImportError("HTML imports require MEDIA_STORAGE_MODE=oss")
    item = safe_id(item_id, label="item_id")
    author = safe_id(user_id, label="user_id")
    requested = list(dict.fromkeys(str(value).strip() for value in required_capabilities))
    if any(value not in ALLOWED_CAPABILITIES for value in requested):
        raise HtmlImportError("HTML import requested an unsupported capability")
    _notify_progress(
        settings,
        import_id=import_id,
        attempt_id=attempt_id,
        stage="validating_zip",
        stage_index=2,
        progress_percent=20,
        detail="正在校验 ZIP 完整性和来源哈希",
    )
    archive = _local_source(
        settings,
        import_id=import_id,
        expected_sha256=source_sha256,
        expected_bytes=source_bytes,
    )
    _ensure_html_creator_pool(db)
    html_base = _html_public_root(settings)
    with tempfile.TemporaryDirectory(
        prefix=f"work-{attempt_id[:8]}-",
        dir=str(archive.parent),
    ) as temporary:
        working = Path(temporary)
        root, stage = working / "source", working / "stage"
        root.mkdir()
        _notify_progress(
            settings,
            import_id=import_id,
            attempt_id=attempt_id,
            stage="extracting_source",
            stage_index=3,
            progress_percent=30,
            detail="正在安全解压 ZIP，不会修改原始上传文件",
        )
        files = _safe_extract(archive, root, settings)
        _notify_progress(
            settings,
            import_id=import_id,
            attempt_id=attempt_id,
            stage="scanning_source",
            stage_index=3,
            progress_percent=42,
            detail="正在扫描入口、资源和浏览器能力调用",
        )
        inspection = _scan(root)
        inspection.update({
            "file_count": len(files),
            "source_sha256": source_sha256,
        })
        selected_entry, auto_selected_multiple = _choose_entry_automatically(root, entry)
        detected = set(inspection["suggested_capabilities"])
        capabilities = [
            value for value in ALLOWED_CAPABILITIES if value in detected | set(requested)
        ]
        _notify_progress(
            settings,
            import_id=import_id,
            attempt_id=attempt_id,
            stage="selecting_entry",
            stage_index=4,
            progress_percent=50,
            detail=(
                f"已自动选择入口 {selected_entry}，请在完成后复核"
                if auto_selected_multiple
                else f"已确认入口 {selected_entry}"
            ),
        )
        _write_trusted_manifest(
            root,
            item_id=item,
            entry=selected_entry,
            title=title,
            description=description,
            capabilities=capabilities,
            user_id=author,
        )

        ai_history: list[dict[str, Any]] = []
        applied_ai_edits: list[dict[str, Any]] = []
        package = None
        qa: dict[str, Any] | None = None
        repair_feedback = ""
        for build_attempt in range(6):
            try:
                if stage.exists():
                    shutil.rmtree(stage)
                _notify_progress(
                    settings,
                    import_id=import_id,
                    attempt_id=attempt_id,
                    stage="adapting_compatibility",
                    stage_index=5,
                    progress_percent=58,
                    detail="正在注入 Host SDK 并适配常见浏览器能力调用",
                )
                package = prepare_package(
                    root,
                    stage,
                    public_base_url=html_base,
                    native_client_path=_default_native_client(),
                    host_sdk_path=_default_host_sdk(),
                    browser_compatibility=True,
                )
                _notify_progress(
                    settings,
                    import_id=import_id,
                    attempt_id=attempt_id,
                    stage="browser_qa",
                    stage_index=6,
                    progress_percent=72,
                    detail="正在 Chromium 中验证页面加载、视频和 Bridge 生命周期",
                )
                qa = (
                    run_playwright_qa(package)
                    if settings.html_import_require_playwright
                    else {"skipped": True}
                )
                break
            except HtmlPackageError as exc:
                if build_attempt >= 5 or not _ai_repairable_error(exc):
                    raise HtmlImportError(str(exc)) from exc
                _notify_progress(
                    settings,
                    import_id=import_id,
                    attempt_id=attempt_id,
                    stage="ai_repair",
                    stage_index=5,
                    progress_percent=min(69, 60 + build_attempt * 2),
                    detail=f"确定性适配未通过，正在尝试第 {build_attempt + 1} 次受限 AI 兼容修复",
                )
                error_context = str(exc)
                if repair_feedback:
                    error_context += f"\nPrevious patch rejection: {repair_feedback}"
                ai_response = _request_ai_patch_with_heartbeat(
                    settings,
                    repair_number=build_attempt + 1,
                    import_id=import_id,
                    attempt_id=attempt_id,
                    root=root,
                    entry=selected_entry,
                    inspection=inspection,
                    qa_error=error_context,
                )
                history_item = {
                    "call_number": int(ai_response.get("call_number") or 0),
                    "outcome": str(ai_response.get("outcome") or "unavailable"),
                    "summary": str(ai_response.get("summary") or "")[:1200],
                }
                try:
                    applied = _apply_ai_patch(root, ai_response)
                except HtmlImportError as patch_exc:
                    history_item["patch_rejected"] = str(patch_exc)
                    ai_history.append(history_item)
                    repair_feedback = str(patch_exc)
                    continue
                ai_history.append(history_item)
                if not applied:
                    raise HtmlImportError(str(exc)) from exc
                history_item["applied_edits"] = applied
                applied_ai_edits.extend(applied)
                repair_feedback = ""
                inspection = _scan(root)
                inspection.update({
                    "file_count": len(files),
                    "source_sha256": source_sha256,
                    "derived_copy_modified": True,
                })
                model_caps = {
                    str(value)
                    for value in ai_response.get("required_capabilities") or []
                    if str(value) in ALLOWED_CAPABILITIES
                }
                detected = set(inspection["suggested_capabilities"])
                capabilities = [
                    value
                    for value in ALLOWED_CAPABILITIES
                    if value in detected | set(requested) | model_caps
                ]
                _write_trusted_manifest(
                    root,
                    item_id=item,
                    entry=selected_entry,
                    title=title,
                    description=description,
                    capabilities=capabilities,
                    user_id=author,
                )
        if package is None or qa is None:
            raise HtmlImportError("HTML package preparation did not complete")
        _notify_progress(
            settings,
            import_id=import_id,
            attempt_id=attempt_id,
            stage="uploading_preview",
            stage_index=7,
            progress_percent=86,
            detail="浏览器校验通过，正在上传不可变预览包",
            skipped_stages=[] if ai_history else ["ai_repair"],
        )
        try:
            uploaded = _upload_preview_with_heartbeat(
                settings,
                package=package,
                import_id=import_id,
                attempt_id=attempt_id,
                skipped_stages=[] if ai_history else ["ai_repair"],
            )
        except HtmlPackageError as exc:
            raise HtmlImportError(str(exc)) from exc
        _notify_progress(
            settings,
            import_id=import_id,
            attempt_id=attempt_id,
            stage="finalizing_preview",
            stage_index=7,
            progress_percent=97,
            detail="预览资源已上传，正在保存最终结果",
            skipped_stages=[] if ai_history else ["ai_repair"],
        )
        return {
            "item_id": package.item_id,
            "version": package.version,
            "entry": package.entry,
            "html_url": package.html_url,
            "user_id": package.user_id,
            "required_capabilities": list(package.required_capabilities),
            "package_id": uploaded["package_id"],
            "compatibility_profile": package.compatibility_profile,
            "inspection": inspection,
            "qa": qa,
            "entry_auto_selected_from_multiple": auto_selected_multiple,
            "ai": {
                "used": any(int(item.get("call_number") or 0) > 0 for item in ai_history),
                "calls": len([item for item in ai_history if int(item.get("call_number") or 0) > 0]),
                "derived_copy_modified": bool(applied_ai_edits),
                "applied_edits": applied_ai_edits,
                "history": ai_history,
            },
            "android_preview_command": (
                "adb shell am start -n com.pixopixo.pixoandroid/.debug.PixoRemoteHtmlPreviewActivity "
                f"--es url '{package.html_url}' --es item_id '{package.item_id}' --ei bridge_version 1 "
                f"--es capabilities '{','.join(package.required_capabilities)}'"
            ),
        }


def archive_local_source(
    db: Session,
    settings: Settings,
    *,
    import_id: str,
    source_sha256: str,
    source_bytes: int,
    filename: str,
) -> dict[str, Any]:
    """Back up the untouched spool ZIP to private OSS, idempotently."""
    if not media_mode_is_oss(settings):
        raise HtmlImportError("HTML imports require MEDIA_STORAGE_MODE=oss")
    archive = _local_source(
        settings,
        import_id=import_id,
        expected_sha256=source_sha256,
        expected_bytes=source_bytes,
    )
    existing = (
        db.query(MediaObject)
        .filter(
            MediaObject.purpose == "html_import_source",
            MediaObject.sha256 == source_sha256,
            MediaObject.size_bytes == source_bytes,
            MediaObject.state == "ready",
        )
        .order_by(MediaObject.created_at.asc())
        .first()
    )
    if existing is not None:
        return {"source_object_id": existing.id, "reused": True}

    digest = hashlib.sha256(f"{import_id}:{source_sha256}".encode()).hexdigest()
    media_id = f"mo_his_{digest[:40]}"
    key = object_key(
        settings,
        "private",
        "html-imports",
        import_id,
        "sources",
        media_id,
        "source.zip",
    )
    row = db.get(MediaObject, media_id)
    if row is not None:
        if (
            row.purpose != "html_import_source"
            or row.object_key != key
            or row.sha256 != source_sha256
            or row.size_bytes != source_bytes
            or row.state != "ready"
        ):
            raise HtmlImportError("source backup identity conflicts with existing media")
        return {"source_object_id": row.id, "reused": True}

    try:
        metadata = head_object(settings, key=key)
    except OssObjectNotFoundError:
        upload_file(
            settings,
            key=key,
            path=archive,
            content_type="application/zip",
            public=False,
            immutable=True,
            extra_headers={
                "x-oss-meta-sha256": source_sha256,
                "x-oss-meta-pixo-import-id": import_id,
            },
        )
        metadata = head_object(settings, key=key)
    if metadata.size_bytes != source_bytes:
        raise HtmlImportError("source backup size does not match OSS metadata")
    stored_hash = metadata.headers.get("x-oss-meta-sha256", "").lower()
    if stored_hash and stored_hash != source_sha256:
        raise HtmlImportError("source backup checksum metadata does not match")
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.lower().endswith(".zip"):
        safe_name = "source.zip"
    row = MediaObject(
        id=media_id,
        upload_session_id=None,
        purpose="html_import_source",
        origin="internal_upload",
        visibility="private",
        state="ready",
        staging_key=key,
        object_key=key,
        original_filename=safe_name,
        content_type="application/zip",
        size_bytes=source_bytes,
        sha256=source_sha256,
        etag=metadata.etag or "",
        extra_json={"import_id": import_id, "archived_from": "shared_spool"},
        verified_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return {"source_object_id": row.id, "reused": False}
