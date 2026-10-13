from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from downloader.services import ytdlp_service
from downloader.services.ytdlp_service import DownloaderError


class Command(BaseCommand):
    help = "Diagnose yt-dlp / YouTube access from this server. Optionally pass a video URL to test an extraction."

    def add_arguments(self, parser):
        parser.add_argument("url", nargs="?", help="YouTube URL to test (metadata only, nothing is downloaded)")

    def handle(self, *args, **opts):
        import yt_dlp

        logging.getLogger("downloader.services.ytdlp_service").setLevel(logging.ERROR)  # keep the report readable
        ok, bad = self.style.SUCCESS("OK  "), self.style.ERROR("MISSING")
        warn = self.style.WARNING
        self.stdout.write(f"yt-dlp version:        {yt_dlp.version.__version__}")
        ejs = importlib.util.find_spec("yt_dlp_ejs") is not None
        self.stdout.write(f"yt-dlp-ejs (solver):   {ok if ejs else bad}" + ("" if ejs else "  -> `uv add 'yt-dlp[default]'` then `uv sync`"))
        runtime = ytdlp_service.detect_js_runtime()
        if runtime:
            name, cfg = next(iter(runtime.items()))
            version = ""
            try:
                version = subprocess.run([cfg["path"], "--version"], capture_output=True, text=True, timeout=5).stdout.strip().splitlines()[0]
            except Exception:
                pass
            self.stdout.write(f"JavaScript runtime:    {ok} {name} {version} ({cfg['path']})")
        else:
            self.stdout.write(f"JavaScript runtime:    {bad}  -> install deno (https://deno.com) or node >= 20")
        self.stdout.write(f"FFmpeg:                {ok if shutil.which('ffmpeg') else bad}")
        cookies = settings.YTDLP_COOKIES_FILE
        self.stdout.write("Cookies file:          " + (f"{ok} {cookies}" if cookies and Path(cookies).is_file() else warn("not configured") + (f" (path {cookies} not found)" if cookies else "")))
        self.stdout.write("Proxy:                 " + (settings.YTDLP_PROXY.split("@")[-1] if settings.YTDLP_PROXY else "none"))

        self.stdout.write("Browser cookies:       " + (settings.YTDLP_COOKIES_FROM_BROWSER or "not configured"))
        self.stdout.write("Pinned clients:         " + (settings.YTDLP_PLAYER_CLIENTS or "none (automatic fallback)"))

        if not opts["url"]:
            self.stdout.write("\nTip: pass a video URL to test how YouTube answers from this machine.")
            return
        self.stdout.write(f"\nTesting {opts['url']} with each YouTube client set (metadata only, nothing is downloaded):")
        try:
            rows = ytdlp_service.diagnose(opts["url"])
        except DownloaderError as exc:
            self.stdout.write(self.style.ERROR(exc.message))
            raise SystemExit(1)
        for row in rows:
            if row["ok"]:
                self.stdout.write(f"  {ok} {row['label']:<38} -> {row['title']!r}")
            else:
                self.stdout.write(f"  {self.style.ERROR('FAIL')} {row['label']:<38} -> [{row['code']}] {row['message']}")
        working = [r for r in rows if r["ok"]]
        if working:
            self.stdout.write(self.style.SUCCESS("\nYouTube is reachable. The app retries these client sets automatically and remembers the one that works."))
            if not rows[0]["ok"]:
                self.stdout.write(f"To skip the retries, set YTDLP_PLAYER_CLIENTS={','.join(working[0]['strategy'])}")
            return
        codes = {r["code"] for r in rows}
        if codes == {"network"}:
            self.stdout.write(self.style.ERROR("\nCouldn't reach YouTube at all from this machine (network, DNS, firewall or proxy). Fix connectivity first."))
            raise SystemExit(1)
        self.stdout.write(self.style.ERROR("\nEvery client set was refused."))
        if "bot_check" in codes:
            self.stdout.write(warn(
                "YouTube is challenging this IP address. Next steps, easiest first:\n"
                "  1. Running on your own computer? Set YTDLP_COOKIES_FROM_BROWSER=firefox (or chrome) in .env while logged in to YouTube in that browser.\n"
                "  2. Try a different network (home connection, not a VPN or datacenter) or set YTDLP_PROXY.\n"
                "  3. Export cookies to a file and set YTDLP_COOKIES_FILE (use a throwaway account).\n"
                "  4. Advanced: install the bgutil PO-token provider plugin."
            ))
        raise SystemExit(1)
