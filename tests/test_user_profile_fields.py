"""Tests for User profile fields (Sub-Spec 1a Foundation)."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError

from apps.accounts.models import (
    LOCATOR_REGEX,
    User,
    avatar_upload_path,
    locator_validator,
)


@pytest.fixture
def user(db):
    return User.objects.create_user(username="OE5TEST", password="x")


@pytest.fixture
def user_admin():
    return admin.site._registry.get(User)


class TestLocatorRegex:
    """Maidenhead 6-char locator format: 2 letters + 2 digits + 2 letters."""

    def test_valid_locator(self):
        assert LOCATOR_REGEX.match("JN78AB")
        assert LOCATOR_REGEX.match("AA00AA")
        assert LOCATOR_REGEX.match("RR99XX")

    def test_invalid_locator_too_short(self):
        assert not LOCATOR_REGEX.match("JN78")
        assert not LOCATOR_REGEX.match("JN78A")

    def test_invalid_locator_wrong_case(self):
        # Regex requires uppercase
        assert not LOCATOR_REGEX.match("jn78ab")

    def test_invalid_locator_digits_in_first_pair(self):
        assert not LOCATOR_REGEX.match("1278AB")
        assert not LOCATOR_REGEX.match("J278AB")

    def test_invalid_locator_letters_out_of_range(self):
        # First pair: A-R only
        assert not LOCATOR_REGEX.match("SS78AB")
        # Last pair: A-X only
        assert not LOCATOR_REGEX.match("JN78YY")

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            " JN78AB",
            "JN78AB ",
            "JN78AB\n",
            "Jn78ab",
            "JN78ABCD",
            "JN78-B",
            "JN78A!",
        ],
    )
    def test_invalid_locator_edge_cases(self, bad):
        assert not LOCATOR_REGEX.match(bad)


class TestLocatorValidator:
    """End-to-end: locator_validator raises ValidationError on bad input."""

    def test_valid_passes(self):
        locator_validator("JN78AB")  # no raise

    def test_invalid_raises(self):
        with pytest.raises(ValidationError):
            locator_validator("INVALID")


class TestAvatarUploadPath:
    """avatar_upload_path returns avatars/<pk-or-new>/<random>.<ext>."""

    def test_known_pk_in_path(self):
        instance = SimpleNamespace(pk=42)
        path = avatar_upload_path(instance, "selfie.jpg")
        assert path.startswith("avatars/42/")

    def test_no_pk_uses_new(self):
        instance = SimpleNamespace(pk=None)
        path = avatar_upload_path(instance, "selfie.jpg")
        assert path.startswith("avatars/new/")

    def test_extension_lowercased(self):
        instance = SimpleNamespace(pk=1)
        path = avatar_upload_path(instance, "FOO.JPG")
        assert path.endswith(".jpg")

    def test_extension_fallback_jpg(self):
        instance = SimpleNamespace(pk=1)
        path = avatar_upload_path(instance, "noext")
        assert path.endswith(".jpg")

    def test_png_extension_preserved(self):
        instance = SimpleNamespace(pk=1)
        path = avatar_upload_path(instance, "icon.PNG")
        assert path.endswith(".png")

    def test_unique_random_suffix(self):
        instance = SimpleNamespace(pk=1)
        paths = {avatar_upload_path(instance, "x.jpg") for _ in range(50)}
        # 50 random suffixes should all be unique (12 hex chars = 48 bits)
        assert len(paths) == 50


@pytest.mark.django_db
class TestUserProfileFieldDefaults:
    """Newly added profile fields exist with the expected defaults."""

    @pytest.mark.parametrize(
        "field,expected",
        [
            ("bio", ""),
            ("avatar", None),
            ("qth_name", ""),
            ("qrz_url", ""),
            ("address", ""),
            ("phone", ""),
            ("latitude", None),
            ("longitude", None),
            ("locator", ""),
            ("is_directory_visible", True),
        ],
    )
    def test_field_default(self, user, field, expected):
        value = getattr(user, field)
        if expected is None:
            # ImageField / nullable DecimalField: falsy (empty file / None)
            assert not value
        else:
            assert value == expected


@pytest.mark.django_db
class TestUserLocatorValidator:
    """User.locator field uses locator_validator."""

    def test_valid_locator_saves(self, user):
        user.locator = "JN78DH"
        user.full_clean()  # runs validators
        user.save()
        user.refresh_from_db()
        assert user.locator == "JN78DH"

    def test_invalid_locator_raises_validation_error(self, user):
        user.locator = "INVALID"
        with pytest.raises(ValidationError):
            user.full_clean()

    def test_empty_locator_allowed(self, user):
        user.locator = ""
        user.full_clean()  # should not raise


@pytest.mark.django_db
class TestUserLatLonValidators:
    """User.latitude / User.longitude reject values outside ±90 / ±180."""

    def test_latitude_out_of_range_rejected(self, user):
        user.latitude = Decimal("95.0")
        with pytest.raises(ValidationError):
            user.full_clean()

    def test_longitude_out_of_range_rejected(self, user):
        user.longitude = Decimal("200.0")
        with pytest.raises(ValidationError):
            user.full_clean()

    def test_latitude_at_boundary_accepted(self, user):
        user.latitude = Decimal("90.0")
        user.full_clean()  # no raise

    def test_longitude_at_boundary_accepted(self, user):
        user.longitude = Decimal("-180.0")
        user.full_clean()  # no raise


class TestUserAdminFieldsets:
    """UserAdmin exposes the new profile fields in dedicated fieldsets."""

    def test_admin_registered(self, user_admin):
        assert user_admin is not None

    @pytest.mark.parametrize(
        "label",
        ["Profile", "Address & Location", "Directory"],
    )
    def test_admin_has_fieldset(self, user_admin, label):
        fieldset_labels = [str(fs[0]) for fs in user_admin.fieldsets]
        assert label in fieldset_labels

    def test_profile_fieldset_contains_expected_fields(self, user_admin):
        profile_fieldset = next(fs for fs in user_admin.fieldsets if str(fs[0]) == "Profile")
        fields = profile_fieldset[1]["fields"]
        assert "avatar" in fields
        assert "bio" in fields
        assert "qth_name" in fields
        assert "qrz_url" in fields
        assert "phone" in fields

    def test_address_fieldset_contains_expected_fields(self, user_admin):
        addr_fieldset = next(
            fs for fs in user_admin.fieldsets if str(fs[0]) == "Address & Location"
        )
        fields = addr_fieldset[1]["fields"]
        assert "address" in fields
        assert "latitude" in fields
        assert "longitude" in fields
        assert "locator" in fields

    def test_directory_fieldset_contains_is_directory_visible(self, user_admin):
        dir_fieldset = next(fs for fs in user_admin.fieldsets if str(fs[0]) == "Directory")
        assert "is_directory_visible" in dir_fieldset[1]["fields"]
