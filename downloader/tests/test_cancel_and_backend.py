"""Thread backend (no separate worker), cancelling downloads, honest status reporting."""
import json
import time
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse

from downloader.services import quick_service, ytdlp_service
from downloader.services.ytdlp_service import DownloadResult, DownloaderError, ProgressUpdate

from .base import URL, VID, AppTestCase, sample_info
from .test_quick import fake_download, post

TOKEN = "k" * 43


class SyncThread:
    """Stands in for threading.Thread so the 'background' job runs inline in tests."""

    started = 0

    def __init__(self, target, args=(), **kwargs):
        self.target, self.args = target, args
        self.daemon = kwargs.get("daemon")

    def start(self):
        SyncThread.started += 1
        self.target(*self.args)


def cancel(token=TOKEN, client=None):
    return (client or Client()).post(reverse("api_quick_cancel", args=[token]), data="{}", content_type="application/json")


def status(token=TOKEN):
    return Client().get(reverse("api_quick_status", args=[token]))


class ThreadBackendTests(AppTestCase):
    """Default backend: downloading starts with the app itself. No Celery, no worker, no ping."""

    @override_settings(DOWNLOAD_BACKEND="thread", CELERY_TASK_ALWAYS_EAGER=False)
    def test_download_runs_without_celery_or_a_worker(self):
        with mock.patch.object(quick_service.threading, "Thread", SyncThread), \
             mock.patch("downloader.tasks.quick_download_task.delay") as delay, \
             mock.patch("config.celery.app.control.ping") as ping, \
             mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()), \
             mock.patch.object(ytdlp_service, "download_video", side_effect=fake_download()):
            res = post(Client(), "api_quick_download", {"url": URL, "preset": "mp4_720"})
        self.assertEqual(res.status_code, 201)
        token = res.json()["video"]["id"]
        self.assertEqual(Client().get(reverse("api_quick_status", args=[token])).json()["video"]["status"], "completed")
        delay.assert_not_called()
        ping.assert_not_called()

    @override_settings(DOWNLOAD_BACKEND="thread")
    def test_job_is_started_in_a_daemon_thread(self):
        with mock.patch.object(quick_service.threading, "Thread") as thread:
            res = post(Client(), "api_quick_download", {"url": URL, "preset": "best"})
        self.assertEqual(res.status_code, 201)
        self.assertTrue(thread.call_args.kwargs["daemon"])
        thread.return_value.start.assert_called_once()

    @override_settings(DOWNLOAD_BACKEND="thread", CELERY_TASK_ALWAYS_EAGER=False)
    def test_thread_start_failure_frees_the_slot(self):
        with mock.patch.object(quick_service.threading, "Thread", side_effect=RuntimeError("can't start new thread")):
            res = post(Client(), "api_quick_download", {"url": URL, "preset": "best"})
        self.assertEqual(res.status_code, 503)
        self.assertEqual(cache.get(quick_service.SLOT_KEY), 0)

    @override_settings(DOWNLOAD_BACKEND="thread")
    def test_unreachable_cache_gives_a_clear_503_instead_of_a_crash(self):
        with mock.patch("django.core.cache.cache.add", side_effect=ConnectionError("Error 111 connecting to localhost:6379")):
            res = post(Client(), "api_quick_download", {"url": URL, "preset": "best"})
        self.assertEqual(res.status_code, 503)
        self.assertIn("temporarily unavailable", res.json()["error"])
        self.assertNotIn("6379", res.json()["error"])

    def test_celery_backend_still_available(self):
        with override_settings(DOWNLOAD_BACKEND="celery"), mock.patch("downloader.tasks.quick_download_task.delay") as delay:
            post(Client(), "api_quick_download", {"url": URL, "preset": "best"})
        delay.assert_called_once()


class StatusReportingTests(AppTestCase):
    def test_title_comes_from_the_preview_and_the_job_skips_a_second_lookup(self):
        with mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()) as extract:
            post(Client(), "api_quick_metadata", {"url": URL})          # the preview: one YouTube lookup
            with mock.patch.object(ytdlp_service, "download_video", side_effect=fake_download()):
                token = post(Client(), "api_quick_download", {"url": URL, "preset": "mp4_720"}).json()["video"]["id"]
        self.assertEqual(extract.call_count, 1)                          # not two
        video = status(token).json()["video"]
        self.assertEqual(video["status"], "completed")
        self.assertEqual(video["title"], "Building REST APIs with Django")
        self.assertEqual(video["filename"], f"Building REST APIs with Django [{VID}].mp4")

    def test_job_looks_the_video_up_itself_when_there_was_no_preview(self):
        with mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()) as extract, \
             mock.patch.object(ytdlp_service, "download_video", side_effect=fake_download()):
            token = post(Client(), "api_quick_download", {"url": URL, "preset": "mp4_720"}).json()["video"]["id"]
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(status(token).json()["video"]["title"], "Building REST APIs with Django")

    def test_status_stays_on_fetching_until_bytes_flow(self):
        seen = []
        original = quick_service._put

        def spy(token, state):
            seen.append(state["status"])
            return original(token, state)

        with mock.patch.object(quick_service, "_put", spy), \
             mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()), \
             mock.patch.object(ytdlp_service, "download_video", side_effect=fake_download()):
            post(Client(), "api_quick_download", {"url": URL, "preset": "mp4_720"})
        order = [s for i, s in enumerate(seen) if i == 0 or s != seen[i - 1]]
        self.assertEqual(order, ["pending", "fetching", "downloading", "completed"])

    def test_failure_before_any_bytes_is_reported_as_failed_while_fetching(self):
        err = DownloaderError("bot_check", "YouTube is blocking downloads from this server right now. Please try again later.")
        with mock.patch.object(ytdlp_service, "extract_video_info", side_effect=err):
            token = post(Client(), "api_quick_download", {"url": URL, "preset": "best"}).json()["video"]["id"]
        video = status(token).json()["video"]
        self.assertEqual((video["status"], video["failed_stage"]), ("failed", "fetching"))

    def test_failure_mid_download_is_reported_as_failed_while_downloading(self):
        def download(url, preset, outdir, *, filename_stem, progress_callback, max_filesize, auth=None):
            progress_callback(ProgressUpdate("downloading", 5, 10, 1.0, 1))
            raise DownloaderError("network", "Couldn't reach YouTube. Check the server's network connection and try again.")

        with mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()), \
             mock.patch.object(ytdlp_service, "download_video", side_effect=download):
            token = post(Client(), "api_quick_download", {"url": URL, "preset": "best"}).json()["video"]["id"]
        self.assertEqual(status(token).json()["video"]["failed_stage"], "downloading")

    @override_settings(QUEUE_TIMEOUT_SECONDS=30)
    def test_unclaimed_job_is_failed_while_pending(self):
        quick_service._put(TOKEN, {"status": "pending", "created": time.time() - 60})
        self.assertEqual(status().json()["video"]["failed_stage"], "pending")

    def test_cache_outage_does_not_break_the_youtube_helpers(self):
        raw = {"id": VID, "title": "T", "uploader": "U", "duration": 60, "upload_date": "20240501", "thumbnail": "", "formats": []}
        ydl = mock.MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.extract_info.return_value = raw
        with mock.patch("django.core.cache.cache.get", side_effect=ConnectionError("redis down")), \
             mock.patch("django.core.cache.cache.set", side_effect=ConnectionError("redis down")), \
             mock.patch("django.core.cache.cache.delete", side_effect=ConnectionError("redis down")), \
             mock.patch("yt_dlp.YoutubeDL", return_value=ydl):
            self.assertEqual(ytdlp_service.extract_video_info(URL).title, "T")


class CancelTests(AppTestCase):
    def start_pending(self):
        """Create a job that no worker has picked up yet."""
        with mock.patch.object(quick_service.secrets, "token_urlsafe", return_value=TOKEN), \
             mock.patch("downloader.tasks.quick_download_task.delay"):
            res = post(Client(), "api_quick_download", {"url": URL, "preset": "mp4_720"})
        self.assertEqual(res.status_code, 201)

    def test_cancel_a_job_that_has_not_started(self):
        self.start_pending()
        self.assertEqual(cache.get(quick_service.SLOT_KEY), 1)
        res = cancel()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["video"]["status"], "cancelled")
        self.assertEqual(cache.get(quick_service.SLOT_KEY), 0)  # capacity returned immediately
        # when the worker finally gets to it, it does nothing and doesn't free the slot twice
        with mock.patch.object(ytdlp_service, "extract_video_info", side_effect=AssertionError("must not run")), \
             mock.patch.object(ytdlp_service, "download_video", side_effect=AssertionError("must not run")):
            quick_service.run(TOKEN, URL, "mp4_720")
        self.assertEqual(status().json()["video"]["status"], "cancelled")
        self.assertEqual(cache.get(quick_service.SLOT_KEY), 0)

    def test_cancel_in_the_middle_of_a_download(self):
        seen = {}

        def download(url, preset, outdir, *, filename_stem, progress_callback, max_filesize, auth=None):
            (Path(outdir) / f"{filename_stem}.mp4.part").write_bytes(b"partial")
            progress_callback(ProgressUpdate("downloading", 5, 10, 1.0, 1))
            seen["response"] = cancel().json()["video"]          # the user clicks Cancel here
            progress_callback(ProgressUpdate("downloading", 6, 10, 1.0, 1))  # next tick must abort
            raise AssertionError("download should have been aborted")

        with mock.patch.object(quick_service.secrets, "token_urlsafe", return_value=TOKEN), \
             mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()), \
             mock.patch.object(ytdlp_service, "download_video", side_effect=download):
            post(Client(), "api_quick_download", {"url": URL, "preset": "mp4_720"})
        self.assertTrue(seen["response"]["cancelling"])          # UI can show "Cancelling…" immediately
        self.assertEqual(seen["response"]["status"], "downloading")
        video = status().json()["video"]
        self.assertEqual(video["status"], "cancelled")
        self.assertFalse(video["is_active"])
        self.assertFalse((quick_service.root_dir() / TOKEN).exists())  # partial files removed
        self.assertEqual(cache.get(quick_service.SLOT_KEY), 0)

    def test_cancel_while_ffmpeg_is_finishing_throws_the_file_away(self):
        def download(url, preset, outdir, *, filename_stem, progress_callback, max_filesize, auth=None):
            path = Path(outdir) / f"{filename_stem}.mp4"
            path.write_bytes(b"done")
            cancel()                                              # too late to interrupt, but not too late to discard
            return DownloadResult(path, "mp4", 4)

        with mock.patch.object(quick_service.secrets, "token_urlsafe", return_value=TOKEN), \
             mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()), \
             mock.patch.object(ytdlp_service, "download_video", side_effect=download):
            post(Client(), "api_quick_download", {"url": URL, "preset": "mp4_720"})
        self.assertEqual(status().json()["video"]["status"], "cancelled")
        self.assertFalse((quick_service.root_dir() / TOKEN).exists())

    def test_discarding_a_finished_but_unsaved_download_deletes_the_file(self):
        with mock.patch.object(quick_service.secrets, "token_urlsafe", return_value=TOKEN), \
             mock.patch.object(ytdlp_service, "extract_video_info", return_value=sample_info()), \
             mock.patch.object(ytdlp_service, "download_video", side_effect=fake_download()):
            post(Client(), "api_quick_download", {"url": URL, "preset": "mp4_720"})
        self.assertTrue((quick_service.root_dir() / TOKEN).exists())
        res = cancel()
        self.assertEqual((res.status_code, res.json()["discarded"]), (200, True))
        self.assertFalse((quick_service.root_dir() / TOKEN).exists())
        self.assertEqual(status().status_code, 404)

    def test_dismissing_a_failed_download_forgets_it(self):
        quick_service._put(TOKEN, {"status": "failed", "error": "x", "created": time.time()})
        self.assertTrue(cancel().json()["discarded"])
        self.assertEqual(status().status_code, 404)

    def test_cancel_unknown_job_is_404(self):
        self.assertEqual(cancel("z" * 43).status_code, 404)
        self.assertEqual(cancel("short").status_code, 404)

    def test_cancel_needs_post_and_csrf(self):
        self.assertEqual(Client().get(reverse("api_quick_cancel", args=[TOKEN])).status_code, 405)
        quick_service._put(TOKEN, {"status": "pending", "created": time.time()})
        strict = Client(enforce_csrf_checks=True)
        res = strict.post(reverse("api_quick_cancel", args=[TOKEN]), data="{}", content_type="application/json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(status().json()["video"]["status"], "pending")  # nothing happened

    def test_cancelled_jobs_close_the_live_stream(self):
        self.start_pending()
        cancel()
        res = Client().get(reverse("api_quick_events", args=[TOKEN]))
        chunks = list(res.streaming_content)                      # ends by itself: cancelled is terminal
        self.assertIn(b'"status":"cancelled"', b"".join(chunks))
