from django.contrib.auth.models import UserManager as BaseUserManager


class UserManager(BaseUserManager):
    """Custom manager for the User model.

    ``create_superuser`` creates the account at ADMIN membership level; ``User.save``
    derives ``is_staff``/``is_superuser`` from it, so ``manage.py createsuperuser``
    bootstraps a usable Vereins-Admin on a fresh install.

    Regular ``create_user`` does NOT auto-assign any membership level beyond the
    field default (APPLICANT); that decision is left to the caller.
    """

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        from apps.accounts.models import User as UserModel

        extra_fields.setdefault("membership_level", UserModel.MembershipLevel.ADMIN)
        return super().create_superuser(username, email, password, **extra_fields)

    # === Sub-Spec 2b Soft-Delete helpers ===
    # No default-filter mutation — User.objects.all() still returns
    # everyone (including soft-deleted). Use these convenience filters
    # explicitly. Callers that should never see soft-deleted users
    # (login, directory, listings) opt in via .active().
    def active(self):
        """Convenience: User.objects.active() → non-soft-deleted."""
        return self.filter(deleted_at__isnull=True)

    def deleted(self):
        """Convenience: User.objects.deleted() → soft-deleted only."""
        return self.filter(deleted_at__isnull=False)

    def get_by_natural_key(self, username):
        """Resolve a login username to the ACTIVE user row.

        Django's authentication backend (``ModelBackend.authenticate``)
        calls ``UserModel._default_manager.get_by_natural_key(username)``
        which defaults to ``get(username=...)``. After 2b's conditional
        UNIQUE on ``username`` (active slice only), multiple rows can
        share the same callsign — one active + N soft-deleted — and the
        default ``get()`` would raise ``MultipleObjectsReturned``,
        breaking login for the active user with a recycled callsign.

        Filter on the active slice so the resolution is unambiguous.
        """
        return self.filter(deleted_at__isnull=True).get(**{self.model.USERNAME_FIELD: username})
