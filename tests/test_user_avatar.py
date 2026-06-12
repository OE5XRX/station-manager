"""Tests for apps/accounts/avatars.py (Sub-Spec 1a Foundation)."""

import io

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile


def _make_jpeg(width=100, height=100, mode="RGB"):
    """Helper: build a small in-memory JPEG."""
    from PIL import Image

    img = Image.new(mode, (width, height), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return buf


def _make_png_with_alpha(width=100, height=100):
    """Helper: build a small in-memory PNG with alpha channel."""
    from PIL import Image

    img = Image.new("RGBA", (width, height), color=(0, 255, 0, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


class TestValidateAvatarUpload:
    """validate_avatar_upload raises ValidationError on bad files."""

    def test_none_returns_silently(self):
        from apps.accounts.avatars import validate_avatar_upload

        validate_avatar_upload(None)  # should not raise

    def test_oversized_file_raises(self):
        from apps.accounts.avatars import (
            MAX_AVATAR_BYTES,
            validate_avatar_upload,
        )

        # Build a 3 MB blob
        payload = b"\xff" * (MAX_AVATAR_BYTES + 100)
        f = SimpleUploadedFile("big.jpg", payload, content_type="image/jpeg")
        with pytest.raises(ValidationError) as exc:
            validate_avatar_upload(f)
        assert "2 MB" in str(exc.value) or "MB" in str(exc.value)

    def test_non_image_raises(self):
        from apps.accounts.avatars import validate_avatar_upload

        f = SimpleUploadedFile("notimg.jpg", b"plain text content", content_type="image/jpeg")
        with pytest.raises(ValidationError):
            validate_avatar_upload(f)

    def test_valid_jpeg_passes(self):
        from apps.accounts.avatars import validate_avatar_upload

        buf = _make_jpeg(256, 256)
        f = SimpleUploadedFile("ok.jpg", buf.read(), content_type="image/jpeg")
        validate_avatar_upload(f)  # should not raise

    def test_valid_png_with_alpha_passes(self):
        from apps.accounts.avatars import validate_avatar_upload

        buf = _make_png_with_alpha(256, 256)
        f = SimpleUploadedFile("ok.png", buf.read(), content_type="image/png")
        validate_avatar_upload(f)  # should not raise

    def test_validate_does_not_advance_cursor(self):
        """validate_avatar_upload must not leave file.tell() != 0,
        sonst kann der nachgelagerte upload-flow das File nicht mehr lesen."""
        from apps.accounts.avatars import validate_avatar_upload

        buf = _make_jpeg(256, 256)
        f = SimpleUploadedFile("ok.jpg", buf.read(), content_type="image/jpeg")
        validate_avatar_upload(f)
        # After validate, the file should be re-seekable to start
        assert f.tell() == 0
