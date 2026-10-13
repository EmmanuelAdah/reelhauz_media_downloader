"""Playback positions: the only thing stored per user. Videos never touch the server."""
from __future__ import annotations

import math
import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from downloader.forms import PreferenceForm
from downloader.models import PlaybackPosition, UserPreference
from downloader.throttle import rate_limit
from downloader.views.common import api_login_required, error, json_body

KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def watch(request):
    return render(request, "downloader/watch.html", {"authenticated": request.user.is_authenticated})


def continue_watching(request):
    return render(request, "downloader/continue_watching.html", {"authenticated": request.user.is_authenticated})


@login_required
def settings_page(request):
    pref, _ = UserPreference.objects.get_or_create(user=request.user)
    form = PreferenceForm(request.POST or None, instance=pref)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Settings saved.")
        return redirect("settings")
    return render(
        request,
        "downloader/settings.html",
        {"form": form, "saved_count": PlaybackPosition.objects.filter(user=request.user).count()},
    )


@api_login_required
@require_http_methods(["GET", "DELETE"])
def playback_list(request):
    if request.method == "DELETE":
        deleted, _ = PlaybackPosition.objects.filter(user=request.user).delete()
        return JsonResponse({"ok": True, "deleted": deleted})
    items = [p.as_dict() for p in PlaybackPosition.objects.filter(user=request.user)[:100]]
    return JsonResponse({"ok": True, "results": items})


@api_login_required
@require_http_methods(["GET", "POST", "DELETE"])
@rate_limit("playback", lambda: settings.RATE_LIMIT_PLAYBACK)
def playback_detail(request, key: str):
    if not KEY_RE.match(key):
        return error("Invalid media key.", 400)
    entry = PlaybackPosition.objects.filter(user=request.user, media_key=key).first()

    if request.method == "GET":
        return JsonResponse({"ok": True, "item": entry.as_dict() if entry else None})

    if request.method == "DELETE":
        if entry:
            entry.delete()
        return JsonResponse({"ok": True})

    body = json_body(request)
    try:
        position = float(body.get("position"))
        duration = float(body.get("duration") or 0)
    except (TypeError, ValueError):
        return error("position and duration must be numbers.")
    if not all(math.isfinite(x) for x in (position, duration)) or position < 0 or duration < 0:
        return error("position and duration must be non-negative numbers.")
    if duration:
        position = min(position, duration)
    title = str(body.get("title") or "")[:300]

    entry, _ = PlaybackPosition.objects.update_or_create(
        user=request.user,
        media_key=key,
        defaults={"position": position, "duration": duration, **({"title": title} if title else {})},
    )
    return JsonResponse({"ok": True, "item": entry.as_dict()})
