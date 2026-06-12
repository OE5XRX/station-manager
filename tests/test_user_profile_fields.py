"""Tests for User profile fields (Sub-Spec 1a Foundation)."""

from types import SimpleNamespace

import pytest

from apps.accounts.models import LOCATOR_REGEX


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

    @pytest.mark.parametrize("bad", [
        "",
        " JN78AB",
        "JN78AB ",
        "JN78AB\n",
        "Jn78ab",
        "JN78ABCD",
        "JN78-B",
        "JN78A!",
    ])
    def test_invalid_locator_edge_cases(self, bad):
        assert not LOCATOR_REGEX.match(bad)


class TestLocatorValidator:
    """End-to-end: locator_validator raises ValidationError on bad input."""

    def test_valid_passes(self):
        from apps.accounts.models import locator_validator
        locator_validator("JN78AB")  # no raise

    def test_invalid_raises(self):
        from django.core.exceptions import ValidationError

        from apps.accounts.models import locator_validator
        with pytest.raises(ValidationError):
            locator_validator("INVALID")


class TestAvatarUploadPath:
    """avatar_upload_path returns avatars/<pk-or-new>/<random>.<ext>."""

    def test_known_pk_in_path(self):
        from apps.accounts.models import avatar_upload_path
        instance = SimpleNamespace(pk=42)
        path = avatar_upload_path(instance, "selfie.jpg")
        assert path.startswith("avatars/42/")

    def test_no_pk_uses_new(self):
        from apps.accounts.models import avatar_upload_path
        instance = SimpleNamespace(pk=None)
        path = avatar_upload_path(instance, "selfie.jpg")
        assert path.startswith("avatars/new/")

    def test_extension_lowercased(self):
        from apps.accounts.models import avatar_upload_path
        instance = SimpleNamespace(pk=1)
        path = avatar_upload_path(instance, "FOO.JPG")
        assert path.endswith(".jpg")

    def test_extension_fallback_jpg(self):
        from apps.accounts.models import avatar_upload_path
        instance = SimpleNamespace(pk=1)
        path = avatar_upload_path(instance, "noext")
        assert path.endswith(".jpg")

    def test_png_extension_preserved(self):
        from apps.accounts.models import avatar_upload_path
        instance = SimpleNamespace(pk=1)
        path = avatar_upload_path(instance, "icon.PNG")
        assert path.endswith(".png")

    def test_unique_random_suffix(self):
        from apps.accounts.models import avatar_upload_path
        instance = SimpleNamespace(pk=1)
        paths = {avatar_upload_path(instance, "x.jpg") for _ in range(50)}
        # 50 random suffixes should all be unique (12 hex chars = 48 bits)
        assert len(paths) == 50


@pytest.mark.django_db
class TestUserProfileFieldDefaults:
    """Newly added profile fields exist with the expected defaults."""

    def test_bio_default_empty(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.bio == ""

    def test_avatar_default_none(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        # ImageField when no file: falsy, often .name == ""
        assert not user.avatar

    def test_qth_name_default_empty(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.qth_name == ""

    def test_qrz_url_default_empty(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.qrz_url == ""

    def test_address_default_empty(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.address == ""

    def test_phone_default_empty(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.phone == ""

    def test_latitude_default_none(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.latitude is None

    def test_longitude_default_none(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.longitude is None

    def test_locator_default_empty(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.locator == ""

    def test_is_directory_visible_default_true(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.is_directory_visible is True


@pytest.mark.django_db
class TestUserLocatorValidator:
    """User.locator field uses locator_validator."""

    def test_valid_locator_saves(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        user.locator = "JN78DH"
        user.full_clean()  # runs validators
        user.save()
        user.refresh_from_db()
        assert user.locator == "JN78DH"

    def test_invalid_locator_raises_validation_error(self):
        from django.core.exceptions import ValidationError

        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        user.locator = "INVALID"
        with pytest.raises(ValidationError):
            user.full_clean()

    def test_empty_locator_allowed(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        user.locator = ""
        user.full_clean()  # should not raise


class TestUserAdminFieldsets:
    """UserAdmin exposes the new profile fields in dedicated fieldsets."""

    def test_admin_has_profile_fieldset(self):
        from django.contrib import admin

        from apps.accounts.models import User

        admin_instance = admin.site._registry.get(User)
        # admin_instance is the registered UserAdmin instance
        assert admin_instance is not None

        fieldset_labels = [str(fs[0]) for fs in admin_instance.fieldsets]
        assert "Profile" in fieldset_labels

    def test_admin_has_address_fieldset(self):
        from django.contrib import admin

        from apps.accounts.models import User

        admin_instance = admin.site._registry.get(User)
        fieldset_labels = [str(fs[0]) for fs in admin_instance.fieldsets]
        assert "Address & Location" in fieldset_labels

    def test_admin_has_directory_fieldset(self):
        from django.contrib import admin

        from apps.accounts.models import User

        admin_instance = admin.site._registry.get(User)
        fieldset_labels = [str(fs[0]) for fs in admin_instance.fieldsets]
        assert "Directory" in fieldset_labels

    def test_profile_fieldset_contains_expected_fields(self):
        from django.contrib import admin

        from apps.accounts.models import User

        admin_instance = admin.site._registry.get(User)
        profile_fieldset = next(
            fs for fs in admin_instance.fieldsets if str(fs[0]) == "Profile"
        )
        fields = profile_fieldset[1]["fields"]
        assert "avatar" in fields
        assert "bio" in fields
        assert "qth_name" in fields
        assert "qrz_url" in fields
        assert "phone" in fields

    def test_address_fieldset_contains_expected_fields(self):
        from django.contrib import admin

        from apps.accounts.models import User

        admin_instance = admin.site._registry.get(User)
        addr_fieldset = next(
            fs for fs in admin_instance.fieldsets if str(fs[0]) == "Address & Location"
        )
        fields = addr_fieldset[1]["fields"]
        assert "address" in fields
        assert "latitude" in fields
        assert "longitude" in fields
        assert "locator" in fields

    def test_directory_fieldset_contains_is_directory_visible(self):
        from django.contrib import admin

        from apps.accounts.models import User

        admin_instance = admin.site._registry.get(User)
        dir_fieldset = next(
            fs for fs in admin_instance.fieldsets if str(fs[0]) == "Directory"
        )
        assert "is_directory_visible" in dir_fieldset[1]["fields"]
