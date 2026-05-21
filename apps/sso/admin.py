from django.contrib import admin, messages
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from oauth2_provider.admin import ApplicationAdmin as DefaultApplicationAdmin
from oauth2_provider.models import Application

from .models import AppGrant, SsoAuditLog


@admin.action(description=_("Revoke selected grants"))
def revoke_selected(modeladmin, request, queryset):
    """Bulk-revoke action: sets revoked_at=now() on active grants only.

    No-op on rows that are already revoked, so a re-run is idempotent.
    Iterates and calls ``.save()`` so the AppGrant pre_save/post_save
    handlers in ``apps.sso.signals`` fire — those cascade token
    revocation and write SsoAuditLog TOKEN_REVOKED entries. Switching
    to ``.update()`` here would silently leave tokens valid until
    their natural expiry, defeating the whole point of revoking.
    """
    now = timezone.now()
    n = 0
    for grant in queryset.filter(revoked_at__isnull=True):
        grant.revoked_at = now
        grant.save(update_fields=["revoked_at"])
        n += 1
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


@admin.register(SsoAuditLog)
class SsoAuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "event_type", "actor", "target_user", "application")
    list_filter = ("event_type", "application")
    search_fields = ("actor__username", "target_user__username", "message")
    readonly_fields = (
        "created_at", "event_type", "actor", "target_user", "application",
        "message", "ip_address",
    )
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False  # audit log entries only created programmatically

    def has_change_permission(self, request, obj=None):
        return False  # audit log is append-only

    def has_delete_permission(self, request, obj=None):
        return False  # append-only: even superusers must not delete


# --- Custom Application admin -----------------------------------------------
#
# DOT ships its own ApplicationAdmin. We unregister it and replace with a
# subclass that:
#   - Adds an `active_grants` derived column to the list view (operator's
#     quick proxy for "is this app actually in use?")
#   - Makes client_secret read-only after creation, preventing an admin
#     from rotating it via the change form and breaking every RP at once.
#
# Operators who DO want to rotate a secret should delete the Application
# and re-create — that forces them to also re-issue grants, which is
# usually what they actually want.


class CustomApplicationAdmin(DefaultApplicationAdmin):
    """ApplicationAdmin override with grant count + locked client_secret."""

    list_display = ("name", "client_id", "client_type", "active_grants", "created")

    def get_queryset(self, request):
        # Annotate the active-grant count once via a JOIN + GROUP BY
        # instead of doing one COUNT query per row in the changelist
        # (N+1 as the number of registered applications grows).
        return (
            super()
            .get_queryset(request)
            .annotate(
                _active_grants=Count(
                    "grants",
                    filter=Q(grants__revoked_at__isnull=True),
                ),
            )
        )

    @admin.display(description="Active grants", ordering="_active_grants")
    def active_grants(self, obj):
        return obj._active_grants

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        if obj is not None and "client_secret" not in ro:
            ro.append("client_secret")
        return ro


# Re-register so our subclass replaces the default.
#
# Wrap the unregister in try/except admin.sites.NotRegistered so a
# future DOT release that ships without auto-registering Application
# (or another app that unregistered it first via INSTALLED_APPS
# ordering) doesn't crash Django startup. The register() call below
# is still required either way.
try:
    admin.site.unregister(Application)
except admin.sites.NotRegistered:
    pass
admin.site.register(Application, CustomApplicationAdmin)
