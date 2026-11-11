"""OSM tile policy: the tile layer must send a cross-origin Referer.

Django's default Referrer-Policy is ``same-origin``, so without a per-layer
override the browser sends no Referer to tile.openstreetmap.org and OSM answers
403 "Access blocked". The global policy stays untouched.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAP_JS = (REPO_ROOT / "static" / "js" / "aprs-map.js").read_text()
LEAFLET = (REPO_ROOT / "static" / "vendor" / "leaflet" / "leaflet.js").read_text()


def test_vendored_leaflet_supports_referrer_policy():
    assert "referrerPolicy" in LEAFLET


def test_tile_layer_sets_referrer_policy():
    match = re.search(r"L\.tileLayer\(.*?\)\.addTo\(map\)", MAP_JS, re.S)
    assert match, "tile layer call not found"
    assert 'referrerPolicy: "strict-origin-when-cross-origin"' in match.group(0)
    assert "OpenStreetMap" in match.group(0), "attribution must stay"
