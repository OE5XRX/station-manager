from django.contrib import admin, messages
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import AppGrant


@admin.action(description=_("Revoke selected grants"))
def revoke_selected(modeladmin, request, queryset):
    """Bulk-revoke action: sets revoked_at=now() on active grants only.

    No-op on rows that are already revoked, so a re-run is idempotent.
    """
    n = queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
    modeladmin.message_user(
        request,
        _("Revoked %(n)d grant(s).") % {"n": n},
        level=messages.SUCCESS,
    )


@admin.register(AppGrant)
class AppGrantAdmin(admin.ModelAdmin):
    list_display = ("user", "application", "granted_at", "granted_by", "revoked_at")
    list_filter = ("revoked_at", "application")
    search_fields = ("user__username", "user__email", "application__name")
    raw_id_fields = ("user", "granted_by")
    readonly_fields = ("granted_at", "revoked_at")
    actions = [revoke_selected]
