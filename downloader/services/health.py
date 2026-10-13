"""Is anything actually going to process queued downloads?"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

NO_WORKER_MESSAGE = (
    "The download service is offline right now (no background worker is running). Please try again later."
)
STALE_MESSAGE = (
    "No background worker picked up this download. Make sure the Celery worker and Redis are running "
    "(and that the worker was restarted after the latest update), then try again."
)


def workers_online() -> bool:
    """True if at least one Celery worker answers a ping (cached for a few seconds)."""
    if settings.CELERY_TASK_ALWAYS_EAGER:
        return True
    cached = cache.get("celery:workers_ok")
    if cached is not None:
        return cached
    try:
        from config.celery import app

        ok = bool(app.control.inspect(timeout=1.0, limit=1).ping())
    except Exception:
        logger.warning("Could not reach the Celery broker/workers", exc_info=True)
        ok = False
    cache.set("celery:workers_ok", ok, 15 if ok else 3)
    return ok
