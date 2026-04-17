import base64

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from django.db import models


class DeviceKey(models.Model):
    """Ed25519 asymmetric key pair for station agent device authentication.

    Only the public key is stored server-side. The private key is generated
    once, shown to the operator, and never stored on the server.
    Supports A/B key rotation via current_public_key / next_public_key.
    """

    station = models.OneToOneField(
        "stations.Station",
        on_delete=models.CASCADE,
        related_name="device_key",
    )
    current_public_key = models.TextField(
        help_text="Base64-encoded Ed25519 public key",
    )
    next_public_key = models.TextField(
        null=True,
        blank=True,
        help_text="Pending key for A/B rotation",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "device key"
        verbose_name_plural = "device keys"

    def __str__(self):
        station_name = str(self.station) if self.station_id else "unlinked"
        return f"{station_name} ({self.current_public_key[:16]}...)"

    @property
    def is_authenticated(self):
        """Required by DRF throttling when key is set as request.user."""
        return True

    @staticmethod
    def generate_keypair():
        """Generate an Ed25519 keypair.

        Returns:
            tuple: (private_key_pem_bytes, public_key_b64_str)
                - private_key_pem_bytes: PEM-encoded private key as bytes
                - public_key_b64_str: Base64-encoded raw public key as str
        """
        private_key = Ed25519PrivateKey.generate()
        private_pem = private_key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption(),
        )
        public_raw = private_key.public_key().public_bytes(
            encoding=Encoding.Raw,
            format=PublicFormat.Raw,
        )
        public_b64 = base64.b64encode(public_raw).decode("ascii")
        return (private_pem, public_b64)

    @staticmethod
    def verify_signature(public_key_b64, signature_b64, data_bytes):
        """Verify an Ed25519 signature against a public key.

        Args:
            public_key_b64: Base64-encoded raw Ed25519 public key
            signature_b64: Base64-encoded signature
            data_bytes: The signed data as bytes

        Returns:
            bool: True if signature is valid, False otherwise
        """
        try:
            pub_bytes = base64.b64decode(public_key_b64)
            signature = base64.b64decode(signature_b64)
            public_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
            public_key.verify(signature, data_bytes)
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False
