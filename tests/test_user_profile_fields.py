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
