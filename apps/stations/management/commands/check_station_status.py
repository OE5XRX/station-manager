"""Management command to mark stale stations as offline.

Run periodically via cron, systemd timer, or as a loop:
    python manage.py check_station_status --loop
"""

import time

from django.core.management.base import BaseCommand

from apps.stations.tasks import mark_stale_stations_offline


class Command(BaseCommand):
    help = "Mark stations as offline if they missed their heartbeat."

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Run continuously every 30 seconds instead of once.",
        )
        parser.add_argument(
            "--interval",
            type=int,
            default=30,
            help="Loop interval in seconds (default: 30).",
        )

    def handle(self, *args, **options):
        if options["loop"]:
            self.stdout.write(f"Checking station status every {options['interval']}s...")
            while True:
                count = mark_stale_stations_offline()
                if count:
                    self.stdout.write(f"Marked {count} station(s) offline.")
                time.sleep(options["interval"])
        else:
            count = mark_stale_stations_offline()
            self.stdout.write(f"Marked {count} station(s) offline.")
