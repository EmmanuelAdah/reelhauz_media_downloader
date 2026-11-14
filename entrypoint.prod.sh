#!/bin/sh
set -eu

echo "===================================="
echo "Starting Django production container"
echo "===================================="

WAIT_FOR_DB="${WAIT_FOR_DB:-true}"
DB_WAIT_TIMEOUT="${DB_WAIT_TIMEOUT:-60}"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-true}"

# Local free-proxy settings (yt-dlp-proxy)
ENABLE_LOCAL_PROXY="${ENABLE_LOCAL_PROXY:-false}"
LOCAL_PROXY_PORT="${LOCAL_PROXY_PORT:-8118}"
LOCAL_PROXY_WAIT="${LOCAL_PROXY_WAIT:-60}"
LOCAL_PROXY_REFRESH="${LOCAL_PROXY_REFRESH:-1800}"   # seconds between list rebuilds

# -----------------------------------------------
# Show where we are trying to connect (password hidden)
# -----------------------------------------------
if [ -n "${DATABASE_URL:-}" ]; then
    echo "DATABASE_URL target: $(echo "$DATABASE_URL" | sed -E 's#(://[^:]+):[^@]*@#\1:***@#')"
else
    echo "WARNING: DATABASE_URL is not set."
fi

# -----------------------------------------------
# Wait for PostgreSQL
# -----------------------------------------------
if [ "$WAIT_FOR_DB" = "true" ]; then
    echo "Waiting for database (timeout: ${DB_WAIT_TIMEOUT}s)..."

    elapsed=0
    last_error=""

    while true; do
        if last_error=$(python -c '
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.db import connections
connections["default"].ensure_connection()
' 2>&1); then
            break
        fi

        if [ "$elapsed" -ge "$DB_WAIT_TIMEOUT" ]; then
            echo "ERROR: Database connection timeout after ${DB_WAIT_TIMEOUT}s."
            echo "Last error:"
            echo "$last_error" | tail -n 5
            exit 1
        fi

        echo "Database not ready. Retrying... ($(echo "$last_error" | tail -n 1))"
        sleep 2
        elapsed=$((elapsed + 2))
    done

    echo "Database connection established."
fi

# -----------------------------------------------
# Run migrations (web only; set RUN_MIGRATIONS=false elsewhere)
# -----------------------------------------------
if [ "$RUN_MIGRATIONS" = "true" ]; then
    echo "Applying database migrations..."
    python manage.py migrate --noinput
    echo "Migrations completed."
fi

# -----------------------------------------------
# Outbound proxy for yt-dlp
# -----------------------------------------------
port_open() {
    python -c '
import socket, sys
s = socket.socket()
s.settimeout(1)
try:
    s.connect(("127.0.0.1", int(sys.argv[1])))
except Exception:
    sys.exit(1)
finally:
    s.close()
' "$1"
}

if [ "$ENABLE_LOCAL_PROXY" = "true" ]; then
    if command -v ytdlp-proxy >/dev/null 2>&1; then
        echo "Starting local yt-dlp-proxy on 127.0.0.1:${LOCAL_PROXY_PORT}..."

        # Background supervisor: rebuild the proxy list, run the proxy,
        # and restart it every LOCAL_PROXY_REFRESH seconds or if it crashes.
        (
            while true; do
                ytdlp-proxy update >/dev/null 2>&1 || echo "[proxy] list update failed"
                timeout "$LOCAL_PROXY_REFRESH" ytdlp-proxy || true
                echo "[proxy] restarting..."
                sleep 3
            done
        ) &

        waited=0
        until port_open "$LOCAL_PROXY_PORT"; do
            if [ "$waited" -ge "$LOCAL_PROXY_WAIT" ]; then
                echo "WARNING: local proxy did not open port ${LOCAL_PROXY_PORT} within ${LOCAL_PROXY_WAIT}s."
                echo "WARNING: continuing WITHOUT a proxy."
                unset YTDLP_PROXY
                break
            fi
            sleep 2
            waited=$((waited + 2))
        done

        if port_open "$LOCAL_PROXY_PORT"; then
            export YTDLP_PROXY="http://127.0.0.1:${LOCAL_PROXY_PORT}"
            echo "Local proxy ready."
        fi
    else
        echo "WARNING: ENABLE_LOCAL_PROXY=true but 'ytdlp-proxy' is not installed. Continuing without it."
        unset YTDLP_PROXY
    fi
fi

if [ -n "${YTDLP_PROXY:-}" ]; then
    # Hide credentials in logs
    echo "YTDLP_PROXY: $(echo "$YTDLP_PROXY" | sed -E 's#(://[^:/@]+):[^@]*@#\1:***@#')"
else
    echo "YTDLP_PROXY: not set (yt-dlp will connect directly)"
fi

# -----------------------------------------------
# Start application
# -----------------------------------------------
echo "Executing application command: $*"
exec "$@"