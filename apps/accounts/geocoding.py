"""Geocoding + Maidenhead locator helpers (Sub-Spec 1a Foundation).

`geocode_address` resolves a postal address to (lat, lon) via Nominatim/OSM
(added in Task 10).
`lat_lon_to_locator` computes the Maidenhead 6-char grid locator from
(lat, lon).

Spec: docs/superpowers/specs/2026-06-12-user-domain-1a-foundation-design.md
"""

import logging
import time
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_TIMEOUT = 10  # seconds
# Nominatim Free-Tier policy requires identifying User-Agent. The default
# carries the project name only; a deployment can override via the
# `NOMINATIM_USER_AGENT` Django setting to add a contact handle as required
# by the Nominatim usage policy.
DEFAULT_USER_AGENT = "OE5XRX-StationManager/1.0"


def _user_agent():
    """Read User-Agent from Django settings with a generic fallback."""
    return getattr(settings, "NOMINATIM_USER_AGENT", DEFAULT_USER_AGENT)


def geocode_address(address):
    """Resolve a postal address to (latitude, longitude) via Nominatim.

    Returns None on any error (network, no result, malformed response,
    timeout, parse failure). The function rate-limits itself with a
    1-second sleep per call to comply with the Nominatim Free-Tier policy.

    NOT thread-safe across multiple concurrent calls in the same process —
    the rate-limit pause is local. For a small Verein (≤ a few save-events
    per minute) that's fine.

    Privacy note: the address is intentionally NOT included in the warning
    log on failure (it can be PII). Callers must propagate the None result
    to their own UI-level error path if user feedback is needed.
    """
    if not address or not address.strip():
        return None

    time.sleep(1)  # Rate-Limit-Compliance

    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={
                "q": address.strip(),
                "format": "json",
                "limit": 1,
                "accept-language": "de,en",
            },
            headers={"User-Agent": _user_agent()},
            timeout=NOMINATIM_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            return None
        first = results[0]
        return (Decimal(first["lat"]), Decimal(first["lon"]))
    except (
        requests.RequestException,
        ValueError,
        KeyError,
        TypeError,
        InvalidOperation,
    ) as exc:
        # Do NOT log the address itself — only the exception class so the
        # operator has a debuggable signal without leaking user PII.
        logger.warning("Nominatim geocode failed: %s: %s", type(exc).__name__, exc)
        return None


def lat_lon_to_locator(lat, lon, precision: int = 6) -> str:
    """Maidenhead-Locator aus (lat, lon).

    precision=6 → 6-Zeichen-Locator (z.B. 'JN78DH').
    precision=4 → 4-Zeichen Grid-Square (z.B. 'JN78').

    Algorithmus:
      1. Verschiebung: lon += 180, lat += 90 (alles wird positiv).
      2. Fields (1. Letter-Pair): 18×18 Grid à 20° lon / 10° lat (A-R).
      3. Squares (2. Digit-Pair): 10×10 Grid à 2° lon / 1° lat (0-9).
      4. Subsquares (3. Letter-Pair): 24×24 Grid à 5' lon / 2.5' lat (A-X).

    Akzeptiert float, int, Decimal — wird intern zu float konvertiert.
    """
    lat_f = float(lat) + 90.0
    lon_f = float(lon) + 180.0
    a_ord = ord("A")
    lon_field, lon_rest = divmod(lon_f, 20.0)
    lat_field, lat_rest = divmod(lat_f, 10.0)
    # Clamp field indices to the valid Maidenhead A-R range (0..17). At the
    # exact boundary lon=180 / lat=90 the divmod result is 18 — without the
    # clamp we'd emit 'S' (out of range) and produce a locator that the
    # LOCATOR_REGEX validator would reject.
    lon_field = min(int(lon_field), 17)
    lat_field = min(int(lat_field), 17)
    out = chr(a_ord + lon_field) + chr(a_ord + lat_field)
    lon_sq, lon_rest = divmod(lon_rest, 2.0)
    lat_sq, lat_rest = divmod(lat_rest, 1.0)
    out += str(int(lon_sq)) + str(int(lat_sq))
    if precision >= 6:
        # Per 2° lon → 24 subsquares (5'/60° per subsquare), so multiply by 12
        # to map [0, 2) to [0, 24).
        lon_sub = int(lon_rest * 12)
        # Per 1° lat → 24 subsquares (2.5'/60° per subsquare), so multiply by 24
        # to map [0, 1) to [0, 24).
        lat_sub = int(lat_rest * 24)
        out += chr(a_ord + lon_sub) + chr(a_ord + lat_sub)
    return out
