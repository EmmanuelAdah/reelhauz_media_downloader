from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings

from downloader.services import ytdlp_service as svc
from downloader.services.ytdlp_service import DownloaderError

from .base import VID


class ValidateUrlTests(SimpleTestCase):
    def test_accepts_common_forms(self):
        for url in (
            f"https://www.youtube.com/watch?v={VID}",
            f"https://youtube.com/watch?v={VID}&t=30s",
            f"https://m.youtube.com/watch?v={VID}",
            f"https://youtu.be/{VID}?si=abc",
            f"https://www.youtube.com/shorts/{VID}",
            f"https://www.youtube.com/embed/{VID}",
            f"www.youtube.com/watch?v={VID}",
        ):
            result = svc.validate_url(url)
            self.assertTrue(result.valid, url)
            self.assertEqual(result.video_id, VID)
            self.assertEqual(result.normalized_url, f"https://www.youtube.com/watch?v={VID}")

    def test_rejects_bad_input(self):
        for url in (
            "",
            "not a url",
            "https://vimeo.com/12345",
            f"https://evil.com/watch?v={VID}",
            f"https://youtube.com.evil.com/watch?v={VID}",
            f"https://youtube.com@evil.com/watch?v={VID}",
            "https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/@somechannel",
            "https://www.youtube.com/watch?v=short",
            f"ftp://www.youtube.com/watch?v={VID}",
            "https://www.youtube.com/watch?v=" + "a" * 5000,
            f"javascript:alert('{VID}')",
        ):
            self.assertFalse(svc.validate_url(url).valid, url)


class ErrorTranslationTests(SimpleTestCase):
    def test_known_errors_are_mapped(self):
        cases = {
            "ERROR: [youtube] x: Private video. Sign in": "private",
            "ERROR: Sign in to confirm your age. This video may be inappropriate": "age_restricted",
            "ERROR: Video unavailable": "unavailable",
            "ERROR: Unable to download webpage: <urlopen error timed out>": "network",
            "ERROR: Requested format is not available": "format_unavailable",
            "ffmpeg not found": "ffmpeg",
        }
        for raw, code in cases.items():
            self.assertEqual(svc._translate_exception(Exception(raw)).code, code, raw)

    def test_unknown_error_hides_raw_text(self):
        err = svc._translate_exception(Exception("/srv/secret/path boom"))
        self.assertEqual(err.code, "unknown")
        self.assertNotIn("secret", err.message)


class ExtractAndFormatTests(SimpleTestCase):
    def fake_ydl(self, info=None, exc=None):
        ydl = mock.MagicMock()
        ydl.__enter__.return_value = ydl
        if exc:
            ydl.extract_info.side_effect = exc
        else:
            ydl.extract_info.return_value = info
        return mock.patch("yt_dlp.YoutubeDL", return_value=ydl)

    RAW = {
        "id": VID, "title": "T", "uploader": "U", "duration": 120, "upload_date": "20240501",
        "thumbnail": "https://i.ytimg.com/vi/x/hq.jpg",
        "formats": [
            {"format_id": "1", "height": 720, "vcodec": "avc1", "acodec": "none", "filesize": 100},
            {"format_id": "2", "height": None, "vcodec": "none", "acodec": "mp4a", "filesize": 10},
        ],
    }

    def test_extract_info_success(self):
        with self.fake_ydl(self.RAW):
            info = svc.extract_video_info(f"https://youtu.be/{VID}")
        self.assertEqual(info.title, "T")
        self.assertEqual(info.duration, 120)
        self.assertEqual(str(info.upload_date), "2024-05-01")

    def test_extract_info_maps_errors(self):
        with self.fake_ydl(exc=Exception("Private video")):
            with self.assertRaises(DownloaderError) as ctx:
                svc.extract_video_info(f"https://youtu.be/{VID}")
        self.assertEqual(ctx.exception.code, "private")

    def test_extract_info_rejects_invalid_url_without_calling_ytdlp(self):
        with mock.patch("yt_dlp.YoutubeDL") as ydl:
            with self.assertRaises(DownloaderError):
                svc.extract_video_info("https://example.com/x")
        ydl.assert_not_called()

    def test_live_streams_rejected(self):
        with self.fake_ydl({**self.RAW, "is_live": True}):
            with self.assertRaises(DownloaderError) as ctx:
                svc.extract_video_info(f"https://youtu.be/{VID}")
        self.assertEqual(ctx.exception.code, "live")

    def test_available_formats_hide_unreachable_heights(self):
        with self.fake_ydl(self.RAW):
            info = svc.extract_video_info(f"https://youtu.be/{VID}")
        keys = [f.key for f in svc.get_available_formats(info)]
        self.assertIn("mp4_360", keys)
        self.assertIn("mp4_720", keys)
        self.assertNotIn("mp4_1080", keys)
        self.assertIn("audio_mp3", keys)
        sizes = {f.key: f.estimated_size for f in svc.get_available_formats(info)}
        self.assertEqual(sizes["mp4_720"], 110)  # video + best audio

    def test_download_rejects_unknown_preset(self):
        with self.assertRaises(DownloaderError) as ctx:
            svc.download_video(f"https://youtu.be/{VID}", "rm -rf /", Path("/tmp"), filename_stem="abc")
        self.assertEqual(ctx.exception.code, "invalid_format")

    def test_download_rejects_unsafe_stem(self):
        with mock.patch.object(svc, "ffmpeg_available", return_value=True):
            with self.assertRaises(DownloaderError):
                svc.download_video(f"https://youtu.be/{VID}", "best", Path("/tmp"), filename_stem="../etc/passwd")

    def test_download_uses_preset_selector_and_returns_file(self):
        import tempfile

        out = Path(tempfile.mkdtemp())
        captured = {}

        def fake_cls(opts):
            captured.update(opts)
            ydl = mock.MagicMock()
            ydl.__enter__.return_value = ydl

            def extract(url, download):
                (out / "abc123.mp4").write_bytes(b"x" * 50)
                opts["progress_hooks"][0]({"status": "downloading", "filename": "f", "downloaded_bytes": 5, "total_bytes": 10, "speed": 1.0, "eta": 5})
                return {"requested_downloads": [{"filepath": str(out / "abc123.mp4")}]}

            ydl.extract_info.side_effect = extract
            return ydl

        updates = []
        with mock.patch.object(svc, "ffmpeg_available", return_value=True), mock.patch("yt_dlp.YoutubeDL", side_effect=fake_cls):
            result = svc.download_video(f"https://youtu.be/{VID}", "mp4_720", out, filename_stem="abc123", progress_callback=updates.append)
        self.assertEqual(result.ext, "mp4")
        self.assertEqual(result.filesize, 50)
        self.assertIn("height<=720", captured["format"])
        self.assertTrue(captured["noplaylist"])
        self.assertEqual(updates[0].percent, 50.0)


class YouTubeBotCheckTests(SimpleTestCase):
    RAW = "ERROR: [youtube] abc: Sign in to confirm you\u2019re not a bot. Use --cookies-from-browser or --cookies"

    @override_settings(DEBUG=False)
    def test_visitors_are_not_told_to_configure_cookies(self):
        err = svc._translate_exception(Exception(self.RAW))
        self.assertEqual(err.code, "bot_check")
        self.assertNotIn("cookie", err.message.lower())
        self.assertNotIn("YTDLP", err.message)
        self.assertIn("try again later", err.message)

    @override_settings(DEBUG=False)
    def test_operator_hint_is_kept_and_logged_but_not_shown(self):
        with self.assertLogs("downloader.services.ytdlp_service", level="WARNING") as logs:
            err = svc._translate_exception(Exception(self.RAW))
        self.assertIn("check_ytdlp", err.hint)
        self.assertTrue(any("HINT" in line for line in logs.output))
        self.assertNotIn(err.hint, err.message)

    @override_settings(DEBUG=True)
    def test_hint_is_shown_to_developers_in_debug(self):
        self.assertIn("check_ytdlp", svc._translate_exception(Exception(self.RAW)).message)

    @override_settings(DEBUG=False)
    def test_age_restricted_message_is_visitor_safe(self):
        err = svc._translate_exception(Exception("Sign in to confirm your age"))
        self.assertEqual(err.code, "age_restricted")
        self.assertNotIn("YTDLP", err.message)


class YtdlpOptionsTests(SimpleTestCase):
    def test_js_runtime_prefers_deno_then_node(self):
        which = {"deno": "/usr/bin/deno", "node": "/usr/bin/node"}
        with mock.patch.object(svc.shutil, "which", side_effect=lambda n: which.get(n)):
            self.assertEqual(svc.detect_js_runtime(), {"deno": {"path": "/usr/bin/deno"}})
        with mock.patch.object(svc.shutil, "which", side_effect=lambda n: {"node": "/usr/bin/node"}.get(n)):
            self.assertEqual(svc.detect_js_runtime(), {"node": {"path": "/usr/bin/node"}})
        with mock.patch.object(svc.shutil, "which", return_value=None):
            self.assertIsNone(svc.detect_js_runtime())

    @override_settings(YTDLP_JS_RUNTIME="bun")
    def test_js_runtime_can_be_forced(self):
        with mock.patch.object(svc.shutil, "which", side_effect=lambda n: {"bun": "/x/bun", "deno": "/x/deno"}.get(n)):
            self.assertEqual(svc.detect_js_runtime(), {"bun": {"path": "/x/bun"}})

    @override_settings(YTDLP_PROXY="http://proxy.example:3128")
    def test_options_carry_proxy_and_runtime(self):
        with mock.patch.object(svc.shutil, "which", side_effect=lambda n: {"node": "/usr/bin/node"}.get(n)):
            opts = svc._base_options()
        self.assertEqual(opts["proxy"], "http://proxy.example:3128")
        self.assertEqual(opts["js_runtimes"], {"node": {"path": "/usr/bin/node"}})
        self.assertTrue(opts["noplaylist"])

    def test_yt_dlp_solver_package_is_installed(self):
        import importlib.util

        self.assertIsNotNone(importlib.util.find_spec("yt_dlp_ejs"), "install yt-dlp[default]")


class ClientFallbackTests(SimpleTestCase):
    """When YouTube shows a bot check, retry with other client sets and remember what worked."""

    BOT = "ERROR: [youtube] x: Sign in to confirm you\u2019re not a bot"
    RAW = {"id": VID, "title": "T", "uploader": "U", "duration": 60, "upload_date": "20240501", "thumbnail": "", "formats": []}
    WORKING = ["tv", "tv_downgraded"]

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.attempts = []

    def factory(self, works=None, error=None):
        works = works or (lambda clients: clients == self.WORKING)

        def make(opts):
            clients = opts.get("extractor_args", {}).get("youtube", {}).get("player_client")
            self.attempts.append(clients)
            ydl = mock.MagicMock()
            ydl.__enter__.return_value = ydl

            def extract(url, download):
                if works(clients):
                    return self.RAW
                raise Exception(error or self.BOT)

            ydl.extract_info.side_effect = extract
            return ydl

        return make

    def test_retries_other_clients_after_a_bot_check_and_succeeds(self):
        with mock.patch("yt_dlp.YoutubeDL", side_effect=self.factory()):
            info = svc.extract_video_info(f"https://youtu.be/{VID}")
        self.assertEqual(info.title, "T")
        self.assertEqual(self.attempts, [None, self.WORKING])  # defaults first, then the first fallback

    def test_remembers_the_working_client_for_next_time(self):
        with mock.patch("yt_dlp.YoutubeDL", side_effect=self.factory()):
            svc.extract_video_info(f"https://youtu.be/{VID}")
            self.attempts.clear()
            svc.extract_video_info(f"https://youtu.be/{VID}")
        self.assertEqual(self.attempts, [self.WORKING])  # straight to what worked, no wasted bot checks

    def test_gives_up_with_a_bot_check_error_when_every_client_is_refused(self):
        with mock.patch("yt_dlp.YoutubeDL", side_effect=self.factory(works=lambda c: False)):
            with self.assertRaises(DownloaderError) as ctx:
                svc.extract_video_info(f"https://youtu.be/{VID}")
        self.assertEqual(ctx.exception.code, "bot_check")
        self.assertEqual(len(self.attempts), len(svc.CANONICAL_STRATEGIES))

    def test_other_errors_are_not_retried(self):
        with mock.patch("yt_dlp.YoutubeDL", side_effect=self.factory(works=lambda c: False, error="ERROR: Private video")):
            with self.assertRaises(DownloaderError) as ctx:
                svc.extract_video_info(f"https://youtu.be/{VID}")
        self.assertEqual((ctx.exception.code, len(self.attempts)), ("private", 1))

    @override_settings(YTDLP_PLAYER_CLIENTS="web_embedded")
    def test_pinned_clients_disable_fallback(self):
        with mock.patch("yt_dlp.YoutubeDL", side_effect=self.factory(works=lambda c: False)):
            with self.assertRaises(DownloaderError):
                svc.extract_video_info(f"https://youtu.be/{VID}")
        self.assertEqual(self.attempts, [["web_embedded"]])

    def test_download_also_retries(self):
        import tempfile

        out = Path(tempfile.mkdtemp())

        def make(opts):
            clients = opts.get("extractor_args", {}).get("youtube", {}).get("player_client")
            self.attempts.append(clients)
            ydl = mock.MagicMock()
            ydl.__enter__.return_value = ydl

            def extract(url, download):
                if clients != self.WORKING:
                    raise Exception(self.BOT)
                (out / "abc123.mp4").write_bytes(b"x" * 10)
                return {"requested_downloads": [{"filepath": str(out / "abc123.mp4")}]}

            ydl.extract_info.side_effect = extract
            return ydl

        with mock.patch.object(svc, "ffmpeg_available", return_value=True), mock.patch("yt_dlp.YoutubeDL", side_effect=make):
            result = svc.download_video(f"https://youtu.be/{VID}", "mp4_720", out, filename_stem="abc123")
        self.assertEqual((result.filesize, self.attempts), (10, [None, self.WORKING]))

    def test_strategy_list_only_uses_clients_that_need_no_po_token(self):
        flat = {c for group in svc.FALLBACK_CLIENTS for c in group}
        self.assertTrue(flat <= {"tv", "tv_downgraded", "web_embedded", "visionos", "mweb", "web_safari"})
        self.assertIsNone(svc.CANONICAL_STRATEGIES[0])  # yt-dlp defaults are always tried first

    def test_diagnose_reports_every_strategy(self):
        with mock.patch("yt_dlp.YoutubeDL", side_effect=self.factory()):
            rows = svc.diagnose(f"https://youtu.be/{VID}")
        self.assertEqual(len(rows), len(svc.CANONICAL_STRATEGIES))
        self.assertFalse(rows[0]["ok"])
        self.assertEqual(rows[0]["code"], "bot_check")
        self.assertTrue(rows[1]["ok"])
        self.assertEqual(rows[1]["strategy"], self.WORKING)


class BrowserCookiesTests(SimpleTestCase):
    @override_settings(YTDLP_COOKIES_FROM_BROWSER="Firefox")
    def test_browser_name(self):
        self.assertEqual(svc._base_options()["cookiesfrombrowser"], ("firefox", None, None, None))

    @override_settings(YTDLP_COOKIES_FROM_BROWSER="chrome:Profile 1")
    def test_browser_with_profile(self):
        self.assertEqual(svc._base_options()["cookiesfrombrowser"], ("chrome", "Profile 1", None, None))

    @override_settings(YTDLP_COOKIES_FROM_BROWSER="")
    def test_not_set_by_default(self):
        self.assertNotIn("cookiesfrombrowser", svc._base_options())

    @override_settings(DEBUG=False)
    def test_unreadable_browser_cookies_get_a_clear_operator_hint(self):
        err = svc._translate_exception(Exception("could not find firefox cookies database in /home/x"))
        self.assertEqual(err.code, "cookies")
        self.assertIn("YTDLP_COOKIES_FROM_BROWSER", err.hint)
        self.assertNotIn("YTDLP", err.message)
