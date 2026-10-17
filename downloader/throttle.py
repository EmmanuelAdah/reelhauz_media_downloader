"""Tiny fixed-window rate limiter backed by Django's cache (Redis in production)."""
from __future__ import annotations

import logging
from functools import wraps
from typing import Callable

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse

logger = logging.getLogger(__name__)


def allow(bucket: str, ident: str | int, limit: int, window: int) -> bool:
    key = f"rl:{bucket}:{ident}"
    try:
        if cache.add(key, 1, timeout=window):
            return True
        try:
            return cache.incr(key) <= limit
        except ValueError:  # key expired between add() and incr()
            cache.set(key, 1, timeout=window)
            return True
    except Exception:
        # Cache unreachable: fail open here. The download code reports the outage with a clear message.
        logger.warning("Rate limiter cache unavailable; not limiting this request", exc_info=True)
        return True


def client_ip(request) -> str:
    """Client IP. Only trusts X-Forwarded-For when TRUSTED_PROXY_COUNT > 0."""
    count = getattr(settings, "TRUSTED_PROXY_COUNT", 0)
    if count > 0:
        parts = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
        if len(parts) >= count:
            return parts[-count]
    return request.META.get("REMOTE_ADDR", "unknown")


def client_ident(request) -> str:
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return f"u{user.pk}"
    return f"ip{client_ip(request)}"


def rate_limit(bucket: str, setting_getter: Callable[[], tuple[int, int]]):
    """Decorator for JSON API views. ``setting_getter`` returns (limit, window_seconds)."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            limit, window = setting_getter()
            if not allow(bucket, client_ident(request), limit, window):
                response = JsonResponse(
                    {"ok": False, "error": "You're doing that too quickly. Wait a moment and try again."},
                    status=429,
                )
                response["Retry-After"] = str(window)
                return response
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
