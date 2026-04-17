from rest_framework import serializers


class HeartbeatSerializer(serializers.Serializer):
    hostname = serializers.CharField(max_length=255)
    os_version = serializers.CharField(max_length=255)
    uptime = serializers.FloatField()
    module_versions = serializers.DictField(child=serializers.CharField())
    ip_address = serializers.IPAddressField()
    agent_version = serializers.CharField(max_length=32, required=False, default="")
    timestamp = serializers.FloatField(required=False, default=None)
    inventory = serializers.DictField(required=False, default=dict)


class HealthSerializer(serializers.Serializer):
    status = serializers.CharField()
