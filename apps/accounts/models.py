from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from .managers import UserManager


class User(AbstractUser):
    """Custom user model with role and language preferences.

    Group membership replaces the old single-valued `role` field. The
    three default groups (admin / operator / member) are created by
    apps.accounts.migrations.0002_role_to_groups; new groups can be
    added freely via Django Admin without code changes.

    Cached properties below mirror the pre-refactor `is_admin` /
    `is_operator` API so call sites need not learn about Groups. A new
    `is_staff_member` covers the common admin-OR-operator gate.

    Staleness caveat: the three properties are @cached_property — once
    read, the value is memoized on the instance. If you mutate
    ``user.groups`` in the same request and then re-check
    ``is_admin`` / etc., you'll see the pre-mutation value. After
    mutating, bust the cache with ``User._invalidate_role_cache(user)``
    (helper below). The Task 13 grant-toggle view will need this.
    """

    class Language(models.TextChoices):
        ENGLISH = "en", _("English")
        GERMAN = "de", _("German")

    # `role` is intentionally retained for one release so the data migration
    # in 0002 and any pre-deploy code keep working. Task 6 (next commit)
    # drops the column once nothing reads it.
    class Role(models.TextChoices):
        ADMIN = "admin", _("Admin")
        OPERATOR = "operator", _("Operator")
        MEMBER = "member", _("Member")

    role = models.CharField(
        _("role"),
        max_length=10,
        choices=Role.choices,
        default=Role.MEMBER,
        help_text=_(
            "DEPRECATED — superseded by Django Groups in migration 0002. "
            "This column is dropped in the next release."
        ),
    )
    language = models.CharField(
        _("language"),
        max_length=2,
        choices=Language.choices,
        default=Language.ENGLISH,
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
    def is_operator(self):
        return self.groups.filter(name="operator").exists()

    @cached_property
    def is_staff_member(self):
        """True iff user is in admin OR operator group."""
        return self.groups.filter(name__in=["admin", "operator"]).exists()

    @staticmethod
    def _invalidate_role_cache(user):
        """Delete cached is_admin / is_operator / is_staff_member entries
        on a User instance after ``user.groups`` has been mutated in the
        same request. Idempotent — safe to call when the properties
        have not yet been read.
        """
        for attr in ("is_admin", "is_operator", "is_staff_member"):
            user.__dict__.pop(attr, None)
