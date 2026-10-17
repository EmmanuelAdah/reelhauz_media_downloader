"""Validation and minimisation of a user-supplied cookies.txt (Netscape format).

Only YouTube/Google cookies are kept; every other line (other sites, banking, mail, ...) is dropped before the
file is written anywhere. The result lives in a private temporary file for one download and is then deleted.
"""
from __future__ import annotations

import re

from django.conf import settings

from downloader.services.ytdlp_service import DownloaderError

ALLOWED_DOMAINS = ("youtube.com", "google.com", "googlevideo.com", "youtube-nocookie.com")
HTTPONLY_PREFIX = "#HttpOnly_"


def _domain_allowed(domain: str) -> bool:
    domain = domain.lstrip(".").lower()
    return any(domain == d or domain.endswith("." + d) for d in ALLOWED_DOMAINS)


def sanitize_cookies(raw: object) -> tuple[str, int]:
    """Return (clean Netscape cookies text, number of cookies kept) or raise DownloaderError."""
    if not isinstance(raw, str) or not raw.strip():
        raise DownloaderError("bad_cookies", "Choose a cookies.txt file first.")
    if len(raw.encode("utf-8", "ignore")) > settings.MAX_COOKIES_FILE_KB * 1024:
        raise DownloaderError("bad_cookies", "That file is too large to be a cookies.txt file.")

    kept: list[str] = []
    for line in raw.splitlines():
        line = line.rstrip("\r\n")
        if not line.strip() or "\x00" in line:
            continue
        body = line[len(HTTPONLY_PREFIX):] if line.startswith(HTTPONLY_PREFIX) else line
        if body.startswith("#"):
            continue  # comment / header
        fields = body.split("\t")
        if len(fields) != 7:  # tolerate copy-paste that turned tabs into spaces
            fields = re.split(r"\s+", body.strip(), maxsplit=6)
            if len(fields) != 7:
                continue
            line = ("#HttpOnly_" if line.startswith(HTTPONLY_PREFIX) else "") + "\t".join(fields)
        if _domain_allowed(fields[0]):
            kept.append(line)

    if not kept:
        raise DownloaderError(
            "bad_cookies",
            "No YouTube cookies were found in that file. Export your cookies for youtube.com in the Netscape cookies.txt "
            "format while logged in to YouTube.",
        )
    return "# Netscape HTTP Cookie File\n" + "\n".join(kept) + "\n", len(kept)
