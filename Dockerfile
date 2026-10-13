FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
# Deno: the JavaScript runtime yt-dlp needs to solve YouTube's challenges
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

COPY . .
RUN DJANGO_DEBUG=False DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput \
    && useradd --create-home app \
    && mkdir -p /app/tmp_downloads \
    && chown -R app:app /app/tmp_downloads
USER app

EXPOSE 8000
# gthread: each open live-progress (SSE) connection holds a thread, so give every worker plenty
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--worker-class", "gthread", "--workers", "2", "--threads", "16", "--timeout", "120"]
