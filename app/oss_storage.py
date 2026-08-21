from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from threading import BoundedSemaphore, local
from typing import BinaryIO
from urllib.parse import urlsplit

from app.config import Settings


class OssStorageError(RuntimeError):
    pass


class OssObjectNotFoundError(OssStorageError):
    pass


class OssImmutableConflictError(OssStorageError):
    pass


@dataclass(frozen=True)
class OssObjectMetadata:
    size_bytes: int
    content_type: str
    etag: str | None
    headers: dict[str, str]


_thread_state = local()
_slots: BoundedSemaphore | None = None
_DOWNLOAD_MAX_ATTEMPTS = 6
_DOWNLOAD_MULTIGET_THRESHOLD_BYTES = 8 * 1024 * 1024
_DOWNLOAD_PART_SIZE_BYTES = 4 * 1024 * 1024
_DOWNLOAD_MAX_THREADS = 4


def _oss2():
    try:
        import oss2
    except ImportError as exc:  # pragma: no cover - deployment/configuration error
        raise OssStorageError("oss2 is required when MEDIA_STORAGE_MODE=oss") from exc
    return oss2


def is_transient_oss_error(exc: Exception) -> bool:
    """Return whether an OSS failure is safe to retry without changing intent."""
    oss2 = _oss2()
    if isinstance(exc, oss2.exceptions.RequestError):
        return True
    if isinstance(exc, oss2.exceptions.ServerError):
        status = int(getattr(exc, "status", 0) or 0)
        return status in {408, 429} or status >= 500
    return False


def _is_retryable_download_error(exc: Exception) -> bool:
    # oss2 wraps failures that happen while reading a response body in a raw
    # requests ConnectionError instead of its own RequestError.  Treat both as
    # transient, while keeping authorization, precondition and validation
    # failures non-retryable.
    try:
        import requests

        if isinstance(
            exc,
            (requests.exceptions.ConnectionError, requests.exceptions.Timeout),
        ):
            return True
    except ImportError:  # pragma: no cover - requests is an oss2 dependency
        pass
    return isinstance(exc, (TimeoutError, ConnectionError)) or is_transient_oss_error(exc)


def _download_error(exc: Exception) -> OssStorageError:
    return OssStorageError("OSS object download failed after retrying transient errors")


def _root(settings: Settings) -> str:
    configured = settings.oss_root_prefix.strip().replace("\\", "/")
    raw = configured.strip("/")
    path = PurePosixPath(raw)
    if (
        not raw
        or configured != raw
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
        or len(path.parts) < 2
        or path.parts[0] != "ivapp-media"
    ):
        raise OssStorageError(
            "OSS_ROOT_PREFIX must use the dedicated ivapp-media/<version> namespace"
        )
    return path.as_posix()


def object_key(settings: Settings, *parts: str) -> str:
    normalized: list[str] = []
    for raw in parts:
        value = str(raw).strip().strip("/").replace("\\", "/")
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or any(part in ("", ".", "..") for part in path.parts)
        ):
            raise OssStorageError("unsafe OSS object key component")
        normalized.extend(path.parts)
    return "/".join((_root(settings), *normalized))


def assert_owned_key(settings: Settings, raw_key: str) -> str:
    value = raw_key.strip().replace("\\", "/")
    path = PurePosixPath(value)
    root = _root(settings)
    if (
        not value
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
        or not value.startswith(root + "/")
    ):
        raise OssStorageError("object key is outside the ivapp prefix")
    return path.as_posix()


def _required_config(settings: Settings) -> tuple[str, str, str, str, str]:
    values = (
        settings.aliyun_oss_region.strip(),
        settings.aliyun_oss_bucket.strip(),
        settings.aliyun_oss_access_key_id.strip(),
        settings.aliyun_oss_access_key_secret.strip(),
        settings.aliyun_oss_public_base_url.strip().rstrip("/"),
    )
    if not all(values):
        raise OssStorageError("incomplete ALIYUN_OSS_* configuration")
    public_base = urlsplit(values[4])
    if (
        public_base.scheme.lower() != "https"
        or not public_base.hostname
        or public_base.username is not None
        or public_base.password is not None
        or public_base.path not in ("", "/")
        or public_base.query
        or public_base.fragment
    ):
        raise OssStorageError(
            "ALIYUN_OSS_PUBLIC_BASE_URL must be an HTTPS origin without a path"
        )
    _root(settings)
    authority = (
        public_base.hostname.lower()
        if public_base.port in (None, 443)
        else f"{public_base.hostname.lower()}:{public_base.port}"
    )
    return (*values[:4], f"https://{authority}")


def validate_oss_config(settings: Settings) -> None:
    """Fail fast on incomplete credentials or an unsafe/shared prefix."""
    _required_config(settings)


def _endpoint(region: str) -> str:
    normalized = region if region.startswith("oss-") else f"oss-{region}"
    return f"https://{normalized}.aliyuncs.com"


def _bucket(settings: Settings):
    oss2 = _oss2()
    region, bucket_name, key_id, key_secret, _public = _required_config(settings)
    signature = (region, bucket_name, key_id, key_secret)
    cached = getattr(_thread_state, "bucket", None)
    if cached is not None and getattr(_thread_state, "signature", None) == signature:
        return cached
    bucket = oss2.Bucket(
        oss2.Auth(key_id, key_secret),
        _endpoint(region),
        bucket_name,
        session=oss2.Session(),
        connect_timeout=float(settings.oss_connect_timeout_seconds),
        region=region,
    )
    _thread_state.bucket = bucket
    _thread_state.signature = signature
    return bucket


def _slot(settings: Settings):
    global _slots
    if _slots is None:
        _slots = BoundedSemaphore(max(1, int(settings.oss_max_concurrency)))
    return _slots


def public_url(settings: Settings, key: str) -> str:
    *_private, base = _required_config(settings)
    return f"{base}/{assert_owned_key(settings, key)}"


def delete_object(settings: Settings, *, key: str) -> None:
    """Delete one object inside the dedicated ivapp namespace only."""
    owned = assert_owned_key(settings, key)
    with _slot(settings):
        _bucket(settings).delete_object(owned)


def _headers(
    *,
    content_type: str,
    public: bool,
    immutable: bool,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    headers = {
        "Content-Type": content_type,
        "Content-Disposition": "inline" if public else "attachment",
        "Cache-Control": (
            "public, max-age=31536000, immutable"
            if public and immutable
            else "no-store"
        ),
        "x-oss-object-acl": "public-read" if public else "private",
        "x-oss-forbid-overwrite": "true",
    }
    if extra:
        headers.update({str(name): str(value) for name, value in extra.items()})
    return headers


def upload_file(
    settings: Settings,
    *,
    key: str,
    path: str | Path,
    content_type: str | None = None,
    public: bool = False,
    immutable: bool = True,
    extra_headers: Mapping[str, str] | None = None,
) -> str:
    owned = assert_owned_key(settings, key)
    file_path = Path(path)
    media_type = content_type or mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    with _slot(settings):
        _bucket(settings).put_object_from_file(
            owned,
            str(file_path),
            headers=_headers(
                content_type=media_type,
                public=public,
                immutable=immutable,
                extra=extra_headers,
            ),
        )
    return public_url(settings, owned) if public else owned


def upload_bytes(
    settings: Settings,
    *,
    key: str,
    payload: bytes | BinaryIO,
    content_type: str,
    public: bool = False,
    immutable: bool = True,
    extra_headers: Mapping[str, str] | None = None,
) -> str:
    owned = assert_owned_key(settings, key)
    with _slot(settings):
        _bucket(settings).put_object(
            owned,
            payload,
            headers=_headers(
                content_type=content_type,
                public=public,
                immutable=immutable,
                extra=extra_headers,
            ),
        )
    return public_url(settings, owned) if public else owned


def head_object(settings: Settings, *, key: str) -> OssObjectMetadata:
    oss2 = _oss2()
    try:
        with _slot(settings):
            result = _bucket(settings).head_object(assert_owned_key(settings, key))
    except oss2.exceptions.NoSuchKey as exc:
        raise OssObjectNotFoundError("OSS object does not exist") from exc
    raw_headers = getattr(result, "headers", {}) or {}
    headers = {str(name).lower(): str(value) for name, value in raw_headers.items()}
    return OssObjectMetadata(
        size_bytes=int(getattr(result, "content_length", 0) or headers.get("content-length") or 0),
        content_type=str(getattr(result, "content_type", "") or headers.get("content-type") or ""),
        etag=str(getattr(result, "etag", "") or headers.get("etag") or "") or None,
        headers=headers,
    )


def download_file(
    settings: Settings,
    *,
    key: str,
    path: str | Path,
    expected_etag: str | None = None,
) -> None:
    owned = assert_owned_key(settings, key)
    destination = Path(path)
    partial = destination.with_name(f".{destination.name}.pixo-part")
    partial.unlink(missing_ok=True)

    metadata: OssObjectMetadata | None = None
    last_error: Exception | None = None
    for _attempt in range(_DOWNLOAD_MAX_ATTEMPTS):
        try:
            metadata = head_object(settings, key=owned)
            break
        except OssObjectNotFoundError:
            raise
        except Exception as exc:
            last_error = exc
            if not _is_retryable_download_error(exc):
                if isinstance(exc, OssStorageError):
                    raise
                raise _download_error(exc) from exc
    if metadata is None:
        assert last_error is not None
        raise _download_error(last_error) from last_error

    if expected_etag and (
        not metadata.etag
        or expected_etag.strip().strip('"') != metadata.etag.strip().strip('"')
    ):
        raise OssStorageError("OSS object ETag changed before download")

    oss2 = _oss2()
    bucket = _bucket(settings)
    checkpoint_store = oss2.ResumableDownloadStore(
        root=str(destination.parent),
        dir=".pixo-oss-downloads",
    )
    checkpoint_key = checkpoint_store.make_store_key(
        bucket.bucket_name,
        owned,
        str(partial),
    )
    failures = 0
    try:
        while True:
            try:
                with _slot(settings):
                    oss2.resumable_download(
                        bucket,
                        owned,
                        str(partial),
                        multiget_threshold=_DOWNLOAD_MULTIGET_THRESHOLD_BYTES,
                        part_size=_DOWNLOAD_PART_SIZE_BYTES,
                        num_threads=max(
                            1,
                            min(
                                _DOWNLOAD_MAX_THREADS,
                                int(settings.oss_max_concurrency),
                            ),
                        ),
                        store=checkpoint_store,
                    )
                break
            except Exception as exc:
                failures += 1
                if (
                    failures >= _DOWNLOAD_MAX_ATTEMPTS
                    or not _is_retryable_download_error(exc)
                ):
                    if isinstance(exc, OssStorageError):
                        raise
                    raise _download_error(exc) from exc
                # The SDK checkpoint records every completed range. Retrying
                # the same source/destination downloads only missing parts.
                continue
        if partial.stat().st_size != metadata.size_bytes:
            raise OssStorageError("OSS object download size does not match metadata")
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        for temporary in destination.parent.glob(f"{partial.name}.tmp-*"):
            temporary.unlink(missing_ok=True)
        try:
            checkpoint_store.delete(checkpoint_key)
        except FileNotFoundError:
            pass
        raise


def read_bytes(settings: Settings, *, key: str, max_bytes: int = 8 * 1024 * 1024) -> bytes:
    with _slot(settings):
        result = _bucket(settings).get_object(assert_owned_key(settings, key))
        payload = result.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise OssStorageError("OSS object exceeds in-memory read limit")
    return payload


def copy_object(
    settings: Settings,
    *,
    source_key: str,
    target_key: str,
    content_type: str,
    public: bool,
    immutable: bool = True,
    expected_etag: str | None = None,
    extra_headers: Mapping[str, str] | None = None,
) -> str:
    _region, bucket_name, *_rest = _required_config(settings)
    headers = _headers(
        content_type=content_type,
        public=public,
        immutable=immutable,
        extra=extra_headers,
    )
    headers["x-oss-metadata-directive"] = "REPLACE"
    if expected_etag:
        value = expected_etag if expected_etag.startswith('"') else f'"{expected_etag}"'
        headers["x-oss-copy-source-if-match"] = value
    oss2 = _oss2()
    try:
        with _slot(settings):
            _bucket(settings).copy_object(
                bucket_name,
                assert_owned_key(settings, source_key),
                assert_owned_key(settings, target_key),
                headers=headers,
            )
    except oss2.exceptions.ServerError as exc:
        status = int(getattr(exc, "status", 0) or 0)
        code = str(getattr(exc, "code", "") or "")
        if status in {409, 412} or code == "ObjectAlreadyExists":
            raise OssImmutableConflictError(
                "immutable OSS target already exists"
            ) from exc
        raise
    return public_url(settings, target_key) if public else target_key


def sign_get_url(
    settings: Settings,
    *,
    key: str,
    expires_seconds: int | None = None,
    filename: str | None = None,
) -> str:
    ttl = int(expires_seconds or settings.oss_private_get_ttl_seconds)
    ttl = max(30, min(3600, ttl))
    params = None
    if filename:
        safe = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        params = {"response-content-disposition": f'inline; filename="{safe}"'}
    return _bucket(settings).sign_url(
        "GET",
        assert_owned_key(settings, key),
        ttl,
        params=params,
        slash_safe=True,
    )


def _post_region(region: str) -> str:
    return region.removeprefix("oss-")


def _hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def create_post_upload(
    settings: Settings,
    *,
    key: str,
    content_type: str,
    size_bytes: int,
    expires_at: datetime,
    extra_fields: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Return an exact-key/exact-size private OSS PostObject V4 policy."""
    region, bucket_name, key_id, key_secret, _public = _required_config(settings)
    owned = assert_owned_key(settings, key)
    normalized_type = content_type.split(";", 1)[0].strip().lower()
    now = datetime.now(timezone.utc)
    expiry = expires_at.astimezone(timezone.utc)
    date = now.strftime("%Y%m%d")
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    signing_region = _post_region(region.lower())
    credential = f"{key_id}/{date}/{signing_region}/oss/aliyun_v4_request"
    fields: dict[str, str] = {
        "key": owned,
        "success_action_status": "200",
        "x-oss-signature-version": "OSS4-HMAC-SHA256",
        "x-oss-credential": credential,
        "x-oss-date": timestamp,
        "x-oss-content-type": normalized_type,
        "Cache-Control": "no-store",
        "Content-Disposition": "attachment",
        "x-oss-object-acl": "private",
        "x-oss-forbid-overwrite": "true",
    }
    if extra_fields:
        fields.update({str(name): str(value) for name, value in extra_fields.items()})
    conditions: list[object] = [
        {"bucket": bucket_name},
        {"x-oss-signature-version": fields["x-oss-signature-version"]},
        {"x-oss-credential": credential},
        {"x-oss-date": timestamp},
        ["eq", "$key", owned],
        ["content-length-range", int(size_bytes), int(size_bytes)],
        ["eq", "$success_action_status", fields["success_action_status"]],
        ["eq", "$x-oss-content-type", normalized_type],
        ["eq", "$cache-control", fields["Cache-Control"]],
        ["eq", "$content-disposition", fields["Content-Disposition"]],
        ["eq", "$x-oss-object-acl", fields["x-oss-object-acl"]],
        ["eq", "$x-oss-forbid-overwrite", fields["x-oss-forbid-overwrite"]],
    ]
    for name, value in (extra_fields or {}).items():
        conditions.append(["eq", f"${name}", str(value)])
    policy = {
        "expiration": expiry.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "conditions": conditions,
    }
    encoded = base64.b64encode(
        json.dumps(policy, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")
    date_key = _hmac(f"aliyun_v4{key_secret}".encode(), date)
    region_key = _hmac(date_key, signing_region)
    service_key = _hmac(region_key, "oss")
    signing_key = _hmac(service_key, "aliyun_v4_request")
    fields["policy"] = encoded
    fields["x-oss-signature"] = hmac.new(
        signing_key,
        encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    endpoint_region = region if region.startswith("oss-") else f"oss-{region}"
    return f"https://{bucket_name}.{endpoint_region}.aliyuncs.com", fields
