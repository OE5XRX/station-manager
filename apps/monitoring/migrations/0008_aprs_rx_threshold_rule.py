"""Replace the special-cased ``passive_no_rx`` alert by a generic threshold rule.

Forward:
1. Resolve alerts still open on the ``passive_no_rx`` rule and delete that rule
   (``Alert.alert_rule`` is SET_NULL, history survives).
2. Create ``aprs.rx_packets_1h < 1`` for 5 h (avg, warning) unless a rule for that key
   already exists. The agent reports the key only while an APRS decoder has run >= 1 h,
   so the 1 h window plus 5 h duration is ~6 h without a packet; the engine freezes the
   rule while the key is missing (no false alarm on use/stop/start).
3. Drop ``passive_no_rx`` from ``alert_type`` choices.

Reverse recreates the (singleton) ``passive_no_rx`` rule; the generic rule stays
(it is a plain threshold rule the operator may have edited).
"""

from django.db import migrations, models
from django.utils import timezone

APRS_RULE = {
    "metric_key": "aprs.rx_packets_1h",
    "operator": "lt",
    "threshold": 1.0,
    "aggregate": "avg",
    "duration_seconds": 5 * 3600,
    "severity": "warning",
    "name": "APRS RX silent",
    "description": "No APRS packet heard for about 6 h (1 h window + 5 h): antenna/RX problem.",
}


def forward(apps, schema_editor):
    AlertRule = apps.get_model("monitoring", "AlertRule")
    Alert = apps.get_model("monitoring", "Alert")
    Alert.objects.filter(alert_rule__alert_type="passive_no_rx", is_resolved=False).update(
        is_resolved=True, resolved_at=timezone.now()
    )
    AlertRule.objects.filter(alert_type="passive_no_rx").delete()
    if not AlertRule.objects.filter(
        alert_type="threshold", metric_key=APRS_RULE["metric_key"]
    ).exists():
        AlertRule.objects.create(alert_type="threshold", is_active=True, **APRS_RULE)


def backward(apps, schema_editor):
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
        ("monitoring", "0007_passive_no_rx_alert_type"),
    ]

    operations = [
        migrations.RunPython(forward, reverse_code=backward),
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
                ],
                max_length=20,
                verbose_name="alert type",
            ),
        ),
    ]
