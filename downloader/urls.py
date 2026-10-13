from django.contrib.auth.views import LogoutView
from django.urls import path

from .views import auth, playback, quick

urlpatterns = [
    # Auth
    path("accounts/login/", auth.StyledLoginView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),  # POST only (Django 5)
    path("accounts/register/", auth.register, name="register"),
    # Pages (all public except settings)
    path("", quick.home, name="home"),
    path("watch/", playback.watch, name="watch"),
    path("continue-watching/", playback.continue_watching, name="continue_watching"),
    path("settings/", playback.settings_page, name="settings"),
    # Downloads: job state is cache-only, files are temporary
    path("api/quick/metadata/", quick.quick_metadata, name="api_quick_metadata"),
    path("api/quick/download/", quick.quick_start, name="api_quick_download"),
    path("api/quick/<str:token>/status/", quick.quick_status, name="api_quick_status"),
    path("api/quick/<str:token>/events/", quick.quick_events, name="api_quick_events"),
    path("quick/<str:token>/file/", quick.quick_file, name="quick_file"),
    # Playback positions (accounts only; anonymous visitors use their browser's localStorage)
    path("api/playback/", playback.playback_list, name="api_playback_list"),
    path("api/playback/<str:key>/", playback.playback_detail, name="api_playback_detail"),
]
