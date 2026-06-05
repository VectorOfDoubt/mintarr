"""Opt-in outbound notifications via Apprise (Phase 3 slice 4).

Default-off: a no-op unless ``MINTARR_NOTIFY_URLS`` is set (comma-separated
Apprise URLs, e.g. ``tgram://token/chat,ntfy://host/topic``). Secret-safe: the
URLs live in the environment and are never logged. This module never raises into
its caller — a notification failure must not affect the import pipeline.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("tidalhires.notify")


def _configured_urls() -> list[str]:
    raw = os.environ.get("MINTARR_NOTIFY_URLS", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def is_enabled() -> bool:
    return bool(_configured_urls())


def notify(title: str, body: str) -> bool:
    """Send a notification to all configured Apprise targets.

    Returns True if at least one target accepted it. No-op (False) when no URLs
    are configured or Apprise is unavailable. Never raises.
    """
    urls = _configured_urls()
    if not urls:
        return False
    try:
        import apprise  # imported lazily so the dep is optional at runtime
    except Exception:
        log.warning("notifications: MINTARR_NOTIFY_URLS set but apprise missing")
        return False
    try:
        ap = apprise.Apprise()
        for url in urls:
            ap.add(url)
        return bool(ap.notify(title=title, body=body))
    except Exception:
        # Do not log the URLs — they can contain tokens.
        log.exception("notifications: send failed")
        return False
