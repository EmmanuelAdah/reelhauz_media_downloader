"""Download endpoints (public: no login needed). Nothing here touches the database."""
from __future__ import annotations

import json
import logging
import time
from typing import Iterator

from django.conf import settings
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.utils.http import content_disposition_header
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from downloader.forms import VideoURLForm
from downloader.presets import DEFAULT_PRESET
from downloader.services import quick_service, ytdlp_service
from downloader.services.ytdlp_service import DownloaderError
from downloader.throttle import rate_limit
from downloader.utils import human_duration, human_size
from downloader.views.common import error, json_body, user_default_preset

logger = logging.getLogger(__name__)
CHUNK = 256 * 1024
EVENT_POLL_SECONDS = 0.4
HEARTBEAT_SECONDS = 15
MAX_STREAM_SECONDS = 4 * 3600


@never_cache
def home(request):
    return render(
        request,
        "downloader/home.html",
        {
            "max_mb": settings.MAX_DOWNLOAD_SIZE_MB,
            "max_minutes": settings.MAX_VIDEO_DURATION_SECONDS // 60,
            "initial_url": request.GET.get("url", ""),
            "default_preset": user_default_preset(request.user),
        },
    )


def metadata_payload(info, formats, *, default_preset: str, size_cap: int | None) -> dict:
    return {
        "ok": True,
        "video": {
            "video_id": info.video_id,
            "url": info.webpage_url,
            "title": info.title,
            "uploader": info.uploader,
            "duration": info.duration,
            "duration_display": human_duration(info.duration),
            "upload_date": info.upload_date.strftime("%b %d, %Y") if info.upload_date else "",
            "thumbnail_url": info.thumbnail_url if info.thumbnail_url.startswith("https://") else "",
        },
        "formats": [
            {
                "key": f.key,
                "label": f.label,
                "quality": f.quality,
                "audio_only": f.audio_only,
                "size_display": human_size(f.estimated_size) if f.estimated_size else "",
                "disabled": bool(size_cap and f.estimated_size and f.estimated_size > size_cap),
            }
            for f in formats
        ],
        "default_preset": default_preset,
    }


@require_POST
@rate_limit("meta", lambda: settings.RATE_LIMIT_METADATA)
def quick_metadata(request):
    form = VideoURLForm(json_body(request))
    if not form.is_valid():
        return error(form.errors["url"][0])
    try:
        info = ytdlp_service.extract_video_info(form.cleaned_data["url"])
        formats = ytdlp_service.get_available_formats(info)
    except DownloaderError as exc:
        return error(exc.message, 422, code=exc.code)
    cap = settings.MAX_DOWNLOAD_SIZE_MB * 1024 * 1024 or None
    response = JsonResponse(
        metadata_payload(info, formats, default_preset=user_default_preset(request.user), size_cap=cap)
    )
    response["Cache-Control"] = "no-store"
    return response


@require_POST
@rate_limit("download", lambda: settings.RATE_LIMIT_DOWNLOAD)
def quick_start(request):
    body = json_body(request)
    form = VideoURLForm(body)
    if not form.is_valid():
        return error(form.errors["url"][0])
    try:
        token, state = quick_service.start(form.cleaned_data["url"], str(body.get("preset") or ""))
    except DownloaderError as exc:
        return error(exc.message, 503 if exc.code in {"busy", "queue", "no_worker"} else 422, code=exc.code)
    response = JsonResponse({"ok": True, "video": quick_service.public_state(token, state)}, status=201)
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def quick_status(request, token: str):
    state = quick_service.get_state(token)
    if state is None:
        return error("This download has expired or doesn't exist.", 404)
    state = quick_service.check_stalled(token, state)
    response = JsonResponse({"ok": True, "video": quick_service.public_state(token, state)})
    response["Cache-Control"] = "no-store"
    return response


def _sse(data: dict | None = None, *, event: str | None = None) -> bytes:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data or {}, separators=(',', ':'))}\n\n".encode()


def _event_stream(token: str) -> Iterator[bytes]:
    """Push every change of a job's state to the browser (Server-Sent Events)."""
    started = last_beat = time.monotonic()
    last_payload = None
    yield b"retry: 2000\n\n"
    while time.monotonic() - started < MAX_STREAM_SECONDS:
        state = quick_service.get_state(token)
        if state is None:
            yield _sse(event="gone")
            return
        state = quick_service.check_stalled(token, state)
        payload = quick_service.public_state(token, state)
        if payload != last_payload:
            last_payload = payload
            last_beat = time.monotonic()
            yield _sse(payload)
            if not payload["is_active"]:
                return
        elif time.monotonic() - last_beat > HEARTBEAT_SECONDS:
            last_beat = time.monotonic()
            yield b": keep-alive\n\n"
        time.sleep(EVENT_POLL_SECONDS)


@require_GET
def quick_events(request, token: str):
    if quick_service.get_state(token) is None:
        return error("This download has expired or doesn't exist.", 404)
    response = StreamingHttpResponse(_event_stream(token), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache, no-store"
    response["X-Accel-Buffering"] = "no"  # tell nginx not to buffer the stream
    return response


def _stream_then_discard(path, token: str) -> Iterator[bytes]:
    """Stream the file; delete everything only if the whole file was delivered."""
    completed = False
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            yield chunk
        completed = True
    if completed:
        quick_service.discard(token)


@require_GET
def quick_file(request, token: str):
    found = quick_service.file_path(token)
    if found is None:
        return error("This file has expired or was already saved.", 404)
    path, state = found
    response = StreamingHttpResponse(
        _stream_then_discard(path, token), content_type=state.get("mime") or "application/octet-stream"
    )
    response["Content-Length"] = str(path.stat().st_size)
    response["Content-Disposition"] = content_disposition_header(True, state.get("filename") or path.name)
    response["Cache-Control"] = "no-store"
    return response
