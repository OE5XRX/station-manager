"""Management command for the GDPR retention job (Löschfristen).

Run as a loop (own compose service ``retention``):
    python manage.py enforce_retention --loop --interval 86400

Without ``RETENTION_ENFORCE=true`` (or with ``--dry-run``) it only counts.
"""

import argparse
import logging
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from apps.audit.retention import RetentionConfig, run_retention

logger = logging.getLogger("apps.audit.retention")


def _positive_int(value):
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive number of seconds")
    return number


class Command(BaseCommand):
    help = (
        "Delete personal audit data after RETENTION_AUDIT_DAYS, clear IPs after RETENTION_IP_DAYS."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only count affected rows, even if RETENTION_ENFORCE is true.",
        )
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Run continuously instead of once.",
        )
        parser.add_argument(
            "--interval",
            type=_positive_int,
            default=86400,
            help="Loop interval in seconds (default: 86400).",
        )

    def handle(self, *args, **options):
        if options["loop"]:
            self.stdout.write(f"Enforcing retention every {options['interval']}s...")
            while True:
                # Long-lived process: drop a connection broken by a DB restart.
                close_old_connections()
                self._run(options["dry_run"])
                time.sleep(options["interval"])
        else:
            result = self._run(options["dry_run"])
            if result.errors:
                # Non-zero exit for one-shot runs (cron/ops checks); the loop keeps going.
                raise CommandError(f"retention steps failed: {', '.join(result.errors)}")

    def _run(self, dry_run):
        cfg = RetentionConfig.from_settings(dry_run=dry_run)
        result = run_retention(cfg)
        mode = "ENFORCE" if result.enforce else "DRY-RUN"
        hint = " (RETENTION_ENFORCE is false)" if not result.enforce and not dry_run else ""
        counts = " ".join(f"{key}={n}" for key, n in result.counts.items())
        line = (
            f"enforce_retention[{mode}]{hint}: {counts} errors={','.join(result.errors) or 'none'}"
        )
        logger.info(line)
        (self.stderr if result.errors else self.stdout).write(line)
        return result
