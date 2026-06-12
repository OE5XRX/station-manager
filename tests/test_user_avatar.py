"""Tests for apps/accounts/avatars.py (Sub-Spec 1a Foundation)."""

import io

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.accounts.avatars import (
    MAX_AVATAR_BYTES,
    process_avatar_file,
    validate_avatar_upload,
)


def _make_jpeg(width=100, height=100, mode="RGB"):
    """Helper: build a small in-memory JPEG."""
    img = Image.new(mode, (width, height), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return buf


def _make_png_with_alpha(width=100, height=100):
    """Helper: build a small in-memory PNG with alpha channel."""
    img = Image.new("RGBA", (width, height), color=(0, 255, 0, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


class TestValidateAvatarUpload:
    """validate_avatar_upload raises ValidationError on bad files."""

    def test_none_returns_silently(self):
        validate_avatar_upload(None)  # should not raise

    def test_oversized_file_raises(self):
        # Build a 3 MB blob
        payload = b"\xff" * (MAX_AVATAR_BYTES + 100)
        f = SimpleUploadedFile("big.jpg", payload, content_type="image/jpeg")
        with pytest.raises(ValidationError) as exc:
            validate_avatar_upload(f)
        assert "2 MB" in str(exc.value) or "MB" in str(exc.value)

    def test_non_image_raises(self):
        f = SimpleUploadedFile("notimg.jpg", b"plain text content", content_type="image/jpeg")
        with pytest.raises(ValidationError):
            validate_avatar_upload(f)

    def test_valid_jpeg_passes(self):
        buf = _make_jpeg(256, 256)
        f = SimpleUploadedFile("ok.jpg", buf.read(), content_type="image/jpeg")
        validate_avatar_upload(f)  # should not raise

    def test_valid_png_with_alpha_passes(self):
        buf = _make_png_with_alpha(256, 256)
        f = SimpleUploadedFile("ok.png", buf.read(), content_type="image/png")
        validate_avatar_upload(f)  # should not raise

    def test_validate_does_not_advance_cursor(self):
        """validate_avatar_upload must not leave file.tell() != 0,
        sonst kann der nachgelagerte upload-flow das File nicht mehr lesen."""
        buf = _make_jpeg(256, 256)
        f = SimpleUploadedFile("ok.jpg", buf.read(), content_type="image/jpeg")
        validate_avatar_upload(f)
        # After validate, the file should be re-seekable to start
        assert f.tell() == 0


class TestProcessAvatarFile:
    """process_avatar_file resizes + re-encodes the file in-place."""

    def test_large_jpeg_resized_to_512(self, tmp_path):
        # 1024x768 source, will be downscaled
        src_path = tmp_path / "big.jpg"
        Image.new("RGB", (1024, 768), color=(255, 0, 0)).save(
            src_path,
            "JPEG",
            quality=85,
        )

        process_avatar_file(str(src_path))

        result = Image.open(src_path)
        assert max(result.size) == 512
        assert result.format == "JPEG"

    def test_png_converted_to_jpeg(self, tmp_path):
        src_path = tmp_path / "in.png"
        Image.new("RGB", (256, 256), color=(0, 255, 0)).save(src_path, "PNG")

        process_avatar_file(str(src_path))

        result = Image.open(src_path)
        assert result.format == "JPEG"

    def test_transparency_flattened_to_rgb(self, tmp_path):
        src_path = tmp_path / "alpha.png"
        Image.new("RGBA", (256, 256), color=(0, 0, 255, 128)).save(src_path, "PNG")

        process_avatar_file(str(src_path))

        result = Image.open(src_path)
        assert result.mode == "RGB"

    def test_small_image_not_upscaled(self, tmp_path):
        src_path = tmp_path / "small.jpg"
        Image.new("RGB", (200, 150), color=(255, 0, 0)).save(
            src_path,
            "JPEG",
            quality=85,
        )

        process_avatar_file(str(src_path))

        result = Image.open(src_path)
        # thumbnail() does not upscale — bleibt bei 200x150
        assert result.size == (200, 150)
