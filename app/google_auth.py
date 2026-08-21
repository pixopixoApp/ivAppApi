from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import requests
from google.auth.exceptions import TransportError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from app.logging_config import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str


class GoogleAuthUnavailable(ValueError):
    pass


class _TimeoutRequest(google_requests.Request):
    def __init__(self, *, timeout_seconds: float) -> None:
        super().__init__(session=requests.Session())
        self._timeout_seconds = timeout_seconds

    def __call__(self, *args, **kwargs):
        kwargs["timeout"] = min(
            float(kwargs.get("timeout") or self._timeout_seconds),
            self._timeout_seconds,
        )
        return super().__call__(*args, **kwargs)


def verify_google_id_token(
    *, token: str, client_ids: Sequence[str], timeout_seconds: float = 5.0
) -> GoogleIdentity:
    """Verify Google ID token; raise ValueError on failure."""
    raw = token.strip() if isinstance(token, str) else ""
    audiences = tuple(dict.fromkeys(
        value.strip() for value in client_ids if isinstance(value, str) and value.strip()
    ))
    if not raw:
        raise ValueError("id_token required")
    if not audiences:
        raise ValueError("google client ids not configured")

    transport = _TimeoutRequest(timeout_seconds=timeout_seconds)
    try:
        claims = google_id_token.verify_oauth2_token(
            raw,
            transport,
            list(audiences),
        )
    except (requests.RequestException, TransportError) as exc:
        log.warning("google id_token network failure: %s", exc)
        raise GoogleAuthUnavailable("google sign-in service unavailable") from exc
    except Exception as exc:
        log.warning("google id_token verify failed: %s", exc)
        raise ValueError("invalid id_token") from exc
    finally:
        transport.session.close()

    iss = str(claims.get("iss") or "")
    if iss not in ("accounts.google.com", "https://accounts.google.com"):
        raise ValueError("invalid id_token issuer")

    sub = str(claims.get("sub") or "").strip()
    if not sub:
        raise ValueError("id_token missing sub")

    email = str(claims.get("email") or "").strip().lower()
    return GoogleIdentity(subject=sub, email=email)
