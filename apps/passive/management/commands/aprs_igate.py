"""APRS RX-iGate worker (own compose service):
python manage.py aprs_igate [--once]
"""

import asyncio
import logging

from django.core.management.base import BaseCommand

from apps.passive.aprs.worker import IgateWorker

logger = logging.getLogger(__name__)


async def _run(once: bool) -> None:
    worker = IgateWorker()
    try:
        if once:
            await worker.step()
        else:
            await worker.run_forever()
    finally:
        await worker.shutdown()


class Command(BaseCommand):
    help = "Gate freshly received APRS packets to APRS-IS (one login per station)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run a single iteration.")

    def handle(self, *args, **options):
        asyncio.run(_run(options["once"]))
