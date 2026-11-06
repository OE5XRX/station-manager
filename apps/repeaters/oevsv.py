"""Client for the ÖVSV repeater directory (https://repeater.oevsv.at)."""

import json
import logging
import math
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

BASE_URL = "https://repeater.oevsv.at/api"
TRX_URL = f"{BASE_URL}/trx?fm=is.true&band=in.(2m,70cm)"
SITE_URL = f"{BASE_URL}/site"
MAX_BYTES = 10 * 1024 * 1024


class OevsvError(Exception):
    """Network, HTTP, JSON or schema failure while talking to the ÖVSV feed."""


def _get_json(url: str, timeout: float) -> list:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "oe5xrx-station-manager"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            body = resp.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise OevsvError(f"{url}: response larger than {MAX_BYTES} bytes")
        data = json.loads(body)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OevsvError(f"{url}: {exc}") from exc
    if not isinstance(data, list):
        raise OevsvError(f"{url}: expected a JSON list")
    return data


def _freq(value) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 < number < 10000:
            return None
        return number.quantize(Decimal("0.00001"))
    except (InvalidOperation, ValueError):
        return None


def _tone(value) -> str:
    """ÖVSV CTCSS number (77, 88.5) -> device tone string ("77.0", "88.5")."""
    if value in (None, "", 0):
        return "none"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "none"
    if not math.isfinite(number) or not 60 <= number <= 260:
        return "none"
    return f"{number:.1f}"


def _coord(value, limit: int) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or abs(number) > limit:
            return None
        return number.quantize(Decimal("0.000001"))
    except (InvalidOperation, ValueError):
        return None


def _normalize_row(row: dict, sites: dict) -> dict | None:
    output_freq = _freq(row.get("frequency_tx"))
    input_freq = _freq(row.get("frequency_rx"))
    if output_freq is None or input_freq is None or not row.get("callsign"):
        return None
    site = sites.get(str(row.get("site_name") or ""), {})
    return {
        "external_id": str(row["uid"]),
        "callsign": str(row["callsign"])[:32],
        "site_name": str(row.get("site_name") or "")[:128],
        "locator": str(site.get("locator_short") or "")[:10],
        "output_frequency": output_freq,
        "input_frequency": input_freq,
        "input_tone": _tone(row.get("ctcss_rx")),
        "output_tone": _tone(row.get("ctcss_tx")),
        "latitude": _coord(site.get("latitude"), 90),
        "longitude": _coord(site.get("longitude"), 180),
        "active": row.get("status") != "historic",
    }


def normalize(trx_rows: list[dict], site_rows: list[dict]) -> list[dict]:
    """Join TRX rows with sites and map to Repeater fields.

    ÖVSV names frequencies from the repeater's perspective: ``frequency_tx`` is
    what the repeater transmits (= our RX, ``output_frequency``), ``frequency_rx``
    what it listens on (= our TX, ``input_frequency``). Likewise ``ctcss_rx`` is the
    access tone we must send (``input_tone``), ``ctcss_tx`` the tone it sends
    (``output_tone``). Malformed rows are skipped, never fatal.
    """
    sites = {}
    for site in site_rows:
        if isinstance(site, dict) and isinstance(site.get("site_name"), str):
            sites.setdefault(site["site_name"], site)

    out = []
    for row in trx_rows:
        if not isinstance(row, dict) or row.get("uid") is None or row.get("fm") is False:
            continue
        try:
            mapped = _normalize_row(row, sites)
        except (TypeError, ValueError, InvalidOperation):
            mapped = None
        if mapped is None:
            logger.warning("oevsv: skipping malformed row %r", row.get("uid"))
            continue
        out.append(mapped)
    return out


def fetch_oevsv(timeout: float = 30.0) -> list[dict]:
    trx = _get_json(TRX_URL, timeout)
    sites = _get_json(SITE_URL, timeout)
    rows = normalize(trx, sites)
    # Schema drift / empty feed must not look like "every repeater is gone".
    if not rows or len(rows) < len(trx) / 2:
        raise OevsvError(f"feed unusable: {len(rows)} usable of {len(trx)} TRX rows")
    return rows
