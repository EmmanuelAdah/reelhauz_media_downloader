"""Runs the real yt-dlp and FFmpeg against a tiny local HTTP server (no internet).

This is what proves live progress and cancelling work with the genuine library, not just with mocks.
"""
import http.server
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from downloader.services import ytdlp_service as svc
from downloader.services.ytdlp_service import DownloadCancelled, UrlValidation


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
class RealYtdlpTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dir = Path(tempfile.mkdtemp(prefix="rh_real_"))
        cls.sample = cls.dir / "sample.mp4"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=640x360:rate=30",
             "-f", "lavfi", "-i", "sine=duration=3", "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "2M", "-c:a", "aac",
             str(cls.sample)], check=True)
        data = cls.sample.read_bytes()
        cls.size = len(data)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.send_response(200); self.send_header("Content-Type", "video/mp4"); self.send_header("Content-Length", str(len(data))); self.end_headers()

            def do_GET(self):
                self.do_HEAD()
                for i in range(0, len(data), 40_000):  # slow enough to produce several progress ticks
                    try:
                        self.wfile.write(data[i:i + 40_000]); self.wfile.flush()
                    except OSError:
                        return
                    time.sleep(0.04)

            def log_message(self, *args):
                pass

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        cls.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/video.mp4"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.dir, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        patcher = mock.patch.object(svc, "validate_url", lambda u: UrlValidation(True, "abcdefghijk", self.url))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.out = Path(tempfile.mkdtemp(prefix="rh_out_"))
        self.addCleanup(shutil.rmtree, self.out, True)

    def test_progress_reaches_the_callback_while_downloading(self):
        ticks = []
        result = svc.download_video(self.url, "mp4_720", self.out, filename_stem="clip", progress_callback=ticks.append)
        self.assertEqual(result.filepath.name, "clip.mp4")
        self.assertEqual(result.filesize, self.size)
        downloading = [t for t in ticks if t.phase == "downloading"]
        self.assertGreaterEqual(len(downloading), 3, "expected several live progress updates")
        sizes = [t.downloaded_bytes for t in downloading]
        self.assertEqual(sizes, sorted(sizes))                      # only ever moves forward
        self.assertTrue(all(t.total_bytes == self.size for t in downloading))
        self.assertTrue(all(0 <= t.percent <= 100 for t in ticks))
        self.assertGreater(max(t.percent for t in downloading), 50)
        self.assertEqual(ticks[-1].phase, "processing")             # then the processing stage

    def test_cancelling_from_the_progress_callback_stops_the_real_download(self):
        count = [0]

        def callback(update):
            count[0] += 1
            if count[0] == 3:
                raise DownloadCancelled()

        with self.assertRaises(DownloadCancelled):                  # not wrapped into a generic error
            svc.download_video(self.url, "mp4_720", self.out, filename_stem="clip", progress_callback=callback)
        self.assertFalse((self.out / "clip.mp4").exists())          # never produced a finished file
        self.assertLess(count[0], 20)                                # and stopped promptly

    def test_audio_conversion_goes_through_the_processing_stage(self):
        phases = []
        result = svc.download_video(self.url, "audio_mp3", self.out, filename_stem="aud", progress_callback=lambda u: phases.append(u.phase))
        self.assertEqual(result.ext, "mp3")
        self.assertIn("downloading", phases)
        self.assertEqual(phases[-1], "processing")

    def test_real_ytdlp_accepts_our_sanitised_cookie_file(self):
        from downloader.services.cookies import sanitize_cookies

        raw = "\n".join([
            "\t".join([".youtube.com", "TRUE", "/", "TRUE", "1999999999", "VISITOR_INFO1_LIVE", "abc"]),
            "\t".join(["#HttpOnly_.youtube.com", "TRUE", "/", "TRUE", "1999999999", "LOGIN_INFO", "def"]),
            "\t".join([".google.com", "TRUE", "/", "TRUE", "1999999999", "SID", "ghi"]),
        ])
        text, count = sanitize_cookies(raw)
        self.assertEqual(count, 3)
        cookie_file = self.out / "cookies.txt"
        cookie_file.write_text(text)
        result = svc.download_video(self.url, "mp4_720", self.out, filename_stem="clip", auth=svc.YtAuth(cookiefile=cookie_file))
        self.assertEqual(result.filesize, self.size)  # yt-dlp loaded the cookie jar without complaint and downloaded

    def test_yt_dlp_really_uses_the_ffmpeg_we_point_it_at(self):
        """Wrap FFmpeg in a logging script and hand that over as the 'bundled' copy: conversion must go through it."""
        wrapper = self.out / "ffmpeg-wrapper"
        log = self.out / "calls.log"
        real = shutil.which("ffmpeg")
        wrapper.write_text(f'#!/bin/sh\necho called >> "{log}"\nexec "{real}" "$@"\n')
        wrapper.chmod(0o755)
        with mock.patch.object(svc, "find_ffmpeg", return_value=svc.FfmpegInfo(str(wrapper), "bundled")):
            result = svc.download_video(self.url, "audio_mp3", self.out, filename_stem="aud")
        self.assertEqual(result.ext, "mp3")
        self.assertTrue(log.exists() and log.read_text().count("called") >= 1, "yt-dlp never called the FFmpeg it was pointed at")
