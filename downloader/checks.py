from django.conf import settings
from django.core.checks import Warning, register


@register()
def shared_cache_check(app_configs, **kwargs):
    """Download progress is shared between web and worker through the cache."""
    if settings.CELERY_TASK_ALWAYS_EAGER:
        return []
    if "locmem" in settings.CACHES["default"]["BACKEND"].lower():
        return [
            Warning(
                "The in-memory cache is in use, so the web server and the Celery worker cannot share "
                "download status and anonymous downloads will stay on 'Pending'.",
                hint="Set REDIS_URL (the same value for the web process and the worker).",
                id="downloader.W001",
            )
        ]
    return []
