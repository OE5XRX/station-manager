"""Tests for the new topology models: Region, StationAssignment, RegionAssignment."""

import pytest
from django.db import IntegrityError

from apps.stations.models import Region, Station


@pytest.mark.django_db
class TestRegion:
    def test_str(self):
        r = Region.objects.create(name="Tirol", slug="tirol")
        assert str(r) == "Tirol"

    def test_unique_name(self):
        Region.objects.create(name="Tirol", slug="tirol-1")
        with pytest.raises(IntegrityError):
            Region.objects.create(name="Tirol", slug="tirol-2")

    def test_unique_slug(self):
        Region.objects.create(name="Tirol", slug="tirol")
        with pytest.raises(IntegrityError):
            Region.objects.create(name="Tirol Süd", slug="tirol")

    def test_description_optional(self):
        r = Region.objects.create(name="Salzburg", slug="sbg")
        assert r.description == ""


@pytest.mark.django_db
class TestStationRegionFK:
    def test_station_can_have_null_region(self):
        s = Station.objects.create(name="OE5XTR", callsign="OE5XTR")
        assert s.region is None

    def test_station_region_set_null_on_delete(self):
        r = Region.objects.create(name="Tirol", slug="tirol")
        s = Station.objects.create(name="OE5XTR", callsign="OE5XTR", region=r)
        r.delete()
        s.refresh_from_db()
        assert s.region is None

    def test_region_stations_reverse_relation(self):
        r = Region.objects.create(name="Tirol", slug="tirol")
        Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        Station.objects.create(name="OE5B", callsign="OE5B", region=r)
        assert r.stations.count() == 2
