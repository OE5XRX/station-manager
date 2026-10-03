"""Explicit writable serializers for the user/automation API (v1).

Curated writable fields only — server-side actor fields and computed/
status/secret fields are read-only or absent. No ``fields="__all__"``.
"""

from rest_framework import serializers

from apps.stations.models import Region, Station, StationTag


class StationWriteSerializer(serializers.ModelSerializer):
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
            "status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "status", "created_at", "updated_at"]


class RegionWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Region
        fields = ["id", "name", "slug", "description", "created_at"]
        read_only_fields = ["id", "created_at"]


class StationTagWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = StationTag
        fields = ["id", "name", "slug", "color", "description", "created_at"]
        read_only_fields = ["id", "created_at"]
