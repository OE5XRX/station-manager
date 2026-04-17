import base64
import hashlib
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from django.urls import reverse

from apps.api.models import DeviceKey
from apps.stations.models import Station


def _make_ed25519_keypair():
    """Helper: generate a raw Ed25519 keypair for testing."""
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
    return private_key, private_pem, public_b64


def _sign_request(private_key, body_bytes, timestamp=None):
    """Helper: create signature headers for a request."""
    if timestamp is None:
        timestamp = str(time.time())
    else:
        timestamp = str(timestamp)

    body_hash = hashlib.sha256(body_bytes).hexdigest()
    signed_data = f"{timestamp}:{body_hash}".encode()
    signature = private_key.sign(signed_data)
    signature_b64 = base64.b64encode(signature).decode("ascii")
    return timestamp, signature_b64


@pytest.fixture
def ed25519_station(db):
    return Station.objects.create(name="Test Station", callsign="OE5XRX")


@pytest.fixture
def ed25519_setup(ed25519_station):
    """Create a station with an Ed25519 DeviceKey and return test data."""
    private_key, private_pem, public_b64 = _make_ed25519_keypair()
    device_key = DeviceKey.objects.create(
        station=ed25519_station,
        current_public_key=public_b64,
    )
    return {
        "station": ed25519_station,
        "device_key": device_key,
        "private_key": private_key,
        "private_pem": private_pem,
        "public_b64": public_b64,
    }


@pytest.mark.django_db
class TestGenerateKeypair:
    def test_generate_keypair(self):
        """Verify key generation produces valid PEM and base64 public key."""
        private_pem, public_b64 = DeviceKey.generate_keypair()

        assert private_pem.startswith(b"-----BEGIN PRIVATE KEY-----")
        assert private_pem.strip().endswith(b"-----END PRIVATE KEY-----")

        # Public key should be valid base64 and 32 bytes (Ed25519)
        pub_bytes = base64.b64decode(public_b64)
        assert len(pub_bytes) == 32

    def test_generate_keypair_unique(self):
        """Each call should produce a different keypair."""
        _, pub1 = DeviceKey.generate_keypair()
        _, pub2 = DeviceKey.generate_keypair()
        assert pub1 != pub2


@pytest.mark.django_db
class TestEd25519AuthValidSignature:
    def test_ed25519_auth_valid_signature(self, client, ed25519_setup):
        """Full auth flow with a valid Ed25519 signature."""
        setup = ed25519_setup
        body = json.dumps(
            {
                "hostname": "station-01",
                "os_version": "Yocto 4.0",
                "uptime": 3600.0,
                "ip_address": "192.168.1.100",
                "module_versions": {},
            }
        ).encode("utf-8")

        timestamp, signature_b64 = _sign_request(setup["private_key"], body)

        response = client.post(
            reverse("api:heartbeat"),
            data=body,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"DeviceKey {setup['station'].pk}",
            HTTP_X_DEVICE_SIGNATURE=signature_b64,
            HTTP_X_DEVICE_TIMESTAMP=timestamp,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


@pytest.mark.django_db
class TestEd25519AuthInvalidSignature:
    def test_ed25519_auth_invalid_signature(self, client, ed25519_setup):
        """Wrong key should return 401."""
        setup = ed25519_setup
        body = json.dumps(
            {
                "hostname": "station-01",
                "os_version": "Yocto 4.0",
                "uptime": 3600.0,
                "ip_address": "192.168.1.100",
                "module_versions": {},
            }
        ).encode("utf-8")

        # Sign with a DIFFERENT key
        wrong_private_key = Ed25519PrivateKey.generate()
        timestamp, signature_b64 = _sign_request(wrong_private_key, body)

        response = client.post(
            reverse("api:heartbeat"),
            data=body,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"DeviceKey {setup['station'].pk}",
            HTTP_X_DEVICE_SIGNATURE=signature_b64,
            HTTP_X_DEVICE_TIMESTAMP=timestamp,
        )
        assert response.status_code == 401


@pytest.mark.django_db
class TestEd25519AuthExpiredTimestamp:
    def test_ed25519_auth_expired_timestamp(self, client, ed25519_setup):
        """Timestamp older than 60 seconds should return 401."""
        setup = ed25519_setup
        body = json.dumps(
            {
                "hostname": "station-01",
                "os_version": "Yocto 4.0",
                "uptime": 3600.0,
                "ip_address": "192.168.1.100",
                "module_versions": {},
            }
        ).encode("utf-8")

        # Use a timestamp 120 seconds in the past
        old_timestamp = time.time() - 120
        timestamp, signature_b64 = _sign_request(
            setup["private_key"], body, timestamp=old_timestamp
        )

        response = client.post(
            reverse("api:heartbeat"),
            data=body,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"DeviceKey {setup['station'].pk}",
            HTTP_X_DEVICE_SIGNATURE=signature_b64,
            HTTP_X_DEVICE_TIMESTAMP=timestamp,
        )
        assert response.status_code == 401


@pytest.mark.django_db
class TestEd25519NextKeyMatches:
    def test_ed25519_next_key_matches(self, client, ed25519_station):
        """Verify authentication works with next_public_key (A/B rotation)."""
        # Create device key with current key
        current_private, _, current_pub_b64 = _make_ed25519_keypair()
        # Create a "next" keypair
        next_private, _, next_pub_b64 = _make_ed25519_keypair()

        DeviceKey.objects.create(
            station=ed25519_station,
            current_public_key=current_pub_b64,
            next_public_key=next_pub_b64,
        )

        body = json.dumps(
            {
                "hostname": "station-01",
                "os_version": "Yocto 4.0",
                "uptime": 3600.0,
                "ip_address": "192.168.1.100",
                "module_versions": {},
            }
        ).encode("utf-8")

        # Sign with the NEXT key (simulating a rotated device)
        timestamp, signature_b64 = _sign_request(next_private, body)

        response = client.post(
            reverse("api:heartbeat"),
            data=body,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"DeviceKey {ed25519_station.pk}",
            HTTP_X_DEVICE_SIGNATURE=signature_b64,
            HTTP_X_DEVICE_TIMESTAMP=timestamp,
        )
        assert response.status_code == 200


@pytest.mark.django_db
class TestHeartbeatWithEd25519:
    def test_heartbeat_with_ed25519(self, client, ed25519_setup):
        """Full heartbeat with Ed25519 updates station fields."""
        setup = ed25519_setup
        station = setup["station"]

        body = json.dumps(
            {
                "hostname": "station-01",
                "os_version": "Yocto 5.0",
                "uptime": 7200.0,
                "ip_address": "10.0.0.42",
                "module_versions": {"fm_trx": "1.2.3"},
                "agent_version": "0.3.0",
            }
        ).encode("utf-8")

        timestamp, signature_b64 = _sign_request(setup["private_key"], body)

        response = client.post(
            reverse("api:heartbeat"),
            data=body,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"DeviceKey {station.pk}",
            HTTP_X_DEVICE_SIGNATURE=signature_b64,
            HTTP_X_DEVICE_TIMESTAMP=timestamp,
        )
        assert response.status_code == 200

        # Verify station was updated
        station.refresh_from_db()
        assert station.current_os_version == "Yocto 5.0"
        assert station.current_agent_version == "0.3.0"
        assert station.last_ip_address == "10.0.0.42"
        assert station.status == "online"
        assert station.last_seen is not None


@pytest.mark.django_db
class TestVerifySignatureMethod:
    def test_verify_signature_valid(self):
        """DeviceKey.verify_signature returns True for valid signature."""
        private_key, _, public_b64 = _make_ed25519_keypair()
        data = b"test data to sign"
        sig = private_key.sign(data)
        sig_b64 = base64.b64encode(sig).decode("ascii")
        assert DeviceKey.verify_signature(public_b64, sig_b64, data) is True

    def test_verify_signature_invalid(self):
        """DeviceKey.verify_signature returns False for invalid signature."""
        _, _, public_b64 = _make_ed25519_keypair()
        wrong_key = Ed25519PrivateKey.generate()
        data = b"test data to sign"
        sig = wrong_key.sign(data)
        sig_b64 = base64.b64encode(sig).decode("ascii")
        assert DeviceKey.verify_signature(public_b64, sig_b64, data) is False

    def test_verify_signature_corrupted(self):
        """DeviceKey.verify_signature returns False for corrupted input."""
        assert DeviceKey.verify_signature("not-valid-b64!", "also-bad!", b"data") is False
