from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
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
        SESSION_REVOKED = "session_revoked", _("Session Revoked (admin)")
        APP_POLICY_CHANGED = "app_policy_changed", _("App Policy Changed")
        GROUP_MEMBERSHIP_CHANGED = "group_membership_changed", _("Group Membership Changed")

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
    def log(
        cls,
        *,
        event_type,
        actor=None,
        target_user=None,
        application=None,
        message="",
        ip_address=None,
    ):
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


class ApplicationPolicy(models.Model):
    """Per-App access policy. 1:1 zu DOT's Application.

    Wenn keine Row existiert -> Policy ist implizit GRANT_REQUIRED
    (Spec §3.1).
    """

    class AccessPolicy(models.TextChoices):
        GRANT_REQUIRED = "grant_required", _("Grant required (default)")
        OPEN_TO_ALL = "open_to_all", _("Open to all (incl. applicants)")
        OPEN_TO_MEMBERS = "open_to_members", _("Open to members and above")
        OPEN_TO_INTERNAL = "open_to_internal", _("Open to staff and admins")
        OPEN_TO_ADMINS = "open_to_admins", _("Open to admins only")

    application = models.OneToOneField(
        "oauth2_provider.Application",
        on_delete=models.CASCADE,
        related_name="sso_policy",
        verbose_name=_("application"),
    )
    access_policy = models.CharField(
        _("access policy"),
        max_length=32,
        choices=AccessPolicy.choices,
        default=AccessPolicy.GRANT_REQUIRED,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)
    modified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="modified_app_policies",
        verbose_name=_("modified by"),
    )

    class Meta:
        verbose_name = _("application policy")
        verbose_name_plural = _("application policies")

    def __str__(self):
        return f"{self.application.name} -> {self.get_access_policy_display()}"


class TokenSessionQuerySet(models.QuerySet):
    """Custom queryset so the same "active" criteria are reused across
    the validator hook, audit-log counters, and admin UI helpers.

    Matches the predicate of ``TokenSession.is_active`` at the DB level:
        revoked_at IS NULL
        AND refresh_token IS NOT NULL
        AND refresh_token.revoked IS NULL
        AND issued_at + REFRESH_TOKEN_EXPIRE_SECONDS > now
    """

    def active(self):
        from django.conf import settings as dj_settings
        from django.utils import timezone

        lifetime_seconds = dj_settings.OAUTH2_PROVIDER.get(
            "REFRESH_TOKEN_EXPIRE_SECONDS", 14 * 24 * 3600
        )
        lifetime_cutoff = timezone.now() - timedelta(seconds=lifetime_seconds)
        return self.filter(
            revoked_at__isnull=True,
            refresh_token__isnull=False,
            refresh_token__revoked__isnull=True,
            issued_at__gt=lifetime_cutoff,
        )


class TokenSession(models.Model):
    """1:1 zu jeder RefreshToken-Issuance (inkl. Rotations-Chain).

    Spec §4.1.
    """

    objects = TokenSessionQuerySet.as_manager()

    class RevokeReason(models.TextChoices):
        ADMIN_REVOKE = "admin_revoke", _("Admin revoke")
        USER_LOGOUT = "user_logout", _("User logout")
        USER_DEACTIVATED = "user_deactivated", _("User deactivated")
        GRANT_REVOKED = "grant_revoked", _("Grant revoked")
        ROTATED = "rotated", _("Rotated (refresh)")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="token_sessions",
        verbose_name=_("user"),
    )
    application = models.ForeignKey(
        "oauth2_provider.Application",
        on_delete=models.CASCADE,
        related_name="token_sessions",
        verbose_name=_("application"),
    )
    refresh_token = models.OneToOneField(
        "oauth2_provider.RefreshToken",
        on_delete=models.CASCADE,
        related_name="sso_session",
        null=True,
        blank=True,
        verbose_name=_("refresh token"),
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
        verbose_name=_("parent session"),
    )

    ip_address = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    user_agent = models.CharField(_("user agent"), max_length=512, blank=True)
    country_code = models.CharField(_("country code"), max_length=2, blank=True)
    city = models.CharField(_("city"), max_length=100, blank=True)

    issued_at = models.DateTimeField(_("issued at"), auto_now_add=True)
    last_seen_at = models.DateTimeField(_("last seen at"), auto_now_add=True)

    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="revoked_sessions",
        verbose_name=_("revoked by"),
    )
    revoke_reason = models.CharField(
        _("revoke reason"),
        max_length=32,
        choices=RevokeReason.choices,
        blank=True,
    )

    class Meta:
        verbose_name = _("token session")
        verbose_name_plural = _("token sessions")
        ordering = ("-issued_at",)
        indexes = [
            models.Index(fields=["user", "-issued_at"]),
            models.Index(fields=["application", "-issued_at"]),
            models.Index(fields=["revoked_at"]),
        ]

    @property
    def is_active(self) -> bool:
        """Lebende Session: nicht revoked, RefreshToken intakt, nicht
        ueber die Refresh-Lifetime hinaus."""
        if self.revoked_at is not None:
            return False
        rt = self.refresh_token
        if rt is None or rt.revoked is not None:
            return False
        max_lifetime = timedelta(
            seconds=settings.OAUTH2_PROVIDER.get("REFRESH_TOKEN_EXPIRE_SECONDS", 14 * 24 * 3600)
        )
        return self.issued_at + max_lifetime > timezone.now()

    def __str__(self):
        # Match the full is_active predicate (refresh-token present and
        # not revoked, within refresh-lifetime). Sessions where the
        # refresh-token aged past the lifetime would otherwise be
        # misleadingly labeled "active" in admin/log output.
        if self.is_active:
            status = "active"
        elif self.revoked_at:
            status = "revoked"
        else:
            status = "expired"
        return f"{self.user} → {self.application} ({status})"
