from __future__ import annotations

from app.share_urls import legacy_share_url, published_share_url, runtime_experience_url


def test_runtime_experience_url_is_stable_and_url_encoded() -> None:
    assert runtime_experience_url(
        "https://pixopixo.com/",
        "7e92cae8-a6a9-40f3-9337-232d9a38edc9",
    ) == (
        "https://pixopixo.com/experience/"
        "7e92cae8-a6a9-40f3-9337-232d9a38edc9"
    )
    assert runtime_experience_url(
        "https://demo.pixopixo.cn/game/",
        "7e92cae8-a6a9-40f3-9337-232d9a38edc9",
    ) == (
        "https://demo.pixopixo.cn/game/"
        "?experience=7e92cae8-a6a9-40f3-9337-232d9a38edc9"
    )
    assert runtime_experience_url(
        "https://demo.pixopixo.cn/game",
        "folder/item one",
    ) == "https://demo.pixopixo.cn/game/?experience=folder%2Fitem+one"


def test_published_share_url_is_content_aware() -> None:
    common = {
        "item_id": "work-1",
        "public_game_base_url": "https://demo.pixopixo.cn/game/",
        "public_share_base_url": "https://api.pixopixo.cn",
    }
    assert published_share_url(content_type="runtime", **common) == (
        "https://demo.pixopixo.cn/game/?experience=work-1"
    )
    assert published_share_url(content_type="html", **common) == (
        "https://api.pixopixo.cn/api/v1/share/work-1"
    )
    assert published_share_url(
        content_type="runtime",
        seo_slug="play-the-rain-work1",
        seo_public_base_url="https://demo.pixopixo.cn",
        **common,
    ) == "https://demo.pixopixo.cn/experiences/play-the-rain-work1"


def test_legacy_share_url_remains_available_without_a_public_origin() -> None:
    assert legacy_share_url("", "folder/item") == "/api/v1/share/folder%2Fitem"
    assert published_share_url(
        content_type="runtime",
        item_id="work-1",
        public_game_base_url="",
        public_share_base_url="https://api.pixopixo.cn/",
    ) == "https://api.pixopixo.cn/api/v1/share/work-1"
