"""Tests for User profile fields (Sub-Spec 1a Foundation)."""

import pytest


class TestLocatorRegex:
    """Maidenhead 6-char locator format: 2 letters + 2 digits + 2 letters."""

    def test_valid_locator(self):
        from apps.accounts.models import LOCATOR_REGEX
        assert LOCATOR_REGEX.match("JN78AB")
        assert LOCATOR_REGEX.match("AA00AA")
        assert LOCATOR_REGEX.match("RR99XX")

    def test_invalid_locator_too_short(self):
        from apps.accounts.models import LOCATOR_REGEX
        assert not LOCATOR_REGEX.match("JN78")
        assert not LOCATOR_REGEX.match("JN78A")

    def test_invalid_locator_wrong_case(self):
        from apps.accounts.models import LOCATOR_REGEX
        # Regex requires uppercase
        assert not LOCATOR_REGEX.match("jn78ab")

    def test_invalid_locator_digits_in_first_pair(self):
        from apps.accounts.models import LOCATOR_REGEX
        assert not LOCATOR_REGEX.match("1278AB")
        assert not LOCATOR_REGEX.match("J278AB")

    def test_invalid_locator_letters_out_of_range(self):
        from apps.accounts.models import LOCATOR_REGEX
        # First pair: A-R only
        assert not LOCATOR_REGEX.match("SS78AB")
        # Last pair: A-X only
        assert not LOCATOR_REGEX.match("JN78YY")
