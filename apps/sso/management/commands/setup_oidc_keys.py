"""Bootstrap the RSA private key used to sign OIDC ID tokens.

Idempotent by design — re-running on a host that already has a key
must NOT regenerate it (that would invalidate every live token).
Pass --force only when an operator deliberately wants to rotate.
"""

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create the RSA-2048 private key for OIDC ID-token signing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            default=None,
            help="Override the destination path (default: settings.OIDC_RSA_KEY_PATH).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Regenerate even if a key already exists at the target path.",
        )

    def handle(self, *args, path=None, force=False, **options):
        from django.conf import settings

        target = Path(path or settings.OIDC_RSA_KEY_PATH)
        if target.exists() and not force:
            self.stdout.write(f"Key already present at {target}; nothing to do.")
            return

        target.parent.mkdir(parents=True, exist_ok=True)

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        target.write_bytes(pem)
        target.chmod(0o600)
        self.stdout.write(self.style.SUCCESS(f"Wrote RSA-2048 private key to {target}."))
