"""Management command to create default alert rules.

The canonical seed runs in the monitoring migrations on every `migrate`
(0002/0004 seed the event rules; 0006_generic_metrics_data converts the legacy
CPU/Disk/RAM rules to threshold rules and adds the 12 V undervoltage rule).
This command remains as an operator escape-hatch for the rare case where a rule
was manually deleted via Django Admin and the operator wants to re-seed without
rolling back the migration.

Idempotent, defaults only apply on first creation:
- event rules: get_or_create on ``alert_type`` (unique per event type);
- threshold rules: grouped by ``metric_key``. If at least as many threshold
  rules exist for the key as there are defaults, nothing is created for it —
  an operator may have edited operator/severity/name of a seeded rule, which
  must not re-seed a duplicate. Otherwise the defaults without an exact match
  (``operator``, plus ``severity`` for keys with a warning + critical pair such
  as ``disk.*.used_pct``) are created, but never more than
  ``group size - existing`` (the name is operator-editable, never matched).
If you change the defaults here, mirror them in the migrations.
"""

from django.core.management.base import BaseCommand

from apps.monitoring.models import AlertRule

EVENT_RULES = [
    {
        "alert_type": AlertRule.AlertType.STATION_OFFLINE,
        "threshold": 5.0,
        "severity": AlertRule.Severity.CRITICAL,
        "description": "Station has not sent a heartbeat for more than 5 minutes.",
    },
    {
        "alert_type": AlertRule.AlertType.OTA_FAILED,
        "threshold": 0.0,
        "severity": AlertRule.Severity.CRITICAL,
        "description": "An OTA deployment failed or was rolled back.",
    },
    {
        "alert_type": AlertRule.AlertType.UNEXPECTED_REBOOT,
        "threshold": 0.0,
        "severity": AlertRule.Severity.WARNING,
        "description": "Unerwarteter Reboot (Crash/Watchdog/Power).",
    },
    {
        "alert_type": AlertRule.AlertType.POWER_WARNING,
        "threshold": 0.0,
        "severity": AlertRule.Severity.WARNING,
        "description": "Undervoltage / Throttling erkannt.",
    },
    {
        "alert_type": AlertRule.AlertType.STORAGE_HEALTH,
        "threshold": 80.0,
        "severity": AlertRule.Severity.WARNING,
        "description": "SD/eMMC-Verschleiß oder I/O-Fehler.",
    },
]

THRESHOLD_RULES = [
    {
        "name": "CPU temperature",
        "metric_key": "cpu.temp_c",
        "operator": AlertRule.Operator.GTE,
        "threshold": 80.0,
        "severity": AlertRule.Severity.WARNING,
        "description": "CPU temperature exceeds 80 degrees Celsius.",
    },
    {
        "name": "Disk usage (warning)",
        "metric_key": "disk.*.used_pct",
        "operator": AlertRule.Operator.GTE,
        "threshold": 90.0,
        "severity": AlertRule.Severity.WARNING,
        "description": "Disk usage exceeds 90% (less than 10% free).",
    },
    {
        "name": "Disk usage (critical)",
        "metric_key": "disk.*.used_pct",
        "operator": AlertRule.Operator.GTE,
        "threshold": 95.0,
        "severity": AlertRule.Severity.CRITICAL,
        "description": "Disk usage exceeds 95% (less than 5% free).",
    },
    {
        "name": "Memory usage",
        "metric_key": "mem.used_pct",
        "operator": AlertRule.Operator.GTE,
        "threshold": 90.0,
        "severity": AlertRule.Severity.CRITICAL,
        "description": "RAM usage exceeds 90%.",
    },
    {
        "name": "12 V rail undervoltage",
        "metric_key": "pwr.12v.voltage_v",
        "operator": AlertRule.Operator.LT,
        "threshold": 11.0,
        "aggregate": AlertRule.Aggregate.AVG,
        "duration_seconds": 60,
        "severity": AlertRule.Severity.WARNING,
        "description": "12 V rail average below 11.0 V for at least 60 s.",
    },
]

# Keys with more than one default rule per operator (warning + critical):
# matched on severity too, so deleting one of them re-seeds only that one.
SEVERITY_DISAMBIGUATED_KEYS = {"disk.*.used_pct"}


class Command(BaseCommand):
    help = "Create default alert rules if they do not already exist."

    def handle(self, *args, **options):
        created_count = 0
        for rule_data in EVENT_RULES:
            _, created = AlertRule.objects.get_or_create(
                alert_type=rule_data["alert_type"],
                defaults=rule_data,
            )
            created_count += self._report(created, rule_data["alert_type"], rule_data)
        groups: dict[str, list[dict]] = {}
        for rule_data in THRESHOLD_RULES:
            groups.setdefault(rule_data["metric_key"], []).append(rule_data)
        for metric_key, defaults in groups.items():
            existing = AlertRule.objects.filter(
                alert_type=AlertRule.AlertType.THRESHOLD, metric_key=metric_key
            )
            room = len(defaults) - existing.count()
            for rule_data in defaults:
                lookup = {"operator": rule_data["operator"]}
                if metric_key in SEVERITY_DISAMBIGUATED_KEYS:
                    lookup["severity"] = rule_data["severity"]
                created = room > 0 and not existing.filter(**lookup).exists()
                if created:
                    AlertRule.objects.create(alert_type=AlertRule.AlertType.THRESHOLD, **rule_data)
                    room -= 1
                label = f"{rule_data['name']} ({metric_key} {rule_data['operator']})"
                created_count += self._report(created, label, rule_data)

        self.stdout.write(f"Done. Created {created_count} new rule(s).")

    def _report(self, created, label, rule_data):
        if created:
            self.stdout.write(
                f"  Created rule: {label} "
                f"(threshold={rule_data['threshold']}, "
                f"severity={rule_data['severity']})"
            )
            return 1
        self.stdout.write(f"  Rule already exists: {label}")
        return 0
