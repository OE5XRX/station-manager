from rest_framework import serializers


class _AnyJSONField(serializers.Field):
    """Accept any JSON value — shape validation happens downstream in ingest_telemetry."""

    def to_internal_value(self, data):
        return data

    def to_representation(self, value):
        return value


class HeartbeatSerializer(serializers.Serializer):
    hostname = serializers.CharField(max_length=255)
    os_version = serializers.CharField(max_length=255)
    uptime = serializers.FloatField()
    module_versions = serializers.DictField(child=serializers.CharField())
    ip_address = serializers.IPAddressField()
    agent_version = serializers.CharField(max_length=32, required=False, default="")
    image_variant = serializers.CharField(max_length=32, required=False, default="")
    timestamp = serializers.FloatField(required=False, default=None)
    inventory = serializers.DictField(required=False, default=dict)
    telemetry = _AnyJSONField(required=False)


class HealthSerializer(serializers.Serializer):
    status = serializers.CharField()
