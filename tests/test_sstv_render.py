"""apps.sstv.render: odd uploads (Review Focus 1), fit, overlays, test picture."""

import io
import struct
import zlib
from datetime import UTC, datetime

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.sstv import render
from apps.sstv.render import UploadError
from station_agent.sstv.modes import MODES

ALL_MODES = list(MODES.values())
NOW = datetime(2026, 7, 20, 18, 5, 0, tzinfo=UTC)


def _encode(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _file(data, name="upload.bin"):
    return SimpleUploadedFile(name, data)


def _png_with_size(width, height, bit_depth=8):
    """A tiny, valid-headered PNG claiming ``width``×``height`` (a decompression bomb)."""

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\0" * 64))
        + chunk(b"IEND", b"")
    )


# --- load_upload: odd uploads ----------------------------------------------


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "GIF"])
def test_load_accepts_supported_formats(fmt):
    img = render.load_upload(_file(_encode(Image.new("RGB", (40, 30), (200, 10, 10)), fmt)))
    assert img.mode == "RGB" and img.size == (40, 30)


def test_load_rejects_other_formats():
    with pytest.raises(UploadError) as exc:
        render.load_upload(_file(_encode(Image.new("RGB", (8, 8)), "BMP")))
    assert exc.value.code == "unsupported"


@pytest.mark.parametrize("data", [b"", b"not an image at all", b"\x89PNG\r\n\x1a\ngarbage" * 4])
def test_load_rejects_non_images(data):
    with pytest.raises(UploadError) as exc:
        render.load_upload(_file(data))
    assert exc.value.code == "unsupported"


def test_load_rejects_truncated_image():
    data = _encode(Image.effect_noise((200, 200), 64).convert("RGB"), "JPEG")
    with pytest.raises(UploadError) as exc:
        render.load_upload(_file(data[: len(data) // 2]))
    assert exc.value.code == "unsupported"


def test_load_rejects_oversized_upload():
    data = _encode(Image.new("RGB", (8, 8)), "PNG")
    with pytest.raises(UploadError) as exc:
        render.load_upload(_file(data), max_bytes=len(data) - 1)
    assert exc.value.code == "too_large"
    assert render.load_upload(_file(data), max_bytes=len(data)).size == (8, 8)


@pytest.mark.parametrize(
    "size",
    [
        (8000, 8000),  # 64 Mpx: above our 40 Mpx limit, below Pillow's own bomb error
        (20000, 20000),  # 400 Mpx: Pillow's DecompressionBombError range
        (render.MAX_IMAGE_PIXELS + 1, 1),
    ],
)
def test_load_rejects_decompression_bomb(size):
    with pytest.raises(UploadError) as exc:
        render.load_upload(_file(_png_with_size(*size)))
    assert exc.value.code == "too_many_pixels"
    assert str(exc.value)  # a message for the user


def test_bomb_guard_does_not_change_pillow_global():
    before = Image.MAX_IMAGE_PIXELS
    with pytest.raises(UploadError):
        render.load_upload(_file(_png_with_size(8000, 8000)))
    assert Image.MAX_IMAGE_PIXELS == before


def test_load_reads_plain_file_objects():
    data = _encode(Image.new("RGB", (5, 6)), "PNG")
    assert render.load_upload(io.BytesIO(data)).size == (5, 6)


def _exif_rotated_jpeg():
    """A 'phone' JPEG: stored landscape 60×40, EXIF orientation 6 (rotate 90° CW to view).

    Stored left half red, right half blue → upright it is 40×60, top red, bottom blue.
    """
    img = Image.new("RGB", (60, 40), (255, 0, 0))
    img.paste((0, 0, 255), (30, 0, 60, 40))
    exif = Image.Exif()
    exif[0x0112] = 6
    return _encode(img, "JPEG", exif=exif.tobytes(), quality=95)


def test_exif_rotation_is_applied():
    img = render.load_upload(_file(_exif_rotated_jpeg()))
    assert img.size == (40, 60)
    top, bottom = img.getpixel((20, 5)), img.getpixel((20, 55))
    assert top[0] > 200 and top[2] < 60, top
    assert bottom[2] > 200 and bottom[0] < 60, bottom


def test_exif_rotated_preview_is_upright():
    mode = MODES["martin1"]
    out = render.fit_to_mode(render.load_upload(_file(_exif_rotated_jpeg())), mode)
    assert out.size == (mode.width, mode.height)
    # Portrait source cover-cropped into landscape: the centre band, red above blue.
    top, bottom = out.getpixel((160, 10)), out.getpixel((160, 245))
    assert top[0] > 200 and top[2] < 60, top
    assert bottom[2] > 200 and bottom[0] < 60, bottom


def test_rgba_is_composited_on_black():
    img = Image.new("RGBA", (20, 10), (255, 255, 255, 0))  # transparent white
    img.paste((0, 255, 0, 255), (10, 0, 20, 10))  # opaque green right half
    out = render.load_upload(_file(_encode(img, "PNG")))
    assert out.mode == "RGB"
    assert out.getpixel((2, 5)) == (0, 0, 0)
    assert out.getpixel((15, 5)) == (0, 255, 0)


def test_half_transparent_pixels_are_blended():
    img = Image.new("RGBA", (4, 4), (255, 255, 255, 128))
    r, g, b = render.load_upload(_file(_encode(img, "PNG"))).getpixel((1, 1))
    assert 120 <= r <= 136 and r == g == b


def test_la_and_palette_transparency_are_opaque():
    la = Image.new("LA", (4, 4), (255, 0))
    assert render.load_upload(_file(_encode(la, "PNG"))).getpixel((0, 0)) == (0, 0, 0)
    p = Image.new("P", (4, 4), 1)
    p.putpalette([0, 0, 0, 255, 255, 255] + [0] * 762)
    p.info["transparency"] = 1
    out = render.load_upload(_file(_encode(p, "PNG", transparency=1)))
    assert out.mode == "RGB" and out.getpixel((0, 0)) == (0, 0, 0)


def test_cmyk_jpeg_becomes_rgb():
    cmyk = Image.new("CMYK", (16, 16), (0, 255, 255, 0))  # pure red in CMYK
    out = render.load_upload(_file(_encode(cmyk, "JPEG", quality=95)))
    assert out.mode == "RGB"
    r, g, b = out.getpixel((8, 8))
    assert r > 200 and g < 60 and b < 60, (r, g, b)


@pytest.mark.parametrize("mode", ["L", "1", "I;16", "P"])
def test_other_pixel_modes_become_rgb(mode):
    out = render.load_upload(_file(_encode(Image.new(mode, (6, 6)), "PNG")))
    assert out.mode == "RGB"


def test_animated_gif_uses_first_frame():
    frames = [Image.new("RGB", (10, 10), c) for c in ((255, 0, 0), (0, 0, 255))]
    data = _encode(frames[0], "GIF", save_all=True, append_images=frames[1:])
    r, g, b = render.load_upload(_file(data)).getpixel((5, 5))
    assert r > 200 and b < 60


def test_corrupt_exif_does_not_crash():
    exif = Image.Exif()
    exif[0x0112] = 99  # invalid orientation value
    data = _encode(Image.new("RGB", (10, 8)), "JPEG", exif=exif.tobytes())
    assert render.load_upload(_file(data)).size == (10, 8)


# --- fit_to_mode -----------------------------------------------------------


@pytest.mark.parametrize("mode", ALL_MODES, ids=lambda m: m.key)
@pytest.mark.parametrize("size", [(4000, 3000), (300, 4000), (10, 10), (320, 256)])
def test_fit_exact_mode_size(mode, size):
    out = render.fit_to_mode(Image.new("RGB", size, (9, 9, 9)), mode)
    assert out.size == (mode.width, mode.height) and out.mode == "RGB"


@pytest.mark.parametrize("size", [(1, 4000), (4000, 1)])
def test_fit_extreme_panorama_fills_the_frame(size):
    mode = MODES["pd120"]
    out = render.fit_to_mode(Image.new("RGB", size, (200, 100, 50)), mode)
    assert out.size == (mode.width, mode.height)
    # Filled, no letterbox: every corner carries the source colour.
    for xy in [
        (0, 0),
        (mode.width - 1, 0),
        (0, mode.height - 1),
        (mode.width - 1, mode.height - 1),
    ]:
        assert out.getpixel(xy) == (200, 100, 50), xy


def test_fit_crops_centred():
    # 3 vertical stripes, wide source: the centre stripe must fill the middle.
    src = Image.new("RGB", (900, 256), (255, 0, 0))
    src.paste((0, 255, 0), (300, 0, 600, 256))
    out = render.fit_to_mode(src, MODES["martin1"])
    assert out.getpixel((160, 128)) == (0, 255, 0)


def test_fit_does_not_modify_input():
    src = Image.new("RGB", (50, 50), (1, 2, 3))
    render.fit_to_mode(src, MODES["robot36"])
    assert src.size == (50, 50)


# --- overlay_callsigns -----------------------------------------------------


def _rows_equal(a, b, y0, y1):
    return a.crop((0, y0, a.width, y1)).tobytes() == b.crop((0, y0, b.width, y1)).tobytes()


@pytest.mark.parametrize("mode", ALL_MODES, ids=lambda m: m.key)
def test_overlay_changes_bottom_rows_only(mode):
    base = render.fit_to_mode(Image.effect_noise((400, 400), 40).convert("RGB"), mode)
    out = render.overlay_callsigns(base, ["OE5XRX", "OE5ABC"])
    assert out.size == base.size and out.mode == "RGB"
    bar = render.bar_height(mode.height)
    assert 0 < bar < mode.height // 4
    assert _rows_equal(base, out, 0, mode.height - bar)
    assert not _rows_equal(base, out, mode.height - bar, mode.height)


def test_overlay_is_high_contrast():
    base = Image.new("RGB", (320, 256), (128, 128, 128))
    out = render.overlay_callsigns(base, ["OE5XRX"])
    colours = {
        c for _, c in out.crop((0, 256 - render.bar_height(256), 320, 256)).getcolors(10**6)
    }
    assert (0, 0, 0) in colours and (255, 255, 255) in colours


def test_overlay_empty_list_is_noop():
    base = Image.new("RGB", (320, 256), (10, 20, 30))
    assert render.overlay_callsigns(base, []).tobytes() == base.tobytes()
    assert render.overlay_callsigns(base, ["", "  "]).tobytes() == base.tobytes()


def test_overlay_does_not_modify_input():
    base = Image.new("RGB", (320, 256), (10, 20, 30))
    snapshot = base.tobytes()
    render.overlay_callsigns(base, ["OE5XRX"])
    assert base.tobytes() == snapshot


def test_overlay_differs_by_callsign():
    base = Image.new("RGB", (320, 256), (10, 20, 30))
    a = render.overlay_callsigns(base, ["OE5XRX"])
    b = render.overlay_callsigns(base, ["OE5ABC"])
    assert a.tobytes() != b.tobytes()


def test_overlay_long_text_stays_inside_the_bar():
    base = Image.new("RGB", (320, 240), (10, 20, 30))
    out = render.overlay_callsigns(base, ["A-VERY-LONG-CALLSIGN-NAME" * 3, "OE5XRX"])
    assert _rows_equal(base, out, 0, 240 - render.bar_height(240))


# --- test_picture ----------------------------------------------------------


@pytest.mark.parametrize("mode", ALL_MODES, ids=lambda m: m.key)
def test_test_picture_size(mode):
    out = render.test_picture(mode, ["OE5XRX"], "Döbriach", NOW)
    assert out.size == (mode.width, mode.height) and out.mode == "RGB"


def test_test_picture_is_deterministic():
    mode = MODES["scottie1"]
    a = render.test_picture(mode, ["OE5XRX"], "Station", NOW)
    b = render.test_picture(mode, ["OE5XRX"], "Station", NOW)
    assert a.tobytes() == b.tobytes()


def test_test_picture_differs_in_callsign_band_only():
    mode = MODES["pd120"]
    a = render.test_picture(mode, ["OE5XRX"], "Station", NOW)
    b = render.test_picture(mode, ["OE5ABC"], "Station", NOW)
    y0, y1 = render.callsign_band(mode.height)
    assert not _rows_equal(a, b, y0, y1)
    assert _rows_equal(a, b, 0, y0) and _rows_equal(a, b, y1, mode.height)


def test_test_picture_depends_on_time_mode_and_station():
    mode = MODES["martin1"]
    base = render.test_picture(mode, ["OE5XRX"], "Station", NOW).tobytes()
    later = NOW.replace(minute=6)
    assert render.test_picture(mode, ["OE5XRX"], "Station", later).tobytes() != base
    assert render.test_picture(mode, ["OE5XRX"], "Other", NOW).tobytes() != base
    m2 = MODES["martin2"]  # same size, different name
    assert render.test_picture(m2, ["OE5XRX"], "Station", NOW).tobytes() != base


def test_test_picture_text_shows_utc_and_vienna():
    lines = render.test_picture_lines(MODES["martin1"], "Station", NOW)
    text = "\n".join(lines)
    assert "2026-07-20" in text
    assert "18:05 UTC" in text
    assert "20:05 CEST" in text  # Europe/Vienna in summer
    assert "Martin M1" in text and "Station" in text
    winter = render.test_picture_lines(
        MODES["martin1"], "S", datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    )
    assert "10:00 CET" in "\n".join(winter)


def test_test_picture_naive_now_is_utc():
    naive = NOW.replace(tzinfo=None)
    mode = MODES["robot36"]
    a = render.test_picture(mode, ["OE5XRX"], "S", naive).tobytes()
    assert a == render.test_picture(mode, ["OE5XRX"], "S", NOW).tobytes()


def test_test_picture_without_callsign_or_station():
    out = render.test_picture(MODES["robot72"], [], "", NOW)
    assert out.size == (320, 240)


def test_test_picture_has_colour_bars():
    mode = MODES["martin1"]
    out = render.test_picture(mode, ["OE5XRX"], "S", NOW)
    y = render.colour_bar_band(mode.height)[0] + 2
    colours = {out.getpixel((x, y)) for x in range(0, mode.width, 4)}
    assert {(255, 255, 0), (0, 255, 255), (255, 0, 0), (0, 0, 255)} <= colours


# --- to_rgb_bytes ----------------------------------------------------------


@pytest.mark.parametrize("mode", ALL_MODES, ids=lambda m: m.key)
def test_rgb_bytes_match_encoder_contract(mode):
    img = render.test_picture(mode, ["OE5XRX"], "S", NOW)
    data = render.to_rgb_bytes(img)
    assert len(data) == mode.width * mode.height * 3
    assert data[:3] == bytes(img.getpixel((0, 0)))


def test_rgb_bytes_converts_other_modes():
    assert render.to_rgb_bytes(Image.new("L", (2, 2), 7)) == bytes([7] * 12)


def test_sixteen_bit_greyscale_is_scaled_not_clipped():
    img = Image.new("I;16", (4, 4), 32768)  # mid grey in 16 bit
    r, g, b = render.load_upload(_file(_encode(img, "PNG"))).getpixel((0, 0))
    assert 120 <= r <= 136 and r == g == b


def test_large_jpeg_is_decoded_reduced_but_fits():
    big = Image.new("RGB", (6000, 4000), (30, 160, 90))
    img = render.load_upload(_file(_encode(big, "JPEG", quality=80)))
    assert img.width >= 1280 and img.height >= 1280 and img.width < 6000
    out = render.fit_to_mode(img, MODES["pd180"])
    assert out.size == (640, 496)


# --- memory: big pictures are cut to the working size right after decoding ----


def test_big_png_is_reduced_after_decoding():
    big = Image.new("RGBA", (8000, 5000), (10, 200, 30, 255))
    img = render.load_upload(_file(_encode(big, "PNG")))
    assert img.mode == "RGB" and 1024 <= min(img.size) < 2048
    assert img.size == (8000 // 4, 5000 // 4)
    assert img.getpixel((10, 10)) == (10, 200, 30)


def test_panorama_keeps_only_the_centre_two_to_one():
    pano = Image.new("L", (9000, 1000), 0)
    pano.paste(255, (4000, 0, 5000, 1000))  # white centre
    img = render.load_upload(_file(_encode(pano, "PNG")))
    assert img.size == (2000, 1000)
    assert img.getpixel((1000, 500)) == (255, 255, 255)
    out = render.fit_to_mode(img, MODES["martin1"])
    reference = render.fit_to_mode(pano.convert("RGB"), MODES["martin1"])
    assert out.tobytes() == reference.tobytes()


def test_exif_rotation_survives_the_shrink():
    img = Image.new("RGB", (3000, 2200), (255, 0, 0))
    img.paste((0, 0, 255), (1500, 0, 3000, 2200))
    exif = Image.Exif()
    exif[0x0112] = 6
    out = render.load_upload(_file(_encode(img, "PNG", exif=exif.tobytes())))
    assert out.size == (1100, 1500)
    top, bottom = out.getpixel((550, 50)), out.getpixel((550, 1450))
    assert top[0] > 200 and top[2] < 60, top
    assert bottom[2] > 200 and bottom[0] < 60, bottom


def test_mpo_phone_jpeg_is_decoded_reduced():
    frames = [Image.new("RGB", (6000, 4000), (255, 0, 0)), Image.new("RGB", (64, 48))]
    data = _encode(frames[0], "MPO", save_all=True, append_images=frames[1:])
    assert Image.open(io.BytesIO(data)).format == "MPO"
    img = render.load_upload(_file(data))
    assert img.size == (3000, 2000)
    r, g, b = img.getpixel((10, 10))
    assert r > 200 and g < 60 and b < 60


def test_sixteen_bit_greyscale_with_transparency_is_scaled():
    img = Image.new("I;16", (4, 4), 30000)
    for xy in [(x, y) for x in range(2) for y in range(4)]:
        img.putpixel(xy, 40000)  # the tRNS key: transparent (paste() mangles I;16 ints)
    out = render.load_upload(_file(_encode(img, "PNG", transparency=40000)))
    assert out.getpixel((0, 0)) == (0, 0, 0)
    r, g, b = out.getpixel((3, 0))
    assert 110 <= r <= 124 and r == g == b


def test_sixteen_bit_has_a_lower_pixel_cap():
    big = render.MAX_IMAGE_PIXELS_16BIT + 1
    with pytest.raises(UploadError) as exc:
        render.load_upload(_file(_png_with_size(big, 1, bit_depth=16)))
    assert exc.value.code == "too_many_pixels"
    assert "16" in str(exc.value)
    # The same size at 8 bit is within the general cap (fails later only as a bad PNG).
    with pytest.raises(UploadError) as exc:
        render.load_upload(_file(_png_with_size(big, 1)))
    assert exc.value.code == "unsupported"


def test_big_sixteen_bit_with_transparency_is_reduced():
    img = Image.new("I;16", (3000, 2400), 30000)
    img.paste(Image.new("I;16", (1500, 2400), 40000), (0, 0))  # left half = tRNS key
    out = render.load_upload(_file(_encode(img, "PNG", transparency=40000)))
    assert out.size == (1500, 1200)
    assert out.getpixel((100, 600)) == (0, 0, 0)
    r, g, b = out.getpixel((1400, 600))
    assert 110 <= r <= 124 and r == g == b


def test_big_palette_with_transparency_is_reduced():
    img = Image.new("P", (2400, 2200), 1)
    img.putpalette([0, 0, 0, 200, 50, 10] + [0] * 762)
    img.paste(0, (0, 0, 1200, 2200))
    out = render.load_upload(_file(_encode(img, "PNG", transparency=0)))
    assert out.size == (1200, 1100)
    assert out.getpixel((100, 500)) == (0, 0, 0)
    assert out.getpixel((1100, 500)) == (200, 50, 10)


@pytest.mark.parametrize(
    ("mode", "key", "fill"),
    [("RGB", (255, 0, 255), (20, 120, 220)), ("L", 200, 90)],
)
def test_big_colour_key_transparency_has_no_fringe(mode, key, fill):
    img = Image.new(mode, (4096, 2400), fill)
    img.paste(key, (0, 0, 2049, 2400))  # key edge not on a reduce-block boundary
    out = render.load_upload(_file(_encode(img, "PNG", transparency=key)))
    assert out.size == (2048, 1200)
    expected = fill if mode == "RGB" else (fill,) * 3
    colours = {c for _, c in out.getcolors(10**6)}
    # Only black (keyed), the fill, and blends of the two — never the key colour.
    assert (0, 0, 0) in colours and expected in colours
    key_rgb = key if mode == "RGB" else (key,) * 3
    assert key_rgb not in colours
    for c in colours:
        assert all(0 <= v <= max(e, 0) for v, e in zip(c, expected, strict=True)), c
