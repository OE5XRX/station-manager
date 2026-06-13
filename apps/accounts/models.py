import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone
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
    """Per-user randomised storage path: avatars/<user_id>/<random>.jpg.

    Each upload produces a fresh path — old files become orphaned but
    are not auto-cleaned (Cleanup-Job out-of-scope; siehe Overview Sektion 7).
    Using a random suffix means re-uploading the same file twice doesn't
    overwrite (and doesn't break browser caching for the old URL).

    Extension is hard-coded to ``.jpg`` because ``process_avatar_file``
    (avatars.py) always re-encodes uploads as JPEG. Preserving the
    original extension would produce filenames whose bytes do not
    match (e.g. ``foo.png`` containing JPEG bytes), which breaks
    Content-Type inference in CDNs and storage backends. The ``filename``
    parameter is part of Django's ``upload_to`` callable contract but
    is intentionally ignored for the extension.
    """
    del filename  # see docstring — bytes are always JPEG after processing
    return f"avatars/{instance.pk or 'new'}/{uuid.uuid4().hex[:12]}.jpg"


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

    # Sub-Spec 2b Soft-Delete: override AbstractUser.username with unique=False
    # so the conditional UniqueConstraint (unique_active_username) in Meta
    # becomes the sole DB-level enforcer. Without this override, Django keeps
    # AbstractUser's unconditional UNIQUE index on username and callsign-reuse
    # after soft-delete fails with IntegrityError.
    username_validator = UnicodeUsernameValidator()

    username = models.CharField(
        _("username"),
        max_length=150,
        # unique=False — DB-level uniqueness is enforced via Meta.constraints
        # (unique_active_username) which uses condition=deleted_at__isnull=True.
        # This allows callsign-reuse after soft-delete.
        unique=False,
        # db_index=True keeps lookups + ordering on the full table (not just
        # the active slice) efficient. The unique_active_username partial
        # index in Meta only indexes deleted_at IS NULL rows, so queries
        # for show=all / show=deleted listings ordered by username would
        # otherwise full-scan as the table grows.
        db_index=True,
        help_text=_("Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only."),
        validators=[username_validator],
        error_messages={
            "unique": _("A user with that username already exists."),
        },
    )

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

    # === Profile fields (added in Sub-Spec 1a Foundation) ===
    # Self-Description, max 500 chars
    bio = models.TextField(_("bio"), max_length=500, blank=True)

    # Profile picture; resized to max 512x512 JPEG by ProfileForm.save() in 1c
    avatar = models.ImageField(
        _("avatar"),
        upload_to=avatar_upload_path,
        null=True,
        blank=True,
    )

    # Amateur-radio standortlabel ("QTH" = ham slang for location)
    qth_name = models.CharField(_("QTH name"), max_length=128, blank=True)

    # Public QRZ.com profile URL — convenience deep-link
    qrz_url = models.URLField(_("QRZ URL"), max_length=200, blank=True)

    # Postal address as free text (multi-line). Geocoding consumes this.
    address = models.TextField(_("address"), blank=True)

    # Phone, free format (international)
    phone = models.CharField(_("phone"), max_length=32, blank=True)

    # Geographic coordinates from geocoding `address`. Not user-edited directly.
    # Range validators run on full_clean(); existing NULL rows are unaffected.
    latitude = models.DecimalField(
        _("latitude"),
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    longitude = models.DecimalField(
        _("longitude"),
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )

    # Maidenhead 6-char locator, computed from lat/lon OR user-set override
    locator = models.CharField(
        _("Maidenhead locator"),
        max_length=6,
        blank=True,
        validators=[locator_validator],
    )

    # Master directory-visibility switch. When False, other members see
    # only callsign + membership pill + avatar.
    is_directory_visible = models.BooleanField(
        _("visible in member directory"),
        default=True,
    )

    # === Sub-Spec 2b Soft-Delete ===
    # NULL = active user. NOT NULL = soft-deleted; the soft-delete flow
    # also flips ``is_active`` to False so login/middleware paths reject
    # the row. The conditional UniqueConstraint on ``username`` below
    # (``unique_active_username``) keeps the callsign available for
    # reuse once a row is soft-deleted.
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text=_(
            "Soft-delete timestamp. NULL = active user. NOT NULL = soft-deleted, "
            "is_active is False, login blocked."
        ),
    )
    deleted_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deleted_users",
        help_text=_("Admin who triggered the soft-delete (SET_NULL on cascade)."),
    )

    objects = UserManager()

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        ordering = ["username"]
        constraints = [
            # Case-insensitive uniqueness on email at the DB layer.
            # Backstops the per-form `email__iexact` validators and
            # closes the race window in the email-verify swap where
            # two concurrent verify-clicks could otherwise commit the
            # same email to two users.
            #
            # Sub-Spec 2b Soft-Delete: narrowed to the active slice
            # (``deleted_at IS NULL``) so a soft-deleted user's email
            # can be re-issued to a fresh active user — mirrors the
            # ``unique_active_username`` constraint below.
            models.UniqueConstraint(
                Lower("email"),
                condition=Q(deleted_at__isnull=True) & ~Q(email=""),
                name="accounts_user_email_ci_unique",
            ),
            # Sub-Spec 2b Soft-Delete: callsign-reuse after soft-delete.
            # AbstractUser declares ``username`` as ``unique=True`` (full
            # uniqueness across all rows). The partial UNIQUE-Index here
            # narrows that to active rows only, so when a user is
            # soft-deleted (``deleted_at`` set), the same callsign can
            # be issued to a fresh row. The AbstractUser-level
            # ``unique=True`` on ``username`` is explicitly relaxed in
            # the field override above; this partial constraint is now
            # the sole DB-level uniqueness enforcer for the active slice.
            models.UniqueConstraint(
                fields=["username"],
                condition=Q(deleted_at__isnull=True),
                name="unique_active_username",
            ),
        ]

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
        # === Existing (do not reorder) ===
        MEMBERSHIP_PROMOTED = "membership_promoted", _("Membership Promoted")
        MEMBERSHIP_DEMOTED = "membership_demoted", _("Membership Demoted")
        REGION_ASSIGNMENT_CREATED = "region_assignment_created", _("Region Assignment Created")
        REGION_ASSIGNMENT_REVOKED = "region_assignment_revoked", _("Region Assignment Revoked")
        REGION_CREATED = "region_created", _("Region Created")
        REGION_UPDATED = "region_updated", _("Region Updated")
        REGION_DELETED = "region_deleted", _("Region Deleted")
        # === Added in Sub-Spec 1a Foundation ===
        USER_CREATED = "user_created", _("User Created")
        USER_UPDATED = "user_updated", _("User Updated")
        USER_DELETED = "user_deleted", _("User Deleted")
        USER_ACTIVATED = "user_activated", _("User Activated")
        USER_DEACTIVATED = "user_deactivated", _("User Deactivated")
        PASSWORD_CHANGED = "password_changed", _("Password Changed")
        STATION_ASSIGNMENT_CREATED = "station_assignment_created", _("Station Assignment Created")
        STATION_ASSIGNMENT_REVOKED = "station_assignment_revoked", _("Station Assignment Revoked")
        # === Added in Sub-Spec 2a Token-Email-Flows ===
        WELCOME_TOKEN_SENT = "welcome_token_sent", _("Welcome Token Sent")
        PASSWORD_RESET_REQUESTED = "password_reset_requested", _("Password Reset Requested")
        PASSWORD_RESET_RATE_LIMITED = (
            "password_reset_rate_limited",
            _("Password Reset Rate Limited"),
        )
        PASSWORD_SET_FROM_TOKEN = "password_set_from_token", _("Password Set From Token")
        EMAIL_VERIFY_REQUESTED = "email_verify_requested", _("Email Verify Requested")
        EMAIL_VERIFIED = "email_verified", _("Email Verified")
        # === Added in Sub-Spec 2b Soft-Delete ===
        # USER_DELETED above stays as deprecated marker for legacy pre-2b DB
        # rows. After 2b the soft-delete flow emits USER_SOFT_DELETED; an
        # explicit hard-purge from the trash bucket emits USER_HARD_PURGED;
        # un-deleting from the trash bucket emits USER_RESTORED.
        USER_SOFT_DELETED = "user_soft_deleted", _("User Soft-Deleted")
        USER_RESTORED = "user_restored", _("User Restored")
        USER_HARD_PURGED = "user_hard_purged", _("User Hard-Purged")

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


class AccountToken(models.Model):
    """Single-use, time-limited token for Welcome / Reset / Verify flows.

    The raw token is generated via ``secrets.token_urlsafe(32)`` and is
    only ever returned from ``issue_token``; the DB stores only the
    SHA-256 hash. Consumption is atomic: ``consume_token`` does a
    SELECT FOR UPDATE on the row and sets ``used_at`` in the same
    transaction, so a parallel request cannot redeem the same token.
    """

    class TokenType(models.TextChoices):
        WELCOME = "welcome", _("Welcome (set initial password)")
        RESET = "reset", _("Password reset")
        VERIFY = "verify", _("Email verification")

    EXPIRY = {
        TokenType.WELCOME: timedelta(days=7),
        TokenType.RESET: timedelta(hours=24),
        TokenType.VERIFY: timedelta(hours=24),
    }

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="account_tokens",
    )
    token_type = models.CharField(
        max_length=16,
        choices=TokenType.choices,
        db_index=True,
    )
    secret_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    ip_created = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "token_type", "used_at"]),
            models.Index(fields=["secret_hash"]),
        ]

    def is_active(self):
        return self.used_at is None and self.expires_at > timezone.now()
