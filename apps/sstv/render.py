"""Pictures for transmission: upload validation, fit to the mode, callsign overlay, test picture.

The preview PNG is exactly what is sent: ``to_rgb_bytes(preview)`` feeds the encoder.
Text drawn into pictures is never translated (it goes out over the air).
"""

import functools
import io
import zoneinfo
from datetime import UTC, datetime

from django.conf import settings
from django.utils.translation import gettext as _
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from station_agent.sstv.modes import Mode

ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")
# Our own bomb limit, checked from the header before decoding. Pillow's global
# ``Image.MAX_IMAGE_PIXELS`` (~89 Mpx) is left alone: it guards every other upload.
MAX_IMAGE_PIXELS = 40_000_000
# JPEGs are decoded at a reduced scale (DCT draft) when far larger than any mode.
_JPEG_DRAFT_SIZE = (1280, 1280)

_FONT_PATH = settings.BASE_DIR / "static" / "fonts" / "IBMPlexSans-Bold.woff2"
_LOCAL_TZ = zoneinfo.ZoneInfo("Europe/Vienna")

_BLACK = (0, 0, 0)
_WHITE = (255, 255, 255)
_BARS = [
    (255, 255, 255),
    (255, 255, 0),
    (0, 255, 255),
    (0, 255, 0),
    (255, 0, 255),
    (255, 0, 0),
    (0, 0, 255),
]


class UploadError(ValueError):
    """An upload that cannot become an SSTV picture; ``str()`` is shown to the user."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def _unsupported() -> UploadError:
    return UploadError(_("The file is not a JPEG, PNG, WebP or GIF picture."), "unsupported")


def load_upload(f, *, max_bytes: int = 15 * 1024 * 1024) -> Image.Image:
    """Decode an uploaded picture into an upright, opaque RGB image.

    JPEG/PNG/WebP/GIF (first frame) only; EXIF orientation applied; transparency
    composited on black. Raises ``UploadError`` for anything else, oversized files
    and decompression bombs (checked from the header, before decoding).
    """
    data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UploadError(
            _("The file is larger than %(mb)d MB.") % {"mb": max_bytes // (1024 * 1024)},
            "too_large",
        )
    bomb = UploadError(
        _("The picture has too many pixels (at most %(mpx)d megapixels).")
        % {"mpx": MAX_IMAGE_PIXELS // 1_000_000},
        "too_many_pixels",
    )
    try:
        img = Image.open(io.BytesIO(data), formats=ALLOWED_FORMATS)
        width, height = img.size
        if width * height > MAX_IMAGE_PIXELS:
            raise bomb
        if img.format == "JPEG":
            img.draft(None, _JPEG_DRAFT_SIZE)
        img.load()
    except Image.DecompressionBombError:
        raise bomb from None
    except UploadError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError):
        raise _unsupported() from None
    return _to_rgb(_upright(img))


def _upright(img: Image.Image) -> Image.Image:
    try:
        return ImageOps.exif_transpose(img)
    except (OSError, ValueError, SyntaxError, KeyError, TypeError):
        return img  # unreadable EXIF: orientation is cosmetic, keep the picture


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, (*_BLACK, 255))
        canvas.alpha_composite(rgba)
        return canvas.convert("RGB")
    if img.mode == "I" or img.mode.startswith("I;16"):
        # 16-bit greyscale: scale to 8 bit (a plain convert would clip to white).
        img = img.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    return img.convert("RGB")


def fit_to_mode(img: Image.Image, mode: Mode) -> Image.Image:
    """Cover-crop (centred) and scale to exactly ``mode.width × mode.height``."""
    return ImageOps.fit(
        img.convert("RGB"),
        (mode.width, mode.height),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


@functools.lru_cache(maxsize=64)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(_FONT_PATH), size)
    except OSError:
        return ImageFont.load_default(size)


def _fit_font(draw, text: str, max_w: int, max_h: int):
    """Largest font (≥ 6 px) whose rendering of ``text`` fits ``max_w × max_h``."""
    size = max(max_h, 6)
    while True:
        font = _font(size)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font, anchor="mm")
        if (right - left <= max_w and bottom - top <= max_h) or size <= 6:
            return font
        size -= 1


def _draw_centred(img: Image.Image, text: str, box: tuple[int, int, int, int], fill):
    """Draw ``text`` centred into ``box`` on a cropped tile: never paints outside the box."""
    x0, y0, x1, y1 = box
    tile = img.crop(box)
    draw = ImageDraw.Draw(tile)
    w, h = x1 - x0, y1 - y0
    pad = max(2, h // 8)
    font = _fit_font(draw, text, w - 2 * pad, h - 2 * pad)
    draw.text((w / 2, h / 2), text, font=font, fill=fill, anchor="mm")
    img.paste(tile, (x0, y0))


def bar_height(height: int) -> int:
    """Height of the callsign bar ``overlay_callsigns`` draws at the bottom."""
    return max(16, round(height * 0.11))


def _clean(callsigns: list[str]) -> list[str]:
    return [c.strip().upper() for c in callsigns if c and c.strip()]


def overlay_callsigns(img: Image.Image, callsigns: list[str]) -> Image.Image:
    """Callsigns in a black bar with white text along the bottom (no-op for none)."""
    calls = _clean(callsigns)
    if not calls:
        return img
    out = img.convert("RGB").copy()
    bar = bar_height(out.height)
    box = (0, out.height - bar, out.width, out.height)
    out.paste(_BLACK, box)
    _draw_centred(out, "  ·  ".join(calls), box, _WHITE)
    return out


def colour_bar_band(height: int) -> tuple[int, int]:
    return 0, round(height * 0.28)


def callsign_band(height: int) -> tuple[int, int]:
    return colour_bar_band(height)[1], round(height * 0.62)


def _aware_utc(now: datetime) -> datetime:
    return now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)


def test_picture_lines(mode: Mode, station_name: str, now: datetime) -> list[str]:
    """The info lines of the test picture: date, UTC + Vienna time, mode, station."""
    utc = _aware_utc(now)
    local = utc.astimezone(_LOCAL_TZ)
    lines = [
        utc.strftime("%Y-%m-%d"),
        f"{utc:%H:%M} UTC  ·  {local:%H:%M} {local.tzname()}",
        mode.name,
    ]
    if station_name.strip():
        lines.append(station_name.strip())
    return lines


def test_picture(
    mode: Mode, callsigns: list[str], station_name: str, now: datetime
) -> Image.Image:
    """Colour bars, the callsigns large, date/time (UTC + Vienna), mode and station name."""
    w, h = mode.width, mode.height
    img = Image.new("RGB", (w, h), _BLACK)

    bars_top, bars_bottom = colour_bar_band(h)
    for i, colour in enumerate(_BARS):
        x0, x1 = round(i * w / len(_BARS)), round((i + 1) * w / len(_BARS))
        img.paste(colour, (x0, bars_top, x1, bars_bottom))

    band_top, band_bottom = callsign_band(h)
    calls = _clean(callsigns)
    if calls:
        line_h = (band_bottom - band_top) // len(calls)
        for i, call in enumerate(calls):
            y0 = band_top + i * line_h
            _draw_centred(img, call, (0, y0, w, y0 + line_h), _WHITE)

    ramp_h = max(4, round(h * 0.05))
    info_top, info_bottom = band_bottom, h - ramp_h
    lines = test_picture_lines(mode, station_name, now)
    line_h = (info_bottom - info_top) // len(lines)
    for i, line in enumerate(lines):
        y0 = info_top + i * line_h
        _draw_centred(img, line, (0, y0, w, y0 + line_h), (220, 220, 220))

    for x in range(w):
        level = round(255 * x / max(w - 1, 1))
        img.paste((level, level, level), (x, h - ramp_h, x + 1, h))
    return img


def to_rgb_bytes(img: Image.Image) -> bytes:
    """Row-major ``width*height*3`` RGB bytes — the encoder's input."""
    return (img if img.mode == "RGB" else img.convert("RGB")).tobytes()
