import os

from apps.passive.aprs.parse import parse_info, parse_thirdparty


def test_uncompressed_position():
    i = parse_info(b"!4812.34N/01412.34E>comment", "APRS")
    assert i.packet_type == "position"
    assert round(i.latitude, 4) == 48.2057 and round(i.longitude, 4) == 14.2057
    assert i.symbol == "/>" and i.comment == "comment"


def test_timestamped_position():
    i = parse_info(b"@092345z4903.50N/07201.75W>x", "APRS")
    assert i.packet_type == "position" and round(i.latitude, 4) == 49.0583
    assert round(i.longitude, 4) == -72.0292


def test_compressed_position():
    i = parse_info(b"=/5L!!<*e7>7P[", "APRS")
    assert i.packet_type == "position"
    assert round(i.latitude, 2) == 49.5 and round(i.longitude, 2) == -72.75
    assert i.symbol == "/>"


def test_mic_e_position():
    # lat 33 25.64 N from "S32U6T"; lon 12 07.74 W (no +100 offset: dest[4]='6')
    info = b"`" + bytes([12 + 28, 7 + 28, 74 + 28]) + b"   >/"
    i = parse_info(info + b"x", "S32U6T")
    assert i.packet_type == "mic_e"
    assert round(i.latitude, 4) == round(33 + 25.64 / 60, 4)
    assert round(i.longitude, 4) == -round(12 + 7.74 / 60, 4)
    assert i.symbol == "/>"


def test_mic_e_longitude_offset():
    # dest "332UVT": north (U), lon +100 (V), west (T): lon 112 07.74 W
    info = b"`" + bytes([12 + 28, 7 + 28, 74 + 28]) + b"   >/"
    i = parse_info(info, "332UVT")
    assert round(i.longitude, 4) == -round(112 + 7.74 / 60, 4)


def test_southern_western_hemisphere_signs():
    i = parse_info(b"!3355.00S/07030.00W>", "APRS")
    assert i.latitude < 0 and i.longitude < 0 and round(i.latitude, 4) == -33.9167


def test_object_and_item_positions():
    o = parse_info(b";LEADER   *092345z4903.50N/07201.75W>x", "APRS")
    assert o.packet_type == "object" and round(o.latitude, 4) == 49.0583
    t = parse_info(b")AID!4903.50N/07201.75WA", "APRS")
    assert t.packet_type == "item" and round(t.longitude, 4) == -72.0292


def test_simple_types():
    assert parse_info(b">status text", "APRS").packet_type == "status"
    assert parse_info(b":OE5ABC   :hello{1", "APRS").packet_type == "message"
    assert parse_info(b"}A>B:>x", "APRS").packet_type == "thirdparty"
    assert parse_info(b"_10090556c220", "APRS").packet_type == "weather"
    assert parse_info(b"?APRS?", "APRS").packet_type == "other"


def test_comment_clipped_and_stripped():
    i = parse_info(b">a\x00b\r\n" + b"x" * 400, "APRS")
    assert "\x00" not in i.comment and "\n" not in i.comment and len(i.comment) == 256


def test_garbage_never_raises():
    for _ in range(300):
        blob = os.urandom(40)
        parse_info(blob, "APRS")
        parse_info(blob, "S32U6T")
    for dti in b"!=/@`';)>:}_":
        for n in range(0, 20):
            parse_info(bytes([dti]) + b"\xff" * n, "")
    assert parse_info(b"", "APRS").packet_type == "other"
    bad = parse_info(b"!9999.99N/99999.99E>", "APRS")
    assert bad.latitude is None


def test_thirdparty_split():
    assert parse_thirdparty(b"}OE1X>APRS,TCPIP,OE1Y*:>hi") == ("OE1X>APRS,TCPIP,OE1Y*", ">hi")
    assert parse_thirdparty(b"}garbage") is None
    assert parse_thirdparty(b">x") is None


def test_thirdparty_header_is_validated():
    for bad in (
        b"}A>B,qAR,X:>hi",  # q-construct from RF
        b"}A>B,C D:>hi",  # space
        b"}A>B," + b",".join([b"W1"] * 9) + b":>hi",  # too many path elements
        b"}A B>C:>hi",
        b"}>C:>hi",
    ):
        assert parse_thirdparty(bad) is None, bad
