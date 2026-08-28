from django.contrib import admin

from .models import PushSubscription


@admin.register(PushSubscription)
class PushSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "label", "endpoint", "last_success_at", "failure_count")
    search_fields = ("user__username", "endpoint", "label")
    readonly_fields = ("created_at", "last_success_at")
