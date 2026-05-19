from django.contrib.auth.models import Group
from django.contrib.auth.models import UserManager as BaseUserManager


class UserManager(BaseUserManager):
    """Custom manager for the User model.

    create_superuser additionally adds the new user to the ``admin``
    auth.Group so that ``is_admin`` (group-backed) returns True on the
    freshly-created superuser. Without this, ``manage.py
    createsuperuser`` would produce an account that satisfies
    ``is_superuser`` but fails ``AdminRequiredMixin.test_func`` —
    breaking the bootstrap on a fresh install.

    Regular create_user does NOT auto-assign any group; admins manage
    group membership via Django Admin's UserAdmin (filter_horizontal
    on the Groups M2M).
    """

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
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
