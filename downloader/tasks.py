from __future__ import annotations

from celery import shared_task


@shared_task(name="downloader.quick_download_task", ignore_result=True)
def quick_download_task(token: str, url: str, preset_key: str) -> None:
    """Download one video into a temporary folder; state is kept in the cache only."""
    from downloader.services import quick_service

    quick_service.run(token, url, preset_key)
