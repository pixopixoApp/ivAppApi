from __future__ import annotations

from dataclasses import dataclass

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from app.logging_config import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str


def verify_google_id_token(*, token: str, client_id: str) -> GoogleIdentity:
    """Verify Google ID token; raise ValueError on failure."""
    raw = token.strip() if isinstance(token, str) else ""
    audience = client_id.strip() if isinstance(client_id, str) else ""
    if not raw:
        raise ValueError("id_token required")
    if not audience:
        raise ValueError("google client id not configured")

    try:
        claims = google_id_token.verify_oauth2_token(
            raw,
            google_requests.Request(),
            audience,
        )
    except Exception as exc:  # noqa: BLE001 — library raises various errors
        log.warning("google id_token verify failed: %s", exc)
        raise ValueError("invalid id_token") from exc

    iss = str(claims.get("iss") or "")
    if iss not in ("accounts.google.com", "https://accounts.google.com"):
        raise ValueError("invalid id_token issuer")

    sub = str(claims.get("sub") or "").strip()
    if not sub:
        raise ValueError("id_token missing sub")

    email = str(claims.get("email") or "").strip().lower()
    return GoogleIdentity(subject=sub, email=email)
