# tests/test_audio_router_backend.py
"""PipeWireRouterBackend: slot→node via OE5XRX_SLOT→api.alsa.card (Spec 0 §12 Finding 2)."""

import json

import pytest

from station_agent.audio.router_backend import PipeWireRouterBackend, RunResult

# A pw-dump snapshot reflecting Finding 2: on REAL HW the node.name carries the module
# serial, NOT the USB port — so resolution MUST key on api.alsa.card, never the name.
_SERIAL = "2031394D3646500E004B004F"
RX_NODE = f"alsa_input.usb-OE5XRX_FM_Transceiver_Board_{_SERIAL}-03.mono-fallback"
TX_NODE = f"alsa_output.usb-OE5XRX_FM_Transceiver_Board_{_SERIAL}-03.mono-fallback"

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
                    "node.name": RX_NODE,
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
                    "node.name": TX_NODE,
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
    assert node == RX_NODE
    assert "1.3" not in node  # Finding 2: the USB port is absent from the real node.name


def test_resolve_tx_node_is_the_sink(tmp_path):
    b = make_backend(tmp_path)
    node = b.resolve_node(1, "tx")
    assert node.startswith("alsa_output.usb-OE5XRX_FM_Transceiver_Board")


def _aloop_backend(tmp_path, rx_props, tx_props, *, card=7, card_id="oe5xrxslot1"):
    """A backend mimicking the sim snd-aloop card: sysfs card<N>/id + udev OE5XRX_SLOT=1."""
    sound = tmp_path / "sound"
    (sound / f"card{card}").mkdir(parents=True)
    (sound / f"card{card}" / "id").write_text(card_id + "\n")
    pw = json.dumps(
        [
            {"id": 43, "type": "PipeWire:Interface:Node", "info": {"props": rx_props}},
            {"id": 42, "type": "PipeWire:Interface:Node", "info": {"props": tx_props}},
        ]
    )

    def fake_run(argv, timeout=5.0):
        if argv[0] == "udevadm":
            cidx = int(argv[-1].rsplit("card", 1)[1])
            return RunResult(0, "OE5XRX_SLOT=1\n" if cidx == card else "", "")
        if argv[0] == "pw-dump":
            return RunResult(0, pw, "")
        return RunResult(1, "", "unknown")

    return PipeWireRouterBackend(run=fake_run, sysfs_sound=str(sound))


def test_resolve_raw_pcm_aloop_node_by_card_id_when_api_alsa_card_absent(tmp_path):
    # Reproduces the Session E QEMU failure: snd-aloop raw-PCM nodes (use-acp=false) carry
    # NO api.alsa.card == <int> — they identify the card via object.path "alsa:pcm:<id>:..".
    # The old `api.alsa.card == card` check returned None though the nodes clearly existed.
    b = _aloop_backend(
        tmp_path,
        rx_props={
            "media.class": "Audio/Source",
            "node.name": "oe5xrx.slot1",
            "object.path": "alsa:pcm:oe5xrxslot1:1:capture",
        },
        tx_props={
            "media.class": "Audio/Sink",
            "node.name": "oe5xrx.slot1.tx",
            "object.path": "alsa:pcm:oe5xrxslot1:1:playback",
        },
    )
    assert b.resolve_node(1, "rx") == "oe5xrx.slot1"
    assert b.resolve_node(1, "tx") == "oe5xrx.slot1.tx"


def test_resolve_matches_string_typed_api_alsa_card(tmp_path):
    # pw-dump may emit api.alsa.card as a STRING; the int == "7" comparison must not fail.
    b = _aloop_backend(
        tmp_path,
        rx_props={
            "media.class": "Audio/Source",
            "node.name": "oe5xrx.slot1",
            "api.alsa.card": "7",
        },
        tx_props={
            "media.class": "Audio/Sink",
            "node.name": "oe5xrx.slot1.tx",
            "api.alsa.card": "7",
        },
    )
    assert b.resolve_node(1, "rx") == "oe5xrx.slot1"
    assert b.resolve_node(1, "tx") == "oe5xrx.slot1.tx"


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


def _mixed_card_backend(tmp_path):
    """sysfs with a FOREIGN untagged card (HDA-like, no `id` file, no OE5XRX_SLOT) next to a
    tagged aloop card — mirrors the QEMU box where an Intel-HDA card0 sits beside snd-aloop."""
    sound = tmp_path / "sound"
    (sound / "card0").mkdir(parents=True)  # foreign HDA: NO id file, NOT tagged
    (sound / "card7").mkdir(parents=True)  # tagged aloop
    (sound / "card7" / "id").write_text("oe5xrxslot1\n")
    pw = json.dumps(
        [
            {
                "id": 60,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "props": {
                        "media.class": "Audio/Source",
                        "api.alsa.card": 0,
                        "node.name": "alsa_input.pci-hda",
                    }
                },
            },
            {
                "id": 61,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "props": {
                        "media.class": "Audio/Source",
                        "node.name": "oe5xrx.slot1",
                        "object.path": "alsa:pcm:oe5xrxslot1:1:capture",
                    }
                },
            },
        ]
    )

    def fake_run(argv, timeout=5.0):
        if argv[0] == "udevadm":
            cidx = int(argv[-1].rsplit("card", 1)[1])
            # Foreign card returns properties but NO OE5XRX_SLOT; tagged card carries it.
            return RunResult(0, "OE5XRX_SLOT=1\n" if cidx == 7 else "ID_BUS=pci\n", "")
        if argv[0] == "pw-dump":
            return RunResult(0, pw, "")
        return RunResult(1, "", "unknown")

    return PipeWireRouterBackend(run=fake_run, sysfs_sound=str(sound))


def test_foreign_untagged_card_is_skipped_and_does_not_crash(tmp_path):
    # RC#2 hardening: a foreign sound card (no OE5XRX_SLOT, no `id` sysfs attr) must never
    # crash enumeration; only the tagged slot is returned.
    b = _mixed_card_backend(tmp_path)
    assert b.list_audio_slots() == [1]


def test_resolve_ignores_foreign_card_node(tmp_path):
    # The tagged aloop RX node resolves even with a foreign HDA source node also present.
    b = _mixed_card_backend(tmp_path)
    assert b.resolve_node(1, "rx") == "oe5xrx.slot1"


def test_enumeration_fails_closed_on_missing_sysfs_base(tmp_path):
    # Defensive: a None/empty sysfs base must fail closed (empty list / None), never
    # os.path.join(None, ...) or open(None).
    for bad in (None, ""):
        b = PipeWireRouterBackend(run=lambda *a, **k: RunResult(1, "", ""), sysfs_sound=bad)
        assert b.list_audio_slots() == []
        assert b.resolve_node(1, "rx") is None
        assert b.alsa_card_for_slot(1) is None
