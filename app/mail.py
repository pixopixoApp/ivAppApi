from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import Settings
from app.logging_config import get_logger

log = get_logger(__name__)


def _send_message(
    settings: Settings,
    *,
    email: str,
    subject: str,
    body: str,
    purpose: str,
) -> None:
    if not settings.smtp_host.strip():
        log.info("smtp disabled; email skipped purpose=%s email=%s", purpose, email)
        return

    from_addr = settings.smtp_from.strip() or settings.smtp_user.strip()
    host = settings.smtp_host.strip()
    port = settings.smtp_port
    use_ssl = settings.smtp_ssl or settings.smtp_port == 465

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = email
    msg.set_content(body)

    log.info(
        "smtp send start purpose=%s host=%s port=%s ssl=%s tls=%s from=%s to=%s",
        purpose,
        host,
        port,
        use_ssl,
        settings.smtp_tls and not use_ssl,
        from_addr,
        email,
    )

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                refused = smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                if settings.smtp_tls:
                    smtp.starttls()
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                refused = smtp.send_message(msg)
    except smtplib.SMTPException:
        log.exception(
            "smtp send failed purpose=%s email=%s host=%s port=%s",
            purpose,
            email,
            host,
            port,
        )
        raise
    except Exception:
        log.exception(
            "smtp send error purpose=%s email=%s host=%s port=%s",
            purpose,
            email,
            host,
            port,
        )
        raise

    if refused:
        log.warning(
            "smtp send refused purpose=%s email=%s host=%s refused=%s",
            purpose,
            email,
            host,
            refused,
        )
        raise RuntimeError(f"smtp refused recipients: {refused}")

    log.info("smtp send ok purpose=%s email=%s refused=%s", purpose, email, refused)


def send_verification_code(settings: Settings, *, email: str, code: str) -> None:
    """Send a short-lived login code. Empty smtp_host logs only in development."""
    if not settings.smtp_host.strip():
        log.info("smtp disabled; verification code email=%s code=%s", email, code)
        return
    minutes = max(1, settings.code_ttl_seconds // 60)
    _send_message(
        settings,
        email=email,
        subject="Your Pixopixo verification code",
        body=f"Your verification code is {code}. It expires in {minutes} minutes.",
        purpose="verification_code",
    )


def send_creator_invite(settings: Settings, *, email: str, code: str) -> None:
    """Deliver one single-use creator code to an approved waitlist applicant."""
    _send_message(
        settings,
        email=email,
        subject="Your Pixo creator access code",
        body=(
            "Your Pixo creator access request is ready.\n\n"
            f"Creator code: {code}\n\n"
            "Open https://www.pixopixo.com/create and enter this code to unlock "
            "creator access. The code works once and is assigned to your account.\n\n"
            "If you did not request creator access, you can ignore this email."
        ),
        purpose="creator_invite",
    )
