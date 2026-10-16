from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.locale import canonical_locale

_CATALOG_DIR = Path(__file__).with_name("locales")


@lru_cache(maxsize=9)
def _catalog(locale: str) -> dict[str, Any]:
    path = _CATALOG_DIR / f"{locale}.json"
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise TypeError(f"server locale catalog must be an object: {path}")
    return value


def _message_template(locale: str, message_key: str) -> str | None:
    value: Any = _catalog(locale)
    for part in message_key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value if isinstance(value, str) and value.strip() else None


def localized_message(
    locale: str,
    *,
    message_key: str,
    message_args: dict[str, Any],
    fallback: str,
) -> str:
    """Render server-owned static copy; creator content never enters this catalog."""
    resolved = canonical_locale(locale) or "en"
    template = _message_template(resolved, message_key)
    if template is None:
        template = _message_template("en", message_key)
    if template is None:
        return fallback
    try:
        return template.format(**message_args)
    except (KeyError, IndexError, ValueError):
        return fallback


def localized_error(
    locale: str,
    *,
    error_code: str,
    message_key: str,
    message_args: dict[str, Any],
    fallback: str,
) -> dict[str, Any]:
    message = localized_message(
        locale,
        message_key=message_key,
        message_args=message_args,
        fallback=fallback,
    )
    return {
        "code": error_code,
        "error_code": error_code,
        "message_key": message_key,
        "message_args": message_args,
        "message": message,
    }
