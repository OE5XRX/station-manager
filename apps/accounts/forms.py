from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import (
    AuthenticationForm,
)
from django.contrib.auth.forms import (
    PasswordChangeForm as DjangoPasswordChangeForm,
)
from django.contrib.auth.forms import (
    UserChangeForm as BaseUserChangeForm,
)
from django.contrib.auth.forms import (
    UserCreationForm as BaseUserCreationForm,
)
from django.utils.translation import gettext_lazy as _

from .avatars import process_avatar_file, validate_avatar_upload
from .models import LOCATOR_REGEX

User = get_user_model()


def _maybe_resize_avatar(user):
    """Run ``process_avatar_file`` on the stored avatar when the backend
    supports filesystem paths. Non-FS storages (S3Boto3Storage,
    InMemoryStorage) raise ``NotImplementedError`` from ``FieldFile.path``;
    treat that as "skip in-place resize" rather than crash the save. When we
    later move to S3 we'll need a storage-agnostic resize pipeline; until
    then this gate keeps the form-save robust across storage backends.
    """
    if not user.avatar:
        return
    try:
        path = user.avatar.path
    except NotImplementedError:
        return
    process_avatar_file(path)


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
    """Form for admins to edit existing users.

    1c-Erweiterung: Identity-Felder plus die neuen Profile-Felder aus 1a.
    Avatar wird beim Save via process_avatar_file resized; Locator wird
    auf uppercase normalisiert und gegen LOCATOR_REGEX validiert.
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
            "bio",
            "avatar",
            "qth_name",
            "qrz_url",
            "phone",
            "address",
            "locator",
            "is_directory_visible",
        )
        widgets = {
            "username": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "language": forms.Select(attrs={"class": "form-select"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "bio": forms.Textarea(attrs={"class": "form-control", "rows": 3, "maxlength": 500}),
            "avatar": forms.ClearableFileInput(
                attrs={"class": "form-control", "accept": "image/*"}
            ),
            "qth_name": forms.TextInput(attrs={"class": "form-control"}),
            "qrz_url": forms.URLInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "address": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "locator": forms.TextInput(attrs={"class": "form-control", "placeholder": "JN78AB"}),
            "is_directory_visible": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean_avatar(self):
        f = self.cleaned_data.get("avatar")
        # Only validate on a fresh upload (or a clear) — re-validating the
        # already-stored FieldFile on every unrelated form submit costs an
        # extra Pillow open + verify for no benefit.
        if "avatar" in self.files:
            validate_avatar_upload(f)
        return f

    def clean_locator(self):
        loc = self.cleaned_data.get("locator", "").strip().upper()
        if loc and not LOCATOR_REGEX.match(loc):
            raise forms.ValidationError(
                _("Locator muss 2 Buchstaben + 2 Ziffern + 2 Buchstaben sein (z.B. JN78AB).")
            )
        return loc

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit and "avatar" in self.changed_data:
            _maybe_resize_avatar(user)
        return user


class ProfileIdentityForm(forms.ModelForm):
    """Self-edit of identity fields (Profile page → Identity panel)."""

    class Meta:
        model = User
        fields = ("email", "first_name", "last_name", "language")
        widgets = {
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "language": forms.Select(attrs={"class": "form-select"}),
        }


class ProfileProfileForm(forms.ModelForm):
    """Self-edit of profile-cosmetic fields (Profile page → Profil panel)."""

    class Meta:
        model = User
        fields = (
            "avatar",
            "bio",
            "qth_name",
            "qrz_url",
            "phone",
            "is_directory_visible",
        )
        widgets = {
            "avatar": forms.ClearableFileInput(
                attrs={"class": "form-control", "accept": "image/*"}
            ),
            "bio": forms.Textarea(attrs={"class": "form-control", "rows": 3, "maxlength": 500}),
            "qth_name": forms.TextInput(attrs={"class": "form-control"}),
            "qrz_url": forms.URLInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "is_directory_visible": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean_avatar(self):
        f = self.cleaned_data.get("avatar")
        # Only validate on a fresh upload (or a clear) — re-validating the
        # already-stored FieldFile on every unrelated form submit costs an
        # extra Pillow open + verify for no benefit.
        if "avatar" in self.files:
            validate_avatar_upload(f)
        return f

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit and "avatar" in self.changed_data:
            _maybe_resize_avatar(user)
        return user


class ProfileAddressForm(forms.ModelForm):
    """Self-edit of address + locator override (Profile page → Adresse panel).

    Geocoding-Trigger lives in ProfileView._maybe_geocode, not here.
    """

    class Meta:
        model = User
        fields = ("address", "locator")
        widgets = {
            "address": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "locator": forms.TextInput(attrs={"class": "form-control", "placeholder": "JN78AB"}),
        }

    def clean_locator(self):
        loc = self.cleaned_data.get("locator", "").strip().upper()
        if loc and not LOCATOR_REGEX.match(loc):
            raise forms.ValidationError(
                _("Locator muss 2 Buchstaben + 2 Ziffern + 2 Buchstaben sein (z.B. JN78AB).")
            )
        return loc


class PasswordChangeForm(DjangoPasswordChangeForm):
    """Bootstrap-styled overlay over Django's PasswordChangeForm.

    Re-Auth via the inherited ``old_password`` field; ProfilePasswordChangeView
    calls ``update_session_auth_hash`` after save() so the user stays logged in.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
