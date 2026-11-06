"""Client for the ÖVSV repeater directory (https://repeater.oevsv.at)."""

import json
import logging
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

BASE_URL = "https://repeater.oevsv.at/api"
TRX_URL = f"{BASE_URL}/trx?fm=is.true&band=in.(2m,70cm)"
SITE_URL = f"{BASE_URL}/site"


class OevsvError(Exception):
    """Network, HTTP, JSON or schema failure while talking to the ÖVSV feed."""


def _get_json(url: str, timeout: float) -> list:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "oe5xrx-station-manager"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OevsvError(f"{url}: {exc}") from exc
    if not isinstance(data, list):
        raise OevsvError(f"{url}: expected a JSON list")
    return data


def _freq(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.00001"))
    except (InvalidOperation, ValueError):
        return None


def _tone(value) -> str:
    """ÖVSV CTCSS number (77, 88.5) -> device tone string ("77.0", "88.5")."""
    if value in (None, "", 0):
        return "none"
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "none"


def _coord(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.000001"))
    except (InvalidOperation, ValueError):
        return None


def normalize(trx_rows: list[dict], site_rows: list[dict]) -> list[dict]:
    """Join TRX rows with sites and map to Repeater fields.

    ÖVSV names frequencies from the repeater's perspective: ``frequency_tx`` is
    what the repeater transmits (= our RX, ``output_frequency``), ``frequency_rx``
    what it listens on (= our TX, ``input_frequency``). Likewise ``ctcss_rx`` is the
    access tone we must send (``input_tone``), ``ctcss_tx`` the tone it sends
    (``output_tone``). Rows without both frequencies are skipped.
    """
    sites = {}
    for site in site_rows:
        if isinstance(site, dict) and site.get("site_name"):
            sites.setdefault(site["site_name"], site)

    out = []
    for row in trx_rows:
        if not isinstance(row, dict) or row.get("uid") is None:
            logger.warning("oevsv: skipping row without uid: %r", row)
            continue
        if row.get("fm") is False:
            continue
        output_freq = _freq(row.get("frequency_tx"))
        input_freq = _freq(row.get("frequency_rx"))
        if output_freq is None or input_freq is None or not row.get("callsign"):
            logger.warning("oevsv: skipping row %s without frequencies/callsign", row["uid"])
            continue
        site = sites.get(row.get("site_name") or "", {})
        out.append(
            {
                "external_id": str(row["uid"]),
                "callsign": str(row["callsign"])[:32],
                "site_name": (row.get("site_name") or "")[:128],
                "locator": (site.get("locator_short") or "")[:10],
                "output_frequency": output_freq,
                "input_frequency": input_freq,
                "input_tone": _tone(row.get("ctcss_rx")),
                "output_tone": _tone(row.get("ctcss_tx")),
                "latitude": _coord(site.get("latitude")),
                "longitude": _coord(site.get("longitude")),
                "active": row.get("status") != "historic",
            }
        )
    return out


def fetch_oevsv(timeout: float = 30.0) -> list[dict]:
    trx = _get_json(TRX_URL, timeout)
    sites = _get_json(SITE_URL, timeout)
    return normalize(trx, sites)
