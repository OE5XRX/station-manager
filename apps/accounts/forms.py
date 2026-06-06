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


class UserCreationForm(BaseUserCreationForm):
    """Form for admins to create new users.

    Identity fields only. ``membership_level`` defaults to APPLICANT
    on creation (set in apps/accounts/models.py); the admin promotes
    the user via the membership-card on the edit page after creation.
    Topology assignments (Region-Manager, Station-Admin/Maintainer)
    are managed from the same edit page once the user is at least
    Vereins-Mitglied — the ``_ApplicantForbiddenMixin`` invariant
    rejects assignments for applicants.
    """

    class Meta:
        model = User
        fields = ("username", "email", "first_name", "last_name", "language")
        widgets = {
            "username": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "language": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password1"].widget.attrs["class"] = "form-control"
        self.fields["password2"].widget.attrs["class"] = "form-control"


class UserChangeForm(BaseUserChangeForm):
    """Form for admins to edit existing users — identity fields only.

    Membership-level promote/demote and topology assignments are NOT
    in this form. They are HTMX-driven cards rendered alongside this
    form in user_form.html (Vereins-Rolle, Region-Manager-Zuordnungen,
    Stations-Zuordnungen) backed by dedicated POST endpoints — see
    apps/accounts/views_membership.py / views_region_assignments.py /
    views_station_assignments.py.
    """

    password = None

    class Meta:
        model = User
        fields = (
            "username",
            "email",
            "first_name",
            "last_name",
            "language",
            "is_active",
        )
        widgets = {
            "username": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
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
