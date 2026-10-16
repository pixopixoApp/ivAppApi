from __future__ import annotations

import re
from collections.abc import Iterable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

SUPPORTED_LOCALES = (
    "en",
    "zh-Hans",
    "zh-Hant",
    "ja",
    "ko",
    "es",
    "pt-BR",
    "fr",
    "de",
)
SUPPORTED_LOCALE_SET = frozenset(SUPPORTED_LOCALES)

_SPANISH_COUNTRIES = frozenset({
    "AR", "BO", "CL", "CO", "CR", "CU", "DO", "EC", "ES", "GT",
    "HN", "MX", "NI", "PA", "PE", "PR", "PY", "SV", "UY", "VE",
})

_EXACT_ALIASES = {
    "zh-cn": "zh-Hans",
    "zh-sg": "zh-Hans",
    "zh-hans": "zh-Hans",
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-mo": "zh-Hant",
    "zh-hant": "zh-Hant",
    "pt-br": "pt-BR",
}
_LANGUAGE_ALIASES = {
    "en": "en",
    "ja": "ja",
    "ko": "ko",
    "es": "es",
    "pt": "pt-BR",
    "fr": "fr",
    "de": "de",
}
_Q_RE = re.compile(r"^q=(0(?:\.\d{0,3})?|1(?:\.0{0,3})?)$", re.IGNORECASE)


def canonical_locale(tag: str | None) -> str | None:
    value = str(tag or "").strip().replace("_", "-").lower()
    if not value or value == "*":
        return None
    exact = _EXACT_ALIASES.get(value)
    if exact:
        return exact
    language = value.split("-", 1)[0]
    if language == "zh":
        # Bare Chinese has no script signal. Simplified is the product default.
        return "zh-Hans"
    return _LANGUAGE_ALIASES.get(language)


def parse_accept_language(value: str | None) -> list[str]:
    """Resolve RFC-style weighted language ranges, preserving header order."""
    weighted: list[tuple[float, int, str]] = []
    for index, raw_part in enumerate(str(value or "").split(",")):
        pieces = [part.strip() for part in raw_part.split(";") if part.strip()]
        if not pieces:
            continue
        quality = 1.0
        invalid = False
        for parameter in pieces[1:]:
            match = _Q_RE.match(parameter)
            if match is None:
                if parameter.lower().startswith("q="):
                    invalid = True
                continue
            try:
                quality = float(match.group(1))
            except ValueError:
                invalid = True
        locale = canonical_locale(pieces[0])
        if invalid or quality <= 0 or locale is None:
            continue
        weighted.append((quality, index, locale))
    weighted.sort(key=lambda item: (-item[0], item[1]))
    result: list[str] = []
    for _quality, _index, locale in weighted:
        if locale not in result:
            result.append(locale)
    return result


def resolve_accept_language(value: str | None, *, default: str = "en") -> str:
    return next(iter(parse_accept_language(value)), default)


def locale_for_country(value: str | None) -> str:
    country = str(value or "").strip().upper()
    if country == "CN":
        return "zh-Hans"
    if country in {"TW", "HK", "MO"}:
        return "zh-Hant"
    if country == "JP":
        return "ja"
    if country == "KR":
        return "ko"
    if country == "BR":
        return "pt-BR"
    if country in _SPANISH_COUNTRIES:
        return "es"
    if country == "FR":
        return "fr"
    if country in {"DE", "AT"}:
        return "de"
    return "en"


def append_vary(existing: str | None, values: Iterable[str]) -> str:
    parts = [part.strip() for part in str(existing or "").split(",") if part.strip()]
    lowered = {part.lower() for part in parts}
    for value in values:
        if value.lower() not in lowered:
            parts.append(value)
            lowered.add(value.lower())
    return ", ".join(parts)


def request_locale(request: Request) -> str:
    value = getattr(request.state, "locale", None)
    return value if value in SUPPORTED_LOCALE_SET else "en"


class LocaleMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        locale = resolve_accept_language(request.headers.get("accept-language"))
        request.state.locale = locale
        response = await call_next(request)
        response.headers["Content-Language"] = locale
        response.headers["Vary"] = append_vary(
            response.headers.get("Vary"),
            ("Accept-Language",),
        )
        return response
