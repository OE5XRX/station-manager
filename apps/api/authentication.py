import hashlib
import time

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from apps.api.models import DeviceKey


class DeviceKeyAuthentication(BaseAuthentication):
    """
    Ed25519 signature-based authentication for station agent devices.

    Expects three headers:
        Authorization: DeviceKey <station_id>
        X-Device-Signature: <base64-encoded Ed25519 signature>
        X-Device-Timestamp: <unix_timestamp>

    The signed data is: "{timestamp}:{sha256(request_body).hexdigest()}"

    Replay protection: timestamps older than 60 seconds are rejected.
    Supports A/B key rotation: tries current_public_key first, then
    next_public_key. Sets request._device_key_used_next if next key matched.
    """

    keyword = "DeviceKey"
    TIMESTAMP_TOLERANCE = 60  # seconds

    def authenticate(self, request):
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header:
            return None

        parts = auth_header.split()

        if len(parts) != 2 or parts[0] != self.keyword:
            return None

        station_id = parts[1]

        # Validate station_id is numeric
        try:
            station_id = int(station_id)
        except (ValueError, TypeError):
            return None

        # Extract required headers
        signature_b64 = request.META.get("HTTP_X_DEVICE_SIGNATURE")
        timestamp_str = request.META.get("HTTP_X_DEVICE_TIMESTAMP")

        if not signature_b64 or not timestamp_str:
            raise AuthenticationFailed("Missing X-Device-Signature or X-Device-Timestamp header.")

        # Validate timestamp (replay protection)
        try:
            timestamp = float(timestamp_str)
        except (ValueError, TypeError):
            raise AuthenticationFailed("Invalid timestamp format.")

        now = time.time()
        if timestamp > now + 5:  # max 5s clock skew into future
            raise AuthenticationFailed("Timestamp is in the future.")
        if now - timestamp > self.TIMESTAMP_TOLERANCE:
            raise AuthenticationFailed("Timestamp expired. Request must be within 60 seconds.")

        # Reconstruct signed data
        body_hash = hashlib.sha256(request.body).hexdigest()
        signed_data = f"{timestamp_str}:{body_hash}".encode()

        # Look up device key
        try:
            device_key = DeviceKey.objects.select_related("station").get(
                station_id=station_id, is_active=True
            )
        except DeviceKey.DoesNotExist:
            raise AuthenticationFailed("No active device key for this station.")

        # Try current key first
        if DeviceKey.verify_signature(device_key.current_public_key, signature_b64, signed_data):
            DeviceKey.objects.filter(pk=device_key.pk).update(last_seen=timezone.now())
            return (device_key, device_key)

        # Try next key (A/B rotation)
        if device_key.next_public_key and DeviceKey.verify_signature(
            device_key.next_public_key, signature_b64, signed_data
        ):
            DeviceKey.objects.filter(pk=device_key.pk).update(last_seen=timezone.now())
            request._device_key_used_next = True
            return (device_key, device_key)

        raise AuthenticationFailed("Invalid signature.")

    def authenticate_header(self, request):
        return self.keyword
