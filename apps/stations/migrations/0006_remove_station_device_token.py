# Generated for device_token removal.

from django.db import migrations


def warn_if_tokens_without_keys(apps, schema_editor):
    """Warn operators if any station has a DeviceToken but no DeviceKey.

    Such stations will lose authentication after this migration runs and
    need an Ed25519 key generated before they can reconnect.
    """
    DeviceToken = apps.get_model("api", "DeviceToken")
    DeviceKey = apps.get_model("api", "DeviceKey")
    Station = apps.get_model("stations", "Station")

    stations_with_token = Station.objects.filter(device_token__isnull=False)
    stations_needing_key = []
    for station in stations_with_token:
        if not DeviceKey.objects.filter(station_id=station.pk).exists():
            stations_needing_key.append(station)

    if stations_needing_key:
        names = ", ".join(f"{s.pk}:{s.name}" for s in stations_needing_key)
        print(
            "\nWARNING: The following stations have a device_token but no "
            "DeviceKey. They will LOSE AUTHENTICATION after this migration. "
            "Generate an Ed25519 key for each before rolling this out: "
            f"{names}"
        )

    orphan_tokens = DeviceToken.objects.filter(station__isnull=True).count()
    if orphan_tokens:
        print(
            f"\nINFO: Dropping {orphan_tokens} orphan DeviceToken row(s) "
            "not linked to any station."
        )


class Migration(migrations.Migration):

    dependencies = [
        ("stations", "0005_station_stations_st_status_bed7d9_idx"),
        ("api", "0003_add_device_key"),
    ]

    operations = [
        migrations.RunPython(warn_if_tokens_without_keys, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="station",
            name="device_token",
        ),
    ]
