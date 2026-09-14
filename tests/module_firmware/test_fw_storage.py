from apps.module_firmware import storage


def test_key_scheme():
    assert (
        storage.release_key("fm", "26.07.04-01", "vhf")
        == "module_firmware/fm/26.07.04-01/vhf.signed.bin"
    )
    assert (
        storage.bundle_key("fm", "26.07.04-01", "vhf")
        == "module_firmware/fm/26.07.04-01/vhf.signed.bin.bundle"
    )


def test_key_scheme_bandless():
    assert (
        storage.release_key("power", "1.0.0-00", "")
        == "module_firmware/power/1.0.0-00/_.signed.bin"
    )
