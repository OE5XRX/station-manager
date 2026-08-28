import base64
from io import StringIO

from django.core.management import call_command


def _b64url_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_generate_vapid_keys_emits_valid_pair():
    out = StringIO()
    call_command("generate_vapid_keys", stdout=out)
    lines = dict(
        line.split("=", 1) for line in out.getvalue().splitlines() if "=" in line
    )
    assert "WEBPUSH_VAPID_PUBLIC_KEY" in lines
    assert "WEBPUSH_VAPID_PRIVATE_KEY" in lines
    # raw scalar is 32 bytes; uncompressed point is 65 bytes (0x04 + X + Y)
    assert len(_b64url_decode(lines["WEBPUSH_VAPID_PRIVATE_KEY"])) == 32
    pub = _b64url_decode(lines["WEBPUSH_VAPID_PUBLIC_KEY"])
    assert len(pub) == 65 and pub[0] == 0x04
