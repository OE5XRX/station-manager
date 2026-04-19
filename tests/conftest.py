import base64
import hashlib
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import User
from apps.api.models import DeviceKey
from apps.deployments.models import Deployment, DeploymentResult
from apps.firmware.models import FirmwareArtifact
from apps.monitoring.models import Alert, AlertRule
from apps.stations.models import Station


def _make_ed25519_keypair():
    """Generate an Ed25519 keypair for tests.

    Returns:
        tuple: (private_key, private_pem_bytes, public_key_b64_str)
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
    return private_key, private_pem, public_b64


def _sign_body(private_key: Ed25519PrivateKey, body_bytes: bytes, timestamp=None):
    """Sign a request body and return signature headers values.

    Returns:
        tuple: (timestamp_str, signature_b64)
    """
    if timestamp is None:
        timestamp = str(time.time())
    else:
        timestamp = str(timestamp)
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    signed_data = f"{timestamp}:{body_hash}".encode()
    signature = private_key.sign(signed_data)
    signature_b64 = base64.b64encode(signature).decode("ascii")
    return timestamp, signature_b64


def device_auth_headers(private_key: Ed25519PrivateKey, station_id: int, body_bytes: bytes = b""):
    """Build the three auth headers for a DeviceKey-authenticated request."""
    timestamp, signature_b64 = _sign_body(private_key, body_bytes)
    return {
        "HTTP_AUTHORIZATION": f"DeviceKey {station_id}",
        "HTTP_X_DEVICE_SIGNATURE": signature_b64,
        "HTTP_X_DEVICE_TIMESTAMP": timestamp,
    }


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        username="admin",
        password="testpass123",
        role="admin",
    )


@pytest.fixture
def operator_user(db):
    return User.objects.create_user(
        username="operator",
        password="testpass123",
        role="operator",
    )


@pytest.fixture
def member_user(db):
    return User.objects.create_user(
        username="member",
        password="testpass123",
        role="member",
    )


@pytest.fixture
def station(db):
    return Station.objects.create(name="Test Station", callsign="OE5XRX")


@pytest.fixture
def station_with_key(station):
    """Station with a DeviceKey linked. Returns (station, private_key)."""
    private_key, _, public_b64 = _make_ed25519_keypair()
    DeviceKey.objects.create(
        station=station,
        current_public_key=public_b64,
    )
    return station, private_key


@pytest.fixture
def firmware_artifact(db, operator_user):
    """A FirmwareArtifact with a small dummy file."""
    dummy_file = SimpleUploadedFile(
        "firmware-test.bin",
        b"\x00\x01\x02\x03" * 64,
        content_type="application/octet-stream",
    )
    artifact = FirmwareArtifact(
        name="test-firmware",
        version="1.0.0",
        artifact_type=FirmwareArtifact.ArtifactType.OS_IMAGE,
        file=dummy_file,
        uploaded_by=operator_user,
    )
    artifact.save()
    return artifact


@pytest.fixture
def image_release(db):
    """An ImageRelease marked as latest for qemux86-64."""
    from apps.images.models import ImageRelease

    return ImageRelease.objects.create(
        tag="v1-alpha",
        machine="qemux86-64",
        s3_key="images/v1-alpha/qemux86-64.wic.bz2",
        sha256="a" * 64,
        size_bytes=1000,
        is_latest=True,
    )


@pytest.fixture
def deployment(image_release, station, operator_user):
    """An in-progress Deployment targeting a single station."""
    dep = Deployment.objects.create(
        image_release=image_release,
        target_type=Deployment.TargetType.STATION,
        target_station=station,
        status=Deployment.Status.IN_PROGRESS,
        created_by=operator_user,
    )
    return dep


@pytest.fixture
def deployment_result(deployment, station):
    """A pending DeploymentResult for the deployment/station pair."""
    return DeploymentResult.objects.create(
        deployment=deployment,
        station=station,
        status=DeploymentResult.Status.PENDING,
        previous_version="0.9.0",
    )


@pytest.fixture
def offline_alert_rule(db):
    """An active AlertRule for station_offline."""
    return AlertRule.objects.create(
        alert_type=AlertRule.AlertType.STATION_OFFLINE,
        threshold=0,
        severity=AlertRule.Severity.CRITICAL,
        is_active=True,
        description="Station offline check",
    )


@pytest.fixture
def cpu_temp_alert_rule(db):
    """An active AlertRule for cpu_temperature with threshold 80."""
    return AlertRule.objects.create(
        alert_type=AlertRule.AlertType.CPU_TEMPERATURE,
        threshold=80.0,
        severity=AlertRule.Severity.WARNING,
        is_active=True,
        description="CPU temperature check",
    )


@pytest.fixture
def disk_warning_alert_rule(db):
    """An active AlertRule for disk_warning with threshold 90."""
    return AlertRule.objects.create(
        alert_type=AlertRule.AlertType.DISK_WARNING,
        threshold=90.0,
        severity=AlertRule.Severity.WARNING,
        is_active=True,
        description="Disk warning check",
    )


@pytest.fixture
def ram_critical_alert_rule(db):
    """An active AlertRule for ram_critical with threshold 90."""
    return AlertRule.objects.create(
        alert_type=AlertRule.AlertType.RAM_CRITICAL,
        threshold=90.0,
        severity=AlertRule.Severity.CRITICAL,
        is_active=True,
        description="RAM critical check",
    )


@pytest.fixture
def ota_failed_alert_rule(db):
    """An active AlertRule for ota_failed."""
    return AlertRule.objects.create(
        alert_type=AlertRule.AlertType.OTA_FAILED,
        threshold=0,
        severity=AlertRule.Severity.CRITICAL,
        is_active=True,
        description="OTA failure check",
    )


@pytest.fixture
def alert(station, offline_alert_rule):
    """An unresolved alert for the station."""
    return Alert.objects.create(
        station=station,
        alert_rule=offline_alert_rule,
        severity=AlertRule.Severity.CRITICAL,
        title=f"Station offline: {station.name}",
        message="Test alert message",
    )
