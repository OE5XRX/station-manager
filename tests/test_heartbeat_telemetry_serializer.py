from apps.api.serializers import HeartbeatSerializer

BASE = dict(hostname="h", os_version="o", uptime=1.0,
            module_versions={}, ip_address="10.0.0.1")


def test_serializer_accepts_telemetry():
    s = HeartbeatSerializer(data={**BASE, "telemetry": {"boot": {"boot_count": 2}}})
    assert s.is_valid(), s.errors
    assert s.validated_data["telemetry"]["boot"]["boot_count"] == 2


def test_serializer_telemetry_optional():
    s = HeartbeatSerializer(data=BASE)
    assert s.is_valid(), s.errors
    assert "telemetry" not in s.validated_data or s.validated_data.get("telemetry") in ({}, None)
