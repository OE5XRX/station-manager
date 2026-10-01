"""Seed the 3 telemetry-related AlertRule rows added in Task 12.

Idempotent: get_or_create matches on the unique alert_type field. Operator-
tuned threshold/severity/is_active values are never overwritten on re-run.

Reverse is a noop — same rationale as 0002_seed_default_rules.
"""

from django.db import migrations

NEW_RULES = [
    {
        "alert_type": "unexpected_reboot",
        "threshold": 0.0,
        "severity": "warning",
        "description": "Unerwarteter Reboot (Crash/Watchdog/Power).",
    },
    {
        "alert_type": "power_warning",
        "threshold": 0.0,
        "severity": "warning",
        "description": "Undervoltage / Throttling erkannt.",
    },
    {
        "alert_type": "storage_health",
        "threshold": 80.0,
        "severity": "warning",
        "description": "SD/eMMC-Verschleiß oder I/O-Fehler.",
    },
]


def seed_telemetry_rules(apps, schema_editor):
    AlertRule = apps.get_model("monitoring", "AlertRule")
    for spec in NEW_RULES:
        AlertRule.objects.get_or_create(
            alert_type=spec["alert_type"],
            defaults={
                "threshold": spec["threshold"],
                "severity": spec["severity"],
                "description": spec["description"],
                "is_active": True,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("monitoring", "0003_alter_alertrule_alert_type"),
    ]

    operations = [
        migrations.RunPython(
            seed_telemetry_rules,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
