from __future__ import annotations

from urllib.parse import quote, urlencode

from app.html_content import CONTENT_TYPE_RUNTIME


def legacy_share_url(public_share_base_url: str, item_id: str) -> str:
    relative = f"/api/v1/share/{quote(item_id, safe='')}"
    base = public_share_base_url.strip().rstrip("/")
    return f"{base}{relative}" if base else relative


def runtime_experience_url(public_game_base_url: str, item_id: str) -> str:
    base = public_game_base_url.strip().rstrip("/")
    if not base or not item_id:
        return ""
    return f"{base}/?{urlencode({'experience': item_id})}"


def published_share_url(
    *,
    content_type: str,
    item_id: str,
    public_game_base_url: str,
    public_share_base_url: str,
) -> str:
    if content_type == CONTENT_TYPE_RUNTIME:
        experience_url = runtime_experience_url(public_game_base_url, item_id)
        if experience_url:
            return experience_url
    return legacy_share_url(public_share_base_url, item_id)
