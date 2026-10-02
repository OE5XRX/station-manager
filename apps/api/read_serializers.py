"""Explicit read-only serializers for the user/automation API (v1).

No ``fields="__all__"`` anywhere — sensitive fields are structurally absent.
"""

from rest_framework import serializers

from apps.stations.models import Region, Station, StationTag


class RegionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Region
        fields = ["id", "name", "slug", "description", "created_at"]


class StationTagSerializer(serializers.ModelSerializer):
    class Meta:
        model = StationTag
        fields = ["id", "name", "slug", "color", "description", "created_at"]


class StationSerializer(serializers.ModelSerializer):
    is_online = serializers.BooleanField(read_only=True)

    class Meta:
        model = Station
        fields = [
            "id",
            "name",
            "callsign",
            "description",
            "location_name",
            "latitude",
            "longitude",
            "altitude",
            "hardware_revision",
            "region",
            "tags",
            "notes",
            "current_os_version",
            "current_agent_version",
            "current_image_variant",
            "last_ip_address",
            "last_seen",
            "status",
            "current_image_release",
            "is_online",
            "created_at",
            "updated_at",
        ]
