import hashlib
import time

from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from apps.api.models import DeviceKey, PersonalAccessToken


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


class PersonalAccessTokenAuthentication(BaseAuthentication):
    """Bearer-token auth for the user/automation API.

    Header: ``Authorization: Bearer <raw_token>``. Resolves the owning
    user and sets request.auth to the PersonalAccessToken. Returns None
    for a non-Bearer header so other authenticators can try.
    """

    keyword = "Bearer"

    def authenticate(self, request):
        auth = get_authorization_header(request).split()
        if not auth:
            return None
        try:
            scheme = auth[0].decode("ascii")
        except UnicodeDecodeError:
            raise AuthenticationFailed("Invalid bearer header.")
        if scheme.lower() != self.keyword.lower():
            return None
        if len(auth) != 2:
            raise AuthenticationFailed("Invalid bearer header.")
        try:
            raw = auth[1].decode("ascii")
        except UnicodeDecodeError:
            raise AuthenticationFailed("Invalid bearer header.")
        token_hash = PersonalAccessToken.hash_token(raw)
        try:
            token = PersonalAccessToken.objects.select_related("user").get(token_hash=token_hash)
        except PersonalAccessToken.DoesNotExist:
            raise AuthenticationFailed("Invalid token.")

        if not token.is_active():
            raise AuthenticationFailed("Token expired or revoked.")
        if not token.user.is_active:
            raise AuthenticationFailed("Token owner is inactive.")

        # best-effort last-used stamp; never break the request on failure
        try:
            PersonalAccessToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
        except Exception:  # noqa: BLE001 - telemetry only, must not 500
            pass

        return (token.user, token)

    def authenticate_header(self, request):
        return self.keyword


class PersonalAccessTokenScheme(OpenApiAuthenticationExtension):
    """Expose PersonalAccessTokenAuthentication as an HTTP Bearer scheme.

    Without this extension drf-spectacular omits the bearer security scheme
    from the generated OpenAPI document, so generated clients and the
    Swagger "Authorize" flow cannot authenticate with personal access tokens.
    """

    target_class = "apps.api.authentication.PersonalAccessTokenAuthentication"
    name = "PersonalAccessToken"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer"}


class DeviceKeyScheme(OpenApiAuthenticationExtension):
    """Document the Ed25519 DeviceKey authenticator in the OpenAPI schema.

    DeviceKeyAuthentication is a custom authenticator, so without this
    extension drf-spectacular cannot resolve it and emits the agent
    operations without any security scheme. The signature auth spans three
    request headers; the primary ``Authorization`` header is modelled as an
    apiKey scheme and the companion headers are described in prose.
    """

    target_class = "apps.api.authentication.DeviceKeyAuthentication"
    name = "DeviceKey"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "Authorization",
            "description": (
                "Ed25519 device-signature auth. Send `Authorization: DeviceKey "
                "<station_id>` together with the `X-Device-Signature` "
                "(base64-encoded Ed25519 signature) and `X-Device-Timestamp` "
                "(unix seconds) headers. The signed payload is "
                "`{timestamp}:{sha256(body)}`."
            ),
        }
