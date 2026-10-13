"""Render the real Django pages to frontend_tests/.pages/*.html for the jsdom tests."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DATABASE_URL", "sqlite:///db.sqlite3")
django.setup()

from django.test import Client
from django.test.utils import setup_test_environment

setup_test_environment()
out = Path(__file__).parent / ".pages"
out.mkdir(exist_ok=True)
client = Client()
for name, url in (("home", "/"), ("watch", "/watch/"), ("cw", "/continue-watching/")):
    (out / f"{name}.html").write_text(client.get(url, HTTP_HOST="localhost").content.decode())
print("rendered pages to", out)
