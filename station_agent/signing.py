"""Ed25519 request signing for the Station Agent.

Signs HTTP requests so the server can verify they originate from
a trusted device. Uses the Ed25519 algorithm with PEM-encoded keys.
"""

import base64
import hashlib
import logging
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

logger = logging.getLogger(__name__)


def load_private_key(key_path: str) -> Ed25519PrivateKey | None:
    """Load an Ed25519 private key from a PEM file.

    Args:
        key_path: Filesystem path to the PEM-encoded private key.

    Returns:
        The loaded private key, or None if the file cannot be read.
    """
    try:
        with open(key_path, "rb") as f:
            key_data = f.read()
        private_key = load_pem_private_key(key_data, password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            logger.error("Key at %s is not an Ed25519 key", key_path)
            return None
        logger.info("Loaded Ed25519 private key from %s", key_path)
        return private_key
    except FileNotFoundError:
        logger.error("Private key file not found: %s", key_path)
        return None
    except Exception as exc:
        logger.error("Failed to load private key from %s: %s", key_path, exc)
        return None


def sign_request(
    private_key: Ed25519PrivateKey, station_id: int, body_bytes: bytes
) -> dict[str, str]:
    """Sign an HTTP request body and return authentication headers.

    The server expects:
        Authorization: DeviceKey <station_id>
        X-Device-Signature: <base64(signature)>
        X-Device-Timestamp: <unix_timestamp>

    The signed data is: "{timestamp}:{sha256(body).hexdigest()}".encode()

    Args:
        private_key: The Ed25519 private key to sign with.
        station_id: The numeric station identifier.
        body_bytes: The raw request body (use b"" for empty bodies).

    Returns:
        Dict of headers to merge into the request.
    """
    timestamp = str(time.time())
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    signed_data = f"{timestamp}:{body_hash}".encode()

    signature = private_key.sign(signed_data)
    signature_b64 = base64.b64encode(signature).decode("ascii")

    return {
        "Authorization": f"DeviceKey {station_id}",
        "X-Device-Signature": signature_b64,
        "X-Device-Timestamp": timestamp,
    }
