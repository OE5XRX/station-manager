"""Management command to run the alert engine.

Run periodically via cron, systemd timer, or as a loop:
    python manage.py check_alerts --loop
"""

import time

from django.core.management.base import BaseCommand

from apps.monitoring.engine import check_alerts
from apps.monitoring.notifications import send_alert_notifications


class Command(BaseCommand):
    help = "Check all stations for alert conditions and send notifications."

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Run continuously instead of once.",
        )
        parser.add_argument(
            "--interval",
            type=int,
            default=30,
            help="Loop interval in seconds (default: 30).",
        )

    def handle(self, *args, **options):
        if options["loop"]:
            self.stdout.write(f"Checking alerts every {options['interval']}s...")
            while True:
                self._run_check()
                time.sleep(options["interval"])
        else:
            self._run_check()

    def _run_check(self):
        new_alerts = check_alerts()
        if new_alerts:
            self.stdout.write(f"Created {len(new_alerts)} new alert(s).")
            for alert in new_alerts:
                send_alert_notifications(alert)
        return new_alerts
