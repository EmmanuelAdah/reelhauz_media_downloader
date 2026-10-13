"""Small, dependency-free formatting helpers."""
from __future__ import annotations


def human_size(num_bytes: float | int | None) -> str:
    if not num_bytes or num_bytes < 0:
        return "—"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def human_duration(seconds: float | int | None) -> str:
    if not seconds or seconds < 0:
        return "0:00"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def human_speed(bytes_per_second: float | None) -> str:
    return f"{human_size(bytes_per_second)}/s" if bytes_per_second else "—"


def human_eta(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {secs:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"
