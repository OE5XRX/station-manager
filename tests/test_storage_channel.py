from apps.images import storage


def test_release_key_includes_channel():
    assert storage.release_key("v1", "qemux86-64", "dev") == "images/v1/dev/qemux86-64.wic.bz2"


def test_release_key_defaults_to_release():
    assert storage.release_key("v1", "qemux86-64") == "images/v1/release/qemux86-64.wic.bz2"


def test_bundle_and_rootfs_keys():
    assert (
        storage.release_bundle_key("v1", "qemux86-64", "dev")
        == "images/v1/dev/qemux86-64.wic.bz2.bundle"
    )
    assert (
        storage.release_rootfs_key("v1", "qemux86-64", "dev")
        == "images/v1/dev/qemux86-64.rootfs.bz2"
    )
