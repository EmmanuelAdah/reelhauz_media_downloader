"""Django settings for the video_downloader project (environment driven)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


# --------------------------------------------------------------------------- #
# Env helpers
# --------------------------------------------------------------------------- #
def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def database_from_url(url: str) -> dict:
    """Parse DATABASE_URL (postgres:// or sqlite:///) without extra dependencies."""
    parsed = urlparse(url)
    if parsed.scheme == "sqlite":
        raw = unquote(parsed.path)
        name = Path(raw[1:]) if raw.startswith("//") else BASE_DIR / raw.lstrip("/")
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": str(name)}
    if parsed.scheme in {"postgres", "postgresql", "psql"}:
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(parsed.path.lstrip("/")),
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname or "localhost",
            "PORT": str(parsed.port or 5432),
            "CONN_MAX_AGE": 60,
        }
    raise ImproperlyConfigured(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")


TESTING = "test" in sys.argv

# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG or TESTING:
        SECRET_KEY = "insecure-development-key-do-not-use-in-production"
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "whitenoise.runserver_nostatic",
    "django.contrib.staticfiles",
    "downloader.apps.DownloaderConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --------------------------------------------------------------------------- #
# Database / cache
# --------------------------------------------------------------------------- #
DATABASES = {"default": database_from_url(os.environ.get("DATABASE_URL") or "sqlite:///db.sqlite3")}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REDIS_URL = os.environ.get("REDIS_URL", "")
if REDIS_URL and not TESTING:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
if TESTING:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # fast tests only

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "home"

# --------------------------------------------------------------------------- #
# i18n / static / media
# --------------------------------------------------------------------------- #
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"
        if not DEBUG
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}

# Temporary staging area for yt-dlp/FFmpeg output. Files live here only until the user has saved them.
DOWNLOAD_TMP_DIR = Path(os.environ.get("DOWNLOAD_TMP_DIR", str(BASE_DIR / "tmp_downloads")))

# --------------------------------------------------------------------------- #
# Downloader configuration
# --------------------------------------------------------------------------- #
MAX_DOWNLOAD_SIZE_MB = env_int("MAX_DOWNLOAD_SIZE_MB", 1024)  # 0 disables
MAX_VIDEO_DURATION_SECONDS = env_int("MAX_VIDEO_DURATION_SECONDS", 3 * 3600)  # 0 disables
MAX_CONCURRENT_DOWNLOADS = env_int("MAX_CONCURRENT_DOWNLOADS", 5)  # server-wide
TRANSFER_TTL_SECONDS = env_int("TRANSFER_TTL_SECONDS", 3600)  # undelivered files/state are purged after this
QUEUE_TIMEOUT_SECONDS = env_int("QUEUE_TIMEOUT_SECONDS", 90)  # fail jobs no worker picked up
CHECK_WORKER_BEFORE_START = env_bool("CHECK_WORKER_BEFORE_START", True)
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "")  # optional, for age-gated/bot-checked videos
YTDLP_COOKIES_FROM_BROWSER = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "")  # local use: "firefox", "chrome" or "chrome:Profile 1"
YTDLP_PLAYER_CLIENTS = os.environ.get("YTDLP_PLAYER_CLIENTS", "")  # pin YouTube clients, e.g. "tv,tv_downgraded" (disables automatic fallback)
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "")  # e.g. http://user:pass@host:port; a residential/home IP avoids most bot checks
YTDLP_JS_RUNTIME = os.environ.get("YTDLP_JS_RUNTIME", "")  # deno | node | bun | quickjs (auto-detected when empty)
RATE_LIMIT_METADATA = (env_int("RATE_LIMIT_METADATA", 10), 60)  # per user (or IP when anonymous), per minute
RATE_LIMIT_DOWNLOAD = (env_int("RATE_LIMIT_DOWNLOAD", 3), 60)
RATE_LIMIT_PLAYBACK = (env_int("RATE_LIMIT_PLAYBACK", 120), 60)
TRUSTED_PROXY_COUNT = env_int("TRUSTED_PROXY_COUNT", 0)  # >0: read client IP from X-Forwarded-For

# --------------------------------------------------------------------------- #
# Celery
# --------------------------------------------------------------------------- #
CELERY_BROKER_URL = REDIS_URL or "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = REDIS_URL or "redis://localhost:6379/0"
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_ALWAYS_EAGER", False) or TESTING
CELERY_TASK_TIME_LIMIT = 4 * 3600
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TRACK_STARTED = True

# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #
SECURE_COOKIES = env_bool("DJANGO_SECURE_COOKIES", False)  # set true behind HTTPS
SESSION_COOKIE_SECURE = SECURE_COOKIES
CSRF_COOKIE_SECURE = SECURE_COOKIES
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "SAMEORIGIN"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
if env_bool("DJANGO_BEHIND_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"std": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "std"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
    "loggers": {"django.db.backends": {"level": "WARNING"}},
}
