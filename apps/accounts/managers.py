from django.contrib.auth.models import Group
from django.contrib.auth.models import UserManager as BaseUserManager


class UserManager(BaseUserManager):
    """Custom manager for the User model.

    Both create_user and create_superuser sync the (deprecated) ``role``
    field into auth.Group membership so that ``is_admin`` /
    ``is_operator`` / ``is_staff_member`` return the correct value on
    the freshly-created instance. Task 6 will drop the ``role`` column
    and remove the sync logic; until then this is the bridge that keeps
    both the column and the M2M consistent.
    """

    def create_user(self, username, email=None, password=None, **extra_fields):
        user = super().create_user(username, email, password, **extra_fields)
        self._sync_role_to_group(user)
        return user

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("role", "admin")
        user = super().create_superuser(username, email, password, **extra_fields)
        # Ensure the new superuser lands in the admin group — is_superuser
        # alone does not satisfy AdminRequiredMixin, which checks
        # ``user.is_admin`` (group-backed). Without this, manage.py
        # createsuperuser produces an account that cannot reach admin
        # views, breaking the chicken-and-egg bootstrap on a fresh
        # install.
        admin_group, _ = Group.objects.get_or_create(name="admin")
        user.groups.add(admin_group)
        # Bust @cached_property entries on the freshly-created instance
        # so callers reading ``user.is_admin`` in the same transaction
        # (tests, post-create hooks) see the post-mutation truth.
        from apps.accounts.models import User as UserModel

        UserModel._invalidate_role_cache(user)
        return user

    def _sync_role_to_group(self, user):
        """Mirror ``user.role`` into Group membership on a freshly-created
        user. Idempotent: safe to call when ``role`` is unset or already
        matches a group the user belongs to. Busts @cached_property
        entries so the next ``is_admin``/etc. check returns the
        post-mutation value.
        """
        if not getattr(user, "role", None):
            return
        target_group, _ = Group.objects.get_or_create(name=user.role)
        user.groups.add(target_group)
        from apps.accounts.models import User as UserModel

        UserModel._invalidate_role_cache(user)
