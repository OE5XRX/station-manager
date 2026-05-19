from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _


class AppGrant(models.Model):
    """Grants a user access to a registered OIDC Application.

    The presence of an active (revoked_at IS NULL) row gates the
    OIDC authorization flow — without a row, the validator (T9)
    returns access_denied before any token is issued.

    Soft delete (revoked_at) preserves the audit trail: queries can
    show 'X had access until DATE' instead of losing the fact
    entirely. Re-granting after revoke is allowed by the partial
    unique index, which only enforces uniqueness on active rows.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="app_grants",
        verbose_name=_("user"),
    )
    application = models.ForeignKey(
        "oauth2_provider.Application",
        on_delete=models.CASCADE,
        related_name="grants",
        verbose_name=_("application"),
    )
    granted_at = models.DateTimeField(_("granted at"), auto_now_add=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_app_grants",
        verbose_name=_("granted by"),
    )
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)

    class Meta:
        verbose_name = _("app grant")
        verbose_name_plural = _("app grants")
        ordering = ("-granted_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["user", "application"],
                condition=Q(revoked_at__isnull=True),
                name="uniq_active_grant_per_user_per_app",
            ),
        ]
        indexes = [
            models.Index(fields=["application", "revoked_at"]),
        ]

    def __str__(self):
        return (
            f"{self.user} → {self.application} "
            f"({'active' if self.revoked_at is None else 'revoked'})"
        )


class SsoAuditLog(models.Model):
    """System-wide audit trail for SSO/OIDC events.

    Parallel to StationAuditLog (which is per-station). The
    apps/audit/ listing view merges both into a single feed —
    see apps/audit/views.py.
    """

    class EventType(models.TextChoices):
        APP_REGISTERED = "app_registered", _("App Registered")
        APP_DELETED = "app_deleted", _("App Deleted")
        GRANT_GIVEN = "grant_given", _("Grant Given")
        GRANT_REVOKED = "grant_revoked", _("Grant Revoked")
        LOGIN_SUCCESS = "login_success", _("Login Success")
        LOGIN_DENIED_NO_GRANT = "login_denied_no_grant", _("Login Denied — No Grant")
        LOGIN_DENIED_INACTIVE = "login_denied_inactive", _("Login Denied — Inactive User")
        TOKEN_REVOKED = "token_revoked", _("Token Revoked")

    event_type = models.CharField(
        _("event type"),
        max_length=32,
        choices=EventType.choices,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sso_audit_logs_as_actor",
        verbose_name=_("actor"),
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sso_audit_logs_as_target",
        verbose_name=_("target user"),
    )
    application = models.ForeignKey(
        "oauth2_provider.Application",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("application"),
    )
    message = models.TextField(_("message"), blank=True)
    ip_address = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("SSO audit log")
        verbose_name_plural = _("SSO audit logs")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["event_type", "-created_at"]),
            models.Index(fields=["target_user", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} @ {self.created_at}"

    @classmethod
    def log(cls, *, event_type, actor=None, target_user=None, application=None,
            message="", ip_address=None):
        """Convenience constructor. Mirrors StationAuditLog.log signature.

        Keyword-only so call sites can't accidentally swap positional args.
        """
        return cls.objects.create(
            event_type=event_type,
            actor=actor,
            target_user=target_user,
            application=application,
            message=message,
            ip_address=ip_address,
        )
