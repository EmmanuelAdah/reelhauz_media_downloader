import json

from django.urls import reverse

from downloader.models import PlaybackPosition

from .base import VID, AppTestCase


def post(client, key, data):
    return client.post(reverse("api_playback_detail", args=[key]), data=json.dumps(data), content_type="application/json")


class PlaybackApiTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("api_playback_list")).status_code, 401)
        self.assertEqual(post(self.client, VID, {"position": 1}).status_code, 401)

    def test_save_then_restore_position(self):
        res = post(self.client, VID, {"position": 754, "duration": 1122, "title": "My video"})
        self.assertEqual(res.status_code, 200)
        item = self.client.get(reverse("api_playback_detail", args=[VID])).json()["item"]
        self.assertEqual(item["position"], 754)
        self.assertEqual(item["position_display"], "12:34")
        self.assertEqual(item["title"], "My video")
        self.assertIn(VID, item["thumbnail_url"])

    def test_upsert_keeps_one_row_and_title(self):
        post(self.client, VID, {"position": 10, "duration": 100, "title": "Keep me"})
        post(self.client, VID, {"position": 50, "duration": 100})
        self.assertEqual(PlaybackPosition.objects.count(), 1)
        entry = PlaybackPosition.objects.get()
        self.assertEqual((entry.position, entry.title), (50, "Keep me"))

    def test_position_clamped_and_validated(self):
        post(self.client, VID, {"position": 5000, "duration": 100})
        self.assertEqual(PlaybackPosition.objects.get().position, 100)
        for bad in ({"position": "x"}, {"position": -5}, {"position": 1, "duration": -1}, {}):
            self.assertEqual(post(self.client, VID, bad).status_code, 400, bad)
        self.assertEqual(post(self.client, "bad key!", {"position": 1}).status_code, 400)

    def test_users_cannot_see_each_others_positions(self):
        PlaybackPosition.objects.create(user=self.bob, media_key=VID, position=99, duration=200, title="Bob's")
        self.assertIsNone(self.client.get(reverse("api_playback_detail", args=[VID])).json()["item"])
        self.assertEqual(self.client.get(reverse("api_playback_list")).json()["results"], [])
        self.client.delete(reverse("api_playback_detail", args=[VID]))
        self.assertEqual(PlaybackPosition.objects.filter(user=self.bob).count(), 1)

    def test_list_delete_one_and_clear_all(self):
        post(self.client, VID, {"position": 10, "duration": 100})
        post(self.client, "local-abc123", {"position": 20, "duration": 100, "title": "Local"})
        results = self.client.get(reverse("api_playback_list")).json()["results"]
        self.assertEqual([r["key"] for r in results], ["local-abc123", VID])  # newest first
        self.assertEqual(results[0]["thumbnail_url"], "")  # no YouTube id, no thumbnail
        self.client.delete(reverse("api_playback_detail", args=[VID]))
        self.assertEqual(PlaybackPosition.objects.count(), 1)
        PlaybackPosition.objects.create(user=self.bob, media_key="b" * 11, position=1)
        self.client.delete(reverse("api_playback_list"))
        self.assertEqual(PlaybackPosition.objects.filter(user=self.alice).count(), 0)
        self.assertEqual(PlaybackPosition.objects.filter(user=self.bob).count(), 1)

    def test_finished_flag(self):
        post(self.client, VID, {"position": 97, "duration": 100})
        self.assertTrue(self.client.get(reverse("api_playback_list")).json()["results"][0]["finished"])

    def test_only_positions_are_stored_no_media_fields(self):
        names = {f.name for f in PlaybackPosition._meta.get_fields()}
        self.assertFalse(names & {"video_file", "file", "thumbnail", "source_url", "file_size"})


class PagesTests(AppTestCase):
    def test_continue_watching_page_flags_auth_mode(self):
        self.assertContains(self.client.get(reverse("continue_watching")), 'data-authenticated="false"')
        self.assertContains(self.client.get(reverse("continue_watching")), "Saved in this browser only")
        self.login()
        self.assertContains(self.client.get(reverse("continue_watching")), 'data-authenticated="true"')

    def test_watch_page_has_player_and_picker(self):
        res = self.client.get(reverse("watch"))
        self.assertContains(res, 'id="file-input"')
        self.assertContains(res, 'id="player"')

    def test_settings_clear_history_button_reflects_count(self):
        self.login()
        self.assertContains(self.client.get(reverse("settings")), "0 saved")
        PlaybackPosition.objects.create(user=self.alice, media_key=VID, position=5, duration=10)
        self.assertContains(self.client.get(reverse("settings")), "1 saved")
