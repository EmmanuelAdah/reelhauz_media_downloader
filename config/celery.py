import os

import logging

from celery import Celery
from celery.signals import worker_ready

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@worker_ready.connect
def _warn_if_cache_not_shared(**kwargs):
    from django.conf import settings

    if "locmem" in settings.CACHES["default"]["BACKEND"].lower() and not settings.CELERY_TASK_ALWAYS_EAGER:
        logging.getLogger(__name__).warning(
            "Worker is using the in-memory cache; the web server cannot see its progress. Set REDIS_URL."
        )
