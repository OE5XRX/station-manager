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
