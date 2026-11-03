"""Convert the legacy CPU/Disk/RAM rules to generic threshold rules (spec §5.3).

Forward:
1. Seed ``MetricDefinition`` rows for the legacy keys that the server derives
   from ``inventory`` for old agents (they send no descriptors). get_or_create
   by key — an existing row (agent- or operator-written) is never touched.
2. Convert ``cpu_temperature`` / ``disk_warning`` / ``disk_critical`` /
   ``ram_critical`` rows *in place* to ``alert_type="threshold"``. Same pk, so
   ``Alert.alert_rule`` FKs stay valid; operator-tuned threshold / severity /
   is_active / description are kept. ``gte`` because the legacy engine
   compared with ``>=``.
3. Resolve alerts that were open on those rules — the generic engine tracks
   state per metric instance and re-fires if the breach persists.
4. Create the ``pwr.12v.voltage_v lt 11.0`` rule (avg, 60 s, warning) if no
   such rule exists yet.

Idempotent: every step matches on existing rows, so a re-run is a no-op.

Reverse — noop (same rationale as 0002_seed_default_rules): mapping threshold
rules back onto typed singletons would lose operator-created rules, and the
legacy engine checks this would feed no longer exist.
"""

from django.db import migrations
from django.utils import timezone

LEGACY_DEFINITIONS = [
    # key, label, unit, group, precision, min, max
    ("cpu.temp_c", "CPU temperature", "°C", "system", 1, -40.0, 125.0),
    ("mem.used_pct", "Memory used", "%", "system", 1, 0.0, 100.0),
    ("disk.root.used_pct", "Disk used /", "%", "disk", 1, 0.0, 100.0),
    ("disk.boot.used_pct", "Disk used /boot", "%", "disk", 1, 0.0, 100.0),
    ("sys.uptime_s", "Uptime", "s", "system", 0, 0.0, None),
]

LEGACY_RULES = {
    # legacy alert_type: (metric_key, name)
    "cpu_temperature": ("cpu.temp_c", "CPU temperature"),
    "disk_warning": ("disk.*.used_pct", "Disk usage (warning)"),
    "disk_critical": ("disk.*.used_pct", "Disk usage (critical)"),
    "ram_critical": ("mem.used_pct", "Memory usage"),
}

UNDERVOLTAGE_RULE = {
    "metric_key": "pwr.12v.voltage_v",
    "operator": "lt",
    "threshold": 11.0,
    "aggregate": "avg",
    "duration_seconds": 60,
    "severity": "warning",
    "name": "12 V rail undervoltage",
    "description": "12 V rail average below 11.0 V for at least 60 s.",
}


def forwards(apps, schema_editor):
    AlertRule = apps.get_model("monitoring", "AlertRule")
    Alert = apps.get_model("monitoring", "Alert")
    MetricDefinition = apps.get_model("monitoring", "MetricDefinition")

    for key, label, unit, group, precision, lo, hi in LEGACY_DEFINITIONS:
        MetricDefinition.objects.get_or_create(
            key=key,
            defaults={
                "label": label,
                "unit": unit,
                "group": group,
                "precision": precision,
                "min_value": lo,
                "max_value": hi,
            },
        )

    now = timezone.now()
    for legacy_type, (metric_key, name) in LEGACY_RULES.items():
        for rule in AlertRule.objects.filter(alert_type=legacy_type):
            Alert.objects.filter(alert_rule=rule, is_resolved=False).update(is_resolved=True, resolved_at=now)
            rule.alert_type = "threshold"
            rule.metric_key = metric_key
            rule.name = name
            rule.operator = "gte"
            rule.aggregate = "avg"
            rule.duration_seconds = 0
            rule.save()

    exists = AlertRule.objects.filter(
        alert_type="threshold",
        metric_key=UNDERVOLTAGE_RULE["metric_key"],
        operator=UNDERVOLTAGE_RULE["operator"],
    ).exists()
    if not exists:
        AlertRule.objects.create(alert_type="threshold", is_active=True, **UNDERVOLTAGE_RULE)


class Migration(migrations.Migration):
    dependencies = [
        ("monitoring", "0005_generic_metrics_schema"),
    ]

    operations = [
        migrations.RunPython(forwards, reverse_code=migrations.RunPython.noop),
    ]
