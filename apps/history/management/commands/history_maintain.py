"""Management command for history upkeep (compaction, partitions, retention).

Run as a loop (own compose service, see spec §3):
    python manage.py history_maintain --loop --interval 600
"""

import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from apps.history.maintenance import run_maintenance


class Command(BaseCommand):
    help = "Compact metric history into hourly rows, create partitions, drop expired data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Run continuously instead of once.",
        )
        parser.add_argument(
            "--interval",
            type=int,
            default=600,
            help="Loop interval in seconds (default: 600).",
        )

    def handle(self, *args, **options):
        if options["loop"]:
            self.stdout.write(f"Maintaining history every {options['interval']}s...")
            while True:
                # Long-lived process: drop a connection broken by a DB restart.
                close_old_connections()
                self._run()
                time.sleep(options["interval"])
        else:
            self._run()

    def _run(self):
        result = run_maintenance()
        line = (
            f"history_maintain: compacted={result['compacted']} "
            f"created={len(result['created'])} dropped={len(result['dropped'])}"
            f"{' (' + ', '.join(result['dropped']) + ')' if result['dropped'] else ''} "
            f"errors={','.join(result['errors']) or 'none'}"
        )
        (self.stderr if result["errors"] else self.stdout).write(line)
        return result
