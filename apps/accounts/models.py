from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from .managers import UserManager


class User(AbstractUser):
    """Custom user model with group-backed role membership and language preference.

    Group membership replaces the old single-valued ``role`` field. The
    three default groups (admin / operator / member) are created by
    apps.accounts.migrations.0002_role_to_groups; new groups can be
    added freely via Django Admin without code changes.

    Cached properties below mirror the pre-refactor ``is_admin`` /
    ``is_operator`` API so call sites need not learn about Groups. A new
    ``is_staff_member`` covers the common admin-OR-operator gate.

    Staleness caveat: the four properties (``is_admin``,
    ``is_operator``, ``is_staff_member``, ``group_names``) are
    @cached_property — once read, the value is memoized on the
    instance. If you mutate ``user.groups`` in the same request and
    then re-check one of them, you'll see the pre-mutation value.
    After mutating, bust the cache with
    ``User._invalidate_role_cache(user)`` (helper below). The
    ``UserCreationForm`` / ``UserChangeForm`` paths in
    ``apps.accounts.forms`` and ``UserManager.create_superuser``
    do exactly this when assigning groups.
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

    # NOTE: the three properties below are @cached_property. Once a
    # caller reads e.g. ``user.is_admin``, the value is memoized on the
    # instance and stays stale through subsequent ``user.groups``
    # mutations on the same instance. Bust with
    # ``User._invalidate_role_cache(user)`` after mutating groups in
    # the same request.
    @cached_property
    def is_admin(self):
        return self.groups.filter(name="admin").exists()

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

    @cached_property
    def is_operator(self):
        return self.groups.filter(name="operator").exists()

    @cached_property
    def is_staff_member(self):
        """True iff user is in admin OR operator group."""
        return self.groups.filter(name__in=["admin", "operator"]).exists()

    @cached_property
    def group_names(self):
        """Sorted list of group names the user is a member of.

        Cached on the User instance so templates that render the user's
        group memberships (e.g. the sidebar role-badge block) don't fire
        a fresh ORM query on every page load.
        """
        return list(self.groups.order_by("name").values_list("name", flat=True))

    @staticmethod
    def _invalidate_role_cache(user):
        """Delete cached is_admin / is_operator / is_staff_member /
        group_names entries on a User instance after ``user.groups`` has
        been mutated in the same request. Idempotent — safe to call when
        the properties have not yet been read.
        """
        for attr in (
            "is_admin",
            "is_internal",
            "is_operator",
            "is_staff_member",
            "group_names",
        ):
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
