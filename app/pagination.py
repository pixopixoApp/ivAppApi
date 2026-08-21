from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any


class CursorError(ValueError):
    pass


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode((raw + padding).encode("ascii"))
    except Exception as exc:
        raise CursorError("invalid cursor encoding") from exc


def encode_cursor(*, kind: str, values: dict[str, Any], secret: str) -> str:
    payload = {"v": 1, "kind": kind, "values": values}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{_b64encode(raw)}.{_b64encode(signature)}"


def decode_cursor(*, cursor: str, kind: str, secret: str) -> dict[str, Any]:
    if not isinstance(cursor, str) or not cursor.strip():
        raise CursorError("cursor required")
    encoded, separator, encoded_signature = cursor.strip().partition(".")
    if not separator:
        raise CursorError("invalid cursor")
    raw = _b64decode(encoded)
    signature = _b64decode(encoded_signature)
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise CursorError("invalid cursor signature")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorError("invalid cursor payload") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise CursorError("unsupported cursor version")
    if payload.get("kind") != kind:
        raise CursorError("cursor does not belong to this list")
    values = payload.get("values")
    if not isinstance(values, dict):
        raise CursorError("invalid cursor values")
    return values


def datetime_to_cursor_value(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def datetime_from_cursor_value(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CursorError("cursor timestamp missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CursorError("invalid cursor timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
