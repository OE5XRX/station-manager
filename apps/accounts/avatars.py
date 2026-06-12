"""Avatar upload validation + post-save processing (Sub-Spec 1a Foundation).

Two helpers:

- `validate_avatar_upload(file)` — called from Form.clean_avatar().
- `process_avatar_file(path)` — called from Form.save() after the file
  is on disk; resizes to max 512×512 and re-encodes as JPEG.

Spec: docs/superpowers/specs/2026-06-12-user-domain-1a-foundation-design.md
"""

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2 MB


def validate_avatar_upload(file):
    """Raises ValidationError if `file` is not a valid avatar upload.

    Checks: not None → exists; size ≤ 2 MB; Pillow recognises as an image.
    Resets the file cursor to 0 after Pillow consumed bytes.
    """
    if file is None:
        return
    if file.size > MAX_AVATAR_BYTES:
        raise ValidationError(_("Avatar darf max. 2 MB sein."))

    # Pillow's verify() reads the file-header and confirms format.
    from PIL import Image, UnidentifiedImageError

    try:
        img = Image.open(file)
        img.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValidationError(_("Datei ist kein gültiges Bild.")) from exc
    finally:
        # img.verify() consumes the file cursor; reset so the
        # subsequent save-pipeline can read from the start.
        try:
            file.seek(0)
        except (AttributeError, OSError):
            pass


def process_avatar_file(file_field_path: str) -> None:
    """Resize and re-encode the avatar file at the given filesystem path.

    In-place mutation: opens, resizes to max 512×512 (proportional),
    converts to RGB (drops alpha), writes back as JPEG quality=85.

    Called from Form.save() after super().save() has written the file
    to MEDIA_ROOT — at that point file_field_path is the actual disk path.
    """
    from PIL import Image

    with Image.open(file_field_path) as img:
        img.thumbnail((512, 512))
        # Convert to RGB to drop alpha channel — JPEG doesn't support alpha.
        # Convert before save() so the conversion is part of the file.
        rgb = img.convert("RGB")
        rgb.save(file_field_path, "JPEG", quality=85, optimize=True)
