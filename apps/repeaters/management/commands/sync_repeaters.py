"""Import the ÖVSV repeater directory.

Run as a loop (own compose service):
    python manage.py sync_repeaters --loop --interval 86400
"""

import argparse
import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from apps.repeaters.oevsv import OevsvError, fetch_oevsv
from apps.repeaters.sync import sync_repeaters

logger = logging.getLogger(__name__)


def _positive_int(value):
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive number of seconds")
    return number


class Command(BaseCommand):
    help = "Sync the repeater list from the ÖVSV directory."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Run continuously.")
        parser.add_argument(
            "--interval",
            type=_positive_int,
            default=86400,
            help="Loop interval in seconds (default: 86400).",
        )

    def handle(self, *args, **options):
        if options["loop"]:
            self.stdout.write(f"Syncing repeaters every {options['interval']}s...")
            while True:
                close_old_connections()
                ok = self._run()
                # Retry a failed run soon instead of waiting a full interval.
                time.sleep(options["interval"] if ok else min(options["interval"], 900))
        else:
            self._run()

    def _run(self):
        try:
            rows = fetch_oevsv()
            result = sync_repeaters(rows, timezone.now())
        except OevsvError as exc:
            # Keep existing data; the next iteration retries.
            logger.error("sync_repeaters: ÖVSV fetch failed: %s", exc)
            self.stderr.write(f"sync_repeaters: fetch failed: {exc}")
            return False
        except Exception:
            logger.exception("sync_repeaters: unexpected error")
            self.stderr.write("sync_repeaters: unexpected error (see log)")
            return False
        self.stdout.write(
            f"sync_repeaters: created={result.created} updated={result.updated} "
            f"deactivated={result.deactivated} skipped={result.skipped}"
        )
        return True
