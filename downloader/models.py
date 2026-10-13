from __future__ import annotations

from django.conf import settings
from django.db import models

from .presets import DEFAULT_PRESET, PRESET_CHOICES
from .utils import human_duration


def video_upload_path(instance, filename):  # pragma: no cover
    """Kept only so migration 0001 (the old server-side library) can still be imported."""
    raise NotImplementedError


def thumbnail_upload_path(instance, filename):  # pragma: no cover
    """Kept only so migration 0001 (the old server-side library) can still be imported."""
    raise NotImplementedError


class PlaybackPosition(models.Model):
    """The only per-user media record: where you stopped watching.

    Videos themselves are never stored on the server. ``media_key`` is the YouTube
    video id when it can be read from the file name (``Title [id].mp4``), otherwise a
    fingerprint of the local file name and size.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="playback_positions")
    media_key = models.CharField(max_length=32)
    title = models.CharField(max_length=300, blank=True)
    position = models.FloatField(default=0.0, help_text="Seconds")
    duration = models.FloatField(default=0.0, help_text="Seconds")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [models.UniqueConstraint(fields=["user", "media_key"], name="unique_position_per_user_media")]
        indexes = [models.Index(fields=["user", "-updated_at"], name="pp_user_updated_idx")]

    def __str__(self) -> str:
        return f"{self.user} @ {self.position:.0f}s of {self.title or self.media_key}"

    @property
    def percent(self) -> int:
        return int(min(100, (self.position / self.duration) * 100)) if self.duration else 0

    @property
    def is_finished(self) -> bool:
        return bool(self.duration) and self.position >= self.duration * 0.95

    @property
    def is_youtube_id(self) -> bool:
        return len(self.media_key) == 11 and not self.media_key.startswith("local-")

    def as_dict(self) -> dict:
        return {
            "key": self.media_key,
            "title": self.title or self.media_key,
            "position": round(self.position, 1),
            "duration": round(self.duration, 1),
            "position_display": human_duration(self.position),
            "duration_display": human_duration(self.duration),
            "percent": self.percent,
            "finished": self.is_finished,
            "thumbnail_url": f"https://i.ytimg.com/vi/{self.media_key}/hqdefault.jpg" if self.is_youtube_id else "",
            "updated_at": self.updated_at.isoformat(),
        }


class UserPreference(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="preference")
    default_preset = models.CharField(max_length=32, choices=PRESET_CHOICES, default=DEFAULT_PRESET)

    def __str__(self) -> str:
        return f"Preferences for {self.user}"
