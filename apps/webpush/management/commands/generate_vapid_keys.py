"""Generate a VAPID keypair for Web-Push.

Outputs env-ready lines. The private key is the base64url-encoded raw
32-byte EC-P256 private scalar (the form pywebpush accepts as a string);
the public key is the base64url-encoded uncompressed point used by the
browser as ``applicationServerKey``. Keys go into env/secrets — never DB
or repo (mirrors the OIDC-key handling).
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.management.base import BaseCommand


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class Command(BaseCommand):
    help = "Generate a VAPID keypair for Web-Push notifications."

    def handle(self, *args, **options):
        key = ec.generate_private_key(ec.SECP256R1())
        priv_raw = key.private_numbers().private_value.to_bytes(32, "big")
        pub_point = key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        self.stdout.write(f"WEBPUSH_VAPID_PUBLIC_KEY={_b64url(pub_point)}")
        self.stdout.write(f"WEBPUSH_VAPID_PRIVATE_KEY={_b64url(priv_raw)}")
        self.stdout.write(
            "WEBPUSH_VAPID_ADMIN_EMAIL=mailto:admin@oe5xrx.org  # adjust"
        )
