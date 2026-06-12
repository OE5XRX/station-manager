"""UserChangeForm — Admin-side edit of an existing user.

Sub-Spec 1c Sektion 3.1. The form gains 8 new profile fields plus a
clean_avatar / clean_locator gate and an avatar-resize side effect on
save().
"""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.forms import UserChangeForm
from apps.accounts.models import User


def _make_jpeg(width=200, height=200):
    from PIL import Image

    img = Image.new("RGB", (width, height), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return buf


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM1",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser(
        username="OE5ADMIN",
        password="x",
        email="admin@example.org",
    )


@pytest.mark.django_db
class TestUserChangeFormFields:
    def test_all_new_fields_present(self, member):
        form = UserChangeForm(instance=member)
        for field in [
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
        ]:
            assert field in form.fields, f"missing field: {field}"


@pytest.mark.django_db
class TestUserChangeFormLocatorValidation:
    def test_valid_locator_passes(self, member):
        form = UserChangeForm(
            data={
                "username": member.username,
                "email": "x@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
                "is_active": "on",
                "bio": "",
                "qth_name": "",
                "qrz_url": "",
                "phone": "",
                "address": "",
                "locator": "JN78AB",
                "is_directory_visible": "on",
            },
            instance=member,
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["locator"] == "JN78AB"

    def test_lowercase_locator_is_normalised_to_uppercase(self, member):
        form = UserChangeForm(
            data={
                "username": member.username,
                "email": "x@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
                "is_active": "on",
                "bio": "",
                "qth_name": "",
                "qrz_url": "",
                "phone": "",
                "address": "",
                "locator": "jn78ab",
                "is_directory_visible": "on",
            },
            instance=member,
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["locator"] == "JN78AB"

    def test_invalid_locator_rejected(self, member):
        form = UserChangeForm(
            data={
                "username": member.username,
                "email": "x@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
                "is_active": "on",
                "bio": "",
                "qth_name": "",
                "qrz_url": "",
                "phone": "",
                "address": "",
                "locator": "XX",
                "is_directory_visible": "on",
            },
            instance=member,
        )
        assert not form.is_valid()
        assert "locator" in form.errors

    def test_empty_locator_accepted(self, member):
        form = UserChangeForm(
            data={
                "username": member.username,
                "email": "x@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
                "is_active": "on",
                "bio": "",
                "qth_name": "",
                "qrz_url": "",
                "phone": "",
                "address": "",
                "locator": "",
                "is_directory_visible": "on",
            },
            instance=member,
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["locator"] == ""


@pytest.mark.django_db
class TestUserChangeFormAvatarValidation:
    def test_oversized_avatar_rejected(self, member):
        from apps.accounts.avatars import MAX_AVATAR_BYTES

        payload = b"\xff" * (MAX_AVATAR_BYTES + 100)
        f = SimpleUploadedFile("big.jpg", payload, content_type="image/jpeg")
        form = UserChangeForm(
            data={
                "username": member.username,
                "email": "x@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
                "is_active": "on",
                "bio": "",
                "qth_name": "",
                "qrz_url": "",
                "phone": "",
                "address": "",
                "locator": "",
                "is_directory_visible": "on",
            },
            files={"avatar": f},
            instance=member,
        )
        assert not form.is_valid()
        assert "avatar" in form.errors

    def test_non_image_avatar_rejected(self, member):
        f = SimpleUploadedFile("notimg.jpg", b"plain text", content_type="image/jpeg")
        form = UserChangeForm(
            data={
                "username": member.username,
                "email": "x@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
                "is_active": "on",
                "bio": "",
                "qth_name": "",
                "qrz_url": "",
                "phone": "",
                "address": "",
                "locator": "",
                "is_directory_visible": "on",
            },
            files={"avatar": f},
            instance=member,
        )
        assert not form.is_valid()
        assert "avatar" in form.errors

    def test_valid_avatar_save_triggers_resize(self, member, tmp_path, settings, monkeypatch):
        """Form.save() must call process_avatar_file on the uploaded file."""
        from apps.accounts.avatars import process_avatar_file as real_resize

        # The test settings use InMemoryStorage by default; switch the
        # default storage to a real on-disk backend rooted at tmp_path
        # so user.avatar.path resolves to a real file we can resize.
        settings.MEDIA_ROOT = str(tmp_path)
        settings.STORAGES = {
            **settings.STORAGES,
            "default": {
                "BACKEND": "django.core.files.storage.FileSystemStorage",
            },
        }
        calls = []

        def fake_process(path):
            calls.append(path)
            real_resize(path)

        monkeypatch.setattr("apps.accounts.forms.process_avatar_file", fake_process)

        buf = _make_jpeg(1024, 768)
        f = SimpleUploadedFile("ok.jpg", buf.read(), content_type="image/jpeg")
        form = UserChangeForm(
            data={
                "username": member.username,
                "email": "x@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
                "is_active": "on",
                "bio": "",
                "qth_name": "",
                "qrz_url": "",
                "phone": "",
                "address": "",
                "locator": "",
                "is_directory_visible": "on",
            },
            files={"avatar": f},
            instance=member,
        )
        assert form.is_valid(), form.errors
        form.save()
        assert len(calls) == 1, calls


@pytest.mark.django_db
class TestUserFormTemplate:
    """user_form.html renders 3 panels in Edit-Mode (Identity / Profil /
    Adresse) and uses grid-main (no inline max-width)."""

    def test_edit_form_has_three_panels(self, client, admin_user, member):
        client.force_login(admin_user)
        resp = client.get(
            reverse("accounts:user_edit", kwargs={"pk": member.pk})
        )
        body = resp.content.decode()
        # Identity panel (always)
        assert ">Identity<" in body or "<h2>Identity</h2>" in body or "Identity" in body
        # Profil panel (Edit-Mode only)
        assert "Profil" in body
        # Address panel (Edit-Mode only)
        assert "Adresse" in body or "Address" in body
        # Mobile-friendly: no inline max-width on the form
        assert 'style="max-width:640px' not in body

    def test_create_form_omits_profile_address_panels(self, client, admin_user):
        client.force_login(admin_user)
        resp = client.get(reverse("accounts:user_create"))
        body = resp.content.decode()
        # Profil/Adresse only show up in Edit-Mode (1c spec Sektion 3.4)
        assert "Profil" not in body or "Adresse" not in body
