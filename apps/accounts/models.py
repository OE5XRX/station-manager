import re
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.validators import RegexValidator
from django.db import models
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from .managers import UserManager

# Maidenhead 6-character grid locator: 2 letters (field, A-R) + 2 digits
# (square, 0-9) + 2 letters (subsquare, A-X). The Maidenhead system is
# defined for amateur radio location reporting.
LOCATOR_REGEX = re.compile(r"^[A-R]{2}[0-9]{2}[A-X]{2}\Z")

locator_validator = RegexValidator(
    regex=LOCATOR_REGEX,
    message=_("Maidenhead locator must be 2 letters + 2 digits + 2 letters (e.g. JN78AB)."),
)


def avatar_upload_path(instance, filename):
    """Per-user randomised storage path: avatars/<user_id>/<random>.<ext>.

    Each upload produces a fresh path — old files become orphaned but
    are not auto-cleaned (Cleanup-Job out-of-scope; siehe Overview Sektion 7).
    Using a random suffix means re-uploading the same file twice doesn't
    overwrite (and doesn't break browser caching for the old URL).
    """
    ext = Path(filename).suffix.lower() or ".jpg"
    return f"avatars/{instance.pk or 'new'}/{uuid.uuid4().hex[:12]}{ext}"


class User(AbstractUser):
    """Custom user model with membership-level role + language preference.

    Role state lives in ``membership_level`` (a TextChoices: applicant /
    member / staff / admin). ``is_admin`` and ``is_internal`` read this
    field directly.

    Cached-property staleness caveat: the two cached properties below
    (``is_admin``, ``is_internal``) are memoized on the instance. If
    you mutate ``user.membership_level`` in the same request and then
    re-check, you'll see the pre-mutation value. After mutating, bust
    the cache with ``User._invalidate_role_cache(user)``.
    ``UserManager.create_superuser`` does this automatically. The
    ``UserCreationForm`` / ``UserChangeForm`` in ``apps.accounts.forms``
    do NOT yet set ``membership_level`` — admin-side membership-level
    promotion happens via Django Admin until the dedicated UI lands
    in PR-2.
    """

    class Language(models.TextChoices):
        ENGLISH = "en", _("English")
        GERMAN = "de", _("German")

    language = models.CharField(
        _("language"),
        max_length=2,
        choices=Language.choices,
        default=Language.ENGLISH,
    )

    class MembershipLevel(models.TextChoices):
        APPLICANT = "applicant", _("Vereins-Bewerber")
        MEMBER = "member", _("Vereins-Mitglied")
        STAFF = "staff", _("Vereins-Staff")
        ADMIN = "admin", _("Vereins-Admin")

    membership_level = models.CharField(
        _("membership level"),
        max_length=10,
        choices=MembershipLevel.choices,
        default=MembershipLevel.APPLICANT,
    )

    objects = UserManager()

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        ordering = ["username"]

    def __str__(self):
        return self.username

    # NOTE: the two properties below are @cached_property. Once a
    # caller reads e.g. ``user.is_admin``, the value is memoized on the
    # instance and stays stale through subsequent ``membership_level``
    # mutations on the same instance. Bust with
    # ``User._invalidate_role_cache(user)`` after mutating
    # ``membership_level`` in the same request.
    @cached_property
    def is_admin(self):
        """True iff Vereins-Admin (membership_level=ADMIN).

        Backwards-compat: kept the same name as the pre-refactor
        group-based check — call-site semantics are unchanged.
        """
        return self.membership_level == self.MembershipLevel.ADMIN

    @cached_property
    def is_internal(self):
        """True iff Vereins-Staff or Vereins-Admin.

        Replaces the pre-refactor ``is_staff_member``. Renamed because
        Django's built-in ``is_staff`` (admin-backend access) is a
        related but distinct concept — keeping both names increases
        confusion. ``is_internal`` reads cleanly at call sites:
        "is this user a member of the internal operations team?"
        """
        return self.membership_level in (
            self.MembershipLevel.STAFF,
            self.MembershipLevel.ADMIN,
        )

    def is_station_admin(self, station):
        return self.station_assignments.filter(
            station=station,
            role="admin",
        ).exists()

    def is_station_maintainer(self, station):
        return self.station_assignments.filter(
            station=station,
            role="maintainer",
        ).exists()

    def is_region_manager(self, region):
        if region is None:
            return False
        return self.region_assignments.filter(
            region=region,
            role="manager",
        ).exists()

    def can_administer_station(self, station):
        """Full operative authority on `station`:
        Vereins-Admin OR Vereins-Staff OR Station-Admin of `station`
        OR Region-Manager of station.region.
        """
        if self.is_internal:
            return True
        if self.is_station_admin(station):
            return True
        if self.is_region_manager(station.region):
            return True
        return False

    def can_maintain_station(self, station):
        """can_administer_station OR Station-Maintainer of `station`.

        Lower bar than administer: maintenance + operational acks,
        but not structural changes (image release, station rename).
        """
        if self.can_administer_station(station):
            return True
        return self.is_station_maintainer(station)

    def can_use_station(self, station):
        """Future hook for radio operation (Funken über die Station).

        Today: every non-Applicant user passes. The Funk-Stack does not
        yet exist; the permission is defined now so its consumers can
        be written against a stable contract. Per-station restriction
        (e.g., only Region-Members may funken on Region-Stations) can
        be added later without changing the signature.
        """
        return self.membership_level != self.MembershipLevel.APPLICANT

    @staticmethod
    def _invalidate_role_cache(user):
        """Delete cached ``is_admin`` / ``is_internal`` entries on a User
        instance after ``membership_level`` has been mutated in the same
        request. Idempotent — safe to call when the properties have not
        yet been read.
        """
        for attr in ("is_admin", "is_internal"):
            user.__dict__.pop(attr, None)


class AccountAuditLog(models.Model):
    """System-wide audit trail for account-management and topology events.

    Parallel to StationAuditLog (per-station) and SsoAuditLog (SSO/OIDC).
    The apps/audit/ listing view merges all three into a single feed.
    """

    class EventType(models.TextChoices):
        MEMBERSHIP_PROMOTED = "membership_promoted", _("Membership Promoted")
        MEMBERSHIP_DEMOTED = "membership_demoted", _("Membership Demoted")
        REGION_ASSIGNMENT_CREATED = "region_assignment_created", _("Region Assignment Created")
        REGION_ASSIGNMENT_REVOKED = "region_assignment_revoked", _("Region Assignment Revoked")
        REGION_CREATED = "region_created", _("Region Created")
        REGION_UPDATED = "region_updated", _("Region Updated")
        REGION_DELETED = "region_deleted", _("Region Deleted")

    event_type = models.CharField(
        _("event type"),
        max_length=32,
        choices=EventType.choices,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="account_audit_logs_as_actor",
        verbose_name=_("actor"),
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="account_audit_logs_as_target",
        verbose_name=_("target user"),
    )
    region = models.ForeignKey(
        "stations.Region",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        verbose_name=_("region"),
    )
    message = models.TextField(_("message"), blank=True)
    ip_address = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("account audit log")
        verbose_name_plural = _("account audit logs")
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
        region=None,
        message="",
        ip_address=None,
    ):
        return cls.objects.create(
            event_type=event_type,
            actor=actor,
            target_user=target_user,
            region=region,
            message=message,
            ip_address=ip_address,
        )
