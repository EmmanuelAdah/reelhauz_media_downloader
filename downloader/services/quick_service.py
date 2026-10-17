"""Download jobs. No database rows, no stored videos.

A job's state lives only in the cache (Redis in production) under an unguessable
token. The finished file lives in a per-token scratch directory until the browser
has fully received it (it is then deleted), or ``TRANSFER_TTL_SECONDS`` passes.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

from downloader.presets import PRESETS
from downloader.services import cookies as cookie_service
from downloader.services import ytdlp_service
from downloader.services.ytdlp_service import ALLOWED_BROWSERS, DownloadCancelled, DownloaderError, ProgressUpdate, YtAuth
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
    "cancelled": "Cancelled",
}
MIME = {"mp4": "video/mp4", "m4a": "audio/mp4", "mp3": "audio/mpeg"}
SLOT_KEY = "transfer:slots"
META_TTL_SECONDS = 900


def _use_celery() -> bool:
    return settings.DOWNLOAD_BACKEND == "celery"


def root_dir() -> Path:
    return Path(settings.DOWNLOAD_TMP_DIR) / "transfers"


def _key(token: str) -> str:
    return f"transfer:{token}"


def _cancel_key(token: str) -> str:
    return f"transfer:{token}:cancel"


def _cache_get(key: str, default: Any = None) -> Any:
    try:
        return cache.get(key, default)
    except Exception:  # a cache hiccup must not break a download
        logger.warning("Cache read failed for %s", key.split(":")[0], exc_info=True)
        return default


def _cache_set(key: str, value: Any, ttl: int) -> None:
    try:
        cache.set(key, value, ttl)
    except Exception:
        logger.warning("Cache write failed for %s", key.split(":")[0], exc_info=True)


def cache_summary(info) -> None:
    """Remember the preview's title/duration so the job needn't look the video up again."""
    _cache_set(f"meta:{info.video_id}", {"title": info.title, "duration": info.duration}, META_TTL_SECONDS)


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
        "title": state.get("title") or "",
        "cancelling": status in ACTIVE and bool(_cache_get(_cancel_key(token))),
        "failed_stage": state.get("failed_stage", ""),
        "error_code": state.get("error_code", ""),
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
# Visitor-chosen access (browser login / cookies.txt): used for ONE request, never stored
# --------------------------------------------------------------------------- #
COOKIES_NAME = "cookies.txt"


def normalise_auth(auth: Any) -> dict[str, str]:
    """Validate the visitor's choice. Returns {"mode": "auto"} or {"mode": "browser", "browser": ...}
    or {"mode": "cookies", "cookies_text": <sanitised text>}."""
    if not auth or not isinstance(auth, dict) or auth.get("mode") in (None, "", "auto"):
        return {"mode": "auto"}
    mode = auth.get("mode")
    if mode == "browser":
        browser = str(auth.get("browser", "")).strip().lower()
        if browser not in ALLOWED_BROWSERS:
            raise DownloaderError("bad_auth", "Choose one of the supported browsers.")
        return {"mode": "browser", "browser": browser}
    if mode == "cookies":
        text, _count = cookie_service.sanitize_cookies(auth.get("cookies"))
        return {"mode": "cookies", "cookies_text": text}
    raise DownloaderError("bad_auth", "That access option isn't available.")


def _write_private_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # readable by this user only
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


@contextlib.contextmanager
def temporary_auth(auth: dict[str, str]):
    """Yield a YtAuth for a single request (e.g. the preview) and delete any cookies file afterwards."""
    if auth["mode"] == "browser":
        yield YtAuth(browser=auth["browser"])
    elif auth["mode"] == "cookies":
        folder = Path(tempfile.mkdtemp(prefix="preview_", dir=_private_tmp_root()))
        path = folder / COOKIES_NAME
        try:
            _write_private_file(path, auth["cookies_text"])
            yield YtAuth(cookiefile=path)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    else:
        yield None


def _private_tmp_root() -> Path:
    root = Path(settings.DOWNLOAD_TMP_DIR) / "previews"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _auth_for_job(token: str, state: dict[str, Any], workdir: Path) -> YtAuth | None:
    chosen = state.get("auth") or {}
    if chosen.get("mode") == "browser" and chosen.get("browser") in ALLOWED_BROWSERS:
        return YtAuth(browser=chosen["browser"])
    if chosen.get("mode") == "cookies":
        path = workdir / COOKIES_NAME
        if not path.is_file():
            raise DownloaderError("bad_cookies", "The cookies you provided are no longer available. Please choose the file again.")
        return YtAuth(cookiefile=path)
    return None


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
    if not _use_celery() or settings.CELERY_TASK_ALWAYS_EAGER or not settings.CHECK_WORKER_BEFORE_START:
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
def start(url: str, preset_key: str, auth: Any = None) -> tuple[str, dict[str, Any]]:
    check = ytdlp_service.validate_url(url)
    if not check.valid:
        raise DownloaderError("invalid_url", check.error)
    if preset_key not in PRESETS:
        raise DownloaderError("invalid_format", "That download format isn't available.")
    chosen = normalise_auth(auth)  # validate before taking a slot
    if not worker_available():
        hint = " Start it with: uv run celery -A config worker --loglevel=info" if settings.DEBUG else ""
        raise DownloaderError("no_worker", "Downloads are unavailable because the download worker isn't running." + hint)

    try:
        sweep()
        have_slot = _acquire_slot()
    except Exception as exc:  # e.g. REDIS_URL is set but Redis isn't running
        logger.error("The job cache is unreachable (%s). Start Redis, or unset REDIS_URL to use in-process memory.", exc)
        raise DownloaderError(
            "queue",
            "Downloads are temporarily unavailable. Please try again later.",
            "The cache backend (REDIS_URL) is unreachable: start Redis or remove REDIS_URL from .env.",
        )
    if not have_slot:
        raise DownloaderError("busy", "The server is busy with other downloads. Try again in a minute.")

    token = secrets.token_urlsafe(32)
    meta = _cache_get(f"meta:{check.video_id}") or {}
    state: dict[str, Any] = {
        "status": "pending",
        "preset": preset_key,
        "title": meta.get("title", ""),
        "duration": meta.get("duration", 0),
        "video_id": check.video_id,
        "created": time.time(),
        # only the *kind* of access is kept in the cache; cookies go to a private file, never to Redis
        "auth": {k: v for k, v in chosen.items() if k in {"mode", "browser"}},
    }
    _put(token, state)
    try:
        if chosen["mode"] == "cookies":
            _write_private_file(root_dir() / token / COOKIES_NAME, chosen["cookies_text"])
        _launch(token, check.normalized_url, preset_key)
    except Exception:
        logger.exception("Could not start download")
        _release_slot()
        discard(token)
        raise DownloaderError("queue", "The download queue is unavailable. Please try again later.")
    return token, state


def _launch(token: str, url: str, preset_key: str) -> None:
    """Run the job in a background thread of this process (default) or hand it to Celery."""
    if _use_celery():
        from downloader.tasks import quick_download_task

        quick_download_task.delay(token, url, preset_key)
        return
    threading.Thread(target=_thread_main, args=(token, url, preset_key), name=f"download-{token[:6]}", daemon=True).start()


def _thread_main(token: str, url: str, preset_key: str) -> None:
    try:
        run(token, url, preset_key)
    except Exception:  # run() handles its own errors; this is the last line of defence
        logger.exception("Download thread crashed")


def check_stalled(token: str, state: dict[str, Any]) -> dict[str, Any]:
    """Fail jobs that no worker picked up, so the UI never waits forever."""
    if state.get("status") == "pending" and time.time() - state.get("created", time.time()) > settings.QUEUE_TIMEOUT_SECONDS:
        logger.error("Download %s was never picked up by a worker.", token[:6])
        state.update(
            status="failed",
            failed_stage="pending",
            error_code="no_worker",
            error="No download worker picked this up. Try again in a moment.",
            slot_released=True,
        )
        _put(token, state)
        _release_slot()
    return state


# --------------------------------------------------------------------------- #
# Run (inside the Celery worker)
# --------------------------------------------------------------------------- #
def _raise_if_cancelled(token: str) -> None:
    if _cache_get(_cancel_key(token)):
        raise DownloadCancelled()


class _Reporter:
    """Writes throttled progress to the cache and aborts the download when the user cancels."""

    def __init__(self, token: str, state: dict[str, Any], min_interval: float = 0.4) -> None:
        self.token, self.state, self.min_interval = token, state, min_interval
        self._last: float | None = None

    def __call__(self, update: ProgressUpdate) -> None:
        _raise_if_cancelled(self.token)
        now = time.monotonic()
        phase_changed = (update.phase == "processing") != (self.state["status"] == "processing")
        first = self._last is None or self.state["status"] == "fetching"  # leave "Fetching" at the first tick
        if not (first or phase_changed) and now - self._last < self.min_interval:
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
    workdir = root_dir() / token
    try:
        if not state or state.get("status") != "pending":
            return
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            state["status"] = "fetching"
            _put(token, state)
            _raise_if_cancelled(token)
            check = ytdlp_service.validate_url(url)
            if not check.valid:
                raise DownloaderError("invalid_url", check.error)
            auth = _auth_for_job(token, state, workdir)
            if not state.get("title"):  # no preview ran first (e.g. direct API call): look the video up now
                info = ytdlp_service.extract_video_info(url, auth=auth)
                state.update(title=info.title, duration=info.duration, video_id=info.video_id)
                _put(token, state)
                _raise_if_cancelled(token)

            cap = settings.MAX_DOWNLOAD_SIZE_MB * 1024 * 1024 or None
            result = ytdlp_service.download_video(
                url,
                preset_key,
                workdir,
                filename_stem=uuid.uuid4().hex,
                progress_callback=_Reporter(token, state),
                max_filesize=cap,
                auth=auth,
            )
            _raise_if_cancelled(token)  # cancelled while FFmpeg was finishing
            state.update(
                status="completed",
                percent=100.0,
                speed=0.0,
                eta=0,
                file=result.filepath.name,
                file_size=result.filesize,
                downloaded_bytes=result.filesize,
                total_bytes=result.filesize,
                filename=safe_filename(state.get("title", ""), state.get("video_id") or check.video_id, result.ext),
                mime=MIME.get(result.ext, "application/octet-stream"),
            )
            _put(token, state)
        except DownloadCancelled:
            logger.info("Download %s cancelled by the user", token[:6])
            _cancelled(token, state, workdir)
        except DownloaderError as exc:
            logger.warning("Download failed: [%s] %s %s", exc.code, exc.message, f"(hint: {exc.hint})" if exc.hint else "")
            _fail(token, state, workdir, exc.message, exc.code)
        except Exception:
            logger.exception("Unexpected download failure")
            _fail(token, state, workdir, "Something unexpected went wrong. Please try again.", "unknown")
    finally:
        if TOKEN_RE.match(token):
            (workdir / COOKIES_NAME).unlink(missing_ok=True)  # the visitor's cookies never outlive the job
        if not slot_already_released:
            _release_slot()


def _fail(token: str, state: dict[str, Any], workdir: Path, message: str, code: str = "") -> None:
    shutil.rmtree(workdir, ignore_errors=True)
    stage = state.get("status") if state.get("status") in ACTIVE else ""
    state.update(status="failed", failed_stage=stage, error=message, error_code=code, speed=0.0, eta=None)
    _put(token, state)


def _cancelled(token: str, state: dict[str, Any], workdir: Path) -> None:
    shutil.rmtree(workdir, ignore_errors=True)  # includes yt-dlp's partial .part files
    state.update(status="cancelled", speed=0.0, eta=None)
    _put(token, state)


def request_cancel(token: str) -> dict[str, Any] | None:
    """Cancel a running download, or discard a finished/failed one. None if the token is unknown."""
    state = get_state(token)
    if state is None:
        return None
    status = state["status"]
    if status in ACTIVE:
        _cache_set(_cancel_key(token), 1, settings.TRANSFER_TTL_SECONDS)
        if status == "pending":  # nothing is running yet: cancel on the spot
            state.update(status="cancelled", slot_released=True)
            _put(token, state)
            _release_slot()
            shutil.rmtree(root_dir() / token, ignore_errors=True)
        return {"video": public_state(token, state), "discarded": False}
    discard(token)  # completed but not yet saved, failed or cancelled: forget it and delete any file
    return {"video": None, "discarded": True}


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
        cache.delete(_cancel_key(token))


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
