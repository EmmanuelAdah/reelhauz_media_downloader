from __future__ import annotations

import shutil
import tempfile
from datetime import date
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from downloader.services.ytdlp_service import VideoInfo

User = get_user_model()
VID = "dQw4w9WgXcQ"
URL = f"https://www.youtube.com/watch?v={VID}"

_TMP = Path(tempfile.mkdtemp(prefix="rh_test_tmp_"))


def sample_info(video_id: str = VID) -> VideoInfo:
    return VideoInfo(
        video_id=video_id,
        title="Building REST APIs with Django",
        description="A tutorial.",
        uploader="Code Channel",
        duration=1122,
        upload_date=date(2024, 5, 1),
        thumbnail_url=f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
        webpage_url=f"https://www.youtube.com/watch?v={video_id}",
        formats=[
            {"format_id": "137", "ext": "mp4", "height": 1080, "vcodec": "avc1", "acodec": "none", "filesize": 80_000_000},
            {"format_id": "136", "ext": "mp4", "height": 720, "vcodec": "avc1", "acodec": "none", "filesize": 40_000_000},
            {"format_id": "134", "ext": "mp4", "height": 360, "vcodec": "avc1", "acodec": "none", "filesize": 10_000_000},
            {"format_id": "140", "ext": "m4a", "height": None, "vcodec": "none", "acodec": "mp4a", "filesize": 5_000_000},
        ],
    )


@override_settings(DOWNLOAD_TMP_DIR=_TMP)
class AppTestCase(TestCase):
    password = "S3cure-passphrase-42"

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_TMP, ignore_errors=True)

    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user("alice", password=self.password)
        self.bob = User.objects.create_user("bob", password=self.password)

    def login(self, user=None):
        self.client.force_login(user or self.alice)
