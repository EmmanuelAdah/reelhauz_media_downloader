FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
# Pin to a specific version, e.g. denoland/deno:bin-2.5.6
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# Non-root user, with /app owned by it from the start
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/tmp_downloads \
    && chown -R app:app /app

WORKDIR /app
USER app

# Dependencies (cached layer)
COPY --chown=app:app pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# Application source
COPY --chown=app:app . .

RUN uv sync --frozen --no-dev \
    && DJANGO_DEBUG=False \
       DJANGO_SECRET_KEY=build-only \
       python manage.py collectstatic --noinput

EXPOSE 10000

ENTRYPOINT ["/bin/sh", "/app/entrypoint.prod.sh"]

CMD ["sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-10000} --workers ${WEB_CONCURRENCY:-2} --threads 4 --timeout 120 --access-logfile - --error-logfile -"]