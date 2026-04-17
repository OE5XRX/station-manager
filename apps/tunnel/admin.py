from django.contrib import admin

from .models import TerminalSession


@admin.register(TerminalSession)
class TerminalSessionAdmin(admin.ModelAdmin):
    list_display = ("station", "user", "status", "started_at", "ended_at")
    list_filter = ("status", "station")
    search_fields = ("station__name", "station__callsign", "user__username")
    readonly_fields = (
        "station",
        "user",
        "status",
        "started_at",
        "ended_at",
        "close_reason",
    )
