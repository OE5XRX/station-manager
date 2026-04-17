"""Management command to create default alert rules."""

from django.core.management.base import BaseCommand

from apps.monitoring.models import AlertRule

DEFAULT_RULES = [
    {
        "alert_type": AlertRule.AlertType.STATION_OFFLINE,
        "threshold": 5.0,
        "severity": AlertRule.Severity.CRITICAL,
        "description": "Station has not sent a heartbeat for more than 5 minutes.",
    },
    {
        "alert_type": AlertRule.AlertType.CPU_TEMPERATURE,
        "threshold": 80.0,
        "severity": AlertRule.Severity.WARNING,
        "description": "CPU temperature exceeds 80 degrees Celsius.",
    },
    {
        "alert_type": AlertRule.AlertType.DISK_WARNING,
        "threshold": 90.0,
        "severity": AlertRule.Severity.WARNING,
        "description": "Disk usage exceeds 90% (less than 10% free).",
    },
    {
        "alert_type": AlertRule.AlertType.DISK_CRITICAL,
        "threshold": 95.0,
        "severity": AlertRule.Severity.CRITICAL,
        "description": "Disk usage exceeds 95% (less than 5% free).",
    },
    {
        "alert_type": AlertRule.AlertType.RAM_CRITICAL,
        "threshold": 90.0,
        "severity": AlertRule.Severity.CRITICAL,
        "description": "RAM usage exceeds 90%.",
    },
    {
        "alert_type": AlertRule.AlertType.OTA_FAILED,
        "threshold": 0.0,
        "severity": AlertRule.Severity.CRITICAL,
        "description": "OTA deployment failed or was rolled back.",
    },
]


class Command(BaseCommand):
    help = "Create default alert rules if they do not already exist."

    def handle(self, *args, **options):
        created_count = 0
        for rule_data in DEFAULT_RULES:
            _, created = AlertRule.objects.get_or_create(
                alert_type=rule_data["alert_type"],
                defaults=rule_data,
            )
            if created:
                created_count += 1
                self.stdout.write(
                    f"  Created rule: {rule_data['alert_type']} "
                    f"(threshold={rule_data['threshold']}, "
                    f"severity={rule_data['severity']})"
                )
            else:
                self.stdout.write(f"  Rule already exists: {rule_data['alert_type']}")

        self.stdout.write(f"Done. Created {created_count} new rule(s).")
