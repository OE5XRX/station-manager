from django.contrib.auth.models import UserManager as BaseUserManager


class UserManager(BaseUserManager):
    """Custom manager for the User model.

    ``create_superuser`` directly sets ``membership_level=ADMIN`` so
    that ``is_admin`` (membership-level-backed since Task 9) returns True
    on the freshly-created superuser. Without this, ``manage.py
    createsuperuser`` would produce an account that satisfies
    ``is_superuser`` but fails ``AdminRequiredMixin.test_func`` —
    breaking the bootstrap on a fresh install.

    Regular ``create_user`` does NOT auto-assign any membership level
    beyond the field default (APPLICANT). In PR-1, admins promote users
    via Django Admin's UserAdmin user-edit page (which honors the
    model field's ``choices``). The project's ``UserCreationForm`` /
    ``UserChangeForm`` in ``apps.accounts.forms`` do NOT yet expose
    ``membership_level`` — the dedicated user-management UI is
    deferred to PR-2. This mirrors Django's stock ``auth.User``
    behaviour: stock ``create_user`` also doesn't pre-populate role
    state, leaving that decision to the caller.
    """

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        user = super().create_superuser(username, email, password, **extra_fields)
        # Ensure the new superuser is Vereins-Admin — is_superuser alone
        # does not satisfy AdminRequiredMixin, which checks
        # ``user.is_admin`` (membership-level-backed). Without this,
        # manage.py createsuperuser produces an account that cannot
        # reach admin views, breaking the chicken-and-egg bootstrap on
        # a fresh install.
        from apps.accounts.models import User as UserModel

        user.membership_level = UserModel.MembershipLevel.ADMIN
        user.save(update_fields=["membership_level"])
        # Bust @cached_property entries on the freshly-created instance
        # so callers reading ``user.is_admin`` in the same transaction
        # (tests, post-create hooks) see the post-mutation truth.
        UserModel._invalidate_role_cache(user)
        return user
