"""Thin, defensive wrapper around the yt-dlp Python API.

* Nothing here shells out; yt-dlp is driven through ``yt_dlp.YoutubeDL``.
* Users never supply format strings: they pick a preset key (see ``presets.py``).
* Every failure is converted into a ``DownloaderError`` carrying a stable
  ``code`` and a human-readable message. Raw exceptions never reach the UI.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.core.cache import cache

from downloader.presets import PRESETS, Preset

logger = logging.getLogger(__name__)

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
class DownloaderError(Exception):
    """A user-presentable failure."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint  # operator-facing advice: logged, never shown to visitors (except in DEBUG)


ALLOWED_BROWSERS = ("firefox", "chrome", "chromium", "edge", "brave", "opera", "vivaldi", "safari")


@dataclass(frozen=True)
class YtAuth:
    """Credentials a visitor chose for ONE request: a private cookies file or a local browser to read."""

    cookiefile: Path | None = None
    browser: str | None = None


class DownloadCancelled(Exception):
    """Raised from a progress callback when the user deleted the job."""


@dataclass(frozen=True)
class UrlValidation:
    valid: bool
    video_id: str = ""
    normalized_url: str = ""
    error: str = ""


@dataclass
class VideoInfo:
    video_id: str
    title: str
    description: str
    uploader: str
    duration: int
    upload_date: date | None
    thumbnail_url: str
    webpage_url: str
    formats: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class FormatOption:
    key: str
    label: str
    format: str
    quality: str
    audio_only: bool
    estimated_size: int | None


@dataclass(frozen=True)
class ProgressUpdate:
    phase: str  # "downloading" | "processing"
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0
    eta: int | None = None

    @property
    def percent(self) -> float:
        if self.phase == "processing":
            return 100.0
        return min(99.9, self.downloaded_bytes / self.total_bytes * 100) if self.total_bytes else 0.0


@dataclass
class DownloadResult:
    filepath: Path
    ext: str
    filesize: int


ProgressCallback = Callable[[ProgressUpdate], None]


# --------------------------------------------------------------------------- #
# URL validation
# --------------------------------------------------------------------------- #
def validate_url(url: str) -> UrlValidation:
    """Accept only single-video YouTube URLs and return a canonical form."""
    url = (url or "").strip()
    if not url:
        return UrlValidation(False, error="Paste a YouTube video link to continue.")
    if len(url) > 2048:
        return UrlValidation(False, error="That link is too long.")
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.hostname or "").lower()
    except ValueError:
        return UrlValidation(False, error="That doesn't look like a valid link.")
    if parsed.scheme not in {"http", "https"} or not host:
        return UrlValidation(False, error="That doesn't look like a valid web link.")
    if host not in YOUTUBE_HOSTS:
        return UrlValidation(False, error="Only YouTube links are supported.")

    video_id = ""
    segments = [s for s in parsed.path.split("/") if s]
    if host.endswith("youtu.be"):
        video_id = segments[0] if segments else ""
    elif segments and segments[0] == "watch":
        video_id = (parse_qs(parsed.query).get("v") or [""])[0]
    elif len(segments) >= 2 and segments[0] in {"shorts", "embed", "live", "v"}:
        video_id = segments[1]

    if not VIDEO_ID_RE.match(video_id):
        return UrlValidation(
            False,
            error="Paste a link to a single YouTube video. Playlists, channels and search pages aren't supported.",
        )
    return UrlValidation(True, video_id=video_id, normalized_url=f"https://www.youtube.com/watch?v={video_id}")


# --------------------------------------------------------------------------- #
# Error translation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FfmpegInfo:
    path: str
    source: str  # "FFMPEG_LOCATION" | "system" | "bundled"


def find_ffmpeg() -> FfmpegInfo | None:
    """Locate FFmpeg: the FFMPEG_LOCATION setting, then the system PATH, then the copy bundled by `uv sync`."""
    configured = (getattr(settings, "FFMPEG_LOCATION", "") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        exe = path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg") if path.is_dir() else path
        if exe.is_file():
            return FfmpegInfo(str(path), "FFMPEG_LOCATION")
        logger.warning("FFMPEG_LOCATION=%s doesn't point to an FFmpeg binary; looking elsewhere", configured)
    system = shutil.which("ffmpeg")
    if system:
        return FfmpegInfo(system, "system")
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).is_file():
            return FfmpegInfo(bundled, "bundled")
    except Exception:  # package missing or no binary for this platform
        logger.debug("No bundled FFmpeg available", exc_info=True)
    return None


def ffmpeg_available() -> bool:
    return find_ffmpeg() is not None


def _translate_exception(exc: BaseException) -> DownloaderError:
    if isinstance(exc, DownloaderError):
        return exc
    raw = str(exc)
    text = re.sub(r"\x1b\[[0-9;]*m", "", raw).lower()  # strip ANSI colours
    cookie_hint = "Run `uv run python manage.py check_ytdlp <url>` and see README > Troubleshooting (JS runtime, yt-dlp update, different IP/proxy, cookies)."
    rules: list[tuple[tuple[str, ...], str, str, str]] = [
        (("private video",), "private", "This video is private and can't be downloaded.", ""),
        (
            ("confirm your age", "age-restricted", "age restricted", "inappropriate for some users"),
            "age_restricted",
            "This video is age-restricted and can't be downloaded.",
            "Age-gated videos need a cookies file from an adult account (YTDLP_COOKIES_FILE).",
        ),
        (
            ("sign in to confirm you", "not a bot"),
            "bot_check",
            "YouTube is blocking downloads from this server right now. Please try again later.",
            "YouTube bot check on this server's IP. " + cookie_hint,
        ),
        (
            ("page needs to be reloaded", "video player configuration error"),
            "client_rejected",
            "YouTube refused this request. Please try again in a moment.",
            "YouTube rejected this client set (\"The page needs to be reloaded\"). The app tries other clients automatically.",
        ),
        (("members-only", "members only", "join this channel"), "members_only", "This video is only available to channel members.", ""),
        (
            ("video unavailable", "this video is not available", "has been removed", "account associated with this video has been terminated", "no longer available"),
            "unavailable",
            "This video is unavailable. It may have been removed or restricted in your region.",
            "",
        ),
        (("live event will begin", "premieres in", "is live"), "live", "Live streams and upcoming premieres can't be downloaded yet.", ""),
        (("requested format is not available", "requested format not available"), "format_unavailable", "That quality isn't available for this video. Pick a different format.",
         "Often means the JavaScript challenge solver is missing: install yt-dlp[default] and a JS runtime (deno or node >= 20). " + cookie_hint),
        (("ffmpeg", "ffprobe", "postprocessing"), "ffmpeg", "Media processing failed. Please try again.",
         "FFmpeg failed while processing the media. Run `uv run python manage.py check_ytdlp` to see which FFmpeg is in use."),
        (
            ("cookies database", "cookie database", "failed to decrypt", "keyring"),
            "cookies",
            "The server couldn't read its saved browser cookies.",
            "YTDLP_COOKIES_FROM_BROWSER is set but the browser's cookies couldn't be read: close that browser, check the profile name, or use YTDLP_COOKIES_FILE instead.",
        ),
        (("http error 429", "too many requests"), "rate_limited", "YouTube is rate limiting this server. Wait a few minutes and try again.", cookie_hint),
        (
            ("unable to download", "urlopen error", "timed out", "connection", "network is unreachable", "temporary failure", "name or service not known", "http error 5"),
            "network",
            "Couldn't reach YouTube. Check the server's network connection and try again.",
            "",
        ),
        (("larger than max-filesize", "max-filesize"), "too_large", "This file is larger than the maximum allowed download size.", ""),
    ]
    for needles, code, message, hint in rules:
        if any(n in text for n in needles):
            logger.warning("yt-dlp error [%s]: %s%s", code, raw[:300], f" | HINT: {hint}" if hint else "")
            shown = f"{message} [{hint}]" if hint and settings.DEBUG else message
            return DownloaderError(code, shown, hint)
    logger.warning("Unclassified yt-dlp error: %s", raw[:500])
    return DownloaderError("unknown", "Something went wrong while talking to YouTube. Please try again.")


# --------------------------------------------------------------------------- #
# yt-dlp helpers
# --------------------------------------------------------------------------- #
class _YtdlpLogger:
    """Route yt-dlp's console output into Python logging (errors are reported via DownloaderError)."""

    def debug(self, msg: str) -> None:  # noqa: D102
        pass

    def info(self, msg: str) -> None:  # noqa: D102
        pass

    def warning(self, msg: str) -> None:  # noqa: D102
        logger.debug("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:  # noqa: D102
        logger.debug("yt-dlp error: %s", msg)


def detect_js_runtime() -> dict[str, dict[str, Any]] | None:
    """Pick the JavaScript runtime yt-dlp uses for YouTube's challenges.

    yt-dlp only enables ``deno`` on its own, so we look for deno, node, bun or quickjs on
    the PATH (or honour YTDLP_JS_RUNTIME). Returns e.g. ``{"node": {"path": "/usr/bin/node"}}``.
    """
    wanted = (getattr(settings, "YTDLP_JS_RUNTIME", "") or "").strip().lower()
    for name in ([wanted] if wanted else ["deno", "node", "bun", "quickjs"]):
        path = shutil.which(name)
        if path:
            return {name: {"path": path}}
    return None


# YouTube "clients" to try, in order, when the default ones hit a bot check. These are the clients
# yt-dlp itself lists as needing no proof-of-origin (PO) token. ``None`` = yt-dlp's own defaults.
FALLBACK_CLIENTS: list[list[str]] = [["tv", "tv_downgraded"], ["web_embedded", "tv"], ["visionos"], ["mweb", "web_safari"]]
CANONICAL_STRATEGIES: list[list[str] | None] = [None, *FALLBACK_CLIENTS]
STRATEGY_CACHE_KEY = "ytdlp:strategy"
STRATEGY_CACHE_SECONDS = 3600


def _strategies() -> list[list[str] | None]:
    pinned = [c.strip() for c in getattr(settings, "YTDLP_PLAYER_CLIENTS", "").split(",") if c.strip()]
    if pinned:
        return [pinned]  # operator chose the clients: no automatic fallback
    order = list(CANONICAL_STRATEGIES)
    try:
        preferred = cache.get(STRATEGY_CACHE_KEY)
    except Exception:  # a cache outage must never break a download
        preferred = None
    if isinstance(preferred, int) and 0 < preferred < len(order):
        order.insert(0, order.pop(preferred))  # start with whatever worked last time
    return order


def _label(strategy: list[str] | None) -> str:
    return "yt-dlp defaults" if strategy is None else "player_client=" + ",".join(strategy)


def _apply_strategy(opts: dict[str, Any], strategy: list[str] | None) -> dict[str, Any]:
    if not strategy:
        return opts
    return {**opts, "extractor_args": {"youtube": {"player_client": list(strategy)}}}


def _remember(strategy: list[str] | None) -> None:
    if strategy not in CANONICAL_STRATEGIES:
        return
    idx = CANONICAL_STRATEGIES.index(strategy)
    try:
        if idx == 0:
            cache.delete(STRATEGY_CACHE_KEY)
        else:
            cache.set(STRATEGY_CACHE_KEY, idx, STRATEGY_CACHE_SECONDS)
    except Exception:
        logger.debug("Could not remember the working YouTube client", exc_info=True)


RETRY_CODES = {"bot_check", "client_rejected"}  # client-specific refusals: another client set may be accepted
AFTER_BOT_CHECK_CODES = {"unknown", "format_unavailable"}  # once YouTube is challenging us, these are usually client-specific too


def _run_with_fallbacks(attempt):
    """Call ``attempt(strategy)``; if YouTube refuses a client set, retry with the next one.

    If every set fails, the first bot check (the root cause) is reported rather than a vaguer
    follow-on error from a fallback client.
    """
    strategies = _strategies()
    first_bot: DownloaderError | None = None
    for n, strategy in enumerate(strategies):
        last = n == len(strategies) - 1
        try:
            result = attempt(strategy)
        except DownloadCancelled:
            raise
        except Exception as exc:  # yt_dlp raises several unrelated exception types
            err = _translate_exception(exc)
            if err.code == "bot_check" and first_bot is None:
                first_bot = err
            retryable = err.code in RETRY_CODES or (first_bot is not None and err.code in AFTER_BOT_CHECK_CODES)
            if retryable and not last:
                logger.warning("%s failed with %s [%s]; trying %s", _label(strategy), err.code, err.message[:60], _label(strategies[n + 1]))
                continue
            if first_bot is not None and err.code in (RETRY_CODES | AFTER_BOT_CHECK_CODES):
                raise first_bot from exc
            raise err from exc
        if len(strategies) > 1:
            if n > 0:
                logger.info("YouTube request succeeded with %s (remembered for an hour)", _label(strategy))
            _remember(strategy)
        return result
    raise DownloaderError("unknown", "Something went wrong while talking to YouTube. Please try again.")  # pragma: no cover


def diagnose(url: str) -> list[dict[str, Any]]:
    """Try the metadata request with every strategy and report each outcome (for check_ytdlp)."""
    import yt_dlp

    check = validate_url(url)
    if not check.valid:
        raise DownloaderError("invalid_url", check.error)
    results = []
    for strategy in CANONICAL_STRATEGIES:
        row: dict[str, Any] = {"label": _label(strategy), "strategy": strategy}
        try:
            with yt_dlp.YoutubeDL(_apply_strategy({**_base_options(), "skip_download": True}, strategy)) as ydl:
                info = ydl.extract_info(check.normalized_url, download=False)
            row.update(ok=True, title=(info or {}).get("title", ""))
        except Exception as exc:
            err = _translate_exception(exc)
            row.update(ok=False, code=err.code, message=err.message)
        results.append(row)
    return results


def _base_options(auth: YtAuth | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "color": "never",
        "logger": _YtdlpLogger(),
    }
    cookies = getattr(settings, "YTDLP_COOKIES_FILE", "")
    if auth and auth.cookiefile:  # what the visitor chose for this request wins over server-wide settings
        opts["cookiefile"] = str(auth.cookiefile)
    elif auth and auth.browser in ALLOWED_BROWSERS:
        opts["cookiesfrombrowser"] = (auth.browser, None, None, None)
    elif cookies and Path(cookies).is_file():
        opts["cookiefile"] = cookies
    elif getattr(settings, "YTDLP_COOKIES_FROM_BROWSER", "").strip():
        name, _, profile = settings.YTDLP_COOKIES_FROM_BROWSER.strip().partition(":")
        opts["cookiesfrombrowser"] = (name.strip().lower(), profile.strip() or None, None, None)
    proxy = getattr(settings, "YTDLP_PROXY", "")
    if proxy:
        opts["proxy"] = proxy
    ffmpeg = find_ffmpeg()
    if ffmpeg and ffmpeg.source != "system":
        opts["ffmpeg_location"] = ffmpeg.path  # configured or bundled copy; a system FFmpeg is found by yt-dlp itself
    runtime = detect_js_runtime()
    if runtime:
        opts["js_runtimes"] = runtime  # YouTube needs an external JS runtime to solve its challenges
    return opts


def _parse_upload_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def _simplify_formats(raw_formats: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for f in raw_formats or []:
        out.append(
            {
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "height": f.get("height"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
            }
        )
    return out


def extract_video_info(url: str, auth: YtAuth | None = None) -> VideoInfo:
    """Fetch metadata only. Nothing is downloaded."""
    check = validate_url(url)
    if not check.valid:
        raise DownloaderError("invalid_url", check.error)
    def attempt(strategy):
        import yt_dlp

        with yt_dlp.YoutubeDL(_apply_strategy({**_base_options(auth), "skip_download": True}, strategy)) as ydl:
            return ydl.extract_info(check.normalized_url, download=False)

    info = _run_with_fallbacks(attempt)

    if not info:
        raise DownloaderError("unavailable", "No information could be found for this video.")
    if info.get("_type") == "playlist":
        raise DownloaderError("invalid_url", "Playlists aren't supported. Paste a single video link.")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise DownloaderError("live", "Live streams and upcoming premieres can't be downloaded yet.")

    duration = int(info.get("duration") or 0)
    limit = getattr(settings, "MAX_VIDEO_DURATION_SECONDS", 0)
    if limit and duration > limit:
        raise DownloaderError("too_long", f"Videos longer than {limit // 3600} hours aren't supported.")

    return VideoInfo(
        video_id=info.get("id") or check.video_id,
        title=(info.get("title") or "")[:300],
        description=info.get("description") or "",
        uploader=(info.get("uploader") or info.get("channel") or "")[:200],
        duration=duration,
        upload_date=_parse_upload_date(info.get("upload_date")),
        thumbnail_url=info.get("thumbnail") or "",
        webpage_url=check.normalized_url,
        formats=_simplify_formats(info.get("formats")),
    )


def _estimate_size(preset: Preset, formats: list[dict[str, Any]]) -> int | None:
    videos = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none") and f.get("height")]
    audios = [f for f in formats if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
    best_audio = max((a["filesize"] for a in audios if a.get("filesize")), default=None)
    if preset.audio_only:
        return best_audio
    candidates = [v for v in videos if preset.height is None or v["height"] <= preset.height]
    if not candidates:
        return None
    top_height = max(v["height"] for v in candidates)
    sizes = [v["filesize"] for v in candidates if v["height"] == top_height and v.get("filesize")]
    if not sizes:
        return None
    return int(max(sizes) + (best_audio or 0))


def get_available_formats(source: str | VideoInfo) -> list[FormatOption]:
    """Return the presets that make sense for this video, with size estimates."""
    info = source if isinstance(source, VideoInfo) else extract_video_info(source)
    heights = [f["height"] for f in info.formats if f.get("height") and f.get("vcodec") not in (None, "none")]
    max_height = max(heights, default=0)

    options: list[FormatOption] = []
    for preset in PRESETS.values():
        if not preset.audio_only and preset.height and max_height and preset.height > max_height:
            continue  # no point offering 1080p for a 480p source
        options.append(
            FormatOption(
                key=preset.key,
                label=preset.label,
                format=preset.format,
                quality=preset.quality,
                audio_only=preset.audio_only,
                estimated_size=_estimate_size(preset, info.formats),
            )
        )
    return options


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def _expected_total(info_dict: dict[str, Any] | None) -> int:
    formats = (info_dict or {}).get("requested_formats") or []
    sizes = [f.get("filesize") or f.get("filesize_approx") for f in formats]
    return int(sum(sizes)) if formats and all(sizes) else 0


def download_video(
    url: str,
    preset_key: str,
    output_dir: Path,
    *,
    filename_stem: str,
    progress_callback: ProgressCallback | None = None,
    max_filesize: int | None = None,
    auth: YtAuth | None = None,
) -> DownloadResult:
    """Download ``url`` using a preset into ``output_dir`` and return the file.

    ``filename_stem`` must be generated by the caller (never from user input).
    """
    check = validate_url(url)
    if not check.valid:
        raise DownloaderError("invalid_url", check.error)
    preset = PRESETS.get(preset_key)
    if preset is None:
        raise DownloaderError("invalid_format", "That download format isn't available.")
    if not ffmpeg_available():
        hint = ("Run `uv sync` (installs a bundled FFmpeg), or install FFmpeg yourself: `sudo apt install ffmpeg`, "
                "`brew install ffmpeg` or `winget install Gyan.FFmpeg`, or set FFMPEG_LOCATION in .env.")
        logger.error("FFmpeg not found. %s", hint)
        message = "The media tool FFmpeg couldn't be found, so this download can't be processed."
        raise DownloaderError("ffmpeg", f"{message} [{hint}]" if settings.DEBUG else message, hint)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", filename_stem):
        raise DownloaderError("invalid_filename", "Internal error: unsafe file name.")

    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    streams: dict[str, tuple[int, int]] = {}

    def on_progress(d: dict[str, Any]) -> None:
        if progress_callback is None:
            return
        name = d.get("filename") or d.get("tmpfilename") or "stream"
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            streams[name] = (int(d.get("downloaded_bytes") or 0), int(total))
        elif status == "finished":
            size = int(d.get("total_bytes") or d.get("downloaded_bytes") or 0)
            streams[name] = (size, size)
        else:
            return
        downloaded = sum(s[0] for s in streams.values())
        seen_total = sum(max(s[1], s[0]) for s in streams.values())
        total = max(_expected_total(d.get("info_dict")), seen_total)
        progress_callback(
            ProgressUpdate(
                phase="downloading",
                downloaded_bytes=downloaded,
                total_bytes=total,
                speed=float(d.get("speed") or 0.0),
                eta=int(d["eta"]) if d.get("eta") is not None else None,
            )
        )

    def on_postprocess(d: dict[str, Any]) -> None:
        if progress_callback is not None and d.get("status") == "started":
            progress_callback(ProgressUpdate(phase="processing"))

    opts: dict[str, Any] = {
        **_base_options(auth),
        "format": preset.selector,
        "paths": {"home": str(output_dir)},
        "outtmpl": f"{filename_stem}.%(ext)s",
        "restrictfilenames": True,
        "windowsfilenames": True,
        "overwrites": True,
        "progress_hooks": [on_progress],
        "postprocessor_hooks": [on_postprocess],
    }
    if max_filesize:
        opts["max_filesize"] = max_filesize
    if preset.audio_only:
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": preset.audio_codec, "preferredquality": "192"}
        ]
    else:
        opts["merge_output_format"] = "mp4"

    def attempt(strategy):
        import yt_dlp

        streams.clear()
        with yt_dlp.YoutubeDL(_apply_strategy(opts, strategy)) as ydl:
            return ydl.extract_info(check.normalized_url, download=True)

    info = _run_with_fallbacks(attempt)

    filepath = _locate_output(info, output_dir, filename_stem)
    if filepath is None:
        if max_filesize:
            raise DownloaderError("too_large", "This file is larger than the maximum allowed download size.")
        raise DownloaderError("no_output", "The download finished but no file was produced.")

    size = filepath.stat().st_size
    if max_filesize and size > max_filesize:
        filepath.unlink(missing_ok=True)
        raise DownloaderError("too_large", "This file is larger than the maximum allowed download size.")
    return DownloadResult(filepath=filepath, ext=filepath.suffix.lstrip(".").lower(), filesize=size)


def _locate_output(info: dict[str, Any] | None, output_dir: Path, stem: str) -> Path | None:
    """Find the finished file, making sure it lives inside ``output_dir``."""
    candidates: list[Path] = []
    for item in (info or {}).get("requested_downloads") or []:
        if item.get("filepath"):
            candidates.append(Path(item["filepath"]))
    candidates.extend(sorted(output_dir.glob(f"{stem}.*")))
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if (
            resolved.is_file()
            and resolved.parent == output_dir
            and resolved.suffix not in {".part", ".ytdl", ".temp"}
        ):
            return resolved
    return None


def build_cli_command(url: str, preset_key: str) -> dict[str, str]:
    """A yt-dlp command the visitor can run on their own computer (their IP, their browser login)."""
    check = validate_url(url)
    if not check.valid:
        raise DownloaderError("invalid_url", check.error)
    preset = PRESETS.get(preset_key)
    if preset is None:
        raise DownloaderError("invalid_format", "That download format isn't available.")
    parts = ["yt-dlp", "--no-playlist", "--windows-filenames", "-f", preset.selector]
    if preset.audio_only:
        parts += ["-x", "--audio-format", preset.audio_codec or "mp3", "--audio-quality", "192K"]
    else:
        parts += ["--merge-output-format", "mp4"]
    # Same "Title [videoid].ext" naming as the app, so the player can find your place again.
    parts += ["-o", "%(title).80s [%(id)s].%(ext)s", check.normalized_url]
    quote = lambda items: " ".join(shlex.quote(p) for p in items)  # noqa: E731
    return {
        "command": quote(parts),
        "command_with_login": quote(parts[:1] + ["--cookies-from-browser", "firefox"] + parts[1:]),
    }
