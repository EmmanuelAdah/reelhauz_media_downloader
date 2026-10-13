from django.contrib import admin

from .models import PlaybackPosition, UserPreference


@admin.register(PlaybackPosition)
class PlaybackPositionAdmin(admin.ModelAdmin):
    list_display = ("user", "title", "media_key", "position", "duration", "updated_at")
    search_fields = ("user__username", "title", "media_key")
    list_select_related = ("user",)


@admin.register(UserPreference)
class UserPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "default_preset")
