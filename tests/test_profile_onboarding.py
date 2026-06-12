"""Onboarding-Hint-Kontext und Render-Bedingungen auf der Profile-Page.

Sub-Spec 1c Sektion 4.3.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import User


@pytest.fixture
def empty_user(db):
    return User.objects.create_user(
        username="OE5EMPTY",
        password="x",
        first_name="",
        last_name="",
        email="empty@example.org",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.mark.django_db
class TestOnboardingHints:
    def test_empty_user_all_hints_active(self, client, empty_user):
        client.force_login(empty_user)
        resp = client.get(reverse("accounts:profile"))
        hints = resp.context["onboarding_hints"]
        assert hints["name_missing"]
        assert hints["avatar_missing"]
        assert hints["bio_missing"]
        assert hints["qth_missing"]
        assert hints["address_missing"]

    def test_bio_filled_no_bio_hint(self, client, empty_user):
        empty_user.bio = "I am a radio amateur."
        empty_user.save()
        client.force_login(empty_user)
        resp = client.get(reverse("accounts:profile"))
        hints = resp.context["onboarding_hints"]
        assert not hints["bio_missing"]
        # Others still missing
        assert hints["name_missing"]

    def test_fully_filled_user_no_hints(self, client, empty_user, tmp_path, settings):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        settings.MEDIA_ROOT = str(tmp_path)
        # Set name, bio, qth, address
        empty_user.first_name = "Hans"
        empty_user.bio = "QRP"
        empty_user.qth_name = "Linz"
        empty_user.address = "Hauptstraße 1"
        # Upload a fake avatar file
        img = Image.new("RGB", (50, 50), color=(255, 0, 0))
        import io

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        buf.seek(0)
        f = SimpleUploadedFile("a.jpg", buf.read(), content_type="image/jpeg")
        empty_user.avatar = f
        empty_user.save()

        client.force_login(empty_user)
        resp = client.get(reverse("accounts:profile"))
        hints = resp.context["onboarding_hints"]
        assert not hints["name_missing"]
        assert not hints["avatar_missing"]
        assert not hints["bio_missing"]
        assert not hints["qth_missing"]
        assert not hints["address_missing"]
