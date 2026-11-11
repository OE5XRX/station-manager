from django.db import migrations, models


def seed_rule(apps, schema_editor):
    AlertRule = apps.get_model("monitoring", "AlertRule")
    AlertRule.objects.get_or_create(
        alert_type="passive_no_rx",
        defaults={
            "threshold": 0.0,
            "severity": "warning",
            "description": "Passive APRS receive is running but has heard no packets "
            "(limit: APRS settings, no-RX alert hours).",
            "is_active": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("monitoring", "0006_generic_metrics_data"),
    ]

    operations = [
        migrations.AlterField(
            model_name="alertrule",
            name="alert_type",
            field=models.CharField(
                choices=[
                    ("station_offline", "Station Offline"),
                    ("ota_failed", "OTA Failed"),
                    ("unexpected_reboot", "Unexpected Reboot"),
                    ("power_warning", "Power Warning"),
                    ("storage_health", "Storage Health"),
                    ("threshold", "Threshold"),
                    ("passive_no_rx", "Passive RX silent"),
                ],
                max_length=20,
                verbose_name="alert type",
            ),
        ),
        migrations.RunPython(seed_rule, reverse_code=migrations.RunPython.noop),
    ]
