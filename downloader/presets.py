"""Download presets.

Users only ever submit a preset *key*. The yt-dlp format selector is looked up
server-side, so no user-controlled string reaches yt-dlp/FFmpeg options.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    format: str  # mp4 | m4a | mp3 (container / audio codec of the stored file)
    quality: str  # human label stored on the model
    height: int | None  # None => not height-limited
    selector: str
    audio_only: bool = False
    audio_codec: str | None = None  # passed to FFmpegExtractAudio


def _mp4(height: int) -> Preset:
    selector = (
        f"bv*[height<={height}][vcodec^=avc1]+ba[acodec^=mp4a]"
        f"/b[height<={height}][ext=mp4]"
        f"/bv*[height<={height}]+ba"
        f"/b[height<={height}]"
        f"/b"
    )
    return Preset(f"mp4_{height}", f"MP4 {height}p", "mp4", f"{height}p", height, selector)


PRESETS: dict[str, Preset] = {
    p.key: p
    for p in (
        _mp4(360),
        _mp4(480),
        _mp4(720),
        _mp4(1080),
        Preset(
            "best",
            "Best available (MP4)",
            "mp4",
            "Best",
            None,
            "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/bv*+ba/b",
        ),
        Preset("audio_m4a", "Audio only (M4A)", "m4a", "Audio", None, "ba[ext=m4a]/ba/b", True, "m4a"),
        Preset("audio_mp3", "Audio only (MP3)", "mp3", "Audio", None, "ba/b", True, "mp3"),
    )
}

DEFAULT_PRESET = "mp4_720"
PRESET_CHOICES = [(p.key, p.label) for p in PRESETS.values()]
