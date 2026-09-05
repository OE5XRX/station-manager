# tests/test_audio_router_backend.py
"""PipeWireRouterBackend: slot→node via OE5XRX_SLOT→api.alsa.card (Spec 0 §12 Finding 2)."""
import json

import pytest

from station_agent.audio.router_backend import PipeWireRouterBackend, RunResult


# A pw-dump snapshot reflecting Finding 2: on REAL HW the node.name carries the module
# serial, NOT the USB port — so resolution MUST key on api.alsa.card, never the name.
PW_DUMP = json.dumps(
    [
        {"id": 30, "type": "PipeWire:Interface:Core"},
        {
            "id": 40,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "media.class": "Audio/Source",
                    "api.alsa.card": 1,
                    "node.name": "alsa_input.usb-OE5XRX_FM_Transceiver_Board_2031394D3646500E004B004F-03.mono-fallback",
                }
            },
        },
        {
            "id": 41,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "media.class": "Audio/Sink",
                    "api.alsa.card": 1,
                    "node.name": "alsa_output.usb-OE5XRX_FM_Transceiver_Board_2031394D3646500E004B004F-03.mono-fallback",
                }
            },
        },
        {
            "id": 50,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "media.class": "Audio/Source",
                    "api.alsa.card": 0,
                    "node.name": "some.other.card",
                }
            },
        },
    ]
)


def make_backend(tmp_path, *, cards=(0, 1), slot_of=None, calls=None):
    """A backend whose sysfs has fake cards and whose `run` fakes udevadm + pw-*."""
    slot_of = slot_of or {1: 1}  # card index -> OE5XRX_SLOT
    sound = tmp_path / "sound"
    for c in cards:
        (sound / f"card{c}").mkdir(parents=True)

    def fake_run(argv, timeout=5.0):
        if calls is not None:
            calls.append(argv)
        prog = argv[0]
        if prog == "udevadm":
            # last arg is the sysfs path .../cardN
            path = argv[-1]
            cidx = int(path.rsplit("card", 1)[1])
            if cidx in slot_of:
                return RunResult(0, f"OE5XRX_SLOT={slot_of[cidx]}\nID_BUS=usb\n", "")
            return RunResult(0, "ID_BUS=usb\n", "")
        if prog == "pw-dump":
            return RunResult(0, PW_DUMP, "")
        if prog == "pw-link":
            return RunResult(0, "", "")
        if prog == "wpctl":
            return RunResult(0, "", "")
        return RunResult(1, "", "unknown")

    return PipeWireRouterBackend(run=fake_run, sysfs_sound=str(sound))


def test_resolve_rx_node_by_alsa_card_not_name(tmp_path):
    b = make_backend(tmp_path)
    node = b.resolve_node(1, "rx")
    # slot 1 → card 1 → the Audio/Source on card 1 (serial-based name; port is NOT in it)
    assert node == "alsa_input.usb-OE5XRX_FM_Transceiver_Board_2031394D3646500E004B004F-03.mono-fallback"


def test_resolve_tx_node_is_the_sink(tmp_path):
    b = make_backend(tmp_path)
    node = b.resolve_node(1, "tx")
    assert node.startswith("alsa_output.usb-OE5XRX_FM_Transceiver_Board")


def test_resolve_unknown_slot_returns_none(tmp_path):
    b = make_backend(tmp_path)
    assert b.resolve_node(3, "rx") is None  # no card tagged OE5XRX_SLOT=3


def test_list_audio_slots(tmp_path):
    b = make_backend(tmp_path, cards=(0, 1, 2), slot_of={1: 1, 2: 3})
    assert b.list_audio_slots() == [1, 3]  # sorted slot numbers with an audio card


def test_link_builds_pw_link_argv(tmp_path):
    calls = []
    b = make_backend(tmp_path, calls=calls)
    assert b.link("nodeA", "nodeB") is True
    pw_link = [c for c in calls if c[0] == "pw-link"][-1]
    assert "nodeA" in pw_link and "nodeB" in pw_link


def test_set_volume_uses_wpctl(tmp_path):
    calls = []
    b = make_backend(tmp_path, calls=calls)
    assert b.set_volume("node.name", 0.8) is True
    wpctl = [c for c in calls if c[0] == "wpctl"][-1]
    assert "0.8" in " ".join(wpctl)


def test_run_failure_fails_closed(tmp_path):
    def boom(argv, timeout=5.0):
        raise OSError("no such binary")

    b = PipeWireRouterBackend(run=boom, sysfs_sound=str(tmp_path))
    assert b.resolve_node(1, "rx") is None
    assert b.link("a", "b") is False
    assert b.set_volume("a", 0.5) is False


def test_bad_direction_raises(tmp_path):
    b = make_backend(tmp_path)
    with pytest.raises(ValueError):
        b.resolve_node(1, "sideways")
