# Generated for device_token removal.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0003_add_device_key"),
        ("stations", "0006_remove_station_device_token"),
    ]

    operations = [
        migrations.DeleteModel(
            name="DeviceToken",
        ),
    ]
