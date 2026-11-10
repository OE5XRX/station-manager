from types import SimpleNamespace

import pytest

from apps.passive.aprs.igate import beacon_line, gate_line, login_line, passcode

IG = "OE5XRX-10"


def _pkt(info=">hi", path=(), src="OE5ABC", dst="APRS"):
    header = ",".join((dst, *path))
    return SimpleNamespace(
        source=src, destination=dst, path=list(path), raw_tnc2=f"{src}>{header}:{info}"
    )


def test_passcode_known_values():
    assert passcode("N0CALL") == 13023 and passcode("n0call-10") == 13023


def test_login_line():
    assert login_line(IG, "", "1.0") == (
        f"user OE5XRX-10 pass {passcode('OE5XRX')} vers OE5XRX-StationManager 1.0\r\n"
    )
    assert login_line(IG, "r/48/14/50", "1.0").endswith(" 1.0 filter r/48/14/50\r\n")


def test_gate_direct():
    line, why = gate_line(_pkt("!4812.34N/01412.34E>x", ["WIDE1-1"]), IG, set())
    assert (line, why) == ("OE5ABC>APRS,WIDE1-1,qAR,OE5XRX-10:!4812.34N/01412.34E>x\r\n", "")


def test_gate_keeps_used_path_star():
    line, _ = gate_line(_pkt(path=["OE5XBL-10*", "WIDE2-1"]), IG, set())
    assert line == "OE5ABC>APRS,OE5XBL-10*,WIDE2-1,qAR,OE5XRX-10:>hi\r\n"


@pytest.mark.parametrize("path", [["TCPIP*"], ["WIDE1-1", "NOGATE"], ["RFONLY"], ["TCPXX*"]])
def test_gate_skips_nogate_paths(path):
    assert gate_line(_pkt(path=path), IG, set()) == (None, "nogate_path")


def test_gate_skips_own_igate_call():
    assert gate_line(_pkt(src="OE5XRX-10"), IG, {"OE5XRX-10"}) == (None, "own_call")


def test_gate_thirdparty_unwrapped():
    p = _pkt("}OE1X>APRS,WIDE1*:>hi")
    assert gate_line(p, IG, set()) == ("OE1X>APRS,WIDE1*,qAR,OE5XRX-10:>hi\r\n", "")


def test_gate_thirdparty_from_inet_skipped():
    assert gate_line(_pkt("}OE1X>APRS,TCPIP,OE1Y*:>hi"), IG, set()) == (None, "thirdparty_inet")
    assert gate_line(_pkt("}garbage"), IG, set()) == (None, "thirdparty_bad")


def test_gate_query_skipped():
    assert gate_line(_pkt("?APRS?"), IG, set()) == (None, "query")


def test_gate_refuses_line_injection():
    assert gate_line(_pkt(">hi\r\nuser X pass 1"), IG, set()) == (None, "bad_chars")


def test_beacon_line():
    assert beacon_line(IG, 48.2057, 14.2057, "OE5XRX Remote Station RX-iGate") == (
        "OE5XRX-10>APZXRX,TCPIP*:!4812.34NR01412.34E&OE5XRX Remote Station RX-iGate\r\n"
    )


def test_beacon_southwest_and_rounding():
    assert beacon_line(IG, -33.9167, -70.5, "x") == (
        "OE5XRX-10>APZXRX,TCPIP*:!3355.00SR07030.00W&x\r\n"
    )
    assert "!4900.00NR" in beacon_line(IG, 48.999999, 14.0, "x")


def test_gate_thirdparty_nogate_inner_path_skipped():
    assert gate_line(_pkt("}OE1X>APRS,NOGATE:>hi"), IG, set()) == (None, "thirdparty_inet")
