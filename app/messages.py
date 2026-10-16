from __future__ import annotations

from typing import Any

_INSUFFICIENT_CREDITS = {
    "en": "You need {required} Credits.",
    "zh-Hans": "你需要 {required} Credits。",
    "zh-Hant": "你需要 {required} Credits。",
    "ja": "{required} Credits が必要です。",
    "ko": "{required} Credits가 필요합니다.",
    "es": "Necesitas {required} Credits.",
    "pt-BR": "Você precisa de {required} Credits.",
    "fr": "Vous avez besoin de {required} Credits.",
    "de": "Du benötigst {required} Credits.",
}


def localized_error(
    locale: str,
    *,
    error_code: str,
    message_key: str,
    message_args: dict[str, Any],
    fallback: str,
) -> dict[str, Any]:
    message = fallback
    if message_key == "errors.insufficientCredits":
        message = _INSUFFICIENT_CREDITS.get(locale, _INSUFFICIENT_CREDITS["en"]).format(
            **message_args
        )
    return {
        "code": error_code,
        "error_code": error_code,
        "message_key": message_key,
        "message_args": message_args,
        "message": message,
    }
