from django.contrib import admin

from .models import AppGrant


@admin.register(AppGrant)
class AppGrantAdmin(admin.ModelAdmin):
    list_display = ("user", "application", "granted_at", "granted_by", "revoked_at")
    list_filter = ("revoked_at", "application")
    search_fields = ("user__username", "user__email", "application__name")
    raw_id_fields = ("user", "granted_by")
    readonly_fields = ("granted_at",)
