from __future__ import annotations

import json
from functools import wraps

from django.http import JsonResponse

from downloader.models import UserPreference
from downloader.presets import DEFAULT_PRESET, PRESETS


def json_body(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def error(message: str, status: int = 400, **extra) -> JsonResponse:
    return JsonResponse({"ok": False, "error": message, **extra}, status=status)


def api_login_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return error("Please sign in again.", 401)
        return view(request, *args, **kwargs)

    return wrapper


def user_default_preset(user) -> str:
    if not user.is_authenticated:
        return DEFAULT_PRESET
    pref = UserPreference.objects.filter(user=user).first()
    return pref.default_preset if pref and pref.default_preset in PRESETS else DEFAULT_PRESET
