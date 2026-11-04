#!/bin/sh
set -e

# Hand off execution directly to CMD (Gunicorn, Celery, etc.)
exec "$@"