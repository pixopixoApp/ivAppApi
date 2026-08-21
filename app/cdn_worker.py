from __future__ import annotations

import signal
import time

from app.cdn_cache import CdnCacheError, process_once, validate_cdn_config
from app.config import get_settings
from app.db import SessionLocal
from app.logging_config import get_logger, setup_logging

log = get_logger(__name__)
_stop = False


def _handle_signal(_signum, _frame) -> None:
    global _stop
    _stop = True


def main() -> None:
    settings = get_settings()
    setup_logging(level=settings.log_level)
    validate_cdn_config(settings)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    log.info("CDN cache worker started enabled=%s", settings.cdn_cache_enabled)
    while not _stop:
        try:
            with SessionLocal() as db:
                processed = process_once(db, settings)
        except CdnCacheError:
            log.exception("CDN cache worker configuration failure")
            processed = 0
        except Exception:
            log.exception("CDN cache worker batch failed")
            processed = 0
        if processed == 0:
            time.sleep(max(0.25, settings.cdn_worker_poll_seconds))
    log.info("CDN cache worker stopped")


if __name__ == "__main__":
    main()
