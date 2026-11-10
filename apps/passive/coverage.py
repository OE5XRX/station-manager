import math

from django.db import transaction
from django.db.models import F
from django.db.models.functions import Greatest, Least

from apps.repeaters.selection import haversine_km

from .models import AprsPacket, CoverageCell

CELLS_PER_DEGREE = 50  # 0.02 degrees, about 2 km


def cell_of(lat: float, lon: float) -> tuple[int, int]:
    return math.floor(lat * CELLS_PER_DEGREE), math.floor(lon * CELLS_PER_DEGREE)


def record_coverage(station, packet: AprsPacket) -> None:
    """Direct (not digipeated) packets with a transmitter position feed the coverage grid."""
    if packet.digipeated or packet.latitude is None or packet.longitude is None:
        return
    if packet.packet_type in ("object", "item"):
        return
    lat, lon = float(packet.latitude), float(packet.longitude)
    if station.latitude is not None and station.longitude is not None:
        dist = haversine_km(station.latitude, station.longitude, lat, lon)
    else:
        dist = 0.0
    cell_lat, cell_lon = cell_of(lat, lon)
    with transaction.atomic():
        updated = CoverageCell.objects.filter(
            station=station, cell_lat=cell_lat, cell_lon=cell_lon
        ).update(
            count=F("count") + 1,
            last_heard=Greatest(F("last_heard"), packet.heard_at),
            first_heard=Least(F("first_heard"), packet.heard_at),
            max_distance_km=Greatest(F("max_distance_km"), dist),
        )
        if not updated:
            CoverageCell.objects.create(
                station=station,
                cell_lat=cell_lat,
                cell_lon=cell_lon,
                count=1,
                first_heard=packet.heard_at,
                last_heard=packet.heard_at,
                max_distance_km=dist,
            )
