"""Visitor-chosen access to YouTube: cookies.txt, browser login, run-it-yourself command. Credentials are never kept."""
import json
import shlex
import stat
import time
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.test import Client, SimpleTestCase, override_settings
from django.urls import reverse

from downloader.services import cookies as cookie_service
from downloader.services import quick_service, ytdlp_service
from downloader.services.ytdlp_service import DownloadResult, DownloaderError, ProgressUpdate, YtAuth

from .base import URL, VID, AppTestCase, sample_info
from .test_quick import post

TOKEN = "k" * 43
TAB = "\t"


def cookie_line(domain, name="SID", value="secret-value", http_only=False):
    prefix = "#HttpOnly_" if http_only else ""
    return TAB.join([f"{prefix}{domain}", "TRUE", "/", "TRUE", "1999999999", name, value])


SAMPLE_COOKIES = "\n".join([
    "# Netscape HTTP Cookie File",
    cookie_line(".youtube.com", "VISITOR_INFO1_LIVE", "yt-secret"),
    cookie_line(".google.com", "SID", "google-secret"),
    cookie_line(".youtube.com", "LOGIN_INFO", "login-secret", http_only=True),
    cookie_line("accounts.google.com", "ACCOUNT", "acct-secret"),
    cookie_line(".mybank.example", "session", "BANK-SECRET"),
    cookie_line("mail.example.org", "mailsid", "MAIL-SECRET"),
    cookie_line(".notyoutube.com", "evil", "LOOKALIKE-SECRET"),
])


class CookieSanitizerTests(SimpleTestCase):
    def test_keeps_only_youtube_and_google_cookies(self):
        text, count = cookie_service.sanitize_cookies(SAMPLE_COOKIES)
        self.assertEqual(count, 4)
        for kept in ("yt-secret", "google-secret", "login-secret", "acct-secret"):
            self.assertIn(kept, text)
        for dropped in ("BANK-SECRET", "MAIL-SECRET", "LOOKALIKE-SECRET"):
            self.assertNotIn(dropped, text)  # other sites' cookies never reach disk
        self.assertTrue(text.startswith("# Netscape HTTP Cookie File\n"))
        self.assertIn("#HttpOnly_.youtube.com", text)  # yt-dlp understands this prefix

    def test_tolerates_tabs_turned_into_spaces(self):
        pasted = "# Netscape HTTP Cookie File\n.youtube.com TRUE / TRUE 1999999999 SID abc123"
        text, count = cookie_service.sanitize_cookies(pasted)
        self.assertEqual(count, 1)
        self.assertIn(TAB.join([".youtube.com", "TRUE", "/", "TRUE", "1999999999", "SID", "abc123"]), text)

    def test_rejects_junk_empty_and_other_sites_only(self):
        for bad in ("", "   ", None, 42, "just some text", "# comment only", cookie_line(".mybank.example")):
            with self.assertRaises(DownloaderError) as ctx:
                cookie_service.sanitize_cookies(bad)
            self.assertEqual(ctx.exception.code, "bad_cookies", bad)

    @override_settings(MAX_COOKIES_FILE_KB=1)
    def test_rejects_oversized_files(self):
        with self.assertRaises(DownloaderError) as ctx:
            cookie_service.sanitize_cookies(cookie_line(".youtube.com") + "\n" + "x" * 5000)
        self.assertIn("too large", ctx.exception.message)

    def test_drops_null_bytes_and_comments(self):
        text, count = cookie_service.sanitize_cookies("#x\n" + cookie_line(".youtube.com", value="a\x00b") + "\n" + cookie_line(".youtube.com", "OK", "fine"))
        self.assertEqual(count, 1)
        self.assertNotIn("\x00", text)


class YtAuthOptionTests(SimpleTestCase):
    def test_visitor_cookie_file_is_used(self):
        opts = ytdlp_service._base_options(YtAuth(cookiefile=Path("/tmp/x/cookies.txt")))
        self.assertEqual(opts["cookiefile"], "/tmp/x/cookies.txt")
        self.assertNotIn("cookiesfrombrowser", opts)

    def test_visitor_browser_is_used(self):
        self.assertEqual(ytdlp_service._base_options(YtAuth(browser="firefox"))["cookiesfrombrowser"], ("firefox", None, None, None))

    def test_unknown_browser_is_ignored(self):
        self.assertNotIn("cookiesfrombrowser", ytdlp_service._base_options(YtAuth(browser="../../etc/passwd")))

    @override_settings(YTDLP_COOKIES_FILE=__file__, YTDLP_COOKIES_FROM_BROWSER="chrome")
    def test_visitor_choice_overrides_server_wide_settings(self):
        self.assertEqual(ytdlp_service._base_options(YtAuth(browser="firefox"))["cookiesfrombrowser"][0], "firefox")
        self.assertEqual(ytdlp_service._base_options(YtAuth(cookiefile=Path("/p/c.txt")))["cookiefile"], "/p/c.txt")
        self.assertEqual(ytdlp_service._base_options()["cookiefile"], __file__)  # unchanged without a choice


class CommandBuilderTests(SimpleTestCase):
    def test_video_command(self):
        out = ytdlp_service.build_cli_command(f"https://youtu.be/{VID}?si=tracking", "mp4_720")
        argv = shlex.split(out["command"])  # round-trips through a shell parser
        self.assertEqual(argv[0], "yt-dlp")
        self.assertIn("height<=720", argv[argv.index("-f") + 1])
        self.assertEqual(argv[argv.index("--merge-output-format") + 1], "mp4")
        self.assertEqual(argv[-1], f"https://www.youtube.com/watch?v={VID}")  # normalised, tracking params dropped
        self.assertIn("[%(id)s]", argv[argv.index("-o") + 1])  # same naming as the app
        self.assertEqual(shlex.split(out["command_with_login"])[1:3], ["--cookies-from-browser", "firefox"])

    def test_audio_command(self):
        argv = shlex.split(ytdlp_service.build_cli_command(f"https://youtu.be/{VID}", "audio_mp3")["command"])
        self.assertIn("-x", argv)
        self.assertEqual(argv[argv.index("--audio-format") + 1], "mp3")
        self.assertNotIn("--merge-output-format", argv)

    def test_nothing_unsafe_reaches_the_command(self):
        # the command is rebuilt from the parsed video id, so text smuggled after the id is simply dropped
        for sneaky in (f"https://youtu.be/{VID}; rm -rf ~", f"https://youtu.be/{VID}?x=$(id)", f"https://youtu.be/{VID}#`reboot`"):
            argv = shlex.split(ytdlp_service.build_cli_command(sneaky, "best")["command"])
            self.assertEqual(argv[-1], f"https://www.youtube.com/watch?v={VID}", sneaky)
            self.assertFalse({"rm", "-rf", "~", "reboot"} & set(argv), sneaky)
            self.assertNotIn("$(", " ".join(argv))
        for bad_url in (f"https://youtu.be/{VID}$(id)", "https://evil.com/x", "javascript:alert(1)"):
            with self.assertRaises(DownloaderError):
                ytdlp_service.build_cli_command(bad_url, "best")
        with self.assertRaises(DownloaderError):
            ytdlp_service.build_cli_command(f"https://youtu.be/{VID}", "best; reboot")


class AccessFlowTests(AppTestCase):
    def metadata_with(self, auth, **extra):
        seen = {}

        def extract(url, auth=None):
            seen["auth"] = auth
            if auth and auth.cookiefile:
                seen["cookie_text"] = Path(auth.cookiefile).read_text()
                seen["cookie_mode"] = stat.S_IMODE(Path(auth.cookiefile).stat().st_mode)
                seen["cookie_path"] = Path(auth.cookiefile)
            return sample_info()

        with mock.patch.object(ytdlp_service, "extract_video_info", side_effect=extract):
            res = post(Client(), "api_quick_metadata", {"url": URL, "auth": auth}, **extra)
        return res, seen

    # ---- cookies.txt for the preview
    def test_preview_uses_the_uploaded_cookies_then_deletes_them(self):
        res, seen = self.metadata_with({"mode": "cookies", "cookies": SAMPLE_COOKIES})
        self.assertEqual(res.status_code, 200)
        self.assertIn("yt-secret", seen["cookie_text"])
        self.assertNotIn("BANK-SECRET", seen["cookie_text"])
        self.assertEqual(seen["cookie_mode"], 0o600)                       # owner-only file
        self.assertFalse(seen["cookie_path"].exists())                      # gone right after the request
        self.assertFalse(seen["cookie_path"].parent.exists())
        self.assertNotIn("secret", res.content.decode().lower())            # never echoed back

    def test_bad_cookies_are_rejected_with_a_clear_message(self):
        res, _ = self.metadata_with({"mode": "cookies", "cookies": "nope"})
        self.assertEqual((res.status_code, res.json()["code"]), (422, "bad_cookies"))

    def test_no_auth_means_no_credentials(self):
        _, seen = self.metadata_with(None)
        self.assertIsNone(seen["auth"])

    def test_unknown_access_mode_is_rejected(self):
        res, _ = self.metadata_with({"mode": "sudo"})
        self.assertEqual((res.status_code, res.json()["code"]), (422, "bad_auth"))

    # ---- cookies.txt for the download job
    def test_job_uses_cookies_from_a_private_file_that_is_removed_afterwards(self):
        puts, observed = [], {}
        original = quick_service._put

        def spy(token, state):
            puts.append(json.dumps(state))
            return original(token, state)

        def download(url, preset, outdir, *, filename_stem, progress_callback, max_filesize, auth=None):
            path = Path(auth.cookiefile)
            observed.update(path=path, mode=stat.S_IMODE(path.stat().st_mode), dir_mode=stat.S_IMODE(path.parent.stat().st_mode),
                            text=path.read_text())
            progress_callback(ProgressUpdate("downloading", 5, 10, 1.0, 1))
            out = Path(outdir) / f"{filename_stem}.mp4"
            out.write_bytes(b"video")
            return DownloadResult(out, "mp4", 5)

        with mock.patch.object(quick_service, "_put", spy), \
             mock.patch.object(quick_service.secrets, "token_urlsafe", return_value=TOKEN), \
             mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()), \
             mock.patch.object(ytdlp_service, "download_video", side_effect=download):
            res = post(Client(), "api_quick_download", {"url": URL, "preset": "mp4_720", "auth": {"mode": "cookies", "cookies": SAMPLE_COOKIES}})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Client().get(reverse("api_quick_status", args=[TOKEN])).json()["video"]["status"], "completed")
        self.assertEqual((observed["mode"], observed["dir_mode"]), (0o600, 0o700))
        self.assertIn("yt-secret", observed["text"])
        self.assertNotIn("BANK-SECRET", observed["text"])
        self.assertFalse(observed["path"].exists(), "cookies must be deleted as soon as the job ends")
        self.assertTrue((quick_service.root_dir() / TOKEN).exists())        # the finished video is still there for the visitor
        # nothing secret ever went into the cache (Redis)
        everything = " ".join(puts) + json.dumps(cache.get(f"transfer:{TOKEN}"))
        for secret in ("yt-secret", "google-secret", "login-secret", "BANK-SECRET", "cookies_text"):
            self.assertNotIn(secret, everything)
        self.assertEqual(json.loads(puts[0])["auth"], {"mode": "cookies"})  # only the *kind* of access is recorded

    def test_cookies_are_deleted_when_the_job_fails_or_is_cancelled(self):
        def failing(url, preset, outdir, *, filename_stem, progress_callback, max_filesize, auth=None):
            self.assertTrue(Path(auth.cookiefile).exists())
            raise DownloaderError("bot_check", "YouTube is blocking downloads from this server right now. Please try again later.")

        with mock.patch.object(quick_service.secrets, "token_urlsafe", return_value=TOKEN), \
             mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()), \
             mock.patch.object(ytdlp_service, "download_video", side_effect=failing):
            post(Client(), "api_quick_download", {"url": URL, "preset": "best", "auth": {"mode": "cookies", "cookies": SAMPLE_COOKIES}})
        video = Client().get(reverse("api_quick_status", args=[TOKEN])).json()["video"]
        self.assertEqual((video["status"], video["error_code"]), ("failed", "bot_check"))   # UI uses this to offer the options
        self.assertFalse((quick_service.root_dir() / TOKEN).exists())

    def test_cookies_are_deleted_even_if_the_job_never_runs(self):
        with mock.patch.object(quick_service.secrets, "token_urlsafe", return_value=TOKEN), \
             mock.patch("downloader.tasks.quick_download_task.delay"):
            post(Client(), "api_quick_download", {"url": URL, "preset": "best", "auth": {"mode": "cookies", "cookies": SAMPLE_COOKIES}})
        path = quick_service.root_dir() / TOKEN / "cookies.txt"
        self.assertTrue(path.exists())
        Client().post(reverse("api_quick_cancel", args=[TOKEN]), data="{}", content_type="application/json")  # visitor cancels
        self.assertFalse(path.exists())

    def test_expired_cookie_file_gives_a_clear_error(self):
        quick_service._put(TOKEN, {"status": "pending", "created": time.time(), "auth": {"mode": "cookies"}, "video_id": VID, "title": "T"})
        quick_service.run(TOKEN, URL, "best")
        video = Client().get(reverse("api_quick_status", args=[TOKEN])).json()["video"]
        self.assertEqual(video["error_code"], "bad_cookies")

    # ---- "use my browser's login": only on the machine that runs the app
    def test_browser_login_is_only_allowed_for_local_requests(self):
        auth = {"mode": "browser", "browser": "firefox"}
        cases = [
            (dict(ALLOW_BROWSER_COOKIES=True), {}, 200),
            (dict(ALLOW_BROWSER_COOKIES=False), {}, 403),
            (dict(ALLOW_BROWSER_COOKIES=True), {"REMOTE_ADDR": "203.0.113.9"}, 403),
            (dict(ALLOW_BROWSER_COOKIES=True), {"HTTP_X_FORWARDED_FOR": "198.51.100.7"}, 403),  # behind a reverse proxy
            (dict(ALLOW_BROWSER_COOKIES=True, TRUSTED_PROXY_COUNT=1), {}, 403),
        ]
        for settings_, extra, expected in cases:
            cache.clear()
            with override_settings(**settings_):
                res, _ = self.metadata_with(auth, **extra)
            self.assertEqual(res.status_code, expected, (settings_, extra))
            if expected == 403:
                self.assertEqual(res.json()["code"], "not_local")

    @override_settings(ALLOW_BROWSER_COOKIES=True)
    def test_browser_login_reaches_ytdlp_for_the_preview_and_the_job(self):
        _, seen = self.metadata_with({"mode": "browser", "browser": "Firefox"})
        self.assertEqual(seen["auth"], YtAuth(browser="firefox"))
        calls = {}

        def download(url, preset, outdir, *, filename_stem, progress_callback, max_filesize, auth=None):
            calls["auth"] = auth
            out = Path(outdir) / f"{filename_stem}.mp4"
            out.write_bytes(b"x")
            return DownloadResult(out, "mp4", 1)

        with mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()), \
             mock.patch.object(ytdlp_service, "download_video", side_effect=download):
            res = post(Client(), "api_quick_download", {"url": URL, "preset": "best", "auth": {"mode": "browser", "browser": "brave"}})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(calls["auth"], YtAuth(browser="brave"))

    @override_settings(ALLOW_BROWSER_COOKIES=True)
    def test_browser_names_are_whitelisted(self):
        for bad in ("../../etc", "firefox; id", "", "netscape"):
            res, _ = self.metadata_with({"mode": "browser", "browser": bad})
            self.assertEqual((res.status_code, res.json()["code"]), (422, "bad_auth"), bad)

    def test_home_offers_browser_login_only_where_it_works(self):
        with override_settings(ALLOW_BROWSER_COOKIES=True):
            self.assertContains(self.client.get(reverse("home")), 'value="browser"')
            self.assertNotContains(self.client.get(reverse("home"), REMOTE_ADDR="203.0.113.9"), 'value="browser"')
        with override_settings(ALLOW_BROWSER_COOKIES=False):
            self.assertNotContains(self.client.get(reverse("home")), 'value="browser"')
        page = self.client.get(reverse("home")).content.decode()
        self.assertIn('value="cookies"', page)           # always available
        self.assertIn("Download on my own computer", page)

    # ---- the "run it on my own computer" command
    def test_command_endpoint(self):
        res = post(Client(), "api_quick_command", {"url": f"https://youtu.be/{VID}", "preset": "mp4_1080"})
        body = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertIn("height<=1080", body["command"])
        self.assertIn("--cookies-from-browser", body["command_with_login"])

    def test_command_endpoint_validates(self):
        self.assertEqual(post(Client(), "api_quick_command", {"url": "https://evil.com/x"}).status_code, 400)
        self.assertEqual(post(Client(), "api_quick_command", {"url": URL, "preset": "x; reboot"}).status_code, 422)
        self.assertEqual(Client().get(reverse("api_quick_command")).status_code, 405)
