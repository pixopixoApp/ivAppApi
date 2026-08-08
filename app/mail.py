from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import Settings
from app.logging_config import get_logger

log = get_logger(__name__)


def send_verification_code(settings: Settings, *, email: str, code: str) -> None:
    """Send 6-digit code via SMTP. Empty smtp_host → log code only (dev)."""
    subject = "验证码"
    minutes = max(1, settings.code_ttl_seconds // 60)
    body = f"您的验证码是 {code}，{minutes} 分钟内有效。"
    if not settings.smtp_host.strip():
        log.info("smtp disabled; verification code email=%s code=%s", email, code)
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
        "smtp send start host=%s port=%s ssl=%s tls=%s from=%s to=%s",
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
        log.exception("smtp send failed email=%s host=%s port=%s", email, host, port)
        raise
    except Exception:
        log.exception("smtp send error email=%s host=%s port=%s", email, host, port)
        raise

    if refused:
        log.warning(
            "smtp send refused email=%s host=%s refused=%s",
            email,
            host,
            refused,
        )
        raise RuntimeError(f"smtp refused recipients: {refused}")

    log.info("smtp send ok email=%s refused=%s", email, refused)
