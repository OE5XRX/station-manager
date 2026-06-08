"""Download + atomic replace of the db-ip.com City Lite DB.

Spec §6.3: daily cron, current-month-first with previous-month fallback,
fails workflow only when BOTH months 404. Existing DB on disk stays
untouched on failure -- lookups keep returning the previous month's
data instead of erroring out.
"""

import gzip
import shutil
import tempfile
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

DBIP_URL_TEMPLATE = "https://download.db-ip.com/free/dbip-city-lite-{year_month}.mmdb.gz"


def _previous_month(today: date) -> date:
    """First day of the previous month. Stdlib-only -- avoid pulling in
    python-dateutil which is only present transitively via boto3."""
    if today.month == 1:
        return today.replace(year=today.year - 1, month=12, day=1)
    return today.replace(month=today.month - 1, day=1)


class Command(BaseCommand):
    help = "Download db-ip.com City Lite DB; atomic replace into GEOIP_DB_PATH."

    def handle(self, *args, **opts):
        target = Path(settings.GEOIP_DB_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)

        today = date.today()
        candidates = [
            today.strftime("%Y-%m"),
            _previous_month(today).strftime("%Y-%m"),
        ]

        downloaded_from = None
        for year_month in candidates:
            url = DBIP_URL_TEMPLATE.format(year_month=year_month)
            try:
                self._download(url, target)
                downloaded_from = year_month
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    self.stdout.write(self.style.WARNING(
                        f"{year_month} not yet published (404), trying previous"
                    ))
                    continue
                raise

        if downloaded_from is None:
            raise SystemExit(
                f"Both {candidates[0]} and {candidates[1]} return 404 -- "
                f"db-ip.com release schedule changed? Manual check needed."
            )

        self.stdout.write(self.style.SUCCESS(
            f"Updated {target} from db-ip.com {downloaded_from}"
        ))

        # Reset the in-process singleton so the next lookup picks up the
        # fresh DB without a worker restart. Other gunicorn workers
        # still hold their stale reader until next restart -- acceptable.
        from apps.sso import geoip
        geoip._reader = None
        geoip._reader_load_failed = False

    def _download(self, url: str, target: Path) -> None:
        with tempfile.NamedTemporaryFile(delete=False, dir=target.parent) as tmp:
            tmp_path = Path(tmp.name)
        try:
            self.stdout.write(f"Download {url} ...")
            with urllib.request.urlopen(url) as resp, gzip.GzipFile(fileobj=resp) as gz:
                with tmp_path.open("wb") as out:
                    shutil.copyfileobj(gz, out)
            tmp_path.replace(target)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
