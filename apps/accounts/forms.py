from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import (
    AuthenticationForm,
)
from django.contrib.auth.forms import (
    UserChangeForm as BaseUserChangeForm,
)
from django.contrib.auth.forms import (
    UserCreationForm as BaseUserCreationForm,
)
from django.utils.translation import gettext_lazy as _

User = get_user_model()


class LoginForm(AuthenticationForm):
    """Login form with Bootstrap styling."""

    username = forms.CharField(
        label=_("Username"),
        widget=forms.TextInput(attrs={"class": "form-control", "autofocus": True}),
    )
    password = forms.CharField(
        label=_("Password"),
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
    )


class _RoleSyncMixin:
    """Mirror the deprecated ``role`` form field into auth.Group
    membership.

    Without this, selecting "Admin" in the form widget writes
    ``user.role='admin'`` but does NOT add the user to the admin group,
    so ``is_admin`` returns False on the freshly-created user. This
    mixin runs once per save and uses ``set()`` (not ``add()``) so that
    *changing* the role on an existing user removes the previously-
    assigned default-group membership.

    Both writes happen here for one release — Task 6 drops the ``role``
    column and turns the form's ``role`` widget into a direct group
    picker, at which point this mixin goes away.
    """

    def save(self, commit=True):
        user = super().save(commit=commit)
        # Skip when caller uses commit=False (the user has no PK yet,
        # so the M2M write would fail). Callers that defer commit must
        # invoke save_m2m() themselves; the role-sync is folded into
        # the standard commit path below.
        if commit and getattr(user, "role", None):
            from django.contrib.auth.models import Group

            target_group, _ = Group.objects.get_or_create(name=user.role)
            # set() not add() — switching a user's role must remove
            # them from the previously-assigned group.
            user.groups.set([target_group])
            # Bust @cached_property entries so subsequent
            # is_admin/is_operator/is_staff_member reads on this
            # instance return the post-mutation truth.
            User._invalidate_role_cache(user)
        return user


class UserCreationForm(_RoleSyncMixin, BaseUserCreationForm):
    """Form for admins to create new users."""

    class Meta:
        model = User
        fields = ("username", "email", "first_name", "last_name", "role", "language")
        widgets = {
            "username": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "role": forms.Select(attrs={"class": "form-select"}),
            "language": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password1"].widget.attrs["class"] = "form-control"
        self.fields["password2"].widget.attrs["class"] = "form-control"


class UserChangeForm(_RoleSyncMixin, BaseUserChangeForm):
    """Form for admins to edit existing users."""

    password = None

    class Meta:
        model = User
        fields = (
            "username",
            "email",
            "first_name",
            "last_name",
            "role",
            "language",
            "is_active",
        )
        widgets = {
            "username": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "role": forms.Select(attrs={"class": "form-select"}),
            "language": forms.Select(attrs={"class": "form-select"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class ProfileForm(forms.ModelForm):
    """Form for users to edit their own profile."""

    class Meta:
        model = User
        fields = ("email", "first_name", "last_name", "language")
        widgets = {
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "language": forms.Select(attrs={"class": "form-select"}),
        }
