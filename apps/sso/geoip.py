"""Thin wrapper around geoip2.database.Reader.

Singleton reader (geoip2 is threadsafe). If the DB file is missing or
the lookup itself fails for any reason, ``lookup_location`` returns
``(None, None)`` — never raises. Token issuance must not block on
GeoIP being broken.

DB-file location is ``settings.GEOIP_DB_PATH`` (Docker volume
``/app/geoip_db`` in prod).
"""

import logging
import threading
from pathlib import Path

import geoip2.database
import geoip2.errors
from django.conf import settings

logger = logging.getLogger(__name__)

_reader = None
_reader_lock = threading.Lock()
_reader_load_failed = False


def _get_reader():
    global _reader, _reader_load_failed
    if _reader is not None:
        return _reader
    if _reader_load_failed:
        return None
    with _reader_lock:
        if _reader is not None:
            return _reader
        path = Path(settings.GEOIP_DB_PATH)
        if not path.exists():
            logger.warning("GeoIP DB not found at %s -- lookups disabled", path)
            _reader_load_failed = True
            return None
        try:
            _reader = geoip2.database.Reader(str(path))
        except Exception:
            logger.exception("GeoIP DB reader could not be initialised")
            _reader_load_failed = True
            return None
    return _reader


def lookup_location(ip):
    """Return (country_code, city_name) for the IP, or (None, None)."""
    if not ip:
        return None, None
    reader = _get_reader()
    if reader is None:
        return None, None
    try:
        resp = reader.city(ip)
    except geoip2.errors.AddressNotFoundError:
        return None, None
    except Exception:
        logger.exception("GeoIP lookup failed for %s", ip)
        return None, None
    return resp.country.iso_code, resp.city.name
