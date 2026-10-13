from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # NOTE: MEDIA_URL is intentionally NOT routed. Media is only served through
    # authenticated, ownership-checked views in `downloader.views.media`.
    path("", include("downloader.urls")),
]
