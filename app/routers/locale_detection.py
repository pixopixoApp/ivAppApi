from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.config import Settings, get_settings
from app.geo_locale import country_code_for_ip
from app.locale import locale_for_country, parse_accept_language

router = APIRouter(prefix="/api/v1/public", tags=["public-locale"])


@router.get("/locale")
def detect_locale(
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    response.headers["Cache-Control"] = "private, no-store"
    accepted = parse_accept_language(request.headers.get("accept-language"))
    if accepted:
        return {"locale": accepted[0], "source": "accept_language"}
    client_ip = request.client.host if request.client else ""
    if settings.geoip_trust_proxy_headers:
        # Production Nginx overwrites X-Real-IP. X-Forwarded-For is never read,
        # so a client-supplied chain cannot affect country selection.
        client_ip = request.headers.get("x-real-ip") or ""
    country = country_code_for_ip(
        client_ip,
        database_path=settings.geoip_country_database,
    )
    return {
        "locale": locale_for_country(country),
        "source": "geoip" if country else "default",
    }
