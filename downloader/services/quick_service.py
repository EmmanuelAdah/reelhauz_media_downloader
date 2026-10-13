"""Download jobs. No database rows, no stored videos.

A job's state lives only in the cache (Redis in production) under an unguessable
token. The finished file lives in a per-token scratch directory until the browser
has fully received it (it is then deleted), or ``TRANSFER_TTL_SECONDS`` passes.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

from downloader.presets import PRESETS
from downloader.services import ytdlp_service
from downloader.services.ytdlp_service import DownloaderError, ProgressUpdate
from downloader.utils import human_eta, human_size, human_speed

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")  # secrets.token_urlsafe(32)
ACTIVE = {"pending", "fetching", "downloading", "processing"}
STATUS_LABELS = {
    "pending": "Pending",
    "fetching": "Fetching",
    "downloading": "Downloading",
    "processing": "Processing",
    "completed": "Completed",
    "failed": "Failed",
}
MIME = {"mp4": "video/mp4", "m4a": "audio/mp4", "mp3": "audio/mpeg"}
SLOT_KEY = "transfer:slots"


def root_dir() -> Path:
    return Path(settings.DOWNLOAD_TMP_DIR) / "transfers"


def _key(token: str) -> str:
    return f"transfer:{token}"


def _put(token: str, state: dict[str, Any]) -> None:
    cache.set(_key(token), state, settings.TRANSFER_TTL_SECONDS)


def get_state(token: str) -> dict[str, Any] | None:
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        return None
    return cache.get(_key(token))


def public_state(token: str, state: dict[str, Any]) -> dict[str, Any]:
    """JSON for the browser. Never includes filesystem paths."""
    status = state["status"]
    file_url = reverse("quick_file", args=[token]) if status == "completed" else ""
    waiting = status == "pending" and time.time() - state.get("created", time.time()) > 8
    return {
        "id": token,
        "title": state.get("title") or "Preparing download…",
        "status": status,
        "status_label": STATUS_LABELS.get(status, status.title()),
        "is_active": status in ACTIVE,
        "is_completed": status == "completed",
        "percent": round(state.get("percent", 0.0), 1),
        "downloaded_bytes": state.get("downloaded_bytes", 0),
        "total_bytes": state.get("total_bytes", 0),
        "downloaded_display": human_size(state.get("downloaded_bytes", 0)),
        "total_display": human_size(state.get("total_bytes", 0)),
        "speed_display": human_speed(state.get("speed", 0.0)),
        "eta_display": human_eta(state.get("eta")),
        "error_message": state.get("error", ""),
        "notice": "Waiting for a free download worker…" if waiting else "",
        "file_size_display": human_size(state.get("file_size", 0)),
        "file_size": state.get("file_size", 0),
        "filename": state.get("filename", "") if status == "completed" else "",
        "video_id": state.get("video_id", ""),
        "file_url": file_url,
    }


# --------------------------------------------------------------------------- #
# Capacity and worker health
# --------------------------------------------------------------------------- #
def _acquire_slot() -> bool:
    cache.add(SLOT_KEY, 0, timeout=2 * 3600)
    try:
        count = cache.incr(SLOT_KEY)
    except ValueError:
        cache.set(SLOT_KEY, 1, timeout=2 * 3600)
        return True
    if count > settings.MAX_CONCURRENT_DOWNLOADS:
        _release_slot()
        return False
    return True


def _release_slot() -> None:
    try:
        if cache.decr(SLOT_KEY) < 0:
            cache.set(SLOT_KEY, 0, timeout=2 * 3600)
    except ValueError:
        pass


def worker_available() -> bool:
    """True if a Celery worker answers. Skipped in eager mode and when disabled."""
    if settings.CELERY_TASK_ALWAYS_EAGER or not settings.CHECK_WORKER_BEFORE_START:
        return True
    cached = cache.get("transfer:worker_ok")
    if cached is not None:
        return cached
    try:
        from config.celery import app

        ok = bool(app.control.ping(timeout=1.0, limit=1))
    except Exception:
        logger.exception("Could not reach the Celery broker")
        ok = False
    cache.set("transfer:worker_ok", ok, timeout=30 if ok else 3)
    if not ok:
        logger.error("No Celery worker is running. Start one with: uv run celery -A config worker --loglevel=info")
    return ok


# --------------------------------------------------------------------------- #
# Start
# --------------------------------------------------------------------------- #
def start(url: str, preset_key: str) -> tuple[str, dict[str, Any]]:
    check = ytdlp_service.validate_url(url)
    if not check.valid:
        raise DownloaderError("invalid_url", check.error)
    if preset_key not in PRESETS:
        raise DownloaderError("invalid_format", "That download format isn't available.")
    if not worker_available():
        hint = " Start it with: uv run celery -A config worker --loglevel=info" if settings.DEBUG else ""
        raise DownloaderError("no_worker", "Downloads are unavailable because the download worker isn't running." + hint)

    sweep()
    if not _acquire_slot():
        raise DownloaderError("busy", "The server is busy with other downloads. Try again in a minute.")

    token = secrets.token_urlsafe(32)
    state: dict[str, Any] = {"status": "pending", "preset": preset_key, "title": "", "created": time.time()}
    _put(token, state)
    try:
        from downloader.tasks import quick_download_task

        quick_download_task.delay(token, check.normalized_url, preset_key)
    except Exception:
        logger.exception("Could not queue download")
        _release_slot()
        discard(token)
        raise DownloaderError("queue", "The download queue is unavailable. Please try again later.")
    return token, state


def check_stalled(token: str, state: dict[str, Any]) -> dict[str, Any]:
    """Fail jobs that no worker picked up, so the UI never waits forever."""
    if state.get("status") == "pending" and time.time() - state.get("created", time.time()) > settings.QUEUE_TIMEOUT_SECONDS:
        logger.error("Download %s was never picked up by a worker.", token[:6])
        state.update(
            status="failed",
            error="No download worker picked this up. Try again in a moment.",
            slot_released=True,
        )
        _put(token, state)
        _release_slot()
    return state


# --------------------------------------------------------------------------- #
# Run (inside the Celery worker)
# --------------------------------------------------------------------------- #
class _Reporter:
    def __init__(self, token: str, state: dict[str, Any], min_interval: float = 0.4) -> None:
        self.token, self.state, self.min_interval = token, state, min_interval
        self._last = 0.0

    def __call__(self, update: ProgressUpdate) -> None:
        now = time.monotonic()
        phase_changed = (update.phase == "processing") != (self.state["status"] == "processing")
        if not phase_changed and now - self._last < self.min_interval:
            return
        self._last = now
        self.state.update(
            status="processing" if update.phase == "processing" else "downloading",
            percent=update.percent,
            downloaded_bytes=update.downloaded_bytes,
            total_bytes=update.total_bytes,
            speed=update.speed,
            eta=update.eta,
        )
        _put(self.token, self.state)


def safe_filename(title: str, video_id: str, ext: str) -> str:
    """``Title [VIDEOID].ext``. The id lets the player recognise the file later."""
    stem = re.sub(r"[^\w\- ]+", "", title, flags=re.UNICODE).strip()[:80].strip() or "video"
    return f"{stem} [{video_id}].{ext}"


def run(token: str, url: str, preset_key: str) -> None:
    state = get_state(token)
    slot_already_released = bool(state and state.get("slot_released"))
    try:
        if not state or state.get("status") != "pending":
            return
        workdir = root_dir() / token
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            state["status"] = "fetching"
            _put(token, state)
            info = ytdlp_service.extract_video_info(url)
            state.update(title=info.title, video_id=info.video_id, duration=info.duration)
            state["status"] = "fetching"
            _put(token, state)

            state.update(status="downloading", percent=0.0)
            _put(token, state)
            cap = settings.MAX_DOWNLOAD_SIZE_MB * 1024 * 1024 or None
            result = ytdlp_service.download_video(
                url,
                preset_key,
                workdir,
                filename_stem=uuid.uuid4().hex,
                progress_callback=_Reporter(token, state),
                max_filesize=cap,
            )
            state.update(
                status="completed",
                percent=100.0,
                speed=0.0,
                eta=0,
                file=result.filepath.name,
                file_size=result.filesize,
                downloaded_bytes=result.filesize,
                total_bytes=result.filesize,
                filename=safe_filename(info.title, info.video_id, result.ext),
                mime=MIME.get(result.ext, "application/octet-stream"),
            )
            _put(token, state)
        except DownloaderError as exc:
            logger.warning("Download failed: [%s] %s %s", exc.code, exc.message, f"(hint: {exc.hint})" if exc.hint else "")
            _fail(token, state, workdir, exc.message)
        except Exception:
            logger.exception("Unexpected download failure")
            _fail(token, state, workdir, "Something unexpected went wrong. Please try again.")
    finally:
        if not slot_already_released:
            _release_slot()


def _fail(token: str, state: dict[str, Any], workdir: Path, message: str) -> None:
    shutil.rmtree(workdir, ignore_errors=True)
    state.update(status="failed", error=message, speed=0.0, eta=None)
    _put(token, state)


# --------------------------------------------------------------------------- #
# Delivery and cleanup
# --------------------------------------------------------------------------- #
def file_path(token: str) -> tuple[Path, dict[str, Any]] | None:
    state = get_state(token)
    if not state or state.get("status") != "completed" or not state.get("file"):
        return None
    workdir = (root_dir() / token).resolve()
    path = (workdir / state["file"]).resolve()
    if path.parent != workdir or not path.is_file():
        return None
    return path, state


def discard(token: str) -> None:
    """Remove the scratch directory and the cached state."""
    if isinstance(token, str) and TOKEN_RE.match(token):
        shutil.rmtree(root_dir() / token, ignore_errors=True)
        cache.delete(_key(token))


def _newest_mtime(path: Path) -> float:
    newest = path.stat().st_mtime
    with os.scandir(path) as it:
        for entry in it:
            try:
                newest = max(newest, entry.stat().st_mtime)
            except OSError:
                pass
    return newest


def sweep(force: bool = False) -> int:
    """Delete scratch directories untouched for TRANSFER_TTL_SECONDS."""
    if not force and not cache.add("transfer:sweep", 1, timeout=300):
        return 0
    root = root_dir()
    if not root.is_dir():
        return 0
    removed = 0
    cutoff = time.time() - settings.TRANSFER_TTL_SECONDS
    for child in root.iterdir():
        try:
            if child.is_dir() and _newest_mtime(child) < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                cache.delete(_key(child.name))
                removed += 1
        except OSError:
            continue
    return removed
