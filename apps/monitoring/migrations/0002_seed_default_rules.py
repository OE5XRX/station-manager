"""Seed the 6 system-managed AlertRule rows.

The engine in apps/monitoring/engine.py has one hard-coded check per
AlertType — without these rows, _get_active_rule() returns None for every
type and every check_alerts() tick silently no-ops. The matching
management command (apps/monitoring/management/commands/
create_default_alert_rules.py) was never wired into a deploy step, so
fresh DBs (incl. DR-restores from an empty snapshot) end up with a dead
alert-monitor loop. This migration closes that gap on every `migrate`
run.

Idempotent: get_or_create matches on the unique alert_type field. An
operator who later tunes threshold/severity/is_active via the UI is
never overwritten by a re-run — defaults only apply on first creation.

Reverse — DATA-LOSS WARNING
---------------------------
Reverse is a noop. Removing seeded rows on rollback would discard any
operator-tuned values that landed on top of them, and the engine
collapses to a silent no-op the moment the rows are gone. If a future
migration genuinely needs to remove a rule, write an explicit data
migration that targets just that alert_type.
"""

from django.db import migrations

DEFAULT_RULES = [
    {
        "alert_type": "station_offline",
        "threshold": 5.0,
        "severity": "critical",
        "description": "Station has not sent a heartbeat for more than 5 minutes.",
    },
    {
        "alert_type": "cpu_temperature",
        "threshold": 80.0,
        "severity": "warning",
        "description": "CPU temperature exceeds 80 degrees Celsius.",
    },
    {
        "alert_type": "disk_warning",
        "threshold": 90.0,
        "severity": "warning",
        "description": "Disk usage exceeds 90% (less than 10% free).",
    },
    {
        "alert_type": "disk_critical",
        "threshold": 95.0,
        "severity": "critical",
        "description": "Disk usage exceeds 95% (less than 5% free).",
    },
    {
        "alert_type": "ram_critical",
        "threshold": 90.0,
        "severity": "critical",
        "description": "RAM usage exceeds 90%.",
    },
    {
        "alert_type": "ota_failed",
        "threshold": 0.0,
        "severity": "critical",
        "description": "An OTA deployment failed or was rolled back.",
    },
]


def seed_default_rules(apps, schema_editor):
    AlertRule = apps.get_model("monitoring", "AlertRule")
    for spec in DEFAULT_RULES:
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
        ("monitoring", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            seed_default_rules,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
