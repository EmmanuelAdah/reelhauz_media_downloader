"""Downloads work without login, keep no database records, and report live status."""
import json
import os
import time
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse

from downloader.models import PlaybackPosition, UserPreference
from downloader.services import quick_service, ytdlp_service
from downloader.services.ytdlp_service import DownloadResult, DownloaderError, ProgressUpdate

from .base import URL, VID, AppTestCase, sample_info


def post(client, name, data, **kw):
    return client.post(reverse(name), data=json.dumps(data), content_type="application/json", **kw)


def fake_download(content=b"downloaded-bytes", ext="mp4"):
    def _download(url, preset, outdir, *, filename_stem, progress_callback, max_filesize, auth=None):
        progress_callback(ProgressUpdate("downloading", 5, 10, 1.0, 1))
        path = Path(outdir) / f"{filename_stem}.{ext}"
        path.write_bytes(content)
        return DownloadResult(path, ext, len(content))

    return _download


def events(response, limit=50):
    """Parse an SSE response into a list of (event, data) tuples."""
    out, buf = [], b""
    for chunk in response.streaming_content:
        buf += chunk
        while b"\n\n" in buf:
            raw, buf = buf.split(b"\n\n", 1)
            lines = raw.decode().splitlines()
            ev = next((l[7:] for l in lines if l.startswith("event: ")), "message")
            data = next((l[6:] for l in lines if l.startswith("data: ")), None)
            if data is not None:
                out.append((ev, json.loads(data)))
        if len(out) >= limit:
            break
    return out


class DownloadFlowTests(AppTestCase):
    def assert_nothing_persisted(self):
        self.assertEqual(PlaybackPosition.objects.count(), 0)
        self.assertEqual(UserPreference.objects.count(), 0)
        self.assertEqual(Session.objects.count(), 0)
        self.assertEqual(get_user_model().objects.count(), 2)

    def run_flow(self, client=None):
        client = client or Client()
        with mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()), \
             mock.patch.object(ytdlp_service, "download_video", side_effect=fake_download()):
            return client, post(client, "api_quick_download", {"url": URL, "preset": "mp4_720"})

    # ---- metadata ---------------------------------------------------------
    def test_metadata_without_login(self):
        with mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()):
            res = post(Client(), "api_quick_metadata", {"url": URL})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["video"]["title"], "Building REST APIs with Django")
        self.assertEqual(body["video"]["duration_display"], "18:42")
        self.assertIn("mp4_1080", [f["key"] for f in body["formats"]])
        self.assertEqual(res["Cache-Control"], "no-store")
        self.assert_nothing_persisted()

    def test_metadata_validates_url(self):
        self.assertEqual(post(Client(), "api_quick_metadata", {"url": "https://vimeo.com/1"}).status_code, 400)

    def test_metadata_translates_service_errors(self):
        err = DownloaderError("private", "This video is private and can't be downloaded.")
        with mock.patch.object(ytdlp_service, "extract_video_info", side_effect=err):
            res = post(Client(), "api_quick_metadata", {"url": URL})
        self.assertEqual((res.status_code, res.json()["code"]), (422, "private"))

    @override_settings(MAX_DOWNLOAD_SIZE_MB=20)
    def test_formats_over_size_cap_are_flagged(self):
        with mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()):
            formats = {f["key"]: f for f in post(Client(), "api_quick_metadata", {"url": URL}).json()["formats"]}
        self.assertTrue(formats["mp4_1080"]["disabled"])
        self.assertFalse(formats["mp4_360"]["disabled"])

    def test_member_default_preset_is_used(self):
        UserPreference.objects.create(user=self.alice, default_preset="audio_m4a")
        self.login()
        with mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()):
            body = post(self.client, "api_quick_metadata", {"url": URL}).json()
        self.assertEqual(body["default_preset"], "audio_m4a")

    # ---- full flow --------------------------------------------------------
    def test_full_flow_without_login(self):
        client, res = self.run_flow()
        self.assertEqual(res.status_code, 201)
        token = res.json()["video"]["id"]
        status = client.get(reverse("api_quick_status", args=[token])).json()["video"]
        self.assertEqual(status["status"], "completed")  # Celery runs eagerly in tests
        self.assertEqual(status["filename"], f"Building REST APIs with Django [{VID}].mp4")
        self.assertEqual(status["video_id"], VID)  # lets the browser key the remembered file location
        self.assertNotIn(str(settings.DOWNLOAD_TMP_DIR), json.dumps(status))

        file_res = client.get(status["file_url"])
        self.assertEqual(file_res.status_code, 200)
        self.assertEqual(b"".join(file_res.streaming_content), b"downloaded-bytes")
        self.assertIn("attachment", file_res["Content-Disposition"])
        self.assertIn(f"[{VID}].mp4", file_res["Content-Disposition"])
        self.assertEqual(file_res["Content-Length"], str(len(b"downloaded-bytes")))

        # nothing remains on the server after delivery
        self.assertFalse((quick_service.root_dir() / token).exists())
        self.assertIsNone(cache.get(f"transfer:{token}"))
        self.assertEqual(client.get(status["file_url"]).status_code, 404)
        self.assert_nothing_persisted()

    def test_interrupted_transfer_keeps_file_for_retry(self):
        client, res = self.run_flow()
        token = res.json()["video"]["id"]
        file_res = client.get(reverse("quick_file", args=[token]))
        next(iter(file_res.streaming_content))
        file_res.close()  # client went away mid-transfer
        self.assertTrue((quick_service.root_dir() / token).exists())
        self.assertEqual(client.get(reverse("quick_file", args=[token])).status_code, 200)

    def test_failure_reported_and_cleaned_up(self):
        err = DownloaderError("private", "This video is private and can't be downloaded.")
        client = Client()
        with mock.patch.object(ytdlp_service, "extract_video_info", side_effect=err):
            token = post(client, "api_quick_download", {"url": URL, "preset": "best"}).json()["video"]["id"]
        status = client.get(reverse("api_quick_status", args=[token])).json()["video"]
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error_message"], "This video is private and can't be downloaded.")
        self.assertFalse((quick_service.root_dir() / token).exists())

    def test_unexpected_error_does_not_leak_details(self):
        client = Client()
        with mock.patch.object(ytdlp_service, "extract_video_info", side_effect=RuntimeError("/etc/secret exploded")):
            token = post(client, "api_quick_download", {"url": URL, "preset": "best"}).json()["video"]["id"]
        status = client.get(reverse("api_quick_status", args=[token])).json()["video"]
        self.assertEqual(status["status"], "failed")
        self.assertNotIn("secret", status["error_message"])

    def test_size_cap_is_passed_to_ytdlp(self):
        with mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()), \
             mock.patch.object(ytdlp_service, "download_video", side_effect=fake_download()) as dl, \
             override_settings(MAX_DOWNLOAD_SIZE_MB=10):
            post(Client(), "api_quick_download", {"url": URL, "preset": "best"})
        self.assertEqual(dl.call_args.kwargs["max_filesize"], 10 * 1024 * 1024)

    def test_status_transitions_follow_the_documented_flow(self):
        seen = []
        original = quick_service._put

        def spy(token, state):
            if not seen or seen[-1] != state["status"]:
                seen.append(state["status"])
            return original(token, state)

        def download(url, preset, outdir, *, filename_stem, progress_callback, max_filesize, auth=None):
            progress_callback(ProgressUpdate("downloading", 5, 10, 1.0, 1))
            progress_callback(ProgressUpdate("processing"))
            path = Path(outdir) / f"{filename_stem}.mp4"
            path.write_bytes(b"x")
            return DownloadResult(path, "mp4", 1)

        with mock.patch.object(quick_service, "_put", spy), \
             mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()), \
             mock.patch.object(ytdlp_service, "download_video", side_effect=download):
            post(Client(), "api_quick_download", {"url": URL, "preset": "best"})
        self.assertEqual(seen, ["pending", "fetching", "downloading", "processing", "completed"])

    def test_input_validation_does_not_queue(self):
        with mock.patch("downloader.tasks.quick_download_task.delay") as delay:
            for body in ({"url": "https://evil.com", "preset": "best"}, {"url": URL, "preset": "bv*; rm -rf /"}, {"url": URL}):
                self.assertIn(post(Client(), "api_quick_download", body).status_code, (400, 422), body)
        delay.assert_not_called()

    # ---- live events (Server-Sent Events) ---------------------------------
    def test_event_stream_pushes_each_status_until_done(self):
        token = "t" * 43
        quick_service._put(token, {"status": "pending", "created": time.time(), "title": ""})
        res = Client().get(reverse("api_quick_events", args=[token]))
        self.assertEqual(res["Content-Type"], "text/event-stream")
        self.assertEqual(res["X-Accel-Buffering"], "no")

        steps = [
            {"status": "fetching"},
            {"status": "downloading", "percent": 40.0, "downloaded_bytes": 4, "total_bytes": 10, "speed": 2.0, "eta": 3, "title": "T"},
            {"status": "processing", "percent": 100.0},
            {"status": "completed", "percent": 100.0, "file": "f.mp4", "filename": "T [x].mp4", "file_size": 10},
        ]
        it = iter(res.streaming_content)
        self.assertEqual(next(it), b"retry: 2000\n\n")
        got = []
        with mock.patch("downloader.views.quick.time.sleep", lambda s: None):
            first = next(it)
            got.append(json.loads(first.decode().split("data: ", 1)[1]))
            for patch in steps:
                state = quick_service.get_state(token)
                state.update(patch)
                quick_service._put(token, state)
                chunk = next(it)
                got.append(json.loads(chunk.decode().split("data: ", 1)[1]))
            with self.assertRaises(StopIteration):  # terminal state closes the stream
                next(it)
        self.assertEqual([g["status"] for g in got], ["pending", "fetching", "downloading", "processing", "completed"])
        self.assertEqual(got[2]["percent"], 40.0)
        self.assertEqual(got[2]["speed_display"], "2 B/s")
        self.assertTrue(got[-1]["file_url"].endswith(f"/quick/{token}/file/"))

    def test_event_stream_for_unknown_token_is_404(self):
        self.assertEqual(Client().get(f"/api/quick/{'z' * 43}/events/").status_code, 404)
        self.assertEqual(Client().get("/api/quick/short/events/").status_code, 404)

    def test_event_stream_announces_expiry(self):
        token = "e" * 43
        quick_service._put(token, {"status": "pending", "created": time.time()})
        res = Client().get(reverse("api_quick_events", args=[token]))
        it = iter(res.streaming_content)
        next(it)
        next(it)  # pending
        cache.delete(f"transfer:{token}")
        with mock.patch("downloader.views.quick.time.sleep", lambda s: None):
            self.assertIn(b"event: gone", next(it))

    # ---- never leave the UI stuck on "Pending" -----------------------------
    @override_settings(QUEUE_TIMEOUT_SECONDS=30)
    def test_unclaimed_jobs_fail_instead_of_pending_forever(self):
        token = "s" * 43
        quick_service._put(token, {"status": "pending", "created": time.time() - 60})
        status = Client().get(reverse("api_quick_status", args=[token])).json()["video"]
        self.assertEqual(status["status"], "failed")
        self.assertIn("No download worker", status["error_message"])

    def test_waiting_notice_shown_when_queue_is_slow(self):
        token = "w" * 43
        quick_service._put(token, {"status": "pending", "created": time.time() - 20})
        status = Client().get(reverse("api_quick_status", args=[token])).json()["video"]
        self.assertEqual(status["status"], "pending")
        self.assertIn("Waiting", status["notice"])

    def test_start_refuses_cleanly_when_no_worker_is_running(self):
        with override_settings(CELERY_TASK_ALWAYS_EAGER=False), \
             mock.patch("config.celery.app.control.ping", return_value=[]), \
             mock.patch("downloader.tasks.quick_download_task.delay") as delay:
            res = post(Client(), "api_quick_download", {"url": URL, "preset": "best"})
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["code"], "no_worker")
        delay.assert_not_called()

    def test_start_proceeds_when_a_worker_answers(self):
        with override_settings(CELERY_TASK_ALWAYS_EAGER=False), \
             mock.patch("config.celery.app.control.ping", return_value=[{"worker@host": {"ok": "pong"}}]), \
             mock.patch("downloader.tasks.quick_download_task.delay") as delay:
            res = post(Client(), "api_quick_download", {"url": URL, "preset": "best"})
        self.assertEqual(res.status_code, 201)
        delay.assert_called_once()

    def test_queue_failure_returns_503_and_frees_slot(self):
        with mock.patch("downloader.tasks.quick_download_task.delay", side_effect=ConnectionError("redis down")):
            res = post(Client(), "api_quick_download", {"url": URL, "preset": "best"})
        self.assertEqual(res.status_code, 503)
        self.assertEqual(cache.get(quick_service.SLOT_KEY), 0)

    # ---- abuse controls -----------------------------------------------------
    @override_settings(MAX_CONCURRENT_DOWNLOADS=1)
    def test_server_wide_concurrency_cap(self):
        with mock.patch("downloader.tasks.quick_download_task.delay"):
            first = post(Client(), "api_quick_download", {"url": URL, "preset": "best"})
            second = post(Client(), "api_quick_download", {"url": URL, "preset": "best"}, REMOTE_ADDR="10.9.9.9")
        self.assertEqual((first.status_code, second.status_code), (201, 503))
        self.assertEqual(second.json()["code"], "busy")

    @override_settings(RATE_LIMIT_DOWNLOAD=(2, 60))
    def test_rate_limit_is_per_ip(self):
        with mock.patch("downloader.tasks.quick_download_task.delay"):
            codes = [post(Client(), "api_quick_download", {"url": URL, "preset": "best"}, REMOTE_ADDR="10.0.0.1").status_code for _ in range(4)]
            other = post(Client(), "api_quick_download", {"url": URL, "preset": "best"}, REMOTE_ADDR="10.0.0.2").status_code
        self.assertEqual(codes, [201, 201, 429, 429])
        self.assertEqual(other, 201)

    @override_settings(TRUSTED_PROXY_COUNT=1, RATE_LIMIT_DOWNLOAD=(1, 60))
    def test_forwarded_for_uses_the_proxy_appended_entry_only(self):
        with mock.patch("downloader.tasks.quick_download_task.delay"):
            codes = [
                post(Client(), "api_quick_download", {"url": URL, "preset": "best"}, HTTP_X_FORWARDED_FOR=f"{spoof}, 9.9.9.9").status_code
                for spoof in ("1.1.1.1", "2.2.2.2", "3.3.3.3")
            ]
        self.assertEqual(codes, [201, 429, 429])  # a spoofed prefix can't dodge the limit

    def test_untrusted_forwarded_for_is_ignored(self):
        with override_settings(RATE_LIMIT_DOWNLOAD=(1, 60)), mock.patch("downloader.tasks.quick_download_task.delay"):
            codes = [
                post(Client(), "api_quick_download", {"url": URL, "preset": "best"}, HTTP_X_FORWARDED_FOR=ip, REMOTE_ADDR="10.7.7.7").status_code
                for ip in ("5.5.5.5", "6.6.6.6")
            ]
        self.assertEqual(codes, [201, 429])

    def test_csrf_enforced(self):
        strict = Client(enforce_csrf_checks=True)
        res = strict.post(reverse("api_quick_metadata"), data=json.dumps({"url": URL}), content_type="application/json")
        self.assertEqual(res.status_code, 403)

    def test_tokens_are_validated(self):
        for bad in ("short", "../../etc/passwd", "a" * 44):
            self.assertEqual(Client().get(f"/api/quick/{bad}/status/").status_code, 404, bad)
        self.assertIsNone(quick_service.file_path("x" * 43))
        quick_service.discard("../../etc")  # no-op

    # ---- cleanup -------------------------------------------------------------
    def test_sweep_removes_only_stale_directories(self):
        old, fresh = quick_service.root_dir() / ("o" * 43), quick_service.root_dir() / ("f" * 43)
        for d in (old, fresh):
            d.mkdir(parents=True, exist_ok=True)
            (d / "x.mp4").write_bytes(b"x")
        stale = time.time() - settings.TRANSFER_TTL_SECONDS - 100
        os.utime(old / "x.mp4", (stale, stale))
        os.utime(old, (stale, stale))
        self.assertEqual(quick_service.sweep(force=True), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_cleanup_command(self):
        out = StringIO()
        call_command("cleanup_transfers", stdout=out)
        self.assertIn("Removed", out.getvalue())
