from apps.module_firmware.releases import parse_sha256sums, parse_variant_assets


def test_parse_two_variants():
    names = {
        "fm-sa818-vhf.signed.bin",
        "fm-sa818-vhf.signed.bin.bundle",
        "fm-sa818-uhf.signed.bin",
        "fm-sa818-uhf.signed.bin.bundle",
        "fm-sa818-vhf.mcuboot.hex",
        "SHA256SUMS",
        "SHA256SUMS.bundle",
    }
    got = {
        v.variant: (v.signed_name, v.bundle_name) for v in parse_variant_assets(names, "fm-sa818")
    }
    assert got == {
        "vhf": ("fm-sa818-vhf.signed.bin", "fm-sa818-vhf.signed.bin.bundle"),
        "uhf": ("fm-sa818-uhf.signed.bin", "fm-sa818-uhf.signed.bin.bundle"),
    }


def test_parse_bandless():
    names = {"power.signed.bin", "power.signed.bin.bundle"}
    got = parse_variant_assets(names, "power")
    assert len(got) == 1 and got[0].variant == "" and got[0].signed_name == "power.signed.bin"


def test_parse_skips_signed_without_bundle():
    assert parse_variant_assets({"fm-sa818-vhf.signed.bin"}, "fm-sa818") == []


def test_parse_sha256sums():
    text = "aaaa  fm-sa818-vhf.signed.bin\nbbbb  fm-sa818-uhf.signed.bin\n"
    d = parse_sha256sums(text)
    assert d["fm-sa818-vhf.signed.bin"] == "aaaa"


# ---------------------------------------------------------------------------
# Finding 4: unsafe variant token rejection
# ---------------------------------------------------------------------------


def test_parse_underscore_variant_skipped():
    """'_' collides with the bandless storage segment — must be rejected."""
    names = {"fm-sa818-_.signed.bin", "fm-sa818-_.signed.bin.bundle"}
    assert parse_variant_assets(names, "fm-sa818") == []


def test_parse_dotdot_variant_skipped():
    """Path-traversal token '../evil' must be rejected."""
    names = {"fm-sa818-../evil.signed.bin", "fm-sa818-../evil.signed.bin.bundle"}
    assert parse_variant_assets(names, "fm-sa818") == []


def test_parse_safe_variant_vhf_accepted():
    """'vhf' is a valid lowercase-alnum variant and must be accepted."""
    names = {"fm-sa818-vhf.signed.bin", "fm-sa818-vhf.signed.bin.bundle"}
    got = parse_variant_assets(names, "fm-sa818")
    assert len(got) == 1
    assert got[0].variant == "vhf"


def test_parse_bandless_still_valid():
    """Bandless variant '' (no '-' suffix) must remain accepted after the safety check."""
    names = {"fm-sa818.signed.bin", "fm-sa818.signed.bin.bundle"}
    got = parse_variant_assets(names, "fm-sa818")
    assert len(got) == 1
    assert got[0].variant == ""
